# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline test runs: no real waits in census.http (fake sessions answer instantly).

Live runs (CENSUS_LIVE_TESTS=1) keep the polite defaults: pacing, backoff and
the delayed firewall-page retry. Values already set in the environment win.
"""

import os

if not os.environ.get("CENSUS_LIVE_TESTS"):
    for _name in ("CENSUS_WWW2_MIN_INTERVAL", "CENSUS_WWW2_BACKOFF_BASE", "CENSUS_WWW2_REJECTED_RETRY_DELAY"):
        os.environ.setdefault(_name, "0")
