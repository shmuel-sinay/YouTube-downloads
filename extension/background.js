// Sends this browser's YouTube cookies to YOUR tracker site only (the address
// and key you set in the popup), so the download server always has fresh ones.
// Nothing is sent anywhere else.

const EVERY_MIN = 20;

async function settings() {
  const s = await chrome.storage.local.get(["site", "key"]);
  return { site: (s.site || "").replace(/\/+$/, ""), key: s.key || "" };
}

// Same format yt-dlp reads (Netscape cookies.txt).
async function youtubeCookies() {
  const all = await chrome.cookies.getAll({ domain: "youtube.com" });
  const seen = new Set();
  const lines = ["# Netscape HTTP Cookie File"];
  let signedIn = false;
  for (const c of all) {
    const id = c.domain + "|" + c.name + "|" + c.path;
    if (seen.has(id)) continue;
    seen.add(id);
    if (c.name === "SAPISID" || c.name === "__Secure-3PSID") signedIn = true;
    let domain = c.domain;
    if (!c.hostOnly && !domain.startsWith(".")) domain = "." + domain;
    lines.push([(c.httpOnly ? "#HttpOnly_" : "") + domain, c.hostOnly ? "FALSE" : "TRUE", c.path,
                c.secure ? "TRUE" : "FALSE", c.expirationDate ? Math.floor(c.expirationDate) : 0,
                c.name, c.value].join("\t"));
  }
  return { text: lines.join("\n") + "\n", count: lines.length - 1, signedIn };
}

async function push(reason) {
  const { site, key } = await settings();
  const status = { at: new Date().toISOString(), reason };
  if (!site || !key) {
    Object.assign(status, { ok: false, msg: "חסרים כתובת האתר והמפתח (לחץ על סמל התוסף)" });
  } else {
    const ck = await youtubeCookies();
    if (!ck.signedIn) {
      Object.assign(status, { ok: false, msg: "לא מחובר ליוטיוב בדפדפן הזה — היכנס ליוטיוב עם חשבון Google" });
    } else {
      try {
        const r = await fetch(site + "/yt/cookies/push", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Push-Key": key },
          body: JSON.stringify({ cookies: ck.text, count: ck.count }),
        });
        const t = await r.text();
        Object.assign(status, r.ok ? { ok: true, msg: "נשלחו " + ck.count + " עוגיות" } : { ok: false, msg: "האתר סירב: " + t.slice(0, 120) });
      } catch (e) {
        Object.assign(status, { ok: false, msg: "אין חיבור לאתר: " + e.message });
      }
    }
  }
  await chrome.storage.local.set({ status });
  chrome.action.setBadgeText({ text: status.ok ? "" : "!" });
  chrome.action.setBadgeBackgroundColor({ color: "#f06464" });
  return status;
}

chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create("push", { periodInMinutes: EVERY_MIN }); push("התקנה"); });
chrome.runtime.onStartup.addListener(() => { chrome.alarms.create("push", { periodInMinutes: EVERY_MIN }); push("הדפדפן נפתח"); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "push") push("כל " + EVERY_MIN + " דק׳"); });

// YouTube rotates its login cookies while you browse — send the new ones
// (at most once a minute).
let timer = null;
chrome.cookies.onChanged.addListener((ch) => {
  if (!/(^|\.)youtube\.com$/.test(ch.cookie.domain.replace(/^\./, "")) || !/SID|SAPISID|LOGIN_INFO/.test(ch.cookie.name)) return;
  clearTimeout(timer);
  timer = setTimeout(() => push("יוטיוב חידש עוגיות"), 60000);
});

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg === "push") { push("ידני").then(reply); return true; }
});
