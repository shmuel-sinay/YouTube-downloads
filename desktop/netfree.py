"""Is a YouTube video open on NetFree?

The check has to run on the home computer — that's the connection NetFree
filters (the download server isn't behind NetFree). It opens the video's
page the way a browser does: the whole page → open; NetFree's block answer
(418 / its block page) → blocked; anything else (no internet, a cut
connection) → "unknown", checked again later — never blocked by a guess.
"""

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
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
    """('open' | 'blocked' | 'unknown', detail).

    Calibrated on a real NetFree line: NetFree filters YouTube by the video
    page itself — an approved video's page comes back whole (~1MB), a
    blocked one gets NetFree's short 418 answer. (NetFree answers 418 to the
    app-style player request for *every* video, so that one says nothing.)"""
    s = session or requests.Session()
    try:
        r = s.get("https://www.youtube.com/watch?v=" + video_id, timeout=timeout,
                  headers={"User-Agent": UA, "Accept-Language": "he,en"})
        body = r.text
    except requests.RequestException as e:
        return "unknown", f"page error {type(e).__name__}"
    detail = f"page {r.status_code} {len(body)}b"
    if _netfree_mark(r, body):
        return "blocked", detail + " · נטפרי חסם את דף הסרטון"
    if r.status_code == 200 and len(body) > 100_000 and video_id in body:
        return "open", detail
    return "unknown", detail + " · תשובה לא צפויה"
