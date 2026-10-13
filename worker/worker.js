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
//   D1 database binding:   SQL  (recommended — free: 5M reads, 100k writes a day)
//   KV namespace binding:  DB   (the original store; copied to D1 automatically)
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
    let store;
    try {
      env = await withStore(env);
      store = env.DB;
      const res = await route(req, env, ctx);
      if (store) await autoCheck(env);
      if (store && store.flush) await store.flush();
      return res;
    } catch (e) {
      try { if (store && store.flush) await store.flush(); } catch (x) {}
      return new Response("Error: " + (e && e.message || e), { status: 500 });
    }
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      env = await withStore(env);
      await putJSON(env, "autocheck", { at: Date.now() });
      for (const pid of await profileIds(env)) await checkAllChannels(profileEnv(env, pid));
      if (env.DB && env.DB.flush) await env.DB.flush();
      await wakeAgent(env);
    })());
  },
};

// Channels are checked every 3 hours even without a Cron Trigger: the first
// request after 3 hours (the desktop app and the download server ask all the
// time) runs the check for every profile.
const AUTO_CHECK_MS = 3 * 60 * 60 * 1000;
async function autoCheck(env) {
  try {
    const last = await getJSON(env, "autocheck", { at: 0 });
    if (Date.now() - (last.at || 0) < AUTO_CHECK_MS) return;
    await putJSON(env, "autocheck", { at: Date.now() });
    let added = 0;
    for (const pid of await profileIds(env)) added += (await checkAllChannels(profileEnv(env, pid))).added;
    if (added) await wakeAgent(env);
  } catch (e) { console.log("autoCheck: " + (e && e.message || e)); }
}

// ---------- storage ----------
// Where the data lives:
//   * D1 (binding "SQL") when connected — free tier: 5M reads and 100k
//     writes a day. On first use everything is copied over from KV.
//   * otherwise KV (binding "DB") — free tier: only 1,000 writes a day.
// On top of either, each profile's queue/history is ONE record ("jobsdb")
// instead of one record per video: showing the page costs a few reads, not
// hundreds, and changes are written once at the end of the request.

function d1Backend(db) {
  return {
    get: async (k) => { const r = await db.prepare("SELECT v FROM kv WHERE k = ?1").bind(k).first(); return r ? r.v : null; },
    put: (k, v) => db.prepare("INSERT INTO kv (k, v) VALUES (?1, ?2) ON CONFLICT(k) DO UPDATE SET v = ?2").bind(k, String(v)).run(),
    delete: (k) => db.prepare("DELETE FROM kv WHERE k = ?1").bind(k).run(),
    deletePrefix: (pre) => db.prepare("DELETE FROM kv WHERE substr(k, 1, ?1) = ?2").bind(pre.length, pre).run(),
    list: async ({ prefix = "" } = {}) => {
      const r = await db.prepare("SELECT k FROM kv WHERE substr(k, 1, ?1) = ?2").bind(prefix.length, prefix).all();
      return { keys: r.results.map((x) => ({ name: x.k })), list_complete: true };
    },
  };
}

// KV → D1, a few hundred records per request (the free plan limits work per
// request). Until it's complete the site keeps using KV.
let D1_STATE = "";     // "", "ready" (remembered per Worker instance)
async function d1Ready(env) {
  if (D1_STATE === "ready") return true;
  const db = env.SQL;
  await db.prepare("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)").run();
  if (await db.prepare("SELECT 1 AS x FROM kv WHERE k = '_migrated'").first()) { D1_STATE = "ready"; return true; }
  if (!env.DB) {
    await db.prepare("INSERT OR IGNORE INTO kv (k, v) VALUES ('_migrated', ?1)").bind(new Date().toISOString()).run();
    D1_STATE = "ready"; return true;
  }
  const kvKeys = [];
  let cursor;
  do { const r = await env.DB.list({ cursor }); kvKeys.push(...r.keys.map((x) => x.name)); cursor = r.list_complete ? null : r.cursor; } while (cursor);
  const have = new Set((await db.prepare("SELECT k FROM kv").all()).results.map((x) => x.k));
  const todo = kvKeys.filter((k) => !have.has(k)).slice(0, 250);
  const vals = await Promise.all(todo.map((k) => env.DB.get(k)));
  const rows = todo.map((k, i) => [k, vals[i]]).filter((r) => r[1] != null);
  for (let i = 0; i < rows.length; i += 40) {          // 40 rows (80 values) per statement
    const part = rows.slice(i, i + 40);
    await db.prepare("INSERT OR IGNORE INTO kv (k, v) VALUES " + part.map((_, j) => `(?${2 * j + 1}, ?${2 * j + 2})`).join(", "))
      .bind(...part.flat()).run();
  }
  if (kvKeys.length - have.size <= todo.length) {
    // Per-video records already folded into a "jobsdb" record aren't needed.
    const all = (await db.prepare("SELECT k FROM kv").all()).results.map((x) => x.k);
    for (const pre of new Set(all.filter((k) => /^(p:[^:]+:)?jobsdb$/.test(k)).map((k) => k.slice(0, -6))))
      await db.prepare("DELETE FROM kv WHERE substr(k, 1, ?1) = ?2").bind(pre.length + 4, pre + "job:").run();
    await db.prepare("INSERT OR IGNORE INTO kv (k, v) VALUES ('_migrated', ?1)").bind(new Date().toISOString()).run();
    D1_STATE = "ready"; return true;
  }
  return false;
}

async function withStore(env) {
  let base = null;
  if (env.SQL) {
    try { if (await d1Ready(env)) base = d1Backend(env.SQL); }
    catch (e) { console.log("D1: " + (e && e.message || e)); }
  }
  if (!base && env.DB) base = env.DB;
  if (!base) return env;                       // nothing connected — route() explains
  return { ...env, KV: env.DB, DB: jobLayer(base) };
}

function jobLayer(base) {
  const blobs = new Map(), dirty = new Set(), converted = [];
  const parse = (key) => { const m = /^((?:p:[^:]+:)?)(?:jobs|job:(.+))$/.exec(key); return m && { pre: m[1], id: m[2] }; };
  function load(pre) {
    const bk = pre + "jobsdb";
    if (!blobs.has(bk)) blobs.set(bk, (async () => {
      const raw = await base.get(bk);
      if (raw) return JSON.parse(raw);
      // Old layout ("jobs" = ids, "job:<id>" each): converted once.
      const ids = JSON.parse((await base.get(pre + "jobs")) || "[]");
      const vals = await Promise.all(ids.map((id) => base.get(pre + "job:" + id)));
      const b = { order: [], map: {} };
      ids.forEach((id, i) => { if (vals[i]) { b.order.push(id); b.map[id] = JSON.parse(vals[i]); } });
      if (ids.length) { dirty.add(bk); converted.push(pre); }
      return b;
    })());
    return blobs.get(bk);
  }
  return {
    async get(key) {
      const p = parse(key);
      if (!p) return base.get(key);
      const b = await load(p.pre);
      if (p.id === undefined) return JSON.stringify(b.order);
      return b.map[p.id] ? JSON.stringify(b.map[p.id]) : null;
    },
    async put(key, v) {
      const p = parse(key);
      if (!p) return base.put(key, v);
      const b = await load(p.pre);
      if (p.id === undefined) {
        b.order = JSON.parse(v);
        const keep = new Set(b.order);
        for (const id of Object.keys(b.map)) if (!keep.has(id)) delete b.map[id];
      } else {
        b.map[p.id] = JSON.parse(v);
      }
      dirty.add(p.pre + "jobsdb");
    },
    async delete(key) {
      const p = parse(key);
      if (!p) return base.delete(key);
      const b = await load(p.pre);
      if (p.id !== undefined) { delete b.map[p.id]; b.order = b.order.filter((x) => x !== p.id); }
      dirty.add(p.pre + "jobsdb");
    },
    list: (opts) => base.list(opts),
    async flush() {
      for (const bk of dirty) await base.put(bk, JSON.stringify(await blobs.get(bk)));
      dirty.clear();
      if (base.deletePrefix) for (const pre of converted.splice(0)) await base.deletePrefix(pre + "job:");   // old records (D1 only)
    },
  };
}

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
  // A NetFree-only profile: videos wait until the desktop app (behind
  // NetFree) confirms they're open. Lists/searches expand first.
  // A profile that downloads on its own computer skips this: the download is the check.
  const pi = await profileInfo(env, env.PROFILE);
  if (pi.netfree && !pi.direct) {
    for (const j of jobs) if (!j.kind && j.status === "queued" && j.nf !== "ok") j.status = "nfcheck";
  }
  const ids = await getJobIds(env);
  for (const j of [...jobs].reverse()) {   // keep the newest on top
    await putJSON(env, "job:" + j.videoId, j);
    if (!ids.includes(j.videoId)) ids.unshift(j.videoId);
  }
  const dropped = ids.splice(JOBS_CAP);
  await putJSON(env, "jobs", ids);
  for (const id of dropped) await env.DB.delete("job:" + id);
}

// Remove jobs from the history completely. A channel's "seen" list still
// remembers them, so the tracker won't download them again by itself.
async function deleteJobs(env, ids) {
  const drop = new Set(ids);
  if ((env.PROFILE || "main") === "main") {     // the admin's own history: also from the general dashboard
    const acts = await getJSON(env, "activity", []);
    const keep = acts.filter((a) => !(a.pid === "main" && drop.has(a.videoId)));
    if (keep.length !== acts.length) await putJSON(env, "activity", keep);
  }
  await putJSON(env, "jobs", (await getJobIds(env)).filter((id) => !drop.has(id)));
  for (const id of drop) await env.DB.delete("job:" + id);
}

// ---------- general dashboard (admin) ----------
// Every download of every profile, kept apart from the profiles' own
// histories: a profile deleting its history doesn't remove anything here.
// Only the admin's own deletions apply (to the admin's own entries).
const ACTIVITY_CAP = 600;
async function recordActivity(env, j, patch) {
  const pid = env.PROFILE || "main";
  const acts = await getJSON(env, "activity", []);
  const i = acts.findIndex((a) => a.pid === pid && a.videoId === j.videoId);
  if (patch) {                      // e.g. "reached the computer": update in place
    if (i < 0) return;
    Object.assign(acts[i], patch);
  } else {
    const prev = i >= 0 ? acts.splice(i, 1)[0] : {};
    acts.unshift({ ...prev, t: new Date().toISOString(), pid, name: (await profileInfo(env, pid)).name,
      videoId: j.videoId, title: j.title, channelName: j.channelName || "", mode: j.mode || "",
      status: j.status, error: j.error || "", kind: j.kind || "" });
  }
  await putJSON(env, "activity", acts.slice(0, ACTIVITY_CAP));
}

function countJobs(jobs) {
  const c = { done: 0, pc: 0, taken: 0, waiting: 0, problem: 0 };
  for (const j of jobs) {
    if (j.status === "done") { c.done++; if (j.pcGot) c.pc++; }
    else if (j.status === "taken") c.taken++;
    else if (["queued", "nfcheck", "paused"].includes(j.status)) c.waiting++;
    else if (["failed", "blocked", "skipped"].includes(j.status)) c.problem++;
  }
  return c;
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

// The channel's feed, plus its "uploads" playlist feed (UU…): YouTube's
// channel feed sometimes lags or misses new uploads; together they don't.
// Never cached, and an answer that isn't a feed is an error, not "nothing new".
async function fetchFeed(q) {
  const r = await fetch("https://www.youtube.com/feeds/videos.xml?" + q, {
    cf: { cacheTtl: 0, cacheEverything: false },
    headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
               "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8" },
  });
  if (!r.ok) throw new Error("RSS " + r.status);
  const xml = await r.text();
  if (!/<feed[\s>]/.test(xml)) throw new Error("RSS: התקבל דף במקום פיד (" + xml.length + "b)");
  return xml;
}

async function readFeed(channelId) {
  const tries = [fetchFeed("channel_id=" + channelId)];
  if (/^UC/.test(channelId)) tries.push(fetchFeed("playlist_id=UU" + channelId.slice(2)));
  const got = await Promise.allSettled(tries);
  const xmls = got.filter((g) => g.status === "fulfilled").map((g) => g.value);
  if (!xmls.length) throw got[0].reason;
  const name = decode((xmls[0].match(/<title>([^<]*)<\/title>/) || [])[1] || channelId);
  const byId = new Map();
  for (const xml of xmls) {
    for (const block of xml.split("<entry>").slice(1)) {
      const id = (block.match(/<yt:videoId>([^<]+)<\/yt:videoId>/) || [])[1];
      if (!id || byId.has(id)) continue;
      byId.set(id, {
        videoId: id,
        title: decode((block.match(/<title>([^<]*)<\/title>/) || [])[1] || id),
        published: (block.match(/<published>([^<]+)<\/published>/) || [])[1] || "",
        // Shorts are linked as youtube.com/shorts/<id> in the feed.
        isShort: /youtube\.com\/shorts\//.test(block),
      });
    }
  }
  const entries = [...byId.values()].sort((x, y) => (y.published || "").localeCompare(x.published || ""));
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

async function checkChannel(env, ch, notes) {
  const seen = await getJSON(env, "seen:" + ch.id, []);
  const { entries } = await readFeed(ch.id);
  const top = entries[0];
  if (notes) notes.push(ch.name + ": " + (top ? "האחרון ביוטיוב " + (top.published || "").slice(0, 10) : "הפיד ריק"));
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
  const errors = [], notes = [];
  for (const ch of channels) {
    try { added += await checkChannel(env, ch, notes); }
    catch (e) { errors.push(ch.name + ": " + e.message); }
  }
  await putJSON(env, "lastCheck", { at: new Date().toISOString(), added, errors });
  await addLog(env, "worker", `בדיקת ערוצים: ${channels.length} ערוצים, נוספו ${added} לתור` +
    (notes.length ? ` · ${notes.join(" | ")}` : "") + (errors.length ? ` · שגיאות: ${errors.join(" | ")}` : ""));
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
  if (res !== "ok") await addLog(env, "worker", "השכמת שרת ההורדות: " + res);   // success isn't logged (saves writes)
  return res;
}

// ---------- profiles ----------
// "main" is the admin (ADMIN_PASSWORD) and keeps the original, unprefixed
// keys. Every other profile has its own copy of everything under
// "p:<id>:" — channels, queue, history, log, settings. "agent" and the
// profile list itself stay global.
const GLOBAL_KEYS = new Set(["agent", "profiles", "mainprof", "activity", "ytcookies", "autocheck"]);

function profileEnv(env, pid) {
  if (!pid || pid === "main") return { ...env, PROFILE: "main" };
  const pre = "p:" + pid + ":";
  const k = (key) => (GLOBAL_KEYS.has(key) ? key : pre + key);
  return { ...env, PROFILE: pid, DB: {
    get: (key) => env.DB.get(k(key)),
    put: (key, v) => env.DB.put(k(key), v),
    delete: (key) => env.DB.delete(k(key)),
  } };
}

// What the admin set for a profile: shown name, "only what NetFree allows",
// and whether the desktop app keeps its files in an encrypted vault.
async function profileInfo(env, pid) {
  if (!pid || pid === "main") {
    const m = await getJSON(env, "mainprof", {});
    return { id: "main", name: m.name || "מנהל", netfree: false, vault: !!m.vault, direct: !!m.direct, admin: true };
  }
  const pr = (await getJSON(env, "profiles", {}))[pid] || {};
  return { id: pid, name: pr.name || pid, netfree: !!pr.netfree, vault: !!pr.vault, direct: !!pr.direct, admin: false, created: pr.created };
}

async function profileIds(env) {
  return ["main", ...Object.keys(await getJSON(env, "profiles", {}))];
}

// ---------- auth ----------

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function profileToken(env, pid) {
  if (pid === "main") return sha256("yt-tracker:main:" + env.ADMIN_PASSWORD);
  const prof = (await getJSON(env, "profiles", {}))[pid];
  return prof ? sha256("yt-tracker:" + pid + ":" + prof.hash) : null;
}

// Which profile is logged in (cookie "auth=<profile>.<token>"), or null.
async function whoIs(req, env) {
  if (!env.ADMIN_PASSWORD) return null;
  const c = req.headers.get("Cookie") || "";
  const m = c.match(/(?:^|;\s*)auth=([\w-]+)\.([a-f0-9]{64})/);
  if (m && m[2] === await profileToken(env, m[1])) return m[1];
  const legacy = c.match(/(?:^|;\s*)auth=([a-f0-9]{64})(?:;|$)/);   // cookie from before profiles
  if (legacy && legacy[1] === await sha256("yt-tracker:" + env.ADMIN_PASSWORD)) return "main";
  return null;
}

async function checkCode(env, pid, password) {
  if (pid === "main") return !!env.ADMIN_PASSWORD && password === env.ADMIN_PASSWORD;
  const pr = (await getJSON(env, "profiles", {}))[pid];
  return !!pr && pr.hash === await sha256("pw:" + password);
}

// Key for the cookie extension: derived from the admin password, shown to
// the admin in Settings.
async function cookiePushKey(env) {
  return (await sha256("yt-cookie-push:" + env.ADMIN_PASSWORD)).slice(0, 32);
}

async function loginProfile(env, password) {
  if (env.ADMIN_PASSWORD && password === env.ADMIN_PASSWORD) return "main";
  const h = await sha256("pw:" + password);
  const profs = await getJSON(env, "profiles", {});
  return Object.keys(profs).find((id) => profs[id].hash === h) || null;
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
  if (!env.DB && !env.SQL) {
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
    if (!agent.seen || Date.now() - Date.parse(agent.seen) > 180000) {
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
      let stuck = [];
      for (const pid of await profileIds(env)) {
        const penv = profileEnv(env, pid);
        for (const j of (await listJobs(penv)).filter((x) => x.status === "taken")) {
          j.status = "queued"; j.updated = new Date().toISOString();
          await putJSON(penv, "job:" + j.videoId, j);
          stuck.push(j);
        }
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
      // Jobs from every profile. "⏫ הורד עכשיו" first (newest bump wins),
      // then oldest first.
      const all = [];
      for (const pid of await profileIds(env)) {
        const penv = profileEnv(env, pid);
        const pi = await profileInfo(env, pid);
        for (const j of (await listJobs(penv)).reverse()) {
          // "מוריד מהמחשב": the profile's computer tries first; the server only
          // takes a job after the computer failed on an unrestricted profile.
          if (pi.direct && !j.fallback) continue;
          all.push({ j, pid, penv, nf: pi.netfree });
        }
      }
      all.sort((a, b) => (b.j.priority || 0) - (a.j.priority || 0));
      for (const { j, pid, penv, nf } of all) {
        if (j.status === "nfcheck" && !(nf && !j.kind)) j.status = "queued";     // restriction lifted
        if (nf && !j.kind && j.status === "queued" && j.nf !== "ok") {
          // Never download for a NetFree-only profile before the check.
          j.status = "nfcheck"; j.updated = new Date().toISOString();
          await putJSON(penv, "job:" + j.videoId, j);
          continue;
        }
        const stale = j.status === "taken" && now - Date.parse(j.updated) > CLAIM_TIMEOUT_MS;
        if (j.status !== "queued" && !stale) continue;
        j.status = "taken"; j.updated = new Date().toISOString(); j.profile = pid;
        // "Convert on the PC": the server skips the slow MP3 encode and the
        // desktop app converts after downloading.
        j.pcConvert = j.mode === "audio";   // the server never encodes MP3 — the app does
        if (nf && j.kind === "search") j.candidates = 5;   // the app keeps the first one NetFree allows
        await putJSON(penv, "job:" + j.videoId, j);
        out.push(j);
        if (out.length >= limit) break;
      }
      if (out.length) await addLog(env, "worker", `הסוכן לקח ${out.length} משימות: ` + out.map((j) => j.title).join(" | "));
      return json({ jobs: out });
    }
    if (p === "/api/enqueue" && req.method === "POST") {
      // Videos found by a 'list' job. Skip ones already in the list.
      const b = await req.json();
      env = profileEnv(env, b.profile);
      const known = new Set(await getJobIds(env));
      const fresh = (b.items || []).filter((it) => it.videoId && !known.has(it.videoId)).map((it) => ({
        videoId: it.videoId, channelId: b.channelId || "", channelName: b.channelName || "",
        title: it.title || it.videoId, published: "", url: "https://www.youtube.com/watch?v=" + it.videoId,
        mode: modeOf(b.mode), minMinutes: b.minMinutes || 0, status: "queued", updated: new Date().toISOString(),
        ...(it.group ? { group: String(it.group), rank: +it.rank || 0 } : {}) }));
      if (b.priority) fresh.forEach((j) => { j.priority = b.priority; });
      await addJobs(env, fresh);
      // A search job is replaced by the video it found (keeps the list tidy).
      if (b.replace) await deleteJobs(env, [String(b.replace)]);
      return json({ added: fresh.length, skipped: (b.items || []).length - fresh.length });
    }
    if (p === "/api/check" && req.method === "POST") {
      // The agent asks every few seconds whether its job was cancelled.
      const b = await req.json();
      env = profileEnv(env, b.profile);
      const j = await getJSON(env, "job:" + b.videoId, null);
      return json({ status: j ? j.status : "missing" });
    }
    if (p === "/api/report" && req.method === "POST") {
      const b = await req.json();
      env = profileEnv(env, b.profile);
      const j = await getJSON(env, "job:" + b.videoId, null);
      if (!j) return json({ error: "no such job" }, 404);
      if (j.status === "cancelled") return json({ ok: true, ignored: "cancelled" });
      Object.assign(j, {
        status: ["done", "failed", "skipped"].includes(b.status) ? b.status : "failed",
        fileUrl: b.fileUrl || "", fileName: b.fileName || "", error: (b.error || "").slice(0, 300),
        updated: new Date().toISOString(),
      });
      await putJSON(env, "job:" + j.videoId, j);
      if (!j.kind) await recordActivity(env, j);
      await addLog(env, "worker", `${STATUS[j.status] || j.status}: ${j.title}` + (j.error ? ` — ${j.error}` : ""));
      return json({ ok: true });
    }
    if (p === "/api/cookies" && req.method === "POST") {
      // Fresh YouTube cookies from the browser extension (see /cookies/push).
      const c = await getJSON(env, "ytcookies", null);
      return json(c ? { text: c.text, at: c.at, count: c.count } : {});
    }
    return json({ error: "not found" }, 404);
  }

  // --- login ---
  if (p === "/login" && req.method === "POST") {
    const f = await req.formData();
    const want = String(f.get("pid") || ""), pw = String(f.get("password") || "");
    const pid = want ? ((await checkCode(env, want, pw)) ? want : null) : await loginProfile(env, pw);
    if (pid) {
      const tok = await profileToken(env, pid);
      return redirect(BASE + "/", { "Set-Cookie": `auth=${pid}.${tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000` });
    }
    return page("כניסה", await loginForm(env, "קוד שגוי", want));
  }
  if (p === "/logout") {
    return redirect(BASE + "/", { "Set-Cookie": "auth=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0" });
  }
  // The browser extension sends YouTube cookies here (with the admin's key);
  // only the download server can read them back (/api/cookies).
  if (p === "/cookies/push" && req.method === "POST") {
    if (!env.ADMIN_PASSWORD || req.headers.get("X-Push-Key") !== await cookiePushKey(env)) {
      return new Response("מפתח שגוי — העתק אותו מהאתר: הגדרות ← תוסף דפדפן", { status: 403 });
    }
    const b = await req.json();
    const text = String(b.cookies || "");
    if (!text.startsWith("# Netscape") || text.length > 200000) return new Response("קובץ עוגיות לא תקין", { status: 400 });
    const old = await getJSON(env, "ytcookies", null);
    // Write at most every 5 minutes unless something changed (saves writes).
    if (!old || old.text !== text || Date.now() - Date.parse(old.at) > 300000) {
      await putJSON(env, "ytcookies", { text, at: new Date().toISOString(), count: +b.count || 0 });
    }
    return json({ ok: true });
  }
  const pid = await whoIs(req, env);
  if (!pid) {
    if (!env.ADMIN_PASSWORD) return page("הגדרה חסרה", "<p>חסר Secret בשם <b>ADMIN_PASSWORD</b> ב-Worker. ראה README.</p>");
    return page("כניסה", await loginForm(env, ""));
  }
  const rootEnv = env;
  env = profileEnv(env, pid);   // from here on everything is this profile's own data
  if (p === "/profiles/add" && req.method === "POST" && pid === "main") {
    const f = await req.formData();
    const name = String(f.get("name") || "").trim().slice(0, 40), pw = String(f.get("password") || "");
    if (!name || pw.length < 4) return new Response("שם וסיסמה (לפחות 4 תווים) נדרשים", { status: 400 });
    if (pw === env.ADMIN_PASSWORD || await loginProfile(rootEnv, pw)) return new Response("הסיסמה כבר בשימוש", { status: 400 });
    const profs = await getJSON(rootEnv, "profiles", {});
    const id = "u" + Date.now().toString(36);
    profs[id] = { name, hash: await sha256("pw:" + pw), created: new Date().toISOString(),
                  netfree: f.get("netfree") === "on", vault: f.get("vault") === "on", direct: f.get("direct") === "on" };
    await putJSON(rootEnv, "profiles", profs);
    await addLog(env, "worker", `נוצר פרופיל: ${name}`);
    return json({ ok: true, id });
  }
  if (p === "/profiles/update" && req.method === "POST" && pid === "main") {
    // Admin only: rename, new code, "only NetFree", vault. Users can't
    // change these themselves.
    const f = await req.formData(), id = String(f.get("id") || "");
    const name = String(f.get("name") || "").trim().slice(0, 40), on = (k) => f.get(k) === "1";
    if (id === "main") {
      const m = await getJSON(rootEnv, "mainprof", {});
      if (f.has("vault")) m.vault = on("vault");
      if (f.has("direct")) m.direct = on("direct");
      if (name) m.name = name;
      await putJSON(rootEnv, "mainprof", m);
      return json({ ok: true });
    }
    const profs = await getJSON(rootEnv, "profiles", {}), pr = profs[id];
    if (!pr) return new Response("אין פרופיל כזה", { status: 404 });
    if (f.has("netfree")) pr.netfree = on("netfree");
    if (f.has("vault")) pr.vault = on("vault");
    if (f.has("direct")) pr.direct = on("direct");
    if (name) pr.name = name;
    const pw = String(f.get("password") || "");
    if (pw) {
      if (pw.length < 4) return new Response("קוד של 4 תווים לפחות", { status: 400 });
      if (pw === env.ADMIN_PASSWORD || await loginProfile(rootEnv, pw)) return new Response("הקוד כבר בשימוש", { status: 400 });
      pr.hash = await sha256("pw:" + pw);            // also signs that profile out everywhere
    }
    await putJSON(rootEnv, "profiles", profs);
    await addLog(env, "worker", `עודכן פרופיל: ${pr.name}` + (f.has("netfree") ? (pr.netfree ? " · רק פתוח בנטפרי" : " · בלי הגבלת נטפרי") : "") +
      (f.has("vault") ? (pr.vault ? " · כספת" : " · בלי כספת") : "") +
      (f.has("direct") ? (pr.direct ? " · מוריד מהמחשב" : " · מוריד בשרת") : "") + (pw ? " · קוד חדש" : ""));
    return json({ ok: true });
  }
  // --- NetFree check, done by the desktop app (it sits behind NetFree; the
  // download server doesn't). Only the admin's app session reports results.
  if (p === "/nf/pending" && pid === "main") {
    const items = [];
    for (const q of await profileIds(rootEnv)) {
      const qi = await profileInfo(rootEnv, q);
      if (!qi.netfree || qi.direct) continue;
      const groups = new Set();
      const jobs = (await listJobs(profileEnv(rootEnv, q)))
        .filter((j) => j.status === "nfcheck" || (j.status === "queued" && !j.kind && j.nf !== "ok"))
        .sort((a, b) => (a.rank || 0) - (b.rank || 0));
      for (const j of jobs) {
        if (j.group) { if (groups.has(j.group)) continue; groups.add(j.group); }   // best search result first
        items.push({ pid: q, videoId: j.videoId, title: j.title });
        if (items.length >= 20) break;
      }
    }
    return json({ items });
  }
  if (p === "/nf/result" && req.method === "POST" && pid === "main") {
    const b = await req.json();
    const penv = profileEnv(rootEnv, String(b.pid || ""));
    const j = await getJSON(penv, "job:" + b.videoId, null);
    if (!j || !(j.status === "nfcheck" || (j.status === "queued" && j.nf !== "ok"))) return json({ ok: true, ignored: true });
    const now = new Date().toISOString();
    const mates = j.group ? (await listJobs(penv)).filter((x) => x.group === j.group && x.videoId !== j.videoId) : [];
    if (b.ok) {
      Object.assign(j, { status: "queued", nf: "ok", error: "", updated: now });
      await putJSON(penv, "job:" + j.videoId, j);
      const drop = mates.filter((x) => ["nfcheck", "blocked"].includes(x.status)).map((x) => x.videoId);
      if (drop.length) await deleteJobs(penv, drop);
      await addLog(penv, "worker", `✓ פתוח בנטפרי: ${j.title}`);
      wake();
    } else if (mates.some((x) => x.status === "nfcheck")) {
      await deleteJobs(penv, [j.videoId]);              // try the next search result
      await addLog(penv, "worker", `חסום בנטפרי, בודק תוצאה אחרת: ${j.title}`);
    } else {
      Object.assign(j, { status: "blocked", error: String(b.detail || "הסרטון לא פתוח בנטפרי").slice(0, 200), updated: now });
      await putJSON(penv, "job:" + j.videoId, j);
      await recordActivity(penv, j);
      await addLog(penv, "worker", `⛔ חסום בנטפרי: ${j.title}`);
    }
    return json({ ok: true });
  }
  if (p === "/activity" && pid === "main") {
    const profiles = [];
    for (const q of await profileIds(rootEnv)) {
      const info = await profileInfo(rootEnv, q);
      profiles.push({ id: q, name: info.name, netfree: info.netfree, vault: info.vault, ...countJobs(await listJobs(profileEnv(rootEnv, q))) });
    }
    return json({ items: await getJSON(rootEnv, "activity", []), profiles });
  }
  // --- downloads done on the profile's own computer (its desktop app) ---
  // YouTube blocks cloud addresses; a home computer isn't blocked, and with
  // NetFree on, a download that succeeds is already a NetFree-approved one.
  if (p === "/pc/next" && req.method === "POST") {
    if (!(await profileInfo(env, pid)).direct) return json({ jobs: [] });
    const now = Date.now();
    const cand = (await listJobs(env)).reverse().sort((a, b) => (b.priority || 0) - (a.priority || 0));
    for (const j of cand) {
      if (j.kind || j.fallback) continue;   // lists/searches go to the server; so do failed computer jobs
      const stale = j.status === "taken" && now - Date.parse(j.updated) > CLAIM_TIMEOUT_MS;
      if (j.status !== "queued" && !stale) continue;
      j.status = "taken"; j.updated = new Date().toISOString(); j.via = "pc";
      j.pcConvert = j.mode === "audio";
      await putJSON(env, "job:" + j.videoId, j);
      return json({ jobs: [j] });           // one at a time: the rest stay in line
    }
    return json({ jobs: [] });
  }
  if (p === "/pc/done" && req.method === "POST") {
    const b = await req.json();
    const j = await getJSON(env, "job:" + b.videoId, null);
    if (!j) return json({ ok: true, ignored: true });
    if (j.status === "cancelled") return json({ ok: true, ignored: "cancelled" });
    const ok = b.status === "done";
    const pi = await profileInfo(env, env.PROFILE);
    // Unrestricted profile: the computer couldn't get it → the server tries.
    // NetFree profile: no fallback — the server would skip the NetFree check.
    const fallback = !ok && pi.direct && !pi.netfree && !j.kind;
    const why = String(b.error || "").slice(0, 300);
    Object.assign(j, { status: ok ? "done" : fallback ? "queued" : "failed", fileUrl: "", pcGot: ok, via: "pc",
                       fallback, error: ok ? "" : (fallback ? "במחשב נכשל, עובר לשרת: " : "") + why,
                       updated: new Date().toISOString() });
    await putJSON(env, "job:" + j.videoId, j);
    await recordActivity(env, j);
    await addLog(env, "pc", (ok ? "✓ ירד למחשב: " : "✗ נכשל במחשב: ") + j.title + (ok ? "" : " — " + j.error));
    return json({ ok: true });
  }
  if (p === "/profiles/remove" && req.method === "POST" && pid === "main") {
    // Removes the profile and all of its data.
    const f = await req.formData(), id = String(f.get("id") || "");
    const profs = await getJSON(rootEnv, "profiles", {});
    if (profs[id]) {
      const name = profs[id].name;
      delete profs[id];
      await putJSON(rootEnv, "profiles", profs);
      let cursor;
      do {
        const r = await rootEnv.DB.list({ prefix: "p:" + id + ":", cursor });
        for (const k of r.keys) await rootEnv.DB.delete(k.name);
        cursor = r.list_complete ? null : r.cursor;
      } while (cursor);
      await addLog(env, "worker", `נמחק פרופיל: ${name}`);
    }
    return json({ ok: true });
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
  if (p === "/settings" && req.method === "POST") {
    const b = await req.json();
    const settings = { ...(await getJSON(env, "settings", {})), pcConvert: !!b.pcConvert };
    await putJSON(env, "settings", settings);
    await addLog(env, "worker", "המרה ל-MP3 " + (settings.pcConvert ? "במחשב (בתוכנה)" : "בשרת"));
    return json(settings);
  }
  if (p === "/clientlog" && req.method === "POST") {
    // The desktop app reports what it downloaded / why it couldn't.
    const b = await req.json();
    await addLogs(env, "pc", (b.lines || []).slice(-50).map(String));
    for (const id of (b.got || []).slice(0, 100)) {      // files now on the PC
      const j = await getJSON(env, "job:" + id, null);
      if (j && !j.pcGot) { j.pcGot = true; await putJSON(env, "job:" + id, j); await recordActivity(env, j, { pcGot: true }); }
    }
    return json({ ok: true });
  }
  if (p === "/jobs/clear" && req.method === "POST") {
    // "Clear history": everything finished (done / failed / skipped / cancelled).
    const done = (await listJobs(env)).filter((j) => ["done", "failed", "skipped", "cancelled", "blocked"].includes(j.status));
    await deleteJobs(env, done.map((j) => j.videoId));
    await addLog(env, "worker", `ההיסטוריה נוקתה: ${done.length} פריטים`);
    return json({ deleted: done.map((j) => j.videoId) });
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
      if (j) {
        // NetFree-only profile: back to the check right away (not via the download server).
        const ri = await profileInfo(env, env.PROFILE);
        j.status = !j.kind && j.nf !== "ok" && ri.netfree && !ri.direct ? "nfcheck" : "queued";
        j.error = ""; j.updated = new Date().toISOString(); await putJSON(env, "job:" + j.videoId, j);
        if (j.status === "queued") wake();
      }
      return redirect(BASE + "/");
    }
    if (p === "/jobs/batch") {
      // "Download N videos from a channel": one 'list' job; the agent lists
      // the channel with yt-dlp (any number — RSS only shows the last 15)
      // and sends the videos back to /api/enqueue as regular jobs.
      const id = await resolveChannelId(String(f.get("channel") || ""));
      const { name } = await readFeed(id).catch(() => ({ name: id }));
      const count = Math.max(1, Math.min(300, parseInt(f.get("count") || "10", 10) || 10));
      const skipShorts = f.get("skipShorts") === "on";
      const lid = "list-" + id + "-" + Date.now().toString(36);
      await addJobs(env, [{
        videoId: lid, kind: "list", channelId: id, channelName: name,
        title: `${count} סרטונים אחרונים מ-${name}`,
        // the "videos" tab has no Shorts; the uploads playlist (UU…) has everything
        url: skipShorts ? `https://www.youtube.com/channel/${id}/videos` : `https://www.youtube.com/playlist?list=UU${id.slice(2)}`,
        count, mode: modeOf(f.get("mode")), minMinutes: Math.max(0, parseInt(f.get("minMinutes") || "0", 10) || 0),
        status: "queued", priority: Date.now(), updated: new Date().toISOString() }]);
      await addLog(env, "worker", `בקשה: ${count} סרטונים מ-${name}`);
      wake();
      return redirect(BASE + "/");
    }
    if (p === "/jobs/list") {
      // A pasted list, one item per line: a video link, a playlist/channel
      // link, or a name to search on YouTube ("singer - song").
      const mode = modeOf(f.get("mode"));
      const minMinutes = Math.max(0, parseInt(f.get("minMinutes") || "0", 10) || 0);
      const lines = String(f.get("lines") || "").split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).slice(0, 300);
      const now = Date.now(), jobs = [];
      lines.forEach((line, i) => {
        const base = { mode, minMinutes, status: "queued", updated: new Date().toISOString(), priority: now - i };
        const v = line.match(/(?:v=|youtu\.be\/|shorts\/|live\/)([\w-]{11})/);
        const pl = line.match(/[?&]list=([\w-]+)/);
        if (v && !pl) {
          jobs.push({ ...base, videoId: v[1], channelName: "רשימה", title: v[1], url: "https://www.youtube.com/watch?v=" + v[1] });
        } else if (/^https?:\/\//.test(line)) {
          // playlist / channel link: expand it like "N videos from a channel"
          jobs.push({ ...base, kind: "list", videoId: "list-" + (now + i).toString(36), channelName: "רשימה",
                      title: "כל הסרטונים מ: " + line, url: line, count: 300 });
        } else {
          jobs.push({ ...base, kind: "search", videoId: "q-" + (now + i).toString(36), channelName: "חיפוש",
                      title: line, query: line });
        }
      });
      await addJobs(env, jobs);
      await addLog(env, "worker", `רשימה: ${jobs.length} פריטים`);
      if (jobs.length) wake();
      return redirect(BASE + "/");
    }
    if (p === "/jobs/delete") {
      // One or more ids (comma separated). A job being downloaded right now
      // is stopped by the agent within seconds (its check finds it missing).
      const ids = String(f.get("id") || "").split(",").map((x) => x.trim()).filter(Boolean);
      await deleteJobs(env, ids);
      await addLog(env, "worker", `נמחקו מההיסטוריה: ${ids.length}`);
      return redirect(BASE + "/");
    }
    if (p === "/jobs/pause") {
      // Waiting job: just set aside. Downloading job: the agent sees this
      // within seconds, stops, and keeps the partial file to resume later.
      const j = await getJSON(env, "job:" + f.get("id"), null);
      if (j && (j.status === "queued" || j.status === "taken")) {
        j.status = "paused"; j.updated = new Date().toISOString();
        await putJSON(env, "job:" + j.videoId, j);
        await addLog(env, "worker", `⏸ הושהה: ${j.title}`);
      }
      return redirect(BASE + "/");
    }
    if (p === "/jobs/resume") {
      const j = await getJSON(env, "job:" + f.get("id"), null);
      if (j && j.status === "paused") {
        j.status = "queued"; j.updated = new Date().toISOString();
        await putJSON(env, "job:" + j.videoId, j);
        await addLog(env, "worker", `▶ ממשיך: ${j.title}`);
        wake();
      }
      return redirect(BASE + "/");
    }
    if (p === "/jobs/priority") {
      // Download this one next. Whatever is downloading now steps back into
      // the queue (keeping its partial file) and continues right after.
      const id = String(f.get("id"));
      const jobs = await listJobs(env);
      const j = jobs.find((x) => x.videoId === id);
      if (j && ["queued", "paused", "failed", "cancelled", "skipped"].includes(j.status)) {
        for (const other of jobs.filter((x) => x.status === "taken")) {
          other.status = "queued"; other.updated = new Date().toISOString();
          await putJSON(env, "job:" + other.videoId, other);
        }
        j.status = "queued"; j.error = ""; j.priority = Date.now(); j.updated = new Date().toISOString();
        await putJSON(env, "job:" + j.videoId, j);
        await addLog(env, "worker", `⏫ הורד עכשיו: ${j.title}`);
        wake();
      }
      return redirect(BASE + "/");
    }
    if (p === "/jobs/cancel") {
      const j = await getJSON(env, "job:" + f.get("id"), null);
      if (j && ["queued", "taken", "paused", "nfcheck"].includes(j.status)) {
        const was = j.status;
        j.status = "cancelled"; j.error = ""; j.updated = new Date().toISOString();
        await putJSON(env, "job:" + j.videoId, j);
        await addLog(env, "worker", `בוטל: ${j.title}` + (was === "taken" ? " (השרת יעצור תוך כמה שניות)" : ""));
      }
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

  if (p === "/status") {
    if (url.searchParams.get("client") === "pc") {
      // Heartbeat from the desktop app (written at most every 3 minutes to
      // stay inside the free KV write quota).
      const pc = await getJSON(env, "pc", {});
      if (!pc.seen || Date.now() - Date.parse(pc.seen) > 600000) {
        await putJSON(env, "pc", { seen: new Date().toISOString() });
      }
    }
    return json(await state(env));
  }
  if (p === "/") return page("מעקב ערוצים", await dashboard(env));
  return new Response("Not found", { status: 404 });
}

// Download kinds. "m4a" keeps YouTube's own audio (no MP3 conversion), which
// is many times faster on a small server.
// MP3 is only made by the desktop app (it converts in seconds; the free
// server would take many minutes), so it's offered only while the app runs.
// video720 / video480: lower quality — a smaller file, so it downloads faster.
const MODES = { video: "סרטון · MP4", video720: "סרטון · 720p (מהיר יותר)", video480: "סרטון · 480p (הכי מהיר)",
                m4a: "שיר מהיר · M4A", audio: "שיר · MP3 (המרה בתוכנה)" };
const modeOf = (v) => (v in MODES ? v : "video");

const STATUS = { queued: "ממתין", taken: "בהורדה", done: "הושלם", failed: "נכשל", skipped: "דולג", cancelled: "בוטל", paused: "מושהה",
                 nfcheck: "ממתין לבדיקת נטפרי", blocked: "חסום בנטפרי" };

// Everything the page shows — also polled by the page every few seconds.
async function state(env) {
  const [channels, jobs, logs, agent, lastCheck, settings, pc] = await Promise.all([
    getChannels(env), listJobs(env), getJSON(env, "logs", []),
    getJSON(env, "agent", {}), getJSON(env, "lastCheck", null), getJSON(env, "settings", {}), getJSON(env, "pc", {})]);
  const profs = await getJSON(env, "profiles", {});
  const pid = env.PROFILE || "main";
  const profile = await profileInfo(env, pid);
  const profiles = profile.admin ? [profile, ...Object.entries(profs).map(([id, v]) => ({ id, name: v.name, created: v.created,
    netfree: !!v.netfree, vault: !!v.vault, direct: !!v.direct }))] : [];
  let cookieExt = null;
  if (profile.admin) {
    const c = await getJSON(env, "ytcookies", null);
    cookieExt = { key: await cookiePushKey(env), at: c && c.at, count: c && c.count };
  }
  return { channels, jobs: jobs.slice(0, 200), logs: logs.slice(-LOG_CAP), agent, lastCheck, settings, pc, profile, profiles, cookieExt,
           renderUrl: env.RENDER_URL || "", now: new Date().toISOString() };
}

// ---------- HTML ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Line icons (24×24, stroke) — drawn inline so nothing is loaded from outside.
const IC = {
  queue: '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
  channels: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M17 2l-5 5-5-5"/>',
  add: '<path d="M12 5v14M5 12h14"/>',
  logs: '<path d="M4 17l6-6-6-6M12 19h8"/>',
  settings: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<path d="M6 4l14 8-14 8z"/>',
  x: '<path d="M18 6L6 18M6 6l12 12"/>',
  retry: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5"/>',
  now: '<path d="M17 11l-5-5-5 5M17 18l-5-5-5 5"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/>',
  zap: '<path d="M13 2L3 14h9l-1 8 10-12h-9z"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  trash: '<path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6M9 6V4h6v2"/>',
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  video: '<path d="M23 7l-7 5 7 5z"/><rect x="1" y="5" width="15" height="14" rx="2"/>',
  layers: '<path d="M12 2L2 7l10 5 10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  server: '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01M6 17h.01"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  link: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>',
  users: '<circle cx="9" cy="8" r="4"/><path d="M1 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1M16 4a4 4 0 0 1 0 8M23 21v-1a6 6 0 0 0-4-5.6"/>',
  dash: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="M21 2l-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
  edit: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
};
const icon = (n, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[n] || ""}</svg>`;

// The app icon (same as desktop/icon.svg), used as the browser-tab icon.
const ICON = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'%3E %3Cdefs%3E %3ClinearGradient id='bg' x1='0' y1='0' x2='1' y2='1'%3E %3Cstop offset='0' stop-color='%238ab4f8'/%3E %3Cstop offset='1' stop-color='%233367d6'/%3E %3C/linearGradient%3E %3ClinearGradient id='badge' x1='0' y1='0' x2='0' y2='1'%3E %3Cstop offset='0' stop-color='%23ff6b5e'/%3E %3Cstop offset='1' stop-color='%23e2382b'/%3E %3C/linearGradient%3E %3C/defs%3E %3Crect x='8' y='8' width='240' height='240' rx='56' fill='url%28%23bg%29'/%3E %3Cpath d='M128 52 v92' stroke='%23fff' stroke-width='26' stroke-linecap='round'/%3E %3Cpath d='M84 104 L128 148 L172 104' fill='none' stroke='%23fff' stroke-width='26' stroke-linecap='round' stroke-linejoin='round'/%3E %3Cpath d='M58 156 v26 a14 14 0 0 0 14 14 h112 a14 14 0 0 0 14 -14 v-26' fill='none' stroke='%23fff' stroke-width='22' stroke-linecap='round' stroke-linejoin='round'/%3E %3Ccircle cx='196' cy='62' r='40' fill='url%28%23badge%29' stroke='%23fff' stroke-width='8'/%3E %3Cpath d='M184 44 L216 62 L184 80 Z' fill='%23fff' stroke='%23fff' stroke-width='6' stroke-linejoin='round'/%3E %3C/svg%3E";

const CSS = `
:root{--bg:#0e1015;--side:#12151b;--panel:#171a21;--panel2:#1e222b;--line:#262b36;--text:#e7e9ee;--muted:#8a92a3;
--primary:#4f8cff;--primary2:#3b74e0;--ok:#2fbf87;--warn:#f0b43c;--bad:#f06464;--radius:12px;--shadow:0 1px 2px #0004,0 4px 16px #0003}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font-family:Rubik,"Segoe UI",system-ui,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14.5px;-webkit-font-smoothing:antialiased}
a{color:var(--primary);text-decoration:none}
.i{width:18px;height:18px;flex:none;vertical-align:middle}
.app{display:flex;min-height:100vh}
/* sidebar */
.side{width:232px;flex:none;background:var(--side);border-inline-end:1px solid var(--line);padding:18px 12px;display:flex;flex-direction:column;gap:4px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;padding:4px 10px 18px;font-weight:700;font-size:1.05rem}
.brand img{width:30px;height:30px}
.nav{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:10px;color:var(--muted);cursor:pointer;border:0;background:none;font:inherit;width:100%;text-align:start}
.nav:hover{background:var(--panel);color:var(--text)}
.nav.on{background:#4f8cff1f;color:var(--primary);font-weight:600}
.nav .count{margin-inline-start:auto;background:var(--panel2);color:var(--text);border-radius:999px;padding:1px 8px;font-size:.75rem}
.side .grow{flex:1}
/* main */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;gap:12px;padding:16px 28px;border-bottom:1px solid var(--line);background:var(--bg);position:sticky;top:0;z-index:5;flex-wrap:wrap}
.topbar h1{margin:0;font-size:1.25rem;font-weight:600;flex:1}
.content{padding:24px 28px;max-width:1100px;width:100%}
.status{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:.85rem;background:var(--panel)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.dot.ok{background:var(--ok);box-shadow:0 0 0 3px #2fbf8726}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
/* buttons */
.btn{display:inline-flex;align-items:center;gap:8px;font:inherit;font-weight:500;border:1px solid transparent;border-radius:10px;padding:8px 14px;cursor:pointer;background:var(--primary);color:#fff;transition:background .15s,border-color .15s}
.btn:hover{background:var(--primary2)}.btn:disabled{opacity:.5;cursor:wait}
.btn.ghost{background:var(--panel);border-color:var(--line);color:var(--text)}.btn.ghost:hover{border-color:#3a4150;background:var(--panel2)}
.ibtn{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:9px;border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer;transition:all .15s}
.ibtn:hover{color:var(--text);border-color:#3a4150;background:var(--panel2)}
.ibtn.primary{color:var(--primary)}.ibtn.danger:hover{color:var(--bad);border-color:#f0646466}.ibtn.go{background:var(--primary);border-color:var(--primary);color:#fff}
.ibtn .i{width:16px;height:16px}
/* cards & forms */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h2{margin:0 0 4px;font-size:1rem;font-weight:600;display:flex;align-items:center;gap:8px}
.card .sub{color:var(--muted);font-size:.85rem;margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.field{display:flex;flex-direction:column;gap:6px;font-size:.82rem;color:var(--muted)}
.field.wide{grid-column:1/-1}
textarea{resize:vertical;min-height:110px;line-height:1.6}
input,select,textarea{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:9px 11px;width:100%}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px #4f8cff26}
.check{display:flex;align-items:center;gap:8px;color:var(--text);font-size:.9rem;cursor:pointer}
.check input{width:auto}
.actions-row{display:flex;justify-content:flex-end;margin-top:16px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
/* segmented filter */
.seg{display:inline-flex;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px;margin-bottom:14px}
.seg button{border:0;background:none;color:var(--muted);font:inherit;padding:6px 14px;border-radius:7px;cursor:pointer}
.seg button.on{background:var(--panel2);color:var(--text);font-weight:500}
/* queue */
.list{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
.row{display:flex;gap:14px;align-items:flex-start;padding:14px 16px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}.row:hover{background:#ffffff05}
.thumb{width:112px;height:63px;border-radius:8px;object-fit:cover;background:var(--panel2);flex:none;display:grid;place-items:center;color:var(--muted)}
.thumb .i{width:24px;height:24px}
.info{flex:1;min-width:0}
.title{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:var(--muted);font-size:.8rem;margin-top:4px}
.meta .i{width:13px;height:13px}
.sep{opacity:.4}
.note{font-size:.8rem;margin-top:6px;color:var(--muted);word-break:break-word}.note.bad{color:var(--bad)}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:.76rem;font-weight:500;padding:3px 10px;border-radius:999px;white-space:nowrap;background:var(--panel2);color:var(--muted)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.b-done{color:var(--ok);background:#2fbf871a}.b-failed{color:var(--bad);background:#f064641a}.b-taken{color:var(--primary);background:#4f8cff1f}
.b-queued{color:var(--warn);background:#f0b43c1a}.b-paused{color:#a78bfa;background:#a78bfa1f}
.b-nfcheck{color:#38bdf8;background:#38bdf81a}.b-blocked{color:var(--bad);background:#f064641a}
.pflags{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:.8rem;color:var(--muted)}.pflags label{display:flex;align-items:center;gap:6px}
.side-col{display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex:none}
/* dashboards */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow);cursor:pointer;text-align:start;font:inherit;color:var(--text)}
.stat:hover{border-color:var(--primary)}
.stat .k{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.85rem}.stat .k .i{width:16px;height:16px}
.stat .v{font-size:2rem;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
.stat .s{color:var(--muted);font-size:.78rem;margin-top:2px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}.dgrid .card{margin:0}
.mini{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}.mini:last-child{border-bottom:0}
.mini .pos{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:var(--panel2);color:var(--muted);font-size:.8rem;font-variant-numeric:tabular-nums}
.mini .t{flex:1;min-width:0}.mini .t b{display:block;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mini .t span{color:var(--muted);font-size:.78rem}
.mini .t .bad{color:var(--bad)}
.ghead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:16px 18px;border-bottom:1px solid var(--line)}
.prow{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}.prow:last-child{border-bottom:0}
.prow .who{display:flex;align-items:center;gap:10px;min-width:150px;flex:1}
.prow .nums{display:flex;gap:18px;flex-wrap:wrap}.prow .nums div{text-align:center;min-width:56px}
.prow .nums b{display:block;font-size:1.15rem;font-variant-numeric:tabular-nums}.prow .nums span{color:var(--muted);font-size:.74rem}
.vnotes{padding:4px 0 8px}.vnotes details{border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-top:10px;background:var(--bg)}
.vnotes summary{cursor:pointer;font-weight:500}.vnotes h4{margin:14px 0 6px;font-size:.9rem}.vnotes ul{margin:6px 0;padding-inline-start:20px;color:var(--muted)}.vnotes li{margin:3px 0}
.vok{color:var(--ok)}.vnew{color:var(--warn)}
.tag{font-size:.7rem;padding:2px 8px;border-radius:999px;background:var(--panel2);color:var(--muted);margin-inline-start:6px}
.acts{display:flex;gap:6px}
.empty{padding:48px 16px;text-align:center;color:var(--muted)}
.empty .i{width:36px;height:36px;margin-bottom:8px;opacity:.6}
/* progress */
.prog{margin-top:10px;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.steps{display:flex;gap:4px;margin-bottom:10px}
.step{flex:1;font-size:.72rem;color:var(--muted);text-align:center;padding-top:6px;border-top:3px solid var(--line)}
.step.done{border-color:var(--ok);color:var(--ok)}.step.now{border-color:var(--primary);color:var(--text);font-weight:600}
.pline{display:flex;justify-content:space-between;font-size:.8rem;margin:6px 0 4px}
.pline b{font-weight:600}
.bar{height:8px;border-radius:999px;background:var(--panel2);overflow:hidden}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--primary),#7aa8ff);border-radius:999px;transition:width .6s}
.bar.thin{height:4px}.bar.busy>i{animation:pulse 1s ease-in-out infinite alternate}
@keyframes pulse{from{opacity:.35}to{opacity:1}}
.nums{font-size:.75rem;color:var(--muted);margin-top:6px;direction:ltr;text-align:right;font-variant-numeric:tabular-nums}
/* channels */
.ch{display:flex;align-items:center;gap:14px;padding:14px 16px;border-bottom:1px solid var(--line)}
.ch:last-child{border-bottom:0}
.avatar{width:40px;height:40px;border-radius:50%;background:var(--panel2);display:grid;place-items:center;color:var(--primary);font-weight:700;flex:none}
/* logs */
.term{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:.8rem;background:#0a0c10;border:1px solid var(--line);border-radius:10px;padding:12px;height:62vh;overflow:auto;direction:ltr;text-align:left}
.term .l{padding:3px 0;border-bottom:1px solid #ffffff08;white-space:pre-wrap;word-break:break-word}
.term .t{color:#5d6576}.term .src{display:inline-block;min-width:62px}.term .s-worker{color:var(--ok)}.term .s-render{color:var(--primary)}.term .s-pc{color:var(--warn)}
.term .m{unicode-bidi:isolate}.term .bad{color:var(--bad)}
/* settings */
.setting{display:flex;align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid var(--line)}
.setting:last-child{border-bottom:0}
.setting .txt{flex:1}.setting .txt b{display:block;font-weight:500;margin-bottom:2px}.setting .txt span{color:var(--muted);font-size:.83rem}
.switch{position:relative;width:42px;height:24px;flex:none}
.switch input{opacity:0;width:0;height:0}
.switch i{position:absolute;inset:0;background:var(--panel2);border:1px solid var(--line);border-radius:999px;cursor:pointer;transition:.2s}
.switch i::before{content:"";position:absolute;width:18px;height:18px;top:2px;right:2px;background:var(--muted);border-radius:50%;transition:.2s}
.switch input:checked+i{background:var(--primary);border-color:var(--primary)}
.switch input:checked+i::before{background:#fff;transform:translateX(-18px)}
/* toast & login */
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);box-shadow:var(--shadow);padding:10px 18px;border-radius:10px;display:none;max-width:90%;z-index:50}
.login{max-width:380px;margin:14vh auto;text-align:center}
.login img{width:56px;height:56px;margin-bottom:10px}
.login input{margin:16px 0 12px}.login .btn{width:100%;justify-content:center}
.hidden{display:none!important}
/* mobile: sidebar becomes a bottom bar */
@media (max-width:760px){
  .side{position:fixed;bottom:0;top:auto;height:auto;width:100%;flex-direction:row;padding:6px;border:0;border-top:1px solid var(--line);z-index:20;justify-content:space-around}
  .brand,.side .grow,.side .extra{display:none}
  .nav{flex-direction:column;gap:3px;font-size:.68rem;padding:6px 4px;width:auto;flex:1;align-items:center}
  .nav .count{display:none}
  .content{padding:16px 14px 90px}.topbar{padding:12px 14px}
  .thumb{width:84px;height:47px}
  .row{flex-wrap:wrap}.side-col{flex-direction:row;align-items:center;width:100%;justify-content:space-between;padding-inline-start:98px}
}
`;

function page(title, body) {
  return new Response(`<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title>
<link rel="icon" href="${ICON}">
<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>${CSS}</style></head><body>${body}</body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

// "Who's signing in?" — a tile per profile, then that profile's code.
async function loginForm(env, msg, pick) {
  const tiles = [];
  for (const id of await profileIds(env)) tiles.push(await profileInfo(env, id));
  const tile = (t) => `<button type="button" class="tile" data-pid="${esc(t.id)}" data-name="${esc(t.name)}">
<span class="avatar">${esc(t.name.charAt(0))}</span><span>${esc(t.name)}</span></button>`;
  return `<style>.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px;margin:14px 0}
.tile{display:flex;flex-direction:column;align-items:center;gap:8px;padding:16px 8px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font:inherit;cursor:pointer}
.tile:hover{border-color:var(--primary)}.tile .avatar{width:44px;height:44px;font-size:1.2rem}</style>
<div class="card login">
<img src="${ICON}" alt=""><h2 style="justify-content:center">מעקב ערוצים</h2>
<div id="pickBox"${pick ? ' class="hidden"' : ""}><p class="sub">מי נכנס?</p><div class="tiles">${tiles.map(tile).join("")}</div></div>
<form method="post" action="${BASE}/login" id="lf"${pick ? "" : ' class="hidden"'}>
<input type="hidden" name="pid" value="${esc(pick || "")}">
<p class="sub" id="who">${pick ? esc((tiles.find((t) => t.id === pick) || {}).name || "") : ""}</p>
${msg ? `<p style="color:var(--bad);margin:0 0 8px">${esc(msg)}</p>` : ""}
<input type="password" name="password" placeholder="קוד" autocomplete="off">
<button class="btn" style="margin-top:10px">כניסה</button>
<p style="margin:12px 0 0"><a href="#" id="back">החלף פרופיל</a></p></form>
<p style="margin:16px 0 0"><a href="/">${icon("chat")} מעבר ל-Gemini</a></p></div>
<script>
const lf = document.getElementById("lf");
document.querySelectorAll(".tile").forEach((t) => t.onclick = () => {
  lf.pid.value = t.dataset.pid; document.getElementById("who").textContent = t.dataset.name;
  document.getElementById("pickBox").classList.add("hidden"); lf.classList.remove("hidden"); lf.password.value = ""; lf.password.focus();
});
document.getElementById("back").onclick = (e) => { e.preventDefault(); lf.classList.add("hidden"); document.getElementById("pickBox").classList.remove("hidden"); };
if (lf.pid.value) lf.password.focus();
lf.onsubmit = async (e) => {
  // Inside the desktop app: it remembers this profile (encrypted, for this
  // Windows user) so its downloads keep running after the window closes.
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.remember || lf.dataset.ok) return;
  e.preventDefault();
  try { await api.remember(lf.pid.value, lf.password.value); } catch (x) {}
  lf.dataset.ok = "1"; lf.submit();
};
</script>`;
}

async function dashboard(env) {
  const initial = JSON.stringify(await state(env)).replace(/</g, "\\u003c");
  const modeOptions = Object.entries(MODES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  return `<div class="app">
<aside class="side">
  <div class="brand"><img src="${ICON}" alt="">מעקב ערוצים</div>
  <button class="nav on" data-page="dash">${icon("dash")}<span>לוח בקרה</span></button>
  <button class="nav hidden" data-page="global" id="globalNav">${icon("users")}<span>לוח בקרה כללי</span></button>
  <button class="nav" data-page="queue">${icon("queue")}<span>תור הורדות</span><span class="count" id="qCount"></span></button>
  <button class="nav" data-page="channels">${icon("channels")}<span>ערוצים</span><span class="count" id="cCount"></span></button>
  <button class="nav" data-page="add">${icon("add")}<span>הוספה</span></button>
  <button class="nav" data-page="logs">${icon("logs")}<span>לוגים</span></button>
  <button class="nav" data-page="settings">${icon("settings")}<span>הגדרות</span></button>
  <button class="nav hidden" id="vaultNav" type="button">${icon("lock")}<span>כספת</span></button>
  <div class="grow"></div>
  <a class="nav extra" href="/">${icon("chat")}<span>Gemini</span></a>
</aside>
<main class="main">
  <div class="topbar">
    <h1 id="pageTitle">לוח בקרה</h1>
    <span class="status"><span class="dot" id="agentDot"></span><span id="agentText">…</span></span>
    <button class="btn ghost" id="wakeBtn" type="button">${icon("zap")}<span>הער שרת</span></button>
    <span class="status" title="מחובר כ">${icon("user")}<span id="profileName"></span></span>
    <a class="ibtn" href="${BASE}/logout" title="התנתק">${icon("logout")}</a>
  </div>
  <div class="content">

  <section data-p="dash">
    <div class="stats" id="dashStats"></div>
    <div class="dgrid">
      <div class="card"><h2>${icon("download")}בהורדה עכשיו</h2><div id="dashNow"></div></div>
      <div class="card"><h2>${icon("queue")}ממתינות בתור</h2><div id="dashWait"></div></div>
      <div class="card"><h2>${icon("check")}ירדו לאחרונה</h2><div id="dashDone"></div></div>
      <div class="card"><h2>${icon("alert")}לא ירדו</h2><div id="dashBad"></div></div>
    </div>
  </section>

  <section data-p="global" class="hidden">
    <p class="sub" style="margin-top:0">כל מה שירד בכל הפרופילים. מחיקת היסטוריה של פרופיל לא מוחקת מכאן — רק מחיקה של ההיסטוריה שלך (המנהל) חלה גם כאן.</p>
    <div class="card" style="padding:0"><div id="gProfiles"></div></div>
    <div class="card" style="padding:0">
      <div class="ghead"><h2 style="margin:0">${icon("clock")}פעילות אחרונה</h2>
        <span style="display:flex;gap:8px;flex-wrap:wrap"><select id="gWho" style="width:auto"><option value="">כל הפרופילים</option></select>
        <select id="gWhat" style="width:auto"><option value="">כל הסטטוסים</option><option value="done">ירדו</option><option value="problem">לא ירדו</option></select></span></div>
      <div id="gList"></div>
    </div>
  </section>

  <section data-p="queue" class="hidden">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
      <div class="seg" id="filter">
        <button class="on" data-f="all">הכל</button><button data-f="active">פעילים</button>
        <button data-f="done">הושלמו</button><button data-f="problem">בעיות</button>
      </div>
      <button class="btn ghost" id="clearHistory" type="button">${icon("trash")}<span>נקה היסטוריה</span></button>
    </div>
    <div class="list" id="jobs"></div>
  </section>

  <section data-p="channels" class="hidden">
    <div class="list" id="channels"></div>
    <div class="actions-row" style="justify-content:space-between;align-items:center">
      <span class="sub" id="checkText" style="margin:0"></span>
      <button class="btn ghost" id="checkBtn" type="button">${icon("refresh")}<span>בדוק עכשיו</span></button>
    </div>
  </section>

  <section data-p="add" class="hidden">
    <div class="cols">
      <form class="card" data-action="/jobs/add">
        <h2>${icon("download")}סרטון בודד</h2><p class="sub">קישור לסרטון ביוטיוב — יורד מיד.</p>
        <div class="grid">
          <label class="field wide">קישור<input name="url" placeholder="https://www.youtube.com/watch?v=…" required></label>
          <label class="field">סוג<select name="mode">${modeOptions}</select></label>
        </div>
        <div class="actions-row"><button class="btn">${icon("add")}<span>הוסף לתור</span></button></div>
      </form>
      <form class="card" data-action="/jobs/list">
        <h2>${icon("queue")}רשימת שירים או קישורים</h2><p class="sub">שורה לכל פריט: קישור ליוטיוב, או שם לחיפוש (זמר - שיר).</p>
        <div class="grid">
          <label class="field wide">הרשימה<textarea name="lines" rows="6" required placeholder="נפתלי קמפה - גרש את העצבות&#10;https://www.youtube.com/watch?v=…&#10;מוטי וייס - רפ״ב"></textarea></label>
          <label class="field">סוג<select name="mode">${modeOptions}</select></label>
          <label class="field">אורך מינימלי (דקות)<input name="minMinutes" type="number" min="0" value="0"></label>
        </div>
        <div class="actions-row"><button class="btn">${icon("download")}<span>הורד את הרשימה</span></button></div>
      </form>
      <form class="card" data-action="/jobs/batch">
        <h2>${icon("layers")}כמות סרטונים מערוץ</h2><p class="sub">מוריד את הסרטונים האחרונים של ערוץ — כמה שתבחר.</p>
        <div class="grid">
          <label class="field wide">ערוץ<input name="channel" placeholder="קישור לערוץ / ‎@handle" required></label>
          <label class="field">כמה סרטונים<input name="count" type="number" min="1" max="300" value="10"></label>
          <label class="field">סוג<select name="mode">${modeOptions}</select></label>
          <label class="field">אורך מינימלי (דקות)<input name="minMinutes" type="number" min="0" value="0"></label>
          <label class="check" style="align-self:end;padding-bottom:10px"><input type="checkbox" name="skipShorts" checked>בלי Shorts</label>
        </div>
        <div class="actions-row"><button class="btn">${icon("download")}<span>הורד</span></button></div>
      </form>
      <form class="card" data-action="/channels/add">
        <h2>${icon("channels")}ערוץ למעקב</h2><p class="sub">כל סרטון חדש שיעלה בערוץ יירד אוטומטית.</p>
        <div class="grid">
          <label class="field wide">ערוץ<input name="channel" placeholder="קישור לערוץ / ‎@handle / UC…" required></label>
          <label class="field">סוג<select name="mode">${modeOptions}</select></label>
          <label class="field">אורך מינימלי (דקות)<input name="minMinutes" type="number" min="0" value="0"></label>
          <label class="field">להוריד עכשיו את האחרונים<input name="backfill" type="number" min="0" max="15" value="0"></label>
          <label class="check" style="align-self:end;padding-bottom:10px"><input type="checkbox" name="skipShorts" checked>בלי Shorts</label>
        </div>
        <div class="actions-row"><button class="btn">${icon("add")}<span>הוסף ערוץ</span></button></div>
      </form>
    </div>
  </section>

  <section data-p="logs" class="hidden">
    <div class="actions-row" style="margin:0 0 12px;gap:8px;justify-content:space-between;align-items:center">
      <span class="sub" style="margin:0">worker = Cloudflare · render = שרת ההורדות · pc = התוכנה במחשב</span>
      <span style="display:flex;gap:8px"><button class="btn ghost" id="copyLogs" type="button">${icon("copy")}<span>העתק</span></button>
      <button class="btn ghost" id="clearLogs" type="button">${icon("trash")}<span>ניקוי</span></button></span>
    </div>
    <div class="term" id="logs"></div>
  </section>

  <section data-p="settings" class="hidden">
    <div class="card">
      <h2>${icon("settings")}הורדה והמרה</h2>
      <div class="setting"><div class="txt"><b>הורדה אוטומטית בדפדפן</b><span>כשהדף פתוח בדפדפן, כל קובץ שמוכן יורד לבד — רק כשהתוכנה למחשב לא פועלת, כדי שלא יירד פעמיים. <b id="pcState" style="display:inline"></b></span></div>
        <label class="switch"><input type="checkbox" id="autoDl"><i></i></label></div>
    </div>
    <div class="card hidden" id="profilesCard">
      <h2>${icon("users")}פרופילים</h2>
      <p class="sub">לכל פרופיל קוד משלו (רק המנהל קובע ומשנה אותו), ותור, ערוצים והיסטוריה נפרדים לגמרי. <b>רק פתוח בנטפרי</b> — יורד רק מה שנטפרי מאשר. <b>מוריד מהמחשב</b> — ההורדה נעשית בתוכנה במחשב של המשתמש (לא בשרת, שיוטיוב חוסם): לפרופיל לא מוגבל, אם נכשל — עובר לשרת; לפרופיל "רק פתוח בנטפרי" ההורדה עצמה היא הבדיקה. <b>כספת</b> — הקבצים נשמרים מוצפנים.</p>
      <div id="profilesList"></div>
      <form class="grid" data-action="/profiles/add" style="margin-top:12px">
        <label class="field">שם<input name="name" placeholder="למשל: אחי" required></label>
        <label class="field">קוד לפרופיל<input name="password" type="password" minlength="4" required></label>
        <label class="field" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" name="netfree" style="width:auto" checked>רק פתוח בנטפרי</label>
        <label class="field" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" name="vault" style="width:auto">כספת מוצפנת</label>
        <label class="field" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" name="direct" style="width:auto">מוריד מהמחשב (לא מהשרת)</label>
        <div class="actions-row" style="align-self:end;margin:0"><button class="btn">${icon("add")}<span>צור פרופיל</span></button></div>
      </form>
    </div>
    <div class="card hidden" id="cookieCard">
      <h2>${icon("key")}תוסף דפדפן — עוגיות יוטיוב</h2>
      <p class="sub">התוסף שולח את עוגיות יוטיוב מהדפדפן שלך (רק לאתר הזה), ושרת ההורדות משתמש תמיד בעדכניות — בלי לייצא קבצים. מתקינים פעם אחת: מורידים את <b>YTCookies-extension.zip</b> מעמוד ההורדות של התוכנה, פותחים אותו, ב-Chrome: chrome://extensions ← מצב מפתח ← "טען תוסף לא ארוז" ← בוחרים את התיקייה. בתוסף מדביקים את המפתח.</p>
      <div class="setting"><div class="txt"><b>מפתח לתוסף</b><span id="ckKey" style="direction:ltr;font-family:monospace"></span></div>
        <button class="btn ghost" id="ckCopy" type="button">${icon("copy")}<span>העתק</span></button></div>
      <div class="setting"><div class="txt"><b>עדכון אחרון מהתוסף</b><span id="ckAt"></span></div></div>
    </div>
    <div class="card hidden" id="desktopCard">
      <h2>${icon("monitor")}התוכנה למחשב</h2>
      <div class="setting"><div class="txt"><b>הפעלה עם Windows</b><span>התוכנה תעלה ברקע כשהמחשב נדלק ותמשיך להוריד גם כשהחלון סגור (אייקון ליד השעון).</span></div>
        <label class="switch"><input type="checkbox" id="autostart"><i></i></label></div>
      <div class="setting"><div class="txt"><b>התראות</b><span>לכל משתמש במחשב הבחירה שלו.</span></div>
        <select id="notifyMode" style="width:auto"><option value="each">על כל קובץ</option><option value="batch">רק בסיום משימת הורדה</option><option value="off">ללא התראות</option></select></div>
      <div class="setting"><div class="txt"><b>תיקיית הורדות</b><span id="folderPath"></span></div>
        <span style="display:flex;gap:8px"><button class="btn ghost hidden" id="pickFolder" type="button">${icon("edit")}<span>שנה</span></button>
        <button class="btn ghost" id="openFolder" type="button">${icon("folder")}<span>פתח</span></button></span></div>
      <div class="setting hidden" id="nfTestRow"><div class="txt"><b>בדיקת נטפרי</b><span>הדבק קישור לסרטון — התוכנה בודקת דרך החיבור בבית אם הוא פתוח בנטפרי. <span id="nfTestOut" style="white-space:pre-wrap"></span></span></div>
        <span style="display:flex;gap:8px"><input id="nfTestUrl" placeholder="https://youtu.be/…" style="width:200px"><button class="btn ghost" id="nfTestBtn" type="button">בדוק</button></span></div>
      <div class="setting"><div class="txt"><b>גרסה</b><span id="appVersion"></span></div>
        <button class="btn hidden" id="updateNow" type="button">${icon("download")}<span>עדכן עכשיו</span></button></div>
      <div class="vnotes hidden" id="whatsNew">
        <details open><summary id="newTitle">מה חדש בגרסה האחרונה</summary><div id="newNotes"></div></details>
        <details><summary>כל השינויים</summary><div id="changelog"></div></details>
      </div>
    </div>
    <div class="card">
      <h2>${icon("server")}שרת ההורדות</h2>
      <div class="setting"><div class="txt"><b>מצב</b><span id="agentInfo"></span></div>
        <button class="btn ghost" id="wakeBtn2" type="button">${icon("zap")}<span>הער</span></button></div>
    </div>
  </section>

  </div>
</main>
</div>
<div id="toast"></div>
<script>
const B = ${JSON.stringify(BASE)}, STATUS = ${JSON.stringify(STATUS)}, MODES = ${JSON.stringify(MODES)}, IC = ${JSON.stringify(IC)};
let S = ${initial}, P = {}, FILTER = "all", HAVE = new Set();
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = (n) => '<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (IC[n] || "") + '</svg>';
const ago = (iso) => { if (!iso) return "אף פעם"; const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return m < 1 ? "עכשיו" : m < 60 ? "לפני " + m + " דק׳" : m < 1440 ? "לפני " + Math.round(m / 60) + " שע׳" : "לפני " + Math.round(m / 1440) + " ימים"; };
const hhmm = (iso) => new Date(iso).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const mmss = (s) => Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
function toast(msg, ms) { const t = $("toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms || 3500); }
const APP = () => window.pywebview && window.pywebview.api;   // running inside the desktop app?

// ---------- pages ----------
const TITLES = { dash: "לוח בקרה", global: "לוח בקרה כללי", queue: "תור הורדות", channels: "ערוצים", add: "הוספה", logs: "לוגים", settings: "הגדרות" };
function showPage(name) {
  if (name === "global" && !(S.profile && S.profile.admin)) name = "dash";
  document.querySelectorAll(".nav[data-page]").forEach((n) => n.classList.toggle("on", n.dataset.page === name));
  document.querySelectorAll("section[data-p]").forEach((s) => s.classList.toggle("hidden", s.dataset.p !== name));
  $("pageTitle").textContent = TITLES[name];
  try { localStorage.setItem("page", name); } catch (e) {}
  if (name === "logs") { const b = $("logs"); b.scrollTop = b.scrollHeight; }
  if (name === "global") setTimeout(loadGlobal, 0);
}
document.querySelectorAll(".nav[data-page]").forEach((n) => n.onclick = () => showPage(n.dataset.page));
try { const p = localStorage.getItem("page"); if (p && TITLES[p]) showPage(p); } catch (e) {}
document.querySelectorAll("#filter button").forEach((b) => b.onclick = () => {
  FILTER = b.dataset.f; document.querySelectorAll("#filter button").forEach((x) => x.classList.toggle("on", x === b)); render(); });

// ---------- rendering ----------
const STAGES = [["start", "התחלה"], ["download", "הורדה"], ["convert", "המרה"], ["zip", "אריזה"], ["finish", "סיום"]];
function progHtml(id) {
  const p = P[id];
  if (!p) return '<div class="pline"><span>ממתין לשרת ההורדות…</span></div><div class="bar thin busy"><i style="width:100%"></i></div>';
  const at = STAGES.findIndex((s) => s[0] === p.stage);
  // A video isn't converted, only its picture and sound joined into one MP4.
  const label = (s) => s[0] === "convert" && /^video/.test(p.mode || "") ? "איחוד" : s[1];
  const steps = STAGES.map((s, i) => '<div class="step ' + (i < at ? "done" : i === at ? "now" : "") + '">' + label(s) + '</div>').join("");
  const known = typeof p.pct === "number", overall = typeof p.overall === "number" ? p.overall : 0, dl = p.stage === "download";
  const nums = [dl && p.size ? p.size : "", dl && p.speed ? p.speed : "", dl && p.eta && p.eta !== "NA" ? "ETA " + p.eta : "",
    p.part > 1 ? "part " + p.part + "/2" : "", mmss(p.elapsed || 0)].filter(Boolean).join("  ·  ");
  return '<div class="steps">' + steps + '</div>' +
    '<div class="pline"><b>התקדמות כוללת</b><b>' + overall.toFixed(0) + '%</b></div><div class="bar"><i style="width:' + overall + '%"></i></div>' +
    '<div class="pline"><span>' + label(STAGES[at] || ["", ""]) + '</span><span>' + (known ? p.pct.toFixed(1) + "%" : "עובד…") + '</span></div>' +
    '<div class="bar thin' + (known ? "" : " busy") + '"><i style="width:' + (known ? p.pct : 100) + '%"></i></div>' +
    '<div class="nums">' + esc(nums) + '</div>';
}
function btn(act, id, ic, title, cls) { return '<button class="ibtn ' + (cls || "") + '" data-act="' + act + '" data-id="' + esc(id) + '" title="' + title + '">' + icon(ic) + '</button>'; }
function jobRow(j) {
  const list = j.kind === "list" || j.kind === "search";
  const thumb = list ? '<div class="thumb">' + icon(j.kind === "search" ? "queue" : "layers") + '</div>'
    : '<img class="thumb" loading="lazy" src="https://i.ytimg.com/vi/' + esc(j.videoId) + '/mqdefault.jpg" onerror="this.style.visibility=\\'hidden\\'">';
  const modeIc = /^video/.test(j.mode || "") ? "video" : "music";
  const meta = [esc(j.channelName || "קישור ידני"), icon(modeIc) + " " + esc(MODES[j.mode] || MODES.audio),
    j.pcConvert ? icon("zap") + " המרה במחשב" : "", j.pcGot ? icon("monitor") + " במחשב" : "",
    icon("clock") + " " + ago(j.updated)].filter(Boolean).join('<span class="sep">•</span>');
  const a = [];
  if (j.status === "done" && APP() && HAVE.has(j.videoId)) a.push(btn("showfile", j.videoId, "folder", "פתח תיקייה", "primary"));
  if (j.status === "done" && j.fileUrl) a.push(APP() ? btn("pcdl", j.videoId, "download", HAVE.has(j.videoId) ? "הורד שוב למחשב" : "הורד עכשיו למחשב", HAVE.has(j.videoId) ? "" : "go")
    : '<a class="ibtn go" href="' + esc(j.fileUrl) + '" target="_blank" rel="noopener" title="הורדה">' + icon("download") + '</a>');
  if (j.status === "queued") a.push(btn("priority", j.videoId, "now", "הורד עכשיו — לפני כל השאר", "primary"));
  if (j.status === "paused") a.push(btn("resume", j.videoId, "play", "המשך", "primary"), btn("priority", j.videoId, "now", "הורד עכשיו"));
  if (j.status === "queued" || j.status === "taken") a.push(btn("pause", j.videoId, "pause", "השהה"));
  if (["failed", "skipped", "cancelled", "blocked"].includes(j.status)) a.push(btn("retry", j.videoId, "retry", j.status === "blocked" ? "בדוק שוב בנטפרי" : "נסה שוב"));
  if (["queued", "taken", "paused", "nfcheck"].includes(j.status)) a.push(btn("cancel", j.videoId, "x", "ביטול", "danger"));
  a.push(btn("delete", j.videoId, "trash", "מחק מההיסטוריה", "danger"));
  return '<div class="row">' + thumb + '<div class="info"><div class="title" title="' + esc(j.title) + '">' + esc(j.title) + '</div>' +
    '<div class="meta">' + meta + '</div>' +
    (j.error ? '<div class="note' + (["failed", "blocked"].includes(j.status) ? " bad" : "") + '">' + esc(j.error) + '</div>' : '') +
    (/confirm you.re not a bot|cookies-from-browser/i.test(j.error || "") ? '<div class="note">' + icon("alert") +
      ' יוטיוב חסם את שרת ההורדות ("תוכיח שאתה לא בוט"). צריך קובץ עוגיות חדש: מייצאים cookies.txt מחלון גלישה בסתר שמחובר ליוטיוב, ומחליפים ב-Render ← Environment ← Secret Files ← cookies.txt. אחר כך "נסה שוב".</div>' : '') +
    (j.status === "nfcheck" ? '<div class="note">התוכנה במחשב בודקת שהסרטון פתוח בנטפרי, ורק אז הוא יורד (אם התוכנה סגורה — ממתין לה).</div>' : '') +
    (j.status === "taken" ? '<div class="prog" data-prog="' + esc(j.videoId) + '">' + progHtml(j.videoId) + '</div>' : '') + '</div>' +
    '<div class="side-col"><span class="badge b-' + esc(j.status) + '">' + (STATUS[j.status] || esc(j.status)) + '</span>' +
    '<div class="acts">' + a.join("") + '</div></div></div>';
}
const IN = { all: () => true, active: (j) => ["queued", "taken", "paused", "nfcheck"].includes(j.status), done: (j) => j.status === "done",
             problem: (j) => ["failed", "skipped", "cancelled", "blocked"].includes(j.status) };
// ---------- dashboard (this profile) ----------
const WAITING = ["queued", "nfcheck", "paused"], PROBLEM = ["failed", "blocked", "skipped"];
function stat(ic, label, value, sub, go) {
  return '<button class="stat" type="button" data-go="' + go + '"><div class="k">' + icon(ic) + label + '</div><div class="v">' + value + '</div><div class="s">' + (sub || "&nbsp;") + '</div></button>';
}
function mini(lead, title, sub, badge) {
  return '<div class="mini">' + lead + '<div class="t"><b title="' + esc(title) + '">' + esc(title) + '</b><span>' + sub + '</span></div>' + (badge || "") + '</div>';
}
const badgeOf = (st) => '<span class="badge b-' + esc(st) + '">' + (STATUS[st] || esc(st)) + '</span>';
// The order the download server will take them (top priority, then oldest).
const waitOrder = (jobs) => jobs.filter((j) => WAITING.includes(j.status)).reverse().sort((a, b) => (b.priority || 0) - (a.priority || 0));
function renderDash() {
  const J = S.jobs.filter((j) => !j.kind), lists = S.jobs.filter((j) => j.kind && WAITING.concat("taken").includes(j.status));
  const done = J.filter((j) => j.status === "done"), now = S.jobs.filter((j) => j.status === "taken");
  const wait = waitOrder(S.jobs), bad = J.filter((j) => PROBLEM.includes(j.status));
  const pc = done.filter((j) => j.pcGot).length, today = done.filter((j) => Date.now() - Date.parse(j.updated) < 86400000).length;
  const nf = wait.filter((j) => j.status === "nfcheck").length, paused = wait.filter((j) => j.status === "paused").length;
  $("dashStats").innerHTML =
    stat("check", "ירדו", done.length, today + " ב-24 השעות האחרונות" + (pc ? " · " + pc + " במחשב" : ""), "done") +
    stat("download", "בהורדה עכשיו", now.length, now.length ? "השרת עובד" : "אין כרגע", "active") +
    stat("queue", "ממתינות", wait.length, [nf ? nf + " בבדיקת נטפרי" : "", paused ? paused + " מושהות" : "", lists.length ? lists.length + " רשימות/חיפושים" : ""].filter(Boolean).join(" · "), "active") +
    stat("alert", "לא ירדו", bad.length, bad.filter((j) => j.status === "blocked").length ? bad.filter((j) => j.status === "blocked").length + " חסומים בנטפרי" : "", "problem");
  $("dashNow").innerHTML = now.length ? now.map((j) => '<div class="mini" style="display:block"><b>' + esc(j.title) + '</b><div class="prog" data-prog="' + esc(j.videoId) + '">' + progHtml(j.videoId) + '</div></div>').join("")
    : '<div class="sub" style="margin:0">שום דבר לא יורד כרגע.</div>';
  $("dashWait").innerHTML = wait.length ? wait.slice(0, 12).map((j, i) => mini('<span class="pos">' + (i + 1) + '</span>', j.title,
      esc(j.channelName || "") + (j.status === "nfcheck" ? " · ממתין לבדיקת נטפרי" : j.status === "paused" ? " · מושהה" : " · נוסף " + ago(j.updated)), badgeOf(j.status))).join("") +
      (wait.length > 12 ? '<div class="sub" style="margin:8px 0 0">ועוד ' + (wait.length - 12) + '…</div>' : "")
    : '<div class="sub" style="margin:0">התור ריק.</div>';
  $("dashDone").innerHTML = done.length ? done.slice(0, 10).map((j) => mini("", j.title, ago(j.updated) + " · " + esc(MODES[j.mode] || "") + (j.pcGot ? " · במחשב" : ""), badgeOf("done"))).join("")
    : '<div class="sub" style="margin:0">עוד לא ירד כלום.</div>';
  $("dashBad").innerHTML = bad.length ? bad.slice(0, 10).map((j) => mini("", j.title, '<span class="bad">' + esc(j.error || STATUS[j.status]) + '</span> · ' + ago(j.updated), badgeOf(j.status))).join("")
    : '<div class="sub" style="margin:0">הכל ירד בהצלחה.</div>';
}
document.addEventListener("click", (e) => {
  const st = e.target.closest(".stat[data-go]"); if (!st) return;
  FILTER = st.dataset.go === "done" ? "done" : st.dataset.go;
  document.querySelectorAll("#filter button").forEach((x) => x.classList.toggle("on", x.dataset.f === FILTER));
  showPage("queue"); render();
});

// ---------- general dashboard (admin): every profile ----------
let G = null;
async function loadGlobal() {
  if (!(S.profile && S.profile.admin)) return;
  try { G = await (await fetch(B + "/activity", { cache: "no-store" })).json(); } catch (e) { return; }
  const sel = $("gWho"), cur = sel.value;
  sel.innerHTML = '<option value="">כל הפרופילים</option>' + G.profiles.map((p) => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join("");
  sel.value = cur; renderGlobal();
}
function renderGlobal() {
  if (!G) return;
  $("gProfiles").innerHTML = G.profiles.map((p) => '<div class="prow"><div class="who"><div class="avatar">' + esc(p.name.charAt(0)) + '</div><div><b>' + esc(p.name) + '</b>' +
    (p.netfree ? '<span class="tag">רק נטפרי</span>' : "") + (p.vault ? '<span class="tag">כספת</span>' : "") + '</div></div><div class="nums">' +
    [["done", "ירדו"], ["pc", "במחשב"], ["taken", "בהורדה"], ["waiting", "ממתינות"], ["problem", "לא ירדו"]].map(([k, l]) => '<div><b>' + p[k] + '</b><span>' + l + '</span></div>').join("") + '</div></div>').join("");
  const who = $("gWho").value, what = $("gWhat").value;
  const items = G.items.filter((a) => (!who || a.pid === who) && (!what || (what === "done" ? a.status === "done" : a.status !== "done"))).slice(0, 150);
  $("gList").innerHTML = items.length ? '<div style="padding:4px 18px">' + items.map((a) => mini('<div class="avatar" title="' + esc(a.name) + '">' + esc((a.name || "?").charAt(0)) + '</div>', a.title,
      esc(a.name) + " · " + ago(a.t) + (a.channelName ? " · " + esc(a.channelName) : "") + (a.pcGot ? " · במחשב" : "") + (a.error && a.status !== "done" ? ' · <span class="bad">' + esc(a.error) + '</span>' : ""),
      badgeOf(a.status))).join("") + '</div>' : '<div class="empty">אין עדיין פעילות</div>';
}
$("gWho").onchange = renderGlobal; $("gWhat").onchange = renderGlobal;
setInterval(() => { if (!$("globalNav").classList.contains("hidden") && !document.querySelector('section[data-p="global"]').classList.contains("hidden")) loadGlobal(); }, 15000);

function render() {
  const seen = S.agent && S.agent.seen, mins = seen ? (Date.now() - Date.parse(seen)) / 60000 : Infinity;
  const busy = S.jobs.some((j) => j.status === "taken"), info = S.agent && S.agent.info;
  $("agentDot").className = "dot " + (busy || mins < 5 ? "ok" : mins < 30 ? "warn" : "bad");
  $("agentText").textContent = !S.renderUrl ? "RENDER_URL לא מוגדר" : busy ? "מוריד" : mins < 5 ? "שרת ער" : "שרת ישן";
  $("agentInfo").textContent = "נראה " + ago(seen) + (info ? " · yt-dlp " + (info.ytdlp || "?") + " · עוגיות: " + (info.cookies ? "יש" : "אין!") +
    (info.pot !== undefined ? " · שרת טוקנים: " + (info.pot || "לא פעיל!") : "") +
    (info.warp !== undefined ? " · WARP: " + (info.warp ? "פעיל" : "לא פעיל") : "") : "");
  const active = S.jobs.filter(IN.active).length;
  $("qCount").textContent = active || ""; $("cCount").textContent = S.channels.length || "";
  $("checkText").textContent = "בדיקה אחרונה: " + ago(S.lastCheck && S.lastCheck.at) + " · אוטומטית כל 3 שעות";
  const jobs = S.jobs.filter(IN[FILTER]);
  $("jobs").innerHTML = jobs.length ? jobs.map(jobRow).join("")
    : '<div class="empty">' + icon("queue") + '<div>' + (FILTER === "all" ? "התור ריק — הוסף סרטון, ערוץ או כמות סרטונים בעמוד \\"הוספה\\"." : "אין פריטים כאן") + '</div></div>';
  $("channels").innerHTML = S.channels.length ? S.channels.map((c) =>
    '<div class="ch"><div class="avatar">' + esc((c.name || "?").trim().charAt(0)) + '</div><div class="info">' +
    '<div class="title"><a href="https://www.youtube.com/channel/' + esc(c.id) + '" target="_blank" rel="noopener">' + esc(c.name) + '</a></div>' +
    '<div class="meta">' + icon(/^video/.test(c.mode || "") ? "video" : "music") + ' ' + esc(MODES[c.mode] || MODES.audio) +
    (c.minMinutes ? '<span class="sep">•</span>' + c.minMinutes + ' דק׳ ומעלה' : '') + (c.skipShorts ? '<span class="sep">•</span>בלי Shorts' : '') + '</div></div>' +
    '<div class="acts">' + btn("batch", c.id, "download", "הורד כמות סרטונים מהערוץ", "primary") + btn("remove", c.id, "trash", "הסר ערוץ", "danger") + '</div></div>').join("")
    : '<div class="empty">' + icon("channels") + '<div>עוד אין ערוצים במעקב</div></div>';
  const box = $("logs"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = S.logs.length ? S.logs.map((l) => '<div class="l"><span class="t">' + hhmm(l.t) + '</span>  <span class="src s-' + esc(l.src) + '">' + esc(l.src) + '</span> ' +
    '<span dir="auto" class="m' + (/ERROR|⚠|נכשל|\\[!\\]|שגיא/.test(l.msg) ? " bad" : "") + '">' + esc(l.msg) + '</span></div>').join("") : '<span class="t">אין עדיין לוגים</span>';
  if (atBottom) box.scrollTop = box.scrollHeight;
  // MP3 only while the desktop app is running (it does the conversion).
  const mp3ok = !!APP() || pcActive();
  document.querySelectorAll('select[name=mode] option[value=audio]').forEach((o) => {
    o.disabled = !mp3ok; o.textContent = mp3ok ? MODES.audio : "שיר · MP3 — זמין רק כשהתוכנה למחשב פועלת";
    if (!mp3ok && o.selected) o.parentNode.value = "video";
  });
  $("profileName").textContent = (S.profile && S.profile.name) || "";
  $("globalNav").classList.toggle("hidden", !(S.profile && S.profile.admin));
  $("cookieCard").classList.toggle("hidden", !S.cookieExt);
  if (S.cookieExt) {
    $("ckKey").textContent = S.cookieExt.key;
    $("ckAt").textContent = S.cookieExt.at ? ago(S.cookieExt.at) + " · " + (S.cookieExt.count || "?") + " עוגיות" : "עוד לא התקבלו עוגיות מהתוסף";
  }
  renderDash();
  $("profilesCard").classList.toggle("hidden", !(S.profile && S.profile.admin));
  const sw = (pr, flag, label) => '<label>' + label + '<span class="switch"><input type="checkbox" data-prof="' + esc(pr.id) + '" data-flag="' + flag + '"' + (pr[flag] ? " checked" : "") + '><i></i></span></label>';
  if (!document.activeElement || !document.activeElement.dataset || !document.activeElement.dataset.prof)
  $("profilesList").innerHTML = (S.profiles || []).map((pr) =>
    '<div class="setting"><div class="avatar">' + esc(pr.name.charAt(0)) + '</div><div class="txt"><b>' + esc(pr.name) + '</b><span>' +
    (pr.admin ? "מנהל · הקוד הוא ADMIN_PASSWORD" : "נוצר " + ago(pr.created)) + '</span></div>' +
    '<div class="pflags">' + (pr.admin ? "" : sw(pr, "netfree", "רק פתוח בנטפרי")) + sw(pr, "vault", "כספת") + sw(pr, "direct", "מוריד מהמחשב") + '</div>' +
    btn("profname", pr.id, "edit", "שינוי שם") + (pr.admin ? "" : btn("profcode", pr.id, "key", "קוד חדש") + btn("profdel", pr.id, "trash", "מחק פרופיל וכל הנתונים שלו", "danger")) + '</div>').join("");
  $("pcState").textContent = pcActive() ? "התוכנה פועלת עכשיו — היא מורידה." : "";
}

$("ckCopy").onclick = async () => { try { await navigator.clipboard.writeText(S.cookieExt.key); toast("המפתח הועתק"); } catch (e) { toast("סמן והעתק ידנית"); } };
document.addEventListener("change", async (e) => {
  const t = e.target; if (!t.dataset || !t.dataset.prof) return;
  const d = new FormData(); d.append("id", t.dataset.prof); d.append(t.dataset.flag, t.checked ? "1" : "0");
  try { await post("/profiles/update", d); toast("נשמר"); t.blur(); refresh(); } catch (x) { toast("שגיאה: " + x.message, 6000); t.checked = !t.checked; }
});

// ---------- data ----------
// Signed out meanwhile (the answer is the sign-in page, not data): show it,
// instead of a stale page whose buttons fail with "<!doctype … not valid JSON".
async function jsonOrLogin(r) {
  if (!(r.headers.get("content-type") || "").includes("json")) { location.reload(); throw new Error("צריך להתחבר מחדש"); }
  return r.json();
}
async function refresh() { try { S = await jsonOrLogin(await fetch(B + "/status")); render(); autoDownload(); } catch (e) {} }
async function post(path, data) {
  const r = await fetch(B + path, { method: "POST", body: data });
  if (!r.ok) throw new Error((await r.text()).replace(/^Error: /, ""));
  return r;
}
async function pollProgress() {
  if (!S.renderUrl || !S.jobs.some((j) => j.status === "taken")) return;
  try { P = (await (await fetch(S.renderUrl.replace(/\\/$/, "") + "/progress", { cache: "no-store" })).json()).jobs || {}; } catch (e) { return; }
  document.querySelectorAll("[data-prog]").forEach((el) => { el.innerHTML = progHtml(el.dataset.prog); });
}

// ---------- actions ----------
document.querySelectorAll("form[data-action]").forEach((f) => f.onsubmit = async (e) => {
  e.preventDefault();
  const b = f.querySelector("button"); b.disabled = true;
  const prof = f.dataset.action.startsWith("/profiles");
  try { await post(f.dataset.action, new FormData(f)); f.reset(); toast(prof ? "הפרופיל נוצר" : "נוסף לתור"); if (!prof) showPage("queue"); await refresh(); }
  catch (err) { toast("שגיאה: " + err.message, 8000); }
  b.disabled = false;
});
const MSG = { pause: "הושהה", resume: "ממשיך", priority: "יורד עכשיו", retry: "חזר לתור", cancel: "בוטל", remove: "הוסר" };
document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]"); if (!el) return;
  const act = el.dataset.act, id = el.dataset.id, d = new FormData(); d.append("id", id);
  try {
    if (act === "cancel" && !confirm("לבטל את ההורדה?")) return;
    if (act === "remove" && !confirm("להסיר את הערוץ מהמעקב?")) return;
    if (act === "profcode" || act === "profname") {
      const v = prompt(act === "profcode" ? "קוד חדש לפרופיל (לפחות 4 תווים). המשתמש לא יוכל לשנות אותו:" : "שם חדש:");
      if (!v) return;
      d.append(act === "profcode" ? "password" : "name", v);
      await post("/profiles/update", d); toast(act === "profcode" ? "הקוד הוחלף" : "השם עודכן"); refresh(); return;
    }
    if (act === "profdel") {
      if (!confirm("למחוק את הפרופיל וכל התור, הערוצים וההיסטוריה שלו?")) return;
      await post("/profiles/remove", d); toast("הפרופיל נמחק"); refresh(); return;
    }
    if (act === "delete") {
      const job = S.jobs.find((x) => x.videoId === id) || {};
      if (!confirm("למחוק מההיסטוריה את: " + (job.title || id) + "?")) return;
      // In the desktop app: also forget it there, and optionally delete the file.
      if (APP() && HAVE.has(id)) await APP().forget([id], confirm("למחוק גם את הקובץ מהמחשב?"));
      await post("/jobs/delete", d); toast("נמחק מההיסטוריה"); await loadHave(); refresh(); return;
    }
    if (act === "pcdl") { toast("מוריד למחשב…"); const r = await APP().download_now(id); toast(r || "ירד"); await loadHave(); render(); return; }
    if (act === "showfile") { await APP().show_file(id); return; }
    if (act === "batch") {
      const n = prompt("כמה סרטונים אחרונים להוריד מהערוץ?", "10"); if (!n) return;
      const c = S.channels.find((x) => x.id === id) || {};
      d.append("channel", id); d.append("count", n); d.append("mode", c.mode || "audio");
      d.append("minMinutes", c.minMinutes || 0); if (c.skipShorts) d.append("skipShorts", "on");
      await post("/jobs/batch", d); toast("נוסף לתור"); showPage("queue"); refresh(); return;
    }
    const path = act === "remove" ? "/channels/remove" : act === "retry" ? "/jobs/retry" : "/jobs/" + act;
    await post(path, d); toast(MSG[act] || "בוצע"); refresh();
  } catch (x) { toast("שגיאה: " + x.message, 6000); }
});
async function wake(btnEl) {
  btnEl.disabled = true; toast("מעיר את שרת ההורדות… (עד דקה)", 60000);
  try { const r = await jsonOrLogin(await fetch(B + "/wake", { method: "POST" })); toast(r.result === "ok" ? "השרת ער ובודק את התור" : "שגיאה: " + r.result, 8000); }
  catch (e) { toast("שגיאה: " + e); }
  btnEl.disabled = false; refresh();
}
$("wakeBtn").onclick = () => wake($("wakeBtn")); $("wakeBtn2").onclick = () => wake($("wakeBtn2"));
$("checkBtn").onclick = async () => { const b = $("checkBtn"); b.disabled = true;
  try { await post("/check", new FormData()); toast("נבדק"); } catch (e) { toast("שגיאה: " + e.message); } b.disabled = false; refresh(); };
$("clearHistory").onclick = async () => {
  if (!confirm("למחוק מההיסטוריה את כל מה שהסתיים (הושלם / נכשל / דולג / בוטל)? הורדות פעילות לא יימחקו.")) return;
  const files = APP() ? confirm("למחוק גם את הקבצים עצמם מהמחשב?") : false;
  try {
    const r = await (await fetch(B + "/jobs/clear", { method: "POST" })).json();
    if (APP()) await APP().forget(r.deleted || [], files);
    toast("נמחקו " + (r.deleted || []).length + " פריטים"); await loadHave(); refresh();
  } catch (e) { toast("שגיאה: " + e); }
};
$("clearLogs").onclick = async () => { await fetch(B + "/logs/clear", { method: "POST" }); refresh(); };
$("copyLogs").onclick = async () => {
  const text = S.logs.map((l) => hhmm(l.t) + " [" + l.src + "] " + l.msg).join("\\n");
  try { await navigator.clipboard.writeText(text); } catch (e) {
    const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); }
  toast("הלוג הועתק");
};
// ---------- browser auto-download (not needed inside the desktop app) ----------
const store = (k, v) => { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} };
const load = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
$("autoDl").checked = !!load("autoDlSince");
$("autoDl").onchange = (e) => store("autoDlSince", e.target.checked ? new Date().toISOString() : undefined);
let dlBusy = false;
// The desktop app pings every few minutes; while it's running it downloads.
const pcActive = () => S.pc && S.pc.seen && Date.now() - Date.parse(S.pc.seen) < 25 * 60000;
async function autoDownload() {
  const since = load("autoDlSince");
  if (!since || dlBusy || APP() || pcActive()) return;   // the desktop app handles it
  dlBusy = true;
  const got = JSON.parse(load("autoDlGot") || "[]");
  for (const j of S.jobs) {
    if (j.status !== "done" || !j.fileUrl || j.pcGot || j.updated < since || got.includes(j.videoId)) continue;
    const f = document.createElement("iframe"); f.style.display = "none"; f.src = j.fileUrl; document.body.appendChild(f);
    got.push(j.videoId); store("autoDlGot", JSON.stringify(got.slice(-500)));
    toast("מוריד: " + j.title); await new Promise((r) => setTimeout(r, 1500));
  }
  dlBusy = false;
}

// ---------- desktop app extras ----------
async function loadHave() { if (!APP()) return; try { HAVE = new Set(await APP().downloaded()); } catch (e) {} }
async function initDesktop() {
  if (!APP()) return;
  await loadHave(); setInterval(async () => { await loadHave(); render(); }, 15000);
  try {
    const info = await APP().app_info((S.profile && S.profile.id) || "");
    $("desktopCard").classList.remove("hidden");
    if (info.vault) { $("vaultNav").classList.remove("hidden"); $("vaultNav").onclick = () => APP().open_vault(); }
    $("folderPath").textContent = info.folder; $("appVersion").textContent = info.version || "פיתוח";
    loadVersion();
    $("autostart").checked = !!info.autostart;
    $("autostart").onchange = async (e) => { await APP().set_autostart(e.target.checked); toast(e.target.checked ? "תעלה עם Windows" : "לא תעלה עם Windows"); };
    $("openFolder").onclick = () => APP().open_folder();
    $("pickFolder").classList.toggle("hidden", !!info.vault);
    $("pickFolder").onclick = async () => { const f = await APP().pick_folder(); if (f) { $("folderPath").textContent = f; toast("התיקייה נשמרה"); } };
    $("nfTestRow").classList.toggle("hidden", !info.admin);
    $("nfTestBtn").onclick = async () => { $("nfTestOut").textContent = "בודק…"; try { $("nfTestOut").textContent = await APP().nf_test($("nfTestUrl").value); } catch (x) { $("nfTestOut").textContent = "שגיאה: " + x; } };
    document.querySelectorAll('a[href$="/logout"]').forEach((a) => a.addEventListener("click", () => { try { APP().sign_out(); } catch (x) {} }));
    $("notifyMode").value = info.notify || "each";
    $("notifyMode").onchange = async (e) => { await APP().set_notify(e.target.value); toast("ההתראות עודכנו"); };
    render();
  } catch (e) {}
}
// Version: installed vs. latest, and what's new (CHANGELOG.md, simple markdown).
function mdList(text) {
  let html = "", open = false;
  for (const raw of String(text || "").split(String.fromCharCode(10))) {
    const l = raw.trim();
    if (!l || l.startsWith("<!--") || l.startsWith("# ")) continue;
    if (l.startsWith("- ")) { if (!open) { html += "<ul>"; open = true; } html += "<li>" + esc(l.slice(2)) + "</li>"; continue; }
    if (open) { html += "</ul>"; open = false; }
    html += l.startsWith("## ") ? "<h4>" + esc(l.slice(3)) + "</h4>" : "<p>" + esc(l) + "</p>";
  }
  return html + (open ? "</ul>" : "");
}
async function loadVersion() {
  let v; try { v = await APP().version_info(); } catch (e) { return; }
  $("appVersion").innerHTML = 'מותקנת: <span style="font-weight:600;color:var(--text)">' + esc(v.installed) + "</span>" +
    (v.latest ? (v.update ? ' · <span class="vnew">יש גרסה חדשה: ' + esc(v.latest) + '</span>' : ' · <span class="vok">' + icon("check") + ' זו הגרסה האחרונה</span>') : (v.error ? " · " + esc(v.error) : ""));
  $("updateNow").classList.toggle("hidden", !v.update);
  $("updateNow").onclick = async () => {
    $("updateNow").disabled = true; toast("מוריד את גרסה " + v.latest + "…", 60000);
    const r = await APP().update_now();
    toast(r === "ok" ? "מתקין ומפעיל מחדש… (ההורדות ממשיכות)" : r, 8000); $("updateNow").disabled = false;
  };
  $("newTitle").textContent = "מה חדש בגרסה " + (v.latest || v.installed);
  $("newNotes").innerHTML = mdList(v.notes) || '<span class="sub">אין פירוט</span>';
  $("changelog").innerHTML = mdList(v.changelog) || '<span class="sub">אין פירוט</span>';
  $("whatsNew").classList.remove("hidden");
}
window.addEventListener("pywebviewready", initDesktop);

render(); autoDownload(); initDesktop();
// Only while the page is on screen (every refresh costs database reads).
setInterval(() => { if (!document.hidden) refresh(); }, 8000); setInterval(() => { if (!document.hidden) pollProgress(); }, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
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
