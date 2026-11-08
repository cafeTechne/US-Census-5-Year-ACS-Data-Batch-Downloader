# Inference check map (PLAN_ROUND_12 A2)

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
| Multilevel, known variance (`census.multilevel`) | `-m pytest -q tests/test_multilevel.py` (includes the dense fixed-variance reference `TestKnownVarianceDenseReference`) | `multilevel_known_variance`, `multilevel_random_slope`, `multilevel_replicate` |
| Difference-in-differences (`census.did`) | `-m pytest -q tests/test_did.py` (includes the hand-built differenced reference `TestTwoGroupDenseReference`) | `did_two_group`, `did_event`, `did_replicate`; serial correlation: `did_serial` |
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
- Multilevel and DiD studies are not run by the script. Run them with
  `census.simulation.run_study(designs_config(fast=False), studies=list(DESIGN_STUDIES))`
  and, for DiD serial correlation,
  `run_study(serial_config(fast=False), studies=["did_serial"])`. Their
  results are recorded in the `census.multilevel` / `census.did` docstrings,
  not gated by the script.

Scope of the dense reference tests: they check fixed-variance GLS algebra
(and, for DiD, that the reported REML A is a stationary point of an
independently written REML likelihood). They do not validate REML
estimation in general, Kenward-Roger adjustments, or interval coverage;
coverage evidence comes only from the T3 studies.


## Release gate scope (decided 2026-11-07)

`python scripts/inference_validation.py --gate` runs and requires the default-method studies, including the Round 10 designs: `multilevel_known_variance`, `multilevel_random_slope`, `did_two_group`, `did_event`. The slower replicate-refit variants (`multilevel_replicate`, `did_replicate`) and the serial-correlation study (`did_serial`) run on demand by naming them with `--studies` (or via `run_study` as above); they are reported but not required.
