# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused, offline scientific/input and refresh regressions (Round 17 I3-I7)."""

import importlib.util
import json
from pathlib import Path
from unittest import mock
from urllib.parse import quote, quote_plus

import numpy as np
import pandas as pd
import pytest
import requests

from census.api import CensusAPIClient
from census.correlation import correlation_matrix
from census.did import difference_in_differences
from census.multilevel import fit_multilevel
from census.pipeline import ResearchOptions, build_entities, run_batch
from census.planning import aggregation_forecast


@pytest.mark.parametrize("se_x,se_y", [(2.0, 2.0), (2.0, 0.5), (0.5, 2.0), (0.0, np.nan)])
def test_disattenuation_requires_two_defined_positive_reliabilities(se_x, se_y):
    # sample Var([0,1,2,3]) = 5/3; SE=2 gives reliability -1.4.
    frame = pd.DataFrame({"x": [0., 1., 2., 3.], "y": [0., 1., 2., 3.],
                          "x_se": se_x, "y_se": se_y})
    result = correlation_matrix(frame, ["x", "y"], disattenuate=True)
    assert result.matrix.loc["x", "y"] == pytest.approx(1.)
    assert np.isnan(result.disattenuated.loc["x", "y"])
    assert any("not positive" in note for note in result.notes)


def test_disattenuation_positive_reliabilities_keep_hand_formula():
    frame = pd.DataFrame({"x": [0., 1., 2., 3.], "y": [0., 1., 3., 2.],
                          "x_se": 0.5, "y_se": 0.5})
    result = correlation_matrix(frame, ["x", "y"], disattenuate=True)
    # Both sample variances = 5/3, covariance = 4/3: observed r=.8, reliability=.85.
    assert result.disattenuated.loc["x", "y"] == pytest.approx(.8 / .85)


def test_disattenuation_zero_reliability_is_undefined():
    # Sample variance is exactly 1, so SE=1 makes reliability exactly zero.
    frame = pd.DataFrame({"x": [-1., 0., 1.], "y": [-1., 0., 1.], "x_se": 1., "y_se": .5})
    result = correlation_matrix(frame, ["x", "y"], disattenuate=True)
    assert result.provenance["reliability"]["x"] == 0.
    assert np.isnan(result.disattenuated.loc["x", "y"])


@pytest.mark.parametrize("rho", [-1.1, 1.1, np.inf, -np.inf, np.nan, -.5])
def test_aggregation_forecast_rejects_infeasible_correlation(rho):
    with pytest.raises(ValueError, match="correlation"):
        aggregation_forecast([100.], [1.645], group_sizes=[5], correlation=rho, target_cv=1.)


@pytest.mark.parametrize("rho,expected_variance", [(-.2, 1.), (-.25, 0.), (0., 5.), (1., 25.)])
def test_aggregation_forecast_feasible_and_singular_boundary(rho, expected_variance):
    # k=5, each variance 1 => variance(sum)=5+20*rho; bound rho=-1/4 is feasible.
    result = aggregation_forecast([100.], [1.645], group_sizes=[5], correlation=rho)
    assert result.loc[0, "se"] ** 2 == pytest.approx(expected_variance)
    assert result.loc[0, "cv"] == pytest.approx(np.sqrt(expected_variance) / 500 * 100)


def _panel():
    return pd.DataFrame({"GEOID": [f"{i:05d}" for i in range(6)] * 2,
                         "Year": [2013] * 6 + [2018] * 6,
                         "y": [4., 6., 7., 9., 12., 14., 6., 7., 10., 13., 17., 18.],
                         "treated": [0, 0, 0, 1, 1, 1] * 2,
                         "y_se": [1.] * 12})


@pytest.mark.parametrize("bad_se", [-1., -555555555., np.inf, -np.inf])
def test_did_rejects_invalid_original_se_before_fit(bad_se):
    frame = _panel()
    frame.loc[0, "y_se"] = bad_se
    with mock.patch("census.did._fit", side_effect=AssertionError("optimizer must not run")):
        with pytest.raises(ValueError, match="SE.*finite|sampling SE"):
            difference_in_differences(frame, "y", "treated", 2018)


@pytest.mark.parametrize("bad_se", [-1., -555555555., np.inf, -np.inf])
def test_multilevel_rejects_invalid_original_se_before_fit(bad_se):
    frame = _panel().assign(group=[f"g{i % 3}" for i in range(12)])
    frame.loc[0, "y_se"] = bad_se
    with mock.patch("census.multilevel._fit_known", side_effect=AssertionError("optimizer must not run")):
        with pytest.raises(ValueError, match="sampling SE"):
            fit_multilevel("y ~ treated", frame, "group", sampling_se="y_se", small_sample=None)


def test_did_missing_se_drop_policy_and_zero_se_are_preserved():
    frame = _panel()
    frame.loc[0, "y_se"] = np.nan
    frame.loc[1, "y_se"] = 0.
    # _prepare is the input boundary: missing rows stay dropped, genuine zero stays exact.
    from census.did import _prepare, _differences
    prepared = _prepare(frame, "y", "treated", "Year", "GEOID", 2018, None, "two_group", None, 5)
    assert len(prepared.data) == 11
    assert (prepared.data.y_se == 0).sum() == 1
    diff = _differences(prepared, "y", "treated", "Year", "GEOID", "two_group")
    assert 0. in diff["Dr"]


@pytest.mark.parametrize("error_type", [requests.HTTPError, requests.ConnectionError, ValueError])
@pytest.mark.parametrize("encoded", [False, True])
def test_api_errors_redact_raw_and_encoded_credentials(error_type, encoded):
    secret = "fake+key/with space?"
    client = CensusAPIClient(api_key=secret, cache=None)
    endpoint, params = client.build_request(2023, "26", "B01003", "state")
    url = endpoint + "?get=NAME&key=" + (quote_plus(secret) if encoded else secret) + "&for=state:26"
    error = error_type("503 unavailable at " + url)
    with mock.patch.object(client.session, "get", side_effect=error):
        assert client._get_json(endpoint, params) is None
    for variant in (secret, quote(secret, safe=""), quote_plus(secret)):
        assert variant not in client.last_error
    assert "503" in client.last_error and endpoint in client.last_error


def test_pipeline_logs_results_and_metadata_do_not_retain_api_key(tmp_path):
    secret = "fake-round17-key"
    client = CensusAPIClient(api_key=secret, cache=None)
    logs = []
    with mock.patch.object(client.session, "get", side_effect=requests.HTTPError(
            "503 unavailable https://api.census.gov/data/2023/acs/acs5?key=" + secret)):
        result = run_batch("B01003", build_entities("state", "26"), [2023], str(tmp_path),
                           "panel", ResearchOptions(use_cache=False), client=client, log=logs.append)
    assert result["failed_calls"]
    texts = [json.dumps(result, default=str), "\n".join(logs)]
    texts += [p.read_text() for p in tmp_path.rglob("*.json")]
    assert len(texts) > 2  # Includes real extraction metadata, not only an isolated sanitizer test.
    assert all(secret not in text for text in texts)
    assert "503" in "\n".join(texts)


def _errata_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "fetch_acs_errata.py"
    spec = importlib.util.spec_from_file_location("r17_fetch_acs_errata", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def _fake_errata_sources(script, monkeypatch, *, failed=False):
    monkeypatch.setattr(script, "LISTINGS", {"errata": "https://example.invalid/listing"})
    items = [{"url": "https://example.invalid/good.html"}, {"url": "https://example.invalid/bad.html"}]
    monkeypatch.setattr(script, "walk_listing", lambda *a: (items, "test-list"))
    def get(_self, url):
        if failed and url.endswith("bad.html"):
            raise requests.HTTPError("503 unavailable")
        return "<html>note</html>"
    monkeypatch.setattr(script.Fetcher, "get", get)
    monkeypatch.setattr(script.errata, "parse_note", lambda page, url, kind, today, item:
                        {"kind": kind, "url": url, "retrieved_at": today, "date_iso": "2020-01-01"})


def test_partial_errata_refresh_preserves_archive_and_sidecar(tmp_path, monkeypatch):
    script = _errata_script()
    monkeypatch.setattr(script, "ROOT", str(tmp_path))
    archive = tmp_path / script.errata.ERRATA_PATH
    sidecar = tmp_path / script.errata.SIDECAR_PATH
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'{"records":[{"id":"existing-warning"}]}\n')
    sidecar.write_bytes(b'{"sha256":"existing-checksum"}\n')
    before = archive.read_bytes(), sidecar.read_bytes()
    _fake_errata_sources(script, monkeypatch, failed=True)
    assert script.main(["--delay", "0"]) != 0
    assert (archive.read_bytes(), sidecar.read_bytes()) == before


def test_complete_mocked_errata_refresh_writes_matching_sidecar(tmp_path, monkeypatch):
    script = _errata_script()
    monkeypatch.setattr(script, "ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    _fake_errata_sources(script, monkeypatch)
    assert script.main(["--delay", "0"]) == 0
    archive = tmp_path / script.errata.ERRATA_PATH
    meta = json.loads((tmp_path / script.errata.SIDECAR_PATH).read_text())
    assert meta["records"] == 2
    assert meta["sha256"] == script.errata.sha256_file(str(archive))


def test_failed_errata_listing_preserves_archive_and_sidecar(tmp_path, monkeypatch):
    script = _errata_script()
    monkeypatch.setattr(script, "ROOT", str(tmp_path))
    archive = tmp_path / script.errata.ERRATA_PATH
    sidecar = tmp_path / script.errata.SIDECAR_PATH
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"existing warning archive")
    sidecar.write_bytes(b"existing warning sidecar")
    monkeypatch.setattr(script, "LISTINGS", {"errata": "https://example.invalid/listing"})
    with mock.patch.object(script, "walk_listing", side_effect=requests.HTTPError("503 unavailable")):
        assert script.main(["--delay", "0"]) != 0
    assert archive.read_bytes() == b"existing warning archive"
    assert sidecar.read_bytes() == b"existing warning sidecar"
