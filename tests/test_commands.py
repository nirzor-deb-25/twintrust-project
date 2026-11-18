import sys
import unittest
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from command_support import CommandBridge, validate

ID = 'a' * 32
class Port:
    def __init__(self): self.writes = []
    def write(self, data): self.writes.append(data); return len(data)

class Commands(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.results = []
        self.bridge = CommandBridge(self.results.append, lambda: self.now)
        self.port = Port()
        self.item = dict(command_id=ID, command='led', value=1)
    def receive(self, **kw): self.bridge.receive(json.dumps(self.item).encode(), **kw)
    def test_requires_board_ack(self):
        self.receive(); self.bridge.tick(self.port)
        self.assertEqual(self.results[-1]['status'], 'sent')
        self.assertEqual(self.port.writes, [f'TT1 {ID} LED 1\n'.encode()])
        ack = dict(device='discovery01', type='command_ack', command_id=ID, status='applied', led=1)
        self.bridge.acknowledge(dict(ack, command_id='b'*32))
        self.assertIsNotNone(self.bridge.active)
        self.bridge.acknowledge(dict(ack, led=0))
        self.assertIsNotNone(self.bridge.active)
        self.bridge.acknowledge(ack)
        self.assertEqual(self.results[-1]['status'], 'applied')
    def test_timeout_does_not_retry(self):
        self.receive(); self.bridge.tick(self.port); self.now = 5; self.bridge.tick(self.port)
        self.assertEqual(self.results[-1]['status'], 'timeout')
        self.receive(); self.bridge.tick(self.port)
        self.assertEqual(self.results[-1]['status'], 'duplicate_ignored')
        self.assertEqual(len(self.port.writes), 1)
    def test_retained_and_invalid_ignored(self):
        self.receive(retained=True); self.bridge.receive(b'bad'); self.bridge.tick(self.port)
        self.assertFalse(self.port.writes)
        for value in (True, 2, '1', 1.0):
            with self.assertRaises(ValueError): validate(dict(self.item, value=value))
    def test_expired_and_conflict(self):
        self.receive(); self.now=3; self.bridge.tick(self.port)
        self.assertEqual(self.results[-1]['status'], 'expired_before_send')
        self.item['value']=0; self.receive(); self.bridge.tick(self.port)
        self.assertEqual(self.results[-1]['status'], 'id_conflict')
        self.assertFalse(self.port.writes)
    def test_shutdown(self):
        self.receive(); self.bridge.tick(self.port); self.bridge.stop()
        self.assertEqual(self.results[-1]['status'], 'gateway_stopped')
    def test_queue_bounded(self):
        for _ in range(30): self.receive()
        self.assertEqual(self.bridge.queue.qsize(), 16)

if __name__ == '__main__': unittest.main()
