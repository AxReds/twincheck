"""Setup wizard, authentication, network restrictions, headers, roles, isolation."""
import os
import json
import re
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.parse import quote

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
        cookie = [x for x in c.last_set_cookies if x.startswith("tc_session=")][0]
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

    def test_relogin_with_a_live_session_cookie(self):
        # a browser that still holds a valid cookie but lost the CSRF token (reload,
        # second tab) must be able to sign in again; the old session is dropped
        c = self.inst.client()
        self.assertEqual(c.login("admin", STRONG).status, 200)
        old_cookie = dict(c.cookies)
        r = c.post("/api/login", {"username": "admin", "password": STRONG}, csrf=False)
        self.assertEqual(r.status, 200, r.text)
        self.assertIn("csrf", r.json())
        stale = self.inst.client()
        stale.cookies = old_cookie
        self.assertEqual(stale.get("/api/me").status, 401)
        # the custom header is still required
        r = c.post("/api/login", {"username": "admin", "password": STRONG}, csrf=False, headers={"x-requested-with": None})
        self.assertEqual(r.status, 403)

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
        self.assertEqual(c.get("/static/../backend/twincheck/config.py").status, 404)
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
        self.assertEqual(self.ops.patch("/api/admin/settings", {"organize": {"no_date_folder": "No_date"}}).status, 200)
        self.assertEqual(self.ops.patch("/api/admin/settings", {"default_lang": "en"}).status, 403)
        self.assertEqual(self.ops.post("/api/admin/groups", {"name": "family"}).status, 201)

    def test_every_admin_endpoint_denies_base_users(self):
        from twincheck.web.app import router

        checked = 0
        for route in router.routes:
            if not route.regex.pattern.startswith("^/api/admin/"):
                continue
            path = route.regex.pattern.removeprefix("^").removesuffix("$")
            path = re.sub(r"\(\?P<[^>]+>[^)]+\)", "1", path).replace("\\", "")
            with self.subTest(method=route.method, path=path):
                self.assertIsNotNone(route.match(path), "test must exercise the actual registered route")
                response = self.alice.request(route.method, path, {})
                self.assertEqual(response.status, 403, response.text)
                self.assertEqual(response.json()["error"], "forbidden")
            checked += 1
        self.assertGreater(checked, 0)

    def test_admin_can_assign_own_source(self):
        admin_id = self.admin.get("/api/bootstrap").json()["user"]["id"]
        path = self.inst.data / "admin-own"
        path.mkdir()
        try:
            response = self.admin.patch(f"/api/admin/users/{admin_id}", {"sources": [{"path": "admin-own"}]})
            self.assertEqual(response.status, 200, response.text)
            source = self.admin.get("/api/sources").json()["sources"][0]
            self.assertEqual(source["path"], "admin-own")
            self.assertEqual(self.ops.patch(f"/api/admin/users/{admin_id}", {"sources": []}).status, 403)
            self.assertEqual(self.alice.patch(f"/api/admin/users/{admin_id}", {"sources": []}).status, 403)
        finally:
            self.assertEqual(self.admin.patch(f"/api/admin/users/{admin_id}", {"sources": []}).status, 200)

    def test_scan_initial_selection_is_accessible_and_user_scoped(self):
        first = self.alice.get("/api/sources").json()
        self.assertEqual(first["scan_selection"]["path"], "alice")
        self.assertEqual(first["scan_selection"]["source_id"], first["sources"][0]["id"])
        nested = self.inst.data / "carol" / "last-scan"
        nested.mkdir(exist_ok=True)
        admin_id = self.admin.get("/api/bootstrap").json()["user"]["id"]
        admin_job = self.inst.core.jobs.submit("scan", admin_id, {"source_id": "adhoc", "root": str(nested),
                                                               "dest": str(nested), "roots": [str(nested)]})
        wait_job(self.inst.core, admin_job)
        self.assertEqual(self.admin.get("/api/sources").json()["scan_selection"]["path"], "carol/last-scan")
        self.assertEqual(self.alice.get("/api/sources").json()["scan_selection"]["path"], "alice",
                         "another user's newer job must not affect selection")
        nested.rmdir()
        self.assertIsNone(self.admin.get("/api/sources").json()["scan_selection"],
                          "inaccessible last scan and no assigned source means no selection")
        (self.inst.data / "admin-fallback").mkdir()
        response = self.admin.patch(f"/api/admin/users/{admin_id}", {"sources": [{"path": "admin-fallback"}]})
        self.assertEqual(response.status, 200, response.text)
        try:
            self.assertEqual(self.admin.get("/api/sources").json()["scan_selection"]["path"], "admin-fallback",
                             "an inaccessible last scan falls back to the first assigned source")
        finally:
            self.assertEqual(self.admin.patch(f"/api/admin/users/{admin_id}", {"sources": []}).status, 200)
        with self.inst.core.vault.transaction() as data:
            data["users"][self.alice_id]["sources"][0]["path"] = "missing-test-folder"
        try:
            self.assertIsNone(self.alice.get("/api/sources").json()["scan_selection"])
        finally:
            with self.inst.core.vault.transaction() as data:
                data["users"][self.alice_id]["sources"][0]["path"] = "alice"

    def test_theme_preference_is_validated_persistent_and_user_scoped(self):
        for theme in ("dark", "light", "auto"):
            response = self.alice.post("/api/me/theme", {"theme": theme})
            self.assertEqual(response.status, 200, response.text)
            self.assertEqual(self.alice.get("/api/bootstrap").json()["user"]["theme"], theme)
        self.assertEqual(self.alice.post("/api/me/theme", {"theme": "invalid"}).status, 400)
        self.assertEqual(self.alice.post("/api/me/theme", {"theme": ["dark"]}).status, 400)
        self.assertEqual(self.alice.post("/api/me/theme", {"theme": "dark", "user_id": self.bob_id}).status, 200)
        self.assertEqual(self.bob.get("/api/me").json()["theme"], "auto")
        self.alice.post("/api/logout")
        self.assertEqual(self.alice.post("/api/me/theme", {"theme": "light"}).status, 401)
        self.assertEqual(self.alice.login("alice", self.alice.password).status, 200)
        self.assertEqual(self.alice.get("/api/bootstrap").json()["user"]["theme"], "dark")
        self.assertEqual(self.inst.core.vault.data["users"][self.alice_id]["theme"], "dark")
        self.alice.post("/api/me/theme", {"theme": "auto"})

    def test_isolation_between_users(self):
        a_src = self.alice.get("/api/sources").json()["sources"][0]["id"]
        b_src = self.bob.get("/api/sources").json()["sources"][0]["id"]
        wait_job(self.inst.core, self.alice.post("/api/scan", {"source_id": a_src}).json()["job_id"])
        wait_job(self.inst.core, self.bob.post("/api/scan", {"source_id": b_src}).json()["job_id"])
        # alice cannot scan bob's source nor an arbitrary path
        self.assertEqual(self.alice.post("/api/scan", {"source_id": b_src}).status, 403)
        self.assertEqual(self.alice.post("/api/scan", {"source_id": a_src, "path": "bob"}).status, 403)
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

    def test_base_user_browses_and_scans_only_assigned_source_subfolders(self):
        inst = self.inst
        admin = self.admin
        for name in ("mine/child/deeper", "mine-dest", "second", "foreign"):
            (inst.data / name).mkdir(parents=True)
        photo = inst.data / "mine/child/deeper/sample.jpg"
        photo.write_bytes(b"synthetic scan fixture")
        os.symlink(inst.data / "foreign", inst.data / "mine/escape")
        os.symlink("/etc", inst.data / "mine/outside")
        owner, base = make_user(admin, "subfolder-user", [{"path": "mine", "dest": "mine-dest"}, {"path": "second"}])
        self.addCleanup(lambda: self.assertEqual(admin.delete("/api/admin/users/" + owner).status, 200))
        sources = base.get("/api/sources").json()
        self.assertTrue(sources["can_browse"])
        sid = sources["sources"][0]["id"]
        sibling = inst.data / "mine/sibling.jpg"
        sibling.write_bytes(b"synthetic sibling")
        (inst.data / "mine-dest/destination.jpg").write_bytes(b"synthetic destination")
        first_job = base.post("/api/scan", {"source_id": sid}).json()["job_id"]
        self.assertEqual(wait_job(inst.core, first_job)["status"], "completed")
        inst.core.db.execute("UPDATE files SET sha256=NULL, phase_hash='pending' WHERE owner_id=? AND cur_path=?", (owner, str(sibling)))
        sibling.rename(inst.data / "mine/sibling-moved.jpg")
        for path, names in (("mine", ["child"]), ("mine/child", ["deeper"])):
            response = base.get("/api/fs/browse?path=" + quote(path, safe=""))
            self.assertEqual(response.status, 200, response.text)
            self.assertEqual(response.json()["dirs"], names)
            self.assertEqual(response.json()["root"], "mine")
        response = base.post("/api/scan", {"source_id": sid, "path": "mine/child"})
        self.assertEqual(response.status, 202, response.text)
        job = wait_job(inst.core, response.json()["job_id"])
        self.assertEqual(job["status"], "completed")
        saved = inst.core.db.query_one("SELECT params, result FROM jobs WHERE id=?", (response.json()["job_id"],))
        self.assertEqual(json.loads(saved["params"])["roots"], [str(inst.data / "mine/child")])
        self.assertEqual(json.loads(saved["result"]), {"indexed": 1, "missing": 0, "hashed": 0, "reused": 1, "unhashed": 0})
        self.assertEqual(inst.core.db.scalar("SELECT COUNT(*) FROM files WHERE owner_id=?", (owner,)), 3)
        outside = inst.core.db.query_one("SELECT present, phase_hash, last_scan_job FROM files WHERE owner_id=? AND cur_path=?", (owner, str(sibling)))
        self.assertEqual(outside, {"present": 1, "phase_hash": "pending", "last_scan_job": first_job})
        self.assertEqual(base.get("/api/sources").json()["scan_selection"], {"path": "mine/child", "source_id": sid})
        for bad in ("", "foreign", "mine-dest", "../foreign", "mine/../../etc", "/etc",
                    str(inst.data / "foreign"), "mine/escape", "mine/outside"):
            with self.subTest(path=bad):
                listed = base.get("/api/fs/browse?path=" + quote(bad, safe=""))
                scanned = base.post("/api/scan", {"source_id": sid, "path": bad})
                self.assertIn(listed.status, (400, 403), listed.text)
                self.assertIn(scanned.status, (400, 403), scanned.text)
        self.assertEqual(base.post("/api/scan", {"source_id": sid, "path": "second"}).status, 403)
        self.assertEqual(base.get("/api/fs/browse?path=second").status, 200)
        self.assertEqual(admin.get("/api/fs/browse").status, 200)
        response = admin.post("/api/scan", {"path": "foreign"})
        self.assertEqual(response.status, 202, response.text)
        self.assertEqual(wait_job(inst.core, response.json()["job_id"])["status"], "completed")

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
    def test_required_password_change_flows_and_responsive_server(self):
        inst = Instance().start()
        try:
            inst.setup()
            admin = inst.admin()
            r = admin.post("/api/admin/users", {"username": "required", "password": STRONG, "sources": []})
            self.assertEqual(r.status, 201, r.text)
            uid = r.json()["id"]
            user = inst.client()
            self.assertEqual(user.login("required", STRONG).status, 200)
            self.assertTrue(user.get("/api/me").json()["must_change_password"])
            self.assertEqual(user.get("/api/sysinfo").json()["error"], "password_change_required")

            wrong = user.post("/api/me/password", {
                "current": "wrong", "password": STRONG + "1", "password_confirm": STRONG + "1",
            })
            self.assertEqual(wrong.json()["error"], "bad_current_password")
            self.assertTrue(user.get("/api/me").json()["must_change_password"])
            weak = user.post("/api/me/password", {
                "current": STRONG, "password": "weak", "password_confirm": "weak",
            })
            self.assertEqual(weak.json()["error"], "weak_password")
            self.assertTrue(user.get("/api/me").json()["must_change_password"])

            changed = STRONG + "1"
            self.assertEqual(user.post("/api/me/password", {
                "current": STRONG, "password": changed, "password_confirm": changed,
            }).status, 200)
            self.assertFalse(user.get("/api/me").json()["must_change_password"])
            self.assertEqual(user.get("/api/sysinfo").status, 200)

            failures = inst.client(ip="10.10.10.10")
            for _ in range(5):
                failures.post("/api/login", {"username": "required", "password": "wrong"})
            self.assertGreater(inst.core.limiter.retry_after(["user:required"]), 0)
            observer = inst.admin()
            hashing = threading.Event()
            release = threading.Event()
            from twincheck.web import routes_admin
            real_reset_hash = routes_admin.hash_password

            def slow_reset_hash(password):
                hashing.set()
                release.wait(5)
                return real_reset_hash(password)

            with mock.patch.object(routes_admin, "hash_password", slow_reset_hash):
                with ThreadPoolExecutor(max_workers=2) as executor, self.assertLogs("twincheck", level="INFO") as logs:
                    resetting = executor.submit(admin.post, f"/api/admin/users/{uid}/password")
                    self.assertTrue(hashing.wait(2), "reset password hashing did not start")
                    other = executor.submit(observer.post, "/api/me/theme", {"theme": "dark"})
                    try:
                        self.assertEqual(other.result(timeout=2).status, 200,
                                         "another user's request waited for admin reset hashing")
                    finally:
                        release.set()
                    reset = resetting.result(timeout=5)
            self.assertEqual(reset.status, 200, reset.text)
            temporary = reset.json()["generated_password"]
            self.assertTrue(any("[PASSWORD_RESET]" in line and "required" in line and "admin" in line for line in logs.output))
            self.assertNotIn(temporary, "\n".join(logs.output))
            self.assertIn("PASSWORD_RESET", [item["event"] for item in inst.core.vault.data["audit"]])
            relogin = inst.client(ip="10.10.10.10")
            self.assertEqual(relogin.login("required", temporary).status, 200)
            self.assertTrue(relogin.get("/api/me").json()["must_change_password"])

            from twincheck.web import app as web_app
            hashing = threading.Event()
            release = threading.Event()
            real_hash = web_app.hash_password

            def slow_hash(password):
                hashing.set()
                release.wait(5)
                return real_hash(password)

            with mock.patch.object(web_app, "hash_password", slow_hash):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    changing = executor.submit(relogin.post, "/api/me/password", {
                        "current": temporary, "password": changed + "2", "password_confirm": changed + "2",
                    })
                    self.assertTrue(hashing.wait(2), "password hashing did not start")
                    other = executor.submit(admin.post, "/api/me/theme", {"theme": "dark"})
                    try:
                        self.assertEqual(other.result(timeout=2).status, 200,
                                         "another user's request waited for password hashing")
                    finally:
                        release.set()
                    self.assertEqual(changing.result(timeout=5).status, 200)
            self.assertEqual(inst.client().get("/healthz").status, 200)
        finally:
            inst.cleanup()

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
            self.assertEqual(c.get("/api/logs?event=PASSWORD_RESET_CLI").json()["items"], [])
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


class UserScopeIsolation(unittest.TestCase):
    def test_stale_same_owner_rows_are_hidden_and_rejected_at_execution(self):
        inst = Instance().start()
        try:
            inst.setup()
            admin = inst.admin()
            source = inst.data / "userone" / "source"
            source.mkdir(parents=True)
            payload = b"identical synthetic scope fixture"
            outside_names = ["03.jpg", "IMG_3437.JPG"]
            (source / "inside.jpg").write_bytes(payload)
            for name in outside_names:
                (inst.data / name).write_bytes(payload)

            scan = admin.post("/api/scan", {"path": ""})
            self.assertEqual(scan.status, 202, scan.text)
            wait_job(inst.core, scan.json()["job_id"])
            r = admin.post("/api/admin/users", {
                "username": "userone", "password": STRONG,
                "sources": [{"path": "userone/source", "dest": "userone/source"}],
            })
            self.assertEqual(r.status, 201, r.text)
            uid = r.json()["id"]
            user = inst.client()
            self.assertEqual(user.login("userone", STRONG).status, 200)
            changed = STRONG + "x"
            self.assertEqual(user.post("/api/me/password", {
                "current": STRONG, "password": changed, "password_confirm": changed,
            }).status, 200)
            own_scan = user.post("/api/scan", {"source_id": user.get("/api/sources").json()["sources"][0]["id"]})
            self.assertEqual(own_scan.status, 202, own_scan.text)
            wait_job(inst.core, own_scan.json()["job_id"])

            outside = inst.core.db.query_one(
                "SELECT * FROM files WHERE owner_id=(SELECT owner_id FROM files WHERE owner_id<>? LIMIT 1) "
                "AND cur_path=?",
                (uid, str(inst.data / outside_names[0])),
            )
            if not outside:
                outside = inst.core.db.query_one("SELECT * FROM files WHERE cur_path=?", (str(inst.data / outside_names[0]),))
            self.assertIsNotNone(outside)
            inst.core.db.execute("UPDATE files SET owner_id=? WHERE id=?", (uid, outside["id"]))
            from twincheck.db.manager import log
            log(inst.core.db, "FILE_HASHED", owner_id=uid, path=outside["cur_path"], sha256="private-out-of-scope")

            analysis = user.post("/api/duplicates/analyze")
            self.assertEqual(analysis.status, 202, analysis.text)
            wait_job(inst.core, analysis.json()["job_id"])
            response = user.get("/api/duplicates")
            body = response.text
            self.assertEqual(response.status, 200)
            for secret in outside_names:
                self.assertNotIn(secret, body)
                self.assertNotIn(secret, user.get("/api/duplicates.csv").text)
            self.assertEqual(response.json()["total"], 0)
            self.assertEqual(response.json()["recoverable"], 0)

            for endpoint in ("/api/report", "/api/review", "/api/logs?limit=1000", "/api/jobs?limit=1000",
                             "/api/manifest.csv", "/api/duplicates.csv"):
                with self.subTest(endpoint=endpoint):
                    text = user.get(endpoint).text
                    self.assertNotIn("03.jpg", text)
                    self.assertNotIn("IMG_3437.JPG", text)
                    self.assertNotIn("private-out-of-scope", text)

            meta = user.post("/api/metadata/analyze")
            self.assertEqual(meta.status, 202, meta.text)
            wait_job(inst.core, meta.json()["job_id"])
            outside["owner_id"] = uid
            self.assertEqual(user.get(f"/api/files/{outside['id']}/content").status, 404)
            self.assertEqual(user.get(f"/api/files/{outside['id']}/preview").status, 404)

            review = user.get("/api/review?limit=1000").json()
            self.assertFalse(any(item["file_id"] == outside["id"] for item in review["items"]))

            plan_payload = {
                "source": outside["cur_path"], "file_id": outside["id"],
                "path": "../03.jpg", "confirm": True,
            }
            self.assertIn(user.post("/api/duplicates/plan", plan_payload).status, (202, 404))
            plans = inst.core.db.query(
                "SELECT * FROM plan_items WHERE file_id=? AND owner_id=?", (outside["id"], uid))
            self.assertFalse(plans, "an out-of-scope file must never enter a plan")

            original = (source / "inside.jpg")
            duplicate = source / "copy.jpg"
            duplicate.write_bytes(payload)
            user.post("/api/scan", {"source_id": user.get("/api/sources").json()["sources"][0]["id"]})
            for job in inst.core.db.query(
                    "SELECT id FROM jobs WHERE owner_id=? AND kind='scan' ORDER BY id DESC LIMIT 1", (uid,)):
                wait_job(inst.core, job["id"])
            analyze = user.post("/api/duplicates/analyze")
            wait_job(inst.core, analyze.json()["job_id"])
            group = user.get("/api/duplicates").json()["groups"][0]
            inside_ids = {member["id"] for member in group["members"]}
            self.assertNotIn(outside["id"], inside_ids)
            preview = user.post("/api/duplicates/plan")
            wait_job(inst.core, preview.json()["job_id"])
            plan_id = preview.json()["job_id"]
            item = inst.core.db.query_one(
                "SELECT * FROM plan_items WHERE plan_job_id=? AND status='planned' LIMIT 1", (plan_id,))
            self.assertIsNotNone(item)

            with inst.core.vault.transaction() as data:
                old = data["users"][uid]["sources"][0]
                (inst.data / "userone" / "other").mkdir(exist_ok=True)
                old["path"] = "userone/other"
                old["dest"] = "userone/other"
            confirm = user.post(f"/api/plans/{plan_id}/confirm", {"confirm": True})
            self.assertEqual(confirm.status, 202, confirm.text)
            applied = wait_job(inst.core, confirm.json()["job_id"])
            self.assertEqual(applied["status"], "completed")
            self.assertTrue(os.path.exists(original) and os.path.exists(duplicate))
            skipped = inst.core.db.query_one("SELECT status, note FROM plan_items WHERE id=?", (item["id"],))
            self.assertEqual((skipped["status"], skipped["note"]), ("skipped", "outside_source"))

            admin_scope = admin.get("/api/duplicates?scope=global")
            self.assertEqual(admin_scope.status, 200)
        finally:
            inst.cleanup()


if __name__ == "__main__":
    unittest.main()
