"""Password hashing (scrypt, individual salt) and strength policy."""
from __future__ import annotations

import base64
import hmac
import secrets
import string

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

N, R, P = 2 ** 15, 8, 1
COMMON = {
    "password", "password1", "passw0rd", "123456789012", "qwertyuiop", "letmein", "admin", "administrator",
    "welcome", "iloveyou", "changeme", "twincheck", "password123", "qwerty123456", "1234567890ab",
}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = Scrypt(salt=salt, length=32, n=N, r=R, p=P).derive(password.encode("utf-8"))
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # constant-ish work to avoid user enumeration through timing
        Scrypt(salt=b"0" * 16, length=32, n=N, r=R, p=P).derive(password.encode("utf-8"))
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        kdf = Scrypt(salt=base64.b64decode(salt), length=32, n=int(n), r=int(r), p=int(p))
        kdf.verify(password.encode("utf-8"), base64.b64decode(digest))
        return True
    except (InvalidKey, ValueError):
        return False


def check_strength(password: str, username: str = "", min_length: int = 12) -> list[str]:
    """Return a list of error codes (empty = acceptable). Codes map to i18n keys
    ``error.password_<code>``."""
    errors: list[str] = []
    if len(password) < min_length:
        errors.append("too_short")
    classes = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ])
    if classes < 3 and len(password) < 20:
        errors.append("too_simple")
    low = password.lower()
    if username and username.lower() in low:
        errors.append("contains_username")
    if low in COMMON:
        errors.append("common")
    return errors


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!@#%+="
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if not check_strength(pw, min_length=length):
            return pw


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
