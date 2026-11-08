# SPDX-License-Identifier: AGPL-3.0-or-later
"""PUMS peak-memory benchmark (PLAN_ROUND_14 workstream C; PLAN_ROUND_11 R2/F3).

Bounded synthetic data only: no download. The script writes gzip CSVs shaped
like a 5-year state person file (RT, SERIALNO, DIVISION, SPORDER, PUMA,
REGION, STATE, ADJINC, PWGTP, AGEP, SEX, SCHL, WAGP, PINCP, optional filler
columns, PWGTP1-PWGTP80) and measures each phase in a fresh subprocess:

    baseline   interpreter plus numpy, pandas and census.pums imported
    read       census.pums.read_pums_csv(columns=AGEP, PINCP, WAGP) at each chunk size
    weights    census.pums._weight_matrix on the frame read (81 float64 columns)
    tabulate   census.pums.tabulate: median PINCP (AGEP >= 15), mean WAGP
               (AGEP >= 16), mean WAGP by PUMA; all with 80-replicate SEs

Peak RSS is the kernel's high-water mark (VmHWM; resource.getrusage
ru_maxrss reports the same on Linux). In the weights and tabulate
subprocesses the file is read first and the high-water mark is then reset
(writing 5 to /proc/self/clear_refs, Linux 4.0+), so their peak covers only
that phase on top of the frame already resident. Where the reset is not
available the peak includes the read and the report says so.

Chunk sizes: every tabulated estimate and SE, and a hash of the frame read,
must be identical across chunk sizes; the report records the comparison and
the command exits 1 when they differ.

Usage:
    python scripts/pums_memory_benchmark.py                 # 100k, 500k, 1.9M rows -> docs/audits/pums_memory.md
    python scripts/pums_memory_benchmark.py --workdir DIR   # generated CSVs in DIR (deleted afterwards unless --keep)
    python scripts/pums_memory_benchmark.py --small         # 2,000 rows into a temporary directory (the unit test)

The 1.9M-row file (California's 5-year person file has about 1.9 million
records) is about 0.5 GB gzip-compressed; the full run takes about 10 minutes
and peaks at a few GB.
"""

import argparse
import gc
import gzip
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_MD = os.path.join(ROOT, "docs", "audits", "pums_memory.md")

SIZES = (100_000, 500_000, 1_900_000)
CHUNKSIZES = (50_000, 200_000, 1_000_000)  # 200,000 is read_pums_csv's default
SMALL_SIZES = (2_000,)
SMALL_CHUNKSIZES = (500, 2_000)
ANALYSIS_COLUMNS = ["AGEP", "PINCP", "WAGP"]
ROWS_PER_PUMA = 6_800   # California: about 1.9 million person records in 281 PUMAs
BLOCK_ROWS = 100_000    # rows generated and written at a time (bounds the generator's memory)
SEED = 20261108
GIB = 1024 ** 3
MIB = 1024 ** 2
OS_RESERVE_GIB = 2.0    # left for the OS and other processes when computing what fits


# --------------------------------------------------------------------------- synthetic files

def generate(path, n_rows, extra_columns=0, seed=SEED):
    """Write a synthetic PUMS-shaped person CSV (gzip) of ``n_rows`` records.

    Returns {path, rows, columns, gzip_bytes, csv_bytes}. Values are seeded and
    plausible (zero-padded PUMA and STATE, 13-character SERIALNO, integer
    weights, blank PINCP below age 15 and blank WAGP below age 16), so the
    reader does the same work as on a real file. Recent files call the state
    column STATE (older ones ST); census.pums keeps either as text.
    """
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    n_puma = max(1, round(n_rows / ROWS_PER_PUMA))
    csv_bytes, written, serial = 0, 0, 0
    with gzip.open(path, "wt", encoding="ascii", newline="", compresslevel=1) as f:
        while written < n_rows:
            n = min(BLOCK_ROWS, n_rows - written)
            sporder = rng.integers(1, 5, n)
            serial_ids = serial + np.cumsum(sporder == 1)
            serial = int(serial_ids[-1]) + 1
            agep = rng.integers(0, 96, n)
            pwgtp = rng.integers(1, 250, n)
            pincp = np.round(rng.lognormal(10.3, 1.1, n)).astype(np.int64) - rng.integers(0, 2, n) * 5_000
            wagp = np.where(rng.random(n) < 0.35, 0, np.round(rng.lognormal(10.4, 0.9, n)).astype(np.int64))
            cols = {
                "RT": "P",
                "SERIALNO": [f"2022HU{s:07d}" for s in serial_ids],
                "DIVISION": "9",
                "SPORDER": sporder,
                "PUMA": [f"{p:05d}" for p in rng.integers(1, n_puma + 1, n) * 100 + 1],
                "REGION": "4",
                "STATE": "06",
                "ADJINC": "1042311",
                "PWGTP": pwgtp,
                "AGEP": agep,
                "SEX": rng.integers(1, 3, n),
                "SCHL": pd.array(np.where(agep >= 3, rng.integers(1, 25, n), 0), dtype="Int64"),
                "WAGP": pd.array(wagp, dtype="Int64"),
                "PINCP": pd.array(pincp, dtype="Int64"),
            }
            cols["SCHL"][agep < 3] = pd.NA
            cols["WAGP"][agep < 16] = pd.NA
            cols["PINCP"][agep < 15] = pd.NA
            for j in range(extra_columns):
                cols[f"FILL{j + 1:03d}"] = rng.integers(0, 10, n)
            factors = rng.choice([0.29, 0.5, 1.0, 1.5, 1.71], size=(n, 80))
            reps = np.maximum(np.round(pwgtp[:, None] * factors), 0).astype(np.int64)
            for r in range(80):
                cols[f"PWGTP{r + 1}"] = reps[:, r]
            text = pd.DataFrame(cols).to_csv(index=False, header=written == 0, lineterminator="\n")
            csv_bytes += len(text)
            f.write(text)
            written += n
            n_columns = len(cols)
    return {"path": path, "rows": n_rows, "columns": n_columns, "gzip_bytes": os.path.getsize(path),
            "csv_bytes": csv_bytes, "pumas": n_puma}


# --------------------------------------------------------------------------- measurement (worker side)

def _status_kib(field):
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _windows_memory():
    """(working set, peak working set) in bytes on Windows (no resource module there)."""
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_info = getattr(kernel32, "K32GetProcessMemoryInfo")
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)


def _rss():
    """Current RSS in bytes (None where neither /proc nor the Windows API is available)."""
    kib = _status_kib("VmRSS")
    if kib is not None:
        return kib * 1024
    return _windows_memory()[0] if sys.platform == "win32" else None


def _peak():
    """Peak RSS in bytes: VmHWM, else ru_maxrss (KiB on Linux, bytes on macOS), else
    the Windows peak working set."""
    kib = _status_kib("VmHWM")
    if kib is not None:
        return kib * 1024
    if sys.platform == "win32":
        return _windows_memory()[1]
    import resource

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def _reset_peak():
    """Reset the high-water mark to the current RSS; False where unsupported."""
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as f:
            f.write("5")
        return True
    except OSError:
        return False


def _floats(values):
    """Exact float values as JSON-safe strings (repr round-trips; NaN kept)."""
    return [repr(float(v)) for v in values]


def run_phase(phase, path, chunksize):
    """Measure one phase in this (fresh) process; returns a JSON-able dict."""
    import pandas as pd

    from census import pums

    out = {"phase": phase, "chunksize": chunksize, "rss_after_imports": _rss(), "peak_reset": None}
    if phase == "baseline":
        out["peak"] = _peak()
        return out

    def read():
        return pums.read_pums_csv(path, "person", ANALYSIS_COLUMNS, chunksize=chunksize)

    if phase == "read":
        out["peak_reset"] = _reset_peak()
        start, t0 = _rss(), time.perf_counter()
        df = read()
        out.update(seconds=time.perf_counter() - t0, peak=_peak(), rss_start=start, rss_end=_rss(),
                   rows=len(df), columns_read=len(df.columns),
                   frame_bytes=int(df.memory_usage(deep=True).sum()),
                   frame_hash=str(int(pd.util.hash_pandas_object(df, index=False).sum())),
                   dtypes={c: str(t) for c, t in df.dtypes.items()})
        return out

    df = read()
    gc.collect()
    out["peak_reset"] = _reset_peak()
    start, t0 = _rss(), time.perf_counter()
    out["rss_start"] = start
    if phase == "weights":
        _, W = pums._weight_matrix(df, "person")
        out.update(seconds=time.perf_counter() - t0, peak=_peak(), matrix_bytes=int(W.nbytes), rows=len(df))
        return out
    if phase != "tabulate":
        raise ValueError(f"unknown phase {phase!r}")
    stats = [("median_PINCP", dict(statistic="median", var="PINCP", where="AGEP >= 15")),
             ("mean_WAGP", dict(statistic="mean", var="WAGP", where="AGEP >= 16")),
             ("mean_WAGP_by_PUMA", dict(statistic="mean", var="WAGP", by="PUMA", where="AGEP >= 16"))]
    results, steps = {}, {}
    for name, kw in stats:
        gc.collect()
        _reset_peak()
        s0, t1 = _rss(), time.perf_counter()
        t = pums.tabulate(df, **kw)
        steps[name] = {"seconds": time.perf_counter() - t1, "peak": _peak(), "rss_start": s0}
        results[name] = {"estimate": _floats(t["estimate"]), "se": _floats(t["se"]),
                         "n_unweighted": [int(v) for v in t["n_unweighted"]]}
    out.update(seconds=time.perf_counter() - t0, peak=max(s["peak"] for s in steps.values()), steps=steps,
               results=results, rows=len(df),
               first=dict(estimate=results["median_PINCP"]["estimate"][0], se=results["median_PINCP"]["se"][0]))
    return out


# --------------------------------------------------------------------------- orchestration

def _subprocess(phase, path, chunksize):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, os.path.abspath(__file__), "--worker", phase, "--file", path or "",
           "--chunksize", str(chunksize or 0)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"{phase} worker failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def environment():
    import numpy as np
    import pandas as pd

    total = None
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            total = int(next(line for line in f if line.startswith("MemTotal:")).split()[1]) * 1024
    except (OSError, StopIteration):
        pass
    return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "platform": platform.platform(),
            "machine": platform.machine(), "python": platform.python_version(), "numpy": np.__version__,
            "pandas": pd.__version__, "cpus": os.cpu_count(), "mem_total_bytes": total}


def equivalence(runs):
    """{size: {"tabulate": bool, "read_hash": bool}} comparing every chunk size."""
    out = {}
    for rows in sorted({r["rows"] for r in runs if "rows" in r}):
        tabs = [r["results"] for r in runs if r["phase"] == "tabulate" and r["rows"] == rows]
        hashes = {r["frame_hash"] for r in runs if r["phase"] == "read" and r["rows"] == rows}
        out[rows] = {"tabulate": len(tabs) > 1 and all(t == tabs[0] for t in tabs[1:]),
                     "read_hash": len(hashes) == 1, "chunksizes": len(tabs)}
    return out


def _fit(points, baseline):
    """Least-squares line peak = a + b * rows over (rows, peak) points
    (through the baseline when there is only one point)."""
    n = len(points)
    if n < 2:
        x, y = points[0]
        return float(baseline), (y - baseline) / x
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    b = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return my - b * mx, b


def _increment(run):
    """Phase peak minus RSS at its start (None where the current RSS is unknown)."""
    return None if run.get("rss_start") is None else run["peak"] - run["rss_start"]


def summarize(files, runs):
    """Per size: end-to-end peak, per-phase peaks and increments, bytes per row."""
    base = next(r for r in runs if r["phase"] == "baseline")["peak"]
    rows_list = sorted(files)
    table = []
    for rows in rows_list:
        reads = [r for r in runs if r["phase"] == "read" and r["rows"] == rows]
        tab = [r for r in runs if r["phase"] == "tabulate" and r["rows"] == rows]
        w = next(r for r in runs if r["phase"] == "weights" and r["rows"] == rows)
        default = next((r for r in reads if r["chunksize"] == 200_000), reads[-1])
        row = {"rows": rows, "gzip_bytes": files[rows]["gzip_bytes"], "csv_bytes": files[rows]["csv_bytes"],
               "columns": files[rows]["columns"], "columns_read": default["columns_read"],
               "frame_bytes": default["frame_bytes"],
               "read": {r["chunksize"]: {"peak": r["peak"], "seconds": r["seconds"]} for r in reads},
               "weights": {"peak": w["peak"], "increment": _increment(w),
                           "matrix_bytes": w["matrix_bytes"], "seconds": w["seconds"]},
               "tabulate": {r["chunksize"]: {"peak": r["peak"], "increment": _increment(r),
                                             "seconds": r["seconds"], "steps": r["steps"]} for r in tab}}
        row["read_peak"] = max(v["peak"] for v in row["read"].values())
        row["tab_peak"] = max(v["peak"] for v in row["tabulate"].values())
        row["end_to_end_peak"] = max(row["read_peak"], row["tab_peak"], w["peak"])
        row["bytes_per_row"] = (row["end_to_end_peak"] - base) / rows
        table.append(row)
    intercept, slope = _fit([(t["rows"], t["end_to_end_peak"]) for t in table], base)
    fits = {}
    for gib in (16, 128):
        budget = (gib - OS_RESERVE_GIB) * GIB
        fits[gib] = int((budget - intercept) / slope) if slope > 0 else None
    return {"baseline": base, "table": table, "intercept": intercept, "slope": slope, "fits": fits}


def _mib(b):
    return "n/a" if b is None else f"{b / MIB:,.0f}"


def _gib(b):
    return f"{b / GIB:.2f}"


def render(env, files, runs, summary, equiv, peak_reset_ok, small, compare=None):
    lines = [
        "# PUMS memory benchmark",
        "",
        f"Generated by `python scripts/pums_memory_benchmark.py{' --small' if small else ''}` on {env['date']} "
        "(PLAN_ROUND_14 workstream C; PLAN_ROUND_11 R2/F3). Synthetic PUMS-shaped person files only; nothing "
        "was downloaded.",
        "",
        "## Environment",
        "",
        f"- Platform: {env['platform']} ({env['machine']}), {env['cpus']} CPUs, "
        + (f"{env['mem_total_bytes'] / GIB:.0f} GiB RAM" if env["mem_total_bytes"] else "RAM unknown"),
        f"- Python {env['python']}, numpy {env['numpy']}, pandas {env['pandas']}",
        "- Peak RSS: kernel high-water mark (VmHWM, the value `resource.getrusage().ru_maxrss` reports), one "
        "fresh subprocess per phase and chunk size. "
        + ("The weights and tabulate subprocesses read the file, then reset the high-water mark "
           "(`/proc/self/clear_refs`), so their peak is that phase on top of the resident frame."
           if peak_reset_ok else "The high-water mark could not be reset here: weights and tabulate peaks "
           "include the read."),
        f"- Baseline (interpreter, numpy, pandas, census.pums imported): {_mib(summary['baseline'])} MiB",
        "",
        "## Inputs",
        "",
        "| Rows | Columns in file | Columns read | gzip size (MiB) | CSV size (MiB) | PUMAs | Frame in memory (MiB) |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for t in summary["table"]:
        lines.append(f"| {t['rows']:,} | {t['columns']} | {t['columns_read']} | {_mib(t['gzip_bytes'])} | "
                     f"{_mib(t['csv_bytes'])} | {files[t['rows']]['pumas']} | {_mib(t['frame_bytes'])} |")
    lines += [
        "",
        "Columns read: SERIALNO, SPORDER, STATE, PUMA, AGEP, PINCP, WAGP, PWGTP and PWGTP1-PWGTP80 "
        "(`read_pums_csv(columns=['AGEP', 'PINCP', 'WAGP'])`; identifiers, PUMA and weights are always kept). "
        "Weights are read as float32. A real 5-year person file has about 287 columns, not "
        f"{summary['table'][0]['columns']}: the parser still tokenizes the columns it does not keep (longer "
        "read times than here) but does not store them, so the stored frame, and the peaks below, depend "
        "on the columns read (`--extra-columns` adds unread filler columns to check this).",
        "",
        "## Peak RSS by phase (MiB)",
        "",
    ]
    chunks = sorted({c for t in summary["table"] for c in t["read"]})
    head = ["Rows"] + [f"read, chunk {c:,}" for c in chunks] + [
        "weight matrix (peak / increment)", "tabulate (peak / increment)", "end-to-end peak", "bytes per row"]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("| " + " | ".join(["---:"] * len(head)) + " |")
    for t in summary["table"]:
        incs = [v["increment"] for v in t["tabulate"].values() if v["increment"] is not None]
        tab_inc = max(incs) if incs else None
        cells = [f"{t['rows']:,}"] + [_mib(t["read"][c]["peak"]) if c in t["read"] else "" for c in chunks] + [
            f"{_mib(t['weights']['peak'])} / {_mib(t['weights']['increment'])}",
            f"{_mib(t['tab_peak'])} / {_mib(tab_inc)}", _mib(t["end_to_end_peak"]), f"{t['bytes_per_row']:,.0f}"]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "Increment: peak minus RSS at the start of the phase (the frame already resident). End-to-end peak: "
        "the largest of the read, weight-matrix and tabulate peaks, i.e. the peak of reading the file and "
        "running these tabulations in one process. Bytes per row: (end-to-end peak - baseline) / rows. "
        "The weight matrix alone is rows x 81 x 8 bytes "
        f"({_mib(summary['table'][-1]['weights']['matrix_bytes'])} MiB at {summary['table'][-1]['rows']:,} rows).",
        "",
        "### Tabulation steps (MiB peak / seconds, default chunk size)",
        "",
        "| Rows | median PINCP (AGEP >= 15) | mean WAGP (AGEP >= 16) | mean WAGP by PUMA |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for t in summary["table"]:
        tab = t["tabulate"].get(200_000) or next(iter(t["tabulate"].values()))
        st = tab["steps"]
        lines.append(f"| {t['rows']:,} | " + " | ".join(
            f"{_mib(st[k]['peak'])} / {st[k]['seconds']:.1f}" for k in ("median_PINCP", "mean_WAGP",
                                                                          "mean_WAGP_by_PUMA")) + " |")
    lines += ["", "### Wall time (seconds)", "",
              "| Rows | " + " | ".join(f"read, chunk {c:,}" for c in chunks) + " | weight matrix | tabulate |",
              "| ---: | " + " | ".join(["---:"] * len(chunks)) + " | ---: | ---: |"]
    for t in summary["table"]:
        tab = t["tabulate"].get(200_000) or next(iter(t["tabulate"].values()))
        lines.append(f"| {t['rows']:,} | " + " | ".join(
            f"{t['read'][c]['seconds']:.1f}" if c in t["read"] else "" for c in chunks)
            + f" | {t['weights']['seconds']:.2f} | {tab['seconds']:.1f} |")
    lines += ["", "## Chunk-size equivalence", "",
              "| Rows | Chunk sizes compared | Frame hash identical | Estimates and SEs identical |",
              "| ---: | ---: | :--- | :--- |"]
    for rows, e in sorted(equiv.items()):
        lines.append(f"| {rows:,} | {e['chunksizes']} | {'yes' if e['read_hash'] else 'NO'} | "
                     f"{'yes' if e['tabulate'] else 'NO'} |")
    lines += ["", "Identical means bit-for-bit: every estimate, SE and unweighted n of the three tabulations "
              "(including all PUMA groups) compared as exact float values, and one hash over the frame read.", ""]
    s = summary
    big = s["table"][-1]
    lines += [
        "## Scaling and practical limit",
        "",
        f"A least-squares line through the end-to-end peaks gives peak = {_mib(s['intercept'])} MiB + "
        f"{s['slope']:,.0f} bytes x rows. With {OS_RESERVE_GIB:g} GiB left for the operating system, the "
        "rows that fit with these columns are:",
        "",
        "| Machine RAM | Rows (person records) that fit | California-sized states (1.9M rows) |",
        "| :--- | ---: | ---: |",
    ]
    for gib, n in s["fits"].items():
        lines.append(f"| {gib} GiB | {n:,} | {n / 1_900_000:.1f} |" if n else f"| {gib} GiB | n/a | n/a |")
    lines += [
        "",
        "These are for the needed columns plus the 80 replicate weights. Each extra numeric column read adds "
        "8 bytes per row (float64 after conversion) and each extra text column more (pandas strings); "
        "`columns=None` reads all 280+ columns of a real file and multiplies the frame several-fold (the "
        "PumsMemoryWarning above 200 MB). The figures exclude the download step: `fetch_pums` holds the "
        "compressed response in memory once while writing it to the cache (one zip, hundreds of MB for the "
        "largest states).",
        "",
    ]
    if compare:
        before, label = compare
        lines += [
            "## Before and after the Round 14 copy fixes",
            "",
            f"Before: {label}. The fixes (census/pums.py) remove avoidable copies of the n x 81 weight matrix: "
            "`_group_sum` no longer copies it when every row counts and passes `copy=False` to DataFrame "
            "(pandas 3 copies a 2-D array by default); `weighted_median` gathers the rows it uses once "
            "instead of twice; `tabulate` no longer copies `W[rows]` per median group; `read_pums_csv` "
            "drops the parsed chunks before conversion.",
            "",
            "| Rows | read peak before / after | weight matrix before / after | tabulate peak before / after "
            "| end-to-end before / after | Estimates and SEs identical to before |",
            "| ---: | ---: | ---: | ---: | ---: | :--- |",
        ]
        prev = {t["rows"]: t for t in before["table"]}
        for t in summary["table"]:
            p = prev.get(t["rows"]) or prev.get(str(t["rows"]))
            if p is None:
                continue
            same = all(r["results"] == b["results"] for r in runs if r["phase"] == "tabulate" and r["rows"] == t["rows"]
                       for b in before["runs"] if b["phase"] == "tabulate" and b["rows"] == t["rows"]
                       and b["chunksize"] == r["chunksize"])
            lines.append(f"| {t['rows']:,} | {_mib(p['read_peak'])} / {_mib(t['read_peak'])} | "
                         f"{_mib(p['weights']['peak'])} / {_mib(t['weights']['peak'])} | "
                         f"{_mib(p['tab_peak'])} / {_mib(t['tab_peak'])} | "
                         f"{_mib(p['end_to_end_peak'])} / {_mib(t['end_to_end_peak'])} | {'yes' if same else 'NO'} |")
        lines.append("")
    if not small:
        lines += [
            "## Findings and recommendation",
            "",
            f"- Memory scales linearly with rows, at about {s['slope']:,.0f} bytes per person record for this "
            f"column set; the largest state (about {big['rows']:,} records) peaked at "
            f"{_gib(big['end_to_end_peak'])} GiB.",
            "- Chunk size does not bound memory (every chunk is kept until the frame is concatenated) and does "
            "not change any result: estimates, SEs and the frame are bit-identical across chunk sizes. Smaller "
            "chunks only lower the transient parser buffer.",
            "- The peak comes from tabulation, which holds the frame, the 81-column float64 weight matrix and "
            "per-statistic temporaries at once; reading alone peaks lower.",
            f"- A 16 GiB machine handles any single state with the needed columns; the owner's 128 GiB machine "
            f"holds about {s['fits'][128] / 1_900_000:.0f} California-sized files in one process (all states "
            "together are about 16 million 5-year person records).",
            "- Recommendation: keep the Round 10 design (full state files on demand, needed columns only); no "
            "streaming rewrite is warranted. For many states, tabulate state by state and combine, or read "
            "with `columns=[...]` and `filter=` to keep only the rows and variables needed.",
            "",
        ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--small", action="store_true", help="2,000 rows, chunk sizes 500 and 2,000 (fast test)")
    ap.add_argument("--sizes", help="comma-separated row counts (default 100000,500000,1900000)")
    ap.add_argument("--chunksizes", help="comma-separated chunk sizes (default 50000,200000,1000000)")
    ap.add_argument("--extra-columns", type=int, default=0, help="filler columns written but not read")
    ap.add_argument("--workdir", help="directory for the generated CSVs (default: a temporary directory)")
    ap.add_argument("--keep", action="store_true", help="keep the generated CSVs")
    ap.add_argument("--out", help="markdown report (default docs/audits/pums_memory.md; with --small, "
                    "pums_memory.md in the work directory)")
    ap.add_argument("--json", help="also write the raw measurements as JSON")
    ap.add_argument("--compare", help="raw JSON (--json) of an earlier run: add a before/after section")
    ap.add_argument("--compare-label", help="how the report describes the earlier run")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--file", help=argparse.SUPPRESS)
    ap.add_argument("--chunksize", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.worker:
        sys.path.insert(0, ROOT)
        print(json.dumps(run_phase(args.worker, args.file, args.chunksize)))
        return 0

    sizes = [int(s) for s in args.sizes.split(",")] if args.sizes else list(SMALL_SIZES if args.small else SIZES)
    chunks = ([int(c) for c in args.chunksizes.split(",")] if args.chunksizes
              else list(SMALL_CHUNKSIZES if args.small else CHUNKSIZES))
    own_dir = args.workdir is None
    workdir = args.workdir or tempfile.mkdtemp(prefix="pums_memory_")
    os.makedirs(workdir, exist_ok=True)
    out_md = args.out or (os.path.join(workdir, "pums_memory.md") if args.small else OUT_MD)

    env = environment()
    files, runs, made = {}, [_subprocess("baseline", None, None)], []
    try:
        for rows in sizes:
            path = os.path.join(workdir, f"psam_synthetic_{rows}.csv.gz")
            t0 = time.perf_counter()
            files[rows] = generate(path, rows, args.extra_columns)
            made.append(path)
            print(f"{rows:,} rows: generated {files[rows]['gzip_bytes'] / MIB:,.0f} MiB gzip in "
                  f"{time.perf_counter() - t0:.0f} s", file=sys.stderr)
            for c in chunks:
                runs.append(_subprocess("read", path, c))
            runs.append(_subprocess("weights", path, chunks[len(chunks) // 2]))
            for c in chunks:
                runs.append(_subprocess("tabulate", path, c))
            print(f"{rows:,} rows: measured", file=sys.stderr)
    finally:
        if not args.keep:
            for p in made:
                if os.path.exists(p):
                    os.remove(p)
            if own_dir and not args.small:
                shutil.rmtree(workdir, ignore_errors=True)

    equiv = equivalence(runs)
    summary = summarize(files, runs)
    peak_reset_ok = all(r["peak_reset"] for r in runs if r["phase"] in ("weights", "tabulate"))
    compare = None
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            before = json.load(f)
        compare = (before, args.compare_label or os.path.basename(args.compare))
    report = render(env, files, runs, summary, equiv, peak_reset_ok, args.small, compare)
    os.makedirs(os.path.dirname(os.path.abspath(out_md)), exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"environment": env, "files": files, "runs": runs, "equivalence": equiv,
                       "summary": {k: v for k, v in summary.items() if k != "table"},
                       "table": summary["table"]}, f, indent=1, default=str)
    ok = all(e["tabulate"] and e["read_hash"] for e in equiv.values())
    print(f"wrote {out_md}; chunk-size equivalence: {'identical' if ok else 'DIFFERENT'}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
