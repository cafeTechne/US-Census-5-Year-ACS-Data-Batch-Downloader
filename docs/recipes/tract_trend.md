# Tract trend with non-overlapping releases

Goal: did a tract's (or county's) rate change over time?

1. Choose NON-overlapping 5-year releases, at least 5 years apart: 2013 (2009-2013), 2018 (2014-2018), 2023 (2019-2023). Overlapping releases share survey years and are refused by `trend_test`.
2. Tracts changed in 2020. Download with `batch_download(..., geo_level="tract", start_year=2013, end_year=2023, harmonize_tracts_to=2020, output_format="panel")` after `plan_extraction` shows `would_block` false.
3. For each chosen release compute the rate and MOE: `derive_estimate(operation="proportion", estimates=[num, den], moes=[moe_num, moe_den], as_percent=true)`.
4. Two releases only: `calculate_significance(est1, moe1, est2, moe2, year1=2018, year2=2023)`.
5. Three or more releases: `trend_test(years=[2013, 2018, 2023], estimates=[...], moes=[...])`. Report `slope` (change per year), its interval (`ci_lower`, `ci_upper`) and `significant`.
6. Many tracts at once: use the full profile's `batch_significance` with `correction="holm"` and report `significant_adjusted`.
7. Optional figure: `make_figure(kind="trend", input_path=..., period_col="Year", estimate_col=..., moe_col=...)`.
