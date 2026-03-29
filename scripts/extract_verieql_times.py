#!/usr/bin/env python3
"""Extract per-pair VeriEQL times from their published experiment data."""

import json
import os

DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "VeriEQL", "experiments", "2023_03_27"
)
OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "results", "verieql_baseline_times.json"
)

SUITES = ["calcite", "literature", "leetcode"]


def process_file(filepath):
    per_pair_times = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            times = record["times"]
            pair_total = 0.0
            for entry in times:
                if entry is not None:
                    traversing, solving = entry
                    if traversing is not None:
                        pair_total += traversing
                    if solving is not None:
                        pair_total += solving
            per_pair_times.append(round(pair_total, 6))
    return per_pair_times


result = {
    "source": "data/VeriEQL/experiments/2023_03_27",
    "description": (
        "Per-pair VeriEQL measured times (traversing + solving) from their "
        "published experiment data. Timeout entries (null) excluded from measured time."
    ),
    "timeout_s": 600,
    "suites": {},
}

for suite in SUITES:
    filepath = os.path.join(DATA_DIR, f"{suite}.out")
    per_pair_times = process_file(filepath)
    total_time = round(sum(per_pair_times), 1)
    result["suites"][suite] = {
        "total_pairs": len(per_pair_times),
        "total_time_s": total_time,
        "per_pair_times_s": per_pair_times,
    }
    print(f"{suite}: {len(per_pair_times)} pairs, total_time={total_time}s")

with open(OUTPUT_PATH, "w") as f:
    json.dump(result, f, indent=2)

print(f"\nWritten to {OUTPUT_PATH}")
