#!/usr/bin/env python3
"""Downloader agent for the YouTube channel tracker.

Runs on a real server (VPS / Oracle Cloud free VM). Every few minutes it asks
the Cloudflare Worker for queued videos, downloads each with yt-dlp, uploads
the file to R2 (optional), and reports the result back to the Worker page.

Two ways to run:
  * VPS / always-on server: runs as a loop, checking every POLL_SECONDS.
  * Render (free web service): when PORT is set, it runs a small web server
    instead. POST /wake (the Worker calls it) starts a pass over the queue;
    finished files are served from GET /files/<name>?k=<FILE_KEY>, and the
    service pings itself while busy so Render doesn't put it to sleep
    mid-download. Render's disk is temporary — download files soon.

Settings come from environment variables (see agent/agent.env.example):
  TRACKER_URL     https://<your-worker>.workers.dev/yt     (required)
  AGENT_TOKEN     same value as the Worker's AGENT_TOKEN   (required)
  OUT_DIR         where files are saved            (default ./downloads)
  POLL_SECONDS    pause between checks; 0 = run once (default 300)
  YTDLP_COOKIES   path to a youtube.com cookies.txt (recommended on a VPS)
  VIDEO_RES       max video height                  (default 1080)
  AGE_LIMIT       skip content the site marks for this age+ (default 17)
  FILE_KEY        Render mode: secret part of file links (default AGENT_TOKEN)
  PUBLIC_URL      Render mode: this service's address (Render sets
                  RENDER_EXTERNAL_URL automatically)
  R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
                  optional — upload to R2 so the Worker page can serve the
                  file; the local copy is then deleted.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TRACKER = os.environ.get("TRACKER_URL", "").rstrip("/")
TOKEN = os.environ.get("AGENT_TOKEN", "")
OUT = Path(os.environ.get("OUT_DIR", "./downloads"))
POLL = int(os.environ.get("POLL_SECONDS", "300"))
RES = os.environ.get("VIDEO_RES", "1080")
AGE_LIMIT = os.environ.get("AGE_LIMIT", "17")
PORT = os.environ.get("PORT")  # set by Render -> web-server mode
FILE_KEY = os.environ.get("FILE_KEY") or TOKEN
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
KEEP_FILES_HOURS = float(os.environ.get("KEEP_FILES_HOURS", "6"))


def cookies_path() -> str | None:
    """yt-dlp rewrites the cookie file when it exits; Render's secret files
    are read-only, so work on a private copy."""
    src = os.environ.get("YTDLP_COOKIES")
    if not src or not Path(src).exists():
        return None
    dst = Path("/tmp/yt-cookies.txt")
    if not dst.exists():
        shutil.copy(src, dst)
    return str(dst)


_log_buf: list[str] = []
_log_lock = threading.Lock()


def log(msg: str) -> None:
    """Print, and queue the line for the Worker's log page."""
    print(msg, flush=True)
    with _log_lock:
        _log_buf.append(time.strftime("%H:%M:%S ") + str(msg)[:400])
        del _log_buf[:-200]


def flush_logs() -> None:
    with _log_lock:
        lines = _log_buf[:]
        _log_buf.clear()
    if not lines:
        return
    try:
        api("/api/log", {"lines": lines})
    except Exception as e:
        print(f"[!] log upload failed: {e}", flush=True)


def log_flusher() -> None:
    while True:
        time.sleep(15)
        flush_logs()


def hello() -> None:
    """Tell the Worker who we are and what we have — shown on the site."""
    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip().splitlines()[0]
        except Exception:
            return "חסר"
    info = {"ytdlp": ver(["yt-dlp", "--version"]), "node": ver(["node", "--version"]),
            "ffmpeg": ver(["ffmpeg", "-version"])[:40], "cookies": bool(cookies_path()),
            "mode": "render" if PORT else "loop", "public": PUBLIC_URL}
    try:
        api("/api/hello", info)
    except Exception as e:
        print(f"[!] hello failed (TRACKER_URL / AGENT_TOKEN?): {e}", flush=True)
    log(f"סוכן עלה: yt-dlp {info['ytdlp']}, node {info['node']}, "
        f"עוגיות {'יש' if info['cookies'] else 'אין!'}")


LAST_ERR = ""  # last failure talking to the Worker — returned on /wake


def api(path: str, body: dict | None = None) -> dict:
    """POST to the Worker. Failures are remembered in LAST_ERR, which the
    agent sends back on /wake — so the site can show why even when the agent
    can't reach the Worker at all (wrong TRACKER_URL, blocked, bad token)."""
    global LAST_ERR
    req = urllib.request.Request(
        TRACKER + path, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                 # Cloudflare may refuse the default "Python-urllib" agent.
                 "User-Agent": "yt-tracker-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        LAST_ERR = ""
        return data
    except urllib.error.HTTPError as e:
        detail = e.read(200).decode("utf-8", "replace").strip()
        LAST_ERR = f"{TRACKER + path} → HTTP {e.code} {detail}"
        raise
    except Exception as e:
        LAST_ERR = f"{TRACKER + path} → {e}"
        raise


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
    cookies = cookies_path()
    if cookies:
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
    log(f"-> מוריד: {job['title']} ({job['url']})")
    OUT.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(ytdlp_cmd(job, OUT), capture_output=True, text=True)
    files = [Path(l.strip()) for l in r.stdout.splitlines()
             if l.strip().startswith(str(OUT)) and Path(l.strip()).is_file()]
    if files:
        f = files[-1]
        try:
            url = upload_r2(f) or ""
        except Exception as e:  # keep the local file, still report success
            log(f"   R2 upload failed: {e}")
            url = ""
        if not url and PORT and PUBLIC_URL:
            url = (f"{PUBLIC_URL}/files/{urllib.parse.quote(f.name)}"
                   f"?k={urllib.parse.quote(FILE_KEY)}")
        note = "" if url else f"נשמר בשרת: {f}"
        if url and PORT and not os.environ.get("R2_BUCKET"):
            note = f"זמני — להוריד בתוך כמה שעות"
        api("/api/report", {"videoId": job["videoId"], "status": "done",
                            "fileUrl": url, "fileName": f.name, "error": note})
        log(f"   ok: {f.name}")
        return
    out = r.stdout + r.stderr
    if "does not pass filter" in out:
        status, err = "skipped", f"קצר מ-{job.get('minMinutes')} דקות"
    elif "age restricted" in out or "age-restricted" in out:
        status, err = "skipped", "הגבלת גיל"
    else:
        errs = [l for l in out.splitlines() if "ERROR" in l or "error" in l]
        status, err = "failed", (errs[-1] if errs else f"yt-dlp exit {r.returncode}")
        for l in (errs[-4:-1] if len(errs) > 1 else []):
            log(f"   {l}")
    api("/api/report", {"videoId": job["videoId"], "status": status, "error": err})
    log(f"   {status}: {err}")


def run_queue() -> int:
    """Process jobs until the queue is empty. Returns how many were handled."""
    done = 0
    while True:
        jobs = api("/api/next?limit=3").get("jobs", [])
        if not jobs:
            return done
        for job in jobs:
            try:
                handle(job)
            except Exception as e:
                log(f"[!] {job.get('videoId')}: {e}")
                try:
                    api("/api/report", {"videoId": job["videoId"], "status": "failed", "error": str(e)[:200]})
                except Exception:
                    pass
            done += 1
            flush_logs()


# ---------- Render (web service) mode ----------

_wake = threading.Event()
_busy = threading.Event()


def cleanup_old_files() -> None:
    cutoff = time.time() - KEEP_FILES_HOURS * 3600
    for f in OUT.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def worker_loop() -> None:
    while True:
        _wake.wait(timeout=POLL if POLL > 0 else 300)
        _wake.clear()
        if not TRACKER or not TOKEN:
            continue  # nothing to do until Render's Environment is fixed
        _busy.set()
        try:
            cleanup_old_files()
            n = run_queue()
            if n:
                log(f"pass done: {n} job(s)")
        except Exception as e:
            log(f"[!] {e}")
        finally:
            _busy.clear()


def keep_awake_loop() -> None:
    """Render sleeps after ~15 min without incoming requests. While working,
    or while fresh files wait to be downloaded, ping our own public URL."""
    while True:
        time.sleep(240)
        fresh = any(f.is_file() and time.time() - f.stat().st_mtime < 1800 for f in OUT.glob("*"))
        if PUBLIC_URL and (_busy.is_set() or fresh):
            try:
                urllib.request.urlopen(PUBLIC_URL + "/ping", timeout=30).read()
            except Exception:
                pass


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/wake"):
            if not _busy.is_set():
                log("קיבלתי קריאת השכמה — בודק את התור")
            _wake.set()
            # "ok", plus the last error reaching the Worker, if any.
            return self._send(200, "ok" + (f"|{LAST_ERR}" if LAST_ERR else ""))
        self._send(404, "not found")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/ping", "/healthz"):
            return self._send(200, "busy" if _busy.is_set() else "idle")
        if u.path == "/wake":
            _wake.set()
            return self._send(200, "ok")
        if u.path.startswith("/files/"):
            key = urllib.parse.parse_qs(u.query).get("k", [""])[0]
            name = urllib.parse.unquote(u.path[len("/files/"):])
            f = OUT / name
            if key != FILE_KEY or "/" in name or not f.is_file():
                return self._send(404, "not found (או שהשרת נרדם והקובץ נמחק)")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4" if f.suffix == ".mp4" else "audio/mpeg")
            self.send_header("Content-Length", str(f.stat().st_size))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(f.name))
            self.end_headers()
            with f.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return
        self._send(404, "not found")

    def do_HEAD(self):
        # Render probes the port with HEAD; answer like GET / without a body.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, fmt, *args):
        line = fmt % args
        if "/ping" not in line and "/healthz" not in line and "HEAD " not in line:
            print("http:", line, flush=True)


def serve() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=log_flusher, daemon=True).start()
    hello()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=keep_awake_loop, daemon=True).start()
    _wake.set()  # woke up — check the queue right away
    log(f"listening on :{PORT} (public: {PUBLIC_URL or '?'})")
    http.server.ThreadingHTTPServer(("0.0.0.0", int(PORT)), Handler).serve_forever()
    return 0


def main() -> int:
    global LAST_ERR
    if not TRACKER or not TOKEN:
        if not PORT:
            sys.exit("Set TRACKER_URL and AGENT_TOKEN (see agent.env.example).")
        # On Render keep serving, so the site can show what's missing.
        LAST_ERR = "חסר TRACKER_URL או AGENT_TOKEN ב-Environment של Render"
        print(LAST_ERR, flush=True)
    if PORT:
        return serve()
    hello()
    while True:
        try:
            run_queue()
        except Exception as e:
            log(f"[!] {e}")
        flush_logs()
        if POLL <= 0:
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    raise SystemExit(main())
