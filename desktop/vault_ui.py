"""The vault window: unlock with a password, browse, play in the regular
player, export, delete, and send to an Android phone over USB.

Android phones connect over MTP ("File transfer" mode). They have no drive
letter, so copying goes through Windows' own Shell (Shell.Application) —
the same thing File Explorer uses — driven by a small PowerShell script.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AUTO_LOCK_MINUTES = 15

PS_DEVICES = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$sh = New-Object -ComObject Shell.Application
$out = @()
foreach ($d in $sh.NameSpace(17).Items()) {
  if ($d.IsFolder -and $d.Path -notmatch '^[A-Za-z]:\\') {
    $st = @($d.GetFolder.Items() | Where-Object { $_.IsFolder } | ForEach-Object { $_.Name })
    $out += [pscustomobject]@{ device = $d.Name; storages = $st }
  }
}
ConvertTo-Json -Compress -InputObject @($out)
"""

# Walk "device / storage / folder / sub…" by display name (MTP has no paths).
PS_NAV = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$sh = New-Object -ComObject Shell.Application
function Child($folder, $name) { $folder.Items() | Where-Object { $_.Name -eq $name } | Select-Object -First 1 }
$dev = Child $sh.NameSpace(17) $env:YT_DEV
if (-not $dev) { 'NODEVICE'; exit 2 }
$stor = Child $dev.GetFolder $env:YT_STOR
if (-not $stor) { 'NOSTORAGE'; exit 3 }
$cur = $stor.GetFolder
$parts = @(($env:YT_PATH -split '/') | Where-Object { $_ -ne '' })
foreach ($p in $parts) {
  $n = Child $cur $p
  if (-not $n -and $env:YT_CREATE -eq '1') { $cur.NewFolder($p); Start-Sleep -Seconds 2; $n = Child $cur $p }
  if (-not $n) { 'NOFOLDER'; exit 4 }
  $cur = $n.GetFolder
}
"""

PS_FOLDERS = PS_NAV + r"""
$names = @($cur.Items() | Where-Object { $_.IsFolder } | ForEach-Object { $_.Name } | Sort-Object)
ConvertTo-Json -Compress -InputObject $names
"""

PS_MKDIR = PS_NAV + r"""
if (-not (Child $cur $env:YT_NEW)) { $cur.NewFolder($env:YT_NEW); Start-Sleep -Seconds 2 }
if (Child $cur $env:YT_NEW) { 'OK' } else { 'FAILED'; exit 5 }
"""

PS_SEND = PS_NAV + r"""
$name = Split-Path $env:YT_FILE -Leaf
$base = [IO.Path]::GetFileNameWithoutExtension($name)
$cur.CopyHere($env:YT_FILE, 4 + 16)
for ($i = 0; $i -lt 1800; $i++) {
  Start-Sleep -Seconds 1
  if ($cur.Items() | Where-Object { $_.Name -eq $name -or $_.Name -eq $base }) { Start-Sleep -Seconds 3; 'OK'; exit 0 }
}
'TIMEOUT'; exit 1
"""
PS_ERRORS = {"NODEVICE": "הטלפון לא מחובר", "NOSTORAGE": "האחסון בטלפון לא נמצא", "NOFOLDER": "התיקייה בטלפון לא נמצאה",
             "TIMEOUT": "ההעתקה לא הסתיימה בזמן", "FAILED": "לא הצלחתי ליצור תיקייה"}



def _ps(script: str, env: dict | None = None, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       env={**os.environ, **(env or {})}, creationflags=NO_WINDOW)
    return r.returncode, (r.stdout or "").strip()


class VaultApi:
    """Called from the vault window's page (window.pywebview.api.…)."""

    def __init__(self, cfg: dict, tmp_dir: Path):
        from vault import Vault
        self._cfg, self._tmp = cfg, tmp_dir
        self._v = Vault(cfg["vault_dir"]) if cfg.get("vault_dir") else None
        self._last = time.time()
        self._window = None
        threading.Thread(target=self._auto_lock, daemon=True).start()

    # -- housekeeping
    def _touch(self):
        self._last = time.time()

    def _auto_lock(self):
        while True:
            time.sleep(30)
            if self._v and self._v.unlocked and time.time() - self._last > AUTO_LOCK_MINUTES * 60:
                self.lock()
                try:
                    self._window.evaluate_js("locked()")
                except Exception:
                    pass

    def wipe_tmp(self):
        """Remove decrypted copies (a file still open in a player is skipped
        and removed next time)."""
        if self._tmp.exists():
            for p in sorted(self._tmp.rglob("*"), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass

    def _require(self):
        if not self._v or not self._v.unlocked:
            raise PermissionError("הכספת נעולה")
        self._touch()

    # -- API
    def info(self) -> dict:
        return {"configured": bool(self._v), "exists": bool(self._v and self._v.exists),
                "unlocked": bool(self._v and self._v.unlocked), "autolock": AUTO_LOCK_MINUTES}

    def unlock(self, password: str) -> bool:
        ok = bool(self._v and self._v.unlock(password))
        self._touch()
        return ok

    def lock(self) -> bool:
        if self._v:
            self._v.lock()
        self.wipe_tmp()
        return True

    def change_password(self, old: str, new: str) -> bool:
        return bool(self._v and len(new) >= 6 and self._v.change_password(old, new))

    def list(self) -> list:
        self._require()
        return self._v.list()

    def image(self, eid: str) -> str:
        """An image entry as a data URL, decrypted in memory only (no file on
        disk), for showing inside the vault. Up to 25MB."""
        self._require()
        import base64
        data, h = self._v.read_bytes(eid, limit=25 << 20)
        ext = (h.get("name", "").rsplit(".", 1)[-1] or "png").lower()
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "bmp": "bmp"}.get(ext, "png")
        return f"data:image/{mime};base64," + base64.b64encode(data).decode()

    def play(self, eid: str) -> str:
        """Decrypt to this user's private temp folder and open in the default player."""
        self._require()
        d = self._tmp / eid
        path = self._v.extract(eid, d)
        os.startfile(str(path))
        return path.name

    def export(self, eids: list) -> str:
        """Decrypt the chosen files into a folder the user picks (USB stick, …)."""
        self._require()
        import webview
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return ""
        dest = Path(res[0] if isinstance(res, (list, tuple)) else res)
        self._op_start("export", eids)
        try:
            for eid in eids:
                if self._op["cancel"]:
                    break
                self._op_file(eid)
                self._v.extract(eid, dest, progress=self._op_progress)
                self._op_done_file()
        finally:
            self._op["running"] = False
        return str(dest)

    # -- progress of a running export / phone transfer (polled by the page)
    def _op_start(self, kind: str, eids: list) -> None:
        items = self._v.list() if self._v else []
        sizes = {x["id"]: int(x.get("size") or 0) for x in items}
        self._names = {x["id"]: x.get("title") or x.get("name") or x["id"] for x in items}
        self._op = {"kind": kind, "running": True, "cancel": False, "total": len(eids), "done": 0,
                    "bytes_total": sum(sizes.get(e, 0) for e in eids) or 1, "bytes_done": 0,
                    "sizes": sizes, "current": "", "stage": "", "file_bytes": 0, "file_done": 0,
                    "copy_started": 0.0, "started": time.time(), "failed": []}

    def _op_file(self, eid: str) -> None:
        self._op.update(current=getattr(self, "_names", {}).get(eid, ""), stage="decrypt", file_bytes=self._op["sizes"].get(eid, 0) or 1, file_done=0)

    def _op_progress(self, written: int) -> None:
        self._op["file_done"] = written
        self._touch()

    def _op_done_file(self) -> None:
        self._op["bytes_done"] += self._op["file_bytes"]
        self._op["done"] += 1
        self._op.update(stage="", file_done=0)

    def op_status(self) -> dict:
        op = getattr(self, "_op", None)
        if not op:
            return {"running": False}
        # Export: decrypting is the whole job. Phone: decrypt ~25%, copy ~75%
        # of each file (MTP doesn't report progress — estimated at ~20 MB/s).
        fb, fd = op["file_bytes"], op["file_done"]
        if op["kind"] == "export":
            frac = fd / fb if fb else 0
        elif op["stage"] == "decrypt":
            frac = 0.25 * (fd / fb if fb else 0)
        elif op["stage"] == "copy":
            frac = 0.25 + 0.75 * min(0.95, (time.time() - op["copy_started"]) * 20e6 / fb)
        else:
            frac = 0
        done = op["bytes_done"] + frac * fb
        pct = min(100.0, 100 * done / op["bytes_total"]) if op["running"] else 100.0
        el = time.time() - op["started"]
        eta = el * (100 - pct) / pct if op["running"] and pct > 2 else None
        return {"running": op["running"], "kind": op["kind"], "total": op["total"], "done": op["done"],
                "current": op["current"], "stage": op["stage"], "pct": pct, "file_pct": 100 * frac,
                "elapsed": el, "eta": eta, "failed": len(op["failed"])}

    def op_cancel(self) -> bool:
        if getattr(self, "_op", None):
            self._op["cancel"] = True
        return True

    def delete(self, eids: list) -> int:
        self._require()
        for eid in eids:
            self._v.delete(eid)
        return len(eids)

    def devices(self) -> list:
        """Android phones connected by USB in 'File transfer' mode."""
        self._touch()
        try:
            code, out = _ps(PS_DEVICES, timeout=60)
            data = json.loads(out or "[]")
            return data if isinstance(data, list) else [data]
        except Exception:
            return []

    def _target_file(self) -> Path:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "YTTracker"
        return base / f"phone-{self._cfg.get('pid') or 'main'}.json"

    def last_target(self) -> dict:
        try:
            return json.loads(self._target_file().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def phone_folders(self, device: str, storage: str, path: str = "") -> list:
        """Folders inside device/storage/path on the phone."""
        self._touch()
        code, out = _ps(PS_FOLDERS, {"YT_DEV": device, "YT_STOR": storage, "YT_PATH": path or ""}, timeout=90)
        if code != 0:
            raise RuntimeError(PS_ERRORS.get(out.strip().splitlines()[-1] if out else "", out or str(code)))
        data = json.loads(out or "[]")
        return data if isinstance(data, list) else [data]

    def phone_mkdir(self, device: str, storage: str, path: str, name: str) -> bool:
        self._touch()
        name = "".join(c for c in (name or "") if c not in '<>:"/\\|?*').strip()
        if not name:
            return False
        code, out = _ps(PS_MKDIR, {"YT_DEV": device, "YT_STOR": storage, "YT_PATH": path or "", "YT_NEW": name}, timeout=90)
        if code != 0:
            raise RuntimeError(PS_ERRORS.get(out.strip().splitlines()[-1] if out else "", out or str(code)))
        return True

    def send(self, eids: list, device: str, storage: str, path: str) -> dict:
        """Copy to the phone: decrypt → Windows copies it over MTP → the
        decrypted temp copy is removed. Progress: op_status()."""
        self._require()
        try:
            self._target_file().parent.mkdir(parents=True, exist_ok=True)
            self._target_file().write_text(json.dumps({"device": device, "storage": storage, "path": path or ""},
                                                      ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self._op_start("phone", eids)
        op, ok = self._op, 0
        try:
            for eid in eids:
                if op["cancel"]:
                    break
                d = self._tmp / ("send-" + eid)
                self._op_file(eid)
                try:
                    path_ = self._v.extract(eid, d, progress=self._op_progress)
                    op.update(stage="copy", copy_started=time.time())
                    code, out = _ps(PS_SEND, {"YT_DEV": device, "YT_STOR": storage, "YT_PATH": path or "",
                                              "YT_CREATE": "1", "YT_FILE": str(path_)}, timeout=1900)
                    if code == 0 and out.endswith("OK"):
                        ok += 1
                    else:
                        last = out.strip().splitlines()[-1] if out else str(code)
                        op["failed"].append(f"{path_.name}: {PS_ERRORS.get(last, last)}")
                except Exception as e:
                    op["failed"].append(f"{eid}: {e}")
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                self._op_done_file()
                self._touch()
        finally:
            op["running"] = False
        return {"ok": ok, "failed": op["failed"], "cancelled": op["cancel"]}


VAULT_HTML = r"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><title>הכספת</title>
<style>
:root{--bg:#0e1015;--panel:#171a21;--panel2:#1e222b;--line:#262b36;--text:#e7e9ee;--muted:#8a92a3;--primary:#4f8cff;--ok:#2fbf87;--bad:#f06464}
*{box-sizing:border-box}body{margin:0;font-family:"Segoe UI",Rubik,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14.5px}
.i{width:18px;height:18px;vertical-align:middle;flex:none}
.top{display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid var(--line)}
.top h1{flex:1;margin:0;font-size:1.15rem;display:flex;align-items:center;gap:8px}
.top .sum{color:var(--muted);font-size:.85rem}
.btn{display:inline-flex;align-items:center;gap:7px;font:inherit;border:1px solid transparent;border-radius:9px;padding:8px 13px;cursor:pointer;background:var(--primary);color:#fff;white-space:nowrap}
.btn.ghost{background:var(--panel);border-color:var(--line);color:var(--text)}.btn.danger{background:var(--panel);border-color:var(--line);color:var(--bad)}
.btn:disabled{opacity:.45;cursor:default}
.wrap{padding:16px 20px}
.center{max-width:360px;margin:12vh auto;text-align:center;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}
.center svg{width:44px;height:44px;color:var(--primary)}
input,select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:8px 10px}
input[type=checkbox]{width:17px;height:17px;padding:0;accent-color:var(--primary);cursor:pointer}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.bar .search{flex:1;min-width:160px;max-width:300px}
.selbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;padding:8px 12px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.selbar label{display:flex;align-items:center;gap:8px;cursor:pointer}
.selbar .info{color:var(--muted);font-size:.85rem}
.list{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none}
.row:last-child{border-bottom:0}.row:hover{background:#ffffff06}.row.sel{background:#4f8cff14}
.row .ic{color:var(--muted)}.row .name{flex:1;min-width:0}
.row .ic.thumb{width:40px;height:40px;border-radius:7px;overflow:hidden;background:var(--panel2);display:grid;place-items:center}
.row .ic.thumb img{width:100%;height:100%;object-fit:cover}
#lightbox{position:fixed;inset:0;background:#000d;display:grid;place-items:center;z-index:100;cursor:zoom-out}
#lightbox img{max-width:94vw;max-height:94vh;border-radius:8px}
.row .name b{display:block;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.muted{color:var(--muted);font-size:.8rem}
.ibtn{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--muted);cursor:pointer;flex:none}
.ibtn:hover{color:var(--text)}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);padding:10px 16px;border-radius:10px;display:none;z-index:9;max-width:90vw}
.modal{position:fixed;inset:0;background:#000a;display:none;place-items:center;z-index:5}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;width:min(520px,94vw);max-height:92vh;display:flex;flex-direction:column}
.modal label.f{display:block;margin:10px 0 4px;color:var(--muted);font-size:.85rem}
.modal select{width:100%}
.crumbs{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:12px 0 8px;font-size:.9rem}
.crumbs a{color:var(--primary);cursor:pointer;text-decoration:none}.crumbs span{color:var(--muted)}
.folders{border:1px solid var(--line);border-radius:10px;overflow:auto;min-height:120px;max-height:280px;background:var(--bg)}
.folder{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.folder:last-child{border-bottom:0}.folder:hover{background:#ffffff08}.folder .i{color:#f0b43c}
.prog{height:6px;background:var(--bg);border-radius:99px;overflow:hidden;margin:10px 0 4px}.prog i{display:block;height:100%;background:var(--primary);transition:width .3s}
.empty{padding:40px;text-align:center;color:var(--muted)}
.opanel{position:fixed;left:16px;bottom:16px;width:min(360px,calc(100vw - 32px));background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:0 8px 30px #0008;z-index:8}
.opanel .h{display:flex;align-items:center;gap:8px;font-weight:600}.opanel .h .pct{margin-inline-start:auto;font-size:1.3rem;font-variant-numeric:tabular-nums}
.opanel .ln{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:.8rem;margin-top:4px}
.opanel .ln b{color:var(--text);font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prog.thin{height:4px;margin:6px 0 2px}.prog.busy i{background:repeating-linear-gradient(45deg,var(--primary) 0 10px,#7aa7ff 10px 20px);animation:mv 1s linear infinite;background-size:28px 28px}
@keyframes mv{to{background-position:28px 0}}
.hidden{display:none!important}
</style></head><body>
<div class="top"><h1><svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>הכספת</h1>
<span class="sum" id="sum"></span>
<button class="btn ghost" style="display:none" id="lockBtn" onclick="doLock()">נעל</button></div>
<div id="view"></div>

<div class="modal" id="phone"><div class="box">
  <h3 style="margin:0 0 4px">העברה לטלפון</h3>
  <p class="muted" style="margin:0">חבר את הטלפון ב-USB, פתח את נעילת המסך ובחר בטלפון "העברת קבצים". <span id="phoneCount"></span></p>
  <label class="f">טלפון ואחסון</label>
  <div style="display:flex;gap:8px"><select id="dev" onchange="pickDev()"></select><button class="btn ghost" onclick="scan()" title="חפש שוב">רענן</button></div>
  <div class="crumbs" id="crumbs"></div>
  <div class="folders" id="folders"></div>
  <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
    <button class="btn ghost" onclick="upDir()" id="upBtn">למעלה</button>
    <button class="btn ghost" onclick="newDir()">תיקייה חדשה</button>
    <span style="flex:1"></span>
    <button class="btn ghost" onclick="quick('Music')">Music</button><button class="btn ghost" onclick="quick('Movies')">Movies</button><button class="btn ghost" onclick="quick('Download')">Download</button>
  </div>
  <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
    <button class="btn ghost" id="closeBtn" onclick="closePhone()">סגור</button>
    <button class="btn" id="sendBtn" onclick="send()">העבר לכאן</button>
  </div></div></div>
<div class="opanel hidden" id="opanel">
  <div class="h"><span id="opTitle">מעביר לטלפון</span><span class="pct" id="opPct">0%</span></div>
  <div class="prog"><i id="opBar" style="width:0"></i></div>
  <div class="ln"><b id="opFile"></b><span id="opCount"></span></div>
  <div class="prog thin" id="opFileProg"><i id="opFileBar" style="width:0"></i></div>
  <div class="ln"><span id="opStage"></span><span id="opEta"></span></div>
  <div style="display:flex;justify-content:flex-end;margin-top:10px"><button class="btn danger" id="opStop" onclick="api().op_cancel();this.disabled=true;toast('עוצר אחרי הקובץ הנוכחי…')">עצור</button></div>
</div>
<div id="toast"></div>
<script>
const api = () => window.pywebview.api;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const size = (b) => b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + " GB" : (b / (1 << 20)).toFixed(1) + " MB";
const SVG = (p) => '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
const IC = { video: SVG('<path d="M23 7l-7 5 7 5z"/><rect x="1" y="5" width="15" height="14" rx="2"/>'),
  audio: SVG('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'),
  file: SVG('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>'),
  image: SVG('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>'),
  folder: SVG('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
  play: '<svg class="i" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4l14 8-14 8z"/></svg>' };
const kindOf = (x) => { const e = (x.name || "").split(".").pop().toLowerCase();
  return ["mp4", "webm", "mkv", "mov", "avi"].includes(e) ? "video" : ["mp3", "m4a", "opus", "ogg", "wav", "flac", "aac"].includes(e) ? "audio"
    : ["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(e) ? "image" : "file"; };
let ITEMS = [], SEL = new Set(), Q = "", KIND = "", SORT = "new", LAST = null;
function toast(m, ms) { const t = $("toast"); t.textContent = m; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms || 3500); }
async function start() {
  const i = await api().info();
  if (!i.configured || !i.exists) { $("view").innerHTML = '<div class="center"><p>לפרופיל הזה אין כספת.</p><p class="muted">את הכספת מפעיל המנהל בהגדרות ← פרופילים.</p></div>'; return; }
  i.unlocked ? load() : locked();
}
function locked() {
  $("lockBtn").style.display = "none"; $("sum").textContent = ""; ITEMS = []; SEL.clear();
  $("view").innerHTML = '<div class="center"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>' +
    '<h2>הכספת נעולה</h2><input type="password" id="pw" placeholder="סיסמת הכספת" style="width:100%" onkeydown="if(event.key===\'Enter\')unlock()">' +
    '<p><button class="btn" style="width:100%;justify-content:center" onclick="unlock()">פתח</button></p><p class="muted" id="err"></p></div>';
  setTimeout(() => $("pw") && $("pw").focus(), 50);
}
async function unlock() {
  $("err").textContent = "בודק…";
  if (await api().unlock($("pw").value)) load(); else { $("err").textContent = "סיסמה שגויה"; $("pw").select(); }
}
async function doLock() { await api().lock(); locked(); }
async function load() {
  $("lockBtn").style.display = "";
  try { ITEMS = await api().list(); } catch (e) { return locked(); }
  ITEMS.forEach((x) => { x.kind = kindOf(x); });
  const ids = new Set(ITEMS.map((x) => x.id)); SEL.forEach((id) => { if (!ids.has(id)) SEL.delete(id); });
  render();
}
function shown() {
  const q = Q.toLowerCase();
  const out = ITEMS.filter((x) => (!q || ((x.title || "") + " " + x.name).toLowerCase().includes(q)) && (!KIND || x.kind === KIND));
  const by = { new: (a, b) => (b.added || "").localeCompare(a.added || ""), old: (a, b) => (a.added || "").localeCompare(b.added || ""),
    name: (a, b) => (a.title || a.name).localeCompare(b.title || b.name, "he"), size: (a, b) => (b.size || 0) - (a.size || 0) };
  return out.sort(by[SORT]);
}
function render() {
  const list = shown(), selSize = ITEMS.filter((x) => SEL.has(x.id)).reduce((s, x) => s + (x.size || 0), 0);
  const allOn = list.length && list.every((x) => SEL.has(x.id)), some = list.some((x) => SEL.has(x.id));
  $("sum").textContent = ITEMS.length + " קבצים · " + size(ITEMS.reduce((s, x) => s + (x.size || 0), 0));
  const focus = document.activeElement && document.activeElement.id === "q";
  $("view").innerHTML = '<div class="wrap"><div class="bar">' +
    '<input class="search" id="q" placeholder="חיפוש…" value="' + esc(Q) + '" oninput="Q=this.value;render()">' +
    '<select onchange="KIND=this.value;render()"><option value="">כל הסוגים</option><option value="video"' + (KIND === "video" ? " selected" : "") + '>סרטונים</option><option value="audio"' + (KIND === "audio" ? " selected" : "") + '>שירים</option><option value="image"' + (KIND === "image" ? " selected" : "") + '>תמונות</option></select>' +
    '<select onchange="SORT=this.value;render()">' + [["new", "חדש קודם"], ["old", "ישן קודם"], ["name", "לפי שם"], ["size", "הגדולים קודם"]].map(([v, l]) => '<option value="' + v + '"' + (SORT === v ? " selected" : "") + '>' + l + '</option>').join("") + '</select>' +
    '<button class="btn ghost" onclick="load()">רענן</button></div>' +
    '<div class="selbar"><label><input type="checkbox" id="all" ' + (allOn ? "checked" : "") + ' onchange="selAll(this.checked)">בחר הכל' + (Q || KIND ? " (מהמוצגים)" : "") + '</label>' +
    '<span class="info">' + (SEL.size ? "נבחרו " + SEL.size + " · " + size(selSize) : "לא נבחר כלום — לחיצה על שורה בוחרת, Shift לטווח, לחיצה כפולה מנגנת") + '</span><span style="flex:1"></span>' +
    (SEL.size ? '<button class="btn ghost" onclick="SEL.clear();render()">נקה בחירה</button><button class="btn ghost" onclick="invert()">הפוך בחירה</button>' : '') +
    '<button class="btn" ' + (SEL.size ? "" : "disabled") + ' onclick="openPhone()">' + SVG('<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/>') + 'העבר לטלפון</button>' +
    '<button class="btn ghost" ' + (SEL.size ? "" : "disabled") + ' onclick="exportSel()">ייצוא לתיקייה</button>' +
    '<button class="btn danger" ' + (SEL.size ? "" : "disabled") + ' onclick="delSel()">מחק</button></div>' +
    '<div class="list">' + (list.length ? list.map((x) => '<div class="row' + (SEL.has(x.id) ? " sel" : "") + '" data-id="' + x.id + '">' +
      '<input type="checkbox" ' + (SEL.has(x.id) ? "checked" : "") + ' tabindex="-1"><span class="ic' + (x.kind === "image" ? " thumb" : "") + '" ' + (x.kind === "image" ? 'data-thumb="' + x.id + '"' : "") + '>' + IC[x.kind] + '</span>' +
      '<div class="name"><b title="' + esc(x.name) + '">' + esc(x.title || x.name) + '</b><span class="muted">' + esc(x.name) + ' · ' + size(x.size || 0) + ' · ' + esc((x.added || "").slice(0, 10)) + '</span></div>' +
      '<button class="ibtn" title="' + (x.kind === "image" ? "הצג" : "נגן") + '" data-' + (x.kind === "image" ? "view" : "play") + '="' + x.id + '">' + (x.kind === "image" ? IC.image : IC.play) + '</button></div>').join("")
      : '<div class="empty">' + (ITEMS.length ? "אין תוצאות" : "הכספת ריקה — קבצים חדשים ייכנסו אליה אוטומטית") + '</div>') + '</div></div>';
  const all = $("all"); if (all) all.indeterminate = some && !allOn;
  if (focus) { const q = $("q"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  setTimeout(loadThumbs, 0);
}
function selAll(on) { shown().forEach((x) => on ? SEL.add(x.id) : SEL.delete(x.id)); render(); }
function invert() { shown().forEach((x) => SEL.has(x.id) ? SEL.delete(x.id) : SEL.add(x.id)); render(); }
document.addEventListener("click", (e) => {
  const big = e.target.closest("#lightbox"); if (big) { big.remove(); return; }
  const vb = e.target.closest("[data-view]"); if (vb) { viewImage(vb.dataset.view); return; }
  const pb = e.target.closest("[data-play]"); if (pb) { play(pb.dataset.play); return; }
  const row = e.target.closest(".row[data-id]"); if (!row) return;
  const id = row.dataset.id, list = shown().map((x) => x.id);
  if (e.shiftKey && LAST) {
    const [a, b] = [list.indexOf(LAST), list.indexOf(id)].sort((x, y) => x - y);
    if (a >= 0) list.slice(a, b + 1).forEach((x) => SEL.add(x));
  } else SEL.has(id) ? SEL.delete(id) : SEL.add(id);
  LAST = id; render();
});
document.addEventListener("dblclick", (e) => { const row = e.target.closest(".row[data-id]"); if (!row) return;
  const x = ITEMS.find((i) => i.id === row.dataset.id); x && x.kind === "image" ? viewImage(x.id) : play(row.dataset.id); });
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && (e.key === "a" || e.key === "A") && document.activeElement.tagName !== "INPUT" && ITEMS.length) { e.preventDefault(); selAll(true); }
  if (e.key === "Escape" && $("phone").style.display !== "grid" && SEL.size) { SEL.clear(); render(); }
});
// ---------- progress panel (export / phone) ----------
const mmss = (s) => { s = Math.max(0, Math.round(s)); return s >= 3600 ? Math.floor(s / 3600) + ":" + String(Math.floor(s / 60) % 60).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0") : Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
let OPT = null;
function watchOp(title) {
  $("opTitle").textContent = title; $("opPct").textContent = "0%"; $("opBar").style.width = "0"; $("opFile").textContent = "מתחיל…";
  $("opCount").textContent = ""; $("opStage").textContent = ""; $("opEta").textContent = ""; $("opStop").disabled = false; $("opStop").classList.remove("hidden");
  $("opanel").classList.remove("hidden");
  clearInterval(OPT);
  OPT = setInterval(async () => { try { showOp(await api().op_status()); } catch (e) {} }, 700);
}
function showOp(s) {
  if (!s || !s.total) return;
  $("opPct").textContent = Math.floor(s.pct) + "%"; $("opBar").style.width = s.pct + "%";
  $("opFile").textContent = s.current || ""; $("opFile").title = s.current || "";
  $("opCount").textContent = Math.min(s.done + (s.running ? 1 : 0), s.total) + " מתוך " + s.total;
  const copy = s.stage === "copy";
  $("opFileProg").classList.toggle("busy", copy);
  $("opFileBar").style.width = (copy ? 100 : Math.min(100, s.kind === "export" ? s.file_pct : s.file_pct * 4)) + "%";
  $("opStage").textContent = s.stage === "decrypt" ? "מפענח " + Math.floor(s.kind === "export" ? s.file_pct : Math.min(100, s.file_pct * 4)) + "%" :
    copy ? "מעתיק לטלפון… (" + mmss(s.elapsed) + ")" : "";
  $("opEta").textContent = s.eta != null ? "נשאר בערך " + mmss(s.eta) : "";
}
function endOp(msg) {
  clearInterval(OPT); OPT = null;
  $("opPct").textContent = "100%"; $("opBar").style.width = "100%"; $("opFileBar").style.width = "100%"; $("opFileProg").classList.remove("busy");
  $("opFile").textContent = msg; $("opStage").textContent = ""; $("opEta").textContent = ""; $("opStop").classList.add("hidden");
  setTimeout(() => { if (!OPT) $("opanel").classList.add("hidden"); }, 8000);
}

async function play(id) { toast("פותח…"); try { toast("נפתח: " + await api().play(id)); } catch (e) { toast("שגיאה: " + e); } }
async function viewImage(id) {
  toast("מפענח…");
  try {
    const src = await api().image(id);
    const d = document.createElement("div"); d.id = "lightbox"; d.innerHTML = '<img src="' + src + '">';
    document.body.appendChild(d); toast("", 1);
  } catch (e) { toast("שגיאה: " + e); }
}
// Thumbnails are decrypted on demand, a few at a time, only while the vault is open.
let THUMBQ = [];
async function loadThumbs() {
  document.querySelectorAll(".thumb[data-thumb]").forEach((el) => { if (!el.dataset.done) THUMBQ.push(el); });
  if (loadThumbs.running) return; loadThumbs.running = true;
  while (THUMBQ.length) {
    const el = THUMBQ.shift(); if (!el.isConnected || el.dataset.done) continue;
    el.dataset.done = "1";
    try { el.innerHTML = '<img src="' + await api().image(el.dataset.thumb) + '">'; } catch (e) {}
  }
  loadThumbs.running = false;
}
async function exportSel() {
  const ids = [...SEL], t = setTimeout(() => watchOp("מייצא לתיקייה"), 600);   // after the folder is chosen
  try { const d = await api().export(ids); clearTimeout(t); if (d) endOp("יוצא אל " + d); else { clearTimeout(t); $("opanel").classList.add("hidden"); clearInterval(OPT); toast("בוטל"); } }
  catch (e) { clearTimeout(t); endOp("שגיאה: " + e); }
}
async function delSel() { if (!confirm("למחוק " + SEL.size + " קבצים מהכספת? אין דרך לשחזר.")) return; await api().delete([...SEL]); SEL.clear(); load(); toast("נמחק"); }

// ---------- phone: browse its folders ----------
let DEVS = [], DEV = null, PATH = [];
async function scan() {
  $("dev").innerHTML = "<option>מחפש טלפונים…</option>"; $("folders").innerHTML = ""; $("crumbs").innerHTML = "";
  DEVS = await api().devices();
  const opts = []; DEVS.forEach((d) => (d.storages || []).forEach((s) => opts.push({ device: d.device, storage: s })));
  if (!opts.length) { DEV = null; $("dev").innerHTML = "<option value=''>לא נמצא טלפון — חבר, פתח נעילה ובחר 'העברת קבצים'</option>"; return; }
  $("dev").innerHTML = opts.map((o, i) => '<option value="' + i + '">' + esc(o.device) + " — " + esc(o.storage) + '</option>').join("");
  $("dev")._opts = opts;
  const last = await api().last_target();
  const li = opts.findIndex((o) => o.device === last.device && o.storage === last.storage);
  $("dev").value = String(li >= 0 ? li : 0);
  DEV = opts[li >= 0 ? li : 0]; PATH = li >= 0 && last.path ? last.path.split("/").filter(Boolean) : [];
  browse();
}
function pickDev() { DEV = ($("dev")._opts || [])[+$("dev").value] || null; PATH = []; browse(); }
async function browse() {
  if (!DEV) return;
  $("crumbs").innerHTML = '<a onclick="go(0)">' + esc(DEV.storage) + '</a>' + PATH.map((p, i) => '<span>›</span><a onclick="go(' + (i + 1) + ')">' + esc(p) + '</a>').join("");
  $("upBtn").disabled = !PATH.length;
  $("folders").innerHTML = '<div class="empty">טוען תיקיות…</div>';
  try {
    const names = await api().phone_folders(DEV.device, DEV.storage, PATH.join("/"));
    $("folders").innerHTML = names.length ? names.map((n) => '<div class="folder" data-dir="' + esc(n) + '">' + IC.folder + '<span>' + esc(n) + '</span></div>').join("")
      : '<div class="empty">אין כאן תיקיות — אפשר להעביר לכאן או ליצור תיקייה חדשה</div>';
  } catch (e) {
    $("folders").innerHTML = '<div class="empty">' + esc(String(e).replace(/^Error: /, "")) + '</div>';
    if (PATH.length) { PATH = []; setTimeout(browse, 1200); }
  }
  $("sendBtn").textContent = "העבר לכאן" + (PATH.length ? " (" + PATH[PATH.length - 1] + ")" : "");
}
$("folders").addEventListener("click", (e) => { const f = e.target.closest("[data-dir]"); if (f) { PATH.push(f.dataset.dir); browse(); } });
function go(n) { PATH = PATH.slice(0, n); browse(); }
function upDir() { PATH.pop(); browse(); }
function quick(name) { PATH = [name]; browse(); }
async function newDir() {
  if (!DEV) return; const n = prompt("שם התיקייה החדשה:"); if (!n) return;
  try { await api().phone_mkdir(DEV.device, DEV.storage, PATH.join("/"), n); PATH.push(n.trim()); browse(); } catch (e) { toast("שגיאה: " + e, 6000); }
}
function openPhone() { $("phone").style.display = "grid"; $("phoneCount").textContent = "נבחרו " + SEL.size + " קבצים."; scan(); }
function closePhone() { $("phone").style.display = "none"; }
async function send() {
  if (!DEV) return toast("לא נבחר טלפון");
  const ids = [...SEL], where = [DEV.storage].concat(PATH).join(" › ");
  closePhone(); watchOp("מעביר לטלפון · " + where);    // the panel stays while you keep browsing
  try { const r = await api().send(ids, DEV.device, DEV.storage, PATH.join("/"));
        const msg = (r.cancelled ? "נעצר. " : "") + "הועברו " + r.ok + " קבצים" + (r.failed.length ? " · נכשלו " + r.failed.length + ": " + r.failed[0] : " ✓");
        endOp(msg); toast(msg, 10000); if (!r.failed.length && !r.cancelled) { SEL.clear(); render(); } }
  catch (e) { endOp("שגיאה: " + e); }
}
let STARTED = false;
async function boot() {
  if (STARTED || !(window.pywebview && window.pywebview.api && window.pywebview.api.info)) return;
  STARTED = true;
  try { await start(); } catch (e) { STARTED = false; $("view").innerHTML = '<div class="center"><p>שגיאה בפתיחת הכספת</p><p class="muted">' + esc(e) + '</p></div>'; }
}
window.addEventListener("pywebviewready", boot);
const BOOT = setInterval(() => { boot(); if (STARTED) clearInterval(BOOT); }, 300);
</script></body></html>"""
