// YouTube channel tracker — Cloudflare Worker (single file, no build step).
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
//   Secrets:               ADMIN_PASSWORD (page login), AGENT_TOKEN (agent API)
//   Cron Trigger:          e.g. "0 */3 * * *"
//   Optional R2 binding:   FILES

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
    ctx.waitUntil(checkAllChannels(env));
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
  const p = url.pathname;

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
      return redirect("/", { "Set-Cookie": `auth=${tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000` });
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
      }
      return redirect("/");
    }
    if (p === "/channels/remove") {
      const id = String(f.get("id"));
      await putJSON(env, "channels", (await getChannels(env)).filter((c) => c.id !== id));
      await env.DB.delete("seen:" + id);
      return redirect("/");
    }
    if (p === "/check") {
      await checkAllChannels(env);
      return redirect("/");
    }
    if (p === "/jobs/retry") {
      const j = await getJSON(env, "job:" + f.get("id"), null);
      if (j) { j.status = "queued"; j.error = ""; j.updated = new Date().toISOString(); await putJSON(env, "job:" + j.videoId, j); }
      return redirect("/");
    }
    if (p === "/jobs/add") {
      // Manual one-off: paste a video link.
      const m = String(f.get("url") || "").match(/(?:v=|youtu\.be\/|shorts\/)([\w-]{11})/);
      if (m) await addJobs(env, [{
        videoId: m[1], channelId: "", channelName: "ידני", title: m[1],
        published: "", url: "https://www.youtube.com/watch?v=" + m[1],
        mode: f.get("mode") === "video" ? "video" : "audio", minMinutes: 0,
        status: "queued", updated: new Date().toISOString() }]);
      return redirect("/");
    }
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
  return `<h1>מעקב ערוצים</h1><form class="card" method="post" action="/login">
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
<td><form method="post" action="/channels/remove" onsubmit="return confirm('להסיר?')"><input type="hidden" name="id" value="${esc(c.id)}"><button class="gray">הסר</button></form></td></tr>`).join("");
  const jobRows = jobs.slice(0, 100).map((j) => `<tr><td>${esc(j.title)}<div class="muted">${esc(j.channelName)} · ${esc((j.published || "").slice(0, 10))}</div></td>
<td class="s-${esc(j.status)}">${STATUS[j.status] || esc(j.status)}${j.error ? `<div class="muted">${esc(j.error)}</div>` : ""}</td>
<td>${j.fileUrl ? `<a href="${esc(j.fileUrl)}">הורדה</a>` : ""}
${["failed", "skipped"].includes(j.status) ? `<form method="post" action="/jobs/retry"><input type="hidden" name="id" value="${esc(j.videoId)}"><button class="gray">שוב</button></form>` : ""}</td></tr>`).join("");
  return `<h1>מעקב ערוצים</h1>
<div class="card"><b>הוספת ערוץ</b><form method="post" action="/channels/add">
<input name="channel" placeholder="קישור לערוץ / @handle / UC…" required style="width:100%">
<select name="mode"><option value="audio">שיר (MP3)</option><option value="video">סרטון (MP4)</option></select>
אורך מינימלי (דקות): <input name="minMinutes" type="number" min="0" value="0" style="width:70px">
<label><input type="checkbox" name="skipShorts" checked> בלי Shorts</label>
להוריד עכשיו את האחרונים: <input name="backfill" type="number" min="0" max="15" value="0" style="width:60px">
<button>הוסף</button></form></div>
<div class="card"><b>הורדה חד-פעמית</b><form method="post" action="/jobs/add">
<input name="url" placeholder="קישור לסרטון ביוטיוב" required style="width:60%">
<select name="mode"><option value="audio">שיר</option><option value="video">סרטון</option></select> <button>הוסף לתור</button></form></div>
<h2>ערוצים (${channels.length})</h2>
<div class="card"><table><tr><th>ערוץ</th><th>סוג</th><th>אורך</th><th>Shorts</th><th></th></tr>${chRows || '<tr><td colspan="5">עוד אין ערוצים</td></tr>'}</table>
<form method="post" action="/check"><button>בדוק עכשיו</button></form>
<div class="muted">בדיקה אחרונה: ${last ? esc(last.at.replace("T", " ").slice(0, 16)) + " UTC · נוספו " + last.added + (last.errors.length ? " · שגיאות: " + esc(last.errors.join(" | ")) : "") : "עוד לא"}</div></div>
<h2>תור והורדות</h2><div class="card"><table><tr><th>סרטון</th><th>מצב</th><th></th></tr>${jobRows || '<tr><td colspan="3">ריק</td></tr>'}</table></div>`;
}
