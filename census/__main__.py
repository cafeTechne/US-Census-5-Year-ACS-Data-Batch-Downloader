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
    python -m census cache stats
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from census import errors
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
    )


def _add_extract_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--table", required=True)
    p.add_argument("--years", required=True, help="e.g. 2015-2022 or 2018,2020")
    p.add_argument("--geo", default="state", help="us, state, county, tract, place, zcta, msa")
    p.add_argument("--state", help="2-digit state FIPS")
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
    p.add_argument("--price-index", default="r-cpi-u-rs", choices=("r-cpi-u-rs", "cpi-u"))
    p.add_argument("--codebook", action="store_true")
    p.add_argument("--slug-style", default="label", choices=("label", "code"))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", action="store_true")


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        entities = build_entities(args.geo, args.state, _list(args.codes))
    except ValueError as exc:
        return _emit(errors.error_result(errors.INVALID_GEO_LEVEL, str(exc)), args.json)
    plan = plan_extraction(
        args.table, entities, _years(args.years), args.format, _options(args),
        args.dataset, _list(args.variables), _client(args.no_cache),
    )
    return _emit(public_plan(plan), args.json)


def cmd_extract(args: argparse.Namespace) -> int:
    try:
        entities = build_entities(args.geo, args.state, _list(args.codes))
    except ValueError as exc:
        return _emit(errors.error_result(errors.INVALID_GEO_LEVEL, str(exc)), args.json)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    result = run_batch(
        args.table, entities, _years(args.years), args.out, args.format, _options(args),
        args.dataset, _client(args.no_cache), log=log, variables=_list(args.variables),
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
