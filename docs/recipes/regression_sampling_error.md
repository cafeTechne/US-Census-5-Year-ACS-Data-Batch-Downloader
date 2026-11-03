# Regression with sampling error

Goal: a regression whose standard errors include ACS sampling error, not only model error.

1. Download the outcome and predictor tables for one release (`plan_extraction`, then `download_table`) for the same geographies (e.g. all counties in a state). Use detailed tables (B...) so replicates exist.
2. `build_analysis_frame(input_path=<downloaded file>, use_replicates=true, table_code="B17001", year=2023)`. Keep `output_path` and `replicates_path` from the result.
3. Check `coverage`: variables with many low-reliability rows (CV > 30%) are noisy; consider a larger geography.
4. `fit_model(input_path=<output_path>, formula="poverty_rate ~ median_rent", method="replicate", replicates_path=<replicates_path>)`.
5. No replicates? Use `method="regression_calibration"` or `"simex"` with `error_covariates=[...]` (noisy predictors), or `method="fay_herriot"` with `se_col` (noisy outcome).
6. Report each coefficient with `se_total`, and say which uncertainty sources it includes (sampling, model, or both).
7. Optional: `make_figure` or `fit_model(..., figure_dir="downloads/figures")` for a coefficient plot.
