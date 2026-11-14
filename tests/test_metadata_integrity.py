# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 18: consumed bytes, source isolation and failure before extraction."""

import gzip
import hashlib
import io
import json
import os
import queue
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from census import metadata as md
from census import pipeline
from census.api import CensusAPIClient
from census.data_integrity import integrity_error_result


VARIABLES = json.dumps(
    {
        "variables": {
            "B01003_001E": {
                "label": "Estimate!!Total population",
                "group": "B01003",
                "concept": "TOTAL POPULATION",
                "predicateType": "int",
            }
        }
    }
).encode()
GROUPS = json.dumps(
    {"groups": [{"name": "B01003", "description": "Population", "universe": "Total population"}]}
).encode()


def write_archive(root, variables=VARIABLES, groups=GROUPS):
    folder = root / "acs5" / "2023"
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, raw in (("variables", variables), ("groups", groups)):
        if raw is not None:
            (folder / f"{name}.json.gz").write_bytes(gzip.compress(raw))
            files[f"acs5/2023/{name}.json.gz"] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "url": f"https://example.invalid/{name}",
            }
    (root / "manifest.json").write_text(json.dumps({"files": files, "vintages": {"acs5": [2023]}}))


@pytest.fixture
def archive(tmp_path, monkeypatch):
    write_archive(tmp_path)
    monkeypatch.delenv("CENSUS_METADATA_DIR", raising=False)
    monkeypatch.setattr(md, "get_resource_path", lambda _: str(tmp_path))
    md.clear_cache()
    yield tmp_path
    md.clear_cache()


def corrupt(root, name):
    path = root / "acs5" / "2023" / f"{name}.json.gz"
    raw = VARIABLES if name == "variables" else GROUPS
    path.write_bytes(gzip.compress(raw.replace(b"population", b"adult population")))


def test_valid_public_lookups_and_actual_provenance(archive):
    variables, source = md.table_variables("B01003", "acs5", 2023)
    assert source == "archive"
    assert variables["B01003_001E"]["label"] == "Estimate!!Total population"
    assert md.vintage_groups("acs5", 2023)[0]["B01003"]["universe"] == "Total population"
    for name, raw in (("variables", VARIABLES), ("groups", GROUPS)):
        info = md.archive_provenance("acs5", 2023, f"{name}.json")
        assert info["sha256"] == hashlib.sha256(raw).hexdigest()
        assert info["integrity"] == "verified"


@pytest.mark.parametrize("name", ["variables", "groups"])
def test_plausible_tamper_stops_public_consumers_and_fallback(archive, name):
    corrupt(archive, name)
    client = Mock(base_url="https://example.invalid")
    consumer = md.vintage_variables if name == "variables" else md.vintage_groups
    with pytest.raises(md.MetadataIntegrityError, match="expected SHA-256"):
        consumer("acs5", 2023, client)
    client._get_json.assert_not_called()
    assert not md.verify_archive_file("acs5", 2023, f"{name}.json")


@pytest.mark.parametrize("case", ["missing_checksum", "missing_manifest", "gzip", "json", "schema"])
def test_present_unusable_bundled_archive_is_not_missing(archive, case):
    path = archive / "acs5/2023/variables.json.gz"
    if case == "missing_checksum":
        (archive / "manifest.json").write_text('{"files":{}}')
    elif case == "missing_manifest":
        (archive / "manifest.json").unlink()
    elif case == "gzip":
        path.write_bytes(b"not gzip")
    else:
        write_archive(archive, variables=b"{" if case == "json" else b"[]")
    with pytest.raises(md.MetadataIntegrityError):
        md.vintage_variables("acs5", 2023)


def test_missing_file_retains_api_and_missing_behavior(archive):
    (archive / "acs5/2023/variables.json.gz").unlink()
    assert md.vintage_variables("acs5", 2023) == ({}, "missing")
    client = Mock(base_url="https://example.invalid")
    client._get_json.return_value = json.loads(VARIABLES)
    assert md.vintage_variables("acs5", 2023, client)[1] == "api"
    client._get_json.assert_called_once()


def test_recompressed_same_json_and_once_per_cached_source(archive):
    path = archive / "acs5/2023/variables.json.gz"
    path.write_bytes(gzip.compress(VARIABLES, compresslevel=1, mtime=123))
    with patch.object(md.gzip, "open", wraps=gzip.open) as opened:
        md.vintage_variables("acs5", 2023)
        assert md.verify_archive_file("acs5", 2023)
        md.archive_provenance("acs5", 2023)
    assert opened.call_count == 1


@pytest.mark.parametrize("manifest", [True, False])
def test_custom_source_records_consumed_hash_without_official_claim(archive, tmp_path, monkeypatch, manifest):
    custom = archive / "custom"
    changed = VARIABLES.replace(b"population", b"adult population")
    write_archive(custom, variables=changed)
    if not manifest:
        (custom / "manifest.json").unlink()
    monkeypatch.setenv("CENSUS_METADATA_DIR", str(custom))
    assert "adult population" in md.vintage_variables("acs5", 2023)[0]["B01003_001E"]["label"]
    info = md.archive_provenance("acs5", 2023)
    assert info["sha256"] == hashlib.sha256(changed).hexdigest()
    assert info["integrity"] == "caller_supplied_unverified"
    # A matching caller manifest remains unverified; a stale one does too.
    if manifest:
        path = custom / "acs5/2023/variables.json.gz"
        path.write_bytes(gzip.compress(changed.replace(b"adult", b"elder")))
        md.clear_cache()
        info = md.archive_provenance("acs5", 2023)
        assert info["sha256"] != info["expected_sha256"]
        assert info["integrity"] == "caller_supplied_unverified"


def test_root_switch_and_resolved_bundled_alias(archive, monkeypatch):
    assert md.vintage_variables("acs5", 2023)[0]["B01003_001E"]["label"].endswith("Total population")
    custom = archive / "other"
    write_archive(custom, variables=VARIABLES.replace(b"population", b"adult population"))
    monkeypatch.setenv("CENSUS_METADATA_DIR", str(custom))
    assert "adult population" in md.vintage_variables("acs5", 2023)[0]["B01003_001E"]["label"]
    assert (
        md.load_manifest()["files"]["acs5/2023/variables.json.gz"]["sha256"]
        == hashlib.sha256(VARIABLES.replace(b"population", b"adult population")).hexdigest()
    )
    monkeypatch.setenv("CENSUS_METADATA_DIR", str(archive / "other" / ".."))
    assert md.archive_provenance("acs5", 2023)["integrity"] == "verified"


def test_failure_repair_and_explicit_refresh(archive):
    corrupt(archive, "variables")
    with pytest.raises(md.MetadataIntegrityError):
        md.vintage_variables("acs5", 2023)
    write_archive(archive)
    assert md.verify_archive_file("acs5", 2023)  # failed reads are not cached
    corrupt(archive, "variables")
    assert md.verify_archive_file("acs5", 2023)  # successful snapshot remains cached
    md.clear_cache()
    assert not md.verify_archive_file("acs5", 2023)


@pytest.mark.parametrize("name", ["variables", "groups"])
def test_extraction_cli_mcp_and_gui_report_same_failure_without_outputs(archive, name):
    from census.__main__ import main
    import mcp_server

    corrupt(archive, name)
    output = archive / "output"
    entity = pipeline.Entity("Alabama", "state", state_code="01")
    with patch.object(CensusAPIClient, "_get_json") as api:
        result = pipeline.run_batch("B01003", [entity], [2023], str(output))
        with redirect_stdout(io.StringIO()) as stream:
            code = main(
                [
                    "extract",
                    "--table",
                    "B01003",
                    "--years",
                    "2023",
                    "--state",
                    "01",
                    "--out",
                    str(output),
                    "--no-cache",
                    "--json",
                ]
            )
        cli = json.loads(stream.getvalue())
        mcp = (
            mcp_server.list_variables("B01003", dataset="acs5", year=2023)
            if name == "variables"
            else (mcp_server.describe_variable("B01003_001E", dataset="acs5", year=2023))
        )
        api.assert_not_called()
    assert code == 4
    for item in (result, cli, mcp):
        assert item["success"] is False
        assert item["error_code"] == "BUNDLED_DATA_INTEGRITY"
        assert "metadata" in item["next_step"]
        assert "price" not in item["next_step"]
    assert not output.exists()
    # Exercise the actual GUI worker without creating a Tk display.
    import acs_downloader

    worker = SimpleNamespace(msg_queue=queue.Queue())
    with patch.object(CensusAPIClient, "_get_json") as api:
        acs_downloader.ACSDownloaderApp._download_worker(
            worker, "B01003", "acs5", [entity], [2023], str(output), "panel", pipeline.ResearchOptions()
        )
        api.assert_not_called()
    assert worker.msg_queue.get_nowait()[0] == "ERROR"
    assert not output.exists()


def test_manifest_corruption_stops_before_current_run_artifacts(archive):
    from census.manifest import run_manifest

    # The established stored-input reference manifest, with a corrupt consumed source.
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "examples/manifests/income_population_tracts.json").read_text())
    year = int(manifest["releases"][0])
    folder = archive / "acs5" / str(year)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "variables.json.gz").write_bytes(gzip.compress(VARIABLES))
    with patch.object(CensusAPIClient, "_get_json") as api:
        result = run_manifest(manifest, archive / "run")
        api.assert_not_called()
    assert result["error_code"] == "BUNDLED_DATA_INTEGRITY"
    assert not (archive / "run").exists()


def test_repair_guidance_and_replay_detect_status_and_groups():
    result = integrity_error_result(md.MetadataIntegrityError("bad metadata"))
    assert "clear_cache" in result["next_step"]
    old = {
        "concordance": {
            "metadata_vintages": {
                "2023": {
                    "source": "archive",
                    "sha256": "same",
                    "integrity": "verified",
                    "groups": {"sha256": "a"},
                }
            }
        }
    }
    new = json.loads(json.dumps(old))
    new["concordance"]["metadata_vintages"]["2023"]["groups"]["sha256"] = "b"
    assert pipeline._provenance_differences(old, new)

    new = json.loads(json.dumps(old))
    new["concordance"]["metadata_vintages"]["2023"]["integrity"] = "caller_supplied_unverified"
    assert pipeline._provenance_differences(old, new)


@pytest.mark.parametrize(
    "loader,name",
    [
        ("load_variable_definitions", "variables"),
        ("load_variable_metadata", "variables"),
        ("load_table_universes", "groups"),
        ("load_table_catalog", "groups"),
    ],
)
def test_legacy_loaders_enforce_same_contract(archive, loader, name):
    from census import tables

    corrupt(archive, name)
    with pytest.raises(md.MetadataIntegrityError):
        getattr(tables, loader)()


def test_legacy_cache_tracks_root_and_clears_after_repair(archive, monkeypatch):
    from census import tables

    assert "adult" not in tables.load_variable_metadata()["B01003_001E"]["label"]
    custom = archive / "custom"
    write_archive(custom, variables=VARIABLES.replace(b"population", b"adult population"))
    monkeypatch.setenv("CENSUS_METADATA_DIR", str(custom))
    assert "adult" in tables.load_variable_metadata()["B01003_001E"]["label"]
    monkeypatch.delenv("CENSUS_METADATA_DIR")
    corrupt(archive, "variables")
    md.clear_cache()
    with pytest.raises(md.MetadataIntegrityError):
        tables.load_variable_definitions()


def test_workflow_adapter_retains_metadata_failure(archive):
    from census import workflows_inference

    result = integrity_error_result(md.MetadataIntegrityError("bad metadata"))
    with patch("census.manifest.run_manifest", return_value=result):
        out = workflows_inference.run_manifest("unused.json", str(archive / "run"))
    assert out == result


def test_direct_mcp_fetch_checks_before_any_data_request(archive):
    import mcp_server

    corrupt(archive, "variables")
    with patch.object(CensusAPIClient, "_get_json") as api:
        result = mcp_server.fetch_table_data(2023, "01", "B01003", geo_level="state")
        api.assert_not_called()
    assert result["error_code"] == "BUNDLED_DATA_INTEGRITY"


def test_malformed_manifest_is_explicit_and_repairable(archive):
    (archive / "manifest.json").write_text("{")
    with pytest.raises(md.MetadataIntegrityError, match="manifest.*malformed"):
        md.vintage_groups("acs5", 2023)
    write_archive(archive)
    assert md.vintage_groups("acs5", 2023)[0]["B01003"]["universe"] == "Total population"


def test_normalized_cache_cannot_outlive_different_consumed_bytes(archive, monkeypatch):
    custom = archive / "custom"
    write_archive(custom)
    monkeypatch.setenv("CENSUS_METADATA_DIR", str(custom))
    assert "adult" not in md.vintage_variables("acs5", 2023)[0]["B01003_001E"]["label"]
    corrupt(custom, "variables")
    # Simulate byte-cache eviction while the separately bounded normalized cache survives.
    md._archive_at.cache_clear()
    assert "adult" in md.vintage_variables("acs5", 2023)[0]["B01003_001E"]["label"]
    info = pipeline._vintage_provenance(SimpleNamespace(dataset="acs5", sources={2023: "archive"}))["2023"]
    changed = VARIABLES.replace(b"population", b"adult population")
    assert info["sha256"] == hashlib.sha256(changed).hexdigest()
    assert info["sha256"] != info["expected_sha256"]
    assert info["integrity"] == "caller_supplied_unverified"
    assert info["groups"]["sha256"] == hashlib.sha256(GROUPS).hexdigest()


def test_api_metadata_memo_does_not_cross_client_endpoints(archive):
    (archive / "acs5/2023/variables.json.gz").unlink()
    first = Mock(base_url="https://one.invalid")
    first._get_json.return_value = json.loads(VARIABLES)
    second = Mock(base_url="https://two.invalid")
    second._get_json.return_value = json.loads(VARIABLES.replace(b"population", b"adult population"))
    md.vintage_variables("acs5", 2023, first)
    result, source = md.vintage_variables("acs5", 2023, second)
    assert source == "api"
    assert "adult" in result["B01003_001E"]["label"]
    second._get_json.assert_called_once()


def test_actual_mcp_error_schema_retains_typed_failure(archive):
    import mcp_server
    from tests.test_reference_workflow import _mcp_call

    corrupt(archive, "groups")
    result = _mcp_call(mcp_server, "search_tables", {"query": "population", "dataset": "acs5", "year": 2023})
    assert result["error_code"] == "BUNDLED_DATA_INTEGRITY"


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks requires optional Windows privileges")
def test_linked_bundled_payload_cannot_bypass_legacy_verification(archive):
    from census import tables

    target = archive / "changed.gz"
    target.write_bytes(gzip.compress(VARIABLES.replace(b"population", b"adult population")))
    path = archive / "acs5/2023/variables.json.gz"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(md.MetadataIntegrityError):
        tables.load_variable_metadata()
