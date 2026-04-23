"""Census ACS and OSRM drive-time fetchers, with disk cache.

All network I/O lives here. The `urlopen` binding is intentionally module
level so tests can monkeypatch it cleanly.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.request import urlopen, Request  # re-exported via build_data for tests
from urllib.error import URLError


ACS_CACHE_DIR = 'acs_cache'
ACS_CACHE_TTL_DAYS = 30


def _acs_cache_path(outdir, name):
    return Path(outdir) / ACS_CACHE_DIR / f'{name}.json'


def _acs_cache_read(outdir, name, ttl_days):
    """Return cached payload if present and fresher than ttl_days, else None."""
    path = _acs_cache_path(outdir, name)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            blob = json.load(f)
    except Exception as e:
        print(f"  ACS cache read failed ({path.name}): {e}")
        return None
    fetched_at = blob.get('fetched_at', 0)
    age_s = time.time() - fetched_at
    if age_s > ttl_days * 86400:
        return None
    return blob


def _acs_cache_write(outdir, name, payload):
    path = _acs_cache_path(outdir, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'fetched_at': time.time(), **payload}, f)
    except Exception as e:
        print(f"  ACS cache write failed ({path.name}): {e}")


def _merge_tract_acs(feats, by_geoid):
    """Copy the per-geoid ACS dict onto each matching TRACT_GEO feature."""
    merged = 0
    for feat in feats:
        geoid = feat.get('properties', {}).get('GEOID', '')
        if geoid in by_geoid:
            for k, v in by_geoid[geoid].items():
                feat['properties'][k] = v
            merged += 1
    return merged


def fetch_tract_acs(tract_geo, outdir='data', use_cache=True, refresh=False,
                    ttl_days=ACS_CACHE_TTL_DAYS):
    """Merge tract-level ACS 5-year variables into TRACT_GEO.features.

    Adds: poverty_rate, median_age, spanish_at_home, elderly_alone.
    Silently no-ops on network failure so the frontend degrades gracefully
    (tract layers other than addr_density will just render as missing).

    Caches the per-geoid ACS dict in data/acs_cache/tracts.json so
    subsequent runs skip the Census API entirely.
    """
    if not tract_geo or not tract_geo.get('features'):
        return
    feats = tract_geo['features']

    if use_cache and not refresh:
        cached = _acs_cache_read(outdir, 'tracts', ttl_days)
        if cached and cached.get('by_geoid'):
            by_geoid = cached['by_geoid']
            yr = cached.get('acs_year')
            merged = _merge_tract_acs(feats, by_geoid)
            print(f"  Tract ACS {yr} (cached): merged {merged}/{len(feats)} tracts")
            return

    vars_ = ','.join([
        'B01003_001E','B01002_001E','B17001_001E','B17001_002E',
        'C16001_001E','C16001_005E','B11007_001E','B11007_003E',
    ])
    for yr in [2023, 2022, 2021]:
        url = (
            f'https://api.census.gov/data/{yr}/acs/acs5?get={vars_}'
            f'&for=tract:*&in=state:35+county:001'
        )
        try:
            with urlopen(url, timeout=10) as resp:
                rows = json.loads(resp.read())
        except (URLError, Exception) as e:
            print(f"  Tract ACS {yr} failed: {e}")
            continue
        header = rows[0]
        idx = {k: header.index(k) for k in header}
        by_geoid = {}
        for row in rows[1:]:
            geoid = f"{row[idx['state']]}{row[idx['county']]}{row[idx['tract']]}"

            def _vf(k):
                """Parse an ACS cell as a float. ACS returns decimals for
                values like median age, and uses large negative sentinels
                (e.g. -666666666) for missing/suppressed data. Normalize
                both to None so downstream math doesn't blow up."""
                try:
                    raw = row[idx[k]]
                    if raw in (None, '', '-', '*'):
                        return None
                    v = float(raw)
                    if v < -1e6:
                        return None
                    return v
                except (ValueError, TypeError):
                    return None

            def _vi(k):
                v = _vf(k)
                return int(v) if v is not None else None

            pop = _vi('B01003_001E') or 0
            pov_univ = _vi('B17001_001E') or 0
            pov_below = _vi('B17001_002E') or 0
            lang_univ = _vi('C16001_001E') or 0
            spanish = _vi('C16001_005E') or 0
            hh_univ = _vi('B11007_001E') or 0
            elderly_alone = _vi('B11007_003E') or 0
            median_age = _vf('B01002_001E')
            by_geoid[geoid] = {
                'poverty_rate': round(pov_below / pov_univ, 4) if pov_univ else None,
                'median_age': round(median_age, 1) if median_age is not None else None,
                'spanish_at_home': round(spanish / lang_univ, 4) if lang_univ else None,
                'elderly_alone': round(elderly_alone / hh_univ, 4) if hh_univ else None,
                'acs_year': yr,
                'tract_pop': pop,
            }
        merged = _merge_tract_acs(feats, by_geoid)
        print(f"  Tract ACS {yr}: merged {merged}/{len(feats)} tracts")
        if use_cache:
            _acs_cache_write(outdir, 'tracts', {'acs_year': yr, 'by_geoid': by_geoid})
        return


def fetch_census_acs(outdir='data', use_cache=True, refresh=False,
                     ttl_days=ACS_CACHE_TTL_DAYS):
    """Fetch ACS 5-year data for Bernalillo County from Census API.

    County and ZIP queries fall through independently: if the 2023 county
    query succeeds but the 2023 ZIP query fails, we still try 2022 / 2021
    ZIPs instead of shipping a ZIP-less build.

    Caches the combined {county, zips} result under data/acs_cache/census.json.
    """
    if use_cache and not refresh:
        cached = _acs_cache_read(outdir, 'census', ttl_days)
        if cached and (cached.get('county') or cached.get('zips')):
            result = {k: v for k, v in cached.items() if k != 'fetched_at'}
            parts = []
            if result.get('county'):
                parts.append(f"county {result['county'].get('year')}")
            if result.get('zips'):
                parts.append(f"{len(result['zips'])} ZCTAs")
            print(f"  Census ACS (cached): {', '.join(parts) if parts else 'empty'}")
            return result
    vars = 'NAME,B01001_001E,B19013_001E,B17001_002E,B02001_002E,B02001_003E,B02001_004E,B02001_005E,B03003_003E,B25001_001E,B25077_001E'
    result = {}
    for yr in [2023, 2022, 2021]:
        if 'county' in result and 'zips' in result:
            break
        if 'county' not in result:
            url = f'https://api.census.gov/data/{yr}/acs/acs5?get={vars}&for=county:001&in=state:35'
            try:
                with urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read())
                    h, v = data[0], data[1]

                    def _cv(k):
                        raw = v[h.index(k)]
                        try:
                            x = int(raw) if raw not in (None, '', '-', '*') else 0
                        except (ValueError, TypeError):
                            return 0
                        return x if x >= 0 else 0

                    pop = _cv('B01001_001E')
                    result['county'] = {
                        'year': yr, 'name': v[h.index('NAME')], 'population': pop,
                        'median_income': _cv('B19013_001E'),
                        'poverty': _cv('B17001_002E'),
                        'hispanic': _cv('B03003_003E'),
                        'white': _cv('B02001_002E'),
                        'black': _cv('B02001_003E'),
                        'native_american': _cv('B02001_004E'),
                        'asian': _cv('B02001_005E'),
                        'housing_units': _cv('B25001_001E'),
                        'median_home_value': _cv('B25077_001E'),
                    }
                    print(f"  County ACS {yr}: pop {pop:,}")
            except (URLError, Exception) as e:
                print(f"  County ACS {yr} failed: {e}")

        bern_zips = {
            '87002','87004','87008','87015','87031','87035','87042','87043',
            '87047','87048','87059','87068','87101','87102','87103','87104',
            '87105','87106','87107','87108','87109','87110','87111','87112',
            '87113','87114','87116','87117','87119','87120','87121','87122',
            '87123','87124','87131','87144','87153','87154','87158','87176',
            '87181','87184','87185','87187','87190','87191','87192','87193',
            '87194','87196','87197','87198','87199',
        }
        if 'zips' not in result:
            zip_vars = 'NAME,B01001_001E,B19013_001E,B17001_002E,B03003_003E,B25001_001E,B25077_001E'
            zip_url = f'https://api.census.gov/data/{yr}/acs/acs5?get={zip_vars}&for=zip%20code%20tabulation%20area:*'
            try:
                with urlopen(zip_url, timeout=10) as resp:
                    zdata = json.loads(resp.read())
                    zh = zdata[0]
                    zips = {}

                    def _zv(row, col):
                        raw = row[zh.index(col)]
                        try:
                            v = int(raw) if raw not in (None, '', '-', '*') else 0
                        except (ValueError, TypeError):
                            return 0
                        return v if v >= 0 else 0

                    for row in zdata[1:]:
                        zcta = row[zh.index('zip code tabulation area')]
                        if zcta not in bern_zips:
                            continue
                        zpop = _zv(row, 'B01001_001E')
                        if zpop == 0:
                            continue
                        zips[zcta] = {
                            'name': row[zh.index('NAME')],
                            'pop': zpop,
                            'income': _zv(row, 'B19013_001E'),
                            'poverty': _zv(row, 'B17001_002E'),
                            'hispanic': _zv(row, 'B03003_003E'),
                            'units': _zv(row, 'B25001_001E'),
                            'home_val': _zv(row, 'B25077_001E'),
                        }
                    result['zips'] = zips
                    print(f"  ZIP ACS {yr}: {len(zips)} ZCTAs in Bernalillo area")
            except (URLError, Exception) as e:
                print(f"  ZIP ACS {yr} failed: {e}")
    if result and use_cache:
        _acs_cache_write(outdir, 'census', result)
    return result if result else None


def fetch_drive_times_osrm(centroid_lookup, cc_coords, outdir,
                           osrm_url='https://router.project-osrm.org',
                           timeout=60, batch_size=90):
    """Query OSRM's Table service for driving duration from each community
    center to every neighborhood centroid. Returns {nbhd_id: minutes} using
    the minimum duration across all community centers.

    Results are cached in data/osrm_drive_times.json so only the first build
    after a change to centroid_lookup or cc_coords hits the network.
    """
    cache_path = Path(outdir) / 'osrm_drive_times.json'
    nbhd_ids = sorted(centroid_lookup.keys())
    fingerprint_parts = [
        ';'.join(f'{n}:{centroid_lookup[n][0]:.4f},{centroid_lookup[n][1]:.4f}'
                 for n in nbhd_ids),
        ';'.join(f'{cc[0]}:{cc[1]:.4f},{cc[2]:.4f}' for cc in cc_coords),
    ]
    fingerprint = hashlib.sha1('\n'.join(fingerprint_parts).encode()).hexdigest()[:12]

    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get('fingerprint') == fingerprint:
                print(f"  OSRM drive times: cache hit ({len(cached.get('times', {}))} nbhds)")
                return {int(k): v for k, v in cached.get('times', {}).items()}
        except Exception as e:
            print(f"  OSRM drive-time cache read failed: {e}")

    print(f"  OSRM drive times: querying {osrm_url} for {len(nbhd_ids)} nbhds × {len(cc_coords)} CCs...")

    cc_lonlat = [(cc[2], cc[1]) for cc in cc_coords]
    num_ccs = len(cc_lonlat)
    times = {}
    batches = [nbhd_ids[i:i + batch_size] for i in range(0, len(nbhd_ids), batch_size)]

    for batch_idx, batch in enumerate(batches):
        batch_lonlat = [(centroid_lookup[n][1], centroid_lookup[n][0]) for n in batch]
        all_coords = cc_lonlat + batch_lonlat
        coord_str = ';'.join(f'{lon:.5f},{lat:.5f}' for lon, lat in all_coords)
        sources = ';'.join(str(i) for i in range(num_ccs))
        destinations = ';'.join(str(num_ccs + i) for i in range(len(batch)))
        url = (
            f'{osrm_url.rstrip("/")}/table/v1/driving/{coord_str}'
            f'?sources={sources}&destinations={destinations}&annotations=duration'
        )
        if batch_idx > 0:
            time.sleep(0.3)
        req = Request(url, headers={
            'User-Agent': 'map-bernalillo-outreach/1.0 (+build_data.py)',
            'Accept': 'application/json',
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except (URLError, Exception) as e:
            print(f"  OSRM batch {batch_idx + 1}/{len(batches)} failed: {e}")
            return times
        code = data.get('code')
        if code == 'TooBig':
            if batch_size > 20:
                print(f"  OSRM TooBig at batch_size={batch_size}; retrying whole run at {batch_size // 2}")
                return fetch_drive_times_osrm(
                    centroid_lookup, cc_coords, outdir,
                    osrm_url=osrm_url, timeout=timeout, batch_size=batch_size // 2,
                )
        if code != 'Ok':
            print(f"  OSRM returned: {data.get('message', code or 'unknown')}")
            return times
        durations = data.get('durations') or []
        for j, nbhd_id in enumerate(batch):
            col = [durations[i][j] for i in range(num_ccs)
                   if i < len(durations) and j < len(durations[i]) and durations[i][j] is not None]
            if col:
                times[nbhd_id] = round(min(col) / 60.0, 1)

    try:
        cache_path.parent.mkdir(exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump({
                'fingerprint': fingerprint,
                'source': osrm_url,
                'times': {str(k): v for k, v in times.items()},
            }, f)
        print(f"  OSRM drive times: cached {len(times)} nbhds to {cache_path}")
    except Exception as e:
        print(f"  OSRM drive-time cache write failed: {e}")

    return times
