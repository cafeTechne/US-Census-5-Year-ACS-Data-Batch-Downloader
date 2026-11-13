# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 17 I2: a bundle contains one run and never adopts stale artifacts."""

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from census import report_tables
from census import workflows_inference
from census.manifest import load_manifest, run_manifest
from tests.test_manifest import EXAMPLE, FakeClient


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in folder.rglob("*") if p.is_file()}


def test_reduced_bundle_cannot_adopt_prior_results(tmp_path):
    folder = tmp_path / "bundle"
    report_tables.results_bundle(folder, results={"a": {"estimate": 1}, "b": {"estimate": 2}},
                                 methods="Original methods")
    before = snapshot(folder)
    with pytest.raises(ValueError, match="fresh or empty"):
        report_tables.results_bundle(folder, results={"a": {"estimate": 3}})
    assert snapshot(folder) == before


@pytest.mark.parametrize("relative", ["notes.txt", "results/old.json", "figures/old.svg", "methods.md"])
def test_nonempty_destination_is_preserved(tmp_path, relative):
    folder = tmp_path / "bundle"
    old = folder / relative
    old.parent.mkdir(parents=True)
    old.write_bytes(b"do not change")
    before = snapshot(folder)
    with pytest.raises(ValueError, match="fresh or empty"):
        report_tables.results_bundle(folder, results={"new": {"estimate": 1}})
    assert snapshot(folder) == before


def test_manifest_success_then_blocked_reuse_is_refused_before_execution(tmp_path):
    folder = tmp_path / "run"
    result = run_manifest(load_manifest(EXAMPLE), folder, client=FakeClient(), use_cache=False)
    assert result["success"], result.get("errors")
    before = snapshot(folder)
    blocked_client = FakeClient(fail_tables={"B01003"})
    with mock.patch.object(blocked_client, "fetch", wraps=blocked_client.fetch) as fetch:
        blocked = run_manifest(load_manifest(EXAMPLE), folder, client=blocked_client, use_cache=False)
    assert not blocked["success"]
    assert any("fresh or empty" in error for error in blocked["errors"])
    fetch.assert_not_called()
    assert snapshot(folder) == before


def test_workflow_refuses_reuse_before_copying_figures(tmp_path):
    folder = tmp_path / "bundle"
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"success": True, "estimate": 1}), encoding="utf-8")
    first = workflows_inference.make_results_bundle(str(folder), {"fit": str(result)})
    assert first["success"], first
    before = snapshot(folder)
    figure = tmp_path / "new.svg"
    figure.write_text("new figure", encoding="utf-8")
    second = workflows_inference.make_results_bundle(str(folder), {"fit": str(result)},
                                                     figure_paths=[str(figure)])
    assert not second["success"]
    assert second["error_code"] == "INVALID_INPUT"
    assert "fresh or empty" in second["error"]
    assert snapshot(folder) == before


def test_workflow_copies_only_current_figures_and_hashes_them(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    paths = []
    for name, value in (("one", "first"), ("two", "second")):
        path = tmp_path / name / "plot.svg"
        path.parent.mkdir()
        path.write_text(value, encoding="utf-8")
        paths.append(str(path))
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"success": True, "estimate": 1}), encoding="utf-8")
    out = workflows_inference.make_results_bundle(str(folder), {"fit": str(result)}, figure_paths=paths)
    assert out["success"], out
    bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    for source, name in zip(paths, ("one_plot.svg", "two_plot.svg")):
        expected = Path(source).read_bytes()
        assert (folder / "figures" / name).read_bytes() == expected
        assert bundle["files"][f"figures/{name}"] == hashlib.sha256(expected).hexdigest()
    assert not (folder / "captions.md").exists()


@pytest.mark.parametrize("category", ["results", "tables", "figures"])
@pytest.mark.parametrize("name", ["../../outside", "/absolute", "..\\outside", "C:outside",
                                  "", ".", "..", "NUL", "con.txt", "LPT¹", "trailing.",
                                  "trailing ", "line\nbreak", "stream:payload", "bad\x00name", "bad\ud800name"])
def test_artifact_names_are_rejected_before_any_write(tmp_path, category, name):
    folder = tmp_path / "bundle"
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"prior research")
    # The valid result comes first: preflight must also catch later bad keys
    # before creating even the destination or writing a partial first result.
    kwargs = {"results": {"valid": {"estimate": 1}}, category: {"valid": {}, name: {}}}
    with pytest.raises(ValueError, match="portable filename component"):
        report_tables.results_bundle(folder, **kwargs)
    assert not folder.exists()
    assert outside.read_bytes() == b"prior research"


@pytest.mark.parametrize("category", ["results", "tables", "figures"])
def test_case_collisions_are_refused_before_any_write(tmp_path, category):
    folder = tmp_path / "bundle"
    with pytest.raises(ValueError, match="case-insensitive"):
        report_tables.results_bundle(folder, **{category: {"Model": {}, "model": {}}})
    assert not folder.exists()


def test_valid_labels_and_same_label_in_distinct_categories_remain_supported(tmp_path):
    import pandas as pd

    table = report_tables.ReportTable("A table", pd.DataFrame({"x": [1]}), pd.DataFrame({"x": [1]}))
    folder = tmp_path / "bundle"
    bundle = report_tables.results_bundle(folder, tables={"Model 1": table},
                                         results={"Model 1": {"estimate": 1}, "résumé_β-2": {"estimate": 2}})
    assert set(bundle["files"]) == {"tables/Model 1.tex", "tables/Model 1.md", "tables/Model 1.csv",
                                    "results/Model 1.json", "results/résumé_β-2.json"}


def test_direct_table_write_cannot_escape_its_folder(tmp_path):
    import pandas as pd

    table = report_tables.ReportTable("A table", pd.DataFrame({"x": [1]}), pd.DataFrame({"x": [1]}))
    folder = tmp_path / "tables"
    with pytest.raises(ValueError, match="portable filename component"):
        table.write(folder, "../outside")
    assert not folder.exists()


def test_exploratory_workflow_returns_invalid_input_without_overwriting(tmp_path):
    result = tmp_path / "input.json"
    result.write_text('{"success":true,"estimate":99}', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"prior research")
    folder = tmp_path / "bundle"
    response = workflows_inference.make_results_bundle(str(folder), {"../../outside": str(result)})
    assert not response["success"] and response["error_code"] == "INVALID_INPUT"
    assert not folder.exists()
    assert outside.read_bytes() == b"prior research"


def figure_output(name, paths):
    import pandas as pd
    from census.figures import FigureOutput

    return FigureOutput(name=name, paths=paths, values=pd.DataFrame(), caption="", provenance={})


def test_nested_bundle_named_payloads_are_in_hash_inventory(tmp_path):
    source = tmp_path / "bundle.json"
    source.write_text('{"source":"figure metadata"}', encoding="utf-8")
    root = tmp_path / "output"
    bundle = report_tables.results_bundle(
        root, results={"bundle": {"estimate": 99}},
        figures={"bundle.json": figure_output("bundle.json", {"provenance": str(source)})},
    )
    assert set(bundle["files"]) == {"results/bundle.json", "figures/bundle.json"}
    for relative, digest in bundle["files"].items():
        assert digest == hashlib.sha256((root / relative).read_bytes()).hexdigest()
    assert "bundle.json" not in bundle["files"]
    assert json.loads((root / "bundle.json").read_text())["files"] == bundle["files"]


@pytest.mark.parametrize("second_name", ["plot.svg", "PLOT.SVG"])
def test_distinct_figure_labels_cannot_overwrite_one_destination(tmp_path, second_name):
    first, second = tmp_path / "first.svg", tmp_path / "second.svg"
    first.write_bytes(b"first image")
    second.write_bytes(b"second image")
    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="destination filename collision"):
        report_tables.results_bundle(root, figures={
            "plot": figure_output("first", {"svg": str(first)}),
            second_name: figure_output("second.svg", {"file": str(second)}),
        })
    assert not root.exists()
    assert first.read_bytes() == b"first image" and second.read_bytes() == b"second image"


@pytest.mark.parametrize("render_first", [True, False])
def test_rendered_and_copied_collisions_leave_no_bundle_or_scratch(tmp_path, render_first):
    copied = tmp_path / "source.svg"
    copied.write_bytes(b"copied image")
    staging = []

    def render(*, out_dir, name):
        staging.append(Path(out_dir))
        path = Path(out_dir) / f"{name}.svg"
        path.write_bytes(b"rendered image")
        return figure_output(name, {"svg": str(path)})

    entries = [("plot", (render, {})),
               ("plot.svg", figure_output("source.svg", {"file": str(copied)}))]
    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="destination filename collision"):
        report_tables.results_bundle(root, figures=dict(entries if render_first else reversed(entries)))
    assert not root.exists() and copied.read_bytes() == b"copied image"
    assert staging and all(not p.exists() for p in staging)


def test_mismatched_figure_prefix_is_rejected_without_silent_rename(tmp_path):
    source = tmp_path / "actual.svg"
    source.write_bytes(b"image")
    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="does not match source name"):
        report_tables.results_bundle(root, figures={"plot": figure_output("wrong", {"svg": str(source)})})
    assert not root.exists() and source.read_bytes() == b"image"


def test_two_paths_with_same_derived_filename_cannot_overwrite(tmp_path):
    paths = {}
    for directory in ("one", "two"):
        path = tmp_path / directory / "plot.svg"
        path.parent.mkdir()
        path.write_text(directory)
        paths[directory] = str(path)
    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="destination filename collision"):
        report_tables.results_bundle(root, figures={"plot": figure_output("plot", paths)})
    assert not root.exists()


def test_renderer_failure_cleans_scratch_and_leaves_destination_absent(tmp_path):
    staging = []

    def render(*, out_dir, name):
        staging.append(Path(out_dir))
        (Path(out_dir) / f"{name}.svg").write_bytes(b"partial rendering")
        raise ValueError("test renderer failed")

    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="test renderer failed"):
        report_tables.results_bundle(root, figures={"plot": (render, {})})
    assert not root.exists()
    assert staging and all(not p.exists() for p in staging)
