"""Minimal ASGI toolkit (standard library only): request parsing, routing,
JSON / file / streaming responses with HTTP Range support.

Synchronous handlers run in a thread pool so blocking I/O (SQLite, disk) never
blocks the event loop.  Handlers flagged ``raw=True`` are ``async`` and receive
the ASGI ``receive`` callable to stream large uploads to disk.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="http")


class HTTPError(Exception):
    def __init__(self, status: int, code: str, **params: Any):
        super().__init__(code)
        self.status = status
        self.code = code
        self.params = params


class Request:
    def __init__(self, scope: dict, body: bytes = b""):
        self.scope = scope
        self.method: str = scope["method"]
        self.path: str = scope["path"]
        self.raw_query = scope.get("query_string", b"").decode("latin-1")
        self.query = {k: v[-1] for k, v in parse_qs(self.raw_query, keep_blank_values=True).items()}
        hdrs: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            key = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            hdrs[key] = f"{hdrs[key]}, {val}" if key in hdrs and key == "x-forwarded-for" else val
        self.headers = hdrs
        self.cookies: dict[str, str] = {}
        if "cookie" in hdrs:
            c = SimpleCookie()
            try:
                c.load(hdrs["cookie"])
                self.cookies = {k: m.value for k, m in c.items()}
            except Exception:
                self.cookies = {}
        client = scope.get("client") or ("0.0.0.0", 0)
        self.peer_ip: str = client[0]
        self.client_ip: str = client[0]      # may be replaced by the trusted-proxy logic
        self.body = body
        self.path_params: dict[str, Any] = {}
        self.user: dict | None = None
        self.session: dict | None = None
        self.lang: str = "en"

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPError(400, "bad_json") from None


class Response:
    def __init__(self, body: bytes | str = b"", status: int = 200, content_type: str = "text/plain; charset=utf-8",
                 headers: dict | None = None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers: dict[str, str] = {"content-type": content_type}
        self.cookies: list[str] = []
        if headers:
            self.headers.update({k.lower(): v for k, v in headers.items()})

    async def send(self, send: Callable, head_only: bool = False) -> None:
        h = dict(self.headers)
        h["content-length"] = str(len(self.body))
        await send({"type": "http.response.start", "status": self.status, "headers": self._hdrs(h)})
        await send({"type": "http.response.body", "body": b"" if head_only else self.body})

    def _hdrs(self, h: dict) -> list:
        out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in h.items()]
        out += [(b"set-cookie", c.encode("latin-1")) for c in self.cookies]
        return out

    def set_cookie(self, name: str, value: str, max_age: int | None = None, http_only: bool = True,
                   same_site: str = "Strict", path: str = "/") -> None:
        # No "Secure" flag on purpose: the application serves plain HTTP only.
        parts = [f"{name}={value}", f"Path={path}", f"SameSite={same_site}"]
        if http_only:
            parts.append("HttpOnly")
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        self.cookies.append("; ".join(parts))


class JSONResponse(Response):
    def __init__(self, data: Any, status: int = 200, headers: dict | None = None):
        super().__init__(json.dumps(data, ensure_ascii=False, default=str), status, "application/json; charset=utf-8", headers)


class StreamResponse(Response):
    """Body produced by a synchronous iterator executed in the thread pool."""

    def __init__(self, iterator_factory: Callable, status: int = 200, content_type: str = "application/octet-stream",
                 headers: dict | None = None, length: int | None = None):
        super().__init__(b"", status, content_type, headers)
        self.factory = iterator_factory
        self.length = length

    async def send(self, send: Callable, head_only: bool = False) -> None:
        h = dict(self.headers)
        if self.length is not None:
            h["content-length"] = str(self.length)
        await send({"type": "http.response.start", "status": self.status, "headers": self._hdrs(h)})
        if head_only:
            await send({"type": "http.response.body", "body": b""})
            return
        loop = asyncio.get_running_loop()
        it = await loop.run_in_executor(POOL, self.factory)
        sentinel = object()
        try:
            while True:
                chunk = await loop.run_in_executor(POOL, next, it, sentinel)
                if chunk is sentinel:
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        finally:
            close = getattr(it, "close", None)
            if close:
                close()
        await send({"type": "http.response.body", "body": b""})


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def file_response(path: str, request: Request, content_type: str | None = None, download_name: str | None = None,
                  extra: dict | None = None) -> Response:
    """Serve a file (with single-range support for video seeking)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        raise HTTPError(404, "not_found") from None
    ctype = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    headers = {"accept-ranges": "bytes", "cache-control": "private, no-store"}
    if download_name:
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", download_name)
        headers["content-disposition"] = f'attachment; filename="{safe}"'
    if extra:
        headers.update(extra)
    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if rng and size:
        m = RANGE_RE.match(rng.strip())
        if not m or (not m.group(1) and not m.group(2)):
            raise HTTPError(416, "bad_range")
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:
            start = max(0, size - int(m.group(2)))
        if start > end or start >= size:
            raise HTTPError(416, "bad_range")
        status = 206
        headers["content-range"] = f"bytes {start}-{end}/{size}"

    def factory():
        def gen():
            with open(path, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    b = fh.read(min(256 * 1024, left))
                    if not b:
                        break
                    left -= len(b)
                    yield b
        return gen()

    return StreamResponse(factory, status, ctype, headers, length=(end - start + 1) if size else 0)


class Route:
    def __init__(self, method: str, pattern: str, handler: Callable, **opts: Any):
        self.method = method
        self.handler = handler
        self.opts = opts
        regex = "^"
        self.converters: dict[str, Callable] = {}
        for part in re.split(r"(\{[^}]+\})", pattern):
            if part.startswith("{"):
                name, _, conv = part[1:-1].partition(":")
                if conv == "int":
                    regex += rf"(?P<{name}>\d+)"
                    self.converters[name] = int
                elif conv == "path":
                    regex += rf"(?P<{name}>.+)"
                else:
                    regex += rf"(?P<{name}>[^/]+)"
            else:
                regex += re.escape(part)
        self.regex = re.compile(regex + "$")

    def match(self, path: str) -> dict | None:
        m = self.regex.match(path)
        if not m:
            return None
        out = {}
        for k, v in m.groupdict().items():
            v = unquote(v)
            out[k] = self.converters.get(k, str)(v)
        return out


class Router:
    def __init__(self):
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, **opts: Any):
        def deco(fn):
            self.routes.append(Route(method, pattern, fn, **opts))
            return fn
        return deco

    def get(self, pattern, **o):
        return self.add("GET", pattern, **o)

    def post(self, pattern, **o):
        return self.add("POST", pattern, **o)

    def put(self, pattern, **o):
        return self.add("PUT", pattern, **o)

    def patch(self, pattern, **o):
        return self.add("PATCH", pattern, **o)

    def delete(self, pattern, **o):
        return self.add("DELETE", pattern, **o)

    def resolve(self, method: str, path: str) -> tuple[Route | None, dict, bool]:
        """-> (route, params, path_exists_with_other_method)"""
        other = False
        for r in self.routes:
            params = r.match(path)
            if params is None:
                continue
            if r.method == method or (method == "HEAD" and r.method == "GET"):
                return r, params, True
            other = True
        return None, {}, other


async def read_body(receive: Callable, limit: int) -> bytes:
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            raise HTTPError(400, "client_disconnected")
        b = msg.get("body", b"")
        size += len(b)
        if size > limit:
            raise HTTPError(413, "request_too_large")
        chunks.append(b)
        if not msg.get("more_body"):
            return b"".join(chunks)
