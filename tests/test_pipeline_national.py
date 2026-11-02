"""Nationwide (state_code='all') entity expansion, plans, failures and cache resume."""

import os
import shutil
import tempfile
import unittest

import pandas as pd
import requests

from census.api import CensusAPIClient, build_geo_params
from census.cache import QueryCache
from census.pipeline import (
    ACS_STATE_CODES,
    ResearchOptions,
    build_entities,
    expand_states,
    plan_extraction,
    run_batch,
)

HEADER = ["GEO_ID", "NAME", "B19013_001E", "B19013_001EA", "B19013_001M", "B19013_001MA"]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """Answers data requests per the for/in predicates; fails listed states."""

    def __init__(self, fail_states=()):
        self.requests = []
        self.fail_states = set(fail_states)

    def get(self, endpoint, params=None, timeout=None):
        params = dict(params or {})
        self.requests.append((endpoint, params))
        if "group(" not in params.get("get", ""):
            raise requests.ConnectionError("not a data request")
        level = params["for"].split(":")[0]
        in_pred = params.get("in", "")
        states = in_pred.split(" ")[0].split(":")[1].split(",") if in_pred else []
        if level == "state":
            states = params["for"].split(":")[1].split(",")
        if self.fail_states & set(states):
            raise requests.ConnectionError(f"HTTP 503 for state {states}")
        rows = []
        for st in states:
            if level == "tract":
                rows.append([f"1400000US{st}001000100", f"Tract {st}", "50000", None, "4000", None, st, "001", "000100"])
            elif level == "county":
                rows.append([f"0500000US{st}001", f"County {st}", "60000", None, "3000", None, st, "001"])
            elif level == "place":
                rows.append([f"1600000US{st}00100", f"Place {st}", "55000", None, "2500", None, st, "00100"])
            else:
                rows.append([f"0400000US{st}", f"State {st}", "70000", None, "500", None, st])
        geo_cols = {"tract": ["state", "county", "tract"], "county": ["state", "county"],
                    "place": ["state", "place"], "state": ["state"]}[level]
        return FakeResponse([HEADER + geo_cols] + rows)


def fake_client(cache=None, fail_states=()):
    client = CensusAPIClient(api_key="", cache=cache)
    client.session = FakeSession(fail_states)
    client.fetch_variable_metadata = lambda *a, **k: {}  # type: ignore[assignment]
    return client


class TestExpandStates(unittest.TestCase):
    def test_all(self):
        states = expand_states("all")
        self.assertEqual(len(states), 51)
        self.assertIn("11", states)
        self.assertNotIn("72", states)
        self.assertEqual(expand_states("ALL", include_pr=True)[-1], "72")
        self.assertEqual(len(expand_states("all", include_pr=True)), 52)

    def test_explicit_codes(self):
        self.assertEqual(expand_states("36"), ["36"])
        self.assertEqual(expand_states("6, 48,06"), ["06", "48"])
        self.assertEqual(expand_states("72"), ["72"])

    def test_rejects_territories_and_unknown(self):
        for bad in ("66", "78", "60", "99", "", None):
            with self.assertRaises(ValueError):
                expand_states(bad)


class TestNationwideEntities(unittest.TestCase):
    def test_county_place_state_single_request(self):
        for level in ("county", "place", "state"):
            ents = build_entities(level, "all")
            self.assertEqual(len(ents), 1, level)
            self.assertEqual(ents[0].state_code.split(","), list(ACS_STATE_CODES))
        params = build_geo_params(**build_entities("county", "all")[0].request_kwargs())
        self.assertEqual(params["for"], "county:*")
        self.assertTrue(params["in"].startswith("state:01,02,04"))
        self.assertNotIn("72", params["in"])
        place = build_geo_params(**build_entities("place", "all", include_pr=True)[0].request_kwargs())
        self.assertEqual(place["for"], "place:*")
        self.assertTrue(place["in"].endswith(",72"))

    def test_tract_one_request_per_state_one_group(self):
        ents = build_entities("tract", "all")
        self.assertEqual(len(ents), 51)
        self.assertEqual({e.group for e in ents}, {"All_States_All_Tracts"})
        self.assertEqual(ents[0].state_code, "01")
        self.assertEqual(len(build_entities("tract", "all", include_pr=True)), 52)

    def test_state_relative_codes_rejected(self):
        for level in ("county", "place", "tract"):
            with self.assertRaises(ValueError):
                build_entities(level, "all", ["001"])

    def test_national_levels_ignore_state(self):
        self.assertEqual(build_entities("us", "all")[0].geo_level, "us")
        self.assertEqual(build_entities("msa", "all")[0].msa_code, "*")
        zcta = build_entities("zcta", "all")
        self.assertEqual((zcta[0].zcta_code, zcta[0].state_code), ("*", None))

    def test_tract_wildcard_state_rejected(self):
        with self.assertRaises(ValueError):
            build_geo_params("tract", state_code="*")


class TestNationwidePlanAndRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = QueryCache(os.path.join(self.dir, "cache.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _plan(self, level, years, client):
        return plan_extraction(
            "B19013", build_entities(level, "all"), years, "panel",
            ResearchOptions(continuity_check=False), client=client,
        )

    def test_plan_call_counts(self):
        client = fake_client(self.cache)
        county = self._plan("county", [2021, 2022], client)
        self.assertTrue(county["success"], county)
        self.assertEqual(county["api_calls"], 2)
        self.assertEqual(county["request_targets"], 1)
        self.assertEqual(county["states_covered"], 51)
        tract = self._plan("tract", [2021, 2022], client)
        self.assertEqual(tract["api_calls"], 102)
        self.assertEqual(tract["cached_calls"], 0)
        self.assertEqual(tract["states_covered"], 51)

    def test_county_nationwide_run(self):
        out = os.path.join(self.dir, "out")
        res = run_batch(
            "B19013", build_entities("county", "all"), [2022], out, "panel",
            ResearchOptions(continuity_check=False), client=fake_client(self.cache),
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(res["successful_calls"], 1)
        panel = pd.read_csv(res["panel_path"], dtype={"geoid": str})
        self.assertEqual(len(panel), 51)
        self.assertNotIn("72001", set(panel["geoid"]))

    def test_partial_failure_then_resume_from_cache(self):
        out = os.path.join(self.dir, "out")
        ents = build_entities("tract", "all")
        flaky = fake_client(self.cache, fail_states={"06", "48"})
        first = run_batch(
            "B19013", ents, [2022], out, "panel", ResearchOptions(), client=flaky,
        )
        self.assertTrue(first["success"])
        self.assertEqual(first["successful_calls"], 49)
        self.assertEqual(
            sorted(f["entity"] for f in first["failed_calls"]),
            ["California_All_Tracts", "Texas_All_Tracts"],
        )
        self.assertIn("query cache", first["resume_hint"])

        plan = plan_extraction("B19013", ents, [2022], "panel", ResearchOptions(), client=flaky)
        self.assertEqual((plan["api_calls"], plan["cached_calls"]), (51, 49))

        healthy = fake_client(self.cache)
        second = run_batch(
            "B19013", ents, [2022], out, "panel", ResearchOptions(), client=healthy,
        )
        self.assertEqual(second["failed_calls"], [])
        self.assertEqual(second["successful_calls"], 51)
        data_requests = [r for r in healthy.session.requests if "group(" in r[1].get("get", "")]
        self.assertEqual(len(data_requests), 2)  # only the two failed states
        panel = pd.read_csv(second["panel_path"], dtype={"geoid": str})
        self.assertEqual(len(panel), 51)


if __name__ == "__main__":
    unittest.main()
