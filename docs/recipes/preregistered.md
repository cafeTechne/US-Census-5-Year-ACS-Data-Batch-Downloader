# Pre-registered analysis

Goal: a confirmatory analysis fixed before the data are pulled.

1. Write the manifest (JSON or YAML): research question, table, variables (codes or `YEAR:CODE`), geography, releases, derivations, tests or model formula, multiple-comparison correction, and reliability exclusions.
2. Validate it with `create_manifest` (full profile); it checks every variable resolves and records the manifest's SHA-256. Commit the manifest to the repository before running.
3. Run it with `run_manifest` and the manifest path. It runs `plan_extraction`, the download, derivations and models exactly as written.
4. Do not change the manifest after seeing results. Anything you run outside it on the same data is exploratory; label it so.
5. Report the manifest hash with every result, plus estimates with 90% MOEs or model SEs with their uncertainty sources.
