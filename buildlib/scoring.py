"""Vulnerability, outreach, and per-year analytical scoring.

All pure functions over the nbhd_stats dict. No network, no file I/O.
"""
from __future__ import annotations

import re

from .spatial import _ols_fit, _mean_of


def _cap(v, ceil):
    """Clamp v/ceil to [0,1]. Shared by severity/vulnerability aggregators
    and the per-year DPI / uptake ratio computations below."""
    if not ceil:
        return 0
    if v is None:
        return 0
    try:
        return min(max(float(v) / ceil, 0.0), 1.0)
    except (TypeError, ValueError):
        return 0


def _noisy_or(*vals):
    """1 - Π(1 - vᵢ), clamped per-input to [0,1]. Bounded in [0,1]."""
    p = 1.0
    for v in vals:
        try:
            x = max(0.0, min(1.0, float(v) if v is not None else 0.0))
        except (TypeError, ValueError):
            x = 0.0
        p *= 1.0 - x
    return 1.0 - p


def _compute_exemption_gaps(nbhd_stats):
    """Populate hoh_gap / vet_gap / vf_gap per neighborhood — both for the
    latest year (bare field, e.g. 'hoh_gap') AND for every per-year field
    the roll produced.

    Each field: residual = actual − predicted, where predicted is either
    a single-predictor OLS against zip_poverty_rate or the county mean
    as a fallback when the regression has no data.
    """
    year_suffixes = set()
    year_pat = re.compile(r'^pct_hoh_(\d+)$')
    for p in nbhd_stats.values():
        for k in p.keys():
            m = year_pat.match(k)
            if m:
                year_suffixes.add(m.group(1))

    def _run_pair(base, predictor_field, gap_base, suffix):
        suf = f'_{suffix}' if suffix else ''
        field = f'{base}{suf}'
        predictor = predictor_field
        pairs = [(p.get(predictor), p.get(field)) for p in nbhd_stats.values()]
        fit = _ols_fit(pairs)
        mean = _mean_of(field, nbhd_stats)
        for p in nbhd_stats.values():
            v = p.get(field)
            if v is None:
                continue
            if fit and p.get(predictor) is not None:
                try:
                    pred = fit['intercept'] + fit['slope'] * float(p[predictor])
                except (TypeError, ValueError):
                    pred = mean
            else:
                pred = mean
            if pred is None:
                continue
            p[f'{gap_base}{suf}'] = round(v - pred, 4)

    def _run_mean_baseline(base, gap_base, suffix):
        """Vet gaps use a plain county-mean baseline (no ZIP predictor) —
        vet claims track Kirtland / VA proximity more than ZIP income."""
        suf = f'_{suffix}' if suffix else ''
        field = f'{base}{suf}'
        mean = _mean_of(field, nbhd_stats)
        if mean is None:
            return
        for p in nbhd_stats.values():
            v = p.get(field)
            if v is None:
                continue
            p[f'{gap_base}{suf}'] = round(v - mean, 4)

    _run_pair('pct_hoh', 'zip_poverty_rate', 'hoh_gap', '')
    _run_pair('pct_val_freeze', 'zip_poverty_rate', 'vf_gap', '')
    _run_mean_baseline('pct_vet', 'vet_gap', '')

    for ys in sorted(year_suffixes):
        _run_pair('pct_hoh', 'zip_poverty_rate', 'hoh_gap', ys)
        _run_pair('pct_val_freeze', 'zip_poverty_rate', 'vf_gap', ys)
        _run_mean_baseline('pct_vet', 'vet_gap', ys)


def _boost_outreach_with_gaps(nbhd_stats):
    """After exemption gaps are computed, bump outreach_need for neighborhoods
    that under-claim relative to their demographic prediction. The boost is
    capped so it augments the existing score rather than overriding it."""
    for p in nbhd_stats.values():
        need = p.get('outreach_need')
        if need is None:
            continue
        boost = 0.0
        hg = p.get('hoh_gap')
        if hg is not None and hg < -0.05:
            boost += min(abs(hg) - 0.05, 0.10)
        vfg = p.get('vf_gap')
        if vfg is not None and vfg < -0.03:
            boost += min(abs(vfg) - 0.03, 0.05)
        if boost > 0:
            p['outreach_need_gap_boost'] = round(boost, 4)
            p['outreach_need'] = round(min(1.0, need + boost), 4)


def _compute_gi_star_per_year(nbhd_stats, centroid_lookup, k=8):
    """Getis-Ord Gi* z-scores for the per-year outreach_need_YY and
    pct_vf_denied_YY series so the frontend's year selector can flip the
    hot/cold-spot cluster layers.

    Writes gi_outreach_need_YY and gi_pct_vf_denied_YY onto each nbhd.
    Skips a (field, year) pair entirely if <20 nbhds have finite values
    or the overall stdev collapses.
    """
    nbhds = sorted(nbhd_stats.keys())
    n_total = len(nbhds)
    if n_total <= k:
        return
    centers = [centroid_lookup.get(n) for n in nbhds]
    knn = {}
    for i, name in enumerate(nbhds):
        c = centers[i]
        if not c:
            knn[name] = None
            continue
        dists = []
        for j, other in enumerate(nbhds):
            oc = centers[j]
            if not oc:
                continue
            dy = c[0] - oc[0]
            dx = c[1] - oc[1]
            dists.append((nbhds[j], dx * dx + dy * dy))
        dists.sort(key=lambda d: d[1])
        knn[name] = [pair[0] for pair in dists[:k]]

    year_suffixes = set()
    for p in nbhd_stats.values():
        for key in p.keys():
            m = re.match(r'^pct_vf_denied_(\d+)$', key)
            if m:
                year_suffixes.add(m.group(1))

    for base in ('outreach_need', 'pct_vf_denied'):
        for ys in sorted(year_suffixes):
            field = f'{base}_{ys}'
            values = {}
            flat = []
            for name in nbhds:
                v = nbhd_stats[name].get(field)
                if v is None:
                    continue
                if not isinstance(v, (int, float)) or v != v:
                    continue
                values[name] = v
                flat.append(v)
            if len(flat) < 20:
                continue
            mean = sum(flat) / len(flat)
            sq = sum((v - mean) ** 2 for v in flat)
            stdev = (sq / (len(flat) - 1)) ** 0.5 if len(flat) > 1 else 0
            if stdev <= 0:
                continue
            for name in nbhds:
                neighbors = knn.get(name)
                if not neighbors:
                    nbhd_stats[name][f'gi_{field}'] = None
                    continue
                local_sum = 0.0
                local_cnt = 0
                for m in neighbors:
                    v = values.get(m)
                    if v is not None:
                        local_sum += v
                        local_cnt += 1
                if local_cnt < k / 2:
                    nbhd_stats[name][f'gi_{field}'] = None
                    continue
                scale = stdev * ((local_cnt * (n_total - local_cnt)) / (n_total - 1)) ** 0.5
                if scale <= 0:
                    nbhd_stats[name][f'gi_{field}'] = None
                    continue
                nbhd_stats[name][f'gi_{field}'] = round(
                    (local_sum - local_cnt * mean) / scale, 4,
                )


def _compute_dpi_per_year(nbhd_stats):
    """Displacement Pressure Index per year.

    DPI_YY combines *who is cycling through* (owner_turnover_YY, hoh_churn_YY)
    with *affordability pressure* (val_change_pct, tract_poverty_rate or
    zip_poverty_rate). Both a displacement and a pressure signal must be
    non-trivial for a high score — so bedroom communities with steady value
    growth don't light up the map just because they're expensive.
    """
    year_pat = re.compile(r'^owner_turnover_(\d+)$')
    for p in nbhd_stats.values():
        years = set()
        for k in p.keys():
            m = year_pat.match(k)
            if m:
                years.add(m.group(1))
        if not years:
            continue
        poverty = (p.get('tract_poverty_rate')
                   if p.get('tract_poverty_rate') is not None
                   else p.get('zip_poverty_rate'))
        pressure = _noisy_or(_cap(p.get('val_change_pct'), 0.5),
                             _cap(poverty, 0.25))
        for ys in sorted(years):
            turn = _cap(p.get(f'owner_turnover_{ys}'), 0.25)
            churn = _cap(p.get(f'hoh_churn_{ys}'), 0.05)
            displacement = _noisy_or(turn, churn)
            p[f'dpi_{ys}'] = round(displacement * pressure, 4)


def _compute_uptake_ratios(nbhd_stats):
    """Exemption uptake as a RATIO of actual/predicted (complement to the
    residual gaps in _compute_exemption_gaps). Intuitive scale: 1.0 = as
    predicted, 0.5 = claiming only half what peers do, 1.5 = over-claim.
    """
    year_pat = re.compile(r'^pct_(?:hoh|vet|val_freeze)_(\d+)$')
    year_suffixes = set()
    for p in nbhd_stats.values():
        for k in p.keys():
            m = year_pat.match(k)
            if m:
                year_suffixes.add(m.group(1))
    RATIO_CEIL = 3.0

    def _run_ratio(base, predictor, out_base, suffix, use_predictor=True):
        suf = f'_{suffix}' if suffix else ''
        field = f'{base}{suf}'
        pairs = [(p.get(predictor), p.get(field)) for p in nbhd_stats.values()]
        fit = _ols_fit(pairs) if use_predictor else None
        mean = _mean_of(field, nbhd_stats)
        if mean is None:
            return
        for p in nbhd_stats.values():
            v = p.get(field)
            if v is None:
                continue
            if fit and use_predictor and p.get(predictor) is not None:
                try:
                    pred = fit['intercept'] + fit['slope'] * float(p[predictor])
                except (TypeError, ValueError):
                    pred = mean
            else:
                pred = mean
            if not pred or pred <= 0:
                continue
            p[f'{out_base}{suf}'] = round(min(v / pred, RATIO_CEIL), 4)

    _run_ratio('pct_hoh', 'zip_poverty_rate', 'hoh_uptake', '')
    _run_ratio('pct_val_freeze', 'zip_poverty_rate', 'vf_uptake', '')
    _run_ratio('pct_vet', None, 'vet_uptake', '', use_predictor=False)
    for ys in sorted(year_suffixes):
        _run_ratio('pct_hoh', 'zip_poverty_rate', 'hoh_uptake', ys)
        _run_ratio('pct_val_freeze', 'zip_poverty_rate', 'vf_uptake', ys)
        _run_ratio('pct_vet', None, 'vet_uptake', ys, use_predictor=False)


def _compute_trend_slopes(nbhd_stats):
    """OLS slope of each per-year series — sign and magnitude of the
    multi-year trend. Slope is in units-per-year. Skipped when fewer
    than 4 years exist.
    """
    BASES = ('outreach_need', 'pct_hoh', 'pct_vet',
             'pct_val_freeze', 'hoh_churn')

    def _slope(pairs):
        n = len(pairs)
        if n < 4:
            return None
        sx = sum(x for x, _ in pairs)
        sy = sum(y for _, y in pairs)
        sxx = sum(x * x for x, _ in pairs)
        sxy = sum(x * y for x, y in pairs)
        mx = sx / n
        vx = sxx - n * mx * mx
        if vx <= 0:
            return None
        return (sxy - n * mx * (sy / n)) / vx

    for p in nbhd_stats.values():
        for base in BASES:
            pat = re.compile(rf'^{base}_(\d+)$')
            pairs = []
            for k, v in p.items():
                m = pat.match(k)
                if not m or v is None or not isinstance(v, (int, float)):
                    continue
                yr = int(m.group(1))
                yr_full = 2000 + yr if yr < 80 else 1900 + yr
                pairs.append((yr_full, float(v)))
            pairs.sort()
            s = _slope(pairs)
            if s is not None:
                p[f'{base}_slope'] = round(s, 6)


def _compute_persistence(nbhd_stats, base='outreach_need', decile=0.9,
                         min_streak=3):
    """Persistence score — "who has been chronically bad."

    For the given base (default outreach_need), finds the top-decile
    threshold in each year across all neighborhoods, then for each
    neighborhood computes:

      {base}_persistence_total     — count of years the nbhd was in the
                                      top decile (any years)
      {base}_persistence_streak    — longest consecutive-year streak in
                                      the top decile
      {base}_persistence_recent    — number of consecutive recent years
                                      (ending at the latest available
                                      year) in the top decile; 0 if the
                                      nbhd isn't currently top-decile

    The thresholds are computed PER YEAR so the score measures how often
    a nbhd is among the worst-served relative to peers, not against an
    absolute cutoff that would favor years with globally worse numbers.

    "Top decile" uses the 90th percentile (configurable via `decile`).
    Sparse / missing years are skipped rather than counted as "not in
    top decile" — otherwise the score would punish nbhds for existing
    across more years.
    """
    year_pat = re.compile(rf'^{base}_(\d+)$')
    # Collect per-year series.
    years = set()
    for p in nbhd_stats.values():
        for k in p.keys():
            m = year_pat.match(k)
            if m:
                years.add(int(m.group(1)))
    if not years:
        return
    years = sorted(years)

    # Per-year threshold = `decile`th quantile of finite non-zero values.
    # Zero typically means "no data" for this base (see outreach_need_5 /
    # avg_appraised_5 which are 0 on every nbhd), so including them would
    # drag the threshold down artificially.
    thresholds = {}
    for yr in years:
        field = f'{base}_{yr}'
        vals = []
        for p in nbhd_stats.values():
            v = p.get(field)
            if v is None or not isinstance(v, (int, float)):
                continue
            if v != v:  # NaN
                continue
            if v == 0:
                continue
            vals.append(v)
        if len(vals) < 20:
            continue
        vals.sort()
        idx = max(0, min(len(vals) - 1, int(round(decile * (len(vals) - 1)))))
        thresholds[yr] = vals[idx]
    if not thresholds:
        return

    usable_years = sorted(thresholds.keys())
    latest = usable_years[-1]

    for p in nbhd_stats.values():
        in_top = {}  # year -> bool (only for years with a threshold)
        for yr in usable_years:
            v = p.get(f'{base}_{yr}')
            if v is None or not isinstance(v, (int, float)) or v != v or v == 0:
                continue
            in_top[yr] = v >= thresholds[yr]
        if not in_top:
            continue

        # Total years top-decile.
        total = sum(1 for v in in_top.values() if v)

        # Longest consecutive streak — iterate usable_years in order,
        # but only count runs where CONSECUTIVE YEARS (by calendar) are
        # all top-decile. Gaps in the data break the streak.
        longest = cur = 0
        prev_yr = None
        for yr in usable_years:
            if yr not in in_top:
                prev_yr = None
                cur = 0
                continue
            if in_top[yr]:
                if prev_yr is not None and yr == prev_yr + 1:
                    cur += 1
                else:
                    cur = 1
                longest = max(longest, cur)
            else:
                cur = 0
            prev_yr = yr

        # Recent streak — ending at the latest year. 0 if the nbhd isn't
        # currently top-decile.
        recent = 0
        expected = latest
        for yr in reversed(usable_years):
            if yr != expected:
                break
            if in_top.get(yr):
                recent += 1
                expected -= 1
            else:
                break

        p[f'{base}_persistence_total'] = total
        p[f'{base}_persistence_streak'] = longest
        p[f'{base}_persistence_recent'] = recent
        p[f'{base}_persistence_chronic'] = bool(longest >= min_streak)
