"""YT Tracker — Windows desktop app.

* Opens the tracker site (/yt/) in its own window — manage channels, the
  queue and logs without a browser.
* In the background, downloads every finished file straight into a folder
  on this computer (no browser download limits), retries on errors, and
  can unzip automatically. If a file is gone from the download server
  (Render wipes its disk on restart), the song goes back to the queue.
* Reports what it did to the site's Logs tab as [pc].

Settings live in %APPDATA%\\YTTracker\\config.json (first run asks for them).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from pathlib import Path

try:
    # Use Windows' own certificate store, so HTTPS works behind filters such
    # as NetFree that install their own root certificate.
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import requests

APP = "YTTracker"
DATA = Path(os.environ.get("APPDATA") or Path.home()) / APP
CONFIG = DATA / "config.json"
STATE = DATA / "state.json"
LOGFILE = DATA / "app.log"
DEFAULT_SITE = "https://my-gemini.g47ben.workers.dev"
# Where new versions are published (GitHub release built by desktop.yml).
RELEASE_API = "https://api.github.com/repos/shmuel-sinay/YouTube-downloads/releases/tags/desktop-latest"
UPDATE_CHECK_HOURS = 6
POLL_SECONDS = 20
WAKE_EVERY = 300          # nudge the download server while work is waiting
RETRY_404_HOURS = 24      # re-queue a vanished file only if it finished recently


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- setup

def icon_path() -> str:
    """icon.ico next to the script, or inside the PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return str(base / "icon.ico")


def set_tk_icon(root) -> None:
    try:
        root.iconbitmap(icon_path())
    except Exception:
        pass


def first_run_setup(cfg: dict) -> dict:
    """Ask for the site, password and folder (small Tk dialogs)."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    root = tk.Tk()
    set_tk_icon(root)
    root.withdraw()
    site = simpledialog.askstring(APP, "כתובת האתר:", initialvalue=cfg.get("site") or DEFAULT_SITE, parent=root)
    if not site:
        sys.exit(0)
    password = simpledialog.askstring(APP, "הסיסמה של דף הערוצים (ADMIN_PASSWORD):", show="*", parent=root)
    if not password:
        sys.exit(0)
    messagebox.showinfo(APP, "עכשיו בחר תיקייה שאליה יירדו הקבצים.", parent=root)
    folder = filedialog.askdirectory(title="תיקיית הורדות", initialdir=str(Path.home() / "Downloads"), parent=root)
    if not folder:
        sys.exit(0)
    unzip = messagebox.askyesno(APP, "לפתוח את קבצי ה-zip אוטומטית אחרי ההורדה?\n(כן = יישאר רק השיר/הסרטון עצמו)", parent=root)
    old = messagebox.askyesno(APP, "להוריד גם קבצים שכבר מוכנים עכשיו בתור?\n(לא = רק מה שיסתיים מעכשיו)", parent=root)
    root.destroy()
    cfg.update(site=site.rstrip("/"), password=password, folder=folder, unzip=unzip,
               include_existing=old, since="" if old else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    save_json(CONFIG, cfg)
    return cfg


# ---------------------------------------------------------------- downloader

class Downloader(threading.Thread):
    def __init__(self, cfg: dict):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.base = cfg["site"].rstrip("/") + "/yt"
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "yt-tracker-desktop/1.0"
        self.state = load_json(STATE, {"got": [], "requeued": {}})
        self.pending_logs: list[str] = []
        self.last_wake = 0.0
        self.last_update_check = time.time()

    # -- helpers
    def log(self, msg: str, remote: bool = True) -> None:
        line = time.strftime("%H:%M:%S ") + msg
        try:
            with LOGFILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if remote:
            self.pending_logs.append(msg)

    def flush_logs(self) -> None:
        if not self.pending_logs:
            return
        lines, self.pending_logs = self.pending_logs[:], []
        try:
            self.s.post(self.base + "/clientlog", json={"lines": lines}, timeout=20)
        except Exception:
            pass

    def login(self) -> bool:
        r = self.s.post(self.base + "/login", data={"password": self.cfg["password"]},
                        allow_redirects=False, timeout=30)
        ok = r.status_code in (302, 303) and "auth" in self.s.cookies
        self.log("התחברתי לאתר" if ok else f"התחברות נכשלה (HTTP {r.status_code}) — בדוק את הסיסמה", remote=ok)
        return ok

    def status(self) -> dict:
        r = self.s.get(self.base + "/status", timeout=30)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
            # Cookie expired / wrong password → the login page comes back.
            if not self.login():
                raise RuntimeError("לא מחובר")
            r = self.s.get(self.base + "/status", timeout=30)
        return r.json()

    def save_state(self) -> None:
        self.state["got"] = self.state["got"][-2000:]
        save_json(STATE, self.state)

    # -- work
    def download(self, job: dict) -> None:
        folder = Path(self.cfg["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        url = job["fileUrl"]
        name = job.get("fileName") or urllib.parse.unquote(urllib.parse.urlparse(url).path.split("/")[-1])
        name = "".join(c for c in name if c not in '<>:"/\\|?*').strip() or (job["videoId"] + ".zip")
        dest = folder / name
        part = dest.with_name(dest.name + ".part")
        with self.s.get(url, stream=True, timeout=(30, 300)) as r:
            if r.status_code == 404:
                return self.vanished(job)
            r.raise_for_status()
            with part.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        part.replace(dest)
        final = dest
        if job.get("pcConvert") and dest.suffix.lower() == ".zip":
            final = self.convert_to_mp3(dest, folder)
        elif self.cfg.get("unzip") and dest.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(dest) as z:
                    names = z.namelist()
                    z.extractall(folder)
                dest.unlink()
                final = folder / names[0] if names else folder
            except zipfile.BadZipFile:
                pass  # keep the file as it is
        self.state["got"].append(job["videoId"])
        self.save_state()
        self.log(f"ירד למחשב: {final.name}")

    def convert_to_mp3(self, zpath: Path, folder: Path) -> Path:
        """The server sent the original audio (no MP3 encode, to save its
        slow CPU): unzip and convert here — seconds on a PC."""
        tmp = Path(tempfile.mkdtemp(prefix="yt-"))
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(tmp)
            src = next(p for p in tmp.rglob("*") if p.is_file())
            out = folder / (src.stem + ".mp3")
            t0 = time.time()
            ok = ffmpeg_mp3(src, out)
            if not ok:
                # Couldn't convert: keep the original audio rather than lose it.
                out = folder / src.name
                shutil.move(str(src), out)
                self.log(f"המרה ל-MP3 נכשלה — נשמר הקובץ המקורי: {out.name}")
            else:
                self.log(f"הומר ל-MP3 במחשב ({time.time() - t0:.0f} שנ׳)")
            zpath.unlink(missing_ok=True)
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def vanished(self, job: dict) -> None:
        """The server lost the file (restart/deploy). Ask for it again once."""
        vid = job["videoId"]
        age_h = (time.time() - _parse_iso(job.get("updated"))) / 3600
        if vid in self.state["requeued"] or age_h > RETRY_404_HOURS:
            self.state["got"].append(vid)  # give up quietly on this one
            self.save_state()
            self.log(f"הקובץ כבר לא קיים בשרת, מדלג: {job.get('title')}")
            return
        self.s.post(self.base + "/jobs/retry", data={"id": vid}, timeout=30)
        self.state["requeued"][vid] = time.time()
        self.save_state()
        self.log(f"הקובץ נמחק בשרת — החזרתי לתור: {job.get('title')}")

    def run(self) -> None:
        self.log(f"התוכנה עלתה · תיקייה: {self.cfg['folder']}", remote=False)
        try:
            self.login()
        except Exception as e:
            self.log(f"אין חיבור לאתר: {e}", remote=False)
        while True:
            try:
                st = self.status()
                since = self.cfg.get("since") or ""
                for job in st.get("jobs", []):
                    if job.get("status") != "done" or not job.get("fileUrl"):
                        continue
                    if job["videoId"] in self.state["got"] or (job.get("updated") or "") < since:
                        continue
                    try:
                        self.download(job)
                    except Exception as e:
                        self.log(f"הורדה נכשלה ({job.get('title')}): {e} — אנסה שוב")
                # Keep the download server awake while work is waiting.
                waiting = any(j.get("status") in ("queued", "taken") for j in st.get("jobs", []))
                if waiting and time.time() - self.last_wake > WAKE_EVERY:
                    self.last_wake = time.time()
                    self.s.post(self.base + "/wake", timeout=90)
            except Exception as e:
                self.log(f"שגיאה: {e}", remote=False)
            self.check_for_update()
            self.flush_logs()
            time.sleep(POLL_SECONDS)

    def check_for_update(self) -> None:
        """While running, just announce a new version; it installs itself
        the next time the app is opened."""
        if not getattr(sys, "frozen", False) or time.time() - self.last_update_check < UPDATE_CHECK_HOURS * 3600:
            return
        self.last_update_check = time.time()
        try:
            sha, _ = latest_release()
            mine = bundled_version()
            if sha and mine and not sha.startswith(mine) and not mine.startswith(sha):
                self.log("יש גרסה חדשה לתוכנה — היא תותקן אוטומטית בפתיחה הבאה")
        except Exception:
            pass


def ffmpeg_exe() -> str:
    """ffmpeg shipped inside the app (imageio-ffmpeg), else one on PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_mp3(src: Path, out: Path) -> bool:
    """Best-quality VBR MP3, keeping tags and the cover picture if present."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no black console window
    base = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    tries = [
        ["-map", "0:a", "-map", "0:v?", "-c:a", "libmp3lame", "-q:a", "0", "-c:v", "copy",
         "-disposition:v", "attached_pic", "-id3v2_version", "3", "-map_metadata", "0"],
        ["-vn", "-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3", "-map_metadata", "0"],
    ]
    for opts in tries:
        r = subprocess.run(base + opts + [str(out)], capture_output=True, creationflags=flags)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True
    out.unlink(missing_ok=True)
    return False


def _parse_iso(s) -> float:
    try:
        return time.mktime(time.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return 0.0


# ---------------------------------------------------------------- window

def open_window(cfg: dict) -> None:
    url = cfg["site"].rstrip("/") + "/yt/"
    try:
        import webview  # pywebview — uses Edge WebView2, built into Windows 10/11

        webview.create_window("מעקב ערוצים", url, width=1100, height=800, min_size=(420, 500))
        # private_mode=False + storage_path: the site's login is remembered.
        webview.start(private_mode=False, storage_path=str(DATA / "web"))
    except Exception:
        # No WebView2: use the normal browser, keep a small window open so
        # the downloader keeps running until it's closed.
        traceback.print_exc()
        webbrowser.open(url)
        import tkinter as tk
        root = tk.Tk()
        set_tk_icon(root)
        root.title(APP)
        tk.Label(root, text="התוכנה מורידה ברקע לתיקייה:\n" + cfg["folder"] +
                 "\n\nסגירת החלון עוצרת את ההורדות.", padx=20, pady=20).pack()
        tk.Button(root, text="פתח את האתר", command=lambda: webbrowser.open(url)).pack(pady=(0, 6))
        tk.Button(root, text="פתח את התיקייה", command=lambda: os.startfile(cfg["folder"])).pack(pady=(0, 16))
        root.mainloop()


# ---------------------------------------------------------------- updates

def bundled_version() -> str:
    """Commit this build was made from (version.txt, written by the build)."""
    try:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        return (base / "version.txt").read_text().strip()
    except Exception:
        return ""


def latest_release() -> tuple[str, str]:
    """(commit, exe download URL) of the newest published build."""
    r = requests.get(RELEASE_API, timeout=15, headers={"User-Agent": "yt-tracker-desktop"})
    r.raise_for_status()
    rel = r.json()
    sha = rel.get("target_commitish", "")
    url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == "YTTracker.exe"), "")
    zurl = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == "YTTracker.zip"), "")
    return sha, url or zurl


def self_update() -> bool:
    """If a newer build exists, download it, swap it in and restart.
    Windows can't overwrite a running .exe but can rename it, so: rename
    current → .old, put the new one in its place, start it, exit."""
    if not getattr(sys, "frozen", False):
        return False                     # running from source — nothing to do
    exe = Path(sys.executable)
    exe.with_suffix(".old.exe").unlink(missing_ok=True)   # leftover from last update
    mine = bundled_version()
    try:
        sha, url = latest_release()
    except Exception:
        return False                     # offline / blocked — try next time
    if not sha or not url or not mine or sha.startswith(mine) or mine.startswith(sha):
        return False
    import tkinter as tk
    root = tk.Tk()
    set_tk_icon(root)
    root.title(APP)
    tk.Label(root, text="מוריד גרסה חדשה של התוכנה…", padx=30, pady=24).pack()
    root.update()
    try:
        tmp = exe.with_name(exe.stem + ".new" + (".zip" if url.endswith(".zip") else ".exe"))
        with requests.get(url, stream=True, timeout=(30, 300), headers={"User-Agent": "yt-tracker-desktop"}) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if tmp.suffix == ".zip":         # the .exe was blocked; the zip wasn't
            with zipfile.ZipFile(tmp) as z:
                data = z.read("YTTracker.exe")
            tmp.unlink()
            tmp = exe.with_name(exe.stem + ".new.exe")
            tmp.write_bytes(data)
        if tmp.stat().st_size < 5_000_000:
            raise RuntimeError("קובץ העדכון קטן מדי — כנראה נחסם")
        exe.replace(exe.with_suffix(".old.exe"))
        tmp.replace(exe)
        root.destroy()
        subprocess.Popen([str(exe)] + sys.argv[1:], close_fds=True)
        return True
    except Exception as e:
        root.destroy()
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + f"עדכון נכשל: {e}\n")
        if not exe.exists() and exe.with_suffix(".old.exe").exists():
            exe.with_suffix(".old.exe").replace(exe)   # put the working one back
        return False


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if self_update():
        return                           # the new version is starting
    cfg = load_json(CONFIG, {})
    if "--setup" in sys.argv or not all(cfg.get(k) for k in ("site", "password", "folder")):
        cfg = first_run_setup(cfg)
    Downloader(cfg).start()
    open_window(cfg)


if __name__ == "__main__":
    main()
