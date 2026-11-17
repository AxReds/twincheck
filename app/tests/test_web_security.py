"""Setup wizard, authentication, network restrictions, headers, roles, isolation."""
import os
import re
import time
import unittest
from pathlib import Path

from helpers import APP_DIR, STRONG, Client, Instance, make_user, sample_library, wait_job
from test_crypto_vault import run_cli


class SetupWizard(unittest.TestCase):
    def setUp(self):
        self.inst = Instance().start()

    def tearDown(self):
        self.inst.cleanup()

    def test_only_wizard_before_setup_and_locked_after(self):
        c = self.inst.client()
        b = c.get("/api/bootstrap").json()
        self.assertTrue(b["setup_required"])
        self.assertEqual(c.get("/api/sysinfo").status, 401, "no system data before login")
        self.assertEqual(c.post("/api/login", {"username": "admin", "password": "x"}).status, 409)
        self.assertEqual(c.post("/api/setup/check-token", {"token": "wrong"}).status, 403)
        r = c.post("/api/setup/complete", {"token": self.inst.core.setup_token, "database": {"type": "sqlite"},
                                           "password": "weak", "password_confirm": "weak"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json()["error"], "weak_password")
        self.assertFalse((self.inst.state / "vault.enc").exists(), "nothing written on failure")
        token = self.inst.core.setup_token
        self.inst.setup()
        self.assertTrue((self.inst.state / "vault.enc").exists())
        # wizard permanently locked
        for path in ("/api/setup/check-token", "/api/setup/test-db", "/api/setup/complete"):
            r = c.post(path, {"token": token, "database": {"type": "sqlite"}, "password": STRONG, "password_confirm": STRONG})
            self.assertEqual(r.status, 410, path)
        self.assertFalse(c.get("/api/bootstrap").json()["setup_required"])

    def test_setup_token_rate_limited(self):
        c = self.inst.client()
        codes = [c.post("/api/setup/check-token", {"token": "nope"}).status for _ in range(6)]
        self.assertIn(429, codes)

    def test_admin_fixed(self):
        self.inst.setup()
        a = self.inst.admin()
        users = a.get("/api/admin/users").json()["users"]
        admin = users[0]
        self.assertEqual(admin["username"], "admin")
        self.assertEqual(a.delete(f"/api/admin/users/{admin['id']}").status, 400)
        self.assertEqual(a.patch(f"/api/admin/users/{admin['id']}", {"active": False}).status, 400)
        self.assertEqual(a.post("/api/admin/users", {"username": "admin", "password": STRONG}).status, 400)


class HttpSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(env={"ALLOWED_NETWORKS": "192.168.10.0/24", "TRUSTED_PROXIES": "10.0.0.5/32"}).start()
        cls.inst.setup()

    @classmethod
    def tearDownClass(cls):
        cls.inst.cleanup()

    def test_cookie_flags_and_headers(self):
        c = self.inst.client()
        r = c.login("admin", STRONG)
        self.assertEqual(r.status, 200)
        cookie = [x for x in c.last_set_cookies if x.startswith("po_session=")][0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)
        r = c.get("/api/me")
        h = r.headers
        self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertNotIn("http:", h["content-security-policy"].replace("'self'", ""))
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertNotIn("strict-transport-security", h)

    def test_csrf(self):
        c = self.inst.client()
        c.login("admin", STRONG)
        token = c.csrf
        c.csrf = ""
        self.assertEqual(c.post("/api/me/lang", {"lang": "en"}).status, 403)
        c.csrf = token
        self.assertEqual(c.post("/api/me/lang", {"lang": "en"}, headers={"origin": "http://evil.example"}).status, 403)
        self.assertEqual(c.post("/api/me/lang", {"lang": "en"}).status, 200)
        anon = self.inst.client()
        self.assertEqual(anon.post("/api/login", {"username": "a", "password": "b"}, headers={"x-requested-with": None}).status, 403)

    def test_allowed_networks_applies_to_everything(self):
        out = self.inst.client(ip="203.0.113.9")
        for p in ("/", "/api/bootstrap", "/api/setup/check-token", "/static/js/app.js"):
            self.assertEqual(out.get(p).status, 403, p)
        self.assertEqual(out.post("/api/login", {"username": "admin", "password": STRONG}).status, 403)
        ok = self.inst.client(ip="192.168.10.20")
        self.assertEqual(ok.get("/api/bootstrap").status, 200)
        self.assertEqual(self.inst.client(ip="127.0.0.1").get("/healthz").status, 200, "loopback always allowed (healthcheck)")
        # X-Forwarded-For does not bypass ALLOWED_NETWORKS
        spoof = self.inst.client(ip="203.0.113.9")
        self.assertEqual(spoof.get("/api/bootstrap", headers={"x-forwarded-for": "192.168.10.20"}).status, 403)

    def test_login_throttling_per_user_and_ip_with_trusted_proxy(self):
        c = self.inst.client(ip="192.168.10.30")
        statuses = [c.post("/api/login", {"username": "admin", "password": "wrong"}).status for _ in range(5)]
        self.assertIn(429, statuses)
        r = c.post("/api/login", {"username": "admin", "password": STRONG})
        self.assertEqual(r.status, 429, "correct password also waits while throttled")
        self.assertIn("retry-after", r.headers)
        self.inst.core.limiter.reset()
        # behind a trusted proxy the real IP comes from X-Forwarded-For
        p = self.inst.client(ip="192.168.10.40")
        self.inst.core.settings.allowed_networks.append(__import__("ipaddress").ip_network("10.0.0.5/32"))
        try:
            proxy = self.inst.client(ip="10.0.0.5")
            for _ in range(4):
                proxy.post("/api/login", {"username": "nobody", "password": "x"}, headers={"x-forwarded-for": "198.51.100.7"})
            self.assertGreater(self.inst.core.limiter.retry_after(["ip:198.51.100.7"]), 0)
            self.assertEqual(self.inst.core.limiter.retry_after(["ip:10.0.0.5"]), 0)
        finally:
            self.inst.core.settings.allowed_networks.pop()
            self.inst.core.limiter.reset()
        self.assertEqual(p.login("admin", STRONG).status, 200)

    def test_static_no_listing_and_no_traversal(self):
        c = self.inst.client(ip="192.168.10.2")
        self.assertEqual(c.get("/static/").status, 404)
        self.assertEqual(c.get("/static/js/").status, 404)
        self.assertEqual(c.get("/static/../backend/photoorg/config.py").status, 404)
        self.assertEqual(c.get("/static/%2e%2e/%2e%2e/README.md").status, 404)
        self.assertEqual(c.get("/").status, 200)

    def test_request_size_limit(self):
        c = self.inst.client()
        c.login("admin", STRONG)
        r = c.request("POST", "/api/me/lang", raw=b"{" + b" " * (2 * 1024 * 1024) + b"}", headers={"content-type": "application/json"})
        self.assertEqual(r.status, 413)

    def test_no_tls_anywhere(self):
        """The application has no HTTPS code, parameters or certificates."""
        pattern = re.compile(r"ssl_|certfile|keyfile|ssl\.|SSLContext|HSTS|Strict-Transport|https_port|TLS_", re.I)
        for p in (APP_DIR / "backend").rglob("*.py"):
            self.assertIsNone(pattern.search(p.read_text()), str(p))
        compose_file = APP_DIR.parent / "docker" / "docker-compose.yml"
        if not compose_file.exists():
            return  # running inside the image: only the application code is checked
        compose = compose_file.read_text()
        self.assertNotIn("443", compose)
        self.assertIsNone(re.search(r"\.(pem|crt|key)\b", compose))


class Roles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = Instance().start()
        cls.inst.setup()
        cls.admin = cls.inst.admin()
        for d in ("alice", "bob", "carol", "shared/inner"):
            (cls.inst.data / d).mkdir(parents=True)
        sample_library(cls.inst.data / "alice")
        sample_library(cls.inst.data / "bob")
        cls.alice_id, cls.alice = make_user(cls.admin, "alice", [{"path": "alice"}])
        cls.bob_id, cls.bob = make_user(cls.admin, "bob", [{"path": "bob"}])
        cls.ops_id, cls.ops = make_user(cls.admin, "operator", [], role="administrator")

    @classmethod
    def tearDownClass(cls):
        cls.inst.cleanup()

    def test_permission_matrix(self):
        admin_only = [("POST", "/api/admin/transfer/preview", {"mode": "export"}), ("POST", "/api/admin/key/prepare", {}),
                      ("PATCH", "/api/admin/security", {"session_hours": 4}), ("GET", "/api/admin/archives", None),
                      ("POST", "/api/admin/global-dedupe/analyze", {})]
        admin_roles = [("GET", "/api/admin/users", None), ("GET", "/api/admin/settings", None), ("GET", "/api/admin/db", None),
                       ("GET", "/api/admin/audit", None), ("GET", "/api/fs/browse", None)]
        for m, p, b in admin_only:
            self.assertNotIn(self.admin.request(m, p, b).status, (401, 403), p)
            self.assertEqual(self.ops.request(m, p, b).status, 403, f"administrator {p}")
            self.assertEqual(self.alice.request(m, p, b).status, 403, f"user {p}")
        for m, p, b in admin_roles:
            self.assertEqual(self.ops.request(m, p, b).status, 200, p)
            self.assertEqual(self.alice.request(m, p, b).status, 403, p)
        for c in (self.admin, self.ops, self.alice):
            self.assertEqual(c.get("/api/sysinfo").status, 200, "every role sees system data")

    def test_administrator_limits(self):
        users = self.admin.get("/api/admin/users").json()["users"]
        admin_id = [u for u in users if u["username"] == "admin"][0]["id"]
        self.assertEqual(self.ops.post("/api/admin/users", {"username": "ops2", "role": "administrator", "password": STRONG}).status, 403)
        self.assertEqual(self.ops.patch(f"/api/admin/users/{admin_id}", {"groups": []}).status, 403)
        self.assertEqual(self.ops.delete(f"/api/admin/users/{admin_id}").status, 403)
        self.assertEqual(self.ops.post(f"/api/admin/users/{admin_id}/password").status, 403)
        self.assertEqual(self.ops.patch(f"/api/admin/users/{self.alice_id}", {"role": "administrator"}).status, 403)
        r = self.ops.post("/api/admin/users", {"username": "dave", "role": "user", "password": STRONG})
        self.assertEqual(r.status, 201)
        self.assertEqual(self.ops.patch("/api/admin/settings", {"organize": {"no_date_folder": "Senza_data"}}).status, 200)
        self.assertEqual(self.ops.patch("/api/admin/settings", {"default_lang": "en"}).status, 403)
        self.assertEqual(self.ops.post("/api/admin/groups", {"name": "family"}).status, 201)

    def test_isolation_between_users(self):
        a_src = self.alice.get("/api/sources").json()["sources"][0]["id"]
        b_src = self.bob.get("/api/sources").json()["sources"][0]["id"]
        wait_job(self.inst.core, self.alice.post("/api/scan", {"source_id": a_src}).json()["job_id"])
        wait_job(self.inst.core, self.bob.post("/api/scan", {"source_id": b_src}).json()["job_id"])
        # alice cannot scan bob's source nor an arbitrary path
        self.assertEqual(self.alice.post("/api/scan", {"source_id": b_src}).status, 403)
        self.assertEqual(self.alice.post("/api/scan", {"source_id": a_src, "path": "bob"}).status in (202, 409), True)
        bob_file = self.inst.core.db.query_one("SELECT id FROM files WHERE owner_id=?", (self.bob_id,))["id"]
        self.assertEqual(self.alice.get(f"/api/files/{bob_file}/content").status, 404)
        self.assertEqual(self.bob.get(f"/api/files/{bob_file}/content").status, 200)
        bob_job = self.inst.core.db.query_one("SELECT id FROM jobs WHERE owner_id=?", (self.bob_id,))["id"]
        self.assertEqual(self.alice.get(f"/api/jobs/{bob_job}").status, 404)
        self.assertEqual(self.alice.post(f"/api/jobs/{bob_job}/cancel").status, 404)
        self.assertTrue(all(j["owner_id"] == self.alice_id for j in self.alice.get("/api/jobs?owner=" + self.bob_id).json()["items"]))
        logs = self.alice.get("/api/logs?limit=1000").json()["items"]
        self.assertTrue(logs)
        self.assertFalse(any("/bob/" in (l["message"] or "") for l in logs))
        # header disks: alice only sees her own source, with relative paths
        disks = self.alice.get("/api/sysinfo").json()["disks"]
        self.assertEqual([d["source_path"] for d in disks], ["alice"])
        self.assertNotIn(str(self.inst.data), self.alice.get("/api/sysinfo").text)
        self.assertEqual(len(self.admin.get("/api/sysinfo").json()["disks"]), 2)
        self.assertIsNotNone(disks[0]["indexed_bytes"])

    def test_path_traversal_and_symlinks(self):
        os.symlink(self.inst.data / "bob", self.inst.data / "alice" / "escape")
        try:
            a_src = self.alice.get("/api/sources").json()["sources"][0]["id"]
            jid = self.alice.post("/api/scan", {"source_id": a_src}).json().get("job_id")
            if jid:
                wait_job(self.inst.core, jid)
            rows = self.inst.core.db.query("SELECT cur_path FROM files WHERE owner_id=?", (self.alice_id,))
            self.assertFalse(any("/escape/" in r["cur_path"] for r in rows), "symlinks are never followed")
            os.symlink("/etc", self.inst.data / "outlink")
            self.addCleanup(os.unlink, self.inst.data / "outlink")
            for bad in ("../bob", "alice/../../etc", "/etc", "outlink"):
                r = self.admin.get("/api/fs/browse?path=" + bad)
                self.assertEqual(r.status, 400, bad)
            r = self.admin.patch(f"/api/admin/users/{self.alice_id}", {"sources": [{"path": "../outside"}]})
            self.assertEqual(r.json()["error"], "path_traversal")
            r = self.admin.patch(f"/api/admin/users/{self.alice_id}", {"sources": [{"path": "outlink"}]})
            self.assertEqual((r.status, r.json()["error"]), (400, "outside_source"))
            # a symlink inside alice's source pointing to bob's folder resolves into bob's area -> overlap refused
            r = self.admin.patch(f"/api/admin/users/{self.alice_id}", {"sources": [{"path": "alice/escape"}]})
            self.assertEqual((r.status, r.json()["error"]), (400, "source_overlap_other_user"))
        finally:
            os.unlink(self.inst.data / "alice" / "escape")

    def test_overlapping_sources_rejected(self):
        r = self.admin.post("/api/admin/users", {"username": "eve", "password": STRONG, "sources": [{"path": "alice/side"}]})
        self.assertEqual(r.json()["error"], "source_overlap_other_user")
        (self.inst.data / "alice2").mkdir(exist_ok=True)
        r = self.admin.post("/api/admin/users", {"username": "eve", "password": STRONG, "sources": [{"path": "shared"}, {"path": "shared/inner"}]})
        self.assertEqual(r.json()["error"], "source_nested_own")
        r = self.admin.post("/api/admin/users", {"username": "eve", "password": STRONG, "sources": [{"path": "carol", "dest": "bob/x"}]})
        self.assertEqual(r.json()["error"], "source_overlap_other_user")
        self.assertEqual(self.admin.post("/api/admin/users", {"username": "eve", "password": STRONG, "sources": [{"path": "carol"}]}).status, 201)

    def test_group_deactivation_blocks_login(self):
        gid = self.admin.post("/api/admin/groups", {"name": "kids"}).json()["id"]
        self.admin.patch(f"/api/admin/groups/{gid}", {"members": [self.bob_id]})
        self.admin.patch(f"/api/admin/groups/{gid}", {"active": False})
        c = Client(self.inst.app)
        self.assertEqual(c.login("bob", self.bob.password).status, 401)
        self.admin.patch(f"/api/admin/groups/{gid}", {"active": True})
        self.inst.core.limiter.reset()
        self.assertEqual(c.login("bob", self.bob.password).status, 200)


class ResetPassword(unittest.TestCase):
    def test_reset_while_running(self):
        inst = Instance().start()
        try:
            inst.setup()
            a = inst.admin()
            self.assertEqual(a.get("/api/me").status, 200)
            # block admin logins with wrong passwords
            bad = inst.client(ip="10.9.9.9")
            for _ in range(6):
                bad.post("/api/login", {"username": "admin", "password": "wrong"})
            self.assertGreater(inst.core.limiter.retry_after(["user:admin"]), 0)
            env = {k: v for k, v in inst.env.items()}
            r = run_cli(["reset-admin-password", "--generate"], env)
            self.assertEqual(r.returncode, 0, r.stderr)
            pw = [l for l in r.stdout.splitlines() if l.strip()][1].strip()
            self.assertNotIn(pw, r.stderr)
            # running app reloads the vault: old session invalid, throttling cleared, change required
            self.assertEqual(a.get("/api/me").status, 401)
            c = inst.client()
            self.assertEqual(c.login("admin", pw).status, 200)
            self.assertEqual(c.get("/api/jobs").status, 403)
            self.assertEqual(c.get("/api/jobs").json()["error"], "password_change_required")
            audit = c.get("/api/me").json()
            self.assertTrue(audit["must_change_password"])
            new = STRONG + "!2"
            self.assertEqual(c.post("/api/me/password", {"current": pw, "password": new, "password_confirm": new}).status, 200)
            ev = [x["event"] for x in c.get("/api/admin/audit").json()["items"]]
            self.assertIn("PASSWORD_RESET_CLI", ev)
            body = c.get("/api/admin/audit").text + c.get("/api/logs?limit=1000").text
            self.assertNotIn(pw, body)
            self.assertIn("PASSWORD_RESET_CLI", c.get("/api/logs?event=PASSWORD_RESET_CLI").text)
            # another user via --user, app stopped
            (inst.data / "u1").mkdir()
            make_user(c, "someone", [{"path": "u1"}])
            inst.stop()
            r = run_cli(["reset-admin-password", "--user", "someone", "--generate"], env)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run_cli(["reset-admin-password", "--user", "ghost", "--generate"], env)
            self.assertEqual(r.returncode, 4)
            r = run_cli(["reset-admin-password"], env, stdin="x\nx\n")
            self.assertEqual(r.returncode, 5, "interactive input requires a TTY")
        finally:
            inst.cleanup()


if __name__ == "__main__":
    unittest.main()
