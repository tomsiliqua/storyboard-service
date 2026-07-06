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

    # rotacja z puli — NIGDY nie uzywamy VM/domowego IP
    pool = active_proxies()
    if not pool:
        raise HTTPException(503, "no active proxy in pool — odmawiam uzycia IP serwera/domowego")

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

DASH_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Storyboard Service</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0e17;color:#e5e7eb;font-size:14px}
.nav{background:#0d1220;border-bottom:1px solid #1e2536;padding:14px 24px;display:flex;align-items:center;gap:12px}
.logo{font-size:18px;font-weight:600}
.badge{background:#1e2536;color:#8b93a7;font-size:11px;padding:2px 8px;border-radius:6px}
.wrap{max-width:1000px;margin:24px auto;padding:0 24px;display:flex;flex-direction:column;gap:20px}
.card{background:#131823;border:1px solid #1e2536;border-radius:12px;padding:20px 24px}
.card h2{font-size:15px;font-weight:600;margin-bottom:16px}
label{display:block;font-size:12px;color:#8b93a7;margin-bottom:6px}
input,textarea{width:100%;background:#0a0e17;border:1px solid #262d3d;border-radius:8px;padding:10px 12px;color:#e5e7eb;font-size:13px;font-family:inherit}
input:focus,textarea:focus{outline:none;border-color:#3b82f6}
.btn{background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:10px 18px;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap}
.btn:hover{background:#2563eb}
.btn.sm{padding:5px 12px;font-size:12px}
.btn.ghost{background:transparent;border:1px solid #262d3d;color:#8b93a7}
.btn.ghost:hover{background:#1a1f2e}
.btn.red{background:transparent;border:1px solid #3d2626;color:#ef4444}
.btn.red:hover{background:#2a1818}
.row{display:flex;gap:10px;align-items:flex-end}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;color:#8b93a7;text-transform:uppercase;letter-spacing:.4px;padding:8px 10px;border-bottom:1px solid #1e2536}
td{padding:10px;border-bottom:1px solid #161b28;font-size:13px;vertical-align:middle}
.pill{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:500}
.pill.on{background:#0f2a1a;color:#22c55e}
.pill.off{background:#2a1515;color:#ef4444}
.muted{color:#8b93a7;font-size:12px}
.empty{color:#8b93a7;text-align:center;padding:24px}
.mono{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px}
</style></head><body>
<div class=nav><span class=logo>Storyboard</span><span class=badge>v1</span><span class=muted style=margin-left:auto id=stat></span></div>
<div class=wrap>

<div class=card>
<h2>Dostęp</h2>
<div class=row><div style=flex:1><label>Dashboard password</label><input id=pw type=password placeholder="hasło"></div><button class=btn onclick=load()>Zaloguj</button></div>
</div>

<div class=card>
<h2>Test storyboard</h2>
<label>YouTube URL</label>
<div class=row><div style=flex:1><input id=turl placeholder="https://www.youtube.com/watch?v=..."></div><button class=btn onclick=test()>Pobierz klatki</button></div>
<div id=tres class=muted style=margin-top:14px></div>
</div>

<div class=card>
<h2>Dodaj proxy</h2>
<label>Jedna na linię — http://user:pass@ip:port</label>
<textarea id=add rows=4 placeholder="http://user:pass@1.2.3.4:8080"></textarea>
<div style=margin-top:12px><button class=btn onclick=addP()>Dodaj do puli</button></div>
</div>

<div class=card>
<h2>Pula proxy — rotacja</h2>
<table><thead><tr><th>ID</th><th>Proxy</th><th>Status</th><th>OK</th><th>Fail</th><th>Consec</th><th></th></tr></thead>
<tbody id=tb><tr><td colspan=7 class=empty>Zaloguj się, aby zobaczyć pulę</td></tr></tbody></table>
</div>

</div>
<script>
const H=()=>({'x-dashboard-password':document.getElementById('pw').value,'Content-Type':'application/json'});
async function load(){let r=await fetch('/api/proxies',{headers:H()});if(!r.ok){alert('Złe hasło');return}
let d=await r.json();let tb=document.getElementById('tb');
document.getElementById('stat').textContent=d.proxies.filter(p=>p.active).length+' / '+d.proxies.length+' proxy aktywnych';
if(!d.proxies.length){tb.innerHTML='<tr><td colspan=7 class=empty>Brak proxy — dodaj powyżej</td></tr>';return}
tb.innerHTML='';for(const p of d.proxies){let tr=document.createElement('tr');
tr.innerHTML='<td class=muted>'+p.id+'</td><td class=mono>'+p.url+'</td><td><span class="pill '+(p.active?'on':'off')+'">'+(p.active?'AKTYWNE':'OFF')+'</span></td><td>'+p.success+'</td><td>'+p.fail+'</td><td>'+p.consec_fail+'</td><td style=text-align:right><button class="btn ghost sm" onclick="tog('+p.id+')">toggle</button> <button class="btn red sm" onclick="del('+p.id+')">usuń</button></td>';tb.appendChild(tr);}}
async function addP(){let v=document.getElementById('add').value;if(!v.trim())return;await fetch('/api/proxies',{method:'POST',headers:H(),body:JSON.stringify({proxies:v})});document.getElementById('add').value='';load();}
async function del(id){await fetch('/api/proxies/'+id,{method:'DELETE',headers:H()});load();}
async function tog(id){await fetch('/api/proxies/'+id+'/toggle',{method:'POST',headers:H()});load();}
async function test(){let u=document.getElementById('turl').value;let el=document.getElementById('tres');if(!u)return;el.textContent='Pobieram klatki...';
let r=await fetch('/storyboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u,max_frames:20})});
let d=await r.json();
if(d.frames){el.innerHTML='<span style=color:#22c55e>&#10003;</span> '+d.returned_frames+' klatek &bull; film '+d.duration+'s &bull; '+(d.proxy_used?'proxy '+d.proxy_used:'VM IP')+'<br><span class=mono>'+d.frames.slice(0,6).map(f=>'t='+f.timestamp+'s').join('&nbsp;&nbsp;')+'</span>';}
else{el.innerHTML='<span style=color:#ef4444>Blad: '+(d.detail||JSON.stringify(d))+'</span>';}}
</script></body></html>"""
