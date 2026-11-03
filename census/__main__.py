# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command-line interface: ``python -m census <command> [options]``.

Every command prints one JSON object to stdout with --json (progress goes to
stderr). Exit codes: 0 success, 2 invalid input, 3 concordance review required
or unverified concordance, 4 API or data failure.

Examples:
    python -m census plan --table DP02 --geo state --state 36 --years 2018-2022 --variables DP02_0016 --json
    python -m census extract --table B19013 --geo tract --state 48 --codes 453 --years 2015-2022 \\
        --format parquet --out out --inflation-base-year 2022 --codebook --json
    python -m census concordance --table B27001 --years 2016-2017 --variables B27001_006
    python -m census describe --code DP02_0002PE --year 2018
    python -m census vintages --dataset subject
    python -m census validate-crosswalks
    python -m census plan --table B19013 --geo county --state all --years 2022 --json
    python -m census replay out/B19013_extraction_metadata.json --json
    python -m census derive proportion --estimates 400,1000 --moes 50,80 --json
    python -m census aggregate --input out/B17001_tract_panel_2023.csv --groups groups.csv --year 2023 \\
        --derive '[{"name": "poverty_rate", "op": "proportion", "num": "B17001_002E", "den": "B17001_001E"}]'
    python -m census median --table B25063 --year 2023 --input out/rent.csv --geoids 36061000100,36061000200
    python -m census significance --input wide.csv --est1 e2018 --moe1 m2018 --est2 e2023 --moe2 m2023 \\
        --year1 2018 --year2 2023 --correction holm
    python -m census harmonize --input tract_panel.csv --target 2020
    python -m census replicates --table B19001 --year 2023 --geo county
    python -m census draft-rule --table S1701 --from-year 2016 --to-year 2017 --pattern P --replacement R \\
        --drafted-by agent:model/session --rationale TEXT --source-url https://www.census.gov/...
    python -m census record-rule draft.json --reviewed-by agent:model/other-session
    python -m census validate --offline --json
    python -m census tool fit_model '{"input_path": "frame.parquet", "formula": "y ~ x"}' --json
    python -m census cache stats
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from census import errors
from census import workflows
from census.api import CensusAPIClient, infer_dataset
from census.cache import get_default_cache
from census.concordance import BLOCKING_KINDS, apply_overrides, build_concordance, validate_all_crosswalks
from census.metadata import available_vintages, describe_variable
from census.pipeline import (
    CONCORDANCE_MODES,
    OUTPUT_FORMATS,
    ResearchOptions,
    build_entities,
    plan_extraction,
    public_plan,
    replay_extraction,
    run_batch,
)

EXIT_OK, EXIT_INPUT, EXIT_REVIEW, EXIT_FAILURE = 0, 2, 3, 4
_INPUT_CODES = {
    errors.INVALID_INPUT,
    errors.INVALID_GEO_LEVEL,
    errors.INVALID_OUTPUT_FORMAT,
    errors.INVALID_DATASET,
    errors.INVALID_VARIABLES,
    errors.VINTAGE_UNAVAILABLE,
    errors.PRICE_INDEX_UNAVAILABLE,
}
_REVIEW_CODES = {errors.CONCORDANCE_REVIEW_REQUIRED, errors.UNVERIFIED_CONCORDANCE}


def _years(text: str) -> List[int]:
    out: List[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return sorted(set(out))


def _list(text: Optional[str]) -> Optional[List[str]]:
    return [x.strip() for x in text.split(",") if x.strip()] if text else None


def _exit_code(result: Dict[str, Any]) -> int:
    if result.get("success", True):
        return EXIT_OK
    code = result.get("error_code")
    if code in _INPUT_CODES:
        return EXIT_INPUT
    if code in _REVIEW_CODES:
        return EXIT_REVIEW
    return EXIT_FAILURE


def _emit(result: Dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for key, value in result.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, default=str)[:300]
            print(f"{key}: {value}")
    return _exit_code(result)


def _client(no_cache: bool) -> CensusAPIClient:
    return CensusAPIClient(cache=None if no_cache else get_default_cache())


def _options(args: argparse.Namespace) -> ResearchOptions:
    return ResearchOptions(
        clean_sentinels=not args.keep_sentinels,
        compute_reliability=args.reliability,
        inflation_base_year=args.inflation_base_year,
        price_index=args.price_index,
        include_codebook=args.codebook,
        use_cache=not args.no_cache,
        slug_style=args.slug_style,
        concordance_mode=args.concordance_mode,
        continuity_check=not args.no_continuity,
        allow_unverified_concordance=args.allow_unverified_concordance,
        allow_provisional_index=args.allow_provisional_index,
        harmonize_tracts_to=args.harmonize_tracts_to,
    )


def _add_extract_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--table", required=True)
    p.add_argument("--years", required=True, help="e.g. 2015-2022 or 2018,2020")
    p.add_argument("--geo", default="state", help="us, state, county, tract, place, zcta, msa")
    p.add_argument("--state", help="2-digit state FIPS, or 'all' (50 states + DC)")
    p.add_argument("--include-pr", action="store_true", help="with --state all, add Puerto Rico")
    p.add_argument("--codes", help="comma-separated place/county/ZCTA/CBSA codes")
    p.add_argument("--variables", help="comma-separated codes, YEAR:CODE, or concept IDs")
    p.add_argument("--dataset", help="acs5, profile, subject, cprofile (inferred)")
    p.add_argument("--format", default="panel", choices=OUTPUT_FORMATS)
    p.add_argument("--concordance-mode", default="fail", choices=CONCORDANCE_MODES)
    p.add_argument("--allow-unverified-concordance", action="store_true")
    p.add_argument("--no-continuity", action="store_true")
    p.add_argument("--reliability", action="store_true", help="add SE/CV/reliability columns")
    p.add_argument("--keep-sentinels", action="store_true")
    p.add_argument("--inflation-base-year", type=int)
    p.add_argument("--price-index", default="r-cpi-u-rs", choices=("r-cpi-u-rs", "cpi-u", "c-cpi-u"))
    p.add_argument("--allow-provisional-index", action="store_true",
                   help="allow index years with unpublished months (2025); adds an advisory")
    p.add_argument("--harmonize-tracts-to", type=int, choices=(2010, 2020),
                   help="apportion a tract panel to one tract vintage")
    p.add_argument("--codebook", action="store_true")
    p.add_argument("--slug-style", default="label", choices=("label", "code"))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", action="store_true")


def _entities(args: argparse.Namespace) -> Any:
    years = _years(args.years) if getattr(args, "years", None) else None
    return build_entities(args.geo, args.state, _list(args.codes), include_pr=args.include_pr, years=years)


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        entities = _entities(args)
    except ValueError as exc:
        return _emit(errors.error_result(errors.INVALID_GEO_LEVEL, str(exc)), args.json)
    plan = plan_extraction(
        args.table, entities, _years(args.years), args.format, _options(args),
        args.dataset, _list(args.variables), _client(args.no_cache),
    )
    return _emit(public_plan(plan), args.json)


def cmd_extract(args: argparse.Namespace) -> int:
    try:
        entities = _entities(args)
    except ValueError as exc:
        return _emit(errors.error_result(errors.INVALID_GEO_LEVEL, str(exc)), args.json)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    result = run_batch(
        args.table, entities, _years(args.years), args.out, args.format, _options(args),
        args.dataset, _client(args.no_cache), log=log, variables=_list(args.variables),
    )
    return _emit(result, args.json)


def cmd_replay(args: argparse.Namespace) -> int:
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    result = replay_extraction(
        args.metadata,
        output_dir=args.output_dir,
        verify=not args.no_verify,
        client=None if not args.no_cache else _client(True),
        refresh=args.refresh,
        log=log,
    )
    return _emit(result, args.json)


def cmd_concordance(args: argparse.Namespace) -> int:
    dataset = args.dataset or infer_dataset(args.table)
    conc = build_concordance(args.table, dataset, _years(args.years), _client(args.no_cache))
    apply_overrides(conc)
    selected, unknown, resolution = conc.explain_selection(_list(args.variables))
    if unknown:
        return _emit(errors.error_result(errors.INVALID_VARIABLES, f"Not found: {unknown}"), args.json)
    issues = [
        i.to_dict() for i in conc.issues_for(selected) if args.all or i.kind in BLOCKING_KINDS
    ]
    if args.csv:
        conc.write_csv(args.csv, selected=selected)
    return _emit(
        {
            "success": True,
            "summary": conc.summary(),
            "selection": resolution,
            "issue_count": len(issues),
            "issues": issues[: args.limit],
            "csv": args.csv,
        },
        args.json,
    )


def cmd_describe(args: argparse.Namespace) -> int:
    return _emit(describe_variable(args.code, args.year, args.dataset), args.json)


def cmd_vintages(args: argparse.Namespace) -> int:
    client = _client(True) if args.refresh else None
    return _emit({"dataset": args.dataset, "years": available_vintages(args.dataset, client, args.refresh)}, args.json)


def cmd_validate(args: argparse.Namespace) -> int:
    result = validate_all_crosswalks()
    if not result["valid"]:
        result = errors.error_result(errors.INVALID_INPUT, "Invalid crosswalk rows.", **result)
    return _emit(result, args.json)


def _floats(text: str) -> List[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def _groups(text: str) -> Dict[str, str]:
    """GEOID -> group mapping from a JSON object or a two-column CSV (GEOID, group)."""
    text = text.strip()
    if text.startswith("{"):
        return {str(k): str(v) for k, v in json.loads(text).items()}
    import pandas as pd

    df = pd.read_csv(text, dtype=str)
    return dict(zip(df.iloc[:, 0], df.iloc[:, 1]))


def cmd_derive(args: argparse.Namespace) -> int:
    return _emit(
        workflows.derive_estimate(
            args.operation, _floats(args.estimates), _floats(args.moes), args.percent,
            args.year_current, args.year_prior,
        ),
        args.json,
    )


def cmd_aggregate(args: argparse.Namespace) -> int:
    return _emit(
        workflows.aggregate_geographies(
            args.input, _groups(args.groups), _list(args.variables),
            json.loads(args.derive) if args.derive else None, args.year, args.method,
            args.replicates, args.table, args.out,
        ),
        args.json,
    )


def cmd_median(args: argparse.Namespace) -> int:
    return _emit(
        workflows.median_from_distribution(
            args.table, args.year, args.input, _list(args.geoids),
            _floats(args.counts) if args.counts else None, args.per_row, args.replicates,
            args.design_factor,
        ),
        args.json,
    )


def cmd_significance(args: argparse.Namespace) -> int:
    return _emit(
        workflows.batch_significance(
            args.input, args.est1, args.moe1, args.est2, args.moe2, args.year1, args.year2,
            args.confidence, args.correction, args.out,
        ),
        args.json,
    )


def cmd_harmonize(args: argparse.Namespace) -> int:
    return _emit(
        workflows.apportion_tracts(args.input, args.target, args.weight, args.weights_file, args.out),
        args.json,
    )


def cmd_replicates(args: argparse.Namespace) -> int:
    return _emit(workflows.check_replicate_availability(args.table, args.year, args.geo), args.json)


def cmd_draft_rule(args: argparse.Namespace) -> int:
    return _emit(
        workflows.draft_rule_override(
            args.table, args.from_year, args.to_year, args.pattern, args.replacement,
            args.drafted_by, args.rationale, args.source_url or "", args.dataset or "",
        ),
        args.json,
    )


def cmd_record_rule(args: argparse.Namespace) -> int:
    with open(args.row, encoding="utf-8") as f:
        row = json.load(f)
    row = row.get("draft_row", row)
    return _emit(workflows.record_rule_review(row, args.reviewed_by, args.dataset or ""), args.json)


def cmd_validate_census(args: argparse.Namespace) -> int:
    import os

    live = args.live or (not args.offline and os.getenv("CENSUS_LIVE_TESTS") == "1")
    result = workflows.run_validation(args.checks, live, args.output_dir)
    if not args.json and result.get("results"):
        for r in result["results"]:
            print(f"{r['check_id']:>4}  {str(r['status']).upper():<7}  compared={r['n_compared']:<7} failed={r['n_failed']}",
                  file=sys.stderr)
    if result.get("error_code"):
        return _emit(result, args.json)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Overall:", "PASS" if result["success"] else "FAIL " + ", ".join(
            result["checks_summary"]["failed"] + result["checks_summary"]["errors"]))
    return EXIT_OK if result["success"] else EXIT_FAILURE


def cmd_tool(args: argparse.Namespace) -> int:
    """Run any shared workflow tool by name with JSON arguments."""
    from census import workflows_geo, workflows_inference

    fn = (getattr(workflows_geo, args.name, None) or getattr(workflows_inference, args.name, None)
          or getattr(workflows, args.name, None))
    if fn is None or args.name.startswith("_") or not callable(fn):
        return _emit(errors.error_result(errors.INVALID_INPUT, f"Unknown tool '{args.name}'."), args.json)
    try:
        kwargs = json.loads(args.arguments) if args.arguments else {}
    except json.JSONDecodeError as exc:
        return _emit(errors.error_result(errors.INVALID_INPUT, f"Arguments are not valid JSON: {exc}"), args.json)
    try:
        result = fn(**kwargs)
    except TypeError as exc:
        result = errors.error_result(errors.INVALID_INPUT, str(exc))
    except Exception as exc:  # noqa: BLE001 - agents need a structured error, not a traceback
        result = errors.error_result(errors.INVALID_INPUT, f"{type(exc).__name__}: {exc}",
                                     error_type=type(exc).__name__)
        result["summary"] = f"{args.name} failed: {type(exc).__name__}: {exc}"
        result["next_step"] = "check the arguments (column names, formula, paths) and call again"
    return _emit(result, args.json)


def cmd_cache(args: argparse.Namespace) -> int:
    cache = get_default_cache()
    if cache is None:
        return _emit({"success": True, "cache": "disabled"}, args.json)
    if args.action == "clear":
        return _emit({"success": True, "cleared": cache.clear()}, args.json)
    return _emit({"success": True, **cache.stats()}, args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m census", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="dry run: calls, concordance issues, would_block")
    _add_extract_args(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("extract", help="plan, then download and write outputs")
    _add_extract_args(p)
    p.add_argument("--out", required=True, help="output directory")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("replay", help="re-run an extraction from its metadata JSON and verify outputs")
    p.add_argument("metadata", help="<table>_extraction_metadata.json")
    p.add_argument("--output-dir", help="default: <original dir>/replays/<timestamp>")
    p.add_argument("--no-verify", action="store_true", help="skip hash and cell comparison")
    p.add_argument("--refresh", action="store_true", help="bypass the query cache (detect revisions)")
    p.add_argument("--no-cache", action="store_true", help="do not read or write the query cache")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("concordance", help="cross-release concordance report (offline)")
    p.add_argument("--table", required=True)
    p.add_argument("--years", required=True)
    p.add_argument("--variables")
    p.add_argument("--dataset")
    p.add_argument("--all", action="store_true", help="include non-blocking issues")
    p.add_argument("--csv", help="also write the concordance CSV here")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_concordance)

    p = sub.add_parser("describe", help="label/concept/universe of a code in one release")
    p.add_argument("--code", required=True)
    p.add_argument("--year", type=int)
    p.add_argument("--dataset")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("vintages", help="available release years")
    p.add_argument("--dataset", default="acs5")
    p.add_argument("--refresh", action="store_true", help="also query the live API catalog")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_vintages)

    p = sub.add_parser("validate-crosswalks", help="check all override files")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("derive", help="derived estimate and MOE (Census approximation formulas)")
    p.add_argument("operation", choices=("proportion", "ratio", "product", "difference", "percent_change", "sum"))
    p.add_argument("--estimates", required=True, help="comma-separated, e.g. numerator,denominator")
    p.add_argument("--moes", required=True, help="comma-separated 90%% MOEs")
    p.add_argument("--percent", action="store_true", help="0-100 scale for proportion/ratio")
    p.add_argument("--year-current", type=int)
    p.add_argument("--year-prior", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_derive)

    p = sub.add_parser("aggregate", help="combine geographies into custom areas, then derive rates")
    p.add_argument("--input", required=True, help="CSV/Parquet from extract")
    p.add_argument("--groups", required=True, help="JSON {GEOID: group} or CSV (GEOID,group)")
    p.add_argument("--variables", help="estimate columns to sum (default: all additive)")
    p.add_argument("--derive", help="JSON derive spec applied after aggregation")
    p.add_argument("--year", type=int)
    p.add_argument("--method", default="auto", choices=("auto", "replicate", "approximate"))
    p.add_argument("--replicates", action="store_true", help="download Census replicate estimates")
    p.add_argument("--table", help="detailed table for replicate lookups")
    p.add_argument("--out", help="output CSV/Parquet path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_aggregate)

    p = sub.add_parser("median", help="median and MOE from a distribution table")
    p.add_argument("--table", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--input", help="CSV/Parquet with the table's columns")
    p.add_argument("--geoids", help="GEOIDs to combine (default: all rows)")
    p.add_argument("--counts", help="bracket counts instead of --input")
    p.add_argument("--per-row", action="store_true")
    p.add_argument("--replicates", action="store_true")
    p.add_argument("--design-factor", type=float)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_median)

    p = sub.add_parser("significance", help="many-geography difference tests with correction")
    p.add_argument("--input", required=True)
    for name in ("est1", "moe1", "est2", "moe2"):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--year1", type=int)
    p.add_argument("--year2", type=int)
    p.add_argument("--confidence", type=float, default=0.90)
    p.add_argument("--correction", default="holm", choices=("holm", "bonferroni", "bh", "by"))
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_significance)

    p = sub.add_parser("harmonize", help="apportion a tract panel across the 2010/2020 tract change")
    p.add_argument("--input", required=True)
    p.add_argument("--target", type=int, default=2020, choices=(2010, 2020))
    p.add_argument("--weight", default="land_area", choices=("land_area", "total_area", "population"))
    p.add_argument("--weights-file", help="NHGIS-style source_geoid,target_geoid,weight CSV")
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_harmonize)

    p = sub.add_parser("replicates", help="is a Census replicate (VRE) table published?")
    p.add_argument("--table", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--geo", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_replicates)

    p = sub.add_parser("draft-rule", help="draft a table-scoped concordance rule (step 1 of 2)")
    p.add_argument("--table", required=True)
    p.add_argument("--from-year", type=int, required=True)
    p.add_argument("--to-year", type=int, required=True)
    p.add_argument("--pattern", required=True)
    p.add_argument("--replacement", required=True)
    p.add_argument("--drafted-by", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--source-url")
    p.add_argument("--dataset")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_draft_rule)

    p = sub.add_parser("record-rule", help="record an independent review of a drafted rule (step 2 of 2)")
    p.add_argument("row", help="JSON file with the draft row (or the whole draft-rule output)")
    p.add_argument("--reviewed-by", required=True)
    p.add_argument("--dataset")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_record_rule)

    p = sub.add_parser("validate", help="check calculations against Census-published values")
    p.add_argument("--checks", help="comma-separated subset of F1-F10")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="query live Census services")
    mode.add_argument("--offline", action="store_true", help="use frozen fixtures (default unless CENSUS_LIVE_TESTS=1)")
    p.add_argument("--output-dir", help="also write validation_report.md/.json here")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate_census)

    p = sub.add_parser("tool", help="run any MCP workflow tool by name, e.g. tool fit_model '{\"input_path\": ...}'")
    p.add_argument("name", help="tool name, e.g. fit_model, small_area_estimates, make_figure")
    p.add_argument("arguments", nargs="?", help="JSON object of keyword arguments")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tool)

    p = sub.add_parser("cache", help="query cache maintenance")
    p.add_argument("action", choices=("stats", "clear"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cache)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
