#!/usr/bin/env python3
"""
Pre-push encryption step for GitHub-Pages deployment.

Reads the rebuilt index.html + data/core.json + data/layers.json, and emits
a public/ folder where:

  public/index.html              — password-prompt loader (v1 + v2 capable)
  public/index_body.html.enc     — AES-GCM-encrypted original index.html
  public/data/core.json.enc      — encrypted core.json
  public/data/layers.json.enc    — encrypted layers.json
  public/data/enc_manifest.json  — manifest (NOT secret)

Two manifest versions are supported:

  v1 (legacy): single password, PBKDF2-SHA256 200k iterations. All files
      share one key. Existing deployments keep working — the loader still
      speaks v1 when it sees {"v": 1, "salt": ..., "iterations": ...}.

  v2 (default for new encrypts): separate public & staff tiers, each with
      its own salt and files. Default KDF is PBKDF2-SHA256 at 600k
      iterations (3× the v1 hardening). Manifest has a "kdf" field so a
      future v3 can switch to Argon2id without another protocol break.

      Files → tier assignment (defaults):
        data/core.json.enc      → public   (map aggregates are lower-risk)
        data/layers.json.enc    → staff    (parcel-level drill-down)
        index_body.html.enc     → staff    (contains the full UI that
                                            surfaces parcel data)

      The gate prompts once for a password, tries each tier's salt, and
      opens the map at whatever tier the password unlocks.

Usage (v2, dual-tier):
  pip install cryptography
  python encrypt_data.py \\
      --public-password "maps-for-all" \\
      --staff-password  "parcels-staff-only"

Usage (v1, single-password, legacy):
  python encrypt_data.py --password "one-password-fits-all" --v1

Upload everything in public/ to the deployed branch.
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


# v1 legacy constants
ITER_V1 = 200_000

# v2 defaults — 3× the KDF cost of v1.
KDF_DEFAULT = "pbkdf2-sha256-600k"
ITER_V2_PBKDF2 = 600_000

SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32  # AES-256

# Known-plaintext check blob per tier. Decrypting it proves the password is
# right without having to fetch-and-attempt a big file first.
VERIFY_PLAINTEXT = b"bernalillo-map-v2-verify"


def derive_key_pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iterations,
    ).derive(password.encode("utf-8"))


def derive_key(password: str, salt: bytes, kdf: str) -> bytes:
    if kdf == "pbkdf2-sha256-600k":
        return derive_key_pbkdf2(password, salt, ITER_V2_PBKDF2)
    if kdf == "pbkdf2-sha256-200k":  # v1 alias
        return derive_key_pbkdf2(password, salt, ITER_V1)
    # argon2id is reserved for v3; encrypt_data.py won't emit it until the
    # JS loader carries a WASM implementation. Fall through to an error so
    # a typo doesn't silently downgrade security.
    raise ValueError(f"Unsupported KDF: {kdf}")


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    iv = os.urandom(IV_BYTES)
    return iv + AESGCM(key).encrypt(iv, plaintext, None)


# Default tier assignment. core.json aggregates → public; everything that
# exposes parcel-level detail or the full interactive UI → staff.
DEFAULT_TIER_ASSIGNMENT = {
    "data/core.json.enc": "public",
    "data/layers.json.enc": "staff",
    "index_body.html.enc": "staff",
}


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
.tier{margin-top:8px;font-size:10px;color:#5a7a99;min-height:12px}
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
    <div class="tier" id="tier"></div>
  </form>
</div>
<script>
const $=id=>document.getElementById(id);
const setErr=s=>$('err').textContent=s||'';
const setPg=s=>$('pg').textContent=s||'';
const setTier=s=>$('tier').textContent=s||'';

function b64ToBytes(b64){const s=atob(b64);const b=new Uint8Array(s.length);for(let i=0;i<s.length;i++)b[i]=s.charCodeAt(i);return b;}

async function deriveKey(pw,salt,kdf){
  // PBKDF2 variants — Argon2id is reserved for a future v3 loader that
  // carries a WASM implementation. Unknown KDFs fail loudly rather than
  // silently falling back.
  let iter;
  if(kdf==='pbkdf2-sha256-600k')iter=600000;
  else if(kdf==='pbkdf2-sha256-200k')iter=200000;
  else throw new Error('Unsupported KDF: '+kdf);
  const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
  return crypto.subtle.deriveKey(
    {name:'PBKDF2',salt,iterations:iter,hash:'SHA-256'},
    km,{name:'AES-GCM',length:256},false,['decrypt']);
}

async function decryptBytes(buf,key){
  if(buf.byteLength<28)throw new Error('ciphertext too small');
  const iv=buf.slice(0,12),ct=buf.slice(12);
  return crypto.subtle.decrypt({name:'AES-GCM',iv},key,ct);
}

async function fetchBytes(path){
  const r=await fetch(path);
  if(!r.ok)throw new Error('HTTP '+r.status+' for '+path);
  return r.arrayBuffer();
}

async function decryptFile(path,key){
  const buf=await fetchBytes(path);
  return decryptBytes(buf,key);
}

// v1 flow: one password, one salt, all files.
async function unlockV1(man,pw){
  const salt=b64ToBytes(man.salt);
  const kdf=man.iterations===600000?'pbkdf2-sha256-600k':'pbkdf2-sha256-200k';
  const key=await deriveKey(pw,salt,kdf);
  const [coreBuf,layersBuf,htmlBuf]=await Promise.all([
    decryptFile('data/core.json.enc',key).catch(e=>{throw new Error('decrypt_core:'+(e&&e.message||e));}),
    decryptFile('data/layers.json.enc',key).catch(e=>{throw new Error('decrypt_layers:'+(e&&e.message||e));}),
    decryptFile('index_body.html.enc',key).catch(e=>{throw new Error('decrypt_html:'+(e&&e.message||e));}),
  ]);
  return {tier:'full',coreBuf,layersBuf,htmlBuf};
}

// v2 flow: try each tier's salt in order. The verify blob identifies
// which tier the password unlocks. A tier's files are the ONLY files
// that key can decrypt.
async function unlockV2(man,pw){
  const kdf=man.kdf||'pbkdf2-sha256-600k';
  const tiers=man.tiers||{};
  const files=man.files||{};
  let unlockedTier=null,unlockedKey=null;
  // Deterministic order: staff first so a password valid at both tiers
  // (legacy deploy, shouldn't happen) defaults to full access.
  for(const tierName of ['staff','public']){
    const t=tiers[tierName];
    if(!t||!t.salt||!t.verify)continue;
    try{
      const k=await deriveKey(pw,b64ToBytes(t.salt),kdf);
      const vb=b64ToBytes(t.verify);
      await decryptBytes(vb.buffer,k);
      unlockedTier=tierName;unlockedKey=k;break;
    }catch(_){/* try next tier */}
  }
  if(!unlockedTier)throw new Error('Wrong password (no tier matched).');
  setTier('Access tier: '+unlockedTier);
  // Fetch + decrypt each file in the tier's list.
  const accessible={};
  for(const [path,tierOfFile] of Object.entries(files)){
    if(tierOfFile!==unlockedTier&&unlockedTier!=='staff')continue;
    // staff tier unlocks public files too — encrypt_data.py shares
    // nothing between tiers, so we derive the public key too when needed.
    if(tierOfFile===unlockedTier){
      try{accessible[path]=await decryptFile(path,unlockedKey);}
      catch(e){throw new Error('decrypt_'+path+':'+(e&&e.message||e));}
    }
  }
  // If the user is staff, additionally derive the public key so we can
  // pick up core.json (which is encrypted under the public key).
  if(unlockedTier==='staff'&&tiers.public){
    const pk=await deriveKey(pw,b64ToBytes(tiers.public.salt),kdf).catch(()=>null);
    if(pk){
      for(const [path,tierOfFile] of Object.entries(files)){
        if(tierOfFile==='public'&&!accessible[path]){
          try{accessible[path]=await decryptFile(path,pk);}catch(_){/* pw only valid at staff */}
        }
      }
    }
  }
  return {tier:unlockedTier,
          coreBuf:accessible['data/core.json.enc'],
          layersBuf:accessible['data/layers.json.enc'],
          htmlBuf:accessible['index_body.html.enc']};
}

$('gateForm').addEventListener('submit',async e=>{
  e.preventDefault();
  setErr('');setTier('');
  const pw=$('pw').value;
  if(!pw){setErr('Enter a password.');return;}
  $('gateBtn').disabled=true;
  try{
    setPg('Fetching manifest...');
    const manRes=await fetch('data/enc_manifest.json');
    if(!manRes.ok)throw new Error('Manifest fetch failed: HTTP '+manRes.status+'.');
    const manText=await manRes.text();
    let man;
    try{man=JSON.parse(manText);}
    catch(_){throw new Error('Manifest is not JSON (server returned '+(manText.slice(0,40))+'...).');}
    setPg('Deriving key...');
    let result;
    if((man.v||1)>=2){
      result=await unlockV2(man,pw);
    }else{
      if(!man.salt||!man.iterations)throw new Error('v1 manifest missing salt or iterations.');
      result=await unlockV1(man,pw);
    }
    if(!result.htmlBuf)throw new Error('This password does not grant access to the map body.');
    setPg('Rendering...');
    const dec=new TextDecoder();
    const coreText=result.coreBuf?dec.decode(result.coreBuf):null;
    const layersText=result.layersBuf?dec.decode(result.layersBuf):null;
    const htmlText=dec.decode(result.htmlBuf);

    const origFetch=window.fetch.bind(window);
    function matchPath(u,name){
      const base=u.split('#')[0].split('?')[0];
      return base===name||base.endsWith('/'+name);
    }
    window.fetch=function(url,init){
      let u='';
      if(typeof url==='string')u=url;
      else if(url&&typeof url.url==='string')u=url.url;
      else if(url)u=String(url);
      if(matchPath(u,'data/core.json')&&coreText!==null)
        return Promise.resolve(new Response(coreText,{status:200,headers:{'Content-Type':'application/json'}}));
      if(matchPath(u,'data/layers.json')&&layersText!==null)
        return Promise.resolve(new Response(layersText,{status:200,headers:{'Content-Type':'application/json'}}));
      return origFetch(url,init);
    };

    document.open();
    document.write(htmlText);
    document.close();
  }catch(err){
    const msg=String(err&&err.message||err);
    if(msg.startsWith('decrypt_')){
      if(msg.indexOf('HTTP ')>=0)setErr('Encrypted asset missing on server: '+msg);
      else setErr('Wrong password (or corrupted data).');
    }else setErr(msg);
    setPg('');setTier('');
    $('gateBtn').disabled=false;
  }
});
</script>
</body></html>
'''


def _encrypt_with_password(password: str, salt: bytes, kdf: str, plaintext: bytes) -> bytes:
    key = derive_key(password, salt, kdf)
    return encrypt(plaintext, key)


def _make_verify_blob(password: str, salt: bytes, kdf: str) -> bytes:
    """Small known-plaintext blob. The JS loader decrypts this to
    identify which tier the user unlocked without attempting the full
    (potentially large) payload files first."""
    return _encrypt_with_password(password, salt, kdf, VERIFY_PLAINTEXT)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", help="Legacy v1 single-password mode. Deprecated — prefer --public-password/--staff-password.")
    ap.add_argument("--public-password", help="Password for the public tier (core.json only).")
    ap.add_argument("--staff-password", help="Password for the staff tier (layers + body HTML).")
    ap.add_argument("--v1", action="store_true", help="Emit a v1 manifest (200k PBKDF2, single key). Only with --password.")
    ap.add_argument("--kdf", default=KDF_DEFAULT,
                    choices=["pbkdf2-sha256-600k", "pbkdf2-sha256-200k"],
                    help="KDF algorithm (v2 only).")
    ap.add_argument("--src", default=".", help="Source directory (contains index.html and data/). Default: current dir.")
    ap.add_argument("--body", default=None, help="Path to the plaintext body HTML to encrypt. Defaults to <src>/index.html.")
    ap.add_argument("--out", default="public", help="Output directory. Default: public/")
    args = ap.parse_args()

    if args.v1 and not args.password:
        sys.exit("--v1 requires --password")
    if args.v1:
        # v1 ships a single-tier 200k-iteration manifest. v2 (default) gives
        # a 3× harder KDF and tier separation. Operators staying on v1 are
        # opting into both a weaker derivation and the loss of a public/
        # staff split — surface that explicitly so it's a deliberate choice.
        print(
            "WARNING: --v1 is legacy. Prefer v2 (omit --v1, pass "
            "--public-password/--staff-password) for 600k PBKDF2 + tier "
            "separation. v1 will be removed in a future release.",
            file=sys.stderr,
        )
    if not args.v1 and not (args.public_password and args.staff_password):
        # Backward-compat: if only --password is given and --v1 isn't set,
        # use it for BOTH tiers. That gives the v2 manifest/hardening with
        # a single operational password for small deployments.
        if args.password and not (args.public_password or args.staff_password):
            args.public_password = args.password
            args.staff_password = args.password
        else:
            sys.exit("v2 requires --public-password and --staff-password (or pass --v1 with --password)")

    src = Path(args.src)
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)

    body_path = Path(args.body) if args.body else (src / "index.html")
    for p in [body_path, src / "data" / "core.json", src / "data" / "layers.json"]:
        if not p.exists():
            sys.exit(f"Missing input: {p}")

    core_plain = (src / "data" / "core.json").read_bytes()
    layers_plain = (src / "data" / "layers.json").read_bytes()
    body_plain = body_path.read_bytes()

    sizes = {}

    if args.v1:
        salt = os.urandom(SALT_BYTES)
        key = derive_key_pbkdf2(args.password, salt, ITER_V1)
        body_enc = encrypt(body_plain, key)
        core_enc = encrypt(core_plain, key)
        layers_enc = encrypt(layers_plain, key)
        (out / "index_body.html.enc").write_bytes(body_enc)
        (out / "data" / "core.json.enc").write_bytes(core_enc)
        (out / "data" / "layers.json.enc").write_bytes(layers_enc)
        sizes["index_body.html.enc"] = (len(body_plain), len(body_enc))
        sizes["data/core.json.enc"] = (len(core_plain), len(core_enc))
        sizes["data/layers.json.enc"] = (len(layers_plain), len(layers_enc))
        manifest = {"v": 1, "salt": base64.b64encode(salt).decode("ascii"), "iterations": ITER_V1}
        (out / "data" / "enc_manifest.json").write_text(json.dumps(manifest, indent=2))
        (out / "index.html").write_text(LOADER, encoding="utf-8")
        print(f"Encrypted (v1 legacy) with AES-256-GCM + PBKDF2-SHA256 {ITER_V1:,} iterations")
    else:
        public_salt = os.urandom(SALT_BYTES)
        staff_salt = os.urandom(SALT_BYTES)

        core_enc = _encrypt_with_password(args.public_password, public_salt, args.kdf, core_plain)
        layers_enc = _encrypt_with_password(args.staff_password, staff_salt, args.kdf, layers_plain)
        body_enc = _encrypt_with_password(args.staff_password, staff_salt, args.kdf, body_plain)

        (out / "data" / "core.json.enc").write_bytes(core_enc)
        (out / "data" / "layers.json.enc").write_bytes(layers_enc)
        (out / "index_body.html.enc").write_bytes(body_enc)
        sizes["data/core.json.enc"] = (len(core_plain), len(core_enc))
        sizes["data/layers.json.enc"] = (len(layers_plain), len(layers_enc))
        sizes["index_body.html.enc"] = (len(body_plain), len(body_enc))

        verify_public = _make_verify_blob(args.public_password, public_salt, args.kdf)
        verify_staff = _make_verify_blob(args.staff_password, staff_salt, args.kdf)

        manifest = {
            "v": 2,
            "kdf": args.kdf,
            "tiers": {
                "public": {
                    "salt": base64.b64encode(public_salt).decode("ascii"),
                    "verify": base64.b64encode(verify_public).decode("ascii"),
                },
                "staff": {
                    "salt": base64.b64encode(staff_salt).decode("ascii"),
                    "verify": base64.b64encode(verify_staff).decode("ascii"),
                },
            },
            "files": dict(DEFAULT_TIER_ASSIGNMENT),
        }
        (out / "data" / "enc_manifest.json").write_text(json.dumps(manifest, indent=2))
        (out / "index.html").write_text(LOADER, encoding="utf-8")
        print(f"Encrypted (v2 dual-tier) with AES-256-GCM + {args.kdf}")
        print(f"  public tier: data/core.json.enc")
        print(f"  staff  tier: data/layers.json.enc, index_body.html.enc")

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
