// my-gemini + YouTube channel tracker — Cloudflare Worker (single file,
// no build step). Two apps on one address, with a button to switch:
//   /     the existing Gemini chat (unchanged behaviour)
//   /yt   the YouTube channel tracker
//
// What it does:
//   * Keeps a list of YouTube channels (KV binding "DB").
//   * Every few hours (Cron Trigger) reads each channel's public RSS feed and
//     queues videos it hasn't seen yet — with that channel's filters.
//   * Serves a small password-protected page: add/remove channels, see the
//     queue, open downloaded files.
//   * Exposes a tiny API for the downloader agent (agent/agent.py), which
//     runs on a real server and does the actual downloading with yt-dlp.
//   * Optional: serves downloaded files from an R2 bucket (binding "FILES").
//
// Setup (Cloudflare dashboard, see README.md):
//   KV namespace binding:  DB
//   Secrets:               GEMINI_API_KEY, GEMINI_PASSWORD (the Gemini chat)
//                          ADMIN_PASSWORD (tracker login), AGENT_TOKEN (agent API)
//   Variable:              RENDER_URL — the download server, e.g.
//                          https://my-agent.onrender.com (woken up on demand)
//   Cron Trigger:          e.g. "0 */3 * * *"
//   Optional R2 binding:   FILES

const BASE = "/yt";     // where the tracker lives; "/" stays the Gemini chat

const SEEN_CAP = 500;   // video ids remembered per channel
const JOBS_CAP = 400;   // jobs kept in the list (oldest dropped)
const CLAIM_TIMEOUT_MS = 3 * 60 * 60 * 1000;  // re-queue a "taken" job after 3h

export default {
  async fetch(req, env, ctx) {
    try {
      return await route(req, env, ctx);
    } catch (e) {
      return new Response("Error: " + (e && e.message || e), { status: 500 });
    }
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(checkAllChannels(env).then(() => wakeAgent(env)));
  },
};

// ---------- storage helpers ----------

async function getJSON(env, key, fallback) {
  const v = await env.DB.get(key);
  return v ? JSON.parse(v) : fallback;
}
const putJSON = (env, key, val) => env.DB.put(key, JSON.stringify(val));

const getChannels = (env) => getJSON(env, "channels", []);
const getJobIds = (env) => getJSON(env, "jobs", []);

async function addJobs(env, jobs) {
  if (!jobs.length) return;
  const ids = await getJobIds(env);
  for (const j of [...jobs].reverse()) {   // keep the newest on top
    await putJSON(env, "job:" + j.videoId, j);
    if (!ids.includes(j.videoId)) ids.unshift(j.videoId);
  }
  const dropped = ids.splice(JOBS_CAP);
  await putJSON(env, "jobs", ids);
  for (const id of dropped) await env.DB.delete("job:" + id);
}

async function listJobs(env) {
  const ids = await getJobIds(env);
  const jobs = await Promise.all(ids.map((id) => getJSON(env, "job:" + id, null)));
  return jobs.filter(Boolean);
}

// ---------- YouTube ----------

// Accepts: UC… id, /channel/UC…, /@handle, or any channel URL.
async function resolveChannelId(input) {
  input = input.trim();
  let m = input.match(/(UC[\w-]{22})/);
  if (m) return m[1];
  let url = input;
  if (/^@/.test(url)) url = "https://www.youtube.com/" + url;
  if (!/^https?:\/\//.test(url)) url = "https://www.youtube.com/@" + url.replace(/^\/+/, "");
  const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0", "Accept-Language": "en" } });
  const html = await r.text();
  m = html.match(/"externalId":"(UC[\w-]{22})"/) ||
      html.match(/<meta itemprop="identifier" content="(UC[\w-]{22})"/) ||
      html.match(/youtube\.com\/channel\/(UC[\w-]{22})/);
  if (!m) throw new Error("לא הצלחתי לזהות את הערוץ מהקישור. נסה להדביק את מזהה הערוץ (מתחיל ב-UC).");
  return m[1];
}

function decode(s) {
  return s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
          .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

async function readFeed(channelId) {
  const r = await fetch("https://www.youtube.com/feeds/videos.xml?channel_id=" + channelId);
  if (!r.ok) throw new Error("RSS " + r.status);
  const xml = await r.text();
  const name = decode((xml.match(/<title>([^<]*)<\/title>/) || [])[1] || channelId);
  const entries = [];
  for (const block of xml.split("<entry>").slice(1)) {
    const id = (block.match(/<yt:videoId>([^<]+)<\/yt:videoId>/) || [])[1];
    if (!id) continue;
    entries.push({
      videoId: id,
      title: decode((block.match(/<title>([^<]*)<\/title>/) || [])[1] || id),
      published: (block.match(/<published>([^<]+)<\/published>/) || [])[1] || "",
      // Shorts are linked as youtube.com/shorts/<id> in the feed.
      isShort: /youtube\.com\/shorts\//.test(block),
    });
  }
  return { name, entries };
}

function jobFor(ch, e) {
  return {
    videoId: e.videoId, channelId: ch.id, channelName: ch.name, title: e.title,
    published: e.published, url: "https://www.youtube.com/watch?v=" + e.videoId,
    mode: ch.mode, minMinutes: ch.minMinutes, status: "queued",
    updated: new Date().toISOString(),
  };
}

async function checkChannel(env, ch) {
  const seen = await getJSON(env, "seen:" + ch.id, []);
  const { entries } = await readFeed(ch.id);
  const fresh = entries.filter((e) => !seen.includes(e.videoId));
  const jobs = fresh.filter((e) => !(ch.skipShorts && e.isShort)).map((e) => jobFor(ch, e));
  await addJobs(env, jobs);
  if (fresh.length) {
    await putJSON(env, "seen:" + ch.id, [...fresh.map((e) => e.videoId), ...seen].slice(0, SEEN_CAP));
  }
  return jobs.length;
}

async function checkAllChannels(env) {
  const channels = await getChannels(env);
  let added = 0;
  const errors = [];
  for (const ch of channels) {
    try { added += await checkChannel(env, ch); }
    catch (e) { errors.push(ch.name + ": " + e.message); }
  }
  await putJSON(env, "lastCheck", { at: new Date().toISOString(), added, errors });
  return { added, errors };
}

// Render's free plan sleeps after ~15 min without traffic; any request wakes
// it. The agent then works through the queue and keeps itself awake.
async function wakeAgent(env) {
  if (!env.RENDER_URL) return "RENDER_URL לא מוגדר";
  try {
    const r = await fetch(env.RENDER_URL.replace(/\/$/, "") + "/wake", { method: "POST" });
    return r.ok ? "ok" : "HTTP " + r.status;
  } catch (e) {
    return String(e.message || e);
  }
}

// ---------- auth ----------

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function isAdmin(req, env) {
  if (!env.ADMIN_PASSWORD) return false;
  const c = req.headers.get("Cookie") || "";
  const m = c.match(/(?:^|;\s*)auth=([a-f0-9]{64})/);
  return !!m && m[1] === await sha256("yt-tracker:" + env.ADMIN_PASSWORD);
}

function isAgent(req, env) {
  const h = req.headers.get("Authorization") || "";
  return !!env.AGENT_TOKEN && h === "Bearer " + env.AGENT_TOKEN;
}

// ---------- routing ----------

const json = (v, status = 200) => new Response(JSON.stringify(v), {
  status, headers: { "Content-Type": "application/json; charset=utf-8" } });
const redirect = (to, headers = {}) => new Response(null, { status: 303, headers: { Location: to, ...headers } });

async function route(req, env, ctx) {
  const url = new URL(req.url);
  if (url.pathname === "/") return gemini(req, env);   // the existing project
  if (url.pathname !== BASE && !url.pathname.startsWith(BASE + "/")) {
    return new Response("Not found", { status: 404 });
  }
  const p = url.pathname.slice(BASE.length) || "/";
  const wake = () => ctx.waitUntil(wakeAgent(env));

  // --- agent API ---
  if (p.startsWith("/api/")) {
    if (!isAgent(req, env)) return json({ error: "unauthorized" }, 401);
    if (p === "/api/next" && req.method === "POST") {
      const limit = Math.min(+(url.searchParams.get("limit") || 3), 20);
      const now = Date.now();
      const out = [];
      for (const j of (await listJobs(env)).reverse()) {   // oldest first
        const stale = j.status === "taken" && now - Date.parse(j.updated) > CLAIM_TIMEOUT_MS;
        if (j.status !== "queued" && !stale) continue;
        j.status = "taken"; j.updated = new Date().toISOString();
        await putJSON(env, "job:" + j.videoId, j);
        out.push(j);
        if (out.length >= limit) break;
      }
      return json({ jobs: out });
    }
    if (p === "/api/report" && req.method === "POST") {
      const b = await req.json();
      const j = await getJSON(env, "job:" + b.videoId, null);
      if (!j) return json({ error: "no such job" }, 404);
      Object.assign(j, {
        status: ["done", "failed", "skipped"].includes(b.status) ? b.status : "failed",
        fileUrl: b.fileUrl || "", fileName: b.fileName || "", error: (b.error || "").slice(0, 300),
        updated: new Date().toISOString(),
      });
      await putJSON(env, "job:" + j.videoId, j);
      return json({ ok: true });
    }
    return json({ error: "not found" }, 404);
  }

  // --- login ---
  if (p === "/login" && req.method === "POST") {
    const f = await req.formData();
    if (env.ADMIN_PASSWORD && f.get("password") === env.ADMIN_PASSWORD) {
      const tok = await sha256("yt-tracker:" + env.ADMIN_PASSWORD);
      return redirect(BASE + "/", { "Set-Cookie": `auth=${tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000` });
    }
    return page("כניסה", loginForm("סיסמה שגויה"));
  }
  if (!(await isAdmin(req, env))) {
    if (!env.ADMIN_PASSWORD) return page("הגדרה חסרה", "<p>חסר Secret בשם <b>ADMIN_PASSWORD</b> ב-Worker. ראה README.</p>");
    return page("כניסה", loginForm(""));
  }

  // --- files from R2 (optional) ---
  if (p.startsWith("/files/")) {
    if (!env.FILES) return new Response("R2 not configured", { status: 404 });
    const key = decodeURIComponent(p.slice("/files/".length));
    const obj = await env.FILES.get(key);
    if (!obj) return new Response("Not found", { status: 404 });
    const name = key.split("/").pop();
    return new Response(obj.body, { headers: {
      "Content-Type": obj.httpMetadata?.contentType || "application/octet-stream",
      "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(name)}`,
    } });
  }

  // --- admin actions (POST forms) ---
  if (p === "/wake" && req.method === "POST") {
    return json({ result: await wakeAgent(env) });
  }
  if (req.method === "POST") {
    const f = await req.formData();
    if (p === "/channels/add") {
      const id = await resolveChannelId(String(f.get("channel") || ""));
      const channels = await getChannels(env);
      if (!channels.some((c) => c.id === id)) {
        const { name, entries } = await readFeed(id);
        const ch = {
          id, name, mode: f.get("mode") === "video" ? "video" : "audio",
          minMinutes: Math.max(0, parseInt(f.get("minMinutes") || "0", 10) || 0),
          skipShorts: f.get("skipShorts") === "on", added: new Date().toISOString(),
        };
        channels.push(ch);
        await putJSON(env, "channels", channels);
        // Everything already on the channel counts as "seen" — except the
        // newest N, which are queued now if the user asked for a backfill.
        const backfill = Math.max(0, Math.min(15, parseInt(f.get("backfill") || "0", 10) || 0));
        const take = entries.filter((e) => !(ch.skipShorts && e.isShort)).slice(0, backfill);
        await addJobs(env, take.map((e) => jobFor(ch, e)));
        await putJSON(env, "seen:" + id, entries.map((e) => e.videoId));
        if (take.length) wake();
      }
      return redirect(BASE + "/");
    }
    if (p === "/channels/remove") {
      const id = String(f.get("id"));
      await putJSON(env, "channels", (await getChannels(env)).filter((c) => c.id !== id));
      await env.DB.delete("seen:" + id);
      return redirect(BASE + "/");
    }
    if (p === "/check") {
      const { added } = await checkAllChannels(env);
      if (added) wake();
      return redirect(BASE + "/");
    }
    if (p === "/jobs/retry") {
      const j = await getJSON(env, "job:" + f.get("id"), null);
      if (j) { j.status = "queued"; j.error = ""; j.updated = new Date().toISOString(); await putJSON(env, "job:" + j.videoId, j); wake(); }
      return redirect(BASE + "/");
    }
    if (p === "/jobs/add") {
      // Manual one-off: paste a video link.
      const m = String(f.get("url") || "").match(/(?:v=|youtu\.be\/|shorts\/)([\w-]{11})/);
      if (m) await addJobs(env, [{
        videoId: m[1], channelId: "", channelName: "ידני", title: m[1],
        published: "", url: "https://www.youtube.com/watch?v=" + m[1],
        mode: f.get("mode") === "video" ? "video" : "audio", minMinutes: 0,
        status: "queued", updated: new Date().toISOString() }]);
      if (m) wake();
      return redirect(BASE + "/");
    }
  }

  if (p === "/status") {
    const jobs = (await listJobs(env)).slice(0, 100)
      .map(({ videoId, title, status, fileUrl, fileName, updated }) => ({ videoId, title, status, fileUrl, fileName, updated }));
    return json({ jobs });
  }
  if (p === "/") return page("מעקב ערוצים", await dashboard(env));
  return new Response("Not found", { status: 404 });
}

// ---------- HTML ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function page(title, body) {
  return new Response(`<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title>
<style>
body{font-family:system-ui,Arial,sans-serif;max-width:900px;margin:0 auto;padding:16px;background:#f6f7f9;color:#222}
h1{font-size:1.4em}h2{font-size:1.1em;margin-top:28px}
.card{background:#fff;border-radius:10px;padding:14px;margin:10px 0;box-shadow:0 1px 3px #0001}
input,select,button{font:inherit;padding:6px 8px;margin:3px 0}
button{cursor:pointer;border:0;border-radius:6px;background:#2563eb;color:#fff}
button.gray{background:#888}table{width:100%;border-collapse:collapse;font-size:.92em}
td,th{padding:6px;border-bottom:1px solid #eee;text-align:right;vertical-align:top}
.s-done{color:#15803d}.s-failed{color:#b91c1c}.s-queued,.s-taken{color:#a16207}.muted{color:#777;font-size:.85em}
</style></head><body>${body}</body></html>`, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function loginForm(msg) {
  return `<h1>מעקב ערוצים</h1><form class="card" method="post" action="${BASE}/login">
${msg ? `<p style="color:#b91c1c">${esc(msg)}</p>` : ""}
<input type="password" name="password" placeholder="סיסמה" autofocus> <button>כניסה</button></form>`;
}

const STATUS = { queued: "ממתין", taken: "בהורדה", done: "ירד ✓", failed: "נכשל", skipped: "דולג" };

async function dashboard(env) {
  const channels = await getChannels(env);
  const jobs = await listJobs(env);
  const last = await getJSON(env, "lastCheck", null);
  const chRows = channels.map((c) => `<tr><td><a href="https://www.youtube.com/channel/${esc(c.id)}" target="_blank">${esc(c.name)}</a></td>
<td>${c.mode === "video" ? "סרטון" : "שיר"}</td><td>${c.minMinutes ? c.minMinutes + " דק׳+" : "—"}</td><td>${c.skipShorts ? "בלי" : "עם"}</td>
<td><form method="post" action="${BASE}/channels/remove" onsubmit="return confirm('להסיר?')"><input type="hidden" name="id" value="${esc(c.id)}"><button class="gray">הסר</button></form></td></tr>`).join("");
  const jobRows = jobs.slice(0, 100).map((j) => `<tr><td>${esc(j.title)}<div class="muted">${esc(j.channelName)} · ${esc((j.published || "").slice(0, 10))}</div></td>
<td class="s-${esc(j.status)}">${STATUS[j.status] || esc(j.status)}${j.error ? `<div class="muted">${esc(j.error)}</div>` : ""}</td>
<td>${j.fileUrl ? `<a href="${esc(j.fileUrl)}">הורדה</a>` : ""}
${["failed", "skipped"].includes(j.status) ? `<form method="post" action="${BASE}/jobs/retry"><input type="hidden" name="id" value="${esc(j.videoId)}"><button class="gray">שוב</button></form>` : ""}</td></tr>`).join("");
  return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
<h1>📺 מעקב ערוצים</h1><a href="/"><button class="gray" type="button">💬 מעבר ל-Gemini</button></a></div>
<div class="card"><button type="button" onclick="wakeAgent(this)">⏰ הער את שרת ההורדות</button>
<span id="wakeRes" class="muted"></span>
<label style="display:block;margin-top:8px"><input type="checkbox" id="autoDl"> הורדה אוטומטית למחשב (כשהדף פתוח)</label>
<div class="muted">קבצים בשרת Render נמחקים כשהוא נרדם — לכן כדאי להשאיר את הדף פתוח עם הורדה אוטומטית. בפעם הראשונה הדפדפן ישאל אם לאשר הורדה של כמה קבצים — לאשר.</div></div>
<div class="card"><b>הוספת ערוץ</b><form method="post" action="${BASE}/channels/add">
<input name="channel" placeholder="קישור לערוץ / @handle / UC…" required style="width:100%">
<select name="mode"><option value="audio">שיר (MP3)</option><option value="video">סרטון (MP4)</option></select>
אורך מינימלי (דקות): <input name="minMinutes" type="number" min="0" value="0" style="width:70px">
<label><input type="checkbox" name="skipShorts" checked> בלי Shorts</label>
להוריד עכשיו את האחרונים: <input name="backfill" type="number" min="0" max="15" value="0" style="width:60px">
<button>הוסף</button></form></div>
<div class="card"><b>הורדה חד-פעמית</b><form method="post" action="${BASE}/jobs/add">
<input name="url" placeholder="קישור לסרטון ביוטיוב" required style="width:60%">
<select name="mode"><option value="audio">שיר</option><option value="video">סרטון</option></select> <button>הוסף לתור</button></form></div>
<h2>ערוצים (${channels.length})</h2>
<div class="card"><table><tr><th>ערוץ</th><th>סוג</th><th>אורך</th><th>Shorts</th><th></th></tr>${chRows || '<tr><td colspan="5">עוד אין ערוצים</td></tr>'}</table>
<form method="post" action="${BASE}/check"><button>בדוק עכשיו</button></form>
<div class="muted">בדיקה אחרונה: ${last ? esc(last.at.replace("T", " ").slice(0, 16)) + " UTC · נוספו " + last.added + (last.errors.length ? " · שגיאות: " + esc(last.errors.join(" | ")) : "") : "עוד לא"}</div></div>
<h2>תור והורדות</h2><div class="card"><table><tr><th>סרטון</th><th>מצב</th><th></th></tr>${jobRows || '<tr><td colspan="3">ריק</td></tr>'}</table></div>
<script>
const B = ${JSON.stringify(BASE)};
async function wakeAgent(btn) {
  btn.disabled = true; document.getElementById("wakeRes").textContent = " מעיר… (עד דקה)";
  try { const r = await (await fetch(B + "/wake", { method: "POST" })).json();
        document.getElementById("wakeRes").textContent = r.result === "ok" ? " ✓ השרת ער" : " ✗ " + r.result; }
  catch (e) { document.getElementById("wakeRes").textContent = " ✗ " + e; }
  btn.disabled = false;
}
// Auto-download: every 30s look for newly finished files and download each
// once (remembered in this browser). Only files finished after the box was
// ticked, so turning it on doesn't download the whole history.
const box = document.getElementById("autoDl");
const store = (k, v) => { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} };
const load = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
box.checked = !!load("autoDlSince");
box.onchange = () => store("autoDlSince", box.checked ? new Date().toISOString() : undefined);
let busy = false;
async function poll() {
  const since = load("autoDlSince");
  if (!since || busy) return;
  busy = true;
  try {
    const { jobs } = await (await fetch(B + "/status")).json();
    const got = JSON.parse(load("autoDlGot") || "[]");
    let changed = false;
    for (const j of jobs) {
      if (j.status !== "done" || !j.fileUrl || j.updated < since || got.includes(j.videoId)) continue;
      const f = document.createElement("iframe");
      f.style.display = "none"; f.src = j.fileUrl; document.body.appendChild(f);
      got.push(j.videoId); changed = true;
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (changed) store("autoDlGot", JSON.stringify(got.slice(-500)));
  } catch (e) {}
  busy = false;
}
setInterval(poll, 30000); poll();
</script>`;
}

// ================================================================
// Gemini chat — the original my-gemini project, served at "/".
// Same page and behaviour as before; the only changes:
//   * the API key and password come from Worker secrets instead of being
//     written in the code (GEMINI_API_KEY, GEMINI_PASSWORD),
//   * a button in the header switches to the YouTube tracker.
// ================================================================

async function gemini(request, env) {
  const GEMINI_API_KEY = env.GEMINI_API_KEY;
  const SECRET_PASSWORD = env.GEMINI_PASSWORD;

  if (request.method === "GET") {
    return new Response(GEMINI_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
  }

  if (request.method === "POST") {
    try {
      const body = await request.json();
      if (!SECRET_PASSWORD || body.password !== SECRET_PASSWORD) {
        return new Response("Unauthorized", { status: 401 });
      }
      if (!GEMINI_API_KEY) {
        return new Response("חסר Secret בשם GEMINI_API_KEY ב-Worker.", { headers: { "Content-Type": "text/plain; charset=utf-8" } });
      }
      const geminiApiUrl = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY;
      const geminiResponse = await fetch(geminiApiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contents: body.contents }),
      });
      const data = await geminiResponse.json();
      let aiText = "";
      if (data && data.candidates && data.candidates.length > 0) {
        aiText = data.candidates[0].content.parts[0].text;
      } else if (data && data.error) {
        aiText = data.error.message.includes("exceeded your current quota")
          ? "המתן מעט... שרתי גוגל עמוסים כרגע. נסה שוב בעוד דקה."
          : "שגיאה רשמית מגוגל: " + data.error.message;
      } else {
        aiText = "תשובה לא מוכרת מ-API: " + JSON.stringify(data);
      }
      return new Response(aiText, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    } catch (error) {
      return new Response("אירעה שגיאה פנימית בקוד: " + error.message, { status: 500 });
    }
  }
  return new Response("Method not allowed", { status: 405 });
}

const GEMINI_HTML = `
        <!DOCTYPE html>
        <html lang="he" dir="rtl">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
          <title>Gemini - Dark Mode</title>
          <link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;700&display=swap" rel="stylesheet">
          <style>
            :root {
              --bg-color: #131314;
              --chat-bg: #1e1f20;
              --user-msg: #333639;
              --ai-msg: #1e1f20;
              --text-main: #e3e3e3;
              --text-muted: #9aa0a6;
              --primary: #8ab4f8;
              --border-color: #444746;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { 
              font-family: 'Rubik', sans-serif; 
              background-color: var(--bg-color); 
              color: var(--text-main); 
              display: flex; 
              justify-content: center; 
              align-items: center;
              height: 100vh; 
            }
            
            /* מסך התחברות */
            #login-screen {
              background: var(--chat-bg);
              padding: 40px;
              border-radius: 16px;
              border: 1px solid var(--border-color);
              text-align: center;
              width: 90%;
              max-width: 400px;
            }
            #login-screen h2 { color: var(--primary); margin-bottom: 20px; }
            #login-screen input {
              width: 100%; padding: 12px; margin-bottom: 20px;
              background: var(--bg-color); color: var(--text-main);
              border: 1px solid var(--border-color); border-radius: 8px; font-size: 1rem;
              outline: none;
            }
            #login-screen input:focus { border-color: var(--primary); }
            #login-screen button {
              width: 100%; padding: 12px; background-color: var(--primary); color: #202124;
              border: none; border-radius: 8px; font-size: 1rem; font-weight: bold; cursor: pointer;
            }

            /* ממשק הצ'אט */
            #app { 
              width: 100%; max-width: 800px; background: var(--chat-bg); 
              display: none; flex-direction: column; height: 100vh; 
            }
            header { 
              padding: 15px 20px; display: flex; justify-content: space-between; align-items: center;
              border-bottom: 1px solid var(--border-color); background: var(--chat-bg); z-index: 10; 
            }
            header h2 { font-size: 1.2rem; color: var(--primary); display: flex; align-items: center; gap: 8px; }
            .switch-app { color: var(--primary); text-decoration: none; border: 1px solid var(--border-color); border-radius: 16px; padding: 4px 12px; font-size: 0.9rem; }
            
            /* מתג (Toggle) מעוצב */
            .toggle-container { display: flex; align-items: center; gap: 10px; font-size: 0.9rem; color: var(--text-muted); }
            .switch { position: relative; display: inline-block; width: 40px; height: 20px; }
            .switch input { opacity: 0; width: 0; height: 0; }
            .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--border-color); transition: .4s; border-radius: 20px; }
            .slider:before { position: absolute; content: ""; height: 14px; width: 14px; left: 3px; bottom: 3px; background-color: var(--text-main); transition: .4s; border-radius: 50%; }
            input:checked + .slider { background-color: var(--primary); }
            input:checked + .slider:before { transform: translateX(20px); background-color: #202124; }

            #chat-box { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; scroll-behavior: smooth; }
            .message { max-width: 85%; padding: 14px 18px; border-radius: 18px; font-size: 1rem; line-height: 1.6; word-wrap: break-word; animation: fadeIn 0.3s ease-out; }
            .user-msg { background-color: var(--user-msg); align-self: flex-start; border-bottom-right-radius: 4px; }
            .ai-msg { background-color: transparent; align-self: flex-end; }
            
            .code-block { background: #282a36; color: #f8f8f2; padding: 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; direction: ltr; text-align: left; font-family: monospace; }
            .error-alert { color: #ff8a80; font-weight: bold; }
            
            .input-container { padding: 20px; background: var(--bg-color); border-top: 1px solid var(--border-color); display: flex; gap: 12px; align-items: flex-end; }
            textarea { 
              flex: 1; resize: none; border: 1px solid var(--border-color); border-radius: 24px; padding: 14px 20px; 
              font-family: inherit; font-size: 1rem; outline: none; background: var(--chat-bg); color: var(--text-main);
              min-height: 52px; max-height: 150px; overflow-y: auto; 
            }
            textarea:focus { border-color: var(--primary); }
            .send-btn { background-color: transparent; color: var(--primary); border: none; width: 52px; height: 52px; display: flex; align-items: center; justify-content: center; cursor: pointer; }
            .send-btn svg { width: 28px; height: 28px; fill: var(--primary); }
            
            .typing-indicator { display: none; align-self: flex-end; padding: 14px 18px; }
            .dots { display: flex; gap: 4px; align-items: center; height: 24px;}
            .dot { width: 6px; height: 6px; background: var(--primary); border-radius: 50%; animation: blink 1.4s infinite both; }
            .dot:nth-child(1) { animation-delay: 0.2s; } .dot:nth-child(2) { animation-delay: 0.4s; } .dot:nth-child(3) { animation-delay: 0.6s; }
            
            @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
            @keyframes blink { 0% { opacity: 0.2; transform: scale(0.8); } 20% { opacity: 1; transform: scale(1.2); } 100% { opacity: 0.2; transform: scale(0.8); } }
          </style>
        </head>
        <body>

          <div id="login-screen">
            <h2>ברוך הבא</h2>
            <p style="margin-bottom:20px; color:var(--text-muted);">הקש את משפט הכניסה:</p>
            <input type="password" id="passInput" placeholder="סיסמה..." onkeydown="if(event.key === 'Enter') enterChat()">
            <button onclick="enterChat()">כניסה</button>
            <p id="login-error" style="color:#ff8a80; display:none; margin-top:10px;">סיסמה שגויה, נסה שוב.</p>
            <p style="margin-top:20px;"><a class="switch-app" href="/yt/">📺 מעבר למעקב ערוצים</a></p>
          </div>

          <div id="app">
            <header>
              <h2>✨ Gemini</h2>
              <a class="switch-app" href="/yt/">📺 ערוצים</a>
              <div class="toggle-container">
                <label class="switch">
                  <input type="checkbox" id="historyToggle" checked>
                  <span class="slider"></span>
                </label>
                <span>זכור היסטוריה</span>
              </div>
            </header>
            <div id="chat-box">
               <div class="message ai-msg">שלום! ההתחברות עברה בהצלחה. איך אפשר לעזור לך?</div>
            </div>
            <div class="typing-indicator" id="loading">
              <div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
            </div>
            <div class="input-container">
              <textarea id="userInput" placeholder="הקלד הודעה... (לחץ Enter לשליחה)" rows="1" oninput="autoResize(this)" onkeydown="handleEnter(event)"></textarea>
              <button class="send-btn" onclick="sendMessage()" title="שלח">
                <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
              </button>
            </div>
          </div>

          <script>
            let savedPassword = "";
            let chatHistory = []; // שמירת ההיסטוריה בדפדפן

            function enterChat() {
              savedPassword = document.getElementById('passInput').value.trim();
              if(savedPassword) {
                document.getElementById('login-screen').style.display = 'none';
                document.getElementById('app').style.display = 'flex';
                document.body.style.alignItems = 'flex-start';
              }
            }

            function autoResize(textarea) {
              textarea.style.height = 'auto';
              textarea.style.height = textarea.scrollHeight + 'px';
            }

            function handleEnter(e) {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }

            function formatAIResponse(text) {
              let res = text;
              
              // תיקון: הדגשת טקסט ע"י זיהוי כוכביות בצורה מוגנת
              res = res.replace(/[*]{2}(.*?)[*]{2}/g, '<b>$1</b>');
              
              // עיצוב קטעי קוד
              var backticks = String.fromCharCode(96) + String.fromCharCode(96) + String.fromCharCode(96);
              res = res.split(backticks).map(function(part, i) {
                  return i % 2 !== 0 ? '<div class="code-block">' + part.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</div>' : part;
              }).join('');
              
              // המרת ירידות שורה
              res = res.replace(/\\n/g, '<br>');
              return res;
            }

            async function sendMessage() {
              const inputField = document.getElementById('userInput');
              const chatBox = document.getElementById('chat-box');
              const loading = document.getElementById('loading');
              const useHistory = document.getElementById('historyToggle').checked;
              
              const userText = inputField.value.trim();
              if (!userText) return;

              chatBox.innerHTML += '<div class="message user-msg">' + userText.replace(/\\n/g, '<br>') + '</div>';
              inputField.value = '';
              inputField.style.height = 'auto';
              chatBox.scrollTop = chatBox.scrollHeight;
              
              loading.style.display = 'flex';
              chatBox.scrollTop = chatBox.scrollHeight;

              let contentsPayload = [];
              if (useHistory) {
                  for(let i=0; i<chatHistory.length; i++) {
                      contentsPayload.push({ role: chatHistory[i].role, parts: [{ text: chatHistory[i].text }] });
                  }
              }
              contentsPayload.push({ role: "user", parts: [{ text: userText }] });

              try {
                const response = await fetch('/', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ 
                    password: savedPassword,
                    contents: contentsPayload 
                  })
                });

                if (response.status === 401) {
                  loading.style.display = 'none';
                  chatBox.innerHTML += '<div class="message ai-msg error-alert">שגיאת הרשאה: משפט הכניסה שגוי.</div>';
                  return;
                }

                let aiResponse = await response.text();
                loading.style.display = 'none';

                if (aiResponse.includes('<html') || aiResponse.includes('NETFREE') || aiResponse.includes('נטפרי')) {
                  chatBox.innerHTML += '<div class="message ai-msg error-alert">⚠️ חסימת נטפרי הופעלה (התוכן סונן).</div>';
                } else {
                  chatBox.innerHTML += '<div class="message ai-msg">' + formatAIResponse(aiResponse) + '</div>';
                  
                  chatHistory.push({ role: "user", text: userText });
                  chatHistory.push({ role: "model", text: aiResponse });
                }
                chatBox.scrollTop = chatBox.scrollHeight;

              } catch (error) {
                loading.style.display = 'none';
                chatBox.innerHTML += '<div class="message ai-msg error-alert">שגיאת תקשורת מול השרת.</div>';
                chatBox.scrollTop = chatBox.scrollHeight;
              }
            }
          </script>
        </body>
        </html>
      `;
