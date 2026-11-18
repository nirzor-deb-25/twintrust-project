import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import serial


def main():
    parser = argparse.ArgumentParser(
        description="Record the original bytes received from the sensor."
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()

    if args.seconds <= 0:
        parser.error("--seconds must be greater than zero")

    root = Path(__file__).resolve().parents[1]
    folder = root / "data" / "original"
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session = f"{stamp}_{uuid.uuid4().hex[:8]}"
    raw_path = folder / f"capture_{session}.jsonl"
    metadata_path = folder / f"capture_{session}.metadata.json"

    started = datetime.now(timezone.utc).isoformat()
    byte_count = 0
    interrupted = False

    print(f"Recording {args.port} for {args.seconds:g} seconds...", flush=True)
    print(f"File: {raw_path}", flush=True)

    try:
        with serial.Serial(args.port, 115200, timeout=0.5) as port:
            with raw_path.open("xb") as output:
                deadline = time.monotonic() + args.seconds
                try:
                    while time.monotonic() < deadline:
                        size = min(max(port.in_waiting, 1), 4096)
                        chunk = port.read(size)
                        if chunk:
                            output.write(chunk)
                            byte_count += len(chunk)
                except KeyboardInterrupt:
                    interrupted = True
    except (serial.SerialException, OSError) as exc:
        print(f"Recording failed: {exc}")
        print("Any partial capture file has been left unchanged.")
        return 1

    complete = 0
    marked_valid = 0
    unparsed = 0

    for line in raw_path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("Expected a JSON object")
            complete += 1
            marked_valid += record.get("valid") is True
        except (ValueError, UnicodeDecodeError):
            unparsed += 1

    metadata = {
        "session_id": session,
        "port": args.port,
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "requested_seconds": args.seconds,
        "interrupted": interrupted,
        "bytes_saved": byte_count,
        "complete_json_objects": complete,
        "records_marked_valid": marked_valid,
        "unparsed_lines": unparsed,
        "raw_file": raw_path.name,
    }

    with metadata_path.open("x", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2)

    print(f"Finished. {args.port} is closed.")
    print(f"Complete JSON records: {complete}")
    print(f"Records marked valid: {marked_valid}")
    print(f"Unparsed lines: {unparsed}")
    print(f"Raw data: {raw_path}")
    print(f"Session details: {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
