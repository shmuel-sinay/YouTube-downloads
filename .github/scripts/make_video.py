"""Makes one video for the site's "סרטונים" page.

Run by .github/workflows/video.yml. Reads the job (prompt, model, the paid
Gemini key if there is one) from the Worker. With the key: Veo. Without it:
free, on a Hugging Face ZeroGPU Space (HF_SPACE, default LTX-Video; HF_TOKEN
gives the account's daily GPU quota instead of the smaller anonymous one).
Either way it downloads the video
and writes it scrambled to out/v.yti — "YTI1" + IV + AES-GCM, the same fixed
key as the Worker's scrambleImage and desktop/images.py. `report` (the last
step) tells the Worker how it went; out/status.json carries it between steps.

The repository is public, so neither the prompt nor the keys are printed.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TRACKER = os.environ["TRACKER_URL"].rstrip("/")
TOKEN = os.environ["AGENT_TOKEN"]
JOB = os.environ["JOB"]
API = "https://generativelanguage.googleapis.com/v1beta/"
KEY = hashlib.sha256(b"yt-tracker-image-v1").digest()
STATUS = "out/status.json"


def http(url: str, body=None, headers=None, method=None) -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {**({"Content-Type": "application/json"} if data else {}), **(headers or {})},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def site(method: str, body=None) -> dict:
    q = f"?id={JOB}&run={os.environ.get('GITHUB_RUN_ID', '')}"
    st, data, _ = http(TRACKER + "/api/vid/job" + q, body, {"Authorization": "Bearer " + TOKEN}, method)
    if st != 200:
        raise SystemExit(f"site: HTTP {st} {data[:200]!r}")
    return json.loads(data)


def message(data: bytes) -> str:
    text = data.decode(errors="replace")
    try:
        return json.loads(text)["error"]["message"]
    except Exception:
        return text[:300]


def key_trouble(st: int, text: str) -> bool:
    """Same test as the Worker's keyTrouble: move on to the next key."""
    return st in (401, 403, 429) or "spending cap" in text.lower() or (
        st == 400 and re.search(r"API_KEY_INVALID|API key not valid|API key expired", text, re.I) is not None)


def scramble(data: bytes, mime: str) -> bytes:
    m = mime.encode()
    iv = os.urandom(12)
    return b"YTI1" + iv + AESGCM(KEY).encrypt(iv, bytes([len(m)]) + m + data, None)


def save(out: dict, video: bytes, mime: str) -> None:
    with open("out/v.yti", "wb") as f:
        f.write(scramble(video, mime if mime.startswith("video/") else "video/mp4"))
    print(f"video: {len(video) // 1024} KB", flush=True)
    out["status"] = "done"


def make_free(out: dict, prompt: str) -> None:
    """A ZeroGPU Space through gradio_client. The endpoint and its other inputs
    are the Space's own (defaults are used); HF_API overrides the name."""
    from gradio_client import Client
    space = os.environ.get("HF_SPACE") or "Lightricks/ltx-video-distilled"
    api = os.environ.get("HF_API") or "/text_to_video"
    print(f"free: {space} {api}", flush=True)
    try:
        client = Client(space, hf_token=os.environ.get("HF_TOKEN") or None, verbose=False)
        res = client.predict(prompt=prompt, api_name=api)
    except Exception as e:
        text = str(e)
        out["error"] = ("נגמרה המכסה היומית החינמית ב-Hugging Face — אפשר לנסות שוב מחר"
                        if re.search(r"quota|ZeroGPU", text, re.I) else "Hugging Face: " + text[:300])
        print(f"free: {type(e).__name__}", flush=True)
        return
    path = res[0] if isinstance(res, (list, tuple)) else res
    if isinstance(path, dict):                           # {"video": path, ...}
        path = path.get("video") or path.get("path")
    if not path or not os.path.exists(str(path)):
        out["error"] = "Hugging Face לא החזיר סרטון"
        return
    with open(path, "rb") as f:
        save(out, f.read(), "video/webm" if str(path).endswith(".webm") else "video/mp4")


def make(out: dict) -> None:
    job = site("GET")
    for k in job["keys"]:
        print(f"::add-mask::{k['key']}", flush=True)
    if not job.get("paid"):
        return make_free(out, job["prompt"])
    if not job["keys"]:
        out["error"] = "המפתח בתשלום לסרטונים לא זמין כרגע"
        return
    model = job["model"]
    body = {"instances": [{"prompt": job["prompt"]}], "parameters": {"aspectRatio": "16:9"}}
    op = key = None
    for k in job["keys"]:
        st, data, _ = http(API + f"models/{model}:predictLongRunning", body, {"x-goog-api-key": k["key"]})
        if st == 200:
            op, key = json.loads(data)["name"], k
            break
        text = data.decode(errors="replace")
        out["failed"].append({"i": k["i"], "status": st, "text": text[:2000]})
        out["error"] = "Gemini לא התחיל סרטון: " + message(data)
        print(f"key {k['i']}: HTTP {st}", flush=True)
        if not key_trouble(st, text):
            return                                       # not the key's fault — another key won't help
    if not op:
        return
    out["used"] = key["i"]
    print("started, waiting for Veo…", flush=True)
    t0 = time.time()
    while True:
        time.sleep(10)
        st, data, _ = http(API + op, headers={"x-goog-api-key": key["key"]})
        if st != 200:
            out["error"] = "Gemini: " + message(data)
            return
        d = json.loads(data)
        if d.get("error"):
            out["error"] = "Gemini: " + str(d["error"].get("message", d["error"]))[:300]
            return
        if d.get("done"):
            break
        if time.time() - t0 > 15 * 60:
            out["error"] = "Veo לא סיים תוך 15 דקות — כדאי לנסות שוב"
            return
    print(f"done after {int(time.time() - t0)}s", flush=True)
    g = d.get("response", {}).get("generateVideoResponse", {})
    samples = g.get("generatedSamples") or []
    uri = samples and samples[0].get("video", {}).get("uri")
    if not uri:
        out["error"] = " ".join(g.get("raiMediaFilteredReasons") or []) or \
            'Gemini סיים בלי סרטון. כדאי לנסח אחרת, למשל: "סרטון של גלים מתנפצים על סלעים בשקיעה"'
        return
    st, video, headers = http(uri, headers={"x-goog-api-key": key["key"]})
    if st != 200:
        out["error"] = f"הורדת הסרטון מ-Gemini נכשלה: HTTP {st}"
        return
    save(out, video, (headers.get("Content-Type") or "").split(";")[0])


def report() -> None:
    try:
        with open(STATUS, encoding="utf-8") as f:
            out = json.load(f)
    except Exception:
        out = {"failed": [], "error": "השרת ב-GitHub נכשל לפני שסיים (ראה Actions ← Make video)"}
    if out.get("status") == "done" and os.environ.get("UPLOAD") != "success":
        out = {**out, "status": "failed", "error": "שמירת הקובץ ב-GitHub נכשלה"}
    site("POST", {**out, "id": JOB})
    print("reported:", out.get("status") or "failed", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["report"]:
        report()
    else:
        os.makedirs("out", exist_ok=True)
        result = {"status": "failed", "failed": []}
        try:
            make(result)
        except SystemExit:
            raise
        except Exception as e:                           # still reported, by the last step
            result["error"] = f"השרת ב-GitHub נכשל: {type(e).__name__}"
            raise
        finally:
            with open(STATUS, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
