"""Documentation and code stay consistent: every documented parameter exists and
every parameter is documented; required README files exist; the language table
matches the locale files; no secrets or real data are committed."""
import json
import re
import unittest
from pathlib import Path

from helpers import APP_DIR

from twincheck.config import ENV_PARAMS

ROOT = APP_DIR.parent
COMPOSE_ONLY = {"BIND_ADDRESS", "PUID", "PGID", "DATA_PATH", "STATE_PATH", "TMP_SIZE", "CONTAINER_NAME", "ALPINE_VERSION", "APP_VERSION"}
IMAGE_INTERNAL = {"DATA_DIR", "STATE_DIR", "APP_PORT", "EXIFTOOL_PATH", "FFMPEG_PATH"}


def table_names(readme: Path) -> set[str]:
    text = readme.read_text(encoding="utf-8")
    block = text.split("<!-- params:start -->")[1].split("<!-- params:end -->")[0]
    return {m.group(1) for m in re.finditer(r"^\|\s*`([A-Z0-9_]+)`\s*\|", block, re.M)}


def repository_files():
    """Files that would be committed: tracked plus untracked-but-not-ignored.

    Uses Git when available so local virtualenvs, caches and other ignored
    paths (e.g. a .venv with pip's cacert.pem) are not reported.  Without Git
    it walks the tree and skips the usual local-only directories.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True).stdout
        return [ROOT / n for n in out.decode("utf-8").split("\0") if n and (ROOT / n).is_file()]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
        return [p for p in ROOT.rglob("*") if p.is_file() and not skip.intersection(p.relative_to(ROOT).parts)]


@unittest.skipUnless((ROOT / "docker" / "README.md").exists(), "repository files not available (running inside the image)")
class DocsConsistency(unittest.TestCase):
    def test_required_readmes_exist(self):
        for p in ("README.md", "CONTRIBUTING.md", "LICENSE", ".gitignore", ".env.example", "app/README.md",
                  "app/backend/README.md", "app/frontend/README.md", "app/frontend/locales/README.md", "app/tests/README.md",
                  "docker/README.md", "docker/Dockerfile", "docker/docker-compose.yml", "compose.yaml"):
            self.assertTrue((ROOT / p).exists(), p)

    def test_backend_readme_matches_env_params(self):
        self.assertEqual(table_names(APP_DIR / "backend" / "README.md"), set(ENV_PARAMS))

    def test_docker_readme_matches_code_and_compose(self):
        documented = table_names(ROOT / "docker" / "README.md")
        expected = (set(ENV_PARAMS) - IMAGE_INTERNAL) | COMPOSE_ONLY
        self.assertEqual(documented, expected)
        compose = (ROOT / "docker" / "docker-compose.yml").read_text()
        used = set(re.findall(r"\$\{([A-Z0-9_]+)", compose))
        self.assertEqual(used, documented, "every compose variable is documented and vice versa")
        env_block = compose.split("environment:")[1].split("volumes:")[0]
        passed = set(re.findall(r"^\s+([A-Z0-9_]+):", env_block, re.M))
        self.assertEqual(passed, set(ENV_PARAMS) - IMAGE_INTERNAL, "every app parameter is passed to the container")
        example = (ROOT / ".env.example").read_text()
        for name in documented:
            self.assertRegex(example, rf"(?m)^#?\s*{name}=", f"{name} missing in .env.example")

    def test_compose_hardening(self):
        for name in ("docker-compose.yml", "portainer-stack.yml"):
            c = (ROOT / "docker" / name).read_text()
            for needle in ('"${BIND_ADDRESS:-0.0.0.0}:7777:80"', "APP_MASTER_KEY: ${APP_MASTER_KEY:?", "read_only: true", "- ALL",
                           "no-new-privileges:true", "net.ipv4.ip_unprivileged_port_start: 0", "user: \"${PUID", "/tmp:",
                           'test: ["CMD", "twincheck-healthcheck"]'):
                self.assertIn(needle, c, name)
            for forbidden in ("privileged: true", "network_mode", "docker.sock", "cap_add", "devices:"):
                self.assertNotIn(forbidden, c, name)

    def test_portainer_stack_matches_compose(self):
        def env_keys(text):
            return set(re.findall(r"^\s+([A-Z0-9_]+):", text.split("environment:")[1].split("volumes:")[0], re.M))
        compose = (ROOT / "docker" / "docker-compose.yml").read_text()
        stack = (ROOT / "docker" / "portainer-stack.yml").read_text()
        self.assertEqual(env_keys(stack), env_keys(compose), "same parameters passed to the container")
        self.assertNotIn("build:", stack, "the stack uses the published image")
        self.assertRegex(stack, r"(?m)^\s+image: ghcr\.io/[a-z0-9-]+/twincheck:\$\{APP_VERSION:-[0-9.]+\}$")
        used = set(re.findall(r"\$\{([A-Z0-9_]+)", stack))
        self.assertLessEqual(used, table_names(ROOT / "docker" / "README.md"), "every stack variable is documented")

    def test_dockerfile_scripts_exist(self):
        d = (ROOT / "docker" / "Dockerfile").read_text()
        names = set(re.findall(r"/usr/local/bin/([A-Za-z0-9_.-]+)", d))
        names |= set(re.findall(r'(?:ENTRYPOINT|CMD) \["(twincheck-[a-z-]+)"', d))
        self.assertTrue(names)
        for n in names:
            self.assertTrue((ROOT / "docker" / "bin" / n).is_file(), f"docker/bin/{n} referenced by the Dockerfile")
        d = (ROOT / "docker" / "Dockerfile").read_text()
        self.assertIn("apk add --no-cache python3 py3-cryptography exiftool ffmpeg", d)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", d)
        self.assertNotRegex(d, r"APP_MASTER_KEY\s*=", "no key in the image")
        self.assertNotRegex(d, r"(?m)^\s*RUN .*(su-exec|gosu|chown -R \$)", "no privilege switch")

    def test_language_table(self):
        locs = {p.stem: json.loads(p.read_text(encoding="utf-8"))["meta"] for p in (APP_DIR / "frontend" / "locales").glob("*.json")}
        for readme in ("README.md",):
            rows = dict(re.findall(r"^\| `([A-Za-z-]+)` \| [^|]+ \| ([a-z-]+)", (ROOT / readme).read_text(encoding="utf-8"), re.M))
            self.assertEqual(set(rows), set(locs), readme)
            for code, status in rows.items():
                expected = "complete" if code == "en" else locs[code]["status"]
                self.assertEqual(status if code != "en" else status, expected, f"{readme} {code}")

    def test_no_secrets_or_data_committed(self):
        bad_ext = {".jpg", ".jpeg", ".heic", ".mov", ".mp4", ".sqlite3", ".db", ".tcarc", ".enc", ".pem", ".key"}
        for p in repository_files():
            self.assertNotIn(p.suffix.lower(), bad_ext, str(p))
            self.assertNotEqual(p.name, ".env", str(p))
        example = (ROOT / ".env.example").read_text()
        self.assertRegex(example, r"(?m)^APP_MASTER_KEY=CHANGE_ME$")
        self.assertNotRegex(example, r"[0-9a-f]{64}")


if __name__ == "__main__":
    unittest.main()
