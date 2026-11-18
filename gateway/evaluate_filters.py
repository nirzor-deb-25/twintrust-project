import copy
import hashlib
import json
import math
import statistics
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from processing.quality import QualityProcessor
BASE = ROOT / "data/scenarios/baseline_20261107T224640Z_fea32582"
FAULTS = BASE / "faults_20261107T224747Z_b17ff2fa"


def load_verified(path, expected_hash):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError(f"Hash mismatch: {path}")
    return [
        json.loads(line)
        for line in raw.splitlines()
        if line.strip()
    ]


def process(records):
    processor = QualityProcessor()
    rows = [processor.process(record) for record in records]
    return rows, processor.missing


def key(record):
    return record["device"], record["seq"], record["uptime_ms"]


def write_jsonl(path, records):
    with path.open("x", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(json.dumps(record, allow_nan=False) + "\n")


def main():
    manifest = json.loads(
        (FAULTS / "manifest.json").read_text(encoding="utf-8")
    )
    baseline = load_verified(
        BASE / "baseline.jsonl", manifest["baseline_sha256"]
    )
    if len(baseline) != 300:
        raise ValueError("Expected 300 baseline readings.")

    # A synthetic process change: ramp +4 C over 60 seconds, then hold.
    rise = copy.deepcopy(baseline)
    start_ms = baseline[60]["uptime_ms"]
    for record in rise:
        elapsed = (record["uptime_ms"] - start_ms) / 1000
        offset = 4.0 * min(max(elapsed / 60.0, 0.0), 1.0)
        record["temperature_c"] = round(
            record["temperature_c"] + offset, 4
        )

    datasets = [("baseline", baseline, baseline)]
    for scenario in manifest["scenarios"]:
        records = load_verified(
            FAULTS / scenario["data_file"], scenario["data_sha256"]
        )
        datasets.append((scenario["name"], records, baseline))

    # The intended rise is the reference for this scenario.
    datasets.append(("sustained_rise", rise, rise))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/results" / (
        f"evaluation_{stamp}_{uuid.uuid4().hex[:8]}"
    )
    folder.mkdir(parents=True, exist_ok=False)

    write_jsonl(folder / "sustained_rise.input.jsonl", rise)

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_sha256": manifest["baseline_sha256"],
        "scenario_manifest": str(FAULTS / "manifest.json"),
        "script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "processing_version": "quality-v1",
        "filter": {
            "trailing_median_window": 5,
            "ema_alpha": 0.25,
            "gap_policy": "Reset; do not reconstruct missing readings",
        },
        "rise": {
            "type": "Synthetic intended process change",
            "start_source_index": 60,
            "ramp_duration_seconds": 60,
            "added_temperature_c": 4.0,
        },
        "results": [],
        "limitations": [
            "Recorded baseline is not calibrated physical ground truth.",
            "MAE excludes missing and unusable readings.",
            "Rise reference includes the intended synthetic rise.",
            "No labels or reference values are inputs to the filter.",
            "This experiment does not establish performance on other sessions.",
            "Shared quality gates require valid metadata and all three measurements in range.",
            "Duplicates and backward counters are excluded; reset explicitly after a confirmed device restart.",
        ],
    }

    print(
        f"{'Dataset':<17} {'Rows':>5} {'Raw MAE':>10} "
        f"{'Filtered MAE':>14} {'Missing seq':>12}"
    )

    rise_rows = None

    for name, records, reference_records in datasets:
        rows, missing = process(records)
        reference = {
            key(record): record["temperature_c"]
            for record in reference_records
        }
        raw_errors = []
        filtered_errors = []

        for row in rows:
            target = reference.get(key(row))
            if target is not None and row["accepted"] and row["filtered_temperature_c"] is not None:
                raw_errors.append(abs(row["raw_temperature_c"] - target))
                filtered_errors.append(
                    abs(row["filtered_temperature_c"] - target)
                )

        raw_mae = statistics.mean(raw_errors) if raw_errors else None
        filtered_mae = (
            statistics.mean(filtered_errors) if filtered_errors else None
        )

        result = {
            "dataset": name,
            "rows": len(rows),
            "compared_readings": len(filtered_errors),
            "accepted_readings": sum(row["accepted"] for row in rows),
            "excluded_readings": sum(not row["accepted"] for row in rows),
            "raw_mae_c": raw_mae,
            "filtered_mae_c": filtered_mae,
            "missing_sequence_numbers": missing,
        }
        report["results"].append(result)
        write_jsonl(folder / f"{name}.filtered.jsonl", rows)

        raw_text = f"{raw_mae:.4f}" if raw_mae is not None else "n/a"
        filtered_text = (
            f"{filtered_mae:.4f}" if filtered_mae is not None else "n/a"
        )
        print(
            f"{name:<17} {len(rows):>5} {raw_text:>10} "
            f"{filtered_text:>14} {missing:>12}"
        )

        if name == "sustained_rise":
            rise_rows = rows

    # Fixed demonstration threshold, chosen before evaluating crossings.
    threshold = 22.0

    def crossing(field):
        for row in rise_rows:
            value = row[field]
            if row["accepted"] and value is not None and value >= threshold:
                return row
        return None

    raw_crossing = crossing("raw_temperature_c")
    filtered_crossing = crossing("filtered_temperature_c")
    delay = None

    if raw_crossing is not None and filtered_crossing is not None:
        delay = (
            filtered_crossing["uptime_ms"] - raw_crossing["uptime_ms"]
        ) / 1000

    report["rise_threshold_test"] = {
        "threshold_c": threshold,
        "purpose": "Demonstration threshold, not a safety limit",
        "raw_crossing_sequence": (
            raw_crossing["seq"] if raw_crossing else None
        ),
        "filtered_crossing_sequence": (
            filtered_crossing["seq"] if filtered_crossing else None
        ),
        "additional_crossing_delay_seconds": delay,
    }

    with (folder / "report.json").open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)

    print()
    if delay is not None:
        print(f"Synthetic rise: filtered crossing of 22 C is {delay:.3f} seconds later.")
    else:
        print("Synthetic rise: threshold crossing unavailable; see report.")

    print(f"Report and per-reading results: {folder}")
    print("Original files unchanged. Nothing published to MQTT.")


if __name__ == "__main__":
    main()
