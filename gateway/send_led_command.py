"""Send one local MQTT command and wait for the matching board result."""
import argparse
import json
import threading
import uuid
import paho.mqtt.client as mqtt
from command_support import COMMAND_TOPIC, RESULT_TOPIC


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('value', type=int, choices=(0, 1))
    args = parser.parse_args()
    item = dict(command_id=uuid.uuid4().hex, command='led', value=args.value)
    subscribed, done = threading.Event(), threading.Event()
    final = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='led-' + item['command_id'])

    def on_connect(c, u, f, reason, properties):
        if reason == 0:
            c.subscribe(RESULT_TOPIC, qos=1)

    def on_subscribe(c, u, mid, reasons, properties):
        if reasons and all(not r.is_failure for r in reasons):
            subscribed.set()

    def on_message(c, u, message):
        try:
            result = json.loads(message.payload)
        except (ValueError, UnicodeError):
            return
        if isinstance(result, dict) and result.get('command_id') == item['command_id']:
            print(json.dumps(result, indent=2), flush=True)
            if result.get('status') != 'sent':
                final.append(result['status'])
                done.set()

    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    try:
        client.connect('127.0.0.1', 1883, 60)
        client.loop_start()
        if not subscribed.wait(5):
            raise RuntimeError('Result subscription not confirmed')
        print('Command ID: ' + item['command_id'], flush=True)
        info = client.publish(COMMAND_TOPIC, json.dumps(item), qos=1, retain=False)
        info.wait_for_publish(3)
        if not info.is_published():
            raise RuntimeError('Broker acknowledgement missing; board result unknown')
        if not done.wait(10):
            raise RuntimeError('No final result: check gateway is running. Board result unknown.')
        return 0 if final == ['applied'] else 1
    except (OSError, RuntimeError) as exc:
        print(str(exc))
        return 1
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == '__main__':
    raise SystemExit(main())
