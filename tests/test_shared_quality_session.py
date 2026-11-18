"""Shared session integration tests. Uses a temporary DB, no MQTT connection."""
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class SharedQualitySessionTests(unittest.TestCase):
    def setUp(self):
        from backend import app as backend
        from fastapi.testclient import TestClient
        self.backend = backend
        self.temp = tempfile.TemporaryDirectory()
        self.database_patch = patch.object(backend, 'DATABASE', Path(self.temp.name) / 'test.sqlite3')
        self.database_patch.start()
        self.flags = (backend.connected.is_set(), backend.subscribed.is_set())
        backend.initialize_database()
        backend.connected.set()
        backend.subscribed.set()
        self.clients = [TestClient(backend.app), TestClient(backend.app)]

    def tearDown(self):
        for client in self.clients:
            client.close()
        for flag, value in zip((self.backend.connected, self.backend.subscribed), self.flags):
            flag.set() if value else flag.clear()
        self.database_patch.stop()
        self.temp.cleanup()

    def insert(self, seq, temperature=20, age=0):
        record = dict(device='discovery01', seq=seq, uptime_ms=seq * 1000,
                      valid=True, temperature_c=temperature, humidity_percent=50,
                      pressure_hpa=1016, error_count=0)
        with closing(self.backend.database_connection()) as connection:
            cursor = connection.execute('''INSERT INTO mqtt_messages
                (received_utc, topic, qos, retained, duplicate_flag, payload)
                VALUES (?, ?, 1, 0, 0, ?)''',
                ((datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(), self.backend.TOPIC, json.dumps(record).encode()))
            connection.commit()
            return cursor.lastrowid

    def test_all_visitors_and_reload_use_saved_boundary(self):
        first = self.insert(100)
        self.backend.save_quality_session(first)
        self.insert(0, 23)
        for client in self.clients:
            for _ in range(2):
                data = client.get('/quality/latest').json()
                self.assertEqual(data['after_id'], first)
                self.assertEqual(data['boundary_source'], 'saved_session')
                self.assertEqual(data['latest_accepted']['seq'], 0)
                self.assertEqual(data['status'], 'ready')

    def test_database_reinitialization_keeps_boundary_and_raw_messages(self):
        old = self.insert(100)
        before = self.clients[0].get('/history').json()
        self.backend.save_quality_session(old)
        self.backend.initialize_database()
        self.assertEqual(self.clients[0].get('/quality/session').json()['after_id'], old)
        self.assertEqual(self.clients[0].get('/history').json(), before)
        self.assertEqual(self.clients[0].get('/quality/latest').json()['status'], 'unavailable')

    def test_capture_current_last_id_and_future_readings(self):
        self.insert(100)
        last = self.insert(101)
        self.assertEqual(self.backend.save_quality_session()['after_id'], last)
        self.insert(0)
        data = self.clients[1].get('/quality/latest').json()
        self.assertEqual(data['after_id'], last)
        self.assertEqual(data['checked_messages'], 1)
        self.assertEqual(data['latest_accepted']['seq'], 0)

    def test_manual_query_does_not_change_shared_setting(self):
        old = self.insert(100)
        self.backend.save_quality_session(old)
        self.insert(0)
        explicit = self.clients[0].get('/quality/latest?after_id=0').json()
        self.assertEqual(explicit['after_id'], 0)
        self.assertEqual(explicit['boundary_source'], 'explicit_query')
        self.assertEqual(self.clients[1].get('/quality/latest').json()['after_id'], old)
        self.assertEqual(self.clients[0].get('/quality/latest?after_id=-1').status_code, 422)

    def test_invalid_boundary_does_not_replace_valid_setting(self):
        current = self.insert(100)
        self.backend.save_quality_session(current)
        for value in (-1, True, 1.5, '1', current + 1, 9007199254740992):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.backend.save_quality_session(value)
        self.assertEqual(self.clients[0].get('/quality/session').json()['after_id'], current)

    def test_later_restart_is_not_automatically_accepted(self):
        old = self.insert(100)
        self.backend.save_quality_session(old)
        self.insert(50, age=10)
        self.insert(0)
        data = self.clients[0].get('/quality/latest').json()
        self.assertEqual(data['latest_issue'], 'backward_or_nonincreasing_counters')
        self.assertNotEqual(data['status'], 'ready')
        self.assertEqual(data['raw_decision'], 'withheld')

    def test_disconnection_withholds_decisions(self):
        self.insert(1)
        self.backend.connected.clear()
        data = self.clients[0].get('/quality/latest').json()
        self.assertEqual(data['status'], 'mqtt_disconnected')
        self.assertEqual(data['raw_decision'], 'withheld')

    def test_no_public_write_endpoint(self):
        self.assertEqual(self.clients[0].post('/quality/session', json={'after_id': 5}).status_code, 405)


if __name__ == '__main__':
    unittest.main()
