"""Encrypted vault (/state/vault.enc).

Holds accounts, roles, groups, sources, password hashes, per-user language,
configuration, MariaDB credentials and the administrative audit log.  The
decrypted content lives only in memory.  File layout::

    MAGIC(8) | kdf(1) | salt(16) | nonce(12) | AES-256-GCM(ciphertext+tag)

The 37-byte header is authenticated as associated data, so any modification of
any byte is detected.  Every write uses a new random nonce and is atomic:
encrypted temp file -> fsync -> encrypted backup of the previous version ->
rename -> fsync of the directory.  Writers serialise on an flock() shared with the
console command ``reset-admin-password``.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from . import events
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import DecryptError, MasterKey, decrypt_blob

MAGIC = b"POVAULT\x01"
INFO = b"twincheck vault v1"
HEADER_LEN = len(MAGIC) + 1 + 16 + 12
AUDIT_MAX = 10000


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, path: Path, key: MasterKey):
        self.path = Path(path)
        self.key = key
        self.lock_path = self.path.parent / ".vault.lock"
        self.data: dict[str, Any] | None = None
        self._sig = None
        self._salt: bytes | None = None
        self._derived: bytes | None = None
        self._kdf = None
        self._mutex = threading.RLock()
        self.on_reload = None  # callback(old, new)

    # -- low level -------------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    @contextlib.contextmanager
    def _flock(self, exclusive: bool) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _signature(self):
        try:
            st = os.stat(self.path)
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    def _key_for(self, salt: bytes) -> bytes:
        if self._salt != salt or self._derived is None:
            self._kdf, self._derived = self.key.derive(salt, INFO)
            self._salt = salt
        return self._derived

    @staticmethod
    def decrypt_bytes(raw: bytes, key: MasterKey) -> dict:
        if len(raw) < HEADER_LEN + 16 or not raw.startswith(MAGIC):
            raise DecryptError("bad header")
        header = raw[:HEADER_LEN]
        kdf = raw[len(MAGIC)]
        salt = raw[len(MAGIC) + 1:len(MAGIC) + 17]
        nonce = raw[len(MAGIC) + 17:HEADER_LEN]
        if kdf != (2 if key.is_passphrase else 1):
            raise DecryptError("key type mismatch")
        _, k = key.derive(salt, INFO)
        plain = decrypt_blob(k, nonce, raw[HEADER_LEN:], header)
        return json.loads(plain.decode("utf-8"))

    def _read(self) -> dict:
        raw = self.path.read_bytes()
        if len(raw) < HEADER_LEN + 16 or not raw.startswith(MAGIC):
            raise DecryptError("bad header")
        header = raw[:HEADER_LEN]
        kdf = raw[len(MAGIC)]
        salt = raw[len(MAGIC) + 1:len(MAGIC) + 17]
        nonce = raw[len(MAGIC) + 17:HEADER_LEN]
        if kdf != (2 if self.key.is_passphrase else 1):
            raise DecryptError("key type mismatch")
        plain = decrypt_blob(self._key_for(salt), nonce, raw[HEADER_LEN:], header)
        return json.loads(plain.decode("utf-8"))

    def _encode(self, data: dict, key: MasterKey | None = None, salt: bytes | None = None) -> bytes:
        key = key or self.key
        if salt is None:
            salt = self._salt or secrets.token_bytes(16)
        if key is self.key:
            k = self._key_for(salt)
        else:
            _, k = key.derive(salt, INFO)
        kdf = 2 if key.is_passphrase else 1
        nonce = secrets.token_bytes(12)  # fresh random nonce on every write
        header = MAGIC + bytes([kdf]) + salt + nonce
        ct = AESGCM(k).encrypt(nonce, json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"), header)
        return header + ct

    def _atomic_write(self, blob: bytes) -> None:
        d = self.path.parent
        tmp = d / f".vault.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, blob)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if self.path.exists():
                # encrypted copy of the previous version (never plaintext)
                bak_tmp = d / f".vault.bak.{secrets.token_hex(4)}.tmp"
                prev = self.path.read_bytes()
                bfd = os.open(bak_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(bfd, prev)
                    os.fsync(bfd)
                finally:
                    os.close(bfd)
                os.replace(bak_tmp, d / "vault.enc.bak")
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # -- public ----------------------------------------------------------------------
    def load(self) -> dict:
        with self._mutex, self._flock(False):
            self.data = self._read()
            self._sig = self._signature()
        return self.data

    def refresh_if_changed(self) -> bool:
        """Reload if another process (e.g. the console) replaced the file."""
        sig = self._signature()
        if sig == self._sig:
            return False
        with self._mutex, self._flock(False):
            old = self.data
            self.data = self._read()
            self._sig = self._signature()
        events.console("VAULT_RELOADED")
        if self.on_reload:
            self.on_reload(old, self.data)
        return True

    def create(self, data: dict) -> None:
        with self._mutex, self._flock(True):
            if self.path.exists():
                raise VaultError("exists")
            self._salt = secrets.token_bytes(16)
            self._derived = None
            self._atomic_write(self._encode(data))
            self.data = data
            self._sig = self._signature()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """Exclusive read-modify-write. Yields a deep copy; saved only on success."""
        with self._mutex, self._flock(True):
            current = self._read()
            work = copy.deepcopy(current)
            yield work
            if work != current:
                self._atomic_write(self._encode(work))
            old = self.data
            self.data = work
            self._sig = self._signature()
        if self.on_reload and old is not None and old != work:
            self.on_reload(old, work)

    def snapshot(self) -> dict:
        self.refresh_if_changed()
        return self.data

    def rotate(self, new_key: MasterKey) -> Path:
        """Re-encrypt with a new key. Keeps the previous file (old key) as
        vault.enc.pre-rotation; on any failure the original stays in place."""
        with self._mutex, self._flock(True):
            current = self._read()
            original = self.path.read_bytes()
            new_salt = secrets.token_bytes(16)
            blob = self._encode(current, key=new_key, salt=new_salt)
            # verify before switching
            if self.decrypt_bytes(blob, new_key) != current:
                raise VaultError("verify_failed")
            backup = self.path.parent / "vault.enc.pre-rotation"
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, original)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                old_key = self.key
                self.key = new_key
                self._salt, self._derived = new_salt, None
                self._atomic_write(blob)
                if self._read() != current:
                    raise VaultError("verify_failed")
            except BaseException:
                self.key = old_key
                self._salt, self._derived = None, None
                tmp = self.path.parent / ".vault.rollback.tmp"
                tmp.write_bytes(original)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
                self._read()
                raise
            self._sig = self._signature()
            return backup


# --- helpers working on the vault dict ---------------------------------------------

def audit(data: dict, actor: str, event: str, **params: Any) -> None:
    """Append an English audit entry (never secrets)."""
    entry = {"ts": utcnow(), "actor": actor, "event": event, "message": events.render(event, {"actor": actor, **params}), "params": params}
    data.setdefault("audit", []).append(entry)
    if len(data["audit"]) > AUDIT_MAX:
        del data["audit"][: len(data["audit"]) - AUDIT_MAX]


def default_vault(instance_lang: str) -> dict:
    return {
        "format": 1,
        "instance_id": secrets.token_hex(8),
        "created_at": utcnow(),
        "setup_complete": False,
        "settings": default_settings(instance_lang),
        "security": default_security(),
        "database": None,
        "previous_databases": [],
        "users": {},
        "groups": {},
        "audit": [],
        "auth_epoch": 0,
        "pending_transfers": {},
    }


def default_settings(lang: str = "en") -> dict:
    return {
        "default_lang": lang,
        "organize": {"no_date_folder": "No_date", "duplicates_folder": "Duplicates"},
        "scan": {
            "exclude": [".*", "@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found", "Thumbs.db", "desktop.ini"],
            "follow_symlinks": False,
        },
        "metadata": {"fields": ["datetime_original", "create_date", "offset", "gps", "make", "model", "orientation", "title", "description"]},
        "previews": {"transcode_height": 720, "crf": 28},
    }


def default_security() -> dict:
    return {"password_min_length": 12, "session_hours": 8, "idle_minutes": 30, "login_attempts_before_delay": 3}
