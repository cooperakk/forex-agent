"""Storage for broker credentials.

A broker password is the account. Anyone holding it can move the money without
touching this software at all, so it gets treated with more care than anything
else this system persists -- and, more importantly, the care is *described
accurately* rather than implied by the word "encrypted".

What this actually gives you
----------------------------
Secrets are sealed with AES-256-GCM. The key comes from, in order:

1. ``SENTINEL_SECRET_KEY`` -- 32 bytes, base64. Injected by systemd from a
   credential store, a hardware token, or an operator's hand at start time.
2. A key file, 0600, whose path is configurable and which may live on a
   different volume, a removable device, or an encrypted home directory.
3. Nothing. There is no third option: the store refuses to operate without a
   key rather than falling back to an obfuscation that looks like encryption.

**If the key file sits in the same directory as the sealed data, this protects
a stolen backup and a misdirected file copy. It does not protect against
someone who has root on this machine, because they can read both files.** That
sentence is repeated in the dashboard and in the deployment guide, because the
difference between those two threat models is the whole value of the feature
and a customer who misunderstands it will make a hosting decision they would
not otherwise make.

Design notes
------------
* The secret's *name* is authenticated as GCM associated data. Without it, an
  attacker who can write the file could swap the sealed blob for the live
  account into the slot for the demo account; the bytes would decrypt fine and
  the system would place live orders believing it was on the simulator.
* Nonces are random 96-bit and never reused, because each seal generates a new
  one and a sealed value is replaced wholesale, never edited in place.
* Values are returned as ``str`` and callers are expected to drop them
  promptly. Python cannot guarantee zeroisation, so this module does not claim
  to; it simply never holds a plaintext longer than one call.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
import threading
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV = "SENTINEL_SECRET_KEY"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretStoreError(RuntimeError):
    """The store cannot do what was asked, and the reason matters."""


class SecretUnavailable(SecretStoreError):
    """No key, so nothing can be read or written."""


class SecretCorrupt(SecretStoreError):
    """The sealed data did not authenticate: wrong key, or it was edited."""


def generate_key() -> str:
    """A fresh base64 key. Print it once; it cannot be recovered."""
    return base64.b64encode(_secrets.token_bytes(_KEY_BYTES)).decode("ascii")


def _load_key(key_path: Path, *, create: bool) -> bytes:
    """Resolve the key, preferring the environment over the file."""
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        try:
            raw = base64.b64decode(env, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SecretUnavailable(
                f"{KEY_ENV} is set but is not valid base64: {exc}") from exc
        if len(raw) != _KEY_BYTES:
            raise SecretUnavailable(
                f"{KEY_ENV} must decode to {_KEY_BYTES} bytes, got {len(raw)}")
        return raw

    if key_path.exists():
        try:
            raw = base64.b64decode(key_path.read_text(encoding="utf-8").strip(),
                                   validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SecretUnavailable(
                f"the key file {key_path} is unreadable or not base64: {exc}") from exc
        if len(raw) != _KEY_BYTES:
            raise SecretUnavailable(
                f"the key file {key_path} holds {len(raw)} bytes, not {_KEY_BYTES}")
        return raw

    if not create:
        raise SecretUnavailable(
            f"no credential key. Set {KEY_ENV} to a base64 32-byte key, or let "
            f"the installer create {key_path}.")

    # Create 0600 BEFORE writing, same reasoning as the user store: a file that
    # is briefly world-readable stays readable through any descriptor opened in
    # that window, and a credential key is the one file where that matters most.
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = _secrets.token_bytes(_KEY_BYTES)
    fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, base64.b64encode(raw))
    finally:
        os.close(fd)
    return raw


class SecretStore:
    """Named secrets, sealed at rest.

    The file holds a JSON object of ``name -> {nonce, ciphertext}``. Names are
    visible; values are not. That is deliberate: an operator needs to be able
    to see *which* credentials exist without being able to read them, and a
    fully opaque blob makes "is the live password even in here?" unanswerable
    without decrypting.
    """

    def __init__(self, path: str | Path, *, key_path: Optional[str | Path] = None,
                 create_key: bool = True) -> None:
        self.path = Path(path)
        self.key_path = Path(key_path) if key_path else self.path.with_suffix(".key")
        self._lock = threading.RLock()
        self._key = _load_key(self.key_path, create=create_key)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_raw({})
        else:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # -- plumbing ----------------------------------------------------------- #

    def _read_raw(self) -> Dict[str, Dict[str, str]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SecretStoreError(f"cannot read {self.path}: {exc}") from exc
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SecretCorrupt(
                f"{self.path} is not valid JSON: {exc}. It has been edited or "
                "truncated; restore it from a backup rather than deleting it, "
                "because deleting it silently un-configures every venue.") from exc
        if not isinstance(data, dict):
            # Returning {} here meant the next put() wrote a file containing
            # ONLY the new entry: every other broker credential gone, with no
            # error. The invalid-JSON branch above already refuses for exactly
            # this reason; the more valuable failure mode must not be the
            # quieter one.
            raise SecretCorrupt(
                f"{self.path} holds a {type(data).__name__}, not an object of "
                "named secrets. It has been replaced by something else; restore "
                "it from a backup rather than deleting it, because deleting it "
                "silently un-configures every venue.")
        return data

    def _write_raw(self, data: Dict[str, Dict[str, str]]) -> None:
        import tempfile
        # A random name in the same directory. A fixed "<path>.tmp" can be
        # squatted by a directory -- after which every write fails -- and two
        # processes holding their own store objects interleave into one file.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- api ---------------------------------------------------------------- #

    def put(self, name: str, value: str) -> None:
        if not name or "/" in name or len(name) > 128:
            raise ValueError("a secret name must be short and contain no slash")
        nonce = _secrets.token_bytes(_NONCE_BYTES)
        # The NAME is authenticated data: a sealed value cannot be moved from
        # one slot to another, which is the difference between "the demo
        # password" and "the live password" for everything downstream.
        sealed = AESGCM(self._key).encrypt(
            nonce, value.encode("utf-8"), name.encode("utf-8"))
        with self._lock:
            data = self._read_raw()
            data[name] = {
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "sealed": base64.b64encode(sealed).decode("ascii"),
            }
            self._write_raw(data)

    def get(self, name: str) -> Optional[str]:
        with self._lock:
            entry = self._read_raw().get(name)
        if not entry:
            return None
        try:
            nonce = base64.b64decode(entry["nonce"], validate=True)
            sealed = base64.b64decode(entry["sealed"], validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SecretCorrupt(f"secret {name!r} is not decodable: {exc}") from exc
        try:
            return AESGCM(self._key).decrypt(
                nonce, sealed, name.encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise SecretCorrupt(
                f"secret {name!r} did not authenticate. Either the credential "
                "key changed, or the file was edited. This is NOT a wrong "
                "password -- the software cannot read its own store."
            ) from exc

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._read_raw()

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self._read_raw()
            existed = data.pop(name, None) is not None
            if existed:
                self._write_raw(data)
            return existed

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._read_raw())

    # -- operator-facing description ---------------------------------------- #

    def protection_note(self) -> Dict[str, object]:
        """An honest statement of what the current setup protects against.

        The dashboard shows this verbatim. It is the only place a customer
        finds out that a key file next to the data file is not a secret from
        anyone who already has the machine.
        """
        from_env = bool(os.environ.get(KEY_ENV, "").strip())
        same_dir = (not from_env
                    and self.key_path.parent.resolve() == self.path.parent.resolve())
        # PERSIAN. This string is rendered verbatim in the dashboard, which is
        # entirely Persian; an English paragraph in the middle of it reads as a
        # bug, and an English sentence inside an RTL banner puts its full stop
        # at the wrong end of the line.
        if from_env:
            level = "good"
            note = ("کلید رمزنگاری از محیط سرویس می‌آید و کنار داده‌ها روی دیسک "
                    "ذخیره نشده است. اگر کسی پوشهٔ داده‌ها را کپی کند، چیز "
                    "قابل‌خواندنی به دست نمی‌آورد.")
        elif same_dir:
            level = "weak"
            note = ("کلید رمزنگاری در همان پوشه‌ای است که رمزهای قفل‌شده ذخیره "
                    "شده‌اند. این جلوی کسی را که یک نسخهٔ پشتیبان را بدزدد یا "
                    "فایلی را اشتباهی جایی بفرستد می‌گیرد. ولی جلوی کسی که به "
                    "خودِ این سرور دسترسی دارد را نمی‌گیرد — چون او هر دو فایل "
                    "را می‌خواند.")
        else:
            level = "fair"
            note = ("کلید رمزنگاری جدا از داده‌های قفل‌شده نگه داشته می‌شود. "
                    "امنیت به اندازهٔ امنیت همان محلِ جداست.")
        return {"key_source": "environment" if from_env else "file",
                "level": level, "note": note,
                "key_path": None if from_env else str(self.key_path)}
