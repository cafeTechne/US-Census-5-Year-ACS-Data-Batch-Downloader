# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audit cross-vintage variable drift using the offline metadata archive.

Builds a concordance for every table in the selected datasets across all
archived vintages and writes:

    docs/audits/concordance_audit.md    summary report
    docs/audits/concordance_issues.csv  one row per issue

Usage:
    python scripts/audit_concordance.py                      # profile, subject, cprofile, acs5 sample
    python scripts/audit_concordance.py --datasets profile   # one dataset
    python scripts/audit_concordance.py --acs5-all           # every detailed table (slow)
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from census.concordance import BLOCKING_KINDS, build_concordance  # noqa: E402
from census.metadata import archived_vintages, vintage_groups  # noqa: E402

ACS5_SAMPLE = [
    "B01001", "B01003", "B02001", "B03002", "B08301", "B15003", "B17001",
    "B19001", "B19013", "B19301", "B23025", "B25003", "B25064", "B25077", "B27001",
]


def tables_for(dataset: str, acs5_all: bool):
    if dataset == "acs5" and not acs5_all:
        return ACS5_SAMPLE
    names = set()
    for year in archived_vintages(dataset):
        groups, _ = vintage_groups(dataset, year)
        names |= set(groups)
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", nargs="+", default=["profile", "subject", "cprofile", "acs5"])
    parser.add_argument("--acs5-all", action="store_true")
    parser.add_argument("--out", default=os.path.join(ROOT, "docs", "audits"))
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    issue_rows = []
    table_rows = []
    for dataset in args.datasets:
        years = archived_vintages(dataset)
        for table in tables_for(dataset, args.acs5_all):
            conc = build_concordance(table, dataset, years)
            if not conc.concepts:
                continue
            s = conc.summary()
            table_rows.append(
                {
                    "dataset": dataset,
                    "table": table,
                    "years": f"{years[0]}-{years[-1]}",
                    "concepts": s["concepts"],
                    **{f"status_{k}": v for k, v in s["status_counts"].items()},
                    **{f"issue_{k}": v for k, v in s["issue_counts"].items()},
                    "blocking": s["blocking_issues"],
                }
            )
            for issue in conc.issues:
                issue_rows.append(
                    {
                        "dataset": dataset,
                        "table": table,
                        "kind": issue.kind,
                        "blocking": issue.blocking,
                        "message": issue.message,
                        "evidence": json.dumps(issue.evidence, sort_keys=True),
                    }
                )
            print(f"{dataset:9s} {table:8s} concepts={s['concepts']:5d} issues={s['issue_counts']}")

    with open(os.path.join(args.out, "concordance_issues.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["dataset", "table", "kind", "blocking", "message", "evidence"])
        writer.writeheader()
        writer.writerows(issue_rows)

    lines = [
        "# Concordance audit",
        "",
        f"Generated {date.today().isoformat()} by `scripts/audit_concordance.py` from the offline",
        "metadata archive (`metadata/`). Regenerate after refreshing the archive.",
        "",
        "## Summary by dataset",
        "",
        "| Dataset | Tables | Concepts | Renumbered | Relabeled (review) | Redefined (split) | Possible continuation | Ambiguous | Universe changed | Tables with blocking issues |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for dataset in args.datasets:
        rows = [r for r in table_rows if r["dataset"] == dataset]
        if not rows:
            continue
        tot = Counter()
        for r in rows:
            for k, v in r.items():
                if isinstance(v, int):
                    tot[k] += v
        lines.append(
            f"| {dataset} | {len(rows)} | {tot['concepts']} | {tot['status_renumbered']} | "
            f"{tot['issue_relabeled']} | {tot['issue_redefined']} | {tot['issue_possible_continuation']} | "
            f"{tot['issue_ambiguous']} | {tot['issue_universe_changed']} | "
            f"{sum(1 for r in rows if r['blocking'])} |"
        )
    lines += [
        "",
        "Blocking kinds: " + ", ".join(sorted(BLOCKING_KINDS)) + ".",
        "",
        "## Tables with the most blocking issues",
        "",
        "| Dataset | Table | Concepts | Blocking issues |",
        "| :--- | :--- | ---: | ---: |",
    ]
    for r in sorted(table_rows, key=lambda r: -r["blocking"])[:25]:
        if r["blocking"]:
            lines.append(f"| {r['dataset']} | {r['table']} | {r['concepts']} | {r['blocking']} |")
    lines += ["", "Full issue list: `concordance_issues.csv`.", ""]
    with open(os.path.join(args.out, "concordance_audit.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n{len(issue_rows)} issues across {len(table_rows)} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
