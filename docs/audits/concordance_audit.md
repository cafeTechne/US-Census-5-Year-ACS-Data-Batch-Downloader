# Concordance audit

Generated 2026-11-01 by `scripts/audit_concordance.py` from the offline
metadata archive (`metadata/`). Regenerate after refreshing the archive.

## Summary by dataset

| Dataset | Tables | Concepts | Renumbered | Relabeled (review) | Redefined (split) | Possible continuation | Ambiguous | Universe changed | Tables with blocking issues |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| profile | 5 | 1496 | 550 | 424 | 1527 | 0 | 175 | 0 | 5 |
| subject | 85 | 40615 | 1314 | 11958 | 10159 | 0 | 860 | 0 | 78 |
| cprofile | 5 | 1552 | 1368 | 26 | 649 | 74 | 48 | 0 | 5 |
| acs5 | 15 | 304 | 0 | 23 | 7 | 0 | 0 | 0 | 2 |

Blocking kinds: ambiguous, continuity_flag, continuity_unavailable, invalid_override, metadata_unavailable, relabeled, universe_changed.

## Tables with the most blocking issues

| Dataset | Table | Concepts | Blocking issues |
| :--- | :--- | ---: | ---: |
| subject | S0502 | 1607 | 842 |
| subject | S0502PR | 1607 | 842 |
| subject | S0505 | 1782 | 630 |
| subject | S0506 | 1782 | 629 |
| subject | S0802 | 1130 | 529 |
| subject | S0804 | 1086 | 506 |
| subject | S0503 | 1188 | 415 |
| subject | S0501 | 1375 | 410 |
| subject | S0504 | 1188 | 398 |
| subject | S1201 | 444 | 343 |
| subject | S1501 | 999 | 339 |
| subject | S1702 | 778 | 332 |
| subject | S2601A | 896 | 327 |
| subject | S2601APR | 896 | 327 |
| subject | S2601B | 905 | 308 |
| subject | S2201 | 523 | 234 |
| subject | S0901 | 346 | 205 |
| profile | DP03 | 318 | 188 |
| subject | S1601 | 403 | 187 |
| subject | S2501 | 411 | 177 |
| subject | S0701PR | 550 | 172 |
| subject | S0701 | 550 | 170 |
| subject | S0601 | 408 | 167 |
| subject | S0601PR | 408 | 165 |
| subject | S0801 | 438 | 164 |

Full issue list: `concordance_issues.csv`.
