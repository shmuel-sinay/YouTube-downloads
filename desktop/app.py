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

# A new copy of this app started from here (after an update, after install)
# must unpack itself fresh. Without this it inherits this process's temp
# folder (_MEI…), which is deleted as this one exits → "Failed to create a
# .NET runtime" and "Failed to remove temporary directory" in the new copy.
os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

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
USERCFG = USERDATA / "user.json"       # before accounts: one profile per Windows user
ACCOUNTS = USERDATA / "accounts.json"  # every profile signed in on this Windows user
STATE = USERDATA / "state.json"        # this user's download history
LOGFILE = USERDATA / "app.log"
TMP = USERDATA / "tmp"                 # decrypted copies for playing — wiped on lock/exit
DEFAULT_SITE = "https://my-gemini.g47ben.workers.dev"
# Where new versions are published (GitHub release built by desktop.yml).
RELEASE_API = "https://api.github.com/repos/shmuel-sinay/YouTube-downloads/releases/tags/desktop-latest"
UPDATE_CHECK_HOURS = 1 / 3   # every 20 minutes (one small request)
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


MB_OK, MB_OKCANCEL, MB_YESNO = 0, 1, 4
MB_ICONINFO, MB_ICONWARN, MB_ICONQUESTION = 0x40, 0x30, 0x20


def msgbox(text: str, flags: int = MB_OK | MB_ICONINFO) -> bool:
    """Native Windows message box (no tkinter). True for OK/Yes."""
    try:
        import ctypes
        r = ctypes.windll.user32.MessageBoxW(None, text, APP, flags | 0x2000 | 0x10000 | 0x80000 | 0x100000)
        return r in (1, 6)
    except Exception:
        print(text)
        return flags & 0xF == MB_OK


# ---------------------------------------------------------------- accounts
# Several people can share one Windows user. Each signs in on the site's
# own "who's signing in?" page inside the app; the app remembers the
# profile (code encrypted with DPAPI, for this Windows user only) and keeps
# downloading for every remembered profile in the background — each into
# its own folder or vault.

class Accounts:
    def __init__(self, site: str):
        self.site = site.rstrip("/")
        self.data: dict = load_json(ACCOUNTS, {})       # pid -> stored account
        self.dls: dict = {}                            # pid -> Downloader
        self.notify = lambda title, msg: None          # set by the tray icon
        self.lock = threading.Lock()

    def save(self) -> None:
        with self.lock:
            out = {}
            for pid, a in self.data.items():
                a = {k: v for k, v in a.items() if k != "password"}
                if self.data[pid].get("password"):
                    a["password_enc"] = protect(self.data[pid]["password"])
                out[pid] = a
            save_json(ACCOUNTS, out)

    def _decrypt(self, a: dict) -> dict:
        if "password" not in a:
            try:
                a["password"] = unprotect(a.get("password_enc", "")) if a.get("password_enc") else ""
            except Exception:
                a["password"] = ""
        a.pop("password_enc", None)
        a.setdefault("site", self.site)
        a.setdefault("unzip", True)
        return a

    def start_all(self) -> None:
        for pid in list(self.data):
            self._start(pid)

    def _start(self, pid: str) -> "Downloader":
        acct = self._decrypt(self.data[pid])
        acct["pid"] = pid
        dl = self.dls.get(pid)
        if dl is None:
            dl = Downloader(acct, self)
            self.dls[pid] = dl
            dl.start()
        return dl

    def default_folder(self, name: str) -> str:
        safe = "".join(c for c in name if c not in '<>:"/\\|?*').strip() or "profile"
        return str(Path.home() / "Music" / APP / safe)

    def remember(self, pid: str, password: str) -> dict:
        """Called by the sign-in page inside the app. Checks the code with the
        site, then keeps this profile downloading in the background."""
        s = requests.Session()
        s.headers["User-Agent"] = "yt-tracker-desktop/1.0"
        r = s.post(self.site + "/yt/login", data={"pid": pid, "password": password}, allow_redirects=False, timeout=30)
        if r.status_code not in (302, 303) or "auth" not in s.cookies:
            return {"ok": False}
        prof = (s.get(self.site + "/yt/status", timeout=30).json().get("profile") or {})
        pid = prof.get("id") or pid
        old = self.data.get(pid)
        if old:
            old = self._decrypt(old)
            if old.get("password") != password:
                # The admin gave this profile a new code: the vault (whose
                # password is the code) follows it.
                if old.get("vault_dir") and old.get("vault_auto"):
                    try:
                        from vault import Vault
                        Vault(old["vault_dir"]).change_password(old["password"], password)
                    except Exception:
                        pass
                old["password"] = password
                if pid in self.dls:
                    self.dls[pid].cfg["password"] = password
        else:
            self.data[pid] = {"password": password, "name": prof.get("name") or pid,
                              "folder": self.default_folder(prof.get("name") or pid), "vault_dir": "",
                              "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "notify": "each"}
        self.data[pid].update(name=prof.get("name") or pid, vault=bool(prof.get("vault")), netfree=bool(prof.get("netfree")))
        self.save()
        dl = self._start(pid)
        dl.apply_profile(prof)
        return {"ok": True, "pid": pid}

    def add_legacy(self, cfg: dict) -> None:
        """One profile from before accounts (user.json): its id is learned on
        the first contact with the site."""
        if not cfg.get("password"):
            return
        self.data["?"] = {"password": cfg["password"], "name": "", "folder": cfg.get("folder") or "",
                          "vault_dir": cfg.get("vault_dir") or "", "since": cfg.get("since") or "",
                          "unzip": cfg.get("unzip", True), "notify": get_pref("notify", "each"), "legacy": True}
        self.save()

    def rekey(self, old: str, new: str) -> None:
        """The legacy account's real profile id is now known."""
        with self.lock:
            if old in self.data:
                if new in self.data:              # already signed in again meanwhile
                    self.data.pop(old)
                else:
                    self.data[new] = self.data.pop(old)
                    self.data[new].pop("legacy", None)
            if old in self.dls:
                self.dls[new] = self.dls.pop(old)
        self.save()
        if STATE.exists() and not state_file(new).exists():
            shutil.copy2(STATE, state_file(new))


def state_file(pid: str) -> Path:
    return USERDATA / f"state-{pid}.json"


# ---------------------------------------------------------------- downloader

class Downloader(threading.Thread):
    """Downloads for one profile (one remembered account)."""
    def __init__(self, cfg: dict, accounts: "Accounts | None" = None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.accounts = accounts
        self.base = cfg["site"].rstrip("/") + "/yt"
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "yt-tracker-desktop/1.0"
        self.state = load_json(self.state_path(), {"got": [], "requeued": {}})
        self.state.setdefault("files", {})       # videoId -> where the file was saved
        self.pending_got: list[str] = []         # reported to the site (browser then skips them)
        self.pending_logs: list[str] = []
        self.last_wake = 0.0
        self.last_update_check = time.time()
        self.lock = threading.Lock()          # one download at a time
        self.nf_seen: dict = {}               # videoId -> time of the last "unknown" NetFree check
        self._seen: set | None = None           # files already announced
        self._batch = 0                         # files in the current "download task"

    # -- helpers
    @property
    def pid(self) -> str:
        return self.cfg.get("pid") or "?"

    def notify(self, title: str, msg: str) -> None:
        if self.accounts:
            self.accounts.notify(title, msg)

    def state_path(self) -> Path:
        return STATE if self.pid == "?" else state_file(self.pid)

    def folder(self) -> Path:
        return Path(self.cfg.get("folder") or (self.accounts.default_folder(self.cfg.get("name") or self.pid)
                                                if self.accounts else USERDATA / "files"))

    def save_cfg(self) -> None:
        if self.accounts:
            self.accounts.save()
        else:
            save_user_cfg(self.cfg)

    def apply_profile(self, prof: dict) -> None:
        """What the admin set for this profile (name, vault) — the user can't
        change these. A profile with a vault gets one here, locked with its
        code (only the admin sets codes)."""
        if not prof:
            return
        pid = prof.get("id") or self.pid
        if self.pid == "?" and self.accounts and pid:
            self.accounts.rekey("?", pid)
            self.cfg["pid"] = pid
            self.save_state()
        changed = False
        for k in ("name", "vault", "netfree", "direct"):
            v = prof.get(k) if k == "name" else bool(prof.get(k))
            if v is not None and self.cfg.get(k) != v:
                self.cfg[k], changed = v, True
        if prof.get("vault") and not self.cfg.get("vault_dir") and self.cfg.get("password"):
            from vault import Vault
            vdir = USERDATA / f"vault-{self.pid}"
            v = Vault(vdir)
            if not v.exists:
                v.create(self.cfg["password"])
            self.cfg.update(vault_dir=str(vdir), vault_auto=True)
            self.log("נוצרה כספת מוצפנת לפרופיל (הסיסמה שלה = הקוד של הפרופיל)")
            changed = True
        if changed:
            self.save_cfg()

    def log(self, msg: str, remote: bool = True) -> None:
        line = time.strftime("%H:%M:%S ") + (f"[{self.cfg.get('name') or self.pid}] " if self.accounts else "") + msg
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
        data = {"password": self.cfg["password"]}
        if self.pid != "?":
            data["pid"] = self.pid
        r = self.s.post(self.base + "/login", data=data, allow_redirects=False, timeout=30)
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
        st = r.json()
        self.apply_profile(st.get("profile") or {})
        return st

    def save_state(self) -> None:
        self.state["got"] = self.state["got"][-2000:]
        save_json(self.state_path(), self.state)

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
        folder = (USERDATA / "incoming" / self.pid) if self.cfg.get("vault_dir") else self.folder()
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
        elif (self.cfg.get("unzip") or self.cfg.get("vault_dir")) and dest.suffix.lower() == ".zip":
            # The zip is only the wrapper that gets through NetFree; the vault
            # keeps the video/song itself, so it shows and plays there.
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
        self.log("ירד למחשב" + (" (נשמר בכספת)" if self.cfg.get("vault_dir") else f": {final.name}"))

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
        self.log("התוכנה עלתה · " + ("כספת מוצפנת" if self.cfg.get("vault_dir") else f"תיקייה: {self.folder()}"), remote=False)
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
            if self.pid == "main":
                try:
                    self.netfree_checks()
                except Exception as e:
                    self.log(f"בדיקת נטפרי: {e}", remote=False)
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
                if self.cfg.get("direct"):
                    self.direct_pass()           # this profile downloads on this computer
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
        mode = self.cfg.get("notify") or get_pref("notify", "each")
        who = self.cfg.get("name") or ""
        if mode == "each":
            for j in new[:5]:
                # A profile with a vault: no titles on screen.
                self.notify("ירד קובץ חדש" + (f" · {who}" if who else ""),
                            "קובץ חדש נשמר בכספת" if self.cfg.get("vault_dir") else (j.get("title") or j["videoId"]))
            if len(new) > 5:
                self.notify("ירדו קבצים חדשים", f"ועוד {len(new) - 5} קבצים")
        elif mode == "batch":
            self._batch += len(new)
            busy = any(j.get("status") in ("queued", "taken") for j in jobs)
            if self._batch and not busy:
                self.notify("ההורדות הסתיימו" + (f" · {who}" if who else ""),
                            f"ירדו {self._batch} קבצים" + (" לכספת" if self.cfg.get("vault_dir") else " לתיקייה"))
                self._batch = 0
        else:
            self._batch = 0

    # -- downloading on this computer (profiles set to "מוריד מהמחשב")
    # YouTube blocks most cloud addresses; a home computer isn't blocked. With
    # NetFree on, a download that succeeds is already a NetFree-approved one.
    def direct_pass(self) -> None:
        r = self.s.post(self.base + "/pc/next", timeout=60)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
            return
        for job in r.json().get("jobs", []):
            vid = job["videoId"]
            try:
                self._download_direct(job)
                self.s.post(self.base + "/pc/done", json={"videoId": vid, "status": "done"}, timeout=30)
            except Exception as e:
                why = (str(e) or type(e).__name__)[:280]
                self.log(f"ההורדה מהמחשב נכשלה ({job.get('title') or vid}): {why}")
                self.s.post(self.base + "/pc/done", json={"videoId": vid, "status": "failed", "error": why}, timeout=30)

    def _download_direct(self, job: dict) -> None:
        import yt_dlp
        vid = job["videoId"]
        work = USERDATA / "pc-work" / vid
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        try:
            audio = job.get("mode") in ("m4a", "audio")
            # "video720" / "video480": lower quality, smaller file, faster.
            h = (job.get("mode") or "")[5:]
            cap = f"[height<={h}]" if h.isdigit() else ""
            opts = {"outtmpl": str(work / "%(id)s.%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True,
                    "format": "ba[ext=m4a]/ba" if audio else
                              f"bv*{cap}[ext=mp4]+ba[ext=m4a]/b{cap}[ext=mp4]/bv*{cap}+ba/b{cap}/b",
                    "merge_output_format": "mp4", "ffmpeg_location": ffmpeg_exe()}
            if shutil.which("node"):
                opts["js_runtimes"] = {"node": {}}          # YouTube's JavaScript challenge
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([job.get("url") or "https://www.youtube.com/watch?v=" + vid])
            files = [p for p in work.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl")]
            if not files:
                raise RuntimeError("לא נוצר קובץ")
            src = max(files, key=lambda p: p.stat().st_size)
            title = "".join(c for c in (job.get("title") or src.stem) if c not in '<>:"/\\|?*').strip() or src.stem
            final = work / (title + src.suffix)
            if job.get("mode") == "audio":
                mp3 = work / (title + ".mp3")
                if ffmpeg_mp3(src, mp3):
                    final = mp3
            if final != src:
                src.replace(final)
            final = self._store(final, job)
            self.state["got"].append(vid)
            self.state["files"][vid] = str(final)
            self.save_state()
            self.pending_got.append(vid)
            self.log("ירד למחשב" + (" (נשמר בכספת)" if self.cfg.get("vault_dir") else f": {final.name}"))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _store(self, final: Path, job: dict) -> Path:
        """Put a finished file where this profile keeps its files."""
        if self.cfg.get("vault_dir"):
            from vault import Vault
            eid = Vault(self.cfg["vault_dir"]).add(final, {"videoId": job["videoId"], "title": job.get("title", ""),
                                                          "mode": job.get("mode", "")})
            return Path(self.cfg["vault_dir"]) / (eid + ".ytv")
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / final.name
        if dest.exists():
            dest = folder / f"{final.stem} ({job['videoId']}){final.suffix}"
        return Path(shutil.move(str(final), str(dest)))

    def netfree_checks(self) -> None:
        """Admin's copy only: videos waiting for "is it open on NetFree?"
        (profiles the admin restricted). This computer is behind NetFree,
        so what gets through here is what NetFree allows."""
        r = self.s.get(self.base + "/nf/pending", timeout=30)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
            return
        from netfree import probe
        for it in r.json().get("items", [])[:10]:
            vid = it["videoId"]
            if time.time() - self.nf_seen.get(vid, 0) < 600:
                continue                              # undecided a moment ago — later
            verdict, detail = probe(vid)
            if verdict == "unknown":
                self.nf_seen[vid] = time.time()
                self.log(f"נטפרי: לא הצלחתי להכריע על {it.get('title') or vid} — אנסה שוב ({detail})")
                continue
            self.s.post(self.base + "/nf/result", timeout=30,
                        json={"pid": it["pid"], "videoId": vid, "ok": verdict == "open",
                              "detail": f"חסום בנטפרי ({detail} · גרסה {bundled_version_name()})"})
            self.log(f"נטפרי: {'פתוח' if verdict == 'open' else 'חסום'} — {it.get('title') or vid} ({detail})")

    def check_for_update(self) -> None:
        """While running, just announce a new version; it installs itself
        the next time the app is opened."""
        if not getattr(sys, "frozen", False) or time.time() - self.last_update_check < UPDATE_CHECK_HOURS * 3600:
            return
        self.last_update_check = time.time()
        try:
            sha, _ = latest_release()
            mine = bundled_version()
            if sha and mine and not same_version(sha, mine):
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
    try:
        return _do_install(src)
    except Exception:
        (DATA / "install-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return 1


def _do_install(src: Path) -> int:
    """Runs elevated (--install-elevated): copy, permissions, shortcuts,
    Apps & features, autostart for everyone."""
    import winreg
    (DATA / "install-error.txt").unlink(missing_ok=True)
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
                          ("DisplayVersion", bundled_version_name())):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    if "--autostart" in sys.argv:
        _set_autostart_machine(True, target)
    return 0


def close_other_copies() -> None:
    """A freshly downloaded copy is about to install: close older copies of
    the app running for this user (e.g. an old version in the tray), or they
    would 'answer' instead and the new version would never install. The
    PyInstaller launcher is our parent process — keep it."""
    script = ("Get-Process -Name YTTracker -ErrorAction SilentlyContinue | "
              f"Where-Object {{ $_.Id -ne {os.getpid()} -and $_.Id -ne {os.getppid()} -and $_.SessionId -eq (Get-Process -Id {os.getpid()}).SessionId }} | "
              "Stop-Process -Force -ErrorAction SilentlyContinue")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, creationflags=NO_WINDOW, timeout=30)
    time.sleep(1)


def is_installed_copy() -> bool:
    return getattr(sys, "frozen", False) and Path(sys.executable).resolve().parent == INSTALL_DIR.resolve()


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
    if not msgbox("התוכנה תותקן לכל המשתמשים במחשב.\n"
                  "Windows יבקש אישור מנהל — לחץ \"כן\".", MB_OKCANCEL | MB_ICONINFO):
        return False                      # run from here this time
    auto = msgbox("להפעיל את התוכנה אוטומטית עם Windows לכל המשתמשים?\n"
                  "(היא תרוץ ברקע ותוריד גם כשהחלון סגור)", MB_YESNO | MB_ICONQUESTION)
    args = ["--install-elevated", str(exe)] + (["--autostart"] if auto else [])
    ok = do_install(exe) == 0 if is_admin() else run_elevated(args)
    if not ok or not target.exists():
        err = DATA / "install-error.txt"
        why = err.read_text(encoding="utf-8").strip().splitlines()[-1] if err.exists() else "לא אושרה הרשאת מנהל"
        msgbox(f"ההתקנה לא הושלמה:\n{why}\n\nהתוכנה תרוץ מהמקום הנוכחי.", MB_OK | MB_ICONWARN)
        return False
    msgbox("התוכנה הותקנה לכל המשתמשים.\nיש קיצור \"מעקב ערוצים\" בשולחן העבודה ובתפריט התחל.")
    subprocess.Popen([str(target)], close_fds=True)
    return True


def uninstall() -> None:
    """Apps & features → Uninstall. Downloaded files and settings are kept."""
    if not is_admin():
        run_elevated(["--uninstall"])
        return
    import winreg
    if not msgbox("להסיר את התוכנה מהמחשב (לכל המשתמשים)?\n(הקבצים שהורדו לא יימחקו)", MB_YESNO | MB_ICONQUESTION):
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
    msgbox("התוכנה הוסרה.")


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
    (window.pywebview.api.…). They act only for the profile that signed in
    through this window (its code was checked by remember()), so one person
    can't reach another's files from the page."""
    def __init__(self, accounts: Accounts):
        self._accounts = accounts
        self._active: str | None = None

    def _dl(self) -> "Downloader":
        dl = self._accounts.dls.get(self._active or "")
        if dl is None:
            raise PermissionError("לא מחובר בתוכנה")
        return dl

    def _sign_out(self) -> None:
        self._active = None

    def sign_out(self) -> bool:
        """The site's sign-out button: lock this profile's vault too."""
        self._active = None
        lock_all_vaults()
        return True

    # -- sign in (the site's sign-in page calls this before submitting)
    def remember(self, pid: str, password: str) -> bool:
        try:
            r = self._accounts.remember(str(pid), str(password))
        except Exception as e:
            traceback.print_exc()
            return False
        if r.get("ok"):
            self._active = r["pid"]
            return True
        return False

    def download_now(self, vid: str) -> str:
        return self._dl().download_now(vid)

    # -- pictures made with Gemini (kept scrambled; see images.py)
    def _gallery(self):
        from images import Gallery
        dl = self._dl()
        if dl.cfg.get("netfree"):
            raise PermissionError("לא זמין בפרופיל הזה")
        return Gallery(USERDATA / f"images-{dl.pid}")

    def img_save(self, b64: str, prompt: str) -> str:
        return self._gallery().add(str(b64), str(prompt))

    def img_list(self) -> list:
        return self._gallery().list()

    def img_get(self, iid: str) -> str:
        return self._gallery().data_url(str(iid))

    def img_export(self, iid: str) -> str:
        dest = self._gallery().export(str(iid), Path.home() / "Pictures" / "Gemini")
        subprocess.Popen(["explorer", "/select,", str(dest)])
        return str(dest)

    def img_delete(self, iid: str) -> bool:
        self._gallery().delete(str(iid))
        return True

    def img_to_vault(self, iid: str) -> str:
        """Move a picture from the gallery into this profile's encrypted vault.
        The vault's public key encrypts it, so no password is needed to put it
        in; viewing it later needs the code. Then it's removed from the gallery."""
        dl = self._dl()
        if not dl.cfg.get("vault_dir"):
            raise PermissionError("אין כספת לפרופיל הזה")
        from vault import Vault
        g = self._gallery()
        tmp = Path(tempfile.mkdtemp(prefix="yt-img-"))
        try:
            src = g.export(str(iid), tmp)                      # a real image file, briefly
            prompt = next((m.get("prompt", "") for m in g.list() if m["id"] == str(iid)), "")
            Vault(dl.cfg["vault_dir"]).add(src, {"title": prompt[:120]}, remove_src=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        g.delete(str(iid))
        return "ok"

    # -- videos made with Gemini (Veo) — same as the pictures, own folder
    def _videos(self):
        from images import Gallery
        dl = self._dl()
        if dl.cfg.get("netfree"):
            raise PermissionError("לא זמין בפרופיל הזה")
        return Gallery(USERDATA / f"videos-{dl.pid}", ".mp4")

    def vid_save(self, b64: str, prompt: str) -> str:
        return self._videos().add(str(b64), str(prompt))

    def vid_list(self) -> list:
        return self._videos().list()

    def vid_get(self, vid: str) -> str:
        return self._videos().data_url(str(vid))

    def vid_export(self, vid: str) -> str:
        dest = self._videos().export(str(vid), Path.home() / "Videos" / "Gemini")
        subprocess.Popen(["explorer", "/select,", str(dest)])
        return str(dest)

    def vid_delete(self, vid: str) -> bool:
        self._videos().delete(str(vid))
        return True

    def vid_to_vault(self, vid: str) -> str:
        """Like img_to_vault: into the encrypted vault, out of the gallery."""
        dl = self._dl()
        if not dl.cfg.get("vault_dir"):
            raise PermissionError("אין כספת לפרופיל הזה")
        from vault import Vault
        g = self._videos()
        tmp = Path(tempfile.mkdtemp(prefix="yt-vid-"))
        try:
            src = g.export(str(vid), tmp)                      # a real video file, briefly
            prompt = next((m.get("prompt", "") for m in g.list() if m["id"] == str(vid)), "")
            Vault(dl.cfg["vault_dir"]).add(src, {"title": prompt[:120]}, remove_src=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        g.delete(str(vid))
        return "ok"

    def open_folder(self) -> None:
        dl = self._dl()
        if dl.cfg.get("vault_dir"):
            open_vault_window(dl.cfg)
        else:
            dl.folder().mkdir(parents=True, exist_ok=True)
            os.startfile(str(dl.folder()))

    def open_vault(self) -> bool:
        open_vault_window(self._dl().cfg)
        return True

    def pick_folder(self) -> str:
        """The user chooses where their own files go (not for a vault profile)."""
        dl = self._dl()
        if dl.cfg.get("vault_dir"):
            return ""
        import webview
        r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=str(dl.folder()))
        if r:
            dl.cfg["folder"] = r[0] if isinstance(r, (list, tuple)) else str(r)
            dl.save_cfg()
            dl.log(f"תיקיית ההורדות שונתה: {dl.cfg['folder']}", remote=False)
        return str(dl.folder())

    def show_file(self, vid: str) -> bool:
        """Open the downloads folder with this video's file selected (or the
        vault, for files kept there)."""
        dl = self._dl()
        path = dl.state.get("files", {}).get(vid)
        if path and path.endswith(".ytv"):
            open_vault_window(dl.cfg)
            return True
        if path and Path(path).exists():
            subprocess.Popen(["explorer", "/select,", path])
            return True
        self.open_folder()
        return False

    def forget(self, vids: list, delete_files: bool = False) -> int:
        """Deleted from the site's history: forget them here too, and
        optionally delete the files from the computer."""
        dl = self._dl()
        st, n = dl.state, 0
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
        dl.save_state()
        return n

    def downloaded(self) -> list:
        """Which videos this computer already has (the site shows a folder button)."""
        try:
            dl = self._dl()
        except PermissionError:
            return []
        return [v for v, p in dl.state.get("files", {}).items() if Path(p).exists()]

    def app_info(self, pid: str = "") -> dict:
        dl = self._dl()
        if pid and pid != self._active:
            raise PermissionError("פרופיל אחר")
        return {"folder": "כספת מוצפנת" if dl.cfg.get("vault_dir") else str(dl.folder()),
                "vault": bool(dl.cfg.get("vault_dir")), "version": bundled_version_name(), "autostart": get_autostart(),
                "notify": dl.cfg.get("notify") or "each", "admin": dl.pid == "main"}

    def get_notify(self) -> str:
        return self._dl().cfg.get("notify") or "each"

    def set_notify(self, mode: str) -> str:
        dl = self._dl()
        if mode in NOTIFY_MODES:
            dl.cfg["notify"] = mode
            dl.save_cfg()
        return dl.cfg.get("notify") or "each"

    def set_autostart(self, on: bool) -> bool:
        set_autostart(bool(on))
        return get_autostart()

    def version_info(self) -> dict:
        """Installed version vs. the newest one, with what's new (the same
        request the app makes anyway to update itself; cached a minute)."""
        out = {"installed": bundled_version_name(), "changelog": _bundled("CHANGELOG.md"), "frozen": getattr(sys, "frozen", False)}
        try:
            lat = latest_release_info(max_age=60)
            out.update(latest=lat["name"] or lat["sha"][:7], notes=lat["notes"], published=lat["published"],
                       update=getattr(sys, "frozen", False) and not same_version(bundled_version(), lat["sha"]))
        except Exception as e:
            out.update(latest="", error=f"אין חיבור לבדיקת גרסה ({type(e).__name__})", update=False)
        return out

    def update_now(self) -> str:
        """Install the newest version now and restart (in the background)."""
        if not getattr(sys, "frozen", False):
            return "לא זמין בהרצה מקוד"
        if self_update(["--after-update", str(os.getpid())]):
            if _QUIT.get("fn"):
                threading.Timer(1.0, _QUIT["fn"]).start()
            return "ok"
        return "אין גרסה חדשה, או שההורדה נכשלה (פרטים בלוג)"

    def restart(self) -> str:
        return restart_app()

    def nf_test(self, link: str) -> str:
        """Admin: check one video against NetFree right now (to calibrate)."""
        dl = self._dl()
        if dl.pid != "main":
            raise PermissionError("רק למנהל")
        import re
        m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", link or "") or re.fullmatch(r"([\w-]{11})", (link or "").strip())
        if not m:
            return "לא זיהיתי קישור לסרטון"
        from netfree import probe
        verdict, detail = probe(m.group(1))
        res = {"open": "פתוח בנטפרי", "blocked": "חסום בנטפרי", "unknown": "לא ברור"}[verdict]
        dl.log(f"בדיקת נטפרי ידנית: {m.group(1)} → {res} ({detail})")
        dl.flush_logs()
        return f"{res}\n{detail}"


_VAULT: dict = {}          # pid -> {"win", "api"}
_QUIT: dict = {}           # "fn": closes the app (set by open_window)


def lock_all_vaults() -> None:
    for v in list(_VAULT.values()):
        try:
            v["api"].lock()
            if v.get("win"):
                v["win"].destroy()
        except Exception:
            pass
    _VAULT.clear()


def open_vault_window(cfg: dict) -> None:
    """One vault window per profile; showing it again brings it forward.
    A vault locked with the profile's code opens straight away (the code was
    just entered to sign in)."""
    import webview
    from vault_ui import VAULT_HTML, VaultApi
    pid = cfg.get("pid") or "?"
    ent = _VAULT.get(pid) or {}
    w = ent.get("win")
    if w is not None:
        try:
            w.show()
            w.restore()
            return
        except Exception:
            pass
    api = ent.get("api") or VaultApi(cfg, TMP / pid)
    if cfg.get("vault_auto") and cfg.get("password") and api._v and not api._v.unlocked:
        try:
            api.unlock(cfg["password"])
        except Exception:
            pass
    w = webview.create_window("הכספת — " + (cfg.get("name") or "מעקב ערוצים"), html=VAULT_HTML, js_api=api,
                              width=900, height=640, min_size=(420, 420), background_color="#0e1015")
    api._window = w
    _VAULT[pid] = {"win": w, "api": api}

    def closed():
        api.lock()                       # closing the vault window locks it
        _VAULT.get(pid, {}).pop("win", None)
    w.events.closed += closed


def restart_app() -> str:
    """Close this copy and start a fresh one — which, as on every start,
    installs a newer version first if there is one."""
    if not getattr(sys, "frozen", False):
        return "לא זמין בהרצה מקוד"
    args = [a for a in sys.argv[1:] if a not in ("--after-update", "--background") and not a.isdigit()]
    subprocess.Popen([sys.executable] + args + ["--after-update", str(os.getpid())], close_fds=True)
    if _QUIT.get("fn"):
        threading.Timer(1.0, _QUIT["fn"]).start()
    return "ok"


def make_tray(on_open, on_quit):
    """Icon next to the clock: keeps the app running with the window closed.
    Nothing personal in its menu — anyone at the computer can click it."""
    try:
        import pystray
        from PIL import Image
        img = Image.open(icon_path())
        menu = pystray.Menu(
            pystray.MenuItem("פתח", lambda: on_open(), default=True),
            pystray.MenuItem("הפעל מחדש (ומתעדכן)", lambda: restart_app()),
            pystray.MenuItem("יציאה", lambda: on_quit()))
        return pystray.Icon(APP, img, f"מעקב ערוצים {bundled_version_name()} — מוריד ברקע", menu)
    except Exception:
        traceback.print_exc()
        return None


def open_window(accounts: Accounts, background: bool) -> None:
    url = accounts.site + "/yt/"
    state = {"quit": False}
    try:
        import webview  # pywebview — uses Edge WebView2, built into Windows 10/11
    except Exception:
        webview = None

    if webview is None:
        # No WebView2: tray only; "open" uses the normal browser.
        tray = make_tray(lambda: webbrowser.open(url), lambda: tray.stop())
        if not background:
            webbrowser.open(url)
        if tray:
            accounts.notify = lambda t, m: tray.notify(m, t)
            tray.run()
        else:
            while True:
                time.sleep(3600)
        return

    api = Api(accounts)
    win = webview.create_window("מעקב ערוצים", url, width=1200, height=820, min_size=(420, 520),
                                hidden=background, js_api=api, background_color="#0e1015")

    def show():
        win.show()
        win.restore()

    def quit_app():
        state["quit"] = True
        lock_all_vaults()
        if tray:
            tray.stop()
        win.destroy()

    tray = make_tray(show, quit_app)

    def quit_for_update():
        state["quit"] = True
        lock_all_vaults()
        if tray:
            tray.stop()
        try:
            win.destroy()
        finally:
            threading.Timer(10, lambda: os._exit(0)).start()
    _QUIT["fn"] = quit_for_update

    def on_closing():
        # Closing the window signs out (next time: "who's signing in?") and
        # locks the vaults; every profile keeps downloading in the tray.
        if state["quit"] or tray is None:
            return True
        win.hide()
        api._sign_out()
        lock_all_vaults()
        try:
            win.load_url(url + "logout")
        except Exception:
            pass
        if not state.get("told"):
            state["told"] = True
            tray.notify("התנתקת. התוכנה ממשיכה להוריד ברקע לכל הפרופילים. ליציאה: לחיצה ימנית על האייקון ליד השעון ← יציאה.", APP)
        return False

    win.events.closing += on_closing

    def keep_updated():
        """A new version installs itself even while the app keeps running
        in the tray: download, swap, start the new one in the background,
        close this one. (Downloads continue where they stopped.)"""
        while not state["quit"]:
            time.sleep(UPDATE_CHECK_HOURS * 3600)
            try:
                if self_update(["--background", "--after-update", str(os.getpid())]):
                    quit_for_update()
                    return
            except Exception:
                traceback.print_exc()
    if getattr(sys, "frozen", False):
        threading.Thread(target=keep_updated, daemon=True).start()
    if tray:
        accounts.notify = lambda t, m: tray.notify(m, t)
        tray.run_detached()
    listen_for_show(show)
    # private_mode: nothing of the sign-in is kept between openings.
    webview.start(private_mode=True)
    if tray:
        tray.stop()


# ---------------------------------------------------------------- updates

def _bundled(name: str) -> str:
    try:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        return (base / name).read_text(encoding="utf-8")
    except Exception:
        return ""


def bundled_version() -> str:
    """Commit this build was made from (version.txt line 1, written by the build)."""
    return (_bundled("version.txt").strip().splitlines() or [""])[0].strip()


def bundled_version_name() -> str:
    """The version people see, e.g. "1.16" (version.txt line 2)."""
    lines = _bundled("version.txt").strip().splitlines()
    return lines[1].strip() if len(lines) > 1 else (lines[0][:7] if lines else "פיתוח")


_LATEST: dict = {}


def latest_release_info(max_age: int = 0) -> dict:
    """The newest published build: commit, version name, what's new, download
    URL. One small request to GitHub (cached for max_age seconds)."""
    if max_age and _LATEST and time.time() - _LATEST.get("at", 0) < max_age:
        return _LATEST
    r = requests.get(RELEASE_API, timeout=15, headers={"User-Agent": "yt-tracker-desktop"})
    r.raise_for_status()
    rel = r.json()
    url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == "YTTracker.exe"), "")
    zurl = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == "YTTracker.zip"), "")
    import re
    m = re.search(r"\d+\.\d+", rel.get("name") or "")
    notes = (rel.get("body") or "").split("\nהורדה:")[0].strip()
    _LATEST.clear()
    _LATEST.update(at=time.time(), sha=rel.get("target_commitish", ""), name=m.group(0) if m else "",
                   notes=notes, url=url or zurl, published=rel.get("published_at", ""))
    return _LATEST


def latest_release() -> tuple[str, str]:
    """(commit, exe download URL) of the newest published build."""
    info = latest_release_info()
    return info["sha"], info["url"]


def same_version(a: str, b: str) -> bool:
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def self_update(extra_args: list | None = None) -> bool:
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
        args = [a for a in sys.argv[1:] if a not in ("--after-update",) and not a.isdigit()]
        subprocess.Popen([str(exe)] + args + (extra_args or []), close_fds=True)
        return True
    except Exception as e:
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
    if getattr(sys, "frozen", False) and not is_installed_copy():
        close_other_copies()             # old versions would block installing
    if "--after-update" in sys.argv:
        # Started by the previous version, which is closing now: wait for it
        # (it still holds the "already running" port for a moment).
        try:
            import ctypes
            old_pid = int(sys.argv[sys.argv.index("--after-update") + 1])
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, old_pid)   # SYNCHRONIZE
            if h:
                ctypes.windll.kernel32.WaitForSingleObject(h, 30000)
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            time.sleep(5)
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
    accounts = Accounts(load_json(CONFIG, {}).get("site") or DEFAULT_SITE)
    if not accounts.data and USERCFG.exists():
        accounts.add_legacy(load_cfg())          # the profile set up before accounts
    accounts.start_all()
    open_window(accounts, background="--background" in sys.argv)

if __name__ == "__main__":
    main()
