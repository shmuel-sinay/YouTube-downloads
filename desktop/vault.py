"""Encrypted vault for downloaded files.

* Adding a file needs NO password: each file is encrypted with its own
  random key, sealed to the vault's public key (X25519). So the app can put
  new downloads straight into the vault in the background.
* Opening/listing needs the password: it unlocks the vault's private key
  (scrypt → AES-256-GCM).
* In Windows Explorer the vault is just files with random names
  (e.g. 3f9a….ytv) — no titles, no extensions, nothing readable.

File format (.ytv):
  b"YTV1" | ephemeral public key (32) | header length (4, big-endian)
  | header: AES-GCM(nonce 0) of JSON {name, size, added, ...}
  | chunks: each 4-byte length + AES-GCM(nonce n) of up to 1 MiB;
    the last chunk is bound with associated data b"end" (no truncation).
"""

import base64
import json
import os
import secrets
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"YTV1"
CHUNK = 1 << 20
META = "vault.json"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _nonce(n: int) -> bytes:
    return n.to_bytes(12, "big")


def _file_key(shared: bytes, eph_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub, info=b"ytv1 file").derive(shared)


def _pw_key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode("utf-8"))


class Vault:
    def __init__(self, folder: str | Path):
        self.dir = Path(folder)
        self._priv: X25519PrivateKey | None = None

    # ---------------------------------------------------------------- setup

    @property
    def exists(self) -> bool:
        return (self.dir / META).exists()

    def create(self, password: str) -> None:
        if self.exists:
            raise RuntimeError("כבר קיימת כספת בתיקייה הזו")
        self.dir.mkdir(parents=True, exist_ok=True)
        priv = X25519PrivateKey.generate()
        raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        meta = {
            "v": 1,
            "pub": _b64(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
            "salt": _b64(salt), "nonce": _b64(nonce),
            "priv": _b64(AESGCM(_pw_key(password, salt)).encrypt(nonce, raw, b"vault-key")),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (self.dir / META).write_text(json.dumps(meta), encoding="utf-8")
        self._priv = priv

    def _meta(self) -> dict:
        return json.loads((self.dir / META).read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- lock

    @property
    def unlocked(self) -> bool:
        return self._priv is not None

    def unlock(self, password: str) -> bool:
        m = self._meta()
        try:
            raw = AESGCM(_pw_key(password, _unb64(m["salt"]))).decrypt(_unb64(m["nonce"]), _unb64(m["priv"]), b"vault-key")
        except Exception:
            return False
        self._priv = X25519PrivateKey.from_private_bytes(raw)
        return True

    def lock(self) -> None:
        self._priv = None

    def change_password(self, old: str, new: str) -> bool:
        if not self.unlock(old):
            return False
        m = self._meta()
        raw = self._priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                       serialization.NoEncryption())
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        m.update(salt=_b64(salt), nonce=_b64(nonce), priv=_b64(AESGCM(_pw_key(new, salt)).encrypt(nonce, raw, b"vault-key")))
        (self.dir / META).write_text(json.dumps(m), encoding="utf-8")
        return True

    # ---------------------------------------------------------------- add (no password needed)

    def add(self, src: str | Path, info: dict | None = None, remove_src: bool = True) -> str:
        """Encrypt `src` into the vault. Returns the entry id."""
        src = Path(src)
        pub = X25519PublicKey.from_public_bytes(_unb64(self._meta()["pub"]))
        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        aes = AESGCM(_file_key(eph.exchange(pub), eph_pub))
        header = json.dumps({"name": src.name, "size": src.stat().st_size,
                             "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **(info or {})},
                            ensure_ascii=False).encode()
        eid = secrets.token_hex(12)
        tmp = self.dir / (eid + ".part")
        with src.open("rb") as fin, tmp.open("wb") as out:
            hdr = aes.encrypt(_nonce(0), header, b"hdr")
            out.write(MAGIC + eph_pub + struct.pack(">I", len(hdr)) + hdr)
            n, chunk = 1, fin.read(CHUNK)
            while True:
                nxt = fin.read(CHUNK)
                enc = aes.encrypt(_nonce(n), chunk, b"end" if not nxt else b"")
                out.write(struct.pack(">I", len(enc)) + enc)
                if not nxt:
                    break
                chunk, n = nxt, n + 1
        tmp.replace(self.dir / (eid + ".ytv"))
        if remove_src:
            src.unlink(missing_ok=True)
        return eid

    # ---------------------------------------------------------------- read (password needed)

    def _open(self, path: Path):
        if not self._priv:
            raise PermissionError("הכספת נעולה")
        f = path.open("rb")
        if f.read(4) != MAGIC:
            f.close()
            raise ValueError("לא קובץ כספת")
        eph_pub = f.read(32)
        aes = AESGCM(_file_key(self._priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub))
        (hlen,) = struct.unpack(">I", f.read(4))
        header = json.loads(aes.decrypt(_nonce(0), f.read(hlen), b"hdr"))
        return f, aes, header

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.ytv")):
            try:
                f, _, h = self._open(p)
                f.close()
                out.append({"id": p.stem, **h})
            except PermissionError:
                raise
            except Exception:
                continue          # damaged / foreign file — skip
        return sorted(out, key=lambda e: e.get("added", ""), reverse=True)

    def extract(self, eid: str, dest_dir: str | Path, progress=None) -> Path:
        """Decrypt one entry into dest_dir (under its real name).
        progress(bytes_written_so_far) is called after every chunk."""
        f, aes, h = self._open(self.dir / (eid + ".ytv"))
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = "".join(c for c in h["name"] if c not in '<>:"/\\|?*') or eid
        out_path = dest_dir / name
        try:
            with f, out_path.open("wb") as out:
                n, written = 1, 0
                while True:
                    raw = f.read(4)
                    if len(raw) < 4:
                        raise ValueError("הקובץ קטוע")
                    (ln,) = struct.unpack(">I", raw)
                    blob = f.read(ln)
                    try:
                        data, last = aes.decrypt(_nonce(n), blob, b""), False
                    except Exception:
                        data, last = aes.decrypt(_nonce(n), blob, b"end"), True
                    out.write(data)
                    written += len(data)
                    if progress:
                        progress(written)
                    if last:
                        break
                    n += 1
        except Exception:
            out_path.unlink(missing_ok=True)
            raise
        return out_path

    def delete(self, eid: str) -> None:
        (self.dir / (eid + ".ytv")).unlink(missing_ok=True)
