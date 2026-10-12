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

PS_SEND = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$sh = New-Object -ComObject Shell.Application
$dev = $sh.NameSpace(17).Items() | Where-Object { $_.Name -eq $env:YT_DEV } | Select-Object -First 1
if (-not $dev) { 'NODEVICE'; exit 2 }
$stor = $dev.GetFolder.Items() | Where-Object { $_.Name -eq $env:YT_STOR } | Select-Object -First 1
if (-not $stor) { 'NOSTORAGE'; exit 3 }
$root = $stor.GetFolder
$t = $root.Items() | Where-Object { $_.Name -eq $env:YT_SUB } | Select-Object -First 1
if (-not $t) { $root.NewFolder($env:YT_SUB); Start-Sleep -Seconds 2; $t = $root.Items() | Where-Object { $_.Name -eq $env:YT_SUB } | Select-Object -First 1 }
if (-not $t) { 'NOFOLDER'; exit 4 }
$name = Split-Path $env:YT_FILE -Leaf
$base = [IO.Path]::GetFileNameWithoutExtension($name)
$t.GetFolder.CopyHere($env:YT_FILE, 4 + 16)
for ($i = 0; $i -lt 1800; $i++) {
  Start-Sleep -Seconds 1
  if ($t.GetFolder.Items() | Where-Object { $_.Name -eq $name -or $_.Name -eq $base }) { Start-Sleep -Seconds 3; 'OK'; exit 0 }
}
'TIMEOUT'; exit 1
"""


def _ps(script: str, env: dict | None = None, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       env={**os.environ, **(env or {})}, creationflags=NO_WINDOW)
    return r.returncode, (r.stdout or "").strip()


class VaultApi:
    """Called from the vault window's page (window.pywebview.api.…)."""

    def __init__(self, cfg: dict, tmp_dir: Path):
        from vault import Vault
        self.cfg, self.tmp = cfg, tmp_dir
        self.v = Vault(cfg["vault_dir"]) if cfg.get("vault_dir") else None
        self.last = time.time()
        self.window = None
        threading.Thread(target=self._auto_lock, daemon=True).start()

    # -- housekeeping
    def _touch(self):
        self.last = time.time()

    def _auto_lock(self):
        while True:
            time.sleep(30)
            if self.v and self.v.unlocked and time.time() - self.last > AUTO_LOCK_MINUTES * 60:
                self.lock()
                try:
                    self.window.evaluate_js("locked()")
                except Exception:
                    pass

    def wipe_tmp(self):
        """Remove decrypted copies (a file still open in a player is skipped
        and removed next time)."""
        if self.tmp.exists():
            for p in sorted(self.tmp.rglob("*"), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass

    def _require(self):
        if not self.v or not self.v.unlocked:
            raise PermissionError("הכספת נעולה")
        self._touch()

    # -- API
    def info(self) -> dict:
        return {"configured": bool(self.v), "exists": bool(self.v and self.v.exists),
                "unlocked": bool(self.v and self.v.unlocked), "autolock": AUTO_LOCK_MINUTES}

    def unlock(self, password: str) -> bool:
        ok = bool(self.v and self.v.unlock(password))
        self._touch()
        return ok

    def lock(self) -> bool:
        if self.v:
            self.v.lock()
        self.wipe_tmp()
        return True

    def change_password(self, old: str, new: str) -> bool:
        return bool(self.v and len(new) >= 6 and self.v.change_password(old, new))

    def list(self) -> list:
        self._require()
        return self.v.list()

    def play(self, eid: str) -> str:
        """Decrypt to this user's private temp folder and open in the default player."""
        self._require()
        d = self.tmp / eid
        path = self.v.extract(eid, d)
        os.startfile(str(path))
        return path.name

    def export(self, eids: list) -> str:
        """Decrypt the chosen files into a folder the user picks (USB stick, …)."""
        self._require()
        import webview
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return ""
        dest = Path(res[0] if isinstance(res, (list, tuple)) else res)
        for eid in eids:
            self.v.extract(eid, dest)
        return str(dest)

    def delete(self, eids: list) -> int:
        self._require()
        for eid in eids:
            self.v.delete(eid)
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

    def send(self, eids: list, device: str, storage: str, sub: str) -> dict:
        """Copy to the phone: decrypt → Windows copies it over MTP → the
        decrypted temp copy is removed."""
        self._require()
        ok, failed = 0, []
        for eid in eids:
            d = self.tmp / ("send-" + eid)
            path = self.v.extract(eid, d)
            try:
                code, out = _ps(PS_SEND, {"YT_DEV": device, "YT_STOR": storage, "YT_SUB": sub or "Download",
                                          "YT_FILE": str(path)}, timeout=1900)
                if code == 0 and out.endswith("OK"):
                    ok += 1
                else:
                    failed.append(f"{path.name}: {out or code}")
            finally:
                shutil.rmtree(d, ignore_errors=True)
            self._touch()
        return {"ok": ok, "failed": failed}


VAULT_HTML = r"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><title>הכספת</title>
<style>
:root{--bg:#0e1015;--panel:#171a21;--panel2:#1e222b;--line:#262b36;--text:#e7e9ee;--muted:#8a92a3;--primary:#4f8cff;--ok:#2fbf87;--bad:#f06464}
*{box-sizing:border-box}body{margin:0;font-family:"Segoe UI",Rubik,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14.5px}
.i{width:18px;height:18px;vertical-align:middle}
.top{display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid var(--line)}
.top h1{flex:1;margin:0;font-size:1.15rem;display:flex;align-items:center;gap:8px}
.btn{display:inline-flex;align-items:center;gap:8px;font:inherit;border:1px solid transparent;border-radius:9px;padding:8px 14px;cursor:pointer;background:var(--primary);color:#fff}
.btn.ghost{background:var(--panel);border-color:var(--line);color:var(--text)}.btn.danger{background:var(--panel);border-color:var(--line);color:var(--bad)}
.btn:disabled{opacity:.45;cursor:default}
.wrap{padding:18px 20px}
.center{max-width:360px;margin:12vh auto;text-align:center;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}
.center svg{width:44px;height:44px;color:var(--primary)}
input,select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:9px 11px;width:100%}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.bar input{max-width:260px}
.list{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}.row:hover{background:#ffffff06}
.row .name{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.muted{color:var(--muted);font-size:.8rem}
.ibtn{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--muted);cursor:pointer}
.ibtn:hover{color:var(--text)}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);padding:10px 16px;border-radius:10px;display:none}
.modal{position:fixed;inset:0;background:#000a;display:none;place-items:center}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;width:min(440px,92vw)}
.modal label{display:block;margin:10px 0 4px;color:var(--muted);font-size:.85rem}
.empty{padding:40px;text-align:center;color:var(--muted)}
</style></head><body>
<div class="top"><h1><svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>הכספת</h1>
<button class="btn ghost hidden" id="lockBtn" onclick="doLock()">נעל</button></div>
<div id="view"></div>
<div class="modal" id="phone"><div class="box">
  <h3 style="margin-top:0">העברה לטלפון</h3>
  <p class="muted">חבר את הטלפון ב-USB, פתח את נעילת המסך ובחר בטלפון "העברת קבצים".</p>
  <label>טלפון ואחסון</label><select id="dev"></select>
  <label>תיקייה בטלפון</label>
  <select id="sub"><option value="Music">Music (מוזיקה)</option><option value="Movies">Movies (סרטונים)</option><option value="Download">Download (הורדות)</option></select>
  <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
    <button class="btn ghost" onclick="scan()">רענן</button><button class="btn ghost" onclick="closePhone()">ביטול</button><button class="btn" id="sendBtn" onclick="send()">העבר</button>
  </div></div></div>
<div id="toast"></div>
<script>
const api = () => window.pywebview.api;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const size = (b) => b > 1 << 30 ? (b / (1 << 30)).toFixed(1) + " GB" : (b / (1 << 20)).toFixed(1) + " MB";
let ITEMS = [], SEL = new Set(), Q = "";
function toast(m, ms) { const t = $("toast"); t.textContent = m; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms || 3500); }
async function start() {
  const i = await api().info();
  if (!i.configured || !i.exists) { $("view").innerHTML = '<div class="center"><p>לא הוגדרה כספת למשתמש הזה.</p><p class="muted">מגדירים בהגדרות התוכנה (YTTracker.exe --setup).</p></div>'; return; }
  i.unlocked ? load() : locked();
}
function locked() {
  $("lockBtn").style.display = "none"; ITEMS = []; SEL.clear();
  $("view").innerHTML = '<div class="center"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>' +
    '<h2>הכספת נעולה</h2><input type="password" id="pw" placeholder="סיסמת הכספת" autofocus onkeydown="if(event.key===\'Enter\')unlock()">' +
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
  render();
}
function render() {
  const shown = ITEMS.filter((x) => !Q || (x.name + " " + (x.title || "")).toLowerCase().includes(Q));
  $("view").innerHTML = '<div class="wrap"><div class="bar">' +
    '<input placeholder="חיפוש…" value="' + esc(Q) + '" oninput="Q=this.value.toLowerCase();render();this.focus();this.setSelectionRange(this.value.length,this.value.length)">' +
    '<span class="muted">' + ITEMS.length + ' קבצים' + (SEL.size ? " · נבחרו " + SEL.size : "") + '</span><span style="flex:1"></span>' +
    '<button class="btn ghost" ' + (SEL.size ? "" : "disabled") + ' onclick="exportSel()">ייצוא לתיקייה</button>' +
    '<button class="btn ghost" ' + (SEL.size ? "" : "disabled") + ' onclick="openPhone()">העבר לטלפון</button>' +
    '<button class="btn danger" ' + (SEL.size ? "" : "disabled") + ' onclick="delSel()">מחק</button></div>' +
    '<div class="list">' + (shown.length ? shown.map((x) => '<div class="row"><input type="checkbox" style="width:auto" ' + (SEL.has(x.id) ? "checked" : "") +
      ' onchange="this.checked?SEL.add(\'' + x.id + '\'):SEL.delete(\'' + x.id + '\');render()">' +
      '<div class="name" title="' + esc(x.name) + '">' + esc(x.title || x.name) + '<div class="muted">' + esc(x.name) + ' · ' + size(x.size || 0) + ' · ' + esc((x.added || "").slice(0, 10)) + '</div></div>' +
      '<button class="ibtn" title="נגן" onclick="play(\'' + x.id + '\')"><svg class="i" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4l14 8-14 8z"/></svg></button></div>').join("")
      : '<div class="empty">' + (ITEMS.length ? "אין תוצאות" : "הכספת ריקה — קבצים חדשים ייכנסו אליה אוטומטית") + '</div>') + '</div></div>';
}
async function play(id) { toast("פותח…"); try { toast("נפתח: " + await api().play(id)); } catch (e) { toast("שגיאה: " + e); } }
async function exportSel() { toast("מייצא…", 60000); try { const d = await api().export([...SEL]); toast(d ? "יוצא אל " + d : "בוטל"); } catch (e) { toast("שגיאה: " + e); } }
async function delSel() { if (!confirm("למחוק " + SEL.size + " קבצים מהכספת? אין דרך לשחזר.")) return; await api().delete([...SEL]); SEL.clear(); load(); toast("נמחק"); }
let DEVS = [];
async function scan() {
  $("dev").innerHTML = "<option>מחפש טלפונים…</option>";
  DEVS = await api().devices();
  const opts = []; DEVS.forEach((d, i) => (d.storages || []).forEach((s) => opts.push('<option value="' + i + "|" + esc(s) + '">' + esc(d.device) + " — " + esc(s) + '</option>')));
  $("dev").innerHTML = opts.length ? opts.join("") : "<option value=''>לא נמצא טלפון — חבר, פתח נעילה ובחר 'העברת קבצים'</option>";
}
function openPhone() { $("phone").style.display = "grid"; scan(); }
function closePhone() { $("phone").style.display = "none"; }
async function send() {
  const v = $("dev").value; if (!v) return toast("לא נבחר טלפון");
  const [i, stor] = [v.split("|")[0], v.split("|").slice(1).join("|")];
  $("sendBtn").disabled = true; toast("מעביר " + SEL.size + " קבצים לטלפון… (אל תנתק)", 600000);
  try { const r = await api().send([...SEL], DEVS[+i].device, stor, $("sub").value);
        toast("הועברו " + r.ok + (r.failed.length ? " · נכשלו " + r.failed.length + ": " + r.failed[0] : ""), 10000); closePhone(); }
  catch (e) { toast("שגיאה: " + e, 8000); }
  $("sendBtn").disabled = false;
}
window.addEventListener("pywebviewready", start);
</script></body></html>"""
