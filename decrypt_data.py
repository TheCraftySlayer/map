#!/usr/bin/env python3
"""
decrypt_data.py — inverse of encrypt_data.py. Supports both v1 and v2.

v1: single --password decrypts all files with one key.
v2: dual-tier. --staff-password is required to decrypt the full set
    (layers.json + index_body.html); --public-password decrypts only
    core.json. If you only have one password, pass it as
    --password and the script tries each tier.

Outputs (in <out>):
  <out>/index_body.html   (staff tier only)
  <out>/data/core.json    (public or staff)
  <out>/data/layers.json  (staff tier only)

None of these are meant to be committed — .gitignore keeps them out of
the repo. After editing (e.g. via patch_body.py), re-encrypt with:

  python encrypt_data.py \\
      --public-password "PUB" --staff-password "STAFF" \\
      --body <out>/index_body.html --out .

Usage:
  pip install cryptography
  python decrypt_data.py --staff-password "staff pwd"
  python decrypt_data.py --password "legacy single password"
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

KDF_ITERATIONS = {
    "pbkdf2-sha256-600k": 600_000,
    "pbkdf2-sha256-200k": 200_000,
}


def derive_key(password: str, salt: bytes, kdf: str) -> bytes:
    iters = KDF_ITERATIONS.get(kdf)
    if iters is None:
        raise ValueError(f"Unsupported KDF: {kdf}")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iters,
    ).derive(password.encode("utf-8"))


def decrypt(blob: bytes, key: bytes) -> bytes:
    if len(blob) < IV_BYTES + 16:
        raise ValueError(f"ciphertext too short ({len(blob)} bytes) to contain IV + tag")
    iv, ct = blob[:IV_BYTES], blob[IV_BYTES:]
    return AESGCM(key).decrypt(iv, ct, None)


def _try_derive(password: str, salt_b64: str, verify_b64: str, kdf: str):
    """Return the AES key if the password's derivation successfully decrypts
    the tier's verify blob; else None."""
    if not password:
        return None
    try:
        salt = base64.b64decode(salt_b64)
        key = derive_key(password, salt, kdf)
        decrypt(base64.b64decode(verify_b64), key)
        return key
    except Exception:
        return None


def _decrypt_v1(manifest, password, src, out):
    try:
        salt = base64.b64decode(manifest["salt"])
        iterations = int(manifest["iterations"])
    except (KeyError, ValueError) as e:
        sys.exit(f"Bad v1 manifest: {e}")
    kdf = "pbkdf2-sha256-600k" if iterations == 600_000 else "pbkdf2-sha256-200k"
    key = derive_key(password, salt, kdf)
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


def _decrypt_v2(manifest, args, src, out):
    kdf = manifest.get("kdf", "pbkdf2-sha256-600k")
    tiers = manifest.get("tiers", {})
    files = manifest.get("files", {})

    public_key = None
    staff_key = None

    # Prefer the tier-specific flag if given; fall back to --password to
    # try both tiers. This lets an operator with a single password still
    # decrypt whatever that password unlocks.
    candidates = [
        (args.public_password or args.password, "public"),
        (args.staff_password or args.password, "staff"),
    ]
    for pw, tier in candidates:
        t = tiers.get(tier)
        if not t or not pw:
            continue
        k = _try_derive(pw, t["salt"], t["verify"], kdf)
        if k is not None:
            if tier == "public":
                public_key = k
            else:
                staff_key = k

    if public_key is None and staff_key is None:
        sys.exit("No tier matched the provided password(s). Check --public-password / --staff-password.")

    # Reverse-lookup: for each file, find the tier it belongs to and use
    # the matching key.
    tier_key = {"public": public_key, "staff": staff_key}
    any_decrypted = False
    for rel_path, tier_name in files.items():
        k = tier_key.get(tier_name)
        if k is None:
            print(f"  skip  {rel_path:<26} (no key for tier={tier_name})")
            continue
        enc_path = src / rel_path
        if not enc_path.exists():
            print(f"  miss  {rel_path:<26} (not on disk)")
            continue
        try:
            plain = decrypt(enc_path.read_bytes(), k)
        except Exception as e:
            print(f"  fail  {rel_path:<26} ({e})")
            continue
        # Write to an output path mirroring the input layout.
        plain_path = out / rel_path.replace(".enc", "")
        plain_path.parent.mkdir(parents=True, exist_ok=True)
        plain_path.write_bytes(plain)
        any_decrypted = True
        print(f"  {rel_path:<26} -> {plain_path}  ({len(plain):,} bytes)")

    if not any_decrypted:
        sys.exit("Nothing was decrypted. Check that the .enc files are under --src and the password matches.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", help="Single password: v1 main key, or v2 fallback for either tier.")
    ap.add_argument("--public-password", help="v2: public-tier password (core.json only).")
    ap.add_argument("--staff-password", help="v2: staff-tier password (layers + body).")
    ap.add_argument("--src", default=".", help="Directory containing the .enc files. Default: current dir.")
    ap.add_argument("--out", default=".", help="Where to write the plaintext. Default: current dir.")
    args = ap.parse_args()

    if not (args.password or args.public_password or args.staff_password):
        sys.exit("Provide --password (any tier) or --public-password/--staff-password.")

    src = Path(args.src)
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)

    manifest_path = src / "data" / "enc_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("v", 1)

    if version == 1:
        if not args.password:
            sys.exit("v1 manifest needs --password.")
        _decrypt_v1(manifest, args.password, src, out)
    elif version == 2:
        _decrypt_v2(manifest, args, src, out)
    else:
        sys.exit(f"Unknown manifest version: {version}")

    print()
    print("Plaintext written. Reminder: these files are gitignored — don't")
    print("let them slip into a commit.")


if __name__ == "__main__":
    main()
