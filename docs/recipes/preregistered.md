# Pre-registered analysis

Goal: a confirmatory analysis fixed before the data are pulled.

1. Write the manifest (JSON or YAML): research question, table, variables (codes or `YEAR:CODE`), geography, releases, derivations, tests or model formula, multiple-comparison correction, and reliability exclusions.
   - Several states: `"geography": {"level": "county", "states": ["26", "36"]}` (instead of `"state"`; no `codes`). States are stacked; add a state term to the formula yourself, e.g. `C(GEOID.str[:2])`.
   - Price adjustment: `"adjustments": [{"type": "rpp", "variables": ["income"], "rpp_year": 2024}]` divides dollar variables (and their SEs and replicates) by the BEA Regional Price Parity of each county's metro area or the state's nonmetro portion. Give `rpp_year`; without it the latest RPP year is used and recorded. One release only.
2. Validate it with `create_manifest` (full profile); it checks every variable resolves and records the manifest's SHA-256. Commit the manifest to the repository before running.
3. Run it with `run_manifest` and the manifest path. It runs `plan_extraction`, the download, derivations and models exactly as written.
4. Do not change the manifest after seeing results. Anything you run outside it on the same data is exploratory; label it so.
5. Report the manifest hash with every result, plus estimates with 90% MOEs or model SEs with their uncertainty sources.

Worked example on real data (two states, RPP-adjusted): docs/case_study_mi_ny.md section 1 runs examples/manifests/mi_ny_income_education.json.

Worked example (synthetic, offline, executable): docs/reference_workflow.md runs examples/manifests/income_population_tracts.json end to end, including a rerun check and a blocked incomplete-replicates case.
