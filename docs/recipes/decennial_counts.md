# Use 2020 Census counts (P.L. 94-171, DHC)

Goal: total population, sex by age, housing units or occupancy from the 2020 Census, and use them correctly.

1. `search_tables(query="occupancy", dataset="dec/dhc")` (DHC: `P1` total population, `P12` sex by age, `H1` housing units, `H3` occupancy status). P.L. 94-171 tables use `dataset="dec/pl"`; table IDs differ by file (P.L. 94-171 `P1` is race).
2. `describe_variable(code="H3_002N")` to confirm the label. Census counts end in `N`, not `E`/`M`.
3. `download_table(table_code="H3", geography="county", state_code="56", dataset="dec/dhc")` (years default to [2020]; the 2020 Census is the only census year here). Blocks: only `dataset="dec/pl"`, or DHC `H1`.
4. Read `uncertainty` and the advisories: the counts are not sample-based, so there is no MOE, SE or CV (they are undefined, never 0). They still carry disclosure-avoidance noise (TopDown differential privacy), coverage and processing error. Only state population, block housing units and occupied group-quarters facilities are exact.
5. Rates from counts: `derive_estimate(operation="proportion", estimates=[occupied, total], product="decennial")` returns the point estimate only. Aggregate blocks or small areas before computing rates.
6. Do not run significance tests between two census counts (`calculate_significance` refuses `product="decennial"`). To compare an ACS estimate with a census count, use `calculate_significance(est1=acs, moe1=acs_moe, est2=census, moe2=0, product="5-year", product2="decennial")` and report its note: only ACS sampling error is measured.
7. Report counts as "2020 Census (DHC/P.L. 94-171), April 1, 2020", without margins of error, and state the disclosure-avoidance caveat.
