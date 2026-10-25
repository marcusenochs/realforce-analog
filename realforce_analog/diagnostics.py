"""Reproducible request timing and matrix snapshots, without changing settings."""

from dataclasses import asdict
from datetime import datetime, timezone
import math
from pathlib import Path
import json
import statistics
import time


def distribution(values):
    values = sorted(values)
    if not values:
        return None
    return {
        "count": len(values), "min": values[0], "median": statistics.median(values),
        "p95": values[math.ceil(len(values) * .95) - 1],
        "p99": values[math.ceil(len(values) * .99) - 1], "max": values[-1],
    }


def matrix_snapshot(reader):
    return [reader.read(index) for index in range(128)]


def benchmark(reader, indices, seconds=5.0):
    indices = tuple(indices)
    if not indices or len(set(indices)) != len(indices):
        raise ValueError("Provide distinct sensor indices.")
    if not math.isfinite(seconds) or not .1 <= seconds <= 120:
        raise ValueError("Duration must be 0.1 through 120 seconds.")
    for _ in range(3):
        for index in indices:
            reader.read(index)
    records = []
    start = time.perf_counter_ns()
    deadline = start + int(seconds * 1e9)
    while time.perf_counter_ns() < deadline:
        for index in indices:
            sample = reader.read(index)
            record = asdict(sample)
            record["delta"] = sample.delta
            records.append(record)
    end = time.perf_counter_ns()
    elapsed = (end - start) / 1e9
    by_key = {}
    for index in indices:
        rows = [row for row in records if row["key"] == index]
        revisits = [(b["received_ns"] - a["received_ns"]) / 1e6
                    for a, b in zip(rows, rows[1:])]
        by_key[str(index)] = {
            "requests": len(rows), "responses_per_second": len(rows) / elapsed,
            "round_trip_ms": distribution([row["round_trip_ms"] for row in rows]),
            "revisit_ms": distribution(revisits),
            "delta": distribution([row["delta"] for row in rows]),
            "baseline": distribution([row["baseline"] for row in rows]),
        }
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "device": reader.info, "duration_seconds": elapsed,
        "scope": "C5 host request/response timing; not fresh physical-sample or key-to-screen latency",
        "keys": by_key, "samples": records,
    }


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
