import os, io, email, subprocess, tempfile, glob, uuid, shutil
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from PIL import Image
import boto3
import yt_dlp

app = FastAPI(title="Storyboard Service")

# --- config z env (ustaw w Coolify) ---
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_PUBLIC   = os.getenv("MINIO_PUBLIC", "https://minio-api.tomek-n8n.xyz")
S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY", "admin")
S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY", "CHANGE_ME")
BUCKET         = os.getenv("BUCKET", "nca-bucket")
API_KEY        = os.getenv("API_KEY", "")          # opcjonalny prosty auth
TILE_W, TILE_H = 320, 180                            # sb0 = kafle 320x180

s3 = boto3.client("s3", endpoint_url=MINIO_ENDPOINT,
                  aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY)


class Req(BaseModel):
    url: str
    proxy: Optional[str] = None          # np. "http://user:pass@ip:port"
    max_frames: Optional[int] = None     # subsample do N klatek (np. 20)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/storyboard")
def storyboard(req: Req, x_api_key: str = ""):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")

    tmp = tempfile.mkdtemp()
    try:
        # 1) metadata (duration, id) — z proxy jesli podany
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        if req.proxy:
            ydl_opts["proxy"] = req.proxy
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
        except Exception as e:
            raise HTTPException(400, f"metadata failed (anti-bot?): {str(e)[:200]}")
        duration = info.get("duration") or 0
        video_id = info.get("id") or uuid.uuid4().hex

        # 2) pobierz sb0 (storyboard) — z proxy
        cmd = ["yt-dlp", "-f", "sb0", "-o", os.path.join(tmp, "sb.%(ext)s"), req.url]
        if req.proxy:
            cmd += ["--proxy", req.proxy]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        mh = glob.glob(os.path.join(tmp, "sb.mhtml"))
        if not mh:
            raise HTTPException(400, f"sb0 download failed: {r.stderr.decode()[:200]}")

        # 3) parse mhtml -> spritesheety
        msg = email.message_from_bytes(open(mh[0], "rb").read())
        sheets = [p.get_payload(decode=True) for p in msg.walk()
                  if p.get_content_type().startswith("image/")]
        if not sheets:
            raise HTTPException(400, "no image parts in storyboard")

        # 4) crop wszystkie kafle do pamieci (bez uploadu)
        tiles = []
        for sheet_bytes in sheets:
            im = Image.open(io.BytesIO(sheet_bytes))
            cols = max(1, im.width // TILE_W)
            rows = max(1, im.height // TILE_H)
            for rr in range(rows):
                for cc in range(cols):
                    box = (cc*TILE_W, rr*TILE_H, (cc+1)*TILE_W, (rr+1)*TILE_H)
                    tiles.append(im.crop(box))
        n_total = len(tiles)
        if n_total == 0:
            raise HTTPException(400, "no tiles")

        # 5) subsample rownomiernie do max_frames (indeksy z calego filmu)
        if req.max_frames and n_total > req.max_frames:
            step = n_total / req.max_frames
            sel = [int(i*step) for i in range(req.max_frames)]
        else:
            sel = list(range(n_total))

        # 6) upload TYLKO wybranych, renumeruj 0..N, timestamp = rzeczywisty (na bazie calego filmu)
        out = []
        for new_i, oi in enumerate(sel):
            buf = io.BytesIO(); tiles[oi].save(buf, "PNG"); buf.seek(0)
            key = f"2026/storyboards/{video_id}/frame_{new_i}.png"
            s3.upload_fileobj(buf, BUCKET, key, ExtraArgs={"ContentType": "image/png"})
            ts = round(oi * (duration / n_total), 2)
            out.append({
                "idx": new_i,
                "timestamp": ts,
                "frame_url": f"{MINIO_PUBLIC}/{BUCKET}/{key}",
            })

        return {
            "video_id": video_id,
            "duration": duration,
            "total_frames": n_total,
            "returned_frames": len(out),
            "frames": out,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
