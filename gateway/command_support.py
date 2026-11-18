"""Bounded LED command protocol; all serial operations run in the gateway thread."""
import json
import re
import time
from collections import OrderedDict
from queue import Queue, Empty, Full

COMMAND_TOPIC = 'twintrust/discovery01/commands'
RESULT_TOPIC = 'twintrust/discovery01/command_results'


def validate(record):
    if not isinstance(record, dict) or set(record) != {'command_id', 'command', 'value'}:
        raise ValueError('Expected command_id, command and value only')
    if not isinstance(record['command_id'], str) or not re.fullmatch('[0-9a-f]{32}', record['command_id']):
        raise ValueError('command_id must be 32 lowercase hexadecimal characters')
    if record['command'] != 'led' or type(record['value']) is not int or record['value'] not in (0, 1):
        raise ValueError('Only led with integer value 0 or 1 is supported')
    return dict(record)


class CommandBridge:
    def __init__(self, emit, clock=time.monotonic):
        self.emit, self.clock = emit, clock
        self.queue = Queue(maxsize=16)
        self.active = None
        self.seen = OrderedDict()

    def receive(self, payload, retained=False):
        # MQTT callback: no serial I/O, GPIO work or blocking network waits.
        if retained or len(payload) > 512:
            return
        try:
            item = validate(json.loads(payload))
            self.queue.put_nowait((self.clock(), item))
        except (ValueError, UnicodeError, Full):
            return

    def result(self, item, status, **extra):
        self.emit(dict(device='discovery01', command_id=item['command_id'],
                       command='led', value=item['value'], status=status, **extra))

    def tick(self, port):
        now = self.clock()
        if self.active and now - self.active[0] >= 5:
            self.result(self.active[1], 'timeout', detail='Board result unknown; no automatic retry')
            self.active = None
        if self.active:
            return
        try:
            received, item = self.queue.get_nowait()
        except Empty:
            return
        key = item['command_id']
        if key in self.seen:
            self.result(item, 'duplicate_ignored' if self.seen[key] == item['value'] else 'id_conflict')
            return
        self.seen[key] = item['value']
        if len(self.seen) > 1024:
            self.seen.popitem(last=False)
        if now - received > 2:
            self.result(item, 'expired_before_send')
            return
        wire = f"TT1 {key} LED {item['value']}\n".encode('ascii')
        try:
            if port.write(wire) != len(wire):
                raise OSError('Partial serial write')
        except OSError as exc:
            self.result(item, 'serial_error', detail=str(exc))
            return
        self.active = (self.clock(), item)
        self.result(item, 'sent', detail='Waiting for board acknowledgement')

    def acknowledge(self, record):
        if record.get('device') != 'discovery01' or record.get('type') != 'command_ack':
            return False
        if self.active and record.get('command_id') == self.active[1]['command_id']:
            item = self.active[1]
            status = record.get('status')
            if status == 'applied' and type(record.get('led')) is int and record['led'] == item['value']:
                self.result(item, 'applied', board_ack=record)
                self.active = None
            elif status == 'id_conflict':
                self.result(item, 'id_conflict', board_ack=record)
                self.active = None
        return True

    def stop(self):
        if self.active:
            self.result(self.active[1], 'gateway_stopped', detail='Board result unknown')
            self.active = None
        while True:
            try:
                _, item = self.queue.get_nowait()
                self.result(item, 'cancelled_before_send')
            except Empty:
                break
