# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local-model evaluation harness (PLAN_ROUND_8 J4).

Launches ``python mcp_server.py --profile core`` as an MCP stdio server, drives
an OpenAI-compatible chat endpoint (Ollama, LM Studio, llama.cpp, vLLM) with the
server's tools, and scores each scripted task on tool choice, argument
correctness, and the final answer. Tasks need no Census API calls; expected
answers are computed by calling the library directly.

    python scripts/local_model_eval.py --model qwen3:8b
    python scripts/local_model_eval.py --base-url http://localhost:1234/v1 --model <name>
    python scripts/local_model_eval.py --model qwen3:8b llama3.1:8b   # several models, one report
    python scripts/local_model_eval.py --offline-check   # verify expectations only

Map and spatial tasks (Round 10 F1) use the offline DC tract boundary fixture
(``tests/fixtures/geo``, passed to the server as ``CENSUS_GEO_DIR``) and a
synthetic frame written to ``.cache/local_model_eval/``; the spatial task runs
against the ``core_spatial`` profile (core + ``spatial_autocorrelation``).

Writes docs/audits/local_model_eval.md (or --out). Not part of CI.
"""

import argparse
import asyncio
import json
import os
import platform
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURE_GEO = os.path.join(ROOT, "tests", "fixtures", "geo")
EVAL_DIR = os.path.join(ROOT, ".cache", "local_model_eval")

SYSTEM_PROMPT = (
    "You are a research assistant using US Census ACS tools. Use the tools to compute "
    "answers; never compute statistics yourself. Report estimates with their 90% margin "
    "of error and the numbers the tools return. Be brief."
)


def _numbers(text: str) -> List[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text)]


def _has_number(text: str, value: float, tol: float) -> bool:
    return any(abs(n - value) <= tol for n in _numbers(text))


def _close(a: Any, b: float, tol: float = 1e-6) -> bool:
    try:
        return abs(float(a) - b) <= tol
    except (TypeError, ValueError):
        return False


@dataclass
class Task:
    name: str
    prompt: str
    tool: str
    check_args: Callable[[Dict[str, Any]], bool]
    check_answer: Callable[[str], bool]
    expected: str
    offline: Callable[[], bool] = lambda: True
    profile: Optional[str] = None  # None: the --profile argument


def _geo_fixture() -> str:
    """DC tract frame (ACS 2023 boundaries from the offline fixture) with a
    north-south gradient, so Moran's I is clearly positive. Deterministic."""
    import numpy as np
    import pandas as pd

    os.environ.setdefault("CENSUS_GEO_DIR", FIXTURE_GEO)
    from census import boundaries

    b = boundaries.get_boundaries("tract", 2023, state="11")
    c = b.geometry.to_crs(5070).centroid
    rng = np.random.default_rng(10)
    north = (c.y - c.y.min()) / (c.y.max() - c.y.min())
    rate = 8 + 20 * north.to_numpy() + rng.normal(0, 3, len(b))
    df = pd.DataFrame({"GEOID": b["GEOID"].astype(str), "poverty_rate": rate.round(1),
                       "poverty_rate_se": (rate * rng.uniform(0.08, 0.35, len(b))).round(2)})
    os.makedirs(EVAL_DIR, exist_ok=True)
    path = os.path.join(EVAL_DIR, "dc_tracts_2023.csv").replace("\\", "/")
    df.to_csv(path, index=False)
    return path


def _same_path(a: Any, b: str) -> bool:
    return os.path.normcase(os.path.abspath(str(a or ""))) == os.path.normcase(os.path.abspath(b))


def _tasks() -> List[Task]:
    import mcp_server as srv

    rate = srv.derive_estimate("proportion", [12000, 80000], [900, 1500], as_percent=True)
    sig = srv.calculate_significance(14.2, 1.1, 11.9, 1.0)
    trend = srv.trend_test([2013, 2018, 2023], [14.1, 13.0, 11.8], [0.4, 0.4, 0.3])
    mdd = srv.minimum_detectable_difference(2.1, 2.4)
    overlap = srv.trend_test([2021, 2022, 2023], [14.1, 13.0, 11.8], [0.4, 0.4, 0.3])

    def lower(t: str) -> str:
        return t.lower()

    from census import workflows_geo

    geo = _geo_fixture()
    map_dir = os.path.join(EVAL_DIR, "expected_map")
    expected_map = workflows_geo.make_map(geo, "poverty_rate", "tract", 2023, se_col="poverty_rate_se",
                                          output_dir=map_dir)
    sep_match = re.search(r"class separability (\d+(?:\.\d+)?)%", expected_map.get("caption", ""))
    separability = float(sep_match.group(1)) if sep_match else float("nan")
    moran = workflows_geo.spatial_autocorrelation(geo, "poverty_rate", "tract", 2023)
    moran_i = float(moran["global"]["I"]) if moran.get("success") else float("nan")

    def geo_args(a: Dict[str, Any]) -> bool:
        return (_same_path(a.get("input_path"), geo) and a.get("column") == "poverty_rate"
                and str(a.get("level", "")).lower() == "tract" and int(a.get("year", 0)) == 2023)

    return [
        Task(
            "poverty_rate_with_moe",
            "In Hays County, 12,000 of the 80,000 people for whom poverty status is determined "
            "are below poverty (MOE 900 and 1,500 respectively). What is the poverty rate in "
            "percent and its 90% MOE?",
            "derive_estimate",
            lambda a: str(a.get("operation", "")).lower() == "proportion"
            and [float(x) for x in a.get("estimates", [])] == [12000.0, 80000.0]
            and [float(x) for x in a.get("moes", [])] == [900.0, 1500.0],
            lambda t: _has_number(t, rate["estimate"], 0.05) and _has_number(t, rate["moe"], 0.06),
            f"{rate['estimate']:.1f}% +/- {rate['moe']:.2f}",
            lambda: _close(rate["estimate"], 15.0),
        ),
        Task(
            "two_county_significance",
            "The 2023 ACS poverty rate is 14.2% (MOE 1.1) in County A and 11.9% (MOE 1.0) in "
            "County B. Is the difference statistically significant at 90% confidence?",
            "calculate_significance",
            lambda a: {round(float(a.get("est1", 0)), 2), round(float(a.get("est2", 0)), 2)} == {14.2, 11.9}
            and {round(float(a.get("moe1", 0)), 2), round(float(a.get("moe2", 0)), 2)} == {1.1, 1.0}
            and not (a.get("year1") and a.get("year2") and a.get("year1") != a.get("year2")),
            lambda t: "significant" in lower(t)
            and not re.search(r"\b(not|no|isn't|is not)\b[^.]{0,30}significant", lower(t)),
            f"yes, difference {sig['difference']:.1f}, p={sig['p_value']:.3f}",
            lambda: bool(sig["significant"]),
        ),
        Task(
            "trend_three_releases",
            "A county's poverty rate was 14.1% (MOE 0.4) in the 2013 ACS 5-year release, 13.0% "
            "(MOE 0.4) in 2018, and 11.8% (MOE 0.3) in 2023. Is there a significant linear trend? "
            "Give the slope per year.",
            "trend_test",
            lambda a: [int(y) for y in a.get("years", [])] == [2013, 2018, 2023]
            and [float(x) for x in a.get("estimates", [])] == [14.1, 13.0, 11.8]
            and [float(x) for x in a.get("moes", [])] == [0.4, 0.4, 0.3],
            lambda t: _has_number(t, trend["slope"], 0.006) and "significant" in lower(t)
            and not re.search(r"\b(not|no)\b[^.]{0,20}significant", lower(t)),
            f"slope {trend['slope']:.3f}/yr, significant",
            lambda: bool(trend["significant"]),
        ),
        Task(
            "overlapping_trend_refused",
            "Test for a linear trend in a tract's poverty rate using the 2021, 2022 and 2023 ACS "
            "5-year releases: 14.1% (MOE 0.4), 13.0% (MOE 0.4), 11.8% (MOE 0.3).",
            "trend_test",
            lambda a: [int(y) for y in a.get("years", [])] == [2021, 2022, 2023],
            lambda t: "overlap" in lower(t),
            "refused: releases overlap; use non-overlapping releases",
            lambda: overlap.get("success") is False,
        ),
        Task(
            "minimum_detectable_difference",
            "Two tract estimates have 90% MOEs of 2.1 and 2.4 percentage points. What is the "
            "smallest difference between them we could reliably detect (80% power)?",
            "minimum_detectable_difference",
            lambda a: {round(float(a.get("moe1", 0)), 2), round(float(a.get("moe2", a.get("moe1", 0))), 2)}
            == {2.1, 2.4},
            lambda t: _has_number(t, mdd["mdd"], 0.06),
            f"{mdd['mdd']:.2f} points",
            lambda: _close(mdd["mdd"], 4.82, 0.01),
        ),
        Task(
            "recipe_tract_trend",
            "Which tool workflow should I follow to test whether a tract's poverty rate changed "
            "over the last decade? List the steps.",
            "get_recipe",
            lambda a: a.get("name") in ("tract_trend", None),
            lambda t: "overlap" in lower(t) and "trend_test" in lower(t),
            "tract_trend recipe (non-overlapping releases, trend_test)",
        ),
        Task(
            "map_with_reliability",
            f"Make a map of poverty_rate for Washington DC census tracts (ACS 2023) from the file {geo}. "
            "Standard errors are in the column poverty_rate_se. Report the class separability.",
            "make_map",
            lambda a: geo_args(a) and a.get("se_col") == "poverty_rate_se"
            and str(a.get("kind", "estimate")) == "estimate",
            lambda t: any(_has_number(m.group(0), separability, 0.51)
                          for m in re.finditer(r".{0,60}separab.{0,60}", lower(t))),
            f"map written; class separability {separability:g}%",
            lambda: bool(expected_map.get("success")) and separability == separability,
        ),
        Task(
            "spatial_clustering",
            f"Is poverty_rate spatially clustered across Washington DC census tracts (ACS 2023)? "
            f"The data are in {geo}. Give Moran's I and say whether it is significant.",
            "spatial_autocorrelation",
            geo_args,
            lambda t: _has_number(t, moran_i, 0.006) and "significant" in lower(t)
            and not re.search(r"(not|no)[^.]{0,20}significant", lower(t)),
            f"Moran's I {moran_i:.3f}, significant (p_sim={moran.get('global', {}).get('p_sim')})",
            lambda: moran_i > 0.3 and moran["global"]["p_sim"] < 0.01,
            profile="core_spatial",
        ),
    ]


def _chat(base_url: str, model: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
          timeout: float) -> Dict[str, Any]:
    body = json.dumps({"model": model, "messages": messages, "tools": tools,
                       "temperature": 0, "max_tokens": 1500}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer local"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["choices"][0]["message"]


def _strip_think(text: Optional[str]) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


async def _run_task(session: Any, tools: List[Dict[str, Any]], task: Task, args: argparse.Namespace) -> Dict[str, Any]:
    system = SYSTEM_PROMPT + (" /no_think" if args.no_think else "")
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": task.prompt}]
    calls: List[Dict[str, Any]] = []
    answer, error = "", ""
    start = time.time()
    try:
        for _ in range(args.max_turns):
            msg = _chat(args.base_url, args.model, messages, tools, args.timeout)
            tool_calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": tool_calls} if tool_calls else {})})
            if not tool_calls:
                answer = _strip_think(msg.get("content"))
                break
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    targs = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    targs = {}
                calls.append({"tool": name, "args": targs})
                result = await session.call_tool(name, targs)
                text = "\n".join(getattr(c, "text", "") for c in result.content)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", name), "content": text[:3000]})
    except Exception as exc:  # network/timeouts are reported, not raised
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - start
    first = next((c for c in calls if c["tool"] == task.tool), None)
    tool_ok = first is not None
    args_ok = bool(first) and _safe(task.check_args, first["args"])
    answer_ok = bool(answer) and _safe(task.check_answer, answer)
    return {"task": task.name, "tool_ok": tool_ok, "args_ok": args_ok, "answer_ok": answer_ok,
            "passed": tool_ok and args_ok and answer_ok, "seconds": round(elapsed, 1),
            "calls": calls, "answer": answer, "error": error, "expected": task.expected}


def _safe(fn: Callable[[Any], bool], value: Any) -> bool:
    try:
        return bool(fn(value))
    except Exception:
        return False


async def _evaluate(args: argparse.Namespace, tasks: List[Task]) -> List[Dict[str, Any]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "CENSUS_GEO_DIR": os.environ.get("CENSUS_GEO_DIR", FIXTURE_GEO)}
    selected = [t for t in tasks if not args.only or t.name in args.only]
    by_name: Dict[str, Dict[str, Any]] = {}
    for profile in dict.fromkeys(t.profile or args.profile for t in selected):
        params = StdioServerParameters(command=sys.executable,
                                       args=[os.path.join(ROOT, "mcp_server.py"), "--profile", profile],
                                       cwd=ROOT, env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = (await session.list_tools()).tools
                tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                           "parameters": t.inputSchema}} for t in listed]
                print(f"[{args.model}] {len(tools)} tools from profile '{profile}'", flush=True)
                for task in selected:
                    if (task.profile or args.profile) != profile:
                        continue
                    res = await _run_task(session, tools, task, args)
                    res.update(profile=profile, n_tools=len(tools))
                    print(f"[{args.model}] {task.name}: {'PASS' if res['passed'] else 'FAIL'} "
                          f"({res['seconds']}s)", flush=True)
                    by_name[task.name] = res
    return [by_name[t.name] for t in selected if t.name in by_name]


def _write_report(path: str, args: argparse.Namespace, runs: Dict[str, List[Dict[str, Any]]],
                  n_tools: Dict[str, int]) -> None:
    yes = lambda b: "yes" if b else "no"  # noqa: E731
    models = list(runs)
    profiles = ", ".join(f"`{p}` ({n} tools)" for p, n in n_tools.items())
    lines = [
        "# Local-model evaluation (PLAN_ROUND_8 J4, Round 10 F1)",
        "",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- Models: {', '.join(f'`{m}`' for m in models)} via `{args.base_url}` (temperature 0"
        + (", Qwen `/no_think`" if args.no_think else "") + ")",
        f"- MCP server: `python mcp_server.py --profile <profile>`: {profiles}",
        f"- Hardware: {platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs, "
        f"{args.hardware_note}",
        "- Result: " + "; ".join(f"`{m}` **{sum(r['passed'] for r in rs)}/{len(rs)}**" for m, rs in runs.items())
        + " (pass = right tool, right arguments, and a final answer containing the expected result)",
        "",
        "Tasks need no Census API calls or network; expected answers come from calling the library "
        "directly. Map and spatial tasks use the offline DC tract boundary fixture.",
        "",
        "Cells read PASS/FAIL then tool/args/answer correctness.",
        "",
        "| Task | Profile | Expected | " + " | ".join(f"{m} | {m} (s)" for m in models) + " |",
        "| :--- | :--- | :--- | " + " | ".join(":--- | ---:" for _ in models) + " |",
    ]
    names = list(dict.fromkeys(r["task"] for rs in runs.values() for r in rs))
    for name in names:
        row = {m: next((r for r in rs if r["task"] == name), None) for m, rs in runs.items()}
        first = next(r for r in row.values() if r)
        cells = []
        for m in models:
            r = row[m]
            if r is None:
                cells.append("- | -")
                continue
            mark = "**PASS**" if r["passed"] else "FAIL"
            cells.append(f"{mark} {yes(r['tool_ok'])}/{yes(r['args_ok'])}/{yes(r['answer_ok'])} | {r['seconds']}")
        lines.append(f"| {name} | {first.get('profile', args.profile)} | {first['expected']} | "
                     + " | ".join(cells) + " |")
    lines += ["", "## Transcripts", ""]
    for m, rs in runs.items():
        for r in rs:
            lines.append(f"### {m}: {r['task']}")
            lines.append("")
            for c in r["calls"]:
                lines.append(f"- call `{c['tool']}` `{json.dumps(c['args'])}`")
            if r["error"]:
                lines.append(f"- error: `{r['error']}`")
            answer = (r["answer"] or "(no final answer)").replace(chr(10), " ")
            lines.append(f"- final answer: {answer[:600]}")
            lines.append("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1"))
    ap.add_argument("--model", nargs="+", default=[os.environ.get("LOCAL_LLM_MODEL", "qwen3:8b")],
                    help="one or more model names (run one after another into one report)")
    ap.add_argument("--profile", default="core")
    ap.add_argument("--max-turns", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds per model request")
    ap.add_argument("--think", dest="no_think", action="store_false",
                    help="let Qwen3-style models think (default: append /no_think)")
    ap.add_argument("--only", nargs="*", help="run only these task names")
    ap.add_argument("--hardware-note", default="no GPU (CPU inference)")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "audits", "local_model_eval.md"))
    ap.add_argument("--offline-check", action="store_true",
                    help="verify each task's expected answer against the library and exit")
    args = ap.parse_args()

    tasks = _tasks()
    if args.offline_check:
        bad = [t.name for t in tasks if not t.offline()]
        for t in tasks:
            print(f"{t.name}: expected {t.expected}")
        print("offline expectations:", "OK" if not bad else f"FAILED {bad}")
        return 1 if bad else 0

    models = list(args.model)
    runs: Dict[str, List[Dict[str, Any]]] = {}
    for model in models:
        args.model = model
        runs[model] = asyncio.run(_evaluate(args, tasks))
        raw = os.path.join(EVAL_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", model) + ".json")
        with open(raw, "w", encoding="utf-8") as f:
            json.dump(runs[model], f, indent=1)  # raw results, kept if a later model fails
    args.model = models
    import mcp_server as srv

    n_tools = {p: len(srv.server_for_profile(p)._tool_manager.list_tools())
               for p in dict.fromkeys(t.profile or args.profile for t in tasks)}
    _write_report(args.out, args, runs, n_tools)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
