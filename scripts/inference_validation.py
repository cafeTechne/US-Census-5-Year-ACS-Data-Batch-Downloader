# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the Round 8 inference-validation study (PLAN_ROUND_8 workstream F1).

Seeded coverage simulations under ACS-like sampling error for every
supported interval method; writes

    docs/audits/inference_validation.md    coverage table and findings
    docs/audits/inference_validation.csv   one row per (method, target)

Usage:
    python scripts/inference_validation.py          # full configuration, serial (~80 min)
    python scripts/inference_validation.py --jobs 0 # same results, one process per CPU (~22 min)
    python scripts/inference_validation.py --fast   # the CI configuration (~1 min)
    python scripts/inference_validation.py --gate   # also exit 1 on a failed/missing required study

Runtime (measured 2026-11-07, 16 cores shared with other jobs, Python 3.14):
full --gate --jobs 0 took 21.7 min wall; the floor is the longest study,
regression_calibration (~22 min). Summed per-study times are ~83 min, the
serial estimate. spatial_fay_herriot takes ~3.5 min for 1000 reps (was 56
min in v0.11.0, when BLAS ran multithreaded on its 100 x 100 matrices).
--fast --gate takes ~50 s serial. The Round 15 info studies in the spatial
group add 5.2 min (spatial_regression_coverage) and ~3.6 min
(local_morans_size) of single-process time, below the --jobs 0 floor.
--jobs output is identical to the serial output at the same seed except the
"seconds" column (wall time per study).

Report versus gate (PLAN_ROUND_12 decision P12-3): by default the command is a
report generator and always exits 0. With --gate it exits 1 when any gated
row has status FAIL or a required study (REQUIRED_STUDIES) produced no gated
row (missing); rows with status "info" never fail the gate. See
docs/audits/CHECK_MAP.md for which study covers which method.

Fast smoke gate (--fast --gate; CI on pull requests, PLAN_ROUND_13 C1). The
fast configuration runs 20-300 repetitions per study, so a correct method
lands outside the 88-92% band by chance (Monte Carlo SE 0.04-0.07). With
--fast --gate a gated row therefore fails only when its coverage is more than
FAST_TOLERANCE_SE (3) Monte Carlo SEs outside the band, the SE being
sqrt(0.9 x 0.1 / reps) at the nominal level (or the row's own mc_se when that
is larger, for rows pooling areas or terms that share a fit). Missing
required studies and non-finite coverage still fail. This only catches gross
regressions (a broken interval, a crash, a study that stops producing rows);
it is not release evidence. The full configuration's band and verdict are
unchanged and remain the release gate.
"""

import argparse
import math
import os
import platform
import sys
from dataclasses import asdict
from datetime import datetime, timezone

# One BLAS/OpenMP thread per process, set before numpy is imported (PLAN_ROUND_13
# C3). The studies fit many small (<= 400 x 400) matrices, where a multithreaded
# BLAS is far slower (spatial_fay_herriot: 3361 s -> 199 s for 1000 reps on a
# 16-core machine) and its reduction order, hence the last bits of results such
# as rho_mean, depends on the machine's thread count. Pinned, the CSV depends
# only on the seed. Applies when run as a script and in --jobs worker processes
# ("__mp_main__"), not when this module is imported.
BLAS_THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")
if __name__ in ("__main__", "__mp_main__"):
    for _v in BLAS_THREAD_VARS:
        os.environ[_v] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from census.lisa_validation import LISA_STUDIES  # noqa: E402
from census.simulation import (BAND, CV_PROFILES, DESIGN_STUDIES, FAST, FULL, NOMINAL, SPATIAL_STUDIES,  # noqa: E402
                               STUDIES, group_config, run_study, run_study_job)

OUT_MD = os.path.join(ROOT, "docs", "audits", "inference_validation.md")

# Studies whose gated rows are release claims (the default run of this script).
# Other studies (fit_replicate_noisy_x, simex, *_low_rel, morans_i_replicate_se,
# and the Round 15 spatial_regression_coverage and local_morans_size, D15-1)
# report info rows only. Fixed here, not derived from the results, so a study
# that silently stops producing gated rows is reported as missing.
# Default-method design studies (Round 10) run in every full report and count
# toward the gate; their replicate-refit (these_areas) variants are slower and
# run only when named with --studies (see docs/audits/CHECK_MAP.md).
DEFAULT_DESIGN_STUDIES = ("multilevel_known_variance", "multilevel_random_slope", "did_two_group", "did_event")
REQUIRED_STUDIES = ("fit_replicate", "regression_calibration", "fay_herriot", "correlation", "linear_trend",
                    "spatial_fay_herriot") + DEFAULT_DESIGN_STUDIES


# --fast --gate only: band widened by this many Monte Carlo SEs (see the header).
FAST_TOLERANCE_SE = 3.0
FAST_GATE_LABEL = f"fast smoke, band widened by {FAST_TOLERANCE_SE:g} MC SE at nominal"


def _fast_smoke_problem(r):
    """None if a gated row is within the widened fast-smoke band, else the reason."""
    cov, reps = r.get("coverage"), r.get("reps")
    try:
        cov, reps = float(cov), float(reps)
    except (TypeError, ValueError):
        return "coverage not numeric"
    if not (math.isfinite(cov) and math.isfinite(reps) and reps > 0):
        return f"non-finite coverage ({cov}) or reps ({reps})"
    se = math.sqrt(NOMINAL * (1 - NOMINAL) / reps)
    mc = r.get("mc_se")
    if mc is not None and mc == mc and math.isfinite(float(mc)):
        se = max(se, float(mc))
    lo, hi = BAND[0] - FAST_TOLERANCE_SE * se, BAND[1] + FAST_TOLERANCE_SE * se
    if lo <= cov <= hi:
        return None
    return f"coverage {cov:.3f} outside the fast-smoke band {lo:.3f}-{hi:.3f}"


def gate(df, required=REQUIRED_STUDIES, fast=False):
    """Release-gate verdict for a results frame (columns study, method, target, status).

    Returns (ok, problems). A problem is a gated row (status other than
    "info") whose status is not "pass", or a required study with no gated
    row in ``df``. Info rows never fail the gate.

    ``fast=True`` (the --fast --gate smoke check, not release evidence):
    a gated row fails only when its coverage is non-finite or more than
    FAST_TOLERANCE_SE Monte Carlo SEs outside the band (see the header);
    missing required studies still fail.
    """
    problems = []
    gated = df[df["status"] != "info"] if len(df) else df
    for r in gated.to_dict(orient="records"):
        if fast:
            why = _fast_smoke_problem(r)
            if why:
                problems.append(f"{r['study']}: {r['method']} / {r['target']}: {why}")
        elif r["status"] != "pass":
            problems.append(f"{r['study']}: {r['method']} / {r['target']}: status {r['status']}")
    present = set(gated["study"]) if len(gated) else set()
    problems += [f"{s}: required study missing (no gated rows in results)" for s in required if s not in present]
    return not problems, problems


def _run_groups(groups, fast: bool, jobs: int, log):
    """Result frames for [(group, studies)] in the serial order, serially or in ``jobs`` processes.

    Each study draws from its own seed (cfg.seed + a per-study offset), so a
    study's rows do not depend on which process runs it or when; frames are
    reassembled in the serial (group, study) order. Workers inherit the
    pinned BLAS thread variables through the environment.
    """
    if jobs <= 1:
        return [run_study(group_config(g, fast), studies=names, log=log) for g, names in groups]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed

    tasks = []
    for g, names in groups:
        cfg = group_config(g, fast)
        for n in (names if names is not None else list(STUDIES)):
            if int(cfg.reps.get(n, 0)) > 0:  # run_study skips studies with no reps
                tasks.append((g, n, fast))
    results = [None] * len(tasks)
    with ProcessPoolExecutor(max_workers=min(jobs, max(len(tasks), 1)),
                             mp_context=multiprocessing.get_context("spawn")) as ex:
        futs = {ex.submit(run_study_job, t): i for i, t in enumerate(tasks)}
        for fut in as_completed(futs):
            df, lines = fut.result()
            results[futs[fut]] = df
            for line in lines:
                log(line)
    return results


def _f(x, d=3):
    try:
        return "" if x != x else f"{x:.{d}f}"
    except TypeError:
        return str(x)


def render(df, cfg, label: str, seconds: float) -> str:
    gated = df[df["status"] != "info"]
    failed = gated[gated["status"] == "FAIL"]
    lines = [
        "# Inference validation (PLAN_ROUND_8 workstream F1)", "",
        f"Generated by `scripts/inference_validation.py` ({label} configuration) on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}; Python {platform.python_version()}; "
        f"{seconds / 60:.1f} min. Seed {cfg.seed}. Do not edit by hand.", "",
        *(["**Fast configuration: a smoke check with few repetitions, not release evidence.** Coverage "
            "here can fall outside the band by chance; the release gate is the full configuration.", ""]
          if label == "fast" else []),
        f"Nominal interval level {NOMINAL:.0%}; acceptance band {BAND[0]:.0%}-{BAND[1]:.0%} "
        "(PLAN_ROUND_8 sec. 14.3). Monte Carlo SE = sqrt(c(1-c)/R) (Fay-Herriot: between-replication "
        "SE of the per-replication coverage, since areas share a fit). Rows with status `info` are "
        "comparisons (naive fits, targets a method does not claim) and are not gated.", "",
        "## Coverage", "",
        "| Study | Method | Target | Reps | Coverage | MC SE | Status | Bias | Notes |",
        "| :--- | :--- | :--- | ---: | ---: | ---: | :--- | ---: | :--- |",
    ]
    for r in df.to_dict(orient="records"):
        notes = []
        for k, lab in (("rel_bias", "rel. bias"), ("naive_rel_bias", "naive rel. bias"),
                       ("mse_rel_bias_mean", "MSE rel. bias (mean over areas)"),
                       ("mse_rel_bias_abs_max", "max abs MSE rel. bias"),
                       ("eblup_mse_vs_direct", "EBLUP MSE / direct variance"),
                       ("min_area_coverage", "min per-area coverage"), ("size", "test size"),
                       ("power", "power"), ("fdr", "FDR")):
            if k in r and r[k] == r[k] and r[k] is not None:
                notes.append(f"{lab} {_f(r[k])}")
        lines.append(f"| {r['study']} | {r['method']} | {r['target']} | {r['reps']} | {_f(r['coverage'])} | "
                     f"{_f(r['mc_se'], 4)} | {r['status']} | {_f(r.get('bias'))} | {'; '.join(notes)} |")
    lines += ["", "## Findings", ""]
    if failed.empty:
        lines.append("Every gated method is inside the acceptance band.")
    else:
        lines.append("Gated rows outside the band (not tuned; these intervals must not be exposed as "
                     "defaults until resolved):")
        lines += [f"- **{r['method']}**, target {r['target']}: coverage {_f(r['coverage'])} "
                  f"(MC SE {_f(r['mc_se'], 4)})." for r in failed.to_dict(orient="records")]
    lines += [
        "",
        "Interpretation notes:",
        "- `fit_replicate` `se_total = sqrt(se_model^2 + se_sampling^2)` (decision 2) double-counts outcome "
        "sampling error for a superpopulation target, because the model residual variance already "
        "absorbs it; over-coverage there is expected (conservative). `se_sampling` alone is the "
        "design-based SE for the finite-population (census-of-areas) coefficient.",
        "- `fit_replicate` does not correct covariate measurement error (noisy-covariate row): use "
        "`regression_calibration` or `simex` for that.",
        "- Correlations among ACS estimates are attenuated by sampling error: Fisher-z intervals cover the "
        "correlation of the observed estimates, not of the true values.",
        "",
        "## Configuration", "",
        "```", *[f"{k} = {v}" for k, v in asdict(cfg).items()], "```", "",
        "CV profiles (log-normal, percent):", "",
        *[f"- `{k}`: median {v['median']}, sigma {v['sigma']}, clip {v['clip']} ({v['source']})"
          for k, v in CV_PROFILES.items()],
        "",
        "Replicates: 80 deviations per unit and variable, N(0, Sigma_i/4) correlated across variables, "
        "rescaled so 4/80 sum (X_r - X)^2 equals the target SE^2 exactly (ACS VRE documentation).",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true", help="CI configuration (prints only unless --out)")
    ap.add_argument("--out", default=None, help="output Markdown path")
    ap.add_argument("--studies", nargs="+", default=None,
                    help="run only these studies (at least one name; omit the flag for every study)")
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 if a required study fails or is missing (default: report only, exit 0); "
                         "with --studies only the selected required studies are required")
    ap.add_argument("--jobs", type=int, default=1,
                    help="run studies in this many worker processes (0 = one per CPU); the results are "
                         "identical to a serial run")
    args = ap.parse_args()
    jobs = (os.cpu_count() or 1) if args.jobs == 0 else max(args.jobs, 1)
    cfg, label = (FAST, "fast") if args.fast else (FULL, "full")
    out = args.out or (None if args.fast else OUT_MD)
    t0 = datetime.now()
    import pandas as _pd

    unknown = sorted(set(args.studies or ()) - set(STUDIES))
    if unknown:
        ap.error(f"unknown studies: {', '.join(unknown)} (choose from {', '.join(STUDIES)})")
    main_studies = None if args.studies is None else [
        s for s in args.studies if s not in SPATIAL_STUDIES and s not in DESIGN_STUDIES and s not in LISA_STUDIES]
    groups = []
    if main_studies is None or main_studies:
        groups.append(("main", main_studies))
    spatial = [s for s in SPATIAL_STUDIES if args.studies is None or s in args.studies]
    if spatial:  # Round 9 spatial studies (own lattice configuration)
        groups.append(("spatial", spatial))
    designs = (list(DEFAULT_DESIGN_STUDIES) if args.studies is None
               else [s for s in args.studies if s in DESIGN_STUDIES])
    if designs:  # Round 10 multilevel and DiD studies (own configuration)
        groups.append(("designs", designs))
    lisa = [] if args.studies is None else [s for s in args.studies if s in LISA_STUDIES]
    if lisa:  # Round 16 LISA validation studies: on demand only (info rows)
        groups.append(("lisa", lisa))
    frames = _run_groups(groups, args.fast, jobs, lambda m: print(m, flush=True))
    frames = [f for f in frames if f is not None and len(f)]
    df = _pd.concat(frames, ignore_index=True) if frames else _pd.DataFrame(
        columns=["study", "method", "target", "reps", "nominal", "coverage", "mc_se", "status"])
    secs = (datetime.now() - t0).total_seconds()
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(df, cfg, label, secs))
        df.to_csv(os.path.splitext(out)[0] + ".csv", index=False, lineterminator="\n")
        print(f"wrote {out}")
    print(df[["study", "method", "target", "coverage", "mc_se", "status"]].to_string(index=False))
    if not args.gate:
        return 0
    required = [s for s in REQUIRED_STUDIES if args.studies is None or s in args.studies]
    ok, problems = gate(df, required, fast=args.fast)
    if args.fast:
        print("fast configuration: smoke check only, not release evidence (release gate: full configuration)")
    print(f"gate{f' ({FAST_GATE_LABEL})' if args.fast else ''}: {'PASS' if ok else 'FAIL'} "
          f"(required studies: {', '.join(required) or 'none selected'})")
    for p in problems:
        print(f"  - {p}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
