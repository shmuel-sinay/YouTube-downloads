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

import hashlib
import http.server
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

TRACKER = os.environ.get("TRACKER_URL", "").rstrip("/")
TOKEN = os.environ.get("AGENT_TOKEN", "")
OUT = Path(os.environ.get("OUT_DIR", "./downloads"))
POLL = int(os.environ.get("POLL_SECONDS", "300"))
RES = os.environ.get("VIDEO_RES", "1080")
AGE_LIMIT = os.environ.get("AGE_LIMIT", "17")
PORT = os.environ.get("PORT")  # set by Render -> web-server mode
# Secret part of file links. Derived from AGENT_TOKEN (never the token
# itself), so a shared or logged file link doesn't reveal the agent's key.
FILE_KEY = os.environ.get("FILE_KEY") or hashlib.sha256(("files:" + TOKEN).encode()).hexdigest()[:24]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
KEEP_FILES_HOURS = float(os.environ.get("KEEP_FILES_HOURS", "6"))


_EXT_COOKIES = {"checked": 0.0, "text": "", "at": ""}


def cookies_path() -> str | None:
    """yt-dlp rewrites the cookie file when it exits; Render's secret files
    are read-only, so yt-dlp always gets a fresh private copy."""
    # 1. fresh cookies from the browser extension (via the Worker), checked
    #    at most every 2 minutes
    if time.time() - _EXT_COOKIES["checked"] > 120:
        _EXT_COOKIES["checked"] = time.time()
        try:
            d = api("/api/cookies", {})
            if d.get("text"):
                _EXT_COOKIES.update(text=d["text"], at=d.get("at", ""))
        except Exception:
            pass
    if _EXT_COOKIES["text"]:
        dst = Path("/tmp/yt-cookies.txt")
        dst.write_text(_EXT_COOKIES["text"])
        return str(dst)
    # 2. the cookies.txt secret file in Render
    src = os.environ.get("YTDLP_COOKIES")
    if not src or not Path(src).exists():
        return None
    # Copy on every use: a cookie file updated in Render must take effect
    # without restarting the server (an old copy kept "not a bot" errors alive).
    dst = Path("/tmp/yt-cookies.txt")
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
            "pot": pot_server() if os.environ.get("USE_POT") == "1" else "כבוי", "warp": warp_up(wait=30), "mode": "render" if PORT else "loop", "public": PUBLIC_URL}
    try:
        api("/api/hello", info)
    except Exception as e:
        print(f"[!] hello failed (TRACKER_URL / AGENT_TOKEN?): {e}", flush=True)
    log(f"סוכן עלה: yt-dlp {info['ytdlp']}, node {info['node']}, "
        f"עוגיות {('מהתוסף' if _EXT_COOKIES['text'] else 'יש') if info['cookies'] else 'אין!'}, שרת טוקנים {info['pot'] or 'לא פעיל!'}, "
        f"WARP {'פעיל' if info['warp'] else 'לא פעיל'}")
    # Show the helpers' own output when they didn't come up (Render's logs
    # aren't reachable from the site).
    for ok, name, path in ((info["pot"], "שרת טוקנים", "/tmp/pot.log"), (info["warp"], "WARP", "/tmp/warp.log")):
        if not ok and Path(path).exists():
            tail = Path(path).read_text(errors="ignore").strip().splitlines()[-5:]
            for line in tail:
                log(f"   [{name}] {line[:300]}")


WARP_PROXY = "socks5://127.0.0.1:40000"


def warp_up(wait: int = 0) -> bool:
    """Is the WARP proxy (start.sh → wireproxy) listening? Optionally wait for
    it — registering with Cloudflare takes a few seconds after startup."""
    import socket
    for i in range(max(1, wait // 2)):
        try:
            socket.create_connection(("127.0.0.1", 40000), timeout=1).close()
            return True
        except OSError:
            if wait:
                time.sleep(2)
    return False


def net_args() -> list[str]:
    """Route yt-dlp through Cloudflare WARP when it's running (YouTube sees a
    Cloudflare address, not Render's). The PO-token plugin passes the same
    proxy on to the token server."""
    # --force-ipv4: WARP has IPv4 and IPv6; YouTube binds the video URL to the
    # address that asked for it, and a v6 request + v4 download = 403.
    return ["--proxy", WARP_PROXY, "--force-ipv4"] if warp_up() else ["--force-ipv4"]


def pot_server() -> str:
    """Version of the bgutil PO-token server on 127.0.0.1:4416, or "" if it
    isn't answering (it starts together with the agent — give it a moment)."""
    import urllib.request
    for _ in range(10):
        try:
            with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=3) as r:
                return "v" + json.loads(r.read().decode()).get("version", "?")
        except Exception:
            time.sleep(2)
    return ""


LAST_ERR = ""  # last failure talking to the Worker — returned on /wake
_job_profile: dict = {}  # profile of the job being worked on (sent back with every call)


def api(path: str, body: dict | None = None) -> dict:
    """POST to the Worker. Failures are remembered in LAST_ERR, which the
    agent sends back on /wake — so the site can show why even when the agent
    can't reach the Worker at all (wrong TRACKER_URL, blocked, bad token)."""
    global LAST_ERR
    if body is not None and "profile" not in body and _job_profile.get("id"):
        body = {**body, "profile": _job_profile["id"]}   # which profile's job this is
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


def prog_file(vid: str) -> Path:
    return Path("/tmp") / f"ffprog-{vid}.txt"


_LAST_GOOD = {"label": ""}     # the way that last got past YouTube


def _without(cmd: list[str], flag: str) -> list[str]:
    """cmd without `flag` and its value."""
    i = cmd.index(flag)
    return cmd[:i] + cmd[i + 2:]


def _bot_check(r) -> bool:
    out = r.stdout + r.stderr
    return "confirm you" in out and "not a bot" in out


def _refusal(r) -> str:
    """Why YouTube refused this attempt ("" = it didn't)."""
    out = r.stdout + r.stderr
    if "confirm you" in out and "not a bot" in out:
        return "not a bot"
    if "HTTP Error 403" in out:
        return "403"
    if "Requested format is not available" in out:
        return "אין פורמט"
    return ""


def _with_clients(cmd: list[str], clients: str) -> list[str]:
    """cmd with another youtube:player_client list."""
    out = list(cmd)
    for i, a in enumerate(out):
        if a.startswith("youtube:player_client="):
            out[i] = "youtube:player_client=" + clients
    return out


def ytdlp_cmd(job: dict, out_dir: Path) -> list[str]:
    """yt-dlp with the free anti-bot stack learned in the zmani project:
    cookies, Node for the JS challenge (yt-dlp[default] ships the solver),
    Chrome TLS impersonation, and the bgutil PO-Token plugin (sidecar)."""
    cmd = ["yt-dlp", job["url"], "--no-playlist",
           "--retries", "8", "--fragment-retries", "8", "--extractor-retries", "3",
           "--output", str(out_dir / "%(title)s [%(id)s].%(ext)s"),
           "--print", "after_move:filepath",
           "--print", "before_dl:DUR %(duration)s",   # for conversion %
           "--no-warnings", "--no-quiet",
           # One progress line at a time, in a form run_ytdlp() can parse.
           "--progress", "--newline", "--progress-template",
           "download:PROG %(progress._percent_str)s · %(progress._total_bytes_str,progress._total_bytes_estimate_str)s"
           " · %(progress._speed_str)s · %(progress._eta_str)s",
           "--js-runtimes", "node", "--impersonate", "chrome",
           # ios/android ignore cookies (→ "not a bot"); yt-dlp's defaults plus
           # mweb use the cookies and the PO tokens from the bgutil server.
           "--extractor-args", "youtube:player_client=default,mweb",
           "--embed-thumbnail", "--embed-metadata"] + net_args()
    if job.get("mode") == "video":
        cmd += ["-f", f"bv*[height<={RES}]+ba/b[height<={RES}]/bv*+ba/b",
                "--merge-output-format", "mp4", "--remux-video", "mp4"]
    elif job.get("mode") == "m4a" or job.get("pcConvert"):
        # Fast audio: take YouTube's own AAC track as-is (no re-encode).
        # --extract-audio only re-encodes if no m4a track exists.
        cmd += ["-f", "bestaudio[ext=m4a]/bestaudio/best", "--extract-audio",
                "--audio-format", "m4a"]
    else:
        cmd += ["-f", "bestaudio/best", "--extract-audio",
                "--audio-format", "mp3", "--audio-quality", "0"]
    # ffmpeg writes its position to a file while converting/merging; the
    # progress monitor turns it into a percentage of the video's duration.
    pf = str(prog_file(job.get("videoId", "x")))
    for pp in ("ExtractAudio", "Merger", "VideoRemuxer"):
        cmd += ["--ppa", f"{pp}+ffmpeg:-progress {pf} -nostats"]
    if job.get("minMinutes"):
        cmd += ["--match-filter", f"duration >= {int(job['minMinutes']) * 60}"]
    if AGE_LIMIT:
        cmd += ["--age-limit", AGE_LIMIT]
    cookies = cookies_path()
    if cookies:
        cmd += ["--cookies", cookies]
    return cmd


def to_zip(path: Path, vid: str = "") -> Path:
    """Wrap the media file in a .zip and delete the original. Some filters
    (e.g. NetFree) block audio/video downloads but let zip files through.
    ZIP_STORED: media is already compressed, so this is just a fast copy."""
    z = path.with_name(path.stem + ".zip")
    total = max(path.stat().st_size, 1)
    done, last = 0, 0.0
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf, path.open("rb") as src, \
            zf.open(zipfile.ZipInfo.from_file(path, path.name), "w", force_zip64=True) as dst:
        while chunk := src.read(4 << 20):
            dst.write(chunk)
            done += len(chunk)
            if vid and time.time() - last > 1:
                set_progress(vid, stage="zip", pct=round(100 * done / total, 1))
                last = time.time()
    path.unlink(missing_ok=True)
    return z


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
    ctype = "application/zip"
    s3.upload_file(str(path), os.environ["R2_BUCKET"], key, ExtraArgs={"ContentType": ctype})
    path.unlink(missing_ok=True)
    return TRACKER + "/files/" + urllib.request.quote(key)


# Live progress of the job being worked on — served at GET /progress, which
# the site polls to draw a progress panel. Not secret: id, stage and %.
PROGRESS: dict = {}


# Share of the whole job each stage takes (rough, per mode): used for the
# overall percentage. Video downloads come in two parts (video, then audio).
WEIGHTS = {
    "audio": {"download": (0, 50), "convert": (50, 45), "zip": (95, 5)},
    "m4a":   {"download": (0, 88), "convert": (88, 6), "zip": (94, 6)},
    "video": {"download": (0, 85), "convert": (85, 10), "zip": (95, 5)},
}


def set_progress(vid: str, **kw) -> None:
    p = {**PROGRESS.get(vid, {}), **kw, "t": time.time()}
    w = WEIGHTS.get(p.get("mode"), WEIGHTS["audio"]).get(p.get("stage"))
    pct = p.get("pct")
    if p.get("stage") == "finish":
        p["overall"] = 100.0
    elif p.get("stage") == "start":
        p["overall"] = 0.0
    elif w:
        frac = (pct or 0) / 100
        if p.get("stage") == "download" and p.get("mode") == "video":
            # part 1 (video) ≈ 80% of the bytes, part 2 (audio) ≈ 20%
            frac = (0.8 * frac) if p.get("part", 1) <= 1 else (0.8 + 0.2 * frac)
        p["overall"] = round(w[0] + w[1] * frac, 1)
    PROGRESS[vid] = p


_current: dict = {}   # the yt-dlp process being run, and whether it was cancelled


class _Result:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run_ytdlp(cmd: list[str], vid: str = "") -> _Result:
    """Run yt-dlp and report progress to the log while it works: download %
    about once a minute, when conversion starts, and a heartbeat during long
    silent steps (ffmpeg converting an hour-long set on a small CPU)."""
    start = time.time()
    state = {"last_log": start}
    out_lines: list[str] = []
    err_lines: list[str] = []
    done = threading.Event()

    def say(msg: str) -> None:
        log(msg)
        state["last_log"] = time.time()

    def heartbeat() -> None:
        while not done.wait(30):
            if time.time() - state["last_log"] >= 120:
                say(f"   … עדיין עובד ({int((time.time() - start) // 60)} דק׳)")

    # Own process group, so cancelling also stops the ffmpeg it starts.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                            start_new_session=True)
    _current["proc"] = proc
    pf = prog_file(vid) if vid else None
    if pf:
        pf.unlink(missing_ok=True)

    def ffmpeg_monitor() -> None:
        """While converting, read ffmpeg's -progress file → percent of the
        duration yt-dlp printed (DUR …)."""
        while not done.wait(2):
            p = PROGRESS.get(vid, {})
            dur = p.get("duration")
            if p.get("stage") != "convert" or not dur or not pf or not pf.exists():
                continue
            try:
                txt = pf.read_text(errors="ignore")
                us = [int(l.split("=", 1)[1]) for l in txt.splitlines()
                      if l.startswith("out_time_us=") and l.split("=", 1)[1].strip().lstrip("-").isdigit()]
                if us:
                    set_progress(vid, pct=round(min(100.0, max(0.0, us[-1] / 1e6 / dur * 100)), 1))
            except Exception:
                pass

    def cancel_watch() -> None:
        """Every few seconds ask the Worker whether this job is still ours:
        cancelled (✖), paused (⏸) or moved back in line for another (⏫)."""
        while not done.wait(4):
            try:
                st = api("/api/check", {"videoId": vid}).get("status") if vid else "taken"
                if st in ("cancelled", "paused", "queued", "missing"):   # missing = deleted
                    _current["stopped"] = st
                    os.killpg(proc.pid, signal.SIGTERM)
                    return
            except Exception:
                pass

    if vid:
        threading.Thread(target=ffmpeg_monitor, daemon=True).start()
        threading.Thread(target=cancel_watch, daemon=True).start()
    t_err = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    t_err.start()
    threading.Thread(target=heartbeat, daemon=True).start()
    last_prog = 0.0
    parts = 0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("PROG "):
            f = [x.strip() for x in line[5:].split("·")]
            try:
                pct = float(f[0].rstrip("%"))
            except ValueError:
                pct = None
            if vid:
                set_progress(vid, stage="download", pct=pct, size=f[1] if len(f) > 1 else "",
                             speed=f[2] if len(f) > 2 else "", eta=f[3] if len(f) > 3 else "",
                             part=max(parts, 1))
            finished = line.split("·")[0].strip().endswith("100.0%")
            if (finished and not state.get("logged100")) or (not finished and time.time() - last_prog >= 60):
                say("   ⬇ " + line[5:].strip())
                last_prog = time.time()
                state["logged100"] = finished
            continue
        if line.startswith("DUR "):
            try:
                if vid:
                    set_progress(vid, duration=float(line[4:]))
            except ValueError:
                pass  # "NA" — no duration known
            continue
        out_lines.append(line)
        if line.startswith("[download] Destination:"):
            parts += 1  # video+audio downloads come in two parts
        if vid and line.startswith(("[ExtractAudio]", "[Merger]", "[VideoRemuxer]")):
            set_progress(vid, stage="convert", pct=0.0 if PROGRESS.get(vid, {}).get("duration") else None)
        if line.startswith(("[ExtractAudio]", "[Merger]", "[VideoRemuxer]")):
            say("   ⚙ ממיר/מאחד (בשרת חלש זה יכול לקחת כמה דקות)…")
    proc.wait()
    t_err.join(timeout=5)
    done.set()
    if pf:
        pf.unlink(missing_ok=True)
    return _Result(proc.returncode, "\n".join(out_lines), "".join(err_lines))


def handle(job: dict) -> None:
    _job_profile["id"] = job.get("profile") or "main"
    try:
        _handle(job)
    finally:
        PROGRESS.pop(job["videoId"], None)
        _job_profile.clear()


def handle_list(job: dict) -> None:
    """A 'download N videos from this channel' request: list the channel
    with yt-dlp (no download), pick the newest N that fit the filters and
    hand them to the Worker as regular jobs."""
    vid, count, min_s = job["videoId"], int(job.get("count") or 10), int(job.get("minMinutes") or 0) * 60
    log(f"-> מחפש {count} סרטונים: {job.get('channelName')}")
    set_progress(vid, stage="start", pct=None, started=time.time(), mode="m4a")
    cmd = ["yt-dlp", job["url"], "--flat-playlist", "--no-warnings",
           "--playlist-end", str(min(count * (3 if min_s else 1) + 5, 600)),
           "--print", "%(id)s\t%(duration)s\t%(title)s",
           "--js-runtimes", "node", "--impersonate", "chrome"] + net_args()
    cookies = cookies_path()
    if cookies:
        cmd += ["--cookies", cookies]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    items = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3 or len(parts[0]) != 11:
            continue
        try:
            dur = float(parts[1])
        except ValueError:
            dur = None                  # unknown here; the real download still filters
        if min_s and dur is not None and dur < min_s:
            continue
        items.append({"videoId": parts[0], "title": parts[2], "duration": dur})
        if len(items) >= count:
            break
    if not items:
        errs = [l for l in r.stderr.splitlines() if "ERROR" in l]
        api("/api/report", {"videoId": vid, "status": "failed",
                            "error": errs[-1] if errs else "לא נמצאו סרטונים מתאימים"})
        log("   לא נמצאו סרטונים")
        return
    res = api("/api/enqueue", {"items": items, "mode": job.get("mode"), "minMinutes": job.get("minMinutes"),
                                "channelId": job.get("channelId"), "channelName": job.get("channelName")})
    msg = f"נוספו לתור {res.get('added', 0)} סרטונים" + (f" ({res['skipped']} כבר היו)" if res.get("skipped") else "")
    api("/api/report", {"videoId": vid, "status": "done", "error": msg})
    log("   " + msg)


def handle_search(job: dict) -> None:
    """'singer - song' from a pasted list: search YouTube, take the first
    result at least minMinutes long, and replace this job with that video."""
    vid, q, min_s = job["videoId"], job.get("query") or job["title"], int(job.get("minMinutes") or 0) * 60
    log(f"-> מחפש: {q}")
    set_progress(vid, stage="start", pct=None, started=time.time(), mode="m4a")
    cmd = ["yt-dlp", f"ytsearch{20 if min_s else 5}:{q}", "--flat-playlist", "--no-warnings",
           "--print", "%(id)s\t%(duration)s\t%(title)s", "--js-runtimes", "node", "--impersonate", "chrome"] + net_args()
    cookies = cookies_path()
    if cookies:
        cmd += ["--cookies", cookies]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    # A NetFree-only profile gets several candidates: the desktop app checks
    # them in order and keeps the first one NetFree lets through.
    want = max(1, min(int(job.get("candidates") or 1), 8))
    found, matches = None, []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3 or len(parts[0]) != 11:
            continue
        try:
            dur = float(parts[1])
        except ValueError:
            dur = None
        if min_s and (dur is None or dur < min_s):
            continue
        matches.append({"videoId": parts[0], "title": parts[2], "duration": dur})
        if len(matches) >= want:
            break
    found = matches[0] if matches else None
    if not found:
        errs = [l for l in r.stderr.splitlines() if "ERROR" in l]
        why = errs[-1] if errs else ("לא נמצאה תוצאה" + (f" באורך {min_s // 60} דקות לפחות" if min_s else ""))
        api("/api/report", {"videoId": vid, "status": "failed", "error": why})
        log(f"   {why}")
        return
    if want > 1:
        for rank, m in enumerate(matches):
            m.update(group=vid, rank=rank)
    api("/api/enqueue", {"items": matches if want > 1 else [found], "mode": job.get("mode"), "minMinutes": job.get("minMinutes"),
                         "channelName": "חיפוש: " + q, "replace": vid,
                         "priority": (job.get("priority") or 0) + 1})  # download it right away
    log(f"   נמצא: {found['title']}" + (f" (+{len(matches) - 1} חלופות לבדיקת נטפרי)" if want > 1 else ""))


def _handle(job: dict) -> None:
    vid = job["videoId"]
    if job.get("kind") == "list":
        return handle_list(job)
    if job.get("kind") == "search":
        return handle_search(job)
    log(f"-> מוריד: {job['title']} ({job['url']})")
    # pcConvert jobs skip the MP3 encode here — weigh them like fast audio.
    set_progress(vid, stage="start", pct=None, started=time.time(),
                 mode="m4a" if job.get("pcConvert") else job.get("mode", "audio"))
    OUT.mkdir(parents=True, exist_ok=True)
    _current.clear()
    cmd = ytdlp_cmd(job, OUT)
    # YouTube can refuse in two ways: "not a bot" (address/cookies) or 403 on
    # the video data (the client type needs a token for the stream). Try other
    # routes and client types before giving up, and say in the log which
    # one worked.
    nc = _without(cmd, "--cookies") if "--cookies" in cmd else cmd
    # Order = what got through on Render (via WARP): tv_simply first.
    tries = [("לקוח tv_simply (בלי עוגיות)", _with_clients(nc, "tv_simply")),
             ("לקוח android_vr (בלי עוגיות)", _with_clients(nc, "android_vr")),
             ("", cmd),
             ("לקוח tv", _with_clients(cmd, "tv")),
             ("לקוח web_safari", _with_clients(cmd, "web_safari"))]
    if "--proxy" in cmd:
        tries.append(("בלי WARP", _without(cmd, "--proxy")))
    # Start with whatever worked last time (no wasted attempts once a way is found).
    tries.sort(key=lambda t: t[0] != _LAST_GOOD["label"])
    seen = set()
    for n, (label, c) in enumerate(tries):
        if tuple(c) in seen:
            continue
        seen.add(tuple(c))
        if n:
            log(f"   יוטיוב סירב ({why}) — מנסה שוב: {label}")
        r = run_ytdlp(c, vid)
        why = _refusal(r)
        out = r.stdout + r.stderr
        if _current.get("stopped") or r.returncode == 0:
            if r.returncode == 0 and label != _LAST_GOOD["label"]:
                _LAST_GOOD["label"] = label
                log(f"   ✓ עבד: {label or 'ברירת מחדל'} — מעכשיו מתחילים בדרך הזו")
            break
        if "does not pass filter" in out or "age restricted" in out or "age-restricted" in out:
            break                               # not YouTube refusing — no point retrying
        if not why:                              # some other error: show it and try the next way
            errs = [l for l in out.splitlines() if "ERROR" in l]
            why = (errs[-1].split("ERROR:")[-1].strip()[:80] if errs else f"exit {r.returncode}")
    stopped = _current.get("stopped")
    if stopped:
        if stopped in ("cancelled", "missing"):
            for leftover in OUT.glob(f"*[[]{vid}[]]*"):   # partial files of this video
                leftover.unlink(missing_ok=True)
            log(f"   ✖ בוטל: {job['title']}")
        else:
            # The .part file stays; yt-dlp continues from it next time
            # (unless Render restarts in between and wipes the disk).
            log(f"   {'⏸ הושהה' if stopped == 'paused' else '⏭ פינה מקום להורדה אחרת'}: {job['title']}")
        return
    files = [Path(l.strip()) for l in r.stdout.splitlines()
             if l.strip().startswith(str(OUT)) and Path(l.strip()).is_file()]
    if files:
        set_progress(vid, stage="zip", pct=None)
        f = to_zip(files[-1], vid)
        set_progress(vid, stage="finish", pct=100)
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
        # One at a time, so "בהורדה" on the site means it's really in progress.
        jobs = api("/api/next?limit=1").get("jobs", [])
        if not jobs:
            return done
        for job in jobs:
            try:
                handle(job)
            except Exception as e:
                log(f"[!] {job.get('videoId')}: {e}")
                try:
                    api("/api/report", {"videoId": job["videoId"], "status": "failed", "error": str(e)[:200],
                                        "profile": job.get("profile") or "main"})
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
        if u.path == "/progress":
            now = time.time()
            body = json.dumps({"busy": _busy.is_set(), "jobs": {
                k: {**v, "elapsed": int(now - v.get("started", now))} for k, v in PROGRESS.items()}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")  # the site reads it
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
            self.send_header("Content-Type", "application/zip")
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
        line = re.sub(r"([?&]k=)[^ &\"]+", r"\1***", fmt % args)  # hide file keys
        if not any(x in line for x in ("/ping", "/healthz", "/progress", "HEAD ")):
            print("http:", line, flush=True)


def youtube_selftest() -> None:
    """On startup, once: does the first route (WARP + tv_simply) get past
    YouTube? One light check (no download), skipped while a job runs — the
    free server has only 512MB."""
    time.sleep(20)
    if _current.get("proc") and _current["proc"].poll() is None:
        return
    cmd = ["yt-dlp", "https://www.youtube.com/watch?v=jNQXAC9IVRw", "--simulate", "--no-playlist",
           "--no-warnings", "-q", "--js-runtimes", "node", "--impersonate", "chrome",
           "--extractor-args", "youtube:player_client=tv_simply"] + net_args()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        res = "✓" if r.returncode == 0 else "✗" + (" (not a bot)" if "not a bot" in r.stderr else "")
    except Exception:
        res = "✗ (timeout)"
    log(f"בדיקת יוטיוב ({'WARP' if warp_up() else 'ישיר'}, tv_simply): {res}")


def serve() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=log_flusher, daemon=True).start()
    hello()
    threading.Thread(target=youtube_selftest, daemon=True).start()
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
