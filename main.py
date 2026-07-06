import os, io, email, subprocess, tempfile, glob, uuid, shutil, sqlite3, threading
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from PIL import Image
import boto3
import yt_dlp

app = FastAPI(title="Storyboard Service")

# --- config (env w Coolify) ---
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_PUBLIC   = os.getenv("MINIO_PUBLIC", "https://minio-api.tomek-n8n.xyz")
S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY", "admin")
S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY", "CHANGE_ME")
BUCKET         = os.getenv("BUCKET", "nca-bucket")
API_KEY        = os.getenv("API_KEY", "")                       # auth dla /storyboard (naglowek x-api-key)
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
DB_PATH        = os.getenv("DB_PATH", "/data/proxies.db")
MAX_PROXY_TRIES = int(os.getenv("MAX_PROXY_TRIES", "3"))        # ile roznych proxy sprobowac zanim fail
AUTO_DISABLE_AFTER = int(os.getenv("AUTO_DISABLE_AFTER", "5"))  # po ilu consec fail wylaczyc proxy
TILE_W, TILE_H = 320, 180

s3 = boto3.client("s3", endpoint_url=MINIO_ENDPOINT,
                  aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY)

# ---------- DB (pula proxy) ----------
_lock = threading.Lock()
_rr = {"i": -1}

def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS proxies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            active INTEGER DEFAULT 1,
            success INTEGER DEFAULT 0,
            fail INTEGER DEFAULT 0,
            consec_fail INTEGER DEFAULT 0)""")
init_db()

def active_proxies():
    with _db() as c:
        return c.execute("SELECT * FROM proxies WHERE active=1 ORDER BY id").fetchall()

def next_proxy(exclude_ids):
    rows = [r for r in active_proxies() if r["id"] not in exclude_ids]
    if not rows:
        return None
    with _lock:
        _rr["i"] = (_rr["i"] + 1) % len(rows)
        return rows[_rr["i"]]

def mark(pid, ok):
    with _db() as c:
        if ok:
            c.execute("UPDATE proxies SET success=success+1, consec_fail=0 WHERE id=?", (pid,))
        else:
            c.execute("UPDATE proxies SET fail=fail+1, consec_fail=consec_fail+1 WHERE id=?", (pid,))
            c.execute("UPDATE proxies SET active=0 WHERE id=? AND consec_fail>=?", (pid, AUTO_DISABLE_AFTER))

# ---------- storyboard core ----------
class Req(BaseModel):
    url: str
    proxy: Optional[str] = None       # override; jak brak -> rotacja z puli
    max_frames: Optional[int] = 20

def fetch_and_process(url, proxy, max_frames):
    tmp = tempfile.mkdtemp()
    try:
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, "socket_timeout": 15}
        if proxy: ydl_opts["proxy"] = proxy
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        video_id = info.get("id") or uuid.uuid4().hex

        cmd = ["yt-dlp", "--socket-timeout", "15", "-f", "sb0", "-o", os.path.join(tmp, "sb.%(ext)s"), url]
        if proxy: cmd += ["--proxy", proxy]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        mh = glob.glob(os.path.join(tmp, "sb.mhtml"))
        if not mh:
            raise RuntimeError(f"sb0 failed: {r.stderr.decode()[:150]}")

        msg = email.message_from_bytes(open(mh[0], "rb").read())
        sheets = [p.get_payload(decode=True) for p in msg.walk()
                  if p.get_content_type().startswith("image/")]
        tiles = []
        for sb in sheets:
            im = Image.open(io.BytesIO(sb))
            cols = max(1, im.width // TILE_W); rows = max(1, im.height // TILE_H)
            for rr in range(rows):
                for cc in range(cols):
                    tiles.append(im.crop((cc*TILE_W, rr*TILE_H, (cc+1)*TILE_W, (rr+1)*TILE_H)))
        n = len(tiles)
        if n == 0:
            raise RuntimeError("no tiles")

        if max_frames and n > max_frames:
            step = n / max_frames
            sel = [int(i*step) for i in range(max_frames)]
        else:
            sel = list(range(n))

        out = []
        for new_i, oi in enumerate(sel):
            buf = io.BytesIO(); tiles[oi].save(buf, "PNG"); buf.seek(0)
            key = f"2026/storyboards/{video_id}/frame_{new_i}.png"
            s3.upload_fileobj(buf, BUCKET, key, ExtraArgs={"ContentType": "image/png"})
            out.append({"idx": new_i, "timestamp": round(oi*(duration/n), 2),
                        "frame_url": f"{MINIO_PUBLIC}/{BUCKET}/{key}"})
        return {"video_id": video_id, "duration": duration, "total_frames": n,
                "returned_frames": len(out), "frames": out}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

@app.get("/health")
def health():
    return {"status": "ok", "active_proxies": len(active_proxies())}

@app.post("/storyboard")
def storyboard(req: Req, x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")

    # override proxy -> jeden strzal
    if req.proxy:
        try:
            return fetch_and_process(req.url, req.proxy, req.max_frames)
        except Exception as e:
            raise HTTPException(400, f"failed: {str(e)[:200]}")

    # rotacja z puli
    pool = active_proxies()
    if not pool:
        # brak proxy -> VM IP (fallback)
        try:
            return fetch_and_process(req.url, None, req.max_frames)
        except Exception as e:
            raise HTTPException(400, f"failed (no proxy, VM IP): {str(e)[:200]}")

    tried, last = set(), ""
    for _ in range(min(MAX_PROXY_TRIES, len(pool))):
        p = next_proxy(tried)
        if not p: break
        tried.add(p["id"])
        try:
            res = fetch_and_process(req.url, p["url"], req.max_frames)
            mark(p["id"], True)
            res["proxy_used"] = p["url"].split("@")[-1]
            return res
        except Exception as e:
            mark(p["id"], False)
            last = str(e)[:150]
    raise HTTPException(400, f"all {len(tried)} proxies failed: {last}")

# ---------- proxy API ----------
def _auth(pwd):
    if pwd != DASHBOARD_PASSWORD:
        raise HTTPException(401, "bad password")

class ProxyAdd(BaseModel):
    proxies: str   # jedna lub wiele linii: http://user:pass@ip:port

@app.get("/api/proxies")
def list_proxies(x_dashboard_password: Optional[str] = Header(default=None)):
    _auth(x_dashboard_password)
    with _db() as c:
        return {"proxies": [dict(r) for r in c.execute("SELECT * FROM proxies ORDER BY id").fetchall()]}

@app.post("/api/proxies")
def add_proxies(body: ProxyAdd, x_dashboard_password: Optional[str] = Header(default=None)):
    _auth(x_dashboard_password)
    added = 0
    with _db() as c:
        for line in body.proxies.splitlines():
            u = line.strip()
            if not u: continue
            try:
                c.execute("INSERT OR IGNORE INTO proxies(url) VALUES(?)", (u,)); added += c.rowcount
            except Exception: pass
    return {"added": added}

@app.delete("/api/proxies/{pid}")
def del_proxy(pid: int, x_dashboard_password: Optional[str] = Header(default=None)):
    _auth(x_dashboard_password)
    with _db() as c:
        c.execute("DELETE FROM proxies WHERE id=?", (pid,))
    return {"ok": True}

@app.post("/api/proxies/{pid}/toggle")
def toggle_proxy(pid: int, x_dashboard_password: Optional[str] = Header(default=None)):
    _auth(x_dashboard_password)
    with _db() as c:
        c.execute("UPDATE proxies SET active=1-active, consec_fail=0 WHERE id=?", (pid,))
    return {"ok": True}

# ---------- dashboard HTML ----------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASH_HTML

DASH_HTML = """<!doctype html><html><head><meta charset=utf-8><title>Storyboard proxies</title>
<style>body{font-family:system-ui;background:#111;color:#eee;max-width:900px;margin:20px auto;padding:0 16px}
input,textarea,button{background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:8px;font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:16px}td,th{border-bottom:1px solid #333;padding:6px 8px;text-align:left;font-size:13px}
.on{color:#4ade80}.off{color:#f87171}button{cursor:pointer}</style></head><body>
<h2>Storyboard — pula proxy</h2>
<div><input id=pw type=password placeholder="dashboard password" style=width:240px> <button onclick=load()>Zaloguj / odswiez</button></div>
<h3>Dodaj proxy (jedna na linie: http://user:pass@ip:port)</h3>
<textarea id=add rows=4 style=width:100%></textarea><br><button onclick=addP()>Dodaj</button>
<table id=tbl><thead><tr><th>id</th><th>proxy</th><th>active</th><th>ok</th><th>fail</th><th>consec</th><th></th></tr></thead><tbody></tbody></table>
<script>
const H=()=>({'x-dashboard-password':document.getElementById('pw').value,'Content-Type':'application/json'});
async function load(){let r=await fetch('/api/proxies',{headers:H()});if(!r.ok){alert('bad password');return}
let d=await r.json();let tb=document.querySelector('#tbl tbody');tb.innerHTML='';
for(const p of d.proxies){let tr=document.createElement('tr');
tr.innerHTML=`<td>${p.id}</td><td>${p.url}</td><td class=${p.active?'on':'off'}>${p.active?'ON':'OFF'}</td><td>${p.success}</td><td>${p.fail}</td><td>${p.consec_fail}</td>
<td><button onclick="tog(${p.id})">toggle</button> <button onclick="del(${p.id})">x</button></td>`;tb.appendChild(tr);}}
async function addP(){await fetch('/api/proxies',{method:'POST',headers:H(),body:JSON.stringify({proxies:document.getElementById('add').value})});document.getElementById('add').value='';load();}
async function del(id){await fetch('/api/proxies/'+id,{method:'DELETE',headers:H()});load();}
async function tog(id){await fetch('/api/proxies/'+id+'/toggle',{method:'POST',headers:H()});load();}
</script></body></html>"""
