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
# Shared by every user of this computer: settings, download history, log.
# (Each user's own browser data — the site login — stays per user: USERDATA.)
PROGRAMDATA = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
DATA = PROGRAMDATA / APP
USERDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP
OLD_DATA = Path(os.environ.get("APPDATA") or Path.home()) / APP   # before all-users install
CONFIG = DATA / "config.json"          # shared: site address only
USERCFG = USERDATA / "user.json"       # this Windows user: profile login, folder, vault
STATE = USERDATA / "state.json"        # this user's download history
LOGFILE = USERDATA / "app.log"
TMP = USERDATA / "tmp"                 # decrypted copies for playing — wiped on lock/exit
DEFAULT_SITE = "https://my-gemini.g47ben.workers.dev"
# Where new versions are published (GitHub release built by desktop.yml).
RELEASE_API = "https://api.github.com/repos/shmuel-sinay/YouTube-downloads/releases/tags/desktop-latest"
UPDATE_CHECK_HOURS = 6
POLL_SECONDS = 20
WAKE_EVERY = 300          # nudge the download server while work is waiting
RETRY_404_HOURS = 24      # re-queue a vanished file only if it finished recently


# Per-user choices (each person on the computer decides for themselves).
PREFS = USERDATA / "prefs.json"
NOTIFY_MODES = {"each": "על כל קובץ", "batch": "רק בסיום משימת הורדה", "off": "ללא התראות"}


def get_pref(key: str, default):
    return load_json(PREFS, {}).get(key, default)


def set_pref(key: str, value) -> None:
    prefs = load_json(PREFS, {})
    prefs[key] = value
    save_json(PREFS, prefs)


# ---- the profile password is stored encrypted for this Windows user only
# (DPAPI): another user of the computer can't read it from the file.

def protect(text: str) -> str:
    import base64
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        data = text.encode("utf-8")
        inp = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            raise OSError("CryptProtectData failed")
        enc = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return "dpapi:" + base64.b64encode(enc).decode()
    except (AttributeError, OSError):
        return "plain:" + base64.b64encode(text.encode()).decode()   # not Windows (tests)


def unprotect(blob: str) -> str:
    import base64
    kind, _, b64 = (blob or "").partition(":")
    raw = base64.b64decode(b64 or "")
    if kind == "plain":
        return raw.decode()
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    inp = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    dec = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return dec.decode("utf-8")


def load_cfg() -> dict:
    """Shared site address + this user's own settings (password decrypted)."""
    shared = load_json(CONFIG, {})
    user = load_json(USERCFG, {})
    cfg = {"site": shared.get("site") or DEFAULT_SITE, **{k: v for k, v in user.items() if k != "password_enc"}}
    try:
        cfg["password"] = unprotect(user["password_enc"]) if user.get("password_enc") else ""
    except Exception:
        cfg["password"] = ""
    return cfg


def save_user_cfg(cfg: dict) -> None:
    user = {k: v for k, v in cfg.items() if k not in ("password", "site")}
    if cfg.get("password"):
        user["password_enc"] = protect(cfg["password"])
    save_json(USERCFG, user)
    shared = load_json(CONFIG, {})
    if cfg.get("site") and shared.get("site") != cfg["site"]:
        shared["site"] = cfg["site"]
    shared.pop("password", None)          # never keep a password in the shared file
    shared.pop("folder", None)            # folders are per user now
    save_json(CONFIG, shared)


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
    """Per Windows user: which profile (by its password), where files go,
    and whether to keep them in an encrypted vault."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    root = tk.Tk()
    set_tk_icon(root)
    root.withdraw()
    site = simpledialog.askstring(APP, "כתובת האתר:", initialvalue=cfg.get("site") or DEFAULT_SITE, parent=root)
    if not site:
        sys.exit(0)
    password = simpledialog.askstring(APP, "הסיסמה שלך לאתר (הפרופיל שלך):\n"
                                      "התוכנה תוריד רק את מה שבפרופיל הזה.", show="*", parent=root)
    if not password:
        sys.exit(0)
    vault_dir = ""
    if messagebox.askyesno(APP, "לשמור את הקבצים שלך בכספת מוצפנת?\n\n"
                           "בסייר של Windows יראו רק קבצים מקודדים בלי שמות, "
                           "ופותחים אותם רק בתוכנה עם סיסמה.", parent=root):
        messagebox.showinfo(APP, "בחר תיקייה ריקה לכספת.", parent=root)
        vault_dir = filedialog.askdirectory(title="תיקיית הכספת", initialdir=str(Path.home()), parent=root)
        if vault_dir:
            from vault import Vault
            v = Vault(vault_dir)
            if not v.exists:
                while True:
                    p1 = simpledialog.askstring(APP, "בחר סיסמה לכספת (לפחות 6 תווים):", show="*", parent=root)
                    if not p1:
                        vault_dir = ""
                        break
                    p2 = simpledialog.askstring(APP, "הקלד שוב את סיסמת הכספת:", show="*", parent=root)
                    if p1 == p2 and len(p1) >= 6:
                        v.create(p1)
                        messagebox.showinfo(APP, "הכספת נוצרה. אין דרך לשחזר את הסיסמה אם תישכח — שמור אותה.", parent=root)
                        break
                    messagebox.showwarning(APP, "הסיסמאות לא זהות או קצרות מדי.", parent=root)
    folder = cfg.get("folder") or ""
    if not vault_dir:
        messagebox.showinfo(APP, "בחר תיקייה שאליה יירדו הקבצים.", parent=root)
        default = Path.home() / "Music" / APP
        default.mkdir(parents=True, exist_ok=True)
        folder = filedialog.askdirectory(title="תיקיית הורדות", initialdir=str(default), parent=root) or str(default)
    old = messagebox.askyesno(APP, "להוריד גם קבצים שכבר מוכנים עכשיו בתור?\n(לא = רק מה שיסתיים מעכשיו)", parent=root)
    root.destroy()
    cfg.update(site=site.rstrip("/"), password=password, folder=folder or str(USERDATA / "files"),
               vault_dir=vault_dir, unzip=True, include_existing=old,
               since="" if old else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    save_user_cfg(cfg)
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
        self.state.setdefault("files", {})       # videoId -> where the file was saved
        self.pending_got: list[str] = []         # reported to the site (browser then skips them)
        self.pending_logs: list[str] = []
        self.last_wake = 0.0
        self.last_update_check = time.time()
        self.lock = threading.Lock()          # one download at a time
        self.notify = lambda title, msg: None  # set by the tray icon
        self._seen: set | None = None           # files already announced
        self._batch = 0                         # files in the current "download task"

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
        if not self.pending_logs and not self.pending_got:
            return
        lines, self.pending_logs = self.pending_logs[:], []
        got, self.pending_got = self.pending_got[:], []
        try:
            self.s.post(self.base + "/clientlog", json={"lines": lines, "got": got}, timeout=20)
        except Exception:
            self.pending_got = got + self.pending_got

    def login(self) -> bool:
        r = self.s.post(self.base + "/login", data={"password": self.cfg["password"]},
                        allow_redirects=False, timeout=30)
        ok = r.status_code in (302, 303) and "auth" in self.s.cookies
        self.log("התחברתי לאתר" if ok else f"התחברות נכשלה (HTTP {r.status_code}) — בדוק את הסיסמה", remote=ok)
        return ok

    def status(self) -> dict:
        # "?client=pc" tells the site the app is running, so an open browser
        # tab doesn't auto-download the same files too.
        r = self.s.get(self.base + "/status?client=pc", timeout=30)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
            # Cookie expired / wrong password → the login page comes back.
            if not self.login():
                raise RuntimeError("לא מחובר")
            r = self.s.get(self.base + "/status?client=pc", timeout=30)
        return r.json()

    def save_state(self) -> None:
        self.state["got"] = self.state["got"][-2000:]
        save_json(STATE, self.state)

    # -- work
    def download_now(self, vid: str) -> str:
        """The site's download button inside the app: fetch this file now,
        even if it was downloaded before."""
        try:
            job = next((j for j in self.status().get("jobs", []) if j.get("videoId") == vid), None)
            if not job or not job.get("fileUrl"):
                return "הקובץ לא מוכן"
            self.download(job)
            return "ירד לתיקייה"
        except Exception as e:
            return f"שגיאה: {e}"

    def download(self, job: dict) -> None:
        with self.lock:
            self._download(job)

    def _download(self, job: dict) -> None:
        # With a vault, files land in a private staging folder and are
        # encrypted straight away; otherwise in the chosen folder.
        folder = (USERDATA / "incoming") if self.cfg.get("vault_dir") else Path(self.cfg["folder"])
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
        if self.cfg.get("vault_dir") and final.is_file():
            from vault import Vault
            eid = Vault(self.cfg["vault_dir"]).add(final, {"videoId": job["videoId"], "title": job.get("title", ""),
                                                          "mode": job.get("mode", "")})
            self.log(f"נשמר בכספת: {final.name}", remote=False)
            final = Path(self.cfg["vault_dir"]) / (eid + ".ytv")
        self.state["got"].append(job["videoId"])
        self.state["files"][job["videoId"]] = str(final)
        self.save_state()
        self.pending_got.append(job["videoId"])
        self.log("ירד למחשב" + ("" if self.cfg.get("vault_dir") else f": {final.name}"))

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
        lock = DownloadLock()
        while True:
            if lock.name is None:
                try:
                    lock.name = (self.status().get("profile") or {}).get("id") or "main"
                except Exception:
                    time.sleep(POLL_SECONDS)
                    continue
            if not lock.acquire():
                # Another user's copy is downloading for this computer; this
                # copy still shows notifications to its own user.
                try:
                    self.notify_from(self.status())
                except Exception:
                    pass
                time.sleep(POLL_SECONDS)
                continue
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
                self.notify_from(st)
            except Exception as e:
                self.log(f"שגיאה: {e}", remote=False)
            self.check_for_update()
            self.flush_logs()
            time.sleep(POLL_SECONDS)

    def notify_from(self, st: dict) -> None:
        """Notifications per this user's choice: each file that reached the
        computer, one summary when the queue empties, or none. Works the same
        in every user's copy (whichever one did the downloading)."""
        jobs = st.get("jobs", [])
        landed = {j["videoId"]: j for j in jobs if j.get("status") == "done" and j.get("pcGot")}
        if self._seen is None:                       # first look: don't announce old files
            self._seen = set(landed)
            return
        new = [landed[v] for v in landed if v not in self._seen]
        self._seen |= set(landed)
        mode = get_pref("notify", "each")
        if mode == "each":
            for j in new[:5]:
                self.notify("ירד קובץ חדש", j.get("title") or j["videoId"])
            if len(new) > 5:
                self.notify("ירדו קבצים חדשים", f"ועוד {len(new) - 5} קבצים")
        elif mode == "batch":
            self._batch += len(new)
            busy = any(j.get("status") in ("queued", "taken") for j in jobs)
            if self._batch and not busy:
                self.notify("ההורדות הסתיימו", f"ירדו {self._batch} קבצים לתיקייה")
                self._batch = 0
        else:
            self._batch = 0

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

# ---------------------------------------------------------------- install
# Installed once (needs administrator rights) for every user of the computer:
#   C:\ProgramData\YTTracker\app\YTTracker.exe — writable by all users, so the
#   app can update itself without asking for administrator rights again;
#   shortcuts on the shared Desktop + Start menu; Apps & features entry.

INSTALL_DIR = DATA / "app"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\YTTracker"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LNK_NAME = "מעקב ערוצים.lnk"


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_elevated(args: list[str]) -> bool:
    """Run this exe with administrator rights (Windows asks the user) and
    wait for it. Returns True if it ran and succeeded."""
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    sei = SEI(cbSize=ctypes.sizeof(SEI), fMask=0x40, lpVerb="runas", lpFile=sys.executable,
              lpParameters=subprocess.list2cmdline(args), nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return False                      # the user said "No" to the prompt
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return code.value == 0


def _shortcut(lnk: Path, target: Path) -> None:
    """Create a .lnk with Windows' own scripting object (no extra libraries)."""
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
          "$s.TargetPath=$env:TGT;$s.IconLocation=$env:TGT+',0';$s.WorkingDirectory=$env:DIR;$s.Save()")
    env = {**os.environ, "LNK": str(lnk), "TGT": str(target), "DIR": str(target.parent)}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                   capture_output=True, creationflags=NO_WINDOW)


def _shortcut_paths() -> list[Path]:
    """Shared Desktop and shared Start menu — seen by every user."""
    public = Path(os.environ.get("PUBLIC") or r"C:\Users\Public")
    return [public / "Desktop" / LNK_NAME,
            PROGRAMDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / LNK_NAME]


def _old_per_user_cleanup() -> None:
    """Remove the earlier per-user install (shortcuts, HKCU entries) and
    bring its settings over to the shared place."""
    import winreg
    old_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / APP
    appdata = Path(os.environ.get("APPDATA") or Path.home())
    userdesk = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
    for lnk in (userdesk / LNK_NAME, appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / LNK_NAME):
        lnk.unlink(missing_ok=True)
    for key, value in ((UNINSTALL_KEY, None), (RUN_KEY, APP)):
        try:
            if value is None:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            else:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, value)
        except OSError:
            pass
    if old_dir.exists():
        subprocess.Popen(f'cmd /c timeout /t 3 /nobreak >nul & rmdir /s /q "{old_dir}"', shell=True,
                         creationflags=NO_WINDOW)
    for name in ("config.json", "state.json"):
        src, dst = OLD_DATA / name, DATA / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


def do_install(src: Path) -> int:
    """Runs elevated (--install-elevated): copy, permissions, shortcuts,
    Apps & features, autostart for everyone."""
    import winreg
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    target = INSTALL_DIR / "YTTracker.exe"
    shutil.copy2(src, target)
    # Every user may modify the app folder and the shared settings — needed
    # for self-update and for each user saving settings/history.
    subprocess.run(["icacls", str(DATA), "/grant", "*S-1-5-32-545:(OI)(CI)M", "/T", "/Q"],
                   capture_output=True, creationflags=NO_WINDOW)
    for lnk in _shortcut_paths():
        lnk.parent.mkdir(parents=True, exist_ok=True)
        _shortcut(lnk, target)
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as k:
        for name, val in (("DisplayName", "מעקב ערוצים (YTTracker)"), ("DisplayIcon", str(target)),
                          ("Publisher", "YTTracker"), ("InstallLocation", str(INSTALL_DIR)),
                          ("UninstallString", f'"{target}" --uninstall'),
                          ("DisplayVersion", bundled_version()[:7] or "1")):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    if "--autostart" in sys.argv:
        _set_autostart_machine(True, target)
    return 0


def install_if_needed() -> bool:
    """Not running from the shared install yet: ask for administrator rights
    once, install for all users, then start the installed copy. Returns True
    if this copy should exit."""
    if not getattr(sys, "frozen", False):
        return False
    exe = Path(sys.executable).resolve()
    target = INSTALL_DIR / "YTTracker.exe"
    if exe.parent == INSTALL_DIR.resolve():
        try:
            _old_per_user_cleanup()
        except Exception:
            pass
        return False                      # already the installed copy
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk(); set_tk_icon(r); r.withdraw()
    if not messagebox.askokcancel(APP, "התוכנה תותקן לכל המשתמשים במחשב.\n"
                                  "Windows יבקש אישור מנהל — לחץ \"כן\".", parent=r):
        r.destroy()
        return False                      # run from here this time
    auto = messagebox.askyesno(APP, "להפעיל את התוכנה אוטומטית עם Windows לכל המשתמשים?\n"
                               "(היא תרוץ ברקע ותוריד גם כשהחלון סגור)", parent=r)
    r.destroy()
    args = ["--install-elevated", str(exe)] + (["--autostart"] if auto else [])
    ok = do_install(exe) == 0 if is_admin() else run_elevated(args)
    if not ok or not target.exists():
        r = tk.Tk(); set_tk_icon(r); r.withdraw()
        messagebox.showwarning(APP, "ההתקנה לא הושלמה (לא אושרה הרשאת מנהל). התוכנה תרוץ מהמקום הנוכחי.", parent=r)
        r.destroy()
        return False
    subprocess.Popen([str(target)], close_fds=True)
    return True


def uninstall() -> None:
    """Apps & features → Uninstall. Downloaded files and settings are kept."""
    if not is_admin():
        run_elevated(["--uninstall"])
        return
    import tkinter as tk
    import winreg
    from tkinter import messagebox
    r = tk.Tk(); set_tk_icon(r); r.withdraw()
    if not messagebox.askyesno(APP, "להסיר את התוכנה מהמחשב (לכל המשתמשים)?\n(הקבצים שהורדו לא יימחקו)", parent=r):
        return
    try:
        _set_autostart_machine(False)
    except Exception:
        pass
    for lnk in _shortcut_paths():
        lnk.unlink(missing_ok=True)
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
    except OSError:
        pass
    # A running exe can't delete itself: let cmd remove the folder after we exit.
    subprocess.Popen(f'cmd /c timeout /t 2 /nobreak >nul & rmdir /s /q "{INSTALL_DIR}"', shell=True,
                     creationflags=NO_WINDOW)
    messagebox.showinfo(APP, "התוכנה הוסרה.", parent=r)


# ---------------------------------------------------------------- autostart (all users)

def get_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
        return True
    except Exception:
        return False


def _set_autostart_machine(on: bool, exe: Path | None = None) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{exe or sys.executable}" --background')
        else:
            try:
                winreg.DeleteValue(k, APP)
            except FileNotFoundError:
                pass


def set_autostart(on: bool) -> None:
    """Start with Windows for every user, hidden in the tray. Changing it
    needs administrator rights, so Windows asks."""
    if is_admin():
        _set_autostart_machine(on)
    else:
        run_elevated(["--set-autostart", "1" if on else "0"])


# ---------------------------------------------------------------- one copy per user session

def _session_port() -> int:
    """Each logged-in user gets their own 'already running' port."""
    try:
        import ctypes
        from ctypes import wintypes
        sid = wintypes.DWORD()
        ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return 47321 + (sid.value % 500)
    except Exception:
        return 47321


def already_running() -> bool:
    """If this user already has the app open, show its window and say so."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", _session_port()), timeout=1) as c:
            c.sendall(b"show")
        return True
    except OSError:
        return False


def listen_for_show(show) -> None:
    import socket
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", _session_port()))
    except OSError:
        return
    srv.listen(1)

    def loop():
        while True:
            c, _ = srv.accept()
            c.close()
            show()
    threading.Thread(target=loop, daemon=True).start()


class DownloadLock:
    """Only one copy per profile downloads at a time (the same profile open
    in two Windows sessions would otherwise fetch everything twice)."""
    def __init__(self):
        self.fh = None
        self.name: str | None = None      # profile id, known after the first status

    def acquire(self) -> bool:
        if self.fh:
            return True
        try:
            import msvcrt
            fh = open(DATA / f"downloader-{self.name or 'main'}.lock", "a+")
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            self.fh = fh
            return True
        except ImportError:
            return True                   # not Windows (testing)
        except OSError:
            try:
                fh.close()
            except Exception:
                pass
            return False


class Api:
    """Functions the site can call when it runs inside the app
    (window.pywebview.api.…)."""
    def __init__(self, cfg: dict, dl: "Downloader"):
        self.cfg, self.dl = cfg, dl

    def download_now(self, vid: str) -> str:
        return self.dl.download_now(vid)

    def open_folder(self) -> None:
        if self.cfg.get("vault_dir"):
            open_vault_window(self.cfg)
        else:
            os.startfile(self.cfg["folder"])

    def open_vault(self) -> bool:
        open_vault_window(self.cfg)
        return True

    def has_vault(self) -> bool:
        return bool(self.cfg.get("vault_dir"))

    def show_file(self, vid: str) -> bool:
        """Open the downloads folder with this video's file selected (or the
        vault, for files kept there)."""
        path = self.dl.state.get("files", {}).get(vid)
        if path and path.endswith(".ytv"):
            open_vault_window(self.cfg)
            return True
        if path and Path(path).exists():
            subprocess.Popen(["explorer", "/select,", path])
            return True
        os.startfile(self.cfg["folder"])
        return False

    def forget(self, vids: list, delete_files: bool = False) -> int:
        """Deleted from the site's history: forget them here too, and
        optionally delete the files from the computer."""
        st, n = self.dl.state, 0
        for vid in vids or []:
            path = st.get("files", {}).pop(vid, None)
            st["requeued"].pop(vid, None)
            if vid in st["got"]:
                st["got"].remove(vid)
            if delete_files and path and Path(path).exists():
                try:
                    Path(path).unlink()
                    n += 1
                except OSError:
                    pass
        self.dl.save_state()
        return n

    def downloaded(self) -> list:
        """Which videos this computer already has (the site shows a folder button)."""
        return [v for v, p in self.dl.state.get("files", {}).items() if Path(p).exists()]

    def app_info(self) -> dict:
        return {"folder": "כספת מוצפנת" if self.cfg.get("vault_dir") else self.cfg["folder"],
                "vault": bool(self.cfg.get("vault_dir")), "version": bundled_version()[:7], "autostart": get_autostart(),
                "notify": get_pref("notify", "each")}

    def get_notify(self) -> str:
        return get_pref("notify", "each")

    def set_notify(self, mode: str) -> str:
        if mode in NOTIFY_MODES:
            set_pref("notify", mode)
        return get_pref("notify", "each")

    def set_autostart(self, on: bool) -> bool:
        set_autostart(bool(on))
        return get_autostart()


_VAULT = {}


def open_vault_window(cfg: dict) -> None:
    """One vault window per app; showing it again just brings it forward."""
    import webview
    from vault_ui import VAULT_HTML, VaultApi
    w = _VAULT.get("win")
    if w is not None:
        try:
            w.show()
            w.restore()
            return
        except Exception:
            pass
    api = _VAULT.get("api") or VaultApi(cfg, TMP)
    _VAULT["api"] = api
    w = webview.create_window("הכספת — מעקב ערוצים", html=VAULT_HTML, js_api=api, width=900, height=640,
                              min_size=(420, 420), background_color="#0e1015")
    api.window = w

    def closed():
        api.lock()                       # closing the vault window locks it
        _VAULT.pop("win", None)
    w.events.closed += closed
    _VAULT["win"] = w


def make_tray(on_open, on_quit, cfg: dict):
    """Icon next to the clock: keeps the app running with the window closed."""
    try:
        import pystray
        from PIL import Image
        img = Image.open(icon_path())
        def pick(mode):
            return lambda icon, item: set_pref("notify", mode)

        def is_on(mode):
            return lambda item: get_pref("notify", "each") == mode
        notify_menu = pystray.Menu(*[pystray.MenuItem(label, pick(m), checked=is_on(m), radio=True)
                                     for m, label in NOTIFY_MODES.items()])
        menu = pystray.Menu(
            pystray.MenuItem("פתח", lambda: on_open(), default=True),
            pystray.MenuItem("התראות", notify_menu),
            pystray.MenuItem("פתח כספת" if cfg.get("vault_dir") else "פתח את תיקיית ההורדות",
                             lambda: open_vault_window(cfg) if cfg.get("vault_dir") else os.startfile(cfg["folder"])),
            pystray.MenuItem("יציאה", lambda: on_quit()))
        return pystray.Icon(APP, img, "מעקב ערוצים — מוריד ברקע", menu)
    except Exception:
        traceback.print_exc()
        return None


def open_window(cfg: dict, dl: "Downloader", background: bool) -> None:
    url = cfg["site"].rstrip("/") + "/yt/"
    state = {"quit": False}
    try:
        import webview  # pywebview — uses Edge WebView2, built into Windows 10/11
    except Exception:
        webview = None

    if webview is None:
        # No WebView2: tray only; "open" uses the normal browser.
        tray = make_tray(lambda: webbrowser.open(url), lambda: tray.stop(), cfg)
        if not background:
            webbrowser.open(url)
        if tray:
            dl.notify = lambda t, m: tray.notify(m, t)
            tray.run()
        else:
            while True:
                time.sleep(3600)
        return

    win = webview.create_window("מעקב ערוצים", url, width=1200, height=820, min_size=(420, 520),
                                hidden=background, js_api=Api(cfg, dl), background_color="#0e1015")

    def show():
        win.show()
        win.restore()

    def quit_app():
        state["quit"] = True
        if _VAULT.get("api"):
            _VAULT["api"].lock()
        if tray:
            tray.stop()
        win.destroy()

    tray = make_tray(show, quit_app, cfg)

    def on_closing():
        # Closing the window hides it — downloads keep running in the tray.
        if state["quit"] or tray is None:
            return True
        win.hide()
        if not state.get("told"):
            state["told"] = True
            tray.notify("התוכנה ממשיכה להוריד ברקע. ליציאה: לחיצה ימנית על האייקון ליד השעון ← יציאה.", APP)
        return False

    win.events.closing += on_closing
    if tray:
        dl.notify = lambda t, m: tray.notify(m, t)
        tray.run_detached()
    listen_for_show(show)
    # private_mode=False + storage_path: the site's login is remembered.
    webview.start(private_mode=False, storage_path=str(USERDATA / "web"))
    if tray:
        tray.stop()


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
    # Leftovers from earlier updates. Another user may still be running one
    # of them (shared install) — then it's locked; just try again next time.
    for old in exe.parent.glob("YTTracker.old*.exe"):
        try:
            old.unlink()
        except OSError:
            pass
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
        tmp = exe.with_name(exe.stem + f".new{os.getpid()}" + (".zip" if url.endswith(".zip") else ".exe"))
        with requests.get(url, stream=True, timeout=(30, 300), headers={"User-Agent": "yt-tracker-desktop"}) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if tmp.suffix == ".zip":         # the .exe was blocked; the zip wasn't
            with zipfile.ZipFile(tmp) as z:
                data = z.read("YTTracker.exe")
            tmp.unlink()
            tmp = exe.with_name(exe.stem + f".new{os.getpid()}.exe")
            tmp.write_bytes(data)
        if tmp.stat().st_size < 5_000_000:
            raise RuntimeError("קובץ העדכון קטן מדי — כנראה נחסם")
        # Unique name: an older .old.exe may still be running for another user.
        exe.replace(exe.with_name(f"YTTracker.old-{int(time.time())}.exe"))
        tmp.replace(exe)
        root.destroy()
        subprocess.Popen([str(exe)] + sys.argv[1:], close_fds=True)
        return True
    except Exception as e:
        root.destroy()
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + f"עדכון נכשל: {e}\n")
        if not exe.exists():                           # put the working one back
            olds = sorted(exe.parent.glob("YTTracker.old-*.exe"))
            if olds:
                try:
                    olds[-1].replace(exe)
                except OSError:
                    pass
        return False


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    USERDATA.mkdir(parents=True, exist_ok=True)
    if "--install-elevated" in sys.argv:          # the administrator half of installing
        sys.exit(do_install(Path(sys.argv[sys.argv.index("--install-elevated") + 1])))
    if "--set-autostart" in sys.argv:
        _set_autostart_machine(sys.argv[sys.argv.index("--set-autostart") + 1] == "1",
                               INSTALL_DIR / "YTTracker.exe")
        sys.exit(0)
    if "--uninstall" in sys.argv:
        return uninstall()
    if already_running():
        return                           # the running copy shows its window
    if install_if_needed():
        return                           # the installed copy is starting
    try:
        if self_update():
            return                       # the new version is starting
    except Exception:
        traceback.print_exc()            # an update problem must never stop the app
    # Older versions kept the password in the shared file — remove it there;
    # each user now signs in to their own profile (stored only for them).
    shared = load_json(CONFIG, {})
    if "password" in shared or "folder" in shared:
        save_json(CONFIG, {"site": shared.get("site") or DEFAULT_SITE})
    if not STATE.exists() and (DATA / "state.json").exists():
        shutil.copy2(DATA / "state.json", STATE)
    shutil.rmtree(TMP, ignore_errors=True)      # decrypted leftovers from last time
    cfg = load_cfg()
    if "--setup" in sys.argv or not cfg.get("password") or not (cfg.get("folder") or cfg.get("vault_dir")):
        cfg = first_run_setup(cfg)
    dl = Downloader(cfg)
    dl.start()
    open_window(cfg, dl, background="--background" in sys.argv)


if __name__ == "__main__":
    main()
