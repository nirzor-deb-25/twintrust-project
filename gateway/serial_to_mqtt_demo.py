import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import serial
import paho.mqtt.client as mqtt
from command_support import CommandBridge, COMMAND_TOPIC, RESULT_TOPIC


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description="TwinTrust USB-to-MQTT gateway")
    parser.add_argument("--port", default="COM8")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()

    if args.seconds <= 0:
        parser.error("--seconds must be greater than zero")

    root = Path(__file__).resolve().parents[1]
    folder = root / "data" / "original"
    folder.mkdir(parents=True, exist_ok=True)

    session = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_" + uuid.uuid4().hex[:8]
    )
    raw_path = folder / f"gateway_{session}.jsonl"
    events_path = folder / f"gateway_{session}.events.jsonl"
    metadata_path = folder / f"gateway_{session}.metadata.json"

    topic = "twintrust/discovery01/telemetry"
    connected = threading.Event()
    command_log = []

    def emit_result(result):
        result['gateway_utc'] = utc_now()
        command_log.append(result)
        print('COMMAND ' + json.dumps(result), flush=True)
        client.publish(RESULT_TOPIC, json.dumps(result), qos=1, retain=False)

    bridge = CommandBridge(emit_result)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"twintrust-{session}",
    )
    client.max_queued_messages_set(20)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(COMMAND_TOPIC, qos=1)
            connected.set()
        else:
            print(f"MQTT rejected connection: {reason_code}", flush=True)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        connected.clear()

    def on_message(client, userdata, message):
        if message.topic == COMMAND_TOPIC:
            bridge.receive(message.payload, retained=message.retain)

    client.on_message = on_message
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    pending = b""
    published = 0
    rejected = 0
    failure = None
    started = utc_now()
    loop_started = False

    try:
        client.connect(args.host, args.mqtt_port, 60)
        client.loop_start()
        loop_started = True

        if not connected.wait(5):
            raise RuntimeError("MQTT connection was not confirmed")

        with serial.Serial(args.port, 115200, timeout=0.05, write_timeout=1) as port:
            with raw_path.open("xb") as raw, events_path.open(
                "x", encoding="utf-8"
            ) as events:
                deadline = time.monotonic() + args.seconds

                print(f"Gateway running for {args.seconds:g} seconds.", flush=True)
                print(f"Topic: {topic}", flush=True)

                while time.monotonic() < deadline:
                    bridge.tick(port)
                    chunk = port.read(min(max(port.in_waiting, 1), 4096))
                    if not chunk:
                        continue

                    raw.write(chunk)
                    raw.flush()
                    pending += chunk

                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if not line.strip():
                            continue

                        received_utc = utc_now()
                        received_ns = time.monotonic_ns()

                        try:
                            record = json.loads(line)
                            if not isinstance(record, dict):
                                raise ValueError("Expected a JSON object")
                            if record.get("type") == "command_ack":
                                bridge.acknowledge(record)
                                events.write(json.dumps({
                                    "received_utc": received_utc,
                                    "event_type": "command_ack",
                                    "accepted": False,
                                    "record": record,
                                }) + "\n")
                                events.flush()
                                continue
                            if record.get("device") != "discovery01":
                                raise ValueError("Unexpected device")
                            if type(record.get("seq")) is not int:
                                raise ValueError("Missing integer sequence")
                            if type(record.get("valid")) is not bool:
                                raise ValueError("Missing boolean validity")
                        except (ValueError, UnicodeDecodeError) as exc:
                            rejected += 1
                            events.write(json.dumps({
                                "received_utc": received_utc,
                                "accepted": False,
                                "reason": str(exc),
                            }) + "\n")
                            events.flush()
                            continue

                        event = {
                            "session_id": session,
                            "received_utc": received_utc,
                            "received_monotonic_ns": received_ns,
                            "accepted": True,
                            "record": record,
                        }
                        events.write(json.dumps(event, allow_nan=False) + "\n")
                        events.flush()

                        try:
                            demo_paused = json.loads((root / "data" / "presenter_control" / "state.json").read_text(encoding="utf-8")).get("paused") is True
                        except (OSError, ValueError):
                            demo_paused = False
                        if demo_paused:
                            events.write(json.dumps({"event_type": "presenter_pause", "forwarded": False,
                                "seq": record["seq"], "received_utc": received_utc}) + "\n")
                            events.flush()
                            print(f"Paused forwarding seq={record['seq']} (raw saved)", flush=True)
                            continue

                        if not connected.is_set():
                            raise RuntimeError(
                                "MQTT disconnected; original data is saved"
                            )

                        info = client.publish(
                            topic, payload=line, qos=1, retain=False
                        )
                        if info.rc != mqtt.MQTT_ERR_SUCCESS:
                            raise RuntimeError(f"Publish failed: {info.rc}")

                        info.wait_for_publish(timeout=3)
                        if not info.is_published():
                            raise RuntimeError(
                                "Broker acknowledgement timed out"
                            )

                        published += 1
                        print(
                            f"Published seq={record['seq']} "
                            f"valid={record['valid']}",
                            flush=True,
                        )

    except KeyboardInterrupt:
        failure = "Stopped by user"
    except (OSError, RuntimeError, ValueError) as exc:
        failure = str(exc)
        print(f"Gateway stopped: {failure}", flush=True)
    finally:
        bridge.stop()
        client.disconnect()
        if loop_started:
            client.loop_stop()

        metadata = {
            "session_id": session,
            "started_utc": started,
            "finished_utc": utc_now(),
            "serial_port": args.port,
            "mqtt_host": args.host,
            "mqtt_port": args.mqtt_port,
            "topic": topic,
            "requested_seconds": args.seconds,
            "broker_acknowledged": published,
            "rejected_complete_lines": rejected,
            "trailing_partial_bytes": len(pending),
            "stop_reason": failure or "Duration completed",
            "command_results": command_log,
        }
        with metadata_path.open("x", encoding="utf-8") as output:
            json.dump(metadata, output, indent=2)

    print(f"\nBroker-acknowledged messages: {published}")
    print(f"Rejected complete lines: {rejected}")
    print(f"Trailing partial bytes: {len(pending)}")
    print(f"Raw data: {raw_path}")
    print(f"Reception events: {events_path}")
    print(f"Session details: {metadata_path}")

    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
