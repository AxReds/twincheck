"""Master-key handling and authenticated encryption helpers.

Only primitives from the ``cryptography`` package are used (AES-256-GCM, HKDF,
scrypt); nothing is implemented by hand.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KDF_HKDF = 1
KDF_SCRYPT = 2
MIN_PASSPHRASE = 20
SCRYPT_N = 2 ** 15


class KeyError_(Exception):
    """Raised when the master key is missing or malformed (reason is an English code)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DecryptError(Exception):
    """Wrong key, tampered or truncated data."""


@dataclass(frozen=True)
class MasterKey:
    raw: bytes            # 32 random bytes, or the UTF-8 passphrase
    is_passphrase: bool

    def derive(self, salt: bytes, info: bytes) -> tuple[int, bytes]:
        if self.is_passphrase:
            k = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1).derive(self.raw)
            return KDF_SCRYPT, HKDF(hashes.SHA256(), 32, salt, info).derive(k)
        return KDF_HKDF, HKDF(hashes.SHA256(), 32, salt, info).derive(self.raw)

    def fingerprint(self) -> str:
        """Non-secret identifier used only to tell keys apart in messages."""
        k = HKDF(hashes.SHA256(), 8, b"photoorg-fp", b"fingerprint").derive(self.raw)
        return k.hex()


def generate_key() -> str:
    return secrets.token_bytes(32).hex()


def parse_key(value: str | None) -> MasterKey:
    if value is None:
        raise KeyError_("missing")
    value = value.strip()
    if not value:
        raise KeyError_("missing")
    if len(value) == 64:
        try:
            return MasterKey(binascii.unhexlify(value), False)
        except binascii.Error:
            pass
    if len(value) in (43, 44):
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_" if ("-" in value or "_" in value) else None, validate=True)
            if len(raw) == 32:
                return MasterKey(raw, False)
        except (binascii.Error, ValueError):
            pass
    if len(value) < MIN_PASSPHRASE:
        raise KeyError_("too_short")
    return MasterKey(value.encode("utf-8"), True)


def load_master_key(environ: dict | None = None) -> MasterKey:
    environ = os.environ if environ is None else environ
    direct = environ.get("APP_MASTER_KEY")
    path = environ.get("APP_MASTER_KEY_FILE")
    if direct and direct.strip() == "file":
        direct = None          # "APP_MASTER_KEY=file": read the key from APP_MASTER_KEY_FILE (Docker secret)
        if not path:
            raise KeyError_("missing")
    if direct and direct.strip():
        return parse_key(direct)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return parse_key(fh.read())
        except OSError:
            raise KeyError_("file_unreadable") from None
    raise KeyError_("missing")


# --- simple blob encryption (vault) -------------------------------------------------

def encrypt_blob(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt_blob(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise DecryptError("authentication failed") from None


# --- chunked stream encryption (migration archives) ---------------------------------

STREAM_MAGIC = b"POARC\x00\x01\n"
CHUNK = 1024 * 1024


def passphrase_key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise KeyError_("missing")
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1).derive(passphrase.encode("utf-8"))


class StreamEncryptor:
    """AES-256-GCM in independent chunks; the last chunk is flagged so that
    truncation and reordering are detected."""

    def __init__(self, fh, passphrase: str):
        self.fh = fh
        self.salt = secrets.token_bytes(16)
        self.prefix = secrets.token_bytes(7)
        self.key = passphrase_key(passphrase, self.salt)
        self.header = STREAM_MAGIC + struct.pack(">I", SCRYPT_N) + self.salt + self.prefix
        self.fh.write(self.header)
        self.counter = 0
        self.buf = bytearray()
        self.aes = AESGCM(self.key)

    def _emit(self, data: bytes, final: bool):
        nonce = self.prefix + struct.pack(">I", self.counter) + (b"\x01" if final else b"\x00")
        aad = self.header + struct.pack(">I", self.counter) + (b"\x01" if final else b"\x00")
        ct = self.aes.encrypt(nonce, bytes(data), aad)
        self.fh.write(struct.pack(">I", len(ct)) + (b"\x01" if final else b"\x00") + ct)
        self.counter += 1

    def write(self, data: bytes):
        self.buf.extend(data)
        while len(self.buf) >= CHUNK:
            self._emit(self.buf[:CHUNK], False)
            del self.buf[:CHUNK]

    def close(self):
        self._emit(self.buf, True)
        self.buf = bytearray()
        self.fh.flush()


class StreamDecryptor:
    """File-like reader returning the decrypted plaintext."""

    def __init__(self, fh, passphrase: str):
        self.fh = fh
        head = fh.read(len(STREAM_MAGIC) + 4 + 16 + 7)
        if len(head) != len(STREAM_MAGIC) + 27 or not head.startswith(STREAM_MAGIC):
            raise DecryptError("not an archive")
        n = struct.unpack(">I", head[len(STREAM_MAGIC):len(STREAM_MAGIC) + 4])[0]
        if n != SCRYPT_N:
            raise DecryptError("unsupported kdf")
        salt = head[len(STREAM_MAGIC) + 4:len(STREAM_MAGIC) + 20]
        self.prefix = head[len(STREAM_MAGIC) + 20:]
        self.header = head
        self.aes = AESGCM(passphrase_key(passphrase, salt))
        self.counter = 0
        self.done = False
        self.buf = b""

    def _next(self) -> bytes:
        meta = self.fh.read(5)
        if len(meta) < 5:
            raise DecryptError("truncated archive")
        length = struct.unpack(">I", meta[:4])[0]
        final = meta[4:5]
        if length > CHUNK + 64:
            raise DecryptError("bad chunk")
        ct = self.fh.read(length)
        if len(ct) != length:
            raise DecryptError("truncated archive")
        nonce = self.prefix + struct.pack(">I", self.counter) + final
        aad = self.header + struct.pack(">I", self.counter) + final
        try:
            pt = self.aes.decrypt(nonce, ct, aad)
        except InvalidTag:
            raise DecryptError("authentication failed") from None
        self.counter += 1
        if final == b"\x01":
            self.done = True
            if self.fh.read(1):
                raise DecryptError("trailing data")
        return pt

    def read(self, size: int = -1) -> bytes:
        while not self.done and (size < 0 or len(self.buf) < size):
            self.buf += self._next()
        if size < 0:
            out, self.buf = self.buf, b""
        else:
            out, self.buf = self.buf[:size], self.buf[size:]
        return out

    def readable(self):
        return True
