# Inference check map (PLAN_ROUND_12 A2)

Round 18 metadata integrity (an input identity contract, not a statistical
coverage claim): `tests/test_metadata_integrity.py` exercises actual hashes,
plausible tampering, unreadable/missing sources, custom manifests, recompression,
root changes, cache repair, legacy loaders, transport/GUI errors and refusal
before extraction output. `tests/test_reference_workflow.py` independently hashes
consumed variable/group archives while retaining the existing scientific expected
values, replay and library/CLI/MCP checks. See [the metadata policy](../metadata_integrity.md).

Round 17 exact proposition contracts and checked matrix rules (not a
statistical coverage claim): run `python -m pytest -q
tests/test_proposition_verification.py tests/test_verification_contract.py
tests/test_workflows_verification.py tests/test_verification_release.py`.
These deterministic tests run in ordinary offline CI and in the tag job
before publication; the tag job rejects missing/skipped verification tests.
The operator-pinned workflow and trust boundary are in
[verification.md](../verification.md). Product identity, fresh bundle
destinations, numerical input guards and redaction regressions are in
`tests/test_review_fixes_r17_{product,bundle,boundaries}.py`.
The bundle checks include path traversal, portable artifact names and
case-insensitive collision rejection before any output is created, plus
preservation of outside files and valid labels.
They also cover nested `bundle.json` payload hashes, final figure destination
collisions (including mixed render/copy and case-insensitive cases), mismatched
source prefixes, and temporary-render cleanup without partial bundle output.
Benchmark transport failures and bounded fake tool loops are covered by
`tests/test_local_model_eval_protocol.py`; this invokes no model runtime and
does not measure model accuracy.
`tests/test_verification_eval.py` checks offline frozen prompts and transcript
scoring: wrong-claim evidence, forged flags, review changes, exact final
references, duplicates, diagnostic-only outcomes and non-overwriting CLI output.
It exercises no model runtime. Live client evaluation remains conditional.
`tests/test_verification_client.py` exercises the injected async client with
fake transports: two phases, actual MCP schema preflight, shared deadline and
tool cancellation, duplicate suppression, cache revocation, forged evidence
and rejection before dispatch. No service or model timing study is involved.
`tests/test_verification_live.py` uses fake HTTP/MCP contexts to check runtime
wiring, loopback restrictions, opt-in, final tool choice, error/truncation
retention and exclusive output. Actual model integration records are separate.
`tests/test_workflows_verification.py` also checks the actual local MCP stdio
transport, including strict schema versions, wrong-claim binding and review
revocation within one session. It requires local subprocess/pipe support;
it contacts no network endpoint. Do not replace this with a skipped test in
release validation because a restrictive development sandbox blocks it.

Which check to run when an inference method changes. Tiers follow
PLAN_ROUND_11 section 12: run the focused offline test (T1/T2, seconds) on
every change; run the applicable seeded study (T3, minutes to an hour) only
for a release or a changed estimator/scientific behavior. Long studies are
not run on every PR. Do not tune seeds or bands after seeing results.

All commands use `.venv/bin/python` from the repository root.

| Method (module) | Focused offline check (every change) | Applicable release study (T3) |
| :--- | :--- | :--- |
| `fit_replicate`, OLS SE targets (`census.models`) | `-m pytest -q tests/test_models.py` | `fit_replicate` (gated); `fit_replicate_noisy_x` (info) |
| `regression_calibration` (`census.models`) | `-m pytest -q tests/test_models.py` | `regression_calibration` (gated); `regression_calibration_low_rel` (info) |
| `simex` (`census.models`) | `-m pytest -q tests/test_models.py` | `simex`, `simex_low_rel` (info only, not a default method) |
| Fay-Herriot EBLUP/MSE (`census.sae`) | `-m pytest -q tests/test_sae.py` | `fay_herriot` (gated) |
| Spatial Fay-Herriot (`census.sae_spatial`) | `-m pytest -q tests/test_sae_spatial.py` | `spatial_fay_herriot` (gated) |
| Correlation, Fisher z (`census.correlation`) | `-m pytest -q tests/test_correlation.py` | `correlation` (gated observed-value row; true-value row info) |
| Linear trend test (`census.trends`) | `-m pytest -q tests/test_trends.py` | `linear_trend` (gated) |
| Moran's I replicate SE (`census.spatial`) | `-m pytest -q tests/test_spatial.py` | `morans_i_replicate_se` (info) |
| Spatial lag / error regression, LM diagnostics (`census.spatial.spatial_regression`) | `-m pytest -q tests/test_spatial.py tests/test_workflows_geo.py` | `spatial_regression_coverage` (info, D15-1) |
| Error model corrected for outcome sampling error (`spatial_regression(model='error', correct_outcome_error=True, se_col=...)`; spatial Fay-Herriot likelihood via `census.sae_spatial`) | `-m pytest -q tests/test_spatial_lambda_correction.py tests/test_workflows_geo.py` (the A1 study's diagonal-Sigma rows call the tool's own function `census.spatial.error_model_known_variances`, checked in `tests/test_review_fixes_r16.py`; it also equals the study-only `me_sem_fit` on the seeded draws, REML and ML; 20-draw coverage smoke check; warnings, boundary, non-convergence, SE = 0 and the `CORRECTION_MAX_N` size limit) | `spatial_error_attenuation` rows "spatial Fay-Herriot REML rho" (the tool's default; ML rows for `method='ml'`) and `spatial_error_attenuation_correlated` (info, on demand) |
| Lag-model effects (direct/indirect/total; `census.spatial.lag_effects`, `report_tables.effects_table`) and the Round 16 study estimators (`census.simulation_spreg`) | `-m pytest -q tests/test_spatial_regression_validation.py` (effects vs S(W) algebra and spreg's multipliers; study likelihoods vs spreg and `census.sae_spatial`) | `spatial_dgp_recovery`, `spatial_error_attenuation`, `spatial_error_attenuation_correlated`, `common_factor_lr`, `common_factor_lr_acs` (info, on demand) |
| Local Moran's I / LISA (`census.spatial.local_morans`) | `-m pytest -q tests/test_spatial.py tests/test_workflows_geo.py` | `local_morans_size` (info, D15-1); `lisa_validation_null`, `lisa_validation_clusters` (info, on demand, Round 16) |
| LISA validation prototypes (`census.lisa_validation`: correlated-error scenarios, replicate-stability filter, LISA on spatial FH EBLUPs, Wong 2021 heuristic) | `-m pytest -q tests/test_lisa_validation.py` | `lisa_validation_null`, `lisa_validation_clusters` (info, on demand) |
| Multilevel, known variance (`census.multilevel`) | `-m pytest -q tests/test_multilevel.py` (includes the dense fixed-variance reference `TestKnownVarianceDenseReference`) | `multilevel_known_variance`, `multilevel_random_slope`, `multilevel_replicate` |
| Difference-in-differences (`census.did`) | `-m pytest -q tests/test_did.py` (includes the hand-built differenced reference `TestTwoGroupDenseReference`) | `did_two_group`, `did_event`, `did_replicate`; serial correlation (info only): `did_ar1_sensitivity`, `did_serial` |
| Release gate itself (`scripts/inference_validation.py`) | `-m pytest -q tests/test_validation_gate.py` | n/a |

Release studies:

- Script studies (rows marked gated/info above except multilevel and DiD):
  `scripts/inference_validation.py` writes the report and exits 0;
  `scripts/inference_validation.py --gate` exits 1 if any gated row is FAIL
  or a required study (`REQUIRED_STUDIES`: fit_replicate,
  regression_calibration, fay_herriot, correlation, linear_trend,
  spatial_fay_herriot) has no gated row. Info rows never fail the gate.
  `--fast` is the CI-size configuration (prints only); `--studies NAME ...`
  limits the run, and then only the selected required studies are required.
- Fast smoke gate in CI (PLAN_ROUND_13 C1, owner decision D13-5):
  `scripts/inference_validation.py --fast --gate` runs on pull requests to
  `defaultBranch` only (ubuntu-latest, Python 3.12; job
  `fast-inference-gate`, about 1 min). With 20-300 repetitions a correct
  method falls outside 88-92% by chance, so in this mode a gated row fails
  only when its coverage is more than 3 Monte Carlo SEs outside the band
  (SE `sqrt(0.9 * 0.1 / reps)` at nominal, or the row's own `mc_se` if
  larger, for rows that pool areas or terms sharing one fit). Missing
  required studies and non-finite coverage still fail. The output is
  labelled `gate (fast smoke, band widened by 3 MC SE at nominal)`. This
  only catches gross regressions (a broken interval, a crash, a study that
  stops producing rows); it is not release evidence. The full-configuration
  band and verdict are unchanged and remain the release gate.
- Multilevel and DiD studies: the default-method ones run in every full
  report and gate (see "Release gate scope" below). The others run when
  named with `--studies` (`multilevel_replicate`, `did_replicate`,
  `did_ar1_sensitivity`), or, for the Round 11 study `did_serial`, only via
  `run_study(serial_config(fast=False), studies=["did_serial"])` (the script
  does not route it; naming it with `--studies` runs nothing). Their results
  are recorded in the `census.multilevel` / `census.did` docstrings.
- DiD serial-correlation sensitivity (PLAN_ROUND_13 C2, owner decision
  D13-3): `scripts/inference_validation.py --studies did_ar1_sensitivity`
  (about 19 min single-threaded). The `did_two_group` (4 periods) and
  `did_event` (5 periods) designs and default population intervals with
  stationary AR(1) area-by-period errors at rho 0, 0.3, 0.6 and 0.9. Every
  row has status `info`: it reports, never gates, and is in neither
  `DEFAULT_DESIGN_STUDIES` nor `REQUIRED_STUDIES`. Model scope: the DiD
  intervals assume area-by-period errors independent across periods (there
  is no clustered-SE option); the 2026-11-07 run (1000 repetitions per cell)
  found coverage 0.882-0.914 at every rho for both designs, so no
  degradation was observed in these short (4-5 period) panels. Results and
  scope note: `census.did` docstring.
- Spatial regression and local Moran's I (PLAN_ROUND_15 workstream B,
  decision D15-1): `spatial_regression_coverage` and `local_morans_size`
  are in the spatial group, so they run in every full report (and with
  `--studies NAME`), but every row is `info`: they are not in
  `REQUIRED_STUDIES` and never fail the gate. Promotion to gated is an
  owner decision. Single-process runtime (full configuration, 16 cores
  shared): `spatial_regression_coverage` 5.2 min, `local_morans_size`
  about 3.6 min (4.4 min under load), both below the `--jobs 0` floor.
  Design: 12x12 rook lattice, tract-profile ACS CVs.
  `spatial_regression_coverage`: lag and error DGPs at rho/lambda 0, 0.3,
  0.6 with sampling error in the outcome, ML and GMM through
  `spatial_regression` (1000 reps per cell; rows of the tool default are
  marked `[tool default]`), plus reference cells without sampling error
  and a noisy-covariate cell (500 reps), and the LM tests' size at rho = 0
  (2000 fits). `local_morans_size` (1000 reps per scenario): spatial
  randomness with random or spatially patterned SEs, and a 4x4 hot spot
  (+1.5 SD); each map is analysed with the tool default and the previous
  default, plus the two cross readings (folded at 9999, two-sided at 999).
- Defaults changed after these studies (owner decision, Round 15):
  `spatial_regression` uses GMM for the lag model (ML lag rho intervals
  covered 0.48 at rho 0.3 and 0.003 at 0.6 under outcome sampling error;
  GMM lag 0.899 and 0.889) and keeps ML for the error model (lambda is
  attenuated by both estimators and GMM is not better: 0.558 vs 0.581 at
  0.3, 0.162 vs 0.192 at 0.6; beta 0.88-0.91 for both). `local_morans`
  uses two-sided p-values (min(1, 2 p_sim); esda's p_sim is folded) from
  9999 permutations with BH at alpha 0.10: per-area rate 0.099 (previous
  default 0.196) and family-wise rate 0.088 (MC SE 0.009) under
  randomness; power for the hot spot 0.011; spatially patterned SEs still
  give at least one significant area in 38% of maps (previous default
  26%); those SEs are heterogeneous but independent, so this is not
  evidence about spatially correlated errors (Round 16
  `lisa_validation_*`). The
  previous behavior stays available (`method="ml"`;
  `permutations=999, sided="folded"`). LM tests' size 0.046-0.056 at
  0.05. Results and caveat text: `census.spatial`.
- LISA under sampling error (PLAN_ROUND_16 workstream B):
  `scripts/inference_validation.py --studies lisa_validation_null
  lisa_validation_clusters --jobs 2` (group `lisa`, own configuration). Run
  on demand only: not in the default report (each study takes about 20-30
  min single-process, above the ~5 min budget for default-report studies),
  not in `REQUIRED_STUDIES`, every row `info`. Five scenarios (no true
  clustering with independent homogeneous, heterogeneous independent and
  spatially correlated errors; a hot spot with independent and correlated
  errors), the correlated errors parameterized by the mean rook-neighbour
  error correlation r1 in -0.2 .. 0.5 (SAR structure, a stated assumption);
  four methods on the same maps (tool-default LISA, LISA filtered by
  `replicate_quadrant_share`, LISA on spatial Fay-Herriot EBLUPs, Wong 2021
  heuristic hot/cold spots, the latter scored against its own target).
  The prototypes live in `census.lisa_validation`; no tool default changed.
  Design, metric definitions, results with MC SEs and proposals:
  `docs/audits/lisa_validation.md`.
- Spatial regression validation (PLAN_ROUND_16 workstream A): five
  info-only studies in `census.simulation_spreg` (group
  `spreg_validation`), run only when named, e.g.
  `scripts/inference_validation.py --studies spatial_dgp_recovery --out
  /tmp/r16a.md` (do not overwrite `docs/audits/inference_validation.md`).
  They are not in the default full report and not in `REQUIRED_STUDIES`
  (each takes more than 5 min single-process).
  `spatial_error_attenuation` / `_correlated`: lambda of the tool's ML
  error model vs the spatial Fay-Herriot rho (REML and ML) under
  independent and SAR-correlated outcome error, rook/queen/kNN. The
  spatial Fay-Herriot REML rows are the evidence for the owner-approved
  tool option `spatial_regression(model='error',
  correct_outcome_error=True)`: those rows call the tool's own
  estimator, `census.spatial.error_model_known_variances`
  (`tests/test_review_fixes_r16.py`, `tests/test_spatial_lambda_correction.py`).
  `spatial_dgp_recovery`: SEM, SAR, SDM, omitted-covariate and Juhl DGPs
  fitted by all four estimators; effects coverage (delta-method and
  simulated intervals) and OLS RS test rejection rates.
  `common_factor_lr`: LR common-factor test in Juhl's (2021) design
  (queen 7x7-20x20) with chi-square and residual-bootstrap critical values;
  `common_factor_lr_acs`: the same under ACS-like outcome error. Designs,
  results and conclusions: `docs/audits/spatial_regression_validation.md`.
- Runtime (PLAN_ROUND_13 C3): the script pins BLAS/OpenMP to one thread per
  process, and `--jobs N` (0 = one per CPU) runs studies in parallel
  processes with output identical to a serial run (except the `seconds`
  column). Full `--gate --jobs 0`: 21.7 min on 16 cores (2026-11-07).

Scope of the dense reference tests: they check fixed-variance GLS algebra
(and, for DiD, that the reported REML A is a stationary point of an
independently written REML likelihood). They do not validate REML
estimation in general, Kenward-Roger adjustments, or interval coverage;
coverage evidence comes only from the T3 studies.


## Release gate scope (decided 2026-11-07)

`python scripts/inference_validation.py --gate` runs and requires the default-method studies, including the Round 10 designs: `multilevel_known_variance`, `multilevel_random_slope`, `did_two_group`, `did_event`. The slower replicate-refit variants (`multilevel_replicate`, `did_replicate`) and the AR(1) sensitivity study (`did_ar1_sensitivity`, info rows only) run on demand by naming them with `--studies`; the Round 11 serial-correlation study (`did_serial`) runs only via `run_study` as above. They are reported but not required.
