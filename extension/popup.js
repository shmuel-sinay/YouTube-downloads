const $ = (id) => document.getElementById(id);
function show(s) {
  if (!s) { $("st").innerHTML = '<span class="m">עוד לא נשלח</span>'; return; }
  $("st").innerHTML = '<b class="' + (s.ok ? "ok" : "bad") + '">' + (s.ok ? "✓ " : "✗ ") + s.msg + '</b><br><span class="m">' +
    new Date(s.at).toLocaleString("he-IL") + " · " + s.reason + "</span>";
}
chrome.storage.local.get(["site", "key", "status", "lastVisit"]).then((s) => {
  $("site").value = s.site || "https://my-gemini.g47ben.workers.dev"; $("key").value = s.key || ""; show(s.status);
  $("vis").textContent = s.lastVisit ? s.lastVisit.msg + " · " + new Date(s.lastVisit.at).toLocaleString("he-IL") : "עוד לא היה ביקור (פעמיים ביום)";
});
$("visit").onclick = async () => { $("vis").textContent = "מבקר ביוטיוב…"; show(await chrome.runtime.sendMessage("visit"));
  const v = (await chrome.storage.local.get("lastVisit")).lastVisit; if (v) $("vis").textContent = v.msg + " · " + new Date(v.at).toLocaleString("he-IL"); };
$("save").onclick = async () => {
  await chrome.storage.local.set({ site: $("site").value.trim(), key: $("key").value.trim() });
  $("st").textContent = "שולח…";
  show(await chrome.runtime.sendMessage("push"));
};
