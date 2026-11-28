"""Encryption of access data: vault format, tampering, wrong key, rotation, no plaintext."""
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import APP_DIR, STRONG, Instance, make_user

from twincheck.crypto import (DecryptError, KeyError_, StreamDecryptor, StreamEncryptor, generate_key, load_master_key,
                             parse_key)
from twincheck.exif import ExifTool
from twincheck.vault import Vault, default_vault


def run_cli(args, env, stdin=None):
    e = {"PATH": os.environ.get("PATH", "/usr/bin"), "PYTHONPATH": str(APP_DIR / "backend"), "PYTHONDONTWRITEBYTECODE": "1", **env}
    return subprocess.run([sys.executable, "-B", "-m", "twincheck", *args], env=e, capture_output=True, text=True, input=stdin, timeout=120)


class KeyTests(unittest.TestCase):
    def test_persistent_exiftool_reuses_and_recovers_process(self):
        inst = Instance()
        photo = inst.data / "persistent.jpg"
        photo.write_bytes(b"synthetic")
        exif = ExifTool(inst.env["EXIFTOOL_PATH"], persistent=True)
        try:
            one_shot = ExifTool(inst.env["EXIFTOOL_PATH"])
            baseline_start = time.monotonic()
            one_shot.read([str(photo)])
            one_shot.read([str(photo)])
            baseline = time.monotonic() - baseline_start
            started = time.monotonic()
            self.assertIn(str(photo), exif.read([str(photo)]))
            first_pid = exif.process_id()
            self.assertIsNotNone(first_pid)
            self.assertIn(str(photo), exif.read([str(photo)]))
            self.assertEqual(exif.process_id(), first_pid)
            elapsed = time.monotonic() - started
            os.kill(first_pid, 9)
            time.sleep(0.05)
            self.assertIn(str(photo), exif.read([str(photo)]))
            self.assertNotEqual(exif.process_id(), first_pid)
            print(f"ExifTool two-read timing: one-shot {baseline:.4f}s; persistent {elapsed:.4f}s")
        finally:
            exif.close()
            inst.cleanup()

    def test_generate_and_parse(self):
        k = generate_key()
        self.assertRegex(k, r"^[0-9a-f]{64}$")
        mk = parse_key(k)
        self.assertEqual(len(mk.raw), 32)
        self.assertFalse(mk.is_passphrase)
        self.assertTrue(parse_key("a long passphrase with enough entropy!").is_passphrase)
        with self.assertRaises(KeyError_):
            parse_key("short")
        with self.assertRaises(KeyError_):
            load_master_key({})

    def test_key_file(self):
        inst = Instance()
        try:
            p = Path(inst.tmp) / "secret"
            p.write_text(generate_key() + "\n")
            self.assertEqual(len(load_master_key({"APP_MASTER_KEY_FILE": str(p)}).raw), 32)
        finally:
            inst.cleanup()


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.path = Path(self.inst.tmp) / "vault.enc"
        self.key = parse_key(generate_key())

    def tearDown(self):
        self.inst.cleanup()

    def test_roundtrip_nonce_permissions(self):
        v = Vault(self.path, self.key)
        v.create(default_vault("en"))
        first = self.path.read_bytes()
        with v.transaction() as d:
            d["users"]["u"] = {"username": "secretname", "password_hash": "scrypt$x"}
        second = self.path.read_bytes()
        self.assertNotEqual(first[25:37], second[25:37], "nonce must change on every write")
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn(b"secretname", second)
        self.assertEqual(Vault(self.path, self.key).load()["users"]["u"]["username"], "secretname")
        self.assertTrue((self.path.parent / "vault.enc.bak").exists())
        self.assertNotIn(b"secretname", (self.path.parent / "vault.enc.bak").read_bytes())

    def test_tamper_any_byte_detected(self):
        v = Vault(self.path, self.key)
        v.create(default_vault("en"))
        raw = self.path.read_bytes()
        for pos in (0, 8, 9, 30, 40, len(raw) // 2, len(raw) - 1):
            b = bytearray(raw)
            b[pos] ^= 0x01
            self.path.write_bytes(bytes(b))
            with self.assertRaises(DecryptError, msg=f"byte {pos}"):
                Vault(self.path, self.key).load()
        self.path.write_bytes(raw[:-5])
        with self.assertRaises(DecryptError):
            Vault(self.path, self.key).load()

    def test_wrong_key(self):
        Vault(self.path, self.key).create(default_vault("en"))
        before = self.path.read_bytes()
        with self.assertRaises(DecryptError):
            Vault(self.path, parse_key(generate_key())).load()
        self.assertEqual(before, self.path.read_bytes())

    def test_rotation_and_rollback(self):
        v = Vault(self.path, self.key)
        v.create(default_vault("en"))
        new = parse_key(generate_key())
        v.rotate(new)
        self.assertEqual(Vault(self.path, new).load()["format"], 1)
        with self.assertRaises(DecryptError):
            Vault(self.path, self.key).load()
        self.assertTrue((self.path.parent / "vault.enc.pre-rotation").exists())
        Vault(self.path.parent / "vault.enc.pre-rotation", self.key).load()  # old copy still opens with the old key
        # failure during rotation -> rollback, old key still valid
        v2 = Vault(self.path, new)
        v2.load()
        original = self.path.read_bytes()
        real = v2._atomic_write

        def boom(blob):
            real(blob)
            raise OSError("disk full")
        v2._atomic_write = boom
        with self.assertRaises(OSError):
            v2.rotate(parse_key(generate_key()))
        self.assertEqual(self.path.read_bytes(), original)
        Vault(self.path, new).load()

    def test_stream_archive(self):
        import io
        buf = io.BytesIO()
        enc = StreamEncryptor(buf, "archive pass 123")
        data = os.urandom(3 * 1024 * 1024 + 17)
        enc.write(data)
        enc.close()
        raw = buf.getvalue()
        self.assertEqual(StreamDecryptor(io.BytesIO(raw), "archive pass 123").read(), data)
        with self.assertRaises(DecryptError):
            StreamDecryptor(io.BytesIO(raw), "wrong pass 1234").read()
        b = bytearray(raw)
        b[len(b) // 2] ^= 1
        with self.assertRaises(DecryptError):
            StreamDecryptor(io.BytesIO(bytes(b)), "archive pass 123").read()
        with self.assertRaises(DecryptError):   # truncation removes the final chunk
            StreamDecryptor(io.BytesIO(raw[: len(raw) - 100]), "archive pass 123").read()


class NoPlaintextInState(unittest.TestCase):
    def test_state_contains_no_account_data(self):
        inst = Instance().start()
        try:
            inst.setup()
            a = inst.admin()
            (inst.data / "bob").mkdir()
            make_user(a, "bobthebuilder", [{"path": "bob"}])
            a.patch("/api/admin/settings", {"organize": {"no_date_folder": "NoDateMarker"}})
            inst.stop()
            needles = [b"bobthebuilder", b"administrator", b"admin", b"NoDateMarker", b"scrypt$", STRONG.encode(), inst.key.encode()]
            for root, _dirs, files in os.walk(inst.state):
                for f in files:
                    p = Path(root) / f
                    data = p.read_bytes()
                    if p.suffix in (".sqlite3", ".sqlite3-wal", ".sqlite3-shm") or "sqlite3" in p.name:
                        # the operational DB holds paths/hashes and owner ids, never names, roles or hashes of passwords
                        for n in (b"bobthebuilder", b"scrypt$", STRONG.encode(), inst.key.encode(), b"NoDateMarker"):
                            self.assertNotIn(n, data, f"{n!r} found in {p}")
                        continue
                    for n in needles:
                        self.assertNotIn(n, data, f"{n!r} found in {p}")
            self.assertFalse(list(inst.state.glob("*.json")), "no plaintext config files")
        finally:
            inst.cleanup()


class StartupRefusal(unittest.TestCase):
    def test_cli_without_key_and_with_wrong_key(self):
        inst = Instance().start()
        try:
            inst.setup()
            inst.stop()
            env = {"DATA_DIR": str(inst.data), "STATE_DIR": str(inst.state)}
            r = run_cli(["reset-admin-password", "--generate"], env)
            self.assertEqual(r.returncode, 2)
            self.assertIn("generate-key", r.stderr)
            before = (inst.state / "vault.enc").read_bytes()
            r = run_cli(["reset-admin-password", "--generate"], {**env, "APP_MASTER_KEY": generate_key()})
            self.assertEqual(r.returncode, 3)
            self.assertEqual(before, (inst.state / "vault.enc").read_bytes(), "nothing modified with a wrong key")
            r = run_cli(["generate-key"], {})
            self.assertEqual(r.returncode, 0)
            self.assertRegex(r.stdout, r"APP_MASTER_KEY=[0-9a-f]{64}")
        finally:
            inst.cleanup()

    def test_app_refuses_wrong_key(self):
        from twincheck.core import App, StartupError
        inst = Instance().start()
        try:
            inst.setup()
            inst.stop()
            before = (inst.state / "vault.enc").read_bytes()
            other = Instance(key=generate_key())
            other.env.update({"DATA_DIR": str(inst.data), "STATE_DIR": str(inst.state)})
            with self.assertRaises(StartupError):
                other.start()
            self.assertEqual(before, (inst.state / "vault.enc").read_bytes())
            other.core = None
            other.cleanup()
        finally:
            inst.cleanup()


class NoSecretsInResponses(unittest.TestCase):
    def test_api_never_returns_hashes_or_db_password(self):
        inst = Instance().start()
        try:
            inst.setup()
            a = inst.admin()
            for url in ("/api/admin/users", "/api/bootstrap", "/api/me", "/api/admin/db", "/api/admin/settings", "/api/admin/audit"):
                body = a.get(url).text
                self.assertNotIn("scrypt$", body, url)
                self.assertNotIn(inst.key, body, url)
                self.assertFalse(re.search(r'"password_hash"', body), url)
        finally:
            inst.cleanup()


if __name__ == "__main__":
    unittest.main()
