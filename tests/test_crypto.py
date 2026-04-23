"""Round-trip tests for the encrypt/decrypt pair, both v1 and v2."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _import_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


encrypt_mod = _import_script("encrypt_data")
decrypt_mod = _import_script("decrypt_data")


class CryptoRoundTripBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.body = b"<html><body>fake map body</body></html>"
        self.core = b'{"DATA":{"features":[]}}'
        self.layers = b'{"SL":[]}'
        (self.root / "index.html").write_bytes(self.body)
        (self.root / "data" / "core.json").write_bytes(self.core)
        (self.root / "data" / "layers.json").write_bytes(self.layers)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_encrypt(self, args):
        """Invoke encrypt_data.main() by patching argv — tests the CLI path."""
        argv = [sys.argv[0]] + args
        old_argv = sys.argv
        sys.argv = argv
        try:
            encrypt_mod.main()
        finally:
            sys.argv = old_argv

    def _run_decrypt(self, args):
        argv = [sys.argv[0]] + args
        old_argv = sys.argv
        sys.argv = argv
        try:
            decrypt_mod.main()
        finally:
            sys.argv = old_argv


class TestV1RoundTrip(CryptoRoundTripBase):
    def test_v1_encrypt_decrypt_roundtrip(self):
        out = self.root / "public"
        self._run_encrypt(["--v1", "--password", "legacy-pw",
                           "--src", str(self.root), "--out", str(out)])
        # Ensure the legacy manifest shape matches what the existing
        # index.html loader expects (v1 field present).
        manifest = json.loads((out / "data" / "enc_manifest.json").read_text())
        self.assertEqual(manifest["v"], 1)
        self.assertEqual(manifest["iterations"], encrypt_mod.ITER_V1)
        # Now decrypt.
        roundtrip = self.root / "roundtrip"
        roundtrip.mkdir()
        # decrypt_data expects .enc files under <src>, mirror public/ layout.
        (roundtrip / "data").mkdir()
        shutil.copy(out / "data" / "enc_manifest.json", roundtrip / "data" / "enc_manifest.json")
        shutil.copy(out / "index_body.html.enc", roundtrip / "index_body.html.enc")
        shutil.copy(out / "data" / "core.json.enc", roundtrip / "data" / "core.json.enc")
        shutil.copy(out / "data" / "layers.json.enc", roundtrip / "data" / "layers.json.enc")
        self._run_decrypt(["--password", "legacy-pw",
                           "--src", str(roundtrip),
                           "--out", str(roundtrip / "out")])
        self.assertEqual((roundtrip / "out" / "index_body.html").read_bytes(), self.body)
        self.assertEqual((roundtrip / "out" / "data" / "core.json").read_bytes(), self.core)
        self.assertEqual((roundtrip / "out" / "data" / "layers.json").read_bytes(), self.layers)


class TestV2RoundTrip(CryptoRoundTripBase):
    def test_v2_dual_tier_staff_unlocks_everything(self):
        out = self.root / "public"
        self._run_encrypt(["--public-password", "pub-pw",
                           "--staff-password", "staff-pw",
                           "--src", str(self.root), "--out", str(out)])
        manifest = json.loads((out / "data" / "enc_manifest.json").read_text())
        self.assertEqual(manifest["v"], 2)
        self.assertEqual(manifest["kdf"], encrypt_mod.KDF_DEFAULT)
        self.assertIn("public", manifest["tiers"])
        self.assertIn("staff", manifest["tiers"])
        self.assertEqual(manifest["files"]["data/core.json.enc"], "public")
        self.assertEqual(manifest["files"]["data/layers.json.enc"], "staff")
        # Staff password should unlock every file (it also re-derives the
        # public key internally — but here we test via decrypt_data).
        rt = self.root / "rt"
        rt.mkdir()
        (rt / "data").mkdir()
        for rel in ["index_body.html.enc", "data/core.json.enc",
                    "data/layers.json.enc", "data/enc_manifest.json"]:
            shutil.copy(out / rel, rt / rel)
        self._run_decrypt(["--staff-password", "staff-pw",
                           "--public-password", "pub-pw",
                           "--src", str(rt), "--out", str(rt / "out")])
        self.assertEqual((rt / "out" / "data" / "core.json").read_bytes(), self.core)
        self.assertEqual((rt / "out" / "data" / "layers.json").read_bytes(), self.layers)
        self.assertEqual((rt / "out" / "index_body.html").read_bytes(), self.body)

    def test_v2_public_password_cannot_decrypt_staff_files(self):
        out = self.root / "public"
        self._run_encrypt(["--public-password", "pub-pw",
                           "--staff-password", "staff-pw",
                           "--src", str(self.root), "--out", str(out)])
        rt = self.root / "rt"
        rt.mkdir()
        (rt / "data").mkdir()
        for rel in ["index_body.html.enc", "data/core.json.enc",
                    "data/layers.json.enc", "data/enc_manifest.json"]:
            shutil.copy(out / rel, rt / rel)
        # Only the public password given — should decrypt core.json and
        # skip the staff-tier files without crashing.
        self._run_decrypt(["--public-password", "pub-pw",
                           "--src", str(rt), "--out", str(rt / "out")])
        self.assertTrue((rt / "out" / "data" / "core.json").exists())
        self.assertFalse((rt / "out" / "data" / "layers.json").exists())
        self.assertFalse((rt / "out" / "index_body.html").exists())

    def test_v2_wrong_password_exits(self):
        out = self.root / "public"
        self._run_encrypt(["--public-password", "pub-pw",
                           "--staff-password", "staff-pw",
                           "--src", str(self.root), "--out", str(out)])
        rt = self.root / "rt"
        rt.mkdir()
        (rt / "data").mkdir()
        for rel in ["data/core.json.enc", "data/layers.json.enc",
                    "index_body.html.enc", "data/enc_manifest.json"]:
            shutil.copy(out / rel, rt / rel)
        with self.assertRaises(SystemExit):
            self._run_decrypt(["--password", "WRONG",
                               "--src", str(rt), "--out", str(rt / "out")])


class TestVerifyBlob(unittest.TestCase):
    """The tier-identifier verify blob must round-trip cleanly."""

    def test_verify_blob_decrypts_with_right_key(self):
        salt = os.urandom(16)
        kdf = encrypt_mod.KDF_DEFAULT
        vb = encrypt_mod._make_verify_blob("hunter2", salt, kdf)
        key = encrypt_mod.derive_key("hunter2", salt, kdf)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        iv, ct = vb[:12], vb[12:]
        plain = AESGCM(key).decrypt(iv, ct, None)
        self.assertEqual(plain, encrypt_mod.VERIFY_PLAINTEXT)

    def test_verify_blob_rejects_wrong_key(self):
        salt = os.urandom(16)
        kdf = encrypt_mod.KDF_DEFAULT
        vb = encrypt_mod._make_verify_blob("right", salt, kdf)
        wrong_key = encrypt_mod.derive_key("wrong", salt, kdf)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        iv, ct = vb[:12], vb[12:]
        from cryptography.exceptions import InvalidTag
        with self.assertRaises(InvalidTag):
            AESGCM(wrong_key).decrypt(iv, ct, None)


if __name__ == "__main__":
    unittest.main()
