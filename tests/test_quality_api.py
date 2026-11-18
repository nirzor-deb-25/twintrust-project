from contextlib import closing
"""HTTP integration tests; install httpx to run (no broker required)."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HAS_API = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'paho'))


@unittest.skipUnless(HAS_API, 'Requires fastapi, paho-mqtt and httpx')
class QualityApiTests(unittest.TestCase):
    def setUp(self):
        from backend import app as backend
        from fastapi.testclient import TestClient
        self.backend = backend
        self.folder = tempfile.TemporaryDirectory()
        self.old_database = backend.DATABASE
        backend.DATABASE = Path(self.folder.name) / 'test.sqlite3'
        self.was_connected = backend.connected.is_set()
        self.was_subscribed = backend.subscribed.is_set()
        backend.initialize_database()
        backend.connected.set(); backend.subscribed.set()
        self.client = TestClient(backend.app)  # No lifespan: no MQTT connection.

    def tearDown(self):
        self.client.close()
        self.backend.DATABASE = self.old_database
        self.backend.connected.set() if self.was_connected else self.backend.connected.clear()
        self.backend.subscribed.set() if self.was_subscribed else self.backend.subscribed.clear()
        self.folder.cleanup()

    def insert(self, payload, age=0):
        received = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        with closing(self.backend.database_connection()) as c:
            c.execute('INSERT INTO mqtt_messages(received_utc,topic,qos,retained,duplicate_flag,payload) VALUES (?,?,1,0,0,?)',
                      (received, self.backend.TOPIC, payload))
            c.commit()

    def test_stale_and_raw_preserved(self):
        from test_quality import sample
        payload = json.dumps(sample()).encode()
        self.insert(payload, age=10)
        response = self.client.get('/quality/latest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'stale')
        self.assertEqual(response.json()['raw_decision'], 'withheld')
        self.assertEqual(self.client.get('/latest').json()['raw_payload'], payload.decode())
        self.assertEqual(self.client.get('/history').json()['count'], 1)

    def test_reboot_boundary_and_disconnection(self):
        from test_quality import sample
        self.insert(json.dumps(sample(100)).encode(), age=2)
        self.insert(json.dumps(sample(0, 23)).encode())
        self.assertEqual(self.client.get('/quality/latest').json()['accepted_messages'], 1)
        reset = self.client.get('/quality/latest?after_id=1').json()
        self.assertEqual(reset['latest_accepted']['seq'], 0)
        self.assertEqual(reset['raw_decision'], 'warning')
        self.backend.connected.clear()
        self.assertEqual(self.client.get('/quality/latest?after_id=1').json()['raw_decision'], 'withheld')
        self.assertEqual(self.client.get('/quality/latest?after_id=-1').status_code, 422)

    def test_bad_json_and_empty(self):
        self.assertEqual(self.client.get('/quality/latest').json()['status'], 'unavailable')
        self.insert(b'not JSON')
        data = self.client.get('/quality/latest').json()
        self.assertEqual(data['accepted_messages'], 0)
        self.assertEqual(data['quality_counts']['parse_error'], 1)
        self.assertEqual(data['raw_decision'], 'withheld')
