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
let ORIGIN = "";        // this Worker's address, remembered for hints in the log

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
  await addLog(env, "worker", `בדיקת ערוצים: ${channels.length} ערוצים, נוספו ${added} לתור` +
    (errors.length ? ` · שגיאות: ${errors.join(" | ")}` : ""));
  return { added, errors };
}

// ---------- log (shown on the site's "לוגים" tab) ----------

const LOG_CAP = 200;
async function addLogs(env, src, lines) {
  if (!lines.length) return;
  const logs = await getJSON(env, "logs", []);
  const t = new Date().toISOString();
  for (const msg of lines) logs.push({ t, src, msg: String(msg).slice(0, 500) });
  await putJSON(env, "logs", logs.slice(-LOG_CAP));
}
const addLog = (env, src, msg) => addLogs(env, src, [msg]);

// Render's free plan sleeps after ~15 min without traffic; any request wakes
// it. The agent then works through the queue and keeps itself awake.
async function wakeAgent(env) {
  if (!env.RENDER_URL) return "RENDER_URL לא מוגדר ב-Cloudflare";
  let res;
  try {
    const r = await fetch(env.RENDER_URL.replace(/\/$/, "") + "/wake", { method: "POST" });
    const body = (await r.text()).trim();
    // Our agent answers "ok", or "ok|<its last error reaching this Worker>".
    // Anything else is some other app (old code, failed build, error page).
    if (r.ok && body.startsWith("ok|")) {
      await addLog(env, "worker", "⚠ שרת ההורדות לא מצליח לדבר עם האתר: " + body.slice(3) +
        " — בדוק ב-Render את TRACKER_URL (צריך להיות " + (ORIGIN || "https://my-gemini.<שם>.workers.dev") + BASE + ") ואת AGENT_TOKEN");
    }
    res = r.ok && body.startsWith("ok") ? "ok"
      : r.ok ? "השרת ענה, אבל לא כמו סוכן ההורדות — כנראה ב-Render עדיין רץ קוד ישן או שהבנייה נכשלה"
      : `HTTP ${r.status} מ-Render`;
  } catch (e) {
    res = "אין תשובה מ-Render: " + (e.message || e);
  }
  await addLog(env, "worker", "השכמת שרת ההורדות: " + (res === "ok" ? "✓ ענה" : res));
  return res;
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
  ORIGIN = url.origin;
  if (url.pathname === "/") return gemini(req, env);   // the existing project
  // The agent API also answers without the /yt prefix, in case TRACKER_URL
  // was set to the bare Worker address.
  const bareApi = url.pathname.startsWith("/api/");
  if (!bareApi && url.pathname !== BASE && !url.pathname.startsWith(BASE + "/")) {
    return new Response("Not found", { status: 404 });
  }
  const p = bareApi ? url.pathname : url.pathname.slice(BASE.length) || "/";
  const wake = () => ctx.waitUntil(wakeAgent(env));
  if (!env.DB) {
    return page("הגדרה חסרה", `<p>חסר חיבור למסד נתונים (KV) בשם <b>DB</b>.</p>
<p>ב-Cloudflare: <b>Workers & Pages ← my-gemini ← Settings ← Bindings ← Add ← KV namespace</b>,
Variable name: <b>DB</b>, ובוחרים KV (אם אין — יוצרים ב-Storage & databases ← Workers KV ← Create). אחר כך מרעננים.</p>`);
  }

  // --- agent API ---
  if (p.startsWith("/api/")) {
    if (!isAgent(req, env)) {
      await addLog(env, "worker", "⚠ הסוכן ניסה להתחבר עם AGENT_TOKEN שגוי (צריך להיות זהה ב-Cloudflare וב-Render)");
      return json({ error: "unauthorized" }, 401);
    }
    const agent = await getJSON(env, "agent", {});
    if (!agent.seen || Date.now() - Date.parse(agent.seen) > 60000) {
      agent.seen = new Date().toISOString();
      await putJSON(env, "agent", agent);
    }
    if (p === "/api/hello" && req.method === "POST") {
      agent.info = await req.json();
      agent.started = new Date().toISOString();
      await putJSON(env, "agent", agent);
      // A (re)started agent has lost whatever it was working on — Render's
      // disk is wiped on every restart/deploy. Put those jobs back in line
      // instead of leaving them "בהורדה" for hours.
      const stuck = (await listJobs(env)).filter((j) => j.status === "taken");
      for (const j of stuck) {
        j.status = "queued"; j.updated = new Date().toISOString();
        await putJSON(env, "job:" + j.videoId, j);
      }
      if (stuck.length) await addLog(env, "worker", `השרת עלה מחדש — ${stuck.length} משימות שהיו בהורדה חזרו לתור`);
      return json({ ok: true });
    }
    if (p === "/api/log" && req.method === "POST") {
      const b = await req.json();
      await addLogs(env, "render", (b.lines || []).slice(-100));
      return json({ ok: true });
    }
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
      if (out.length) await addLog(env, "worker", `הסוכן לקח ${out.length} משימות: ` + out.map((j) => j.title).join(" | "));
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
      await addLog(env, "worker", `${STATUS[j.status] || j.status}: ${j.title}` + (j.error ? ` — ${j.error}` : ""));
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
  if (p === "/logs/clear" && req.method === "POST") {
    await putJSON(env, "logs", []);
    return json({ ok: true });
  }
  if (req.method === "POST") {
    const f = await req.formData();
    if (p === "/channels/add") {
      const id = await resolveChannelId(String(f.get("channel") || ""));
      const channels = await getChannels(env);
      if (!channels.some((c) => c.id === id)) {
        const { name, entries } = await readFeed(id);
        const ch = {
          id, name, mode: modeOf(f.get("mode")),
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
        await addLog(env, "worker", `נוסף ערוץ: ${name}` + (take.length ? ` (${take.length} סרטונים לתור)` : ""));
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
        mode: modeOf(f.get("mode")), minMinutes: 0,
        status: "queued", updated: new Date().toISOString() }]);
      if (m) wake();
      return redirect(BASE + "/");
    }
  }

  if (p === "/status") return json(await state(env));
  if (p === "/") return page("מעקב ערוצים", await dashboard(env));
  return new Response("Not found", { status: 404 });
}

// ---------- HTML ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Download kinds. "m4a" keeps YouTube's own audio (no MP3 conversion), which
// is many times faster on a small server.
const MODES = { audio: "🎵 שיר (MP3)", m4a: "⚡ שיר מהיר (M4A)", video: "🎬 סרטון (MP4)" };
const modeOf = (v) => (v in MODES ? v : "audio");

const STATUS = { queued: "ממתין", taken: "בהורדה", done: "ירד ✓", failed: "נכשל", skipped: "דולג" };

// Everything the page shows — also polled by the page every few seconds.
async function state(env) {
  const [channels, jobs, logs, agent, lastCheck] = await Promise.all([
    getChannels(env), listJobs(env), getJSON(env, "logs", []),
    getJSON(env, "agent", {}), getJSON(env, "lastCheck", null)]);
  return { channels, jobs: jobs.slice(0, 150), logs: logs.slice(-LOG_CAP), agent, lastCheck,
           renderUrl: env.RENDER_URL || "", now: new Date().toISOString() };
}

const CSS = `
:root{--bg:#131314;--panel:#1e1f20;--panel2:#26272a;--line:#3a3b3d;--text:#e3e3e3;--muted:#9aa0a6;
--primary:#8ab4f8;--ok:#81c995;--warn:#fdd663;--bad:#f28b82;--radius:14px}
*{box-sizing:border-box}
body{margin:0;font-family:Rubik,system-ui,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:15px}
a{color:var(--primary)}
.wrap{max-width:980px;margin:0 auto;padding:16px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:14px}
header.top h1{margin:0;font-size:1.35rem;color:var(--primary)}
.btn{font:inherit;border:0;border-radius:999px;padding:8px 16px;cursor:pointer;background:var(--primary);color:#202124;font-weight:600;text-decoration:none;display:inline-block}
.btn.ghost{background:transparent;color:var(--primary);border:1px solid var(--line)}
.btn.small{padding:4px 12px;font-size:.85rem}
.btn:disabled{opacity:.5;cursor:wait}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px;margin-bottom:14px}
.status-bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;background:var(--panel2);font-size:.85rem;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.tabs{display:flex;gap:6px;margin:6px 0 14px;flex-wrap:wrap}
.tab{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:999px;padding:7px 16px;cursor:pointer;font:inherit}
.tab.on{background:var(--primary);color:#202124;border-color:var(--primary);font-weight:600}
.hidden{display:none!important}
input,select{font:inherit;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
input:focus,select:focus{outline:none;border-color:var(--primary)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
.grow{flex:1;min-width:200px}
.muted{color:var(--muted);font-size:.85rem}
.job{display:flex;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}
.job:last-child{border-bottom:0}
.thumb{width:96px;height:54px;border-radius:8px;object-fit:cover;background:var(--panel2);flex:none}
.job .info{flex:1;min-width:0}
.job .title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{font-size:.8rem;padding:3px 10px;border-radius:999px;background:var(--panel2);white-space:nowrap}
.chip.done{color:var(--ok)}.chip.failed{color:var(--bad)}.chip.queued,.chip.taken{color:var(--warn)}.chip.skipped{color:var(--muted)}
.err{color:var(--bad);font-size:.8rem;margin-top:2px;word-break:break-word}
.ch{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);gap:10px}
.ch:last-child{border-bottom:0}
.logs{font-family:ui-monospace,Consolas,monospace;font-size:.82rem;direction:ltr;text-align:left;background:#0d0d0e;border-radius:10px;padding:10px;max-height:60vh;overflow:auto;white-space:pre-wrap;word-break:break-word}
.logs .l{padding:2px 0;border-bottom:1px solid #1b1b1d}
.logs .t{color:var(--muted)}.logs .s-render{color:var(--primary)}.logs .s-worker{color:var(--ok)}
.logs .bad{color:var(--bad)}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);padding:10px 16px;border-radius:12px;display:none;max-width:90%}
.login{max-width:380px;margin:12vh auto;text-align:center}
.logs .m{unicode-bidi:isolate}
@media (max-width:600px){.job{flex-wrap:wrap}.job .info{flex-basis:calc(100% - 100px)}.thumb{width:80px;height:45px}
.job .chip{margin-inline-start:92px}}
.login input{width:100%;margin:14px 0}
`;

function page(title, body) {
  return new Response(`<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title>
<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;700&display=swap" rel="stylesheet">
<style>${CSS}</style></head><body><div class="wrap">${body}</div></body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function loginForm(msg) {
  return `<form class="card login" method="post" action="${BASE}/login">
<h2 style="color:var(--primary);margin-top:0">📺 מעקב ערוצים</h2>
${msg ? `<p style="color:var(--bad)">${esc(msg)}</p>` : ""}
<input type="password" name="password" placeholder="סיסמה" autofocus>
<button class="btn" style="width:100%">כניסה</button>
<p><a href="/">💬 מעבר ל-Gemini</a></p></form>`;
}

async function dashboard(env) {
  const initial = JSON.stringify(await state(env)).replace(/</g, "\\u003c");
  return `
<header class="top"><h1>📺 מעקב ערוצים</h1><a class="btn ghost" href="/">💬 מעבר ל-Gemini</a></header>

<div class="card status-bar">
  <span class="pill"><span class="dot" id="agentDot"></span><span id="agentText">…</span></span>
  <span class="pill" id="checkText">…</span>
  <button class="btn small" id="wakeBtn" type="button">⏰ הער את שרת ההורדות</button>
  <label class="pill" style="cursor:pointer"><input type="checkbox" id="autoDl" style="margin:0"> הורדה אוטומטית למחשב</label>
</div>

<div class="tabs">
  <button class="tab on" data-tab="queue">📥 תור והורדות <span id="qCount"></span></button>
  <button class="tab" data-tab="channels">📡 ערוצים <span id="cCount"></span></button>
  <button class="tab" data-tab="add">➕ הוספה</button>
  <button class="tab" data-tab="logs">📜 לוגים</button>
</div>

<section id="tab-queue"><div class="card" id="jobs"></div></section>

<section id="tab-channels" class="hidden">
  <div class="card" id="channels"></div>
  <div class="card row" style="margin-top:0"><button class="btn small" id="checkBtn" type="button">🔄 בדוק עכשיו</button>
  <span class="muted">בדיקה אוטומטית כל 3 שעות</span></div>
</section>

<section id="tab-add" class="hidden">
  <form class="card" data-action="/channels/add">
    <b>הוספת ערוץ למעקב</b>
    <div class="row"><input class="grow" name="channel" placeholder="קישור לערוץ / @handle / UC…" required></div>
    <div class="row">
      <select name="mode">${Object.entries(MODES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
      <label>אורך מינימלי <input name="minMinutes" type="number" min="0" value="0" style="width:70px"> דק׳</label>
      <label><input type="checkbox" name="skipShorts" checked> בלי Shorts</label>
      <label>להוריד עכשיו את <input name="backfill" type="number" min="0" max="15" value="0" style="width:60px"> האחרונים</label>
    </div>
    <div class="row"><button class="btn">הוסף ערוץ</button></div>
  </form>
  <form class="card" data-action="/jobs/add">
    <b>הורדה חד-פעמית</b>
    <div class="row"><input class="grow" name="url" placeholder="קישור לסרטון ביוטיוב" required>
    <select name="mode">${Object.entries(MODES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
    <button class="btn">הוסף לתור</button></div>
  </form>
</section>

<section id="tab-logs" class="hidden">
  <div class="card">
    <div class="row" style="margin:0 0 10px;justify-content:space-between">
      <span class="muted"><span style="color:var(--ok)">worker</span> = Cloudflare · <span style="color:var(--primary)">render</span> = שרת ההורדות</span>
      <span><button class="btn small" id="copyLogs" type="button">📋 העתק הכל</button>
      <button class="btn small ghost" id="clearLogs" type="button">ניקוי</button></span>
    </div>
    <div class="logs" id="logs"></div>
  </div>
</section>

<div id="toast"></div>

<script>
const B = ${JSON.stringify(BASE)};
const STATUS = ${JSON.stringify(STATUS)};
const MODES = ${JSON.stringify(MODES)};
let S = ${initial};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago = (iso) => { if (!iso) return "אף פעם"; const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return m < 1 ? "עכשיו" : m < 60 ? "לפני " + m + " דק׳" : m < 1440 ? "לפני " + Math.round(m / 60) + " שע׳" : "לפני " + Math.round(m / 1440) + " ימים"; };
const hhmm = (iso) => new Date(iso).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function toast(msg, ms = 4000) { const t = $("toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms); }

function render() {
  // agent status
  const seen = S.agent && S.agent.seen, mins = seen ? (Date.now() - Date.parse(seen)) / 60000 : Infinity;
  const busy = S.jobs.some((j) => j.status === "taken");
  $("agentDot").className = "dot " + (mins < 5 ? "ok" : mins < 30 ? "warn" : "bad");
  const info = S.agent && S.agent.info;
  $("agentText").textContent = !S.renderUrl ? "RENDER_URL לא מוגדר" :
    "שרת ההורדות: " + (busy ? "מוריד…" : mins < 5 ? "ער" : "ישן") + " · נראה " + ago(seen) +
    (info && !info.cookies ? " · ⚠ אין עוגיות" : "");
  $("checkText").textContent = "בדיקת ערוצים: " + ago(S.lastCheck && S.lastCheck.at);
  // queue
  const active = S.jobs.filter((j) => j.status === "queued" || j.status === "taken").length;
  $("qCount").textContent = active ? "(" + active + ")" : "";
  $("cCount").textContent = "(" + S.channels.length + ")";
  $("jobs").innerHTML = S.jobs.length ? S.jobs.map((j) =>
    '<div class="job"><img class="thumb" loading="lazy" src="https://i.ytimg.com/vi/' + esc(j.videoId) + '/mqdefault.jpg" onerror="this.style.visibility=\\'hidden\\'">' +
    '<div class="info"><div class="title" title="' + esc(j.title) + '">' + esc(j.title) + '</div>' +
    '<div class="muted">' + esc(j.channelName) + ' · ' + (MODES[j.mode] || MODES.audio) + ' · ' + ago(j.updated) + '</div>' +
    (j.error ? '<div class="' + (j.status === "failed" ? "err" : "muted") + '">' + esc(j.error) + '</div>' : '') + '</div>' +
    '<span class="chip ' + esc(j.status) + '">' + (STATUS[j.status] || esc(j.status)) + '</span>' +
    (j.fileUrl ? '<a class="btn small" href="' + esc(j.fileUrl) + '">⬇ הורדה</a>' : '') +
    (["failed", "skipped", "taken"].includes(j.status) ? '<button class="btn small ghost" data-retry="' + esc(j.videoId) + '">שוב</button>' : '') +
    '</div>').join("") : '<div class="muted">התור ריק. הוסף ערוץ או קישור בלשונית ➕ הוספה.</div>';
  // channels
  $("channels").innerHTML = S.channels.length ? S.channels.map((c) =>
    '<div class="ch"><div><a href="https://www.youtube.com/channel/' + esc(c.id) + '" target="_blank">' + esc(c.name) + '</a>' +
    '<div class="muted">' + (MODES[c.mode] || MODES.audio) + (c.minMinutes ? " · " + c.minMinutes + " דק׳ ומעלה" : "") +
    (c.skipShorts ? " · בלי Shorts" : "") + '</div></div>' +
    '<button class="btn small ghost" data-remove="' + esc(c.id) + '">הסר</button></div>').join("")
    : '<div class="muted">עוד אין ערוצים. הוסף בלשונית ➕ הוספה.</div>';
  // logs (newest at the bottom)
  const box = $("logs"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = S.logs.length ? S.logs.map((l) => '<div class="l"><span class="t">' + hhmm(l.t) + '</span> <span class="s-' + esc(l.src) + '">[' + esc(l.src) + ']</span> ' +
    '<span dir="auto" class="m ' + (/ERROR|⚠|נכשל|\\[!\\]|שגיא/.test(l.msg) ? "bad" : "") + '">' + esc(l.msg) + '</span></div>').join("") : '<span class="t">אין עדיין לוגים</span>';
  if (atBottom) box.scrollTop = box.scrollHeight;
}

async function refresh() {
  try { S = await (await fetch(B + "/status")).json(); render(); autoDownload(); } catch (e) {}
}

async function post(path, data) {
  const r = await fetch(B + path, { method: "POST", body: data });
  if (!r.ok) throw new Error((await r.text()).replace(/^Error: /, ""));
  return r;
}

// tabs (remembered in this browser)
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === name));
  document.querySelectorAll("section[id^=tab-]").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + name));
  try { localStorage.setItem("tab", name); } catch (e) {}
  if (name === "logs") { const b = $("logs"); b.scrollTop = b.scrollHeight; }
}
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => showTab(t.dataset.tab));
try { const t = localStorage.getItem("tab"); if (t) showTab(t); } catch (e) {}

// forms
document.querySelectorAll("form[data-action]").forEach((f) => f.onsubmit = async (e) => {
  e.preventDefault();
  const btn = f.querySelector("button"); btn.disabled = true;
  try { await post(f.dataset.action, new FormData(f)); f.reset(); toast("✓ נוסף"); showTab("queue"); await refresh(); }
  catch (err) { toast("✗ " + err.message, 8000); }
  btn.disabled = false;
});
document.addEventListener("click", async (e) => {
  const r = e.target.dataset.retry, rm = e.target.dataset.remove;
  if (r) { const d = new FormData(); d.append("id", r); await post("/jobs/retry", d).catch((x) => toast(x.message)); refresh(); }
  if (rm && confirm("להסיר את הערוץ?")) { const d = new FormData(); d.append("id", rm); await post("/channels/remove", d).catch((x) => toast(x.message)); refresh(); }
});
$("wakeBtn").onclick = async () => {
  const b = $("wakeBtn"); b.disabled = true; toast("מעיר את שרת ההורדות… (עד דקה)", 60000);
  try { const r = await (await fetch(B + "/wake", { method: "POST" })).json(); toast(r.result === "ok" ? "✓ השרת ער ובודק את התור" : "✗ " + r.result, 10000); }
  catch (e) { toast("✗ " + e); }
  b.disabled = false; refresh();
};
$("checkBtn").onclick = async () => {
  const b = $("checkBtn"); b.disabled = true;
  try { await post("/check", new FormData()); toast("✓ נבדק"); } catch (e) { toast("✗ " + e.message); }
  b.disabled = false; refresh();
};
$("clearLogs").onclick = async () => { await fetch(B + "/logs/clear", { method: "POST" }); refresh(); };
$("copyLogs").onclick = async () => {
  const text = S.logs.map((l) => hhmm(l.t) + " [" + l.src + "] " + l.msg).join("\\n");
  try { await navigator.clipboard.writeText(text); toast("✓ הועתק — אפשר להדביק"); }
  catch (e) { const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); toast("✓ הועתק"); }
};

// Auto-download: each newly finished file is downloaded once (remembered in
// this browser) — only files finished after the box was ticked.
const store = (k, v) => { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} };
const load = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
$("autoDl").checked = !!load("autoDlSince");
$("autoDl").onchange = (e) => store("autoDlSince", e.target.checked ? new Date().toISOString() : undefined);
let dlBusy = false;
async function autoDownload() {
  const since = load("autoDlSince");
  if (!since || dlBusy) return;
  dlBusy = true;
  const got = JSON.parse(load("autoDlGot") || "[]");
  for (const j of S.jobs) {
    if (j.status !== "done" || !j.fileUrl || j.updated < since || got.includes(j.videoId)) continue;
    const f = document.createElement("iframe"); f.style.display = "none"; f.src = j.fileUrl; document.body.appendChild(f);
    got.push(j.videoId); store("autoDlGot", JSON.stringify(got.slice(-500)));
    toast("⬇ מוריד: " + j.title);
    await new Promise((r) => setTimeout(r, 1500));
  }
  dlBusy = false;
}

render(); autoDownload();
setInterval(refresh, 8000);
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
