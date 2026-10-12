const $ = (id) => document.getElementById(id);
function show(s) {
  if (!s) { $("st").innerHTML = '<span class="m">עוד לא נשלח</span>'; return; }
  $("st").innerHTML = '<b class="' + (s.ok ? "ok" : "bad") + '">' + (s.ok ? "✓ " : "✗ ") + s.msg + '</b><br><span class="m">' +
    new Date(s.at).toLocaleString("he-IL") + " · " + s.reason + "</span>";
}
chrome.storage.local.get(["site", "key", "status"]).then((s) => {
  $("site").value = s.site || "https://my-gemini.g47ben.workers.dev"; $("key").value = s.key || ""; show(s.status);
});
$("save").onclick = async () => {
  await chrome.storage.local.set({ site: $("site").value.trim(), key: $("key").value.trim() });
  $("st").textContent = "שולח…";
  show(await chrome.runtime.sendMessage("push"));
};
