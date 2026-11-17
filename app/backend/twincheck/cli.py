"""Container entry point and console commands.

    serve                         run the web application (default)
    generate-key                  print a new random master key for the .env file
    reset-admin-password [--user NAME] [--generate]
    healthcheck                   used by the Docker HEALTHCHECK

Console texts are in English, or in the language given by APP_LANG.  Log
lines are always English.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import urllib.request

from . import events
from .config import load_settings
from .crypto import KeyError_, generate_key, load_master_key
from .i18n import ConsoleText


def _text():
    s = load_settings()
    return s, ConsoleText(s.app_lang, s.locales_dir, s.state_locales_dir)


def _key_or_exit(t) -> object:
    try:
        return load_master_key()
    except KeyError_ as exc:
        events.console("KEY_MISSING" if exc.code == "missing" else "KEY_INVALID", level=40, reason=exc.code)
        print(t("cli.key_missing"), file=sys.stderr)
        print("  docker compose run --rm app generate-key", file=sys.stderr)
        sys.exit(2)


def cmd_generate_key(_args) -> int:
    _, t = _text()
    key = generate_key()
    print(t("cli.generate_key_intro"))
    print()
    print(f"APP_MASTER_KEY={key}")
    print()
    print(t("cli.generate_key_warning"))
    return 0


def cmd_reset_password(args) -> int:
    from .db.manager import log, make_database
    from .crypto import DecryptError
    from .passwords import check_strength, generate_password, hash_password
    from .vault import Vault, audit

    s, t = _text()
    key = _key_or_exit(t)
    vault = Vault(s.vault_path, key)
    if not vault.exists():
        print(t("cli.no_vault"), file=sys.stderr)
        return 3
    try:
        data = vault.load()
    except DecryptError:
        events.console("VAULT_DECRYPT_FAILED", level=40)
        print(t("cli.wrong_key"), file=sys.stderr)
        return 3
    username = args.user or "admin"
    user = next((u for u in data["users"].values() if u["username"].lower() == username.lower()), None)
    if not user:
        print(t("cli.user_not_found", username=username), file=sys.stderr)
        return 4
    minlen = int(data.get("security", {}).get("password_min_length", 12))
    if user["role"] == "admin":
        minlen = max(minlen, 12)
    if args.generate:
        password = generate_password()
    else:
        if not sys.stdin.isatty():
            print(t("cli.needs_tty"), file=sys.stderr)
            return 5
        password = getpass.getpass(t("cli.new_password") + " ")
        again = getpass.getpass(t("cli.confirm_password") + " ")
        if password != again:
            print(t("cli.password_mismatch"), file=sys.stderr)
            return 6
        errs = check_strength(password, user["username"], minlen)
        if errs:
            print(t("cli.password_weak", min_length=minlen), file=sys.stderr)
            for e in errs:
                print("  - " + t(f"error.password_{e}", min_length=minlen), file=sys.stderr)
            return 6
    # same lock as the application; the running app detects the change and reloads
    with vault.transaction() as d:
        u = d["users"][user["id"]]
        u["password_hash"] = hash_password(password)
        u["must_change_password"] = bool(args.generate)
        u["session_epoch"] = u.get("session_epoch", 0) + 1          # invalidates every session of the user
        u["active"] = True if u["role"] == "admin" else u.get("active", True)
        d["auth_epoch"] = d.get("auth_epoch", 0) + 1                 # clears login throttling
        audit(d, "console", "PASSWORD_RESET_CLI", username=u["username"])
    events.console("PASSWORD_RESET_CLI", username=user["username"])
    params = data.get("database")
    if params:
        try:
            db = make_database(params, os.environ.get("MARIADB_PASSWORD_FILE"))
            log(db, "PASSWORD_RESET_CLI", actor="console", username=user["id"])   # ids only in the operational DB
            db.close()
        except Exception:
            pass
    if args.generate:
        print(t("cli.generated_password_once"))
        print()
        print(password)
        print()
        print(t("cli.change_at_first_login"))
    else:
        print(t("cli.password_reset_done", username=user["username"]))
    return 0


def cmd_healthcheck(_args) -> int:
    s = load_settings()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s.port}/healthz", timeout=4) as r:
            return 0 if r.status == 200 else 1
    except Exception:
        return 1


def cmd_serve(_args) -> int:
    import uvicorn

    from .core import App, StartupError, started_message
    from .web.app import create_app

    s, t = _text()
    events.setup_logging(s.log_level)
    key = _key_or_exit(t)
    core = App(s, key)
    try:
        core.startup()
    except StartupError as exc:
        if exc.code == "vault_decrypt_failed":
            print(t("cli.wrong_key"), file=sys.stderr)
        return 3
    started_message()
    app = create_app(core)
    config = uvicorn.Config(app, host="0.0.0.0", port=s.port, http="h11", ws="none", lifespan="on", loop="asyncio",
                            server_header=False, proxy_headers=False, access_log=True, log_config=None,
                            timeout_keep_alive=5, limit_concurrency=256, log_level=s.log_level.lower())
    uvicorn.Server(config).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="twincheck")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    sub.add_parser("generate-key")
    r = sub.add_parser("reset-admin-password")
    r.add_argument("--user", default=None)
    r.add_argument("--generate", action="store_true")
    sub.add_parser("healthcheck")
    args = p.parse_args(argv or ["serve"])
    return {"serve": cmd_serve, "generate-key": cmd_generate_key, "reset-admin-password": cmd_reset_password,
            "healthcheck": cmd_healthcheck}[args.cmd or "serve"](args)
