import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
import app
import auth
import sales


def pricing_fixture():
    # Fictional values for isolated tests only, never installed in the user's database.
    return dict(version=0, currency='TWD', margin='20', validity_days='14', per_km='10', charter_day='1000',
                terms='Test terms: taxes excluded; transport only.',
                routes=[dict(id='north', name='Test route', origin='Taipei', destination='Taichung', base='1000', max_kg='1000', max_cbm='10')],
                cargo=[dict(id='general', name='General freight', generic=True, per_kg='2', per_cbm='10', handling='50', max_kg='500', max_cbm='5'),
                       dict(id='cold', name='Temperature controlled', generic=False, per_kg='4', per_cbm='20', handling='100', max_kg='500', max_cbm='5')])


def specs_fixture(**changes):
    data = dict(customer='Test Client', email='client@example.com', description='Boxed parts', origin='Taipei', destination='Taichung',
                route_id='north', cargo_id='general', weight_kg='100', volume_cbm='2', packages='10',
                pickup=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat(), eta=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat(),
                requirements='', outbound_km='0', return_km='0', charter_days='0', tolls='0', fuel='0', other='0', extras_note='')
    return dict(data, **changes)


class SalesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = app.DB
        app.DB = Path(self.temp.name)/'workflow.sqlite3'
        app.init_db()
        self.users = {}
        with app.connect() as db:
            for role in ('Admin', 'Sales', 'Operations', 'Viewer'):
                auth.add_user(db, dict(username=role.lower(), name=role, role=role, password='Test-only-password'))
                self.users[role] = auth.public(db.execute('SELECT * FROM users WHERE username=?', (role.lower(),)).fetchone())
        self.call('/api/pricing', pricing_fixture(), 'Admin')

    def tearDown(self):
        app.DB = self.old_db
        self.temp.cleanup()

    def call(self, path, data, role='Sales'):
        with app.connect() as db:
            return sales.action(db, path, data, self.users[role], app.create)

    def quote(self, **changes):
        result = self.call('/api/quotes', specs_fixture(**changes))
        return self.get(result['id'])

    def get(self, qid):
        with app.connect() as db:
            return sales.decode(db.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone())

    def action(self, q, name, role='Sales', **kwargs):
        self.call('/api/quotes/'+name, dict(id=q['id'], version=q['version'], **kwargs), role)
        return self.get(q['id'])

    def test_standard_price_snapshot_and_handoff(self):
        q = self.quote()
        self.assertEqual(q['status'], 'Proposal ready')
        self.assertEqual(q['pricing']['cost'], '1270.00')
        self.assertEqual(q['pricing']['total'], '1587.50')
        with app.connect() as db:
            p = sales.proposal(db, q['id'], self.users['Sales'])
            self.assertFalse(any(k in p for k in ('margin', 'profit', 'cost', 'lines')))
        changed = dict(pricing_fixture(), version=1, margin='50')
        self.call('/api/pricing', changed, 'Admin')
        self.assertEqual(self.get(q['id'])['pricing']['total'], '1587.50')
        with self.assertRaises(ValueError):
            self.action(q, 'close', acceptance='PO 1', confirmed=True)
        q = self.action(q, 'sent', note='Email to customer')
        q = self.action(q, 'close', acceptance='PO 1 / approved by buyer', confirmed=True)
        self.assertEqual(q['status'], 'Closed won')
        self.assertIsNotNone(q['shipment_id'])
        with self.assertRaises(ValueError):
            self.action(q, 'close', acceptance='PO 1', confirmed=True)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shipments').fetchone()[0], 1)
            notices = sales.state(db, self.users['Operations'])['notices']
            self.assertEqual(len(notices), 1)
            self.assertIn('deal confirmed', notices[0]['message'])
            self.assertIn('@sales', db.execute('SELECT actor FROM events').fetchone()[0])

    def test_custom_roundtrip_charter_and_review(self):
        q = self.quote(route_id='', destination='Custom port', cargo_id='cold', outbound_km='100', return_km='80', charter_days='2', tolls='50')
        self.assertEqual(q['status'], 'Operations review')
        self.assertEqual(q['pricing']['cost'], '4390.00')
        self.assertEqual(q['pricing']['total'], '5487.50')
        with app.connect() as db:
            with self.assertRaises(ValueError):
                sales.proposal(db, q['id'], self.users['Sales'])
        with self.assertRaises(auth.AccessError):
            self.action(q, 'review', decision='approve', note='Attempted Sales bypass', lines=[dict(label='All costs', amount='1')])
        q = self.action(q, 'review', 'Operations', decision='approve', note='Carrier confirmed all costs and capacity', lines=[dict(label='Verified total', amount='4400')])
        self.assertEqual(q['status'], 'Proposal ready')
        self.assertEqual(q['pricing']['total'], '5500.00')
        q = self.action(q, 'sent', note='Sent approved proposal')
        q = self.action(q, 'close', acceptance='Buyer approval', confirmed=True)
        self.assertEqual(q['status'], 'Closed won')

    def test_limits_special_requirements_and_route_tampering(self):
        for change in [dict(weight_kg='501'), dict(volume_cbm='6'), dict(requirements='Crane unloading'), dict(fuel='10'), dict(cargo_id='cold')]:
            self.assertEqual(self.quote(**change)['status'], 'Operations review')
        with self.assertRaises(ValueError):
            self.quote(destination='Different city')
        for bad in ('NaN', 'Infinity', '-1'):
            with self.assertRaises(ValueError):
                self.quote(weight_kg=bad)

    def test_changes_requested_revision_and_conflict(self):
        q = self.quote(cargo_id='cold')
        returned = self.action(q, 'review', 'Operations', decision='return', note='Need refrigeration details')
        self.assertEqual(returned['status'], 'Changes requested')
        with self.assertRaises(auth.AccessError):
            self.action(q, 'review', 'Operations', decision='return', note='Stale action')
        revised = self.action(returned, 'revise', **specs_fixture(cargo_id='cold', requirements='Maintain 4C'))
        self.assertEqual(revised['status'], 'Operations review')
        with app.connect() as db:
            self.assertEqual(len(sales.state(db, self.users['Sales'])['notices']), 1)

    def test_roles_and_owner_isolation(self):
        for role in ('Sales', 'Operations', 'Viewer'):
            with self.assertRaises(auth.AccessError):
                self.call('/api/pricing', dict(pricing_fixture(), version=1), role)
        for role in ('Operations', 'Viewer'):
            with self.assertRaises(auth.AccessError):
                self.call('/api/quotes', specs_fixture(), role)
        q = self.quote()
        with app.connect() as db:
            auth.add_user(db, dict(username='sales2', name='Sales 2', role='Sales', password='Test-only-password'))
            other = auth.public(db.execute('SELECT * FROM users WHERE username="sales2"').fetchone())
            self.assertEqual(sales.state(db, other)['quotes'], [])
            with self.assertRaises(auth.AccessError):
                sales.proposal(db, q['id'], other)

    def test_expiry_zero_cost_and_notifications_read(self):
        q = self.quote()
        with app.connect() as db:
            db.execute('UPDATE quotes SET valid_until=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(), q['id']))
        with self.assertRaises(ValueError):
            self.action(q, 'sent', note='Expired')
        q = self.quote(cargo_id='cold')
        with self.assertRaises(ValueError):
            self.action(q, 'review', 'Operations', decision='approve', note='No costs', lines=[dict(label='Zero', amount='0')])
        with app.connect() as db:
            notices = sales.state(db, self.users['Operations'])['notices']
        self.call('/api/notices/read', dict(id=notices[0]['id']), 'Operations')
        with app.connect() as db:
            self.assertTrue(sales.state(db, self.users['Operations'])['notices'][0]['is_read'])
        with self.assertRaises(auth.AccessError):
            self.call('/api/notices/read', dict(id=notices[0]['id']))

    def test_tiny_standard_rate_cannot_create_zero_price_proposal(self):
        cfg = pricing_fixture()
        cfg.update(version=1)
        cfg['routes'][0]['base'] = '0.001'
        cfg['cargo'][0].update(per_kg='0', per_cbm='0', handling='0')
        self.call('/api/pricing', cfg, 'Admin')
        with self.assertRaises(ValueError):
            self.quote()

    def test_past_pickup_cannot_be_sent(self):
        q = self.quote()
        with app.connect() as db:
            s = q['specs']
            s['pickup'] = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
            db.execute('UPDATE quotes SET specs=? WHERE id=?', (json.dumps(s), q['id']))
        with self.assertRaises(ValueError):
            self.action(q, 'sent', note='Cannot promise a past pickup')

    def test_malformed_review_line_is_validation_error(self):
        q = self.quote(cargo_id='cold')
        with self.assertRaises(ValueError):
            self.action(q, 'review', 'Operations', decision='approve', note='Invalid input', lines=[None])

    def test_fractional_validity_rejected(self):
        with self.assertRaises(ValueError):
            self.call('/api/pricing', dict(pricing_fixture(), version=1, validity_days='1.5'), 'Admin')

    def test_concurrent_close_creates_one_shipment(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        q = self.action(self.quote(), 'sent', note='Sent to client')
        barrier = threading.Barrier(2)
        def close_once():
            barrier.wait()
            try:
                self.action(q, 'close', acceptance='PO-TEST', confirmed=True)
                return 'closed'
            except auth.AccessError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: close_once(), range(2)))
        self.assertCountEqual(outcomes, ['closed', 409])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shipments').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM team_notices').fetchone()[0], 1)

    def test_handoff_failure_rolls_back_shipment_and_quote(self):
        q = self.action(self.quote(), 'sent', note='Sent to client')
        def failed_create(db, data, user):
            app.create(db, data, user)
            raise RuntimeError('Simulated failure after insert')
        with self.assertRaises(RuntimeError):
            with app.connect() as db:
                sales.action(db, '/api/quotes/close', dict(id=q['id'], version=q['version'], acceptance='PO', confirmed=True), self.users['Sales'], failed_create)
        self.assertEqual(self.get(q['id'])['status'], 'Awaiting client')
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shipments').fetchone()[0], 0)

    def test_unconfigured_pricing_blocks_quotes(self):
        with app.connect() as db:
            db.execute('UPDATE pricing_config SET version=0 WHERE id=1')
        with self.assertRaises(ValueError):
            self.quote()


if __name__ == '__main__':
    unittest.main()
