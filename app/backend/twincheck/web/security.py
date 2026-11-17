"""Sessions, CSRF, login throttling, network allow-list and security headers."""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time

from ..config import LOOPBACK

SESSION_COOKIE = "tc_session"
LANG_COOKIE = "tc_lang"

SECURITY_HEADERS = {
    "content-security-policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                                "media-src 'self' blob:; connect-src 'self'; font-src 'self'; manifest-src 'self'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def ip_in(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return any(addr in n for n in nets)


def network_allowed(peer_ip: str, allowed: list) -> bool:
    """ALLOWED_NETWORKS is applied to the direct peer address of every request.
    Loopback is always allowed (healthcheck inside the container)."""
    if ip_in(peer_ip, LOOPBACK):
        return True
    if not allowed:
        return True
    return ip_in(peer_ip, allowed)


def real_client_ip(peer_ip: str, xff: str | None, trusted: list) -> str:
    """Only used for login throttling: walk X-Forwarded-For from the right while
    the hop is a trusted proxy."""
    if not trusted or not xff or not ip_in(peer_ip, trusted):
        return peer_ip
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not ip_in(hop, trusted):
            try:
                ipaddress.ip_address(hop)
                return hop
            except ValueError:
                return peer_ip
    return peer_ip


class SessionStore:
    """In-memory sessions (one container process). Tokens are stored hashed."""

    def __init__(self):
        self._s: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, user: dict) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        sess = {"user_id": user["id"], "created": now, "last": now, "epoch": user.get("session_epoch", 0),
                "csrf": secrets.token_urlsafe(24)}
        with self._lock:
            self._s[self._h(token)] = sess
        return token, sess

    def get(self, token: str | None, max_age: float, idle: float) -> dict | None:
        if not token:
            return None
        h = self._h(token)
        now = time.time()
        with self._lock:
            sess = self._s.get(h)
            if not sess:
                return None
            if now - sess["created"] > max_age or now - sess["last"] > idle:
                self._s.pop(h, None)
                return None
            sess["last"] = now
            return sess

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._s.pop(self._h(token), None)

    def drop_user(self, user_id: str) -> int:
        with self._lock:
            keys = [k for k, v in self._s.items() if v["user_id"] == user_id]
            for k in keys:
                del self._s[k]
            return len(keys)

    def count(self) -> int:
        return len(self._s)


class LoginLimiter:
    """Failures per IP and per user name; after ``threshold`` failures every new
    attempt waits 2^(n-threshold) seconds (max 15 minutes)."""

    def __init__(self):
        self._f: dict[str, list] = {}
        self._lock = threading.Lock()
        self.epoch = 0

    def sync_epoch(self, epoch: int) -> None:
        if epoch != self.epoch:
            self.reset()
            self.epoch = epoch

    def retry_after(self, keys: list[str]) -> int:
        now = time.time()
        with self._lock:
            wait = 0.0
            for k in keys:
                v = self._f.get(k)
                if v and v[1] > now:
                    wait = max(wait, v[1] - now)
            return int(wait + 0.999)

    def failure(self, keys: list[str], threshold: int) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                v = self._f.setdefault(k, [0, 0.0])
                v[0] += 1
                if v[0] >= threshold:
                    v[1] = now + min(2 ** (v[0] - threshold), 900)

    def success(self, keys: list[str]) -> None:
        with self._lock:
            for k in keys:
                self._f.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._f.clear()
