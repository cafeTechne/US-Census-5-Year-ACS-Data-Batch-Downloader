# SPDX-License-Identifier: AGPL-3.0-or-later
"""Time and overlapping-period designs (PLAN_ROUND_8.md workstream G).

ACS 5-year releases ending in consecutive years share 4 of 5 sample years, so
a panel of annual 5-year estimates carries far fewer independent periods than
it has rows. :func:`overlap_design_check` reports this and suggests
non-overlapping releases; :func:`linear_trend_test` tests a linear trend
across >= 3 periods with known sampling variances by GLS.

Overlap covariance: Census publishes only the approximation
Var(X1 - X2) ~= (1 - C)(SE1^2 + SE2^2), C = overlapping years / 5 (Round 7,
census.stats.overlap_factor). The implied covariance
Cov(X1, X2) = C (SE1^2 + SE2^2) / 2 is used when overlapping periods are
explicitly allowed; it is an approximation, and exact cross-release
covariances are not published.
"""

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from scipy import stats as sps

from census.stats import ACS_MOE_Z, acs5_overlap_years

PERIOD_YEARS = 5
OVERLAP_MODES = ("error", "census_approx", "independent")


def suggest_nonoverlapping(years: Sequence[int], period_years: int = PERIOD_YEARS) -> List[int]:
    """Largest set of releases with no shared sample years, anchored on the latest."""
    chosen: List[int] = []
    for y in sorted({int(y) for y in years}, reverse=True):
        if not chosen or chosen[-1] - y >= period_years:
            chosen.append(y)
    return sorted(chosen)


def overlap_design_check(years: Sequence[int], period_years: int = PERIOD_YEARS) -> Dict[str, Any]:
    """Overlap diagnostics for a panel of ACS 5-year releases (by end year).

    ``effective_independent_periods`` = distinct sample years spanned / 5
    (capped at the number of releases), e.g. 2014-2023 annual releases span
    sample years 2010-2023 -> 14/5 = 2.8, not 10.
    """
    ys = sorted({int(y) for y in years})
    if not ys:
        raise ValueError("no years given")
    pairs = []
    for i, a in enumerate(ys):
        for b in ys[i + 1:]:
            ov = acs5_overlap_years(a, b) if period_years == PERIOD_YEARS else max(0, period_years - abs(a - b))
            if ov > 0:
                pairs.append({"year1": a, "year2": b, "overlap_years": ov})
    covered = set()
    for y in ys:
        covered.update(range(y - period_years + 1, y + 1))
    effective = min(len(ys), len(covered) / period_years)
    suggested = suggest_nonoverlapping(ys, period_years)
    notes = []
    if pairs:
        notes.append(
            f"{len(pairs)} pair(s) of releases share sample years: treating them as independent "
            f"overstates precision. {len(ys)} releases carry about {effective:.1f} independent periods. "
            f"For confirmatory trend tests use non-overlapping releases {suggested}."
        )
    return {
        "years": ys, "n_releases": len(ys), "overlapping": bool(pairs),
        "overlapping_pairs": pairs, "sample_years_covered": len(covered),
        "effective_independent_periods": effective,
        "suggested_nonoverlapping": suggested, "notes": notes,
    }


def linear_trend_test(
    years: Sequence[float],
    estimates: Sequence[float],
    moes: Optional[Sequence[float]] = None,
    ses: Optional[Sequence[float]] = None,
    confidence_level: float = 0.90,
    overlap: str = "error",
    moe_confidence_z: float = ACS_MOE_Z,
) -> Dict[str, Any]:
    """GLS test of a linear trend (slope per year) across >= 3 periods.

    Model y_t = a + b t + e_t with Var(e) = Sigma known (diagonal SE^2 for
    non-overlapping periods). b^ = (X'S^-1X)^-1 X'S^-1 y, z = b^ / SE(b^),
    two-sided normal p-value. A lack-of-fit chi-square (residuals vs known
    variances, df = n - 2) checks whether a straight line is adequate. With two
    periods this reduces to the Round 7 z-test of the difference.

    ``overlap``: ``"error"`` (default) refuses overlapping 5-year periods and
    names non-overlapping alternatives; ``"census_approx"`` uses the Census
    approximate covariance; ``"independent"`` ignores overlap (flagged).
    """
    if overlap not in OVERLAP_MODES:
        raise ValueError(f"overlap must be one of {OVERLAP_MODES}")
    t = np.asarray(years, dtype=float)
    y = np.asarray(estimates, dtype=float)
    if (moes is None) == (ses is None):
        raise ValueError("give exactly one of moes or ses")
    se = np.abs(np.asarray(ses if ses is not None else moes, dtype=float))
    if moes is not None:
        se = se / moe_confidence_z
    if not (len(t) == len(y) == len(se)):
        raise ValueError("years, estimates and MOEs/SEs must have equal length")
    if len(t) < 3:
        raise ValueError("a linear trend test needs >= 3 periods; for two use census.stats.z_test")
    if len(set(t.tolist())) != len(t):
        raise ValueError("years must be distinct")
    if not (np.all(np.isfinite(y)) and np.all(np.isfinite(se)) and np.all(se > 0)):
        raise ValueError("every period needs a finite estimate and a positive finite SE/MOE")
    design = overlap_design_check([int(v) for v in t])
    notes: List[str] = []
    S = np.diag(se ** 2)
    if design["overlapping"]:
        if overlap == "error":
            raise ValueError(
                "periods overlap (shared ACS sample years); use non-overlapping releases "
                f"{design['suggested_nonoverlapping']} or pass overlap='census_approx'. "
                + " ".join(design["notes"])
            )
        if overlap == "census_approx":
            for i in range(len(t)):
                for j in range(len(t)):
                    if i != j:
                        c = acs5_overlap_years(int(t[i]), int(t[j])) / PERIOD_YEARS
                        S[i, j] = c * (se[i] ** 2 + se[j] ** 2) / 2.0
            if np.min(np.linalg.eigvalsh(S)) <= 0:
                raise ValueError("Census-approximate overlap covariance is not positive definite "
                                 "for these SEs; use non-overlapping releases")
            notes.append("overlapping periods: covariance from the Census approximation "
                         "Cov = C (SE1^2 + SE2^2) / 2 (approximate; exact covariance unpublished)")
        else:
            notes.append("overlapping periods treated as independent: SEs are understated")
        notes.extend(design["notes"])
    X = np.column_stack([np.ones_like(t), t - t.mean()])
    Si = np.linalg.inv(S)
    cov = np.linalg.inv(X.T @ Si @ X)
    beta = cov @ X.T @ Si @ y
    slope, se_slope = float(beta[1]), float(math.sqrt(cov[1, 1]))
    z = slope / se_slope
    level = confidence_level if confidence_level <= 1 else confidence_level / 100
    zc = float(sps.norm.ppf(1 - (1 - level) / 2))
    p = float(2 * sps.norm.sf(abs(z)))
    r = y - X @ beta
    chi2 = float(r @ Si @ r)
    df = len(t) - 2
    return {
        "slope": slope, "se_slope": se_slope, "z_statistic": z, "p_value": p,
        "ci_lower": slope - zc * se_slope, "ci_upper": slope + zc * se_slope,
        "significant": bool(abs(z) >= zc), "confidence_level": level,
        "intercept_at_mean_year": float(beta[0]), "mean_year": float(t.mean()),
        "lack_of_fit_chi2": chi2, "lack_of_fit_df": df,
        "lack_of_fit_p": float(sps.chi2.sf(chi2, df)),
        "n_periods": int(len(t)), "overlap_mode": overlap,
        "overlapping_periods": design["overlapping"],
        "effective_independent_periods": design["effective_independent_periods"],
        "method": "GLS linear trend with known sampling variances (two-sided z)",
        "notes": notes,
    }
