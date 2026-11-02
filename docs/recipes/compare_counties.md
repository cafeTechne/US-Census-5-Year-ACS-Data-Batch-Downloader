# Compare a rate between two counties

Goal: is the poverty rate (or any rate) different in county A and county B in one release?

1. `search_tables(query="poverty")` and pick a detailed table (e.g. `B17001`).
2. `describe_variable(code="B17001_001E", year=2023)` (denominator) and `B17001_002E` (numerator) to confirm the labels.
3. `plan_extraction(state_code="48", table_code="B17001", start_year=2023, end_year=2023, geo_level="county", county_codes=["453", "491"], variables=["B17001_001", "B17001_002"])`. Continue only if `would_block` is false.
4. `batch_download(...)` with the same arguments and `output_format="panel"`. Read each county's numerator, denominator and their MOEs from the preview or file.
5. For each county: `derive_estimate(operation="proportion", estimates=[numerator, denominator], moes=[moe_num, moe_den], as_percent=true)`.
6. `calculate_significance(est1=rate_A, moe1=moe_A, est2=rate_B, moe2=moe_B)`. Same release, different places: do not pass year1/year2.
7. Report both rates with their 90% MOEs, the difference, the p-value, and the verdict. Never claim a difference the test does not support.
