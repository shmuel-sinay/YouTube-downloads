"""Pictures made with Gemini (the site's "תמונות" page).

They arrive scrambled ("YTI1" + IV + AES-GCM, same fixed key as the Worker's
scrambleImage) and stay that way on disk; a picture exists only in memory,
to be shown in the window — or as a real file when the user saves one.
"""

import base64
import hashlib
import json
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = hashlib.sha256(b"yt-tracker-image-v1").digest()
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def unscramble(blob: bytes) -> tuple[str, bytes]:
    if blob[:4] != b"YTI1":
        raise ValueError("לא קובץ תמונה של התוכנה")
    plain = AESGCM(KEY).decrypt(blob[4:16], blob[16:], None)
    n = plain[0]
    return plain[1:1 + n].decode(), plain[1 + n:]


class Gallery:
    """One profile's pictures: <id>.yti (scrambled) + index.json (prompts)."""

    def __init__(self, folder: Path):
        self.dir = folder
        self.index_file = folder / "index.json"

    def _index(self) -> list[dict]:
        try:
            return json.loads(self.index_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _write(self, items: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.index_file)

    def add(self, b64: str, prompt: str) -> str:
        blob = base64.b64decode(b64)
        unscramble(blob)                                # reject anything that isn't one
        iid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / (iid + ".yti")).write_bytes(blob)
        self._write([{"id": iid, "prompt": prompt[:500], "t": time.time()}] + self._index())
        return iid

    def list(self) -> list[dict]:
        return [m for m in self._index() if (self.dir / (m["id"] + ".yti")).exists()]

    def _load(self, iid: str) -> tuple[str, bytes]:
        if not all(c.isalnum() or c == "-" for c in iid):
            raise ValueError("bad id")
        return unscramble((self.dir / (iid + ".yti")).read_bytes())

    def data_url(self, iid: str) -> str:
        mime, data = self._load(iid)
        return f"data:{mime};base64," + base64.b64encode(data).decode()

    def export(self, iid: str, folder: Path) -> Path:
        mime, data = self._load(iid)
        prompt = next((m["prompt"] for m in self._index() if m["id"] == iid), iid)
        name = "".join(c for c in prompt[:40] if c not in '<>:"/\\|?*\n\r').strip() or iid
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (name + EXT.get(mime, ".png"))
        if dest.exists():
            dest = folder / (f"{name} {iid}" + EXT.get(mime, ".png"))
        dest.write_bytes(data)
        return dest

    def delete(self, iid: str) -> None:
        self._load(iid)                                  # validates the id
        (self.dir / (iid + ".yti")).unlink(missing_ok=True)
        self._write([m for m in self._index() if m["id"] != iid])
