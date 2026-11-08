# SPDX-License-Identifier: AGPL-3.0-or-later
"""Difference-in-differences on ACS area-level estimates (PLAN_ROUND_10.md workstream D).

Model for area i in non-overlapping period t (two-way fixed effects)::

    y_it = alpha_i + lambda_t + delta' w_it + u_it + e_it
    u_it ~ N(0, A)        area-by-period model error (estimated, REML)
    e_it ~ N(0, D_it)     ACS sampling error, D_it = SE_it^2 known

``w_it`` is ``treated_i * post_t`` (``design="two_group"``) or one
indicator ``treated_i * 1[t = k]`` per non-reference period k
(``design="event"``; leads before treatment, lags after). Area effects are
removed exactly by differencing against a reference period r (default: the
last pre-treatment period): for each area the K-1 differences have
covariance A (I + J) + diag(D_it) + D_ir J, independent across areas, so
GLS/REML on the differences equals GLS/REML of the fixed-effects model. A is
estimated by REML with the one-dimensional maximizer of census.sae (the
Fay-Herriot likelihood with block-diagonal V). Sampling errors are
independent across periods only when the ACS periods share no sample years,
so the Round 8 overlap check (census.trends.overlap_design_check) is
enforced: overlapping periods raise ValueError.

Interval targets as in census.models: "population" (default; se_model from
GLS with estimated A, which includes the known sampling variances) and
"these_areas" (se_sampling from 80 replicate GLS refits; needs replicates).

Serial correlation (PLAN_ROUND_11 Q1/D8). The model assumes the area-by-
period errors u_it are independent across periods given the area and period
effects. Bertrand, Duflo & Mullainathan (2004, QJE 119(1), 249-275) showed
that serially correlated shocks make conventional DiD SEs far too small in long
panels, and Cameron & Miller (2015, JHR 50(2), 317-372) recommend
cluster-robust SEs then. ACS DiD panels are short: non-overlapping 5-year
releases give 2 to 4 periods (e.g. 2009/2014/2019/2024). Measured 90%
coverage with AR(1) u_it (census.simulation.study_did_serial, 150 areas, known
sampling variances; 1000 repetitions for "population", 200 for
"these_areas"; event rows pool 3 effect terms per repetition). The
these_areas target is the estimator's weighting rule applied to the true
area-period values: the GLS DiD with the estimator's full-sample REML A
(under AR(1) the working covariance is misspecified, so the generating
variance is not the estimator's weight):

    layout     design     rho=0.0  rho=0.5  rho=0.8   target
    2 periods  two_group  0.872    0.902    0.897     population
    4 periods  two_group  0.887    0.896    0.905     population
    4 periods  event      0.887    0.898    0.897     population
    2 periods  two_group  0.930    0.965    0.915     these_areas
    4 periods  two_group  0.940    0.940    0.900     these_areas
    4 periods  event      0.905    0.922    0.898     these_areas

No configuration falls below 0.88 at rho 0.5 or 0.8 (MC SE about 0.01 for
population, 0.012-0.021 for these_areas): with
2 periods the single difference absorbs any correlation into A, and with 4
periods the misspecified block covariance changes the GLS weights but not the
validity of the variance. A CR1 cluster-by-area sandwich (Liang-Zeger with
G/(G-1)(N-1)/(N-K)) was measured and did no better (0.873-0.909, 0.881-0.883
at rho=0), so no cluster option is offered; the independence assumption
above is stated in results instead. Panels with more than 4 periods (not
possible with non-overlapping 5-year ACS releases; possible with 1-year
releases) are not validated: results then carry a "SERIAL CORRELATION NOT
VALIDATED" note instead of the validation note.

AR(1) sensitivity of the default intervals (PLAN_ROUND_13 C2; report only,
owner decision D13-3; census.simulation.study_did_ar1_sensitivity, run with
``scripts/inference_validation.py --studies did_ar1_sensitivity``). The
did_two_group / did_event study designs (150 areas; 4 periods two_group, 5
periods event with 2 leads and 2 lags; population target, 1000 repetitions
per cell, MC SE about 0.01 two_group and 0.006 event) with stationary AR(1)
u_it, 90% coverage:

    design            rho=0.0  rho=0.3  rho=0.6  rho=0.9
    two_group (4 p)   0.914    0.892    0.882    0.896
    event (5 p)       0.890    0.913    0.899    0.896
    PT test 1 - size  0.879    0.904    0.914    0.906

No degradation with rho was observed in these short panels; the
independence assumption is still the stated model scope (no clustered-SE
option), and the 5-period event panel carries the "NOT VALIDATED" note above
because that note counts periods, not this study.

Parallel trends: with at least three pre-treatment periods the event-style
leads (pre-period treated-control differences relative to the reference) are
tested jointly (Wald chi-square); a non-rejection is not proof of parallel
trends.

Reference reproduction (tests/test_did.py): Card & Krueger (1994), AER
84(4):772-793, Table 3 (FTE employment, NJ vs PA, mean and SE by state and
wave): DiD 2.76 (SE 1.36) from the change estimates.
"""

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats as sps

from .models import INTERVAL_TARGETS, _table
from .multilevel import BlockGLS
from .results import (SOURCE_MODEL, SOURCE_SAMPLING_MODEL, SOURCE_SAMPLING_REPLICATE, ModelResult,
                      check_refit_count, check_replicate_policy, precheck_replicates,
                      replicate_completeness)
from .sae import _maximize
from .trends import PERIOD_YEARS, overlap_design_check
from .variance import REPLICATE_COLUMNS, REPLICATE_COUNT, REPLICATE_FACTOR

DESIGNS = ("two_group", "event")
# Largest panel (number of periods) covered by the PLAN_ROUND_11 Q1 AR(1) coverage study.
SERIAL_VALIDATED_PERIODS = 4
REFERENCES = [
    "Card, D. & Krueger, A.B. (1994). Minimum wages and employment. AER 84(4), 772-793, Table 3.",
    "Angrist, J.D. & Pischke, J.-S. (2009). Mostly Harmless Econometrics, sec. 5.2.",
    "Fay, R.E. & Herriot, R.A. (1979). JASA 74(366), 269-277 (known sampling variances).",
]


def _did_vfun(theta, cls):
    A = float(theta[0])
    n = cls["D"].shape[1]
    V = np.broadcast_to((A + cls["Dr"][:, :1])[:, :, None], (cls["Dr"].shape[0], n, n)).copy()
    V[:, np.arange(n), np.arange(n)] += A + cls["D"]
    return V


def _wald(b: np.ndarray, cov: np.ndarray) -> Dict[str, float]:
    stat = float(b @ np.linalg.solve(cov, b))
    df = len(b)
    return {"chi2": stat, "df": df, "p_value": float(sps.chi2.sf(stat, df))}


class _Prepared:
    pass


def _prepare(frame, outcome, treated, period, unit, treatment_period, se, design, reference, period_years):
    for c in (outcome, treated, period, unit):
        if c not in frame.columns:
            raise KeyError(f"column {c!r} not in frame")
    se_col = se if se is not None else (f"{outcome}_se" if f"{outcome}_se" in frame.columns else None)
    if se_col is not None and se_col not in frame.columns:
        raise KeyError(f"SE column {se_col!r} not in frame")
    cols = [unit, period, outcome, treated] + ([se_col] if se_col else [])
    data = frame[cols].dropna().copy()
    data[unit] = data[unit].astype(str)
    data[period] = data[period].astype(int)
    if data.duplicated([unit, period]).any():
        raise ValueError("more than one row per unit and period")
    tr = data.groupby(unit)[treated].nunique()
    if (tr > 1).any():
        raise ValueError(f"treatment indicator varies within unit for {int((tr > 1).sum())} unit(s)")
    data[treated] = data[treated].astype(float)
    if not set(data[treated].unique()) <= {0.0, 1.0}:
        raise ValueError("treated must be 0/1")
    periods = sorted(data[period].unique().tolist())
    overlap = overlap_design_check(periods, period_years)
    if overlap["overlapping"]:
        raise ValueError(
            "ACS periods overlap (shared sample years), so sampling errors are correlated across periods and "
            f"the DiD variance would be wrong. Use non-overlapping releases, e.g. {overlap['suggested_nonoverlapping']}. "
            + " ".join(overlap["notes"]))
    if treatment_period is None:
        raise ValueError("treatment_period (first post-treatment period) is required")
    pre = [p for p in periods if p < treatment_period]
    post = [p for p in periods if p >= treatment_period]
    if not pre or not post:
        raise ValueError(f"need at least one pre and one post period around {treatment_period}; have {periods}")
    ref = pre[-1] if reference is None else int(reference)
    if ref not in periods:
        raise ValueError(f"reference period {ref} not in data periods {periods}")
    if design == "two_group" and ref >= treatment_period:
        # The two-group regressor treated * post is only correct when the
        # reference is pre-treatment; a post-treatment reference would leave a
        # singular or mis-differenced design (PLAN_ROUND_11 C6, decision D5).
        raise ValueError(
            f"reference period {ref} is not before treatment_period {treatment_period}; the two-group DiD "
            f"needs a pre-treatment reference (one of {pre})")
    if data[treated].nunique() < 2:
        raise ValueError("need both treated and control units")
    P = _Prepared()
    P.__dict__.update(data=data, se_col=se_col, periods=periods, pre=pre, post=post, ref=ref, overlap=overlap)
    return P


def _differences(P, outcome, treated, period, unit, design, y_override=None):
    data = P.data
    y = data[outcome].to_numpy(float) if y_override is None else y_override
    D = data[P.se_col].to_numpy(float) ** 2 if P.se_col else np.zeros(len(data))
    t = data[period].to_numpy(int)
    u = data[unit].to_numpy()
    is_ref = t == P.ref
    ref_y = pd.Series(y[is_ref], index=u[is_ref])
    ref_D = pd.Series(D[is_ref], index=u[is_ref])
    keep = (~is_ref) & pd.Series(u).isin(ref_y.index).to_numpy()
    dy = y[keep] - ref_y.reindex(u[keep]).to_numpy()
    others = [p for p in P.periods if p != P.ref]
    tk, Tk = t[keep], data[treated].to_numpy(float)[keep]
    cols, names = [], []
    for p in others:
        cols.append((tk == p).astype(float))
        names.append(f"period[{p}]")
    effect_terms = []
    if design == "two_group":
        cols.append(Tk * (tk >= min(P.post)))
        names.append("did")
        effect_terms.append("did")
    else:
        for p in others:
            nm = f"{'lead' if p < min(P.post) else 'lag'}[{p}]"
            cols.append(Tk * (tk == p))
            names.append(nm)
            effect_terms.append(nm)
    X = np.column_stack(cols)
    return {"dy": dy, "X": X, "names": names, "effects": effect_terms, "unit": u[keep], "D": D[keep],
            "Dr": ref_D.reindex(u[keep]).to_numpy(), "keep": keep,
            "n_dropped_units": int(len(set(u)) - len(ref_y.index))}


def _fit(diff, method):
    eng = BlockGLS(diff["unit"], diff["dy"], diff["X"], _did_vfun, D=diff["D"], extra={"Dr": diff["Dr"]})
    scale = float(np.var(diff["dy"])) if len(diff["dy"]) > 1 else 1.0
    upper = max(scale * 10.0, float(np.max(diff["D"] + diff["Dr"])) * 10.0 if len(diff["D"]) else 0.0, 1e-12)
    A = _maximize(lambda a: eng.loglik(np.array([a]), method), upper)
    sol = eng._solve(np.array([A]), keep=True)
    return eng, A, sol


def difference_in_differences(
    frame: pd.DataFrame,
    outcome: str,
    treated: str,
    treatment_period: int,
    period: str = "Year",
    unit: str = "GEOID",
    se: Optional[str] = None,
    design: str = "two_group",
    reference: Optional[int] = None,
    replicates: Optional[pd.DataFrame] = None,
    target: str = "population",
    confidence_level: float = 0.90,
    period_years: int = PERIOD_YEARS,
    method: str = "REML",
    max_failed_replicates: int = 0,
    replicate_refit: str = "full",
    replicate_policy: str = "flag",
) -> ModelResult:
    """Two-group or event-style DiD on area-level estimates with known variances.

    ``frame``: one row per unit and period (``period`` = ACS release end
    year), with ``outcome``, its SE (``se``, default "<outcome>_se" if present;
    without SEs D = 0 and A absorbs all noise), and a time-invariant 0/1
    ``treated``. ``treatment_period``: first post-treatment period.
    ``period_years``: 5 for ACS 5-year releases, 1 for 1-year.
    ``replicates``: long frame with GEOID, Year, variable (= outcome) and
    rep_1..rep_80. ``replicate_refit``: "full" (default) re-estimates A in
    every replicate refit, replicating the whole estimator (Wolter 2007,
    Introduction to Variance Estimation, 2nd ed., ch. 4); "gls" holds A at
    its full-sample value, which understates se_sampling because the GLS
    weights depend on the data (validated coverage 0.875 vs 0.90 nominal).
    ``replicate_policy``: "flag" (default) records failed refits (up to
    ``max_failed_replicates``) and unit-periods without replicates in
    diagnostics["replicates_incomplete"]; "strict" raises ValueError
    (census.results.replicate_completeness, PLAN_ROUND_11 Q2/D7).
    se_model assumes area-by-period errors independent across periods; with
    AR(1) errors (rho up to 0.8) on 2-4 non-overlapping periods coverage was
    measured at 0.896-0.905, so no cluster-robust option is needed (module
    docstring, PLAN_ROUND_11 Q1/D8). Panels with more than 4 periods were
    not validated and get a "SERIAL CORRELATION NOT VALIDATED" note.
    Returns a ModelResult whose coefficients include the
    period effects and the DiD term(s) ("did", or "lead[...]"/"lag[...]").
    """
    method = method.upper()
    check_replicate_policy(replicate_policy)
    if design not in DESIGNS:
        raise ValueError(f"design must be one of {DESIGNS}")
    if target not in INTERVAL_TARGETS:
        raise ValueError(f"target must be one of {INTERVAL_TARGETS}")
    if target == "these_areas" and (replicates is None or not len(replicates)):
        raise ValueError("target='these_areas' needs replicates (se_sampling)")
    if replicate_refit not in ("full", "gls"):
        raise ValueError("replicate_refit must be 'full' or 'gls'")
    P = _prepare(frame, outcome, treated, period, unit, treatment_period, se, design, reference, period_years)
    diff = _differences(P, outcome, treated, period, unit, design)
    if diff["X"].shape[0] <= diff["X"].shape[1]:
        raise ValueError("not enough area-period differences for the number of parameters")
    eng, A, sol = _fit(diff, method)
    names = diff["names"]
    params = pd.Series(sol["beta"], index=names)
    se_model = pd.Series(np.sqrt(np.diag(sol["Q"])), index=names)
    notes: List[str] = []
    sources = [SOURCE_MODEL] + ([SOURCE_SAMPLING_MODEL] if P.se_col else [])
    if diff["n_dropped_units"]:
        notes.append(f"{diff['n_dropped_units']} unit(s) without the reference period {P.ref} dropped")
    if not P.se_col:
        notes.append("no SE column: sampling error is not modelled separately (absorbed into A)")
    # Sampling covariance in the difference metric includes the shared reference-period error.
    S = _diff_sampling_cov(eng, sol, diff) if P.se_col else np.zeros_like(sol["Q"])
    diagnostics: Dict[str, Any] = {
        "A_area_period_var": A, "reference_period": P.ref, "pre_periods": P.pre, "post_periods": P.post,
        "overlap_check": P.overlap, "n_units": int(len(set(diff["unit"]))),
        "se_sampling_analytic": dict(zip(names, np.sqrt(np.clip(np.diag(S), 0, None)).tolist())),
    }
    se_sampling = pd.Series(np.nan, index=names)
    method_label = "gls_reml"
    if replicates is not None and len(replicates):
        se_sampling, rep_notes = _replicate_se(P, diff, eng, A, sol, replicates, outcome, treated, period, unit,
                                               design, params, replicate_refit, method, replicate_policy,
                                               max_failed_replicates, diagnostics, notes)
        notes += rep_notes
        sources.append(SOURCE_SAMPLING_REPLICATE)
        method_label = f"gls_reml_replicate_{replicate_refit}"
    table = _table(params, se_model, se_sampling, confidence_level, target)
    table["role"] = ["effect" if n in diff["effects"] else "period" for n in names]
    # Parallel-trends diagnostic from the event-style leads.
    if len(P.pre) >= 3:
        ev = diff if design == "event" else _differences(P, outcome, treated, period, unit, "event")
        if design == "event":
            evb, evQ = sol["beta"], sol["Q"]
        else:
            _, _, evsol = _fit(ev, method)
            evb, evQ = evsol["beta"], evsol["Q"]
        idx = [i for i, n in enumerate(ev["names"]) if n.startswith("lead[")]
        pt = _wald(evb[idx], evQ[np.ix_(idx, idx)])
        pt.update({"leads": [ev["names"][i] for i in idx], "estimates": evb[idx].tolist(),
                   "note": "joint Wald test that pre-period treated-control differences equal the reference "
                           "period's (population SEs); failing to reject is not proof of parallel trends"})
        diagnostics["parallel_trends"] = pt
        if pt["p_value"] < 1 - confidence_level:
            notes.append(f"PARALLEL TRENDS: pre-period leads differ jointly (p={pt['p_value']:.3g}); the DiD "
                         "estimate may be biased")
    else:
        diagnostics["parallel_trends"] = None
        notes.append(f"parallel-trends diagnostic needs >= 3 non-overlapping pre-periods (have {len(P.pre)})")
    diagnostics["interval_target"] = target
    n_periods = len(P.periods)
    if n_periods <= SERIAL_VALIDATED_PERIODS:
        notes.append("se_model assumes area-by-period errors independent across periods (validated with AR(1) "
                     "errors up to rho=0.8 on 2-4 non-overlapping periods; see census.did)")
    else:
        notes.append(f"SERIAL CORRELATION NOT VALIDATED: se_model assumes area-by-period errors independent across "
                     f"periods; coverage was validated only for 2-{SERIAL_VALIDATED_PERIODS} periods and this panel "
                     f"has {n_periods}. With serially correlated shocks over more periods conventional DiD SEs can "
                     "be understated (Bertrand, Duflo & Mullainathan 2004, QJE 119(1):249-275)")
    notes.append(f"intervals and p-values use the '{target}' target")
    return ModelResult(
        model_type=f"did_{design}", formula=f"{outcome} ~ unit FE + period FE + treated x post ({design})",
        n_obs=int(len(P.data)), coefficients=table, method=method_label, uncertainty_sources=sources,
        confidence_level=confidence_level, notes=notes, diagnostics=diagnostics,
        provenance={"references": REFERENCES, "period_years": period_years, "treatment_period": treatment_period,
                    "unit": unit, "period": period, "se_column": P.se_col, "method": method})


def _diff_sampling_cov(eng: BlockGLS, sol, diff) -> np.ndarray:
    """Q X'V^-1 S V^-1 X Q with S = diag(D_it) + D_ir J per unit (sampling part of Var(dy))."""
    p = eng.p
    M = np.zeros((p, p))
    for cls, Vi in zip(eng.classes, sol["vinv"]):
        n = cls["D"].shape[1]
        S = np.broadcast_to(cls["Dr"][:, :1, None], (cls["Dr"].shape[0], n, n)).copy()
        S[:, np.arange(n), np.arange(n)] += cls["D"]
        W = Vi @ eng.X[cls["idx"]]
        M += np.einsum("mnp,mnk,mkq->pq", W, S, W)
    return sol["Q"] @ M @ sol["Q"]


def _refit_A(eng: BlockGLS, dy: np.ndarray, A0: float, method: str) -> float:
    """Re-estimate A for replicate data, searching log A near the full-sample value."""
    from scipy import optimize
    f = lambda a: eng.loglik(np.array([a]), method, dy)  # noqa: E731
    if A0 <= 0:
        cand = [0.0] + list(np.geomspace(1e-6, 1.0, 13) * max(float(np.var(dy)), 1e-12))
        return float(max(cand, key=f)) if max(f(c) for c in cand) > f(0.0) else 0.0
    res = optimize.minimize_scalar(lambda t: -f(float(np.exp(t))), bounds=(np.log(A0) - 3, np.log(A0) + 3),
                                   method="bounded", options={"xatol": 1e-6})
    a = float(np.exp(res.x))
    return 0.0 if f(0.0) >= f(a) else a


def _replicate_se(P, diff, eng, A, sol, replicates, outcome, treated, period, unit, design, params,
                  replicate_refit="full", method="REML", replicate_policy="flag", max_failed=0,
                  diagnostics=None, notes=None):
    rep = replicates[replicates["variable"] == outcome] if "variable" in replicates.columns else replicates
    if not len(rep) or (period not in rep.columns and "Year" not in rep.columns):
        raise ValueError(f"replicates need GEOID, {period}, variable={outcome!r} and rep_1..rep_80")
    pcol = period if period in rep.columns else "Year"
    ucol = unit if unit in rep.columns else "GEOID"
    rep = rep.assign(**{ucol: rep[ucol].astype(str), pcol: rep[pcol].astype(int)}).set_index([ucol, pcol])
    key = pd.MultiIndex.from_arrays([P.data[unit].to_numpy(), P.data[period].to_numpy(int)])
    R = rep[REPLICATE_COLUMNS].reindex(key).to_numpy(float, copy=True)
    miss = ~np.isfinite(R).all(axis=1)
    y0 = P.data[outcome].to_numpy(float)
    n_fixed = int(miss.sum())
    fixed = {outcome: n_fixed} if n_fixed else None
    precheck_replicates(replicate_policy, rows_held_fixed=fixed, context="difference_in_differences")
    if miss.any():
        R[miss] = y0[miss, None]
    coefs, failed = [], 0
    for r in range(REPLICATE_COUNT):
        try:
            d = _differences(P, outcome, treated, period, unit, design, y_override=R[:, r])
            if replicate_refit == "gls":
                coefs.append(eng.gls(np.array([A]), d["dy"], None, sol["vinv"]))
            else:
                coefs.append(eng.gls(np.array([_refit_A(eng, d["dy"], A, method)]), d["dy"]))
        except Exception:  # noqa: BLE001
            failed += 1
    replicate_completeness(replicate_policy, failed_refits=failed, rows_held_fixed=fixed,
                           context="difference_in_differences", diagnostics=diagnostics, notes=notes)
    check_refit_count(failed, len(coefs), max_failed, "difference_in_differences")
    B = np.vstack(coefs)
    se = pd.Series(np.sqrt(REPLICATE_FACTOR * ((B - params.to_numpy(float)) ** 2).sum(axis=0)), index=params.index)
    return se, ["replicate refits hold A at its full-sample value (GLS linearization; understates se_sampling)"
                if replicate_refit == "gls" else "replicate refits re-estimate A (full replication of the estimator)"]


def did_from_changes(changes: Sequence[float], change_ses: Sequence[float], treated: Sequence[int],
                     confidence_level: float = 0.90) -> Dict[str, float]:
    """Two-group DiD from per-group change estimates and their SEs (independent groups).

    delta = change_treated - change_control, SE = sqrt(se_t^2 + se_c^2). This is
    the published-table special case (one aggregate per group), e.g. Card &
    Krueger (1994) Table 3, row 3.
    """
    c = np.asarray(changes, float)
    s = np.asarray(change_ses, float)
    t = np.asarray(treated, int)
    if sorted(t.tolist()) != [0, 1]:
        raise ValueError("expect exactly one treated and one control group")
    est = float(c[t == 1][0] - c[t == 0][0])
    se = float(math.sqrt(s[t == 1][0] ** 2 + s[t == 0][0] ** 2))
    z = float(sps.norm.ppf(0.5 + confidence_level / 2))
    return {"estimate": est, "se": se, "ci_lower": est - z * se, "ci_upper": est + z * se}
