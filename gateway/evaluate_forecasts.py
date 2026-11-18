import bisect
import hashlib
import json
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from processing.quality import trend_forecast


def load(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def reference_at(rows, target):
    times = [row["uptime_ms"] for row in rows]
    index = bisect.bisect_left(times, target)

    if index < len(rows) and times[index] == target:
        return rows[index]["raw_temperature_c"]

    if index == 0 or index == len(rows):
        return None

    before, after = rows[index - 1], rows[index]
    interval = after["uptime_ms"] - before["uptime_ms"]

    if interval <= 0 or interval > 2000:
        return None

    fraction = (target - before["uptime_ms"]) / interval
    return before["raw_temperature_c"] + fraction * (
        after["raw_temperature_c"] - before["raw_temperature_c"]
    )


def main():
    folders = sorted(
        path for path in (ROOT / "data/results").glob("evaluation_*")
        if (path / "report.json").is_file()
    )
    if not folders:
        raise SystemExit("Run evaluate_filters.py first.")

    source = folders[-1]
    source_report = json.loads((source / "report.json").read_text(encoding="utf-8"))
    if source_report.get("processing_version") != "quality-v1":
        raise SystemExit("Run the updated evaluate_filters.py first (quality-v1 required).")
    baseline = load(source / "baseline.filtered.jsonl")
    rise = load(source / "sustained_rise.filtered.jsonl")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "data/results" / (
        f"forecast_{stamp}_{uuid.uuid4().hex[:8]}"
    )
    output.mkdir(parents=True, exist_ok=False)

    report = {
        "processing_version": "quality-v1",
        "input_evaluation": str(source),
        "horizon_seconds": 30,
        "history_seconds": 30,
        "minimum_readings": 20,
        "minimum_history_span_seconds": 19,
        "method": "Ordinary least-squares straight-line extrapolation",
        "target": "Reference temperature exactly 30 seconds ahead",
        "target_alignment": "Linear interpolation between adjacent reference readings",
        "results": [],
        "input_sha256": {},
        "limitations": [
            "One recorded session and its synthetic variants.",
            "No parameter search or held-out-session validation performed.",
            "Overlapping forecast windows are not independent trials.",
            "Reference interpolation is for scoring only.",
            "Raw and filtered trends use identical forecast methods.",
            "Shared quality gates precede both trends; their forecast algorithms are identical.",
            "Excluded duplicate and backward events do not update history or refresh freshness.",
            "These source-time datasets cannot establish real transport delay or reception freshness.",
            "Missing forecasts are excluded from MAE and counted separately.",
            "A trend forecast can overshoot when a rise stops.",
        ],
    }

    print(
        f"{'Dataset':<17} {'Scored':>7} {'Raw trend':>11} "
        f"{'Filt trend':>11} {'Hold raw':>10}"
    )

    for name in ["baseline", "noise", "spikes", "dropout", "sustained_rise"]:
        input_path = source / f"{name}.filtered.jsonl"
        report["input_sha256"][input_path.name] = hashlib.sha256(
            input_path.read_bytes()
        ).hexdigest()

        rows = load(input_path)
        reference = rise if name == "sustained_rise" else baseline
        history = []
        predictions = []
        errors = {"raw_trend": [], "filtered_trend": [], "hold_raw": []}
        eligible = 0
        unavailable = 0

        for row in rows:
            if not row.get("accepted", False):
                if row.get("filter_reset"):
                    history.clear()
                    timestamp = row.get("uptime_ms")
                    if type(timestamp) is int and timestamp >= 0 and timestamp + 30000 <= reference[-1]["uptime_ms"]:
                        eligible += 1
                        unavailable += 1
                continue
            now = row["uptime_ms"]
            target_time = now + 30000

            if target_time <= reference[-1]["uptime_ms"]:
                eligible += 1

            if row["filter_reset"]:
                history.clear()

            if (
                row["raw_temperature_c"] is None
                or row["filtered_temperature_c"] is None
            ):
                history.clear()
                if target_time <= reference[-1]["uptime_ms"]:
                    unavailable += 1
                continue

            history.append(row)
            history = [
                item for item in history
                if now - item["uptime_ms"] <= 30000
            ]

            raw_prediction = trend_forecast(
                history, "raw_temperature_c", now
            )
            filtered_prediction = trend_forecast(
                history, "filtered_temperature_c", now
            )

            # Look up future reference only after producing the predictions.
            actual = reference_at(reference, target_time)

            if (
                raw_prediction is None
                or filtered_prediction is None
                or actual is None
            ):
                if target_time <= reference[-1]["uptime_ms"]:
                    unavailable += 1
                continue

            predictions.append({
                "origin_seq": row["seq"],
                "origin_uptime_ms": now,
                "target_uptime_ms": target_time,
                "raw_trend_c": raw_prediction,
                "filtered_trend_c": filtered_prediction,
                "hold_raw_c": row["raw_temperature_c"],
                "reference_target_c": actual,
            })

            errors["raw_trend"].append(abs(raw_prediction - actual))
            errors["filtered_trend"].append(abs(filtered_prediction - actual))
            errors["hold_raw"].append(
                abs(row["raw_temperature_c"] - actual)
            )

        scores = {
            method: statistics.mean(values) if values else None
            for method, values in errors.items()
        }

        report["results"].append({
            "dataset": name,
            "scored_forecasts": len(predictions),
            "excluded_events": sum(not row.get("accepted", False) for row in rows),
            "eligible_origins": eligible,
            "unavailable_forecasts": unavailable,
            "coverage_fraction": (
                len(predictions) / eligible if eligible else None
            ),
            "mae_c": scores,
        })

        with (output / f"{name}.predictions.jsonl").open(
            "x", encoding="utf-8"
        ) as handle:
            for prediction in predictions:
                handle.write(json.dumps(prediction, allow_nan=False) + "\n")

        def display(value):
            return f"{value:.4f}" if value is not None else "n/a"

        print(
            f"{name:<17} {len(predictions):>7} "
            f"{display(scores['raw_trend']):>11} "
            f"{display(scores['filtered_trend']):>11} "
            f"{display(scores['hold_raw']):>10}"
        )

    with (output / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)

    print()
    print("All MAE values are in degrees C; lower is better.")
    print("Methods are scored on matching forecast origins within each dataset.")
    print("Coverage can differ between datasets, especially after dropout.")
    print(f"Saved results: {output}")
    print("Original files unchanged. Nothing published to MQTT.")


if __name__ == "__main__":
    main()
