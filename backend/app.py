import base64
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "results" / "telemetry.sqlite3"
TOPIC = "twintrust/discovery01/telemetry"

logger = logging.getLogger("uvicorn.error")
connected = threading.Event()
subscribed = threading.Event()
state_lock = threading.Lock()
state = {"last_storage_error": None}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def database_connection():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)

    with closing(database_connection()) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS mqtt_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_utc TEXT NOT NULL,
                topic TEXT NOT NULL,
                qos INTEGER NOT NULL,
                retained INTEGER NOT NULL,
                duplicate_flag INTEGER NOT NULL,
                payload BLOB NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS quality_session_settings (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                after_id INTEGER NOT NULL CHECK (after_id >= 0),
                updated_utc TEXT NOT NULL
            )
        """)
        connection.execute("""
            INSERT OR IGNORE INTO quality_session_settings
                (singleton, after_id, updated_utc) VALUES (1, 0, ?)
        """, (utc_now(),))
        connection.commit()


def read_quality_session(connection):
    row = connection.execute(
        "SELECT after_id, updated_utc FROM quality_session_settings WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Quality session settings are missing. Restart the backend.")
    return dict(row)


def save_quality_session(after_id=None):
    """Local operator action. None captures the current last stored ID.

    Stop the gateway first, capture this boundary, then reset the board and
    restart the gateway. This does not detect or prove a board restart.
    Existing raw messages are never deleted or changed.
    """
    if after_id is not None and (type(after_id) is not int or not 0 <= after_id <= 9007199254740991):
        raise ValueError("Boundary must be a nonnegative safe integer.")
    initialize_database()
    with closing(database_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM mqtt_messages"
        ).fetchone()[0]
        chosen = latest if after_id is None else after_id
        if chosen > latest:
            raise ValueError("Boundary cannot exceed the last stored message ID.")
        if chosen and not connection.execute(
            "SELECT 1 FROM mqtt_messages WHERE id = ?", (chosen,)
        ).fetchone():
            raise ValueError("Boundary must refer to an existing message ID, or 0.")
        connection.execute("""
            UPDATE quality_session_settings SET after_id = ?, updated_utc = ?
            WHERE singleton = 1
        """, (chosen, utc_now()))
        connection.commit()
        return read_quality_session(connection)


def on_connect(client, userdata, flags, reason_code, properties):
    subscribed.clear()

    if reason_code.is_failure:
        connected.clear()
        logger.error("MQTT connection rejected: %s", reason_code)
        return

    connected.set()
    result, _ = client.subscribe(TOPIC, qos=1)

    if result != mqtt.MQTT_ERR_SUCCESS:
        logger.error("MQTT subscribe request failed: %s", result)


def on_subscribe(client, userdata, mid, reason_codes, properties):
    if reason_codes and all(not code.is_failure for code in reason_codes):
        subscribed.set()
        logger.info("MQTT subscribed: %s", TOPIC)
    else:
        subscribed.clear()
        logger.error("MQTT subscription rejected: %s", reason_codes)


def on_disconnect(
    client, userdata, disconnect_flags, reason_code, properties
):
    connected.clear()
    subscribed.clear()
    logger.info("MQTT disconnected: %s", reason_code)


def on_message(client, userdata, message):
    received_utc = utc_now()

    try:
        with closing(database_connection()) as connection:
            connection.execute("""
                INSERT INTO mqtt_messages (
                    received_utc, topic, qos, retained,
                    duplicate_flag, payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                received_utc,
                message.topic,
                message.qos,
                int(message.retain),
                int(message.dup),
                bytes(message.payload),
            ))
            connection.commit()

        with state_lock:
            state["last_storage_error"] = None

    except sqlite3.Error as error:
        with state_lock:
            state["last_storage_error"] = str(error)

        logger.exception("Could not store MQTT message")


@asynccontextmanager
async def lifespan(app):
    initialize_database()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"twintrust-backend-{uuid.uuid4().hex[:8]}",
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)

    client.connect_async("127.0.0.1", 1883, keepalive=60)
    client.loop_start()

    try:
        yield
    finally:
        client.disconnect()
        client.loop_stop()
        connected.clear()
        subscribed.clear()


app = FastAPI(
    title="TwinTrust Backend",
    version="0.1.0",
    lifespan=lifespan,
)


def reject_nonfinite(value):
    raise ValueError(f"Non-finite JSON value: {value}")


def serialize_message(row):
    payload = bytes(row["payload"])
    result = {
        "id": row["id"],
        "received_utc": row["received_utc"],
        "topic": row["topic"],
        "qos": row["qos"],
        "retained": bool(row["retained"]),
        "duplicate_flag": bool(row["duplicate_flag"]),
        "record": None,
        "parse_error": None,
    }

    try:
        text = payload.decode("utf-8")
        result["raw_payload"] = text
    except UnicodeDecodeError:
        result["raw_payload_base64"] = base64.b64encode(
            payload
        ).decode("ascii")
        result["parse_error"] = "Payload is not UTF-8"
        return result

    try:
        record = json.loads(
            text,
            parse_constant=reject_nonfinite,
        )
        if not isinstance(record, dict):
            raise ValueError("Expected a JSON object")

        # Also reject overflowing JSON numbers, such as 1e999.
        json.dumps(record, allow_nan=False)
        result["record"] = record

    except (ValueError, RecursionError) as error:
        result["parse_error"] = str(error)

    return result


@app.get("/health")
def health():
    with closing(database_connection()) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM mqtt_messages"
        ).fetchone()[0]
        latest = connection.execute("""
            SELECT received_utc FROM mqtt_messages
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    with state_lock:
        storage_error = state["last_storage_error"]

    return {
        "api_running": True,
        "mqtt_connected": connected.is_set(),
        "mqtt_subscribed": subscribed.is_set(),
        "topic": TOPIC,
        "stored_messages": count,
        "last_received_utc": (
            latest["received_utc"] if latest else None
        ),
        "last_storage_error": storage_error,
    }


@app.get("/latest")
def latest():
    with closing(database_connection()) as connection:
        row = connection.execute("""
            SELECT * FROM mqtt_messages
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No MQTT messages stored yet. Run the gateway.",
        )

    return serialize_message(row)


@app.get("/history")
def history(limit: int = Query(default=30, ge=1, le=1000)):
    with closing(database_connection()) as connection:
        rows = connection.execute("""
            SELECT * FROM mqtt_messages
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()

    return {
        "count": len(rows),
        "order": "oldest_to_newest",
        "messages": [
            serialize_message(row) for row in reversed(rows)
        ],
    }


# TwinTrust dashboard route
from fastapi.responses import FileResponse


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "web" / "index.html")


# TwinTrust offline evaluation routes
import json as evaluation_json


@app.get("/experiments/latest")
def latest_experiment():
    folders = sorted(
        folder for folder in (ROOT / "data" / "results").glob("evaluation_*")
        if folder.is_dir() and (folder / "report.json").is_file()
    )
    if not folders:
        raise HTTPException(404, "No saved evaluation found.")

    folder = folders[-1]
    report = evaluation_json.loads(
        (folder / "report.json").read_text(encoding="utf-8")
    )
    datasets = {}

    for name in ["baseline", "noise", "spikes", "dropout", "sustained_rise"]:
        path = folder / f"{name}.filtered.jsonl"
        if not path.is_file():
            raise HTTPException(409, f"Evaluation file missing: {name}")
        datasets[name] = [
            evaluation_json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    return {
        "evaluation_id": folder.name,
        "report": report,
        "datasets": datasets,
    }


# TwinTrust detection report endpoint
@app.get("/experiments/detection/latest")
def latest_detection_report():
    folders = sorted(
        folder
        for folder in (ROOT / "data" / "results").glob("detection_*")
        if folder.is_dir() and (folder / "report.json").is_file()
    )
    if not folders:
        raise HTTPException(404, "No saved detection report found.")

    folder = folders[-1]
    report = evaluation_json.loads(
        (folder / "report.json").read_text(encoding="utf-8")
    )

    allowed = {
        "control", "delay", "duplicates", "out_of_order",
        "stuck", "bias_drift", "unit_mismatch"
    }
    examples = {}

    for result in report["results"]:
        name = result["dataset"]
        if name not in allowed:
            continue

        path = folder / f"{name}.findings.jsonl"
        examples[name] = []

        if path.is_file():
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if line.strip():
                        examples[name].append(evaluation_json.loads(line))
                    if len(examples[name]) >= 5:
                        break

    return {
        "detection_id": folder.name,
        "report": report,
        "finding_examples": examples,
    }


# TwinTrust forecast report endpoint
@app.get("/experiments/forecast/latest")
def latest_forecast_report():
    folders = sorted(
        folder
        for folder in (ROOT / "data" / "results").glob("forecast_*")
        if folder.is_dir() and (folder / "report.json").is_file()
    )
    if not folders:
        raise HTTPException(404, "No saved forecast report found.")

    folder = folders[-1]
    report = evaluation_json.loads(
        (folder / "report.json").read_text(encoding="utf-8")
    )
    return {
        "forecast_id": folder.name,
        "report": report,
    }


# Derived view only; raw messages and existing endpoints remain unchanged.
from processing.quality import QualityProcessor


@app.get("/quality/latest")
def latest_quality(after_id: int | None = Query(default=None, ge=0, le=9007199254740991)):
    with closing(database_connection()) as connection:
        # Read the shared setting and messages from one database snapshot.
        connection.execute("BEGIN")
        session = read_quality_session(connection)
        boundary_source = "saved_session" if after_id is None else "explicit_query"
        if after_id is None:
            after_id = session["after_id"]
        rows = connection.execute("""
            SELECT * FROM mqtt_messages WHERE id > ? ORDER BY id DESC LIMIT 1000
        """, (after_id,)).fetchall()
    processor = QualityProcessor()
    counts = {}
    accepted = 0
    first_id = None
    for row in reversed(rows):
        first_id = row["id"] if first_id is None else first_id
        message = serialize_message(row)
        try:
            timestamp = datetime.fromisoformat(message["received_utc"])
            if timestamp.tzinfo is None:
                raise ValueError("Reception timestamp has no timezone")
            arrival = timestamp.timestamp() * 1000
        except (ValueError, TypeError, OverflowError):
            arrival = float("nan")
        result = processor.process(message["record"], arrival, message["parse_error"])
        accepted += int(result["accepted"])
        for flag in result["quality_flags"]:
            counts[flag] = counts.get(flag, 0) + 1
    result = processor.snapshot(datetime.now(timezone.utc).timestamp() * 1000)
    with state_lock:
        storage_error = state["last_storage_error"]
    if storage_error or not connected.is_set() or not subscribed.is_set():
        result["status"] = "storage_error" if storage_error else "mqtt_disconnected"
        result["raw_decision"] = result["filtered_decision"] = "withheld"
        result["forecast_30s_c"] = dict.fromkeys(result["forecast_30s_c"])
        result["forecast_status"] = "withheld"
    result.update(processing_version="quality-v1", checked_messages=len(rows),
                  accepted_messages=accepted, quality_counts=counts, after_id=after_id,
                  boundary_source=boundary_source, saved_after_id=session["after_id"],
                  session_updated_utc=session["updated_utc"],
                  first_checked_id=first_id, last_checked_id=rows[0]["id"] if rows else None,
                  limitations=[
                      "Derived from at most the latest 1000 stored messages; counters and deduplication are scoped to this window.",
                      "No boot identifier: backward counters are rejected conservatively; restart and delayed old data are ambiguous.",
                      "The backend saves one session boundary for all visitors. A local operator must start a new segment after a confirmed board restart.",
                      "Replay starts without history before the checked window and can differ from an uninterrupted processor.",
                      "Range and validity gates do not prove physical accuracy or detect bias drift.",
                      "Fresh reception does not prove a fresh source measurement or measure end-to-end delay.",
                      "Forecast is a simple unvalidated trend extrapolation, not a safety signal.",
                  ])
    return result


@app.get("/quality/session")
def quality_session():
    """Read-only shared setting. No public HTTP endpoint can change it."""
    with closing(database_connection()) as connection:
        return read_quality_session(connection)


def session_command():
    import argparse
    parser = argparse.ArgumentParser(description="Manage the shared TwinTrust board session locally.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start-new-quality-session", action="store_true",
                        help="Capture the last stored ID. Stop gateway first, then reset board and restart gateway.")
    action.add_argument("--set-quality-boundary", type=int, metavar="ID",
                        help="Save a known last ID from before a confirmed board restart. 0 restores full-window processing.")
    action.add_argument("--show-quality-session", action="store_true")
    args = parser.parse_args()
    try:
        if args.show_quality_session:
            initialize_database()
            with closing(database_connection()) as connection:
                result = read_quality_session(connection)
        else:
            result = save_quality_session(args.set_quality_boundary)
        print(json.dumps(result, indent=2))
        if not args.show_quality_session:
            print("Shared boundary saved. Raw messages unchanged. All dashboard visitors use this setting.")
    except (ValueError, sqlite3.Error, RuntimeError) as error:
        parser.exit(1, f"Cannot update quality session: {error}\n")


if __name__ == "__main__":
    session_command()


# TwinTrust authenticated presenter controls
from backend.presenter_controls import install_routes as _install_presenter_routes
_install_presenter_routes(app, ROOT)
