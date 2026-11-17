"""Test helpers: in-process ASGI client, temporary instance, sample library.

Only the standard library is used, so the suite runs anywhere (``python -m
unittest``) and also under pytest.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from http.cookies import SimpleCookie
from pathlib import Path

sys.dont_write_bytecode = True
APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "backend"))

from twincheck import events  # noqa: E402
from twincheck.config import load_settings  # noqa: E402
from twincheck.crypto import generate_key, parse_key  # noqa: E402

events.setup_logging("WARNING")
STRONG = "Correct-Horse-Battery-9"


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


class Client:
    """Minimal ASGI client with a cookie jar and CSRF handling."""

    def __init__(self, app, ip="127.0.0.1", host="localhost:7777"):
        self.app = app
        self.ip = ip
        self.host = host
        self.cookies: dict[str, str] = {}
        self.csrf = ""
        self.last_set_cookies: list[str] = []

    def request(self, method, path, body=None, headers=None, raw=None, ip=None, csrf=True):
        path, _, qs = path.partition("?")
        hdrs = {"host": self.host, "x-requested-with": "twincheck"}
        if csrf and self.csrf:
            hdrs["x-csrf-token"] = self.csrf
        if self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        if body is not None:
            hdrs["content-type"] = "application/json"
        hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
        scope = {"type": "http", "method": method, "path": path, "query_string": qs.encode(), "client": (ip or self.ip, 50000),
                 "headers": [(k.encode(), v.encode()) for k, v in hdrs.items() if v is not None], "scheme": "http",
                 "http_version": "1.1", "server": ("127.0.0.1", 80)}
        out = {"status": None, "headers": [], "body": b""}
        chunks = [data[i:i + 65536] for i in range(0, len(data), 65536)] or [b""]

        async def receive():
            if chunks:
                c = chunks.pop(0)
                return {"type": "http.request", "body": c, "more_body": bool(chunks)}
            await asyncio.sleep(3600)

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = msg["headers"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        headers_l = [(k.decode().lower(), v.decode()) for k, v in out["headers"]]
        self.last_set_cookies = [v for k, v in headers_l if k == "set-cookie"]
        for c in self.last_set_cookies:
            sc = SimpleCookie()
            sc.load(c)
            for k, m in sc.items():
                if m.value:
                    self.cookies[k] = m.value
                else:
                    self.cookies.pop(k, None)
        return Resp(out["status"], dict(headers_l), out["body"])

    def get(self, p, **kw):
        return self.request("GET", p, **kw)

    def post(self, p, body=None, **kw):
        return self.request("POST", p, body if body is not None else {}, **kw)

    def patch(self, p, body=None, **kw):
        return self.request("PATCH", p, body or {}, **kw)

    def delete(self, p, **kw):
        return self.request("DELETE", p, {}, **kw)

    def login(self, username, password):
        r = self.post("/api/login", {"username": username, "password": password})
        if r.status == 200:
            self.csrf = r.json()["csrf"]
        return r


def exiftool_for_tests(tmpdir: str) -> tuple[str, dict]:
    """Real ExifTool if installed (container test-suite), otherwise the fake one."""
    real = shutil.which("exiftool")
    if real and not os.environ.get("PHOTOORG_FAKE_EXIFTOOL"):
        return real, {}
    dst = os.path.join(tmpdir, "fake_exiftool")
    shutil.copy(Path(__file__).with_name("fake_exiftool.py"), dst)
    os.chmod(dst, 0o755)
    db = os.path.join(tmpdir, "fake_exif.json")
    os.environ["FAKE_EXIF_DB"] = db
    return dst, {"FAKE_EXIF_DB": db}


def using_fake_exiftool() -> bool:
    return not shutil.which("exiftool") or bool(os.environ.get("PHOTOORG_FAKE_EXIFTOOL"))


class Instance:
    """A complete application instance in a temporary directory."""

    def __init__(self, key: str | None = None, env: dict | None = None, data_dir: str | None = None):
        self.tmp = tempfile.mkdtemp(prefix="twincheck-test-")
        self.data = Path(data_dir or os.path.join(self.tmp, "data"))
        self.state = Path(self.tmp) / "state"
        self.data.mkdir(parents=True, exist_ok=True)
        self.key = key or generate_key()
        exiftool, exenv = exiftool_for_tests(self.tmp)
        self.env = {"EXIFTOOL_PATH": exiftool, **exenv, "DATA_DIR": str(self.data), "STATE_DIR": str(self.state), "APP_MASTER_KEY": self.key, "JOB_WORKERS": "2",
                    "LOG_LEVEL": "WARNING", **(env or {})}
        self.core = None
        self.app = None

    def start(self):
        from twincheck.core import App
        from twincheck.web.app import create_app
        old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            settings = load_settings()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.core = App(settings, parse_key(self.key))
        self.core.startup()
        self.app = create_app(self.core)
        return self

    def client(self, **kw):
        return Client(self.app, **kw)

    def setup(self, db=None, password=STRONG):
        c = self.client()
        r = c.post("/api/setup/complete", {"token": self.core.setup_token, "database": db or {"type": "sqlite"},
                                           "password": password, "password_confirm": password, "lang": "en"})
        assert r.status == 200, r.text
        return c

    def admin(self):
        c = self.client()
        assert c.login("admin", STRONG).status == 200
        return c

    def stop(self):
        if self.core:
            self.core.shutdown()
            self.core = None

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def wait_job(core, job_id, timeout=60, states=("completed", "failed", "cancelled", "paused", "interrupted")):
    end = time.time() + timeout
    while time.time() < end:
        row = core.db.query_one("SELECT status, error FROM jobs WHERE id=?", (job_id,))
        if row and row["status"] in states:
            return row
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def make_user(admin: Client, username, sources, role="user"):
    r = admin.post("/api/admin/users", {"username": username, "role": role, "password": STRONG, "sources": sources})
    assert r.status == 201, r.text
    uid = r.json()["id"]
    c = admin.__class__(admin.app)
    assert c.login(username, STRONG).status == 200
    # first sign-in requires a password change
    new = STRONG + "x"
    assert c.post("/api/me/password", {"current": STRONG, "password": new, "password_confirm": new}).status == 200
    c.password = new
    return uid, c


def write(path: Path, data: bytes, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def jpeg_bytes(seed: int, size=(32, 24)) -> bytes:
    """A small, valid JPEG without EXIF (Pillow if present, else a fixed minimal JPEG + marker)."""
    try:
        import io
        from PIL import Image
        img = Image.new("RGB", size, ((seed * 40) % 256, (seed * 90) % 256, (seed * 150) % 256))
        b = io.BytesIO()
        img.save(b, "JPEG", quality=80)
        return b.getvalue()
    except Exception:
        return b"\xff\xd8\xff\xe0" + seed.to_bytes(4, "big") * 64 + b"\xff\xd9"


def sample_library(root: Path) -> dict:
    """Sample folder required by the specification:
    duplicates in different folders, files without EXIF, date conflicts (via
    sidecar), sidecars, same name different content, a Live Photo pair."""
    a = jpeg_bytes(1)
    b = jpeg_bytes(2)
    c = jpeg_bytes(3)
    t0 = 1_600_000_000
    files = {
        "dup_1": write(root / "2019 trip" / "IMG_0001.jpg", a, t0),
        "dup_2": write(root / "backup" / "copy" / "IMG_0001 (1).jpg", a, t0 + 86400 * 30),
        "dup_3": write(root / "phone" / "DSC_9.jpg", a, t0 + 86400 * 60),
        "noexif": write(root / "misc" / "scan_without_exif.jpg", b, t0),
        "samename_1": write(root / "x" / "photo.jpg", c, t0),
        "samename_2": write(root / "y" / "photo.jpg", jpeg_bytes(4), t0),
        "named_date": write(root / "misc" / "IMG_20210704_101500.jpg", jpeg_bytes(5), t0),
        "sidecar_img": write(root / "side" / "pic.jpg", jpeg_bytes(6), t0),
        "sidecar_xmp": write(root / "side" / "pic.xmp", b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", t0),
        "sidecar_aae": write(root / "side" / "pic.aae", b"<plist/>", t0),
        "live_img": write(root / "live" / "IMG_7000.jpg", jpeg_bytes(7), t0),
        "live_mov": write(root / "live" / "IMG_7000.mov", b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64, t0),
        "unicode": write(root / "ünïcødé" / "日本語 фото.jpg", jpeg_bytes(8), t0),
    }
    return files
