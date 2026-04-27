# Roadmap

Tracking the items from the brainstorm thread that aren't yet shipped.
Anything in this file is intentionally deferred — either because it
requires inputs/credentials/data not in the repo, or because it's a
multi-day project that doesn't fit a single review cycle.

## Shipped on `claude/brainstorm-SjOC5`

- `encrypt_data.py --v1` now prints a deprecation warning. v1 stays
  decryptable in the loader so legacy deploys keep working, but new
  encrypts should drop `--v1` in favor of dual-tier v2 (600k PBKDF2).
- `buildlib/scoring.py::_flag_low_confidence` writes `low_confidence`
  and `low_confidence_reason` onto neighborhoods whose underlying
  signals are too thin for stable scoring (parcels < 100 OR
  tract_pop < 500). MOE-proxy until real ACS margins are fetched.
- `tests/test_schema.py` snapshots the per-year and scalar key set
  the scoring pipeline produces, so a silent rename or dropped layer
  trips the test suite.
- `patch_body.py` gains the `MAP_EXT_V1` injection — three frontend
  features (permalinks, split-screen via second window, worklist CSV
  export) added in one end-of-body `<script>` block. Operator must
  run `decrypt_data.py → patch_body.py → encrypt_data.py` to deploy.

## Blocked — needs external inputs

These items can't be merged from this repo alone.

### Real ACS margins of error

`buildlib/census.py` only fetches the `_E` (estimate) variables today.
To upgrade `_flag_low_confidence` from a sparsity-based proxy to a
true MOE/CV threshold, also fetch the matching `_M` variables (e.g.
`B17001_002M` for poverty count) and store them per-tract. Then
flag when MOE/estimate > 0.4. Not blocked technically, just hadn't
been wired up in this branch.

### Nightly ACS rebuild — GitHub Action

A workflow that runs `build_data.py` on a schedule, diffs against the
current `data/core.json.enc`, and opens a PR if any tract changed
cluster category. Needs:

- `CENSUS_API_KEY` repo secret (Census ACS allows higher rate limits
  with a key; current build_data.py runs anonymous, which is fine for
  manual runs but flaky in CI).
- Decrypted access to the prior `core.json` for the diff — easiest
  approach is to commit a redacted `core_summary.json` (no parcel-level
  data) so diffs can run without unlocking the staff tier.

### Cloudflare Turnstile / rate-limit on the gate

The current loader has no friction beyond PBKDF2 600k iterations. To
slow brute force, gate the manifest fetch behind Turnstile (or a
Workers KV nonce) before the password attempt. Needs a Cloudflare
account on the deploy domain.

### Tax-roll ↔ ACS reconciliation

Cross-check parcel-level exemption uptake against ACS-derived
eligibility per tract. Surface tracts where the two diverge sharply.
Needs the parcel/exemption tax roll, which lives outside this repo
(it's an input to `build_data.py --roll`, not stored here).

### Outreach dose-response layer

Overlay where Assessor outreach $ / staff-hours have actually been
spent against `outreach_need_YY`. Closes the diagnostic-to-action
loop. Needs the Assessor's outreach spend data — not currently
public.

### HUD CHAS + eviction-filings layers

New ETL: pull HUD CHAS for renter cost-burden, UNM Eviction Lab for
filings density, join to tract centroids, write to `layers.json`.
Multi-day; requires evaluating data licenses for the gated deploy.

### Per-district / per-commissioner PDF export

Browser-only feature: select a commissioner district, render a one-
page PDF with map + summary stats. Needs:

- Bernalillo Commission district boundaries (publicly available, just
  hadn't been added to `layers.json`).
- jsPDF + html2canvas as bundled deps in the body — the body is
  currently dependency-free past Leaflet.
- Browser testing that I can't do from a sandbox.

### Bayesian small-area smoothing

Borrow strength from neighbors for tracts with low denominators rather
than dropping them under `low_confidence`. Research-grade — needs a
validation set against ground truth (e.g. parcel-level vs tract-level
HOH uptake rates).

### Tier rotation in `encrypt_data.py`

Today, rotating the staff salt forces re-encrypting public data too
because `--public-password` and `--staff-password` are taken in the
same invocation. Refactor to allow rotating one tier without touching
the other tier's ciphertext. Security-sensitive — wants explicit
review.

### Body-extension JS as separate file

The `MAP_EXT_V1` patch in `patch_body.py` inlines ~120 lines of JS
into the encrypted body. It would be cleaner to ship as a separate
decryptable asset (`data/body_ext.js.enc`) so it can iterate without
re-encrypting the full body. Needs the loader to handle a fourth
encrypted asset and the body to `eval` (or `Function`) it after
`document.write`. Worth the effort once the extensions stabilize.

## Considered & dropped

- **SRI hash on the loader.** The loader is the entry point — there
  are no external `<script src=…>` includes to SRI-check. The right
  defense for a tampered loader is signing or pinning the gateway
  domain, not SRI. Removed from the active list.
- **Manifest v1/v2 reconciliation as a code change.** On closer
  inspection there's no live mismatch — the v1 manifest matches the
  v1-encrypted ciphertext on disk. The deprecation warning above is
  the lighter-weight intervention.
- **Wholesale ETL pipeline extraction from `build_data.py`.** Still
  worth doing, but a 1,780-line refactor isn't safe inside a
  brainstorm branch. Track separately.
