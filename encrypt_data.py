#!/usr/bin/env python3
"""
Pre-push encryption step for GitHub-Pages deployment.

Reads the rebuilt index.html + data/core.json + data/layers.json, and emits
a public/ folder where:

  public/index.html              — tiny password-prompt loader (~4 KB)
  public/index_body.html.enc     — AES-GCM-encrypted original index.html
  public/data/core.json.enc      — encrypted core.json
  public/data/layers.json.enc    — encrypted layers.json
  public/data/enc_manifest.json  — {salt, iterations}  (NOT secret)

Crypto: PBKDF2-HMAC-SHA256 (200k iterations) derives an AES-256 key from the
shared password. Each file gets a random 12-byte IV prepended; AES-GCM's
16-byte auth tag is appended by the AESGCM API. The browser uses SubtleCrypto
to derive the same key and verify/decrypt — a wrong password fails the GCM
tag check and surfaces a clean "wrong password" error.

Usage:
  pip install cryptography
  python encrypt_data.py --password "your-shared-password"
  # then upload everything in public/ to the deployed branch.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
except ImportError:
    sys.exit("This script needs the 'cryptography' package.\n  pip install cryptography")


ITER = 200_000
SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32  # AES-256


def derive_key(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=ITER,
    ).derive(password.encode("utf-8"))


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    iv = os.urandom(IV_BYTES)
    return iv + AESGCM(key).encrypt(iv, plaintext, None)


LOADER = '''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Bernalillo County Assessor – Spatial Equity</title>
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<style>
body{margin:0;font-family:system-ui,-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f3f4f6;color:#222}
.gate{background:#fff;padding:28px 32px;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:380px;width:92%}
.gate h1{margin:0 0 12px;font-size:16px;color:#08306b}
.gate p{margin:0 0 14px;font-size:12px;color:#666;line-height:1.5}
.gate input{width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:4px;font-size:14px;box-sizing:border-box}
.gate button{margin-top:10px;width:100%;padding:9px;font-size:13px;font-weight:600;background:#185FA5;color:#fff;border:none;border-radius:4px;cursor:pointer}
.gate button:disabled{opacity:.55;cursor:not-allowed}
.err{color:#c41a1a;font-size:11px;margin-top:6px;min-height:15px}
.pg{margin-top:8px;font-size:11px;color:#888;min-height:14px}
</style>
</head><body>
<div class="gate">
  <h1>Bernalillo County Spatial Equity</h1>
  <p>Protected access. Enter the password provided by the Assessor's Office.</p>
  <form id="gateForm">
    <input type="password" id="pw" placeholder="Password" autocomplete="current-password" autofocus>
    <button id="gateBtn" type="submit">Unlock</button>
    <div class="err" id="err"></div>
    <div class="pg" id="pg"></div>
  </form>
</div>
<script>
const $=id=>document.getElementById(id);
const setErr=s=>$('err').textContent=s||'';
const setPg=s=>$('pg').textContent=s||'';

function b64ToBytes(b64){const s=atob(b64);const b=new Uint8Array(s.length);for(let i=0;i<s.length;i++)b[i]=s.charCodeAt(i);return b;}

async function deriveKey(pw,salt,iter){
  const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
  return crypto.subtle.deriveKey(
    {name:'PBKDF2',salt,iterations:iter,hash:'SHA-256'},
    km,{name:'AES-GCM',length:256},false,['decrypt']);
}

async function decryptFile(path,key){
  const buf=await(await fetch(path)).arrayBuffer();
  if(buf.byteLength<28)throw new Error('File too small: '+path);
  const iv=buf.slice(0,12),ct=buf.slice(12);
  return crypto.subtle.decrypt({name:'AES-GCM',iv},key,ct);
}

$('gateForm').addEventListener('submit',async e=>{
  e.preventDefault();
  setErr('');
  const pw=$('pw').value;
  if(!pw){setErr('Enter a password.');return;}
  $('gateBtn').disabled=true;
  try{
    setPg('Fetching manifest...');
    const man=await(await fetch('data/enc_manifest.json')).json();
    const salt=b64ToBytes(man.salt);
    setPg('Deriving key...');
    const key=await deriveKey(pw,salt,man.iterations||200000);

    setPg('Fetching encrypted assets...');
    const [coreBuf,layersBuf,htmlBuf]=await Promise.all([
      decryptFile('data/core.json.enc',key).catch(e=>{throw new Error('decrypt_core');}),
      decryptFile('data/layers.json.enc',key).catch(e=>{throw new Error('decrypt_layers');}),
      decryptFile('index_body.html.enc',key).catch(e=>{throw new Error('decrypt_html');}),
    ]);

    setPg('Rendering...');
    const dec=new TextDecoder();
    const coreText=dec.decode(coreBuf);
    const layersText=dec.decode(layersBuf);
    const htmlText=dec.decode(htmlBuf);

    // Intercept the real page's fetch calls for data/core.json &
    // data/layers.json and hand back the decrypted payload instead.
    const origFetch=window.fetch.bind(window);
    window.fetch=function(url,init){
      const u=(typeof url==='string')?url:(url&&url.url)||'';
      if(u==='data/core.json'||u.endsWith('/data/core.json'))
        return Promise.resolve(new Response(coreText,{status:200,headers:{'Content-Type':'application/json'}}));
      if(u==='data/layers.json'||u.endsWith('/data/layers.json'))
        return Promise.resolve(new Response(layersText,{status:200,headers:{'Content-Type':'application/json'}}));
      return origFetch(url,init);
    };

    // Replace the document with the decrypted HTML. document.open() keeps
    // the same window, so our fetch override and all other globals persist
    // into the newly written page.
    document.open();
    document.write(htmlText);
    document.close();
  }catch(err){
    const msg=String(err&&err.message||err);
    if(msg.startsWith('decrypt_'))setErr('Wrong password (or corrupted data).');
    else setErr(msg);
    setPg('');
    $('gateBtn').disabled=false;
  }
});
</script>
</body></html>
'''


def main():
    ap = argparse.ArgumentParser(description="Encrypt index.html + data/*.json for password-gated deployment.")
    ap.add_argument("--password", required=True, help="Shared password (users enter this to unlock the site).")
    ap.add_argument("--src", default=".", help="Source directory (contains index.html and data/). Default: current dir.")
    ap.add_argument("--out", default="public", help="Output directory. Default: public/")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)

    for p in [src / "index.html", src / "data" / "core.json", src / "data" / "layers.json"]:
        if not p.exists():
            sys.exit(f"Missing input: {p}")

    salt = os.urandom(SALT_BYTES)
    key = derive_key(args.password, salt)

    def _write_enc(plain_path, out_path):
        data = Path(plain_path).read_bytes()
        enc = encrypt(data, key)
        Path(out_path).write_bytes(enc)
        return len(data), len(enc)

    sizes = {}
    sizes["index_body.html.enc"] = _write_enc(src / "index.html", out / "index_body.html.enc")
    sizes["data/core.json.enc"] = _write_enc(src / "data" / "core.json", out / "data" / "core.json.enc")
    sizes["data/layers.json.enc"] = _write_enc(src / "data" / "layers.json", out / "data" / "layers.json.enc")

    manifest = {"v": 1, "salt": base64.b64encode(salt).decode("ascii"), "iterations": ITER}
    (out / "data" / "enc_manifest.json").write_text(json.dumps(manifest, indent=2))

    (out / "index.html").write_text(LOADER, encoding="utf-8")

    print(f"Encrypted with AES-256-GCM (PBKDF2 {ITER:,} iterations)")
    print(f"Wrote {out.resolve()}:")
    print(f"  index.html                 {len(LOADER):>10,} bytes  (loader)")
    for name, (plain, enc) in sizes.items():
        print(f"  {name:<26} {enc:>10,} bytes  (plaintext {plain:,})")
    print(f"  data/enc_manifest.json     {(out / 'data' / 'enc_manifest.json').stat().st_size:>10,} bytes")
    print()
    print("Upload ALL files under public/ to the deployed branch, overwriting")
    print("the existing index.html and data/ contents. Delete the plaintext")
    print("data/core.json and data/layers.json from the branch — they aren't")
    print("needed anymore and would otherwise still be publicly downloadable.")


if __name__ == "__main__":
    main()
