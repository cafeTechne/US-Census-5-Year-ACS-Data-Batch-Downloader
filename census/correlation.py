# SPDX-License-Identifier: AGPL-3.0-or-later
"""Correlation matrices that respect ACS sampling error (PLAN_ROUND_8.md, I2).

Correlations come from ``scipy.stats`` (``pearsonr`` / ``spearmanr``) on
pairwise-complete units of an analysis frame (``census.results`` convention:
``v``, ``v_se`` per variable). For each pair the result reports:

* the observed coefficient, pairwise n and the scipy p-value;
* a Fisher-z confidence interval: z = atanh(r), SE_z = 1/sqrt(n-3) for
  Pearson (Fisher 1921) and sqrt(1.06/(n-3)) for Spearman (Fieller,
  Hartley and Pearson 1957), back-transformed with tanh;
* the p-values adjusted across the matrix (each unordered pair once) with
  Round 7 ``adjust_pvalues``;
* with replicates, a design-based SE: the coefficient recomputed on each of
  the 80 ACS replicate estimates, Var = 4/80 * sum_r (r_r - r)^2;
* optionally Spearman's correction for attenuation, r / sqrt(rel_x rel_y),
  with reliability rel_v = 1 - mean(SE_v^2) / Var(v) from the known sampling
  variances. It is reported alongside, never instead of, the observed r.
"""

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from .multitest import adjust_pvalues
from .results import (SOURCE_SAMPLING_REPLICATE, CorrelationResult, check_replicate_policy, replicate_completeness,
                      stochastic_variables)
from .variance import REPLICATE_COLUMNS, REPLICATE_FACTOR

METHODS = ("pearson", "spearman")
SPEARMAN_FIELLER = 1.06


def _corr(x: np.ndarray, y: np.ndarray, method: str):
    """(r, p) from scipy; NaN when undefined (n < 3 or a constant column)."""
    if x.size < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return math.nan, math.nan
    res = stats.pearsonr(x, y) if method == "pearson" else stats.spearmanr(x, y)
    return float(res[0]), float(res[1])


def fisher_z_interval(r: float, n: int, confidence_level: float = 0.90,
                      method: str = "pearson"):
    """Fisher-z confidence interval for a correlation (see module docstring)."""
    if not (n > 3 and np.isfinite(r)):
        return math.nan, math.nan
    if abs(r) >= 1:
        return float(r), float(r)
    scale = SPEARMAN_FIELLER if method == "spearman" else 1.0
    se = math.sqrt(scale / (n - 3))
    zc = stats.norm.ppf(1 - (1 - confidence_level) / 2)
    z = math.atanh(r)
    return math.tanh(z - zc * se), math.tanh(z + zc * se)


def reliability(estimates: Any, se: Any) -> float:
    """Reliability 1 - mean(SE^2)/Var(estimate) (sample variance, ddof=1)."""
    x = np.asarray(estimates, dtype=float)
    s = np.asarray(se, dtype=float)
    ok = np.isfinite(x) & np.isfinite(s)
    if ok.sum() < 2:
        return math.nan
    var = float(np.var(x[ok], ddof=1))
    if var <= 0:
        return math.nan
    return 1.0 - float(np.mean(s[ok] ** 2)) / var


def _keys(frame: pd.DataFrame, replicates: pd.DataFrame) -> List[str]:
    keys = [k for k in ("GEOID", "Year") if k in frame.columns and k in replicates.columns]
    if "GEOID" not in keys:
        raise ValueError("frame and replicates must both have a GEOID column")
    return keys


def _replicate_cube(frame: pd.DataFrame, replicates: pd.DataFrame,
                    variables: Sequence[str]) -> Dict[str, np.ndarray]:
    """variable -> (n_units, 80) replicate values aligned to ``frame`` rows."""
    keys = _keys(frame, replicates)
    missing = [c for c in REPLICATE_COLUMNS if c not in replicates.columns]
    if missing:
        raise ValueError(f"replicates frame lacks {len(missing)} replicate columns (e.g. {missing[0]})")
    left = frame[keys].astype(str).reset_index(drop=True)
    out: Dict[str, np.ndarray] = {}
    for v in variables:
        sub = replicates[replicates["variable"] == v]
        if sub.empty:
            continue
        sub = sub.assign(**{k: sub[k].astype(str) for k in keys})
        if sub.duplicated(keys).any():
            raise ValueError(f"replicates for {v!r} have duplicate {keys} rows")
        merged = left.merge(sub[keys + REPLICATE_COLUMNS], on=keys, how="left")
        out[v] = merged[REPLICATE_COLUMNS].to_numpy(float)
    return out


def _square(values: np.ndarray, variables: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(values, index=list(variables), columns=list(variables))


def correlation_matrix(
    frame: pd.DataFrame,
    variables: Sequence[str],
    method: str = "pearson",
    confidence_level: float = 0.90,
    replicates: Optional[pd.DataFrame] = None,
    correction: Optional[str] = "holm",
    disattenuate: bool = False,
    replicate_policy: str = "flag",
) -> CorrelationResult:
    """Correlation matrix with intervals, adjusted p-values and optional
    replicate SEs and disattenuated coefficients.

    Args:
        frame: Analysis frame (``v`` and, for disattenuation, ``v_se``).
        variables: Two or more estimate columns.
        method: ``pearson`` or ``spearman``.
        confidence_level: Interval level, e.g. 0.90 (ACS default).
        replicates: Optional long replicate frame (``GEOID`` [+ ``Year``],
            ``variable``, ``rep_1..rep_80``) giving design-based SEs.
        correction: ``adjust_pvalues`` method across the matrix, or None.
        disattenuate: Also report Spearman-corrected coefficients.
        replicate_policy: With ``replicates``, units with an estimate but no
            (finite) replicates, or variables with an SE column but no
            replicates, make the replicate SEs incomplete. ``flag`` (default)
            records provenance["replicates_incomplete"] and a note;
            ``strict`` (census.manifest) raises IncompleteReplicatesError
            (census.results.replicate_completeness).
    """
    method = str(method).lower()
    check_replicate_policy(replicate_policy)
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    level = confidence_level / 100.0 if confidence_level > 1 else float(confidence_level)
    if not 0 < level < 1:
        raise ValueError("confidence_level must be in (0, 1)")
    variables = list(variables)
    if len(variables) < 2 or len(set(variables)) != len(variables):
        raise ValueError("need at least two distinct variables")
    absent = [v for v in variables if v not in frame.columns]
    if absent:
        raise ValueError(f"variables not in frame: {absent}")

    k = len(variables)
    data = {v: pd.to_numeric(frame[v], errors="coerce").to_numpy(float) for v in variables}
    reps = _replicate_cube(frame, replicates, variables) if replicates is not None else None
    incomplete = None
    if reps is not None:
        units_missing = {v: int((np.isfinite(data[v]) & ~np.isfinite(reps[v]).all(axis=1)).sum())
                         for v in variables if v in reps}
        incomplete = replicate_completeness(
            replicate_policy, rows_held_fixed=units_missing,
            variables_without_replicates=stochastic_variables(frame, [v for v in variables if v not in reps]),
            context="correlation_matrix", rows_label="units missing replicates (excluded from replicate SEs)")
    r = np.eye(k)
    n = np.zeros((k, k), dtype=int)
    p = np.full((k, k), np.nan)
    lo, hi = np.eye(k), np.eye(k)
    se_rep = np.zeros((k, k)) if reps is not None else None
    notes: List[str] = []
    if incomplete:
        notes.append(f"REPLICATES INCOMPLETE: {incomplete['note']}")
    for i, vi in enumerate(variables):
        n[i, i] = int(np.isfinite(data[vi]).sum())
        for j in range(i + 1, k):
            vj = variables[j]
            ok = np.isfinite(data[vi]) & np.isfinite(data[vj])
            rij, pij = _corr(data[vi][ok], data[vj][ok], method)
            l, h = fisher_z_interval(rij, int(ok.sum()), level, method)
            r[i, j] = r[j, i] = rij
            p[i, j] = p[j, i] = pij
            n[i, j] = n[j, i] = int(ok.sum())
            lo[i, j] = lo[j, i] = l
            hi[i, j] = hi[j, i] = h
            if se_rep is not None:
                se_rep[i, j] = se_rep[j, i] = _replicate_se(reps, vi, vj, data, rij, method, notes)

    p_adj = None
    if correction:
        iu = np.triu_indices(k, 1)
        adj = np.asarray(adjust_pvalues(p[iu], correction), dtype=float)
        pa = np.full((k, k), np.nan)
        pa[iu] = adj
        pa[(iu[1], iu[0])] = adj
        p_adj = _square(pa, variables)
        notes.append(f"p_adjusted: {correction} correction across {len(adj)} pairs")

    disatt = None
    provenance: Dict[str, Any] = {"method": method, "variables": variables,
                                  "ci_method": "fisher_z", "correction": correction}
    if disattenuate:
        rel = {}
        for v in variables:
            sc = f"{v}_se"
            if sc not in frame.columns:
                raise ValueError(f"disattenuation needs standard errors in column {sc!r}")
            rel[v] = reliability(data[v], pd.to_numeric(frame[sc], errors="coerce"))
        d = np.eye(k)
        for i, vi in enumerate(variables):
            for j in range(i + 1, k):
                denom = rel[vi] * rel[variables[j]]
                d[i, j] = d[j, i] = r[i, j] / math.sqrt(denom) if denom > 0 else math.nan
        disatt = _square(d, variables)
        provenance["reliability"] = rel
        notes.append("disattenuated: Spearman's correction r/sqrt(rel_x*rel_y), "
                     "rel = 1 - mean(SE^2)/Var(estimate); reported alongside the observed r")
        if any(not (x > 0) for x in rel.values()):
            notes.append("some reliabilities are not positive; their disattenuated values are NaN")
        if np.nanmax(np.abs(d - np.eye(k))) > 1:
            notes.append("some disattenuated |r| exceed 1 (reliability underestimated or sampling noise)")
        if method == "spearman":
            notes.append("disattenuation formula is exact for Pearson; applied to Spearman as an approximation")
    if incomplete:
        provenance["replicates_incomplete"] = incomplete
    if reps is not None:
        provenance["uncertainty_sources"] = [SOURCE_SAMPLING_REPLICATE]
        notes.append("se_replicate: correlation recomputed on 80 replicates, Var = 4/80*sum(r_r - r)^2")

    return CorrelationResult(
        matrix=_square(r, variables), n=_square(n, variables), p_values=_square(p, variables),
        ci_lower=_square(lo, variables), ci_upper=_square(hi, variables), method=method,
        se_replicate=None if se_rep is None else _square(se_rep, variables),
        p_adjusted=p_adj, disattenuated=disatt, confidence_level=level,
        notes=notes, provenance=provenance,
    )


def _replicate_se(reps, vi, vj, data, r_full, method, notes) -> float:
    if vi not in reps or vj not in reps:
        notes.append(f"no replicates for pair ({vi}, {vj}); se_replicate is NaN")
        return math.nan
    ri, rj = reps[vi], reps[vj]
    ok = (np.isfinite(data[vi]) & np.isfinite(data[vj])
          & np.isfinite(ri).all(axis=1) & np.isfinite(rj).all(axis=1))
    if ok.sum() < 3:
        return math.nan
    base, _ = _corr(data[vi][ok], data[vj][ok], method)
    if ok.sum() < (np.isfinite(data[vi]) & np.isfinite(data[vj])).sum():
        notes.append(f"({vi}, {vj}): replicate SE uses {int(ok.sum())} units with complete replicates")
    rr = np.array([_corr(ri[ok, c], rj[ok, c], method)[0] for c in range(ri.shape[1])])
    return float(math.sqrt(REPLICATE_FACTOR * np.sum((rr - base) ** 2)))
