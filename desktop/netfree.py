"""Is a YouTube video open on NetFree?

The check has to run on the home computer — that's the connection NetFree
filters (the download server isn't behind NetFree). It asks for the video
the way a player would and sees whether NetFree lets it through:

  1. the watch page,
  2. the player data (where the stream addresses come from),
  3. the first bytes of the stream itself.

Getting real video/audio bytes means NetFree allows it. A NetFree block
page / status anywhere on the way means it doesn't. Anything else (no
internet, YouTube itself refusing) is "unknown" and is checked again later.
Every step is written into `detail`, so a wrong verdict can be calibrated
from the log.
"""

import json

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
VR_VERSION = "1.65.10"
VR_UA = (f"com.google.android.apps.youtube.vr.oculus/{VR_VERSION} (Linux; U; Android 12L; "
         "eureka-user Build/SQ3A.220605.009.A1) gzip")
BLOCK_STATUS = {418, 451}


def _netfree_mark(r: requests.Response, body: str = "") -> bool:
    """NetFree's block answer: a redirect to its own site, its headers, a
    filter status code, or its block page."""
    if r.status_code in BLOCK_STATUS:
        return True
    hosts = [r.url] + [h.headers.get("Location", "") for h in r.history]
    if any("netfree" in (u or "").lower() for u in hosts):
        return True
    if any("netfree" in (k + v).lower() for k, v in r.headers.items()):
        return True
    low = body[:20000].lower()
    return r.status_code >= 400 and ("netfree" in low or "נטפרי" in body[:20000])


def probe(video_id: str, session: requests.Session | None = None, timeout: int = 25) -> tuple[str, str]:
    """('open' | 'blocked' | 'unknown', detail)."""
    s = session or requests.Session()
    steps = []

    # 1. the watch page
    try:
        r = s.get("https://www.youtube.com/watch?v=" + video_id, headers={"User-Agent": UA, "Accept-Language": "he,en"},
                  timeout=timeout)
        body = r.text
        steps.append(f"page {r.status_code} {len(body)}b")
        if _netfree_mark(r, body):
            return "blocked", "; ".join(steps) + " · חסימת נטפרי בדף הסרטון"
    except requests.RequestException as e:
        steps.append(f"page error {type(e).__name__}")

    # 2. player data
    url = ""
    try:
        r = s.post("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", timeout=timeout,
                   headers={"User-Agent": VR_UA, "Content-Type": "application/json",
                            "X-YouTube-Client-Name": "28", "X-YouTube-Client-Version": VR_VERSION},
                   data=json.dumps({"videoId": video_id, "contentCheckOk": True, "racyCheckOk": True,
                                    "context": {"client": {"clientName": "ANDROID_VR", "clientVersion": VR_VERSION,
                                                           "deviceMake": "Oculus", "deviceModel": "Quest 3",
                                                           "androidSdkVersion": 32, "osName": "Android",
                                                           "osVersion": "12L", "hl": "he", "gl": "IL"}}}))
        if _netfree_mark(r, r.text):
            return "blocked", "; ".join(steps + [f"player {r.status_code}"]) + " · חסימת נטפרי בנתוני הנגן"
        data = r.json()
        ps = (data.get("playabilityStatus") or {})
        steps.append(f"player {r.status_code} {ps.get('status')}")
        fmts = (data.get("streamingData") or {}).get("formats", []) + \
               (data.get("streamingData") or {}).get("adaptiveFormats", [])
        fmts = [f for f in fmts if f.get("url")]
        # smallest audio first — cheapest to probe
        fmts.sort(key=lambda f: (not f.get("mimeType", "").startswith("audio"), int(f.get("contentLength") or 1e12)))
        if fmts:
            url = fmts[0]["url"]
        elif ps.get("status") and ps.get("status") != "OK":
            steps.append(str(ps.get("reason") or "")[:80])
    except (requests.RequestException, ValueError) as e:
        steps.append(f"player error {type(e).__name__}")

    # 3. the stream itself
    if url:
        try:
            r = s.get(url, headers={"User-Agent": VR_UA, "Range": "bytes=0-131071"}, timeout=timeout, stream=True)
            chunk = next(r.iter_content(65536), b"") if r.ok else b""
            ctype = r.headers.get("Content-Type", "")
            steps.append(f"stream {r.status_code} {ctype} {len(chunk)}b")
            if _netfree_mark(r, "" if r.ok else r.text):
                return "blocked", "; ".join(steps) + " · נטפרי חסם את הזרם"
            if r.ok and len(chunk) > 1000 and (ctype.startswith(("video/", "audio/", "application/octet-stream"))):
                return "open", "; ".join(steps)
            if r.ok and b"netfree" in chunk[:4000].lower():
                return "blocked", "; ".join(steps) + " · דף חסימה במקום הסרטון"
        except requests.RequestException as e:
            # NetFree sometimes just cuts the connection of a blocked stream
            steps.append(f"stream error {type(e).__name__}")
    return "unknown", "; ".join(steps)
