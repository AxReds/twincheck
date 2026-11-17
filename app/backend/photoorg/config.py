"""Runtime configuration read from environment variables.

Every variable the application reads is declared in ``ENV_PARAMS``.  The test
``test_docs_consistency.py`` checks that each declared variable is documented in
the README files and that every documented variable exists here (or in the
compose-only list of the Docker README), so documentation and code never drift.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

# name -> (default, required, description)
ENV_PARAMS: dict[str, tuple[str | None, bool, str]] = {
    "APP_MASTER_KEY": (None, True, "Master key (32 random bytes, hex or base64) or a long passphrase used to encrypt /state/vault.enc."),
    "APP_MASTER_KEY_FILE": (None, False, "Path of a file (e.g. a Docker secret) holding the master key; alternative to APP_MASTER_KEY."),
    "APP_LANG": ("en", False, "Language of console commands and initial default language of the instance."),
    "HOST_LABEL": (None, False, "Host name shown in the header; falls back to the container host name."),
    "ALLOWED_NETWORKS": ("", False, "Comma-separated CIDR list allowed to reach the app. Empty = all. Loopback is always allowed."),
    "TRUSTED_PROXIES": ("", False, "Comma-separated CIDR list of reverse proxies whose X-Forwarded-For is trusted for login rate limiting only."),
    "DISK_WARN_FREE_PERCENT": ("15", False, "Free-space percentage under which disks are shown with a warning colour."),
    "DISK_CRIT_FREE_PERCENT": ("5", False, "Free-space percentage under which disks are shown with a critical colour."),
    "DATA_DIR": ("/data", False, "Mount point of the photo libraries inside the container."),
    "STATE_DIR": ("/state", False, "Mount point of the state volume inside the container."),
    "APP_PORT": ("80", False, "Internal HTTP port the server listens on inside the container."),
    "MAX_REQUEST_KB": ("1024", False, "Maximum size of a normal API request body in KiB."),
    "MAX_UPLOAD_MB": ("102400", False, "Maximum size in MiB of a migration archive uploaded from the browser."),
    "MARIADB_PASSWORD_FILE": (None, False, "File (Docker secret) holding the MariaDB password; overrides the one stored in the vault."),
    "JOB_WORKERS": ("2", False, "Number of background jobs that may run at the same time."),
    "PREVIEW_CACHE_MAX_MB": ("2048", False, "Maximum size of the preview cache in /state/cache before old entries are pruned."),
    "LOG_LEVEL": ("INFO", False, "Container log level (DEBUG, INFO, WARNING, ERROR)."),
    "EXIFTOOL_PATH": ("exiftool", False, "ExifTool executable."),
    "FFMPEG_PATH": ("ffmpeg", False, "ffmpeg executable."),
}

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


def env(name: str) -> str | None:
    default = ENV_PARAMS[name][0]
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def parse_networks(value: str | None) -> list:
    nets = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


@dataclass
class Settings:
    data_dir: Path
    state_dir: Path
    port: int
    app_lang: str
    host_label: str | None
    allowed_networks: list = field(default_factory=list)
    trusted_proxies: list = field(default_factory=list)
    disk_warn: float = 15.0
    disk_crit: float = 5.0
    max_request_bytes: int = 1024 * 1024
    max_upload_bytes: int = 100 * 1024 ** 3
    job_workers: int = 2
    preview_cache_max_bytes: int = 2 * 1024 ** 3
    log_level: str = "INFO"
    exiftool: str = "exiftool"
    ffmpeg: str = "ffmpeg"
    frontend_dir: Path = Path(__file__).resolve().parents[2] / "frontend"

    @property
    def vault_path(self) -> Path:
        return self.state_dir / "vault.enc"

    @property
    def locales_dir(self) -> Path:
        return self.frontend_dir / "locales"

    @property
    def state_locales_dir(self) -> Path:
        return self.state_dir / "locales"

    @property
    def exports_dir(self) -> Path:
        return self.state_dir / "exports"

    @property
    def imports_dir(self) -> Path:
        return self.state_dir / "imports"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def default_sqlite_path(self) -> Path:
        return self.state_dir / "photoorg.sqlite3"


def load_settings() -> Settings:
    def as_int(name):
        return int(env(name))

    return Settings(
        data_dir=Path(env("DATA_DIR")),
        state_dir=Path(env("STATE_DIR")),
        port=as_int("APP_PORT"),
        app_lang=env("APP_LANG") or "en",
        host_label=env("HOST_LABEL"),
        allowed_networks=parse_networks(env("ALLOWED_NETWORKS")),
        trusted_proxies=parse_networks(env("TRUSTED_PROXIES")),
        disk_warn=float(env("DISK_WARN_FREE_PERCENT")),
        disk_crit=float(env("DISK_CRIT_FREE_PERCENT")),
        max_request_bytes=as_int("MAX_REQUEST_KB") * 1024,
        max_upload_bytes=as_int("MAX_UPLOAD_MB") * 1024 * 1024,
        job_workers=max(1, as_int("JOB_WORKERS")),
        preview_cache_max_bytes=as_int("PREVIEW_CACHE_MAX_MB") * 1024 * 1024,
        log_level=(env("LOG_LEVEL") or "INFO").upper(),
        exiftool=env("EXIFTOOL_PATH"),
        ffmpeg=env("FFMPEG_PATH"),
    )


def ensure_state_dirs(s: Settings) -> None:
    """Create the sub-directories of /state. Never changes ownership."""
    for d in (s.state_dir, s.exports_dir, s.imports_dir, s.state_locales_dir, s.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
