import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
import app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = app.DB
        app.DB = Path(self.temp.name) / 'test.sqlite3'
        app.init_db()

    def tearDown(self):
        app.DB = self.original
        self.temp.cleanup()

    def shipment(self, hours=5):
        data = dict(reference='test-001', customer='Test customer', email='test@example.com',
                    origin='Taipei', destination='Tainan', carrier='Test carrier', tracking='T001',
                    eta=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat())
        app.mutate('/api/shipments', data)
        return app.snapshot()['shipments'][0]

    def update(self, s, status=None, eta=None, note=''):
        app.mutate('/api/update', dict(id=s['id'], status=status or s['status'], eta=eta or s['eta'], note=note))

    def test_booking_persists_after_database_reopen(self):
        s = self.shipment()
        app.init_db()
        state = app.snapshot()
        self.assertEqual(state['shipments'][0]['reference'], 'TEST-001')
        self.assertEqual(len(state['notifications']), 1)
        self.assertEqual(state['notifications'][0]['shipment_id'], s['id'])

    def test_repeat_status_does_not_duplicate_notification(self):
        s = self.shipment()
        self.update(s, 'In transit')
        self.update(s, 'In transit')
        self.assertEqual(len(app.snapshot()['notifications']), 2)

    def test_overdue_dedup_and_delivery_resolution(self):
        s = self.shipment(-2)
        app.snapshot()
        state = app.snapshot()
        self.assertEqual(len(state['alerts']), 1)
        self.assertEqual(len(state['notifications']), 2)
        self.update(s, 'Delivered')
        state = app.snapshot()
        self.assertTrue(state['alerts'][0]['resolved'])
        self.assertEqual(len(state['notifications']), 3)

    def test_delay_recurs_after_eta_change(self):
        s = self.shipment(-2)
        future = (datetime.now(timezone.utc)+timedelta(hours=4)).isoformat()
        self.update(s, eta=future)
        self.assertTrue(app.snapshot()['alerts'][0]['resolved'])
        self.update(s)
        self.assertIsNone(app.snapshot()['alerts'][0]['resolved'])
        self.assertEqual(len(app.snapshot()['alerts']), 1)

    def test_exception_resolves_and_note_stays_internal(self):
        s = self.shipment()
        self.update(s, 'Exception', note='Internal investigation')
        self.assertEqual(app.snapshot()['alerts'][0]['kind'], 'exception')
        self.update(s, 'In transit')
        state = app.snapshot()
        self.assertTrue(state['alerts'][0]['resolved'])
        self.assertTrue(any('Internal investigation' in e['message'] for e in state['events']))
        self.assertFalse(any('Internal investigation' in n['body'] for n in state['notifications']))

    def test_invalid_update_rolls_back(self):
        s = self.shipment()
        with self.assertRaises(ValueError):
            self.update(s, 'Invalid')
        with self.assertRaises(ValueError):
            self.update(s, eta='2026-01-01T12:00')
        state = app.snapshot()
        self.assertEqual(state['shipments'][0]['status'], 'Booked')
        self.assertEqual(len(state['notifications']), 1)

    def test_duplicate_reference_is_rejected(self):
        import sqlite3
        self.shipment()
        with self.assertRaises(sqlite3.IntegrityError):
            self.shipment()
        self.assertEqual(len(app.snapshot()['shipments']), 1)


if __name__ == '__main__':
    unittest.main()
