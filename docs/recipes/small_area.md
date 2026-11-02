# Small-area estimates (Fay-Herriot)

Goal: more reliable estimates for small areas (tracts, small counties) whose direct estimates are noisy.

1. Download the outcome and covariates for one release (`plan_extraction`, `batch_download`).
2. `build_analysis_frame(input_path=<file>)` so the outcome has a `_se` column (e.g. `poverty_rate_se`).
3. `minimum_detectable_difference(moe1=...)` on a typical MOE shows how noisy the direct estimates are.
4. Full profile: `small_area_estimates(input_path=<frame>, formula="poverty_rate ~ unemployment_rate + median_rent", se_col="poverty_rate_se")`. Core profile: `fit_model(..., method="fay_herriot", se_col="poverty_rate_se")` gives the model coefficients.
5. Read `gamma` (shrinkage): near 1 keeps the direct estimate, near 0 relies on the model.
6. Report the EBLUP with its MSE-based interval next to the direct estimate and MOE. Label model-based estimates as such.
