#!/usr/bin/env python3
"""Downloader agent for the YouTube channel tracker.

Runs on a real server (VPS / Oracle Cloud free VM). Every few minutes it asks
the Cloudflare Worker for queued videos, downloads each with yt-dlp, uploads
the file to R2 (optional), and reports the result back to the Worker page.

Settings come from environment variables (see agent/agent.env.example):
  TRACKER_URL     https://<your-worker>.workers.dev        (required)
  AGENT_TOKEN     same value as the Worker's AGENT_TOKEN   (required)
  OUT_DIR         where files are saved            (default ./downloads)
  POLL_SECONDS    pause between checks; 0 = run once (default 300)
  YTDLP_COOKIES   path to a youtube.com cookies.txt (recommended on a VPS)
  VIDEO_RES       max video height                  (default 1080)
  AGE_LIMIT       skip content the site marks for this age+ (default 17)
  R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
                  optional — upload to R2 so the Worker page can serve the
                  file; the local copy is then deleted.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TRACKER = os.environ.get("TRACKER_URL", "").rstrip("/")
TOKEN = os.environ.get("AGENT_TOKEN", "")
OUT = Path(os.environ.get("OUT_DIR", "./downloads"))
POLL = int(os.environ.get("POLL_SECONDS", "300"))
RES = os.environ.get("VIDEO_RES", "1080")
AGE_LIMIT = os.environ.get("AGE_LIMIT", "17")


def api(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        TRACKER + path, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def ytdlp_cmd(job: dict, out_dir: Path) -> list[str]:
    """yt-dlp with the free anti-bot stack learned in the zmani project:
    cookies, Node for the JS challenge (yt-dlp[default] ships the solver),
    Chrome TLS impersonation, and the bgutil PO-Token plugin (sidecar)."""
    cmd = ["yt-dlp", job["url"], "--no-playlist",
           "--retries", "8", "--fragment-retries", "8", "--extractor-retries", "3",
           "--output", str(out_dir / "%(title)s [%(id)s].%(ext)s"),
           "--print", "after_move:filepath",
           "--no-warnings", "--no-quiet", "--no-progress",
           "--js-runtimes", "node", "--impersonate", "chrome",
           "--extractor-args", "youtube:player_client=web,ios,android",
           "--embed-thumbnail", "--embed-metadata"]
    if job.get("mode") == "video":
        cmd += ["-f", f"bv*[height<={RES}]+ba/b[height<={RES}]/bv*+ba/b",
                "--merge-output-format", "mp4", "--remux-video", "mp4"]
    else:
        cmd += ["-f", "bestaudio/best", "--extract-audio",
                "--audio-format", "mp3", "--audio-quality", "0"]
    if job.get("minMinutes"):
        cmd += ["--match-filter", f"duration >= {int(job['minMinutes']) * 60}"]
    if AGE_LIMIT:
        cmd += ["--age-limit", AGE_LIMIT]
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies and Path(cookies).exists():
        cmd += ["--cookies", cookies]
    return cmd


def upload_r2(path: Path) -> str | None:
    """Upload to R2; return the Worker URL that serves it, or None."""
    if not os.environ.get("R2_BUCKET"):
        return None
    import boto3  # only needed when R2 is configured
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    key = path.name
    ctype = "video/mp4" if path.suffix == ".mp4" else "audio/mpeg"
    s3.upload_file(str(path), os.environ["R2_BUCKET"], key, ExtraArgs={"ContentType": ctype})
    path.unlink(missing_ok=True)
    return TRACKER + "/files/" + urllib.request.quote(key)


def handle(job: dict) -> None:
    print(f"-> {job['title']} ({job['url']})", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(ytdlp_cmd(job, OUT), capture_output=True, text=True)
    files = [Path(l.strip()) for l in r.stdout.splitlines()
             if l.strip().startswith(str(OUT)) and Path(l.strip()).is_file()]
    if files:
        f = files[-1]
        try:
            url = upload_r2(f) or ""
        except Exception as e:  # keep the local file, still report success
            print(f"   R2 upload failed: {e}", flush=True)
            url = ""
        api("/api/report", {"videoId": job["videoId"], "status": "done",
                            "fileUrl": url, "fileName": f.name,
                            "error": "" if url else f"נשמר בשרת: {f}"})
        print(f"   ok: {f.name}", flush=True)
        return
    out = r.stdout + r.stderr
    if "does not pass filter" in out:
        status, err = "skipped", f"קצר מ-{job.get('minMinutes')} דקות"
    elif "age restricted" in out or "age-restricted" in out:
        status, err = "skipped", "הגבלת גיל"
    else:
        errs = [l for l in out.splitlines() if "ERROR" in l or "error" in l]
        status, err = "failed", (errs[-1] if errs else f"yt-dlp exit {r.returncode}")
    api("/api/report", {"videoId": job["videoId"], "status": status, "error": err})
    print(f"   {status}: {err}", flush=True)


def main() -> int:
    if not TRACKER or not TOKEN:
        sys.exit("Set TRACKER_URL and AGENT_TOKEN (see agent.env.example).")
    while True:
        try:
            jobs = api("/api/next?limit=3").get("jobs", [])
            for job in jobs:
                handle(job)
            if jobs:
                continue  # more may be waiting — ask again right away
        except Exception as e:
            print(f"[!] {e}", flush=True)
        if POLL <= 0:
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    raise SystemExit(main())
