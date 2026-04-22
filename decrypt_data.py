#!/usr/bin/env python3
"""
decrypt_data.py — inverse of encrypt_data.py.

Reads data/enc_manifest.json + the three .enc files and writes out the
plaintext bodies so they can be edited locally. Outputs:

  <out>/index_body.html
  <out>/data/core.json
  <out>/data/layers.json

None of these are meant to be committed — .gitignore keeps them out of
the repo. After editing (e.g. via patch_body.py), re-encrypt with:

  python encrypt_data.py --password "PWD" --body <out>/index_body.html --out .

Usage:
  pip install cryptography
  python decrypt_data.py --password "your-shared-password"
"""

import argparse
import base64
import json
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
except ImportError:
    sys.exit("This script needs the 'cryptography' package.\n  pip install cryptography")


KEY_BYTES = 32
IV_BYTES = 12


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iterations,
    ).derive(password.encode("utf-8"))


def decrypt(blob: bytes, key: bytes) -> bytes:
    if len(blob) < IV_BYTES + 16:
        raise ValueError(f"ciphertext too short ({len(blob)} bytes) to contain IV + tag")
    iv, ct = blob[:IV_BYTES], blob[IV_BYTES:]
    return AESGCM(key).decrypt(iv, ct, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", required=True, help="Shared password used at encrypt time.")
    ap.add_argument("--src", default=".", help="Directory containing the .enc files. Default: current dir.")
    ap.add_argument("--out", default=".", help="Where to write the plaintext. Default: current dir.")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)

    manifest_path = src / "data" / "enc_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        salt = base64.b64decode(manifest["salt"])
        iterations = int(manifest["iterations"])
    except (KeyError, ValueError) as e:
        sys.exit(f"Bad manifest (missing salt/iterations): {e}")

    key = derive_key(args.password, salt, iterations)

    pairs = [
        (src / "index_body.html.enc", out / "index_body.html"),
        (src / "data" / "core.json.enc", out / "data" / "core.json"),
        (src / "data" / "layers.json.enc", out / "data" / "layers.json"),
    ]

    for enc_path, plain_path in pairs:
        if not enc_path.exists():
            sys.exit(f"Missing ciphertext: {enc_path}")
        try:
            plain = decrypt(enc_path.read_bytes(), key)
        except Exception as e:
            sys.exit(f"Decryption of {enc_path.name} failed: {e}\n"
                     "Likely a wrong password or a mismatched salt/iterations.")
        plain_path.write_bytes(plain)
        print(f"  {enc_path.name:<26} -> {plain_path}  ({len(plain):,} bytes)")

    print()
    print("Plaintext written. Reminder: these files are gitignored — don't")
    print("let them slip into a commit. Re-encrypt with:")
    print(f'  python encrypt_data.py --password "YOUR_PWD" --body {out / "index_body.html"} --out .')


if __name__ == "__main__":
    main()
