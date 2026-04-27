# CLAUDE.md — orientation for Claude sessions

This is a Bernalillo County Assessor spatial-equity map. Static site,
password-gated, deployed via GitHub Pages. The plaintext map body and
data are intentionally **not** in the repo — only AES-GCM ciphertext.

## Repo layout

```
build_data.py          ETL CLI: tax roll DBF → core.json + layers.json
buildlib/              Pure-Python helpers extracted from build_data.py
  io_utils.py            DBF/XLSX/CSV readers, coord transforms, CC list
  spatial.py             point-in-polygon, OLS fit, column means
  scoring.py             gap residuals, Gi*, DPI, uptake ratios, slopes,
                         _flag_low_confidence
  census.py              Census ACS fetch + disk cache, OSRM drive times
  pipeline.py            Assemble + write core.json/layers.json (extracted)
encrypt_data.py        Encrypt → public/ for deploy. Supports v1 (legacy
                       single-tier) and v2 (dual-tier, 600k PBKDF2).
                       --rotate-tier rotates one tier without touching
                       the other's ciphertext.
decrypt_data.py        Inverse of encrypt_data — reads .enc files and
                       writes plaintext (gitignored). --check verifies
                       a deploy bundle decrypts without writing plaintext.
patch_body.py          Apply named patches to a local plaintext body.
                       Idempotent (each patch carries a marker comment
                       it tags into the file). See PATCHES list at bottom.
index.html             The loader (ciphertext stays); prompts for password,
                       fetches+decrypts, document.write()s the body.
data/
  core.json.enc          Public-tier (in v2). Tract aggregates.
  layers.json.enc        Staff-tier. Parcel-level layers.
  enc_manifest.json      Public manifest: salt(s), iterations/KDF, tier→file map.
  acs_cache/             gitignored disk cache for fetch_*_acs
index_body.html.enc    Staff-tier ciphertext. The plaintext body is
                       NOT in the repo and never should be.
scripts/               Operator scripts that aren't part of the rebuild:
                         enrich_core.py, cluster_snapshot.py,
                         reconcile_tax_roll.py, fetch_chas.py,
                         fetch_evictions.py, merge_outreach_dose.py
tests/                 unittest suite. Run with: python -m unittest discover -s tests
```

## Critical workflow facts

### The body is encrypted and the plaintext stays off-disk

Frontend changes (anything in `index_body.html`) require this ritual:

```bash
python decrypt_data.py --staff-password "$STAFF_PW" --src . --out _work
python patch_body.py _work/index_body.html
python encrypt_data.py --public-password "$PUB" --staff-password "$STAFF_PW" \
    --body _work/index_body.html --out .
rm -rf _work
```

`patch_body.py` is the canonical edit path — direct text edits to a
plaintext body get lost on rebuild. Each entry in the `PATCHES` list
is a named, idempotent transform with a unique marker comment so
re-running is a no-op.

`make patch` and `make encrypt` (see Makefile) wrap this. Use them.

### Patch idempotency markers

Each `patch_body.py` PATCHES entry is either a 3-tuple
`(name, old, new)` (legacy) or a 4-tuple `(name, old, new, marker)`.

**Use the 4-tuple form whenever the new content embeds the old anchor
as a substring.** Example: appending to a `<script>` block whose marker
is the only unique substring you can rely on after the patch lands.
Without the marker, the engine sees `old` still present after apply
and re-injects on the next run. `tests/test_patch_body.py` enforces
that every shipped P5+ patch has a marker.

### Two-tier encryption

- **public** tier: `core.json.enc` only. Lower-risk aggregates.
- **staff** tier: `layers.json.enc` + `index_body.html.enc`. Parcel-level data + the full UI.
- A staff password unlocks both tiers (the loader derives both keys
  from it). A public password unlocks only `core.json`.
- Use `encrypt_data.py --rotate-tier {public,staff}` to rotate one
  tier's salt without touching the other's ciphertext.

### v1 vs v2 manifest

- v1 = single password, 200k PBKDF2. **Legacy.** `--v1` prints a
  deprecation warning.
- v2 = dual-tier, 600k PBKDF2, default. New deploys should use v2.
- The loader speaks both for backwards compatibility.

## Conventions

### Per-year fields

Roll-derived fields use `_YY` suffixes (last two digits of the tax year):
`pct_hoh_23`, `outreach_need_24`, `dpi_22`. The frontend's year scrubber
and `propYrFields` map (in `patch_body.py` P4) flip these en masse.

### Adding a new analytical layer

1. Compute it in `buildlib/scoring.py`. Write per-year (`field_YY`)
   if it varies by year.
2. Register it in `tests/test_schema.py::EXPECTED_PER_YEAR_BASES` so
   the schema snapshot test catches a silent rename later.
3. Add it to `propYrFields` (patch P4 in `patch_body.py`).
4. Add a radio + legend entry to the body HTML (operator-side patch).
5. Document the layer's meaning + caveats in `REPORTS_V1` methodology
   page (P9 in `patch_body.py`).

### Tests

`python -m unittest discover -s tests` runs everything (~70 tests).
The crypto tests need real `cryptography` — `pip install cryptography`
upgrades the system version if the Rust bindings panic.

## Common pitfalls

- **Don't direct-edit `index_body.html`.** The plaintext is
  intermediate state; changes go through `patch_body.py`.
- **Don't commit plaintext data files.** `.gitignore` covers
  `core.json` / `layers.json` / `index_body.html` and the `acs_cache/`
  directory. The pre-commit hook (see `.githooks/pre-commit`) refuses
  the commit if any slip through.
- **Don't bump `--v1` defaults silently.** It's a deliberate operator
  choice; the warning is the friction.
- **Watch for `final_layers` vs `existing_layers`** when touching
  `build_data.py:main()`. The pipeline extraction in
  `buildlib/pipeline.py` returns sizes; `final_layers` is still
  computed locally for the sidebar update / logging that follows.

## When in doubt

- Work on a feature branch named `claude/<topic>`.
- Open a PR, don't push to `main`.
- Run the test suite before pushing.
- Ship in small batches with focused commit messages.
- Update `ROADMAP.md` for anything you defer.
