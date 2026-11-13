# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 17: product identity must survive analysis and guard VRE lookup."""

import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from census.analysis import analysis_frame, attach_replicates
from census import workflows, workflows_inference
from census.variance import REPLICATE_COLUMNS


def source(product=None, geoid="01001"):
    frame = pd.DataFrame({"GEOID": [geoid], "Year": [2023],
                          "B01001_001E": [1000.0], "B01001_001M": [164.5]})
    if product:
        frame.attrs.update(product=product, dataset="acs1" if product == "1-year" else "acs5")
    return frame


def write_source(tmp_path, product="1-year", fmt="csv", name="B01001_county_panel_2023"):
    path = tmp_path / f"{name}.{fmt}"
    frame = source(product)
    if fmt == "parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)
    # CSV has no attrs; metadata must explicitly name this output, not merely
    # happen to exist in the same directory.
    meta = {"product": product, "dataset": frame.attrs["dataset"],
            "output_files": {path.name: "fixture-output"}}
    (tmp_path / f"{name}_extraction_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


@pytest.mark.parametrize("product,prefix", [("1-year", "acs1"), ("5-year", "acs5")])
def test_source_attrs_determine_release_and_survive_analysis(product, prefix):
    frame = analysis_frame(source(product))
    assert frame["release"].tolist() == [f"{prefix}_2023"]
    assert frame.attrs["product"] == product
    assert frame["B01001_001_se"].iloc[0] == pytest.approx(100.0)


def test_explicit_release_and_legacy_default():
    assert analysis_frame(source(), release="custom-study")["release"].tolist() == ["custom-study"]
    assert analysis_frame(source(), release="acs1_2023")["release"].tolist() == ["acs1_2023"]
    assert analysis_frame(source())["release"].tolist() == ["acs5_2023"]
    with pytest.raises(ValueError, match="1-year.*5-year|5-year.*1-year"):
        analysis_frame(source("1-year"), release="acs5_2023")


def test_release_column_is_preserved_and_cannot_conflict_with_product():
    raw = source()
    raw["release"] = "acs1_2023"
    out = analysis_frame(raw)
    assert out["release"].tolist() == ["acs1_2023"]
    assert out.attrs["product"] == "1-year"
    raw.attrs["product"] = "5-year"
    with pytest.raises(ValueError):
        analysis_frame(raw)


def test_dataset_alone_establishes_product():
    raw = source()
    raw.attrs["dataset"] = "acs1/profile"
    assert analysis_frame(raw)["release"].tolist() == ["acs1_2023"]


def test_different_declared_products_are_not_stacked_or_joined(tmp_path):
    with pytest.raises(ValueError):
        analysis_frame([source("1-year"), source("5-year", "01003")])
    first = write_source(tmp_path, "1-year", name="one")
    second = write_source(tmp_path, "5-year", name="five")
    with pytest.raises(ValueError):
        workflows_inference._load_frames([str(first), str(second)])


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_file_product_recovered_for_library_and_workflow(tmp_path, fmt):
    path = write_source(tmp_path, fmt=fmt)
    frame = analysis_frame(str(path))
    assert frame["release"].tolist() == ["acs1_2023"]
    output = tmp_path / "analysis.parquet"
    result = workflows_inference.build_analysis_frame(str(path), output_path=str(output))
    assert result["success"], result
    saved = pd.read_parquet(output)
    assert saved["release"].tolist() == ["acs1_2023"]
    assert saved.attrs["product"] == "1-year"
    assert saved["GEOID"].tolist() == ["01001"]


def test_metadata_must_list_output_and_parquet_cannot_disagree(tmp_path):
    path = write_source(tmp_path, fmt="parquet")
    conflicting = source("5-year")
    conflicting.to_parquet(path, index=False)
    with pytest.raises(ValueError):
        analysis_frame(str(path))
    # An unrelated sidecar must not relabel an otherwise legacy CSV.
    legacy = tmp_path / "unrelated.csv"
    source().to_csv(legacy, index=False)
    assert analysis_frame(str(legacy))["release"].tolist() == ["acs5_2023"]


def test_conflicting_associated_sidecars_are_not_resolved_by_file_order(tmp_path):
    path = write_source(tmp_path)
    other = {"product": "5-year", "dataset": "acs5", "output_files": {path.name: "fixture-output"}}
    (tmp_path / "other_extraction_metadata.json").write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(ValueError):
        analysis_frame(str(path))


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_one_year_workflow_refuses_vre_before_fetch_or_output(tmp_path, fmt):
    path = write_source(tmp_path, fmt=fmt)
    output = tmp_path / "must-not-exist.parquet"
    with patch.object(workflows, "fetch_vre", side_effect=AssertionError("must not fetch")) as fetch:
        result = workflows_inference.build_analysis_frame(
            str(path), use_replicates=True, table_code="B01001", year=2023, output_path=str(output))
    assert not result["success"]
    assert "1-year" in result["error"]
    fetch.assert_not_called()
    assert not output.exists()


def test_join_preserves_product_for_distinct_tables_and_blocks_one_year_vre(tmp_path):
    first = write_source(tmp_path, name="first")
    second = write_source(tmp_path, name="second")
    raw = source("1-year").rename(columns={"B01001_001E": "B11001_001E", "B01001_001M": "B11001_001M"})
    raw.to_csv(second, index=False)
    joined = workflows_inference._load_frames([str(first), str(second)])
    assert joined.attrs["product"] == "1-year"
    assert set(joined.columns) >= {"B01001_001E", "B11001_001E"}
    with patch.object(workflows, "fetch_vre") as fetch, pytest.raises(ValueError, match="1-year"):
        workflows._replicates_for("B01001", 2023, joined, ["01001"])
    fetch.assert_not_called()


def reps(estimate=1000.0):
    values = estimate + np.tile([-1.0, 1.0], 40)
    return pd.DataFrame([{"GEOID": "01001", "Year": 2023, "variable": "B01001_001",
                          "estimate": estimate, **dict(zip(REPLICATE_COLUMNS, values))}])


def test_direct_attach_cannot_bypass_product_check():
    with pytest.raises(ValueError, match="1-year"):
        attach_replicates(analysis_frame(source("1-year")), reps())


def test_matching_five_year_replicates_and_rounding_tolerance():
    frame = analysis_frame(source("5-year"))
    attached, long = attach_replicates(frame, reps(1000.4))
    # Independent 4/80 formula: each replicate differs by +/-1 from its own
    # central estimate, hence variance 4 and SE 2. Rounding is not a mismatch.
    assert attached["B01001_001_se"].iloc[0] == pytest.approx(2.0)
    assert attached["release"].tolist() == ["acs5_2023"]
    assert len(long) == 1
    with pytest.raises(ValueError, match="product, release"):
        attach_replicates(frame, reps(1200.0))


def test_valid_five_year_workflow_fetches_once(tmp_path):
    path = write_source(tmp_path, "5-year")
    with patch.object(workflows, "fetch_vre", return_value=reps()) as fetch:
        result = workflows_inference.build_analysis_frame(
            str(path), use_replicates=True, table_code="B01001", year=2023,
            output_path=str(tmp_path / "five-analysis.parquet"))
    assert result["success"], result
    fetch.assert_called_once_with("B01001", 2023, "county")
