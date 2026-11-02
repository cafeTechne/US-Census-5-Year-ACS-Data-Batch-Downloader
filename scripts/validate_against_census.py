# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the validation battery against Census-published values (PLAN_ROUND_7.md section 8).

    python scripts/validate_against_census.py [--checks F1,F2,...] [--offline|--live]
                                              [--output-dir DIR] [--record]

Writes validation_report.md and validation_report.json into --output-dir
(default docs/audits/). Exit code 1 when any check fails or errors.

Mode: --live queries api.census.gov (key from CENSUS_API_KEY, e.g. in .env)
and www2.census.gov; --offline reads the frozen fixtures in
tests/fixtures/validation/. Without either flag the mode is live when the
environment variable CENSUS_LIVE_TESTS=1, otherwise offline. --record (live
only) rewrites the frozen fixtures from the live responses.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from census.validation import CHECK_IDS, DEFAULT_REPORT_DIR, parse_checks, run_validation  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checks", default=None, help=f"comma-separated subset of {','.join(CHECK_IDS)}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="query the live Census services")
    mode.add_argument("--offline", action="store_true", help="use the frozen fixtures")
    parser.add_argument("--output-dir", default=DEFAULT_REPORT_DIR, help="report directory (default docs/audits)")
    parser.add_argument("--fixture-dir", default=None, help="fixture directory (default tests/fixtures/validation)")
    parser.add_argument("--record", action="store_true", help="with --live, rewrite the frozen fixtures")
    args = parser.parse_args(argv)

    live = args.live or (not args.offline and os.getenv("CENSUS_LIVE_TESTS") == "1")
    if args.record and not live:
        parser.error("--record needs live mode")
    try:
        checks = parse_checks(args.checks)
    except ValueError as exc:
        parser.error(str(exc))
    command = "python scripts/validate_against_census.py " + " ".join(
        a for a in (argv if argv is not None else sys.argv[1:])
    )
    payload = run_validation(
        checks=checks,
        live=live,
        output_dir=args.output_dir,
        fixture_dir=args.fixture_dir,
        record=args.record,
        meta={"command": command.strip()},
    )
    for r in payload["results"]:
        print(f"{r['check_id']:>4}  {r['status'].upper():<7}  compared={r['n_compared']:<7} failed={r['n_failed']}")
    print(f"Reports: {payload['report_paths']['markdown']}, {payload['report_paths']['json']}")
    summary = payload["summary"]
    print("Overall:", "PASS" if summary["all_passed"] else f"FAIL ({', '.join(summary['failed'] + summary['errors'])})")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
