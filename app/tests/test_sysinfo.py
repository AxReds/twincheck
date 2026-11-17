"""Header metrics: values match the system, n/a when unavailable, cgroup limits."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import Instance, make_user

from photoorg import sysinfo
from photoorg.fsutil import mount_for, mounts


class Metrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(env={"HOST_LABEL": "nas-living-room"}).start()
        cls.inst.setup()
        cls.a = cls.inst.admin()

    @classmethod
    def tearDownClass(cls):
        cls.inst.cleanup()

    def test_values_match_system(self):
        m = self.a.get("/api/sysinfo").json()
        self.assertEqual(m["host"], "nas-living-room")
        self.assertEqual(m["arch"], os.uname().machine)
        self.assertEqual(m["kernel"], os.uname().release)
        nproc = len(os.sched_getaffinity(0))
        self.assertLessEqual(m["cpu"]["cores"], nproc)
        self.assertGreaterEqual(m["cpu"]["cores"], 1)
        st = os.statvfs(self.inst.data)
        self.assertEqual(m["volumes"]["data"]["total"], st.f_blocks * st.f_frsize, "same as df")
        self.assertAlmostEqual(m["volumes"]["data"]["free"], st.f_bavail * st.f_frsize, delta=64 * 1024 * 1024)
        meminfo = Path("/proc/meminfo").read_text()
        total = int(meminfo.split("MemTotal:")[1].split()[0]) * 1024
        self.assertLessEqual(m["memory"]["total"], total, "never more than free(1) reports")
        self.assertIsNotNone(m["uptime"]["container"])
        self.assertEqual(m["database"]["type"], "sqlite")
        self.assertTrue(m["database"]["connected"])
        self.assertIn("jobs_running", m)
        second = self.a.get("/api/sysinfo").json()
        self.assertTrue(second["cpu"]["percent"] is None or 0 <= second["cpu"]["percent"] <= 100)

    def test_not_available_is_none(self):
        with mock.patch.object(sysinfo, "_read", return_value=None), \
                mock.patch("photoorg.sysinfo.os.getloadavg", side_effect=OSError):
            m = sysinfo.system(self.inst.core.settings, {}, 0)
        self.assertIsNone(m["os"])
        self.assertIsNone(m["memory"]["total"])
        self.assertIsNone(m["cpu"]["load"])
        v = sysinfo.volume("/nonexistent/path", 10, 5)
        self.assertEqual((v["total"], v["level"]), (None, "unknown"))

    def test_cgroup_memory_limit_respected(self):
        files = {"/sys/fs/cgroup/memory.max": "268435456", "/sys/fs/cgroup/memory.current": "100000000",
                 "/sys/fs/cgroup/memory.stat": "inactive_file 10000000", "/proc/meminfo": "MemTotal: 16000000 kB\nMemAvailable: 8000000 kB"}
        with mock.patch.object(sysinfo, "_read", side_effect=lambda p: files.get(p)):
            mem = sysinfo.memory()
        self.assertEqual(mem["total"], 268435456)
        self.assertEqual(mem["used"], 90000000)
        self.assertTrue(mem["cgroup_limited"])
        with mock.patch.object(sysinfo, "_read", side_effect=lambda p: {"/sys/fs/cgroup/cpu.max": "150000 100000"}.get(p)):
            self.assertLessEqual(sysinfo.cpu_count(), 2)

    def test_disk_levels_and_read_only_mount(self):
        self.assertEqual(sysinfo.level(50, 15, 5), "ok")
        self.assertEqual(sysinfo.level(10, 15, 5), "warning")
        self.assertEqual(sysinfo.level(2, 15, 5), "critical")
        table = [{"mount_point": "/", "options": ["rw"], "fstype": "overlay", "source": ""},
                 {"mount_point": "/srv/ro", "options": ["ro", "nosuid"], "fstype": "ext4", "source": "/dev/sdb1"}]
        self.assertEqual(mount_for("/srv/ro/x", table)["fstype"], "ext4")
        with mock.patch("photoorg.sysinfo.os.path.exists", return_value=True), \
                mock.patch("photoorg.sysinfo.disk_usage", return_value={"total": 100, "used": 95, "free": 5, "used_pct": 95.0, "free_pct": 5.0, "read_only": False}):
            v = sysinfo.volume("/srv/ro/photos", 15, 5, table)
        self.assertTrue(v["read_only"])
        self.assertEqual(v["level"], "warning")
        self.assertIsInstance(mounts(), list)

    def test_different_volumes_detected(self):
        areas = [{"id": "s1", "label": "a", "path": str(self.inst.data), "dest": "/proc"}]
        out = sysinfo.source_disks(self.inst.core.settings, areas, {}, lambda p: p)
        self.assertFalse(out[0]["same_volume"])
        self.assertIsNone(out[0]["indexed_bytes"], "n/a before the first scan")
        areas = [{"id": "s1", "label": "a", "path": str(self.inst.data), "dest": str(self.inst.data)}]
        self.assertTrue(sysinfo.source_disks(self.inst.core.settings, areas, {"s1": 10}, lambda p: p)[0]["same_volume"])

    def test_not_before_login(self):
        self.assertEqual(self.inst.client().get("/api/sysinfo").status, 401)


class FrontendAssets(unittest.TestCase):
    def test_offline_assets_and_logo(self):
        from helpers import APP_DIR
        static = APP_DIR / "frontend" / "static"
        for f in static.rglob("*"):
            if f.suffix in (".html", ".js", ".css", ".svg"):
                txt = f.read_text(encoding="utf-8")
                self.assertNotRegex(txt, r"https?://(?!www\.w3\.org/2000/svg)", f"external resource in {f}")
        logo = (static / "img" / "logo.svg").read_text()
        self.assertIn("<svg", logo)
        self.assertTrue((static / "img" / "favicon.svg").exists())
        css = (static / "css" / "app.css").read_text()
        self.assertNotRegex(css, r"(?<!-)\b(margin|padding)-(left|right)\b", "logical properties only (RTL)")
        self.assertNotRegex(css, r"\b(left|right)\s*:", "logical properties only (RTL)")
        self.assertIn("@media (max-width", css, "small-screen layout")
        app_js = (static / "js" / "app.js").read_text()
        self.assertIn("5000", app_js, "metrics refreshed every 5 s")
        comp = (static / "js" / "components.js").read_text()
        self.assertIn("document.hidden", comp, "polling suspended when the tab is hidden")


if __name__ == "__main__":
    unittest.main()
