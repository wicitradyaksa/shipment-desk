import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
import app
import auth
import backups
import run_host
import workspace
import test_billing
import test_accounts


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.base = test_billing.BillingTests()
        self.base.setUp()
        self.users = self.base.users

    def tearDown(self):
        self.base.tearDown()

    def queue(self, role):
        with app.connect() as db:
            return workspace.state(db, self.users[role])

    def test_sales_isolation_and_role_scopes(self):
        q = self.base.quote()
        self.assertEqual(self.queue('Sales')['items'][0]['record_id'], q['id'])
        other = dict(self.users['Sales'], id=-1)
        with app.connect() as db:
            self.assertEqual(workspace.state(db, other)['items'], [])
            self.assertEqual(workspace.state(db, dict(other, role='Viewer'))['items'], [])
            with self.assertRaises(auth.AccessError): workspace.state(db, None)
        self.assertFalse(any(i['team']=='Sales' for i in self.queue('Operations')['items']))

    def test_handoffs_costs_and_approval_follow_source(self):
        self.base.prepare()
        ops = self.queue('Operations')['items']
        self.assertTrue(any(i['title']=='Complete costs and supporting documents' for i in ops))
        self.base.submit_costs()
        self.assertFalse(any(i['view']=='billing' for i in self.queue('Operations')['items']))
        self.assertTrue(any(i['title']=='Review billing approval' for i in self.queue('Admin')['items']))
        self.base.approve()
        items=self.queue('Admin')['items']
        self.assertFalse(any(i['title']=='Review billing approval' for i in items))
        self.assertTrue(any(i['title']=='Prepare customer invoice' for i in items))
        self.assertEqual(len({i['id'] for i in items}), len(items))
        self.assertEqual(items, self.queue('Admin')['items'])

    def test_disabled_owner_becomes_shared_and_overdue_is_urgent(self):
        self.base.prepare()
        with app.connect() as db:
            db.execute("UPDATE shipments SET status='Booked',eta='2000-01-01T00:00:00+00:00' WHERE id=?", (self.base.sid,))
            db.execute('INSERT INTO dispatch_plans(shipment_id,owner_id) VALUES (?,?)', (self.base.sid,self.users['Operations']['id']))
        item=next(i for i in self.queue('Admin')['items'] if i['view']=='dispatch')
        self.assertEqual(item['owner_id'],self.users['Operations']['id'])
        self.assertTrue(item['urgent'])
        with app.connect() as db: db.execute('UPDATE users SET active=0 WHERE id=?',(self.users['Operations']['id'],))
        self.assertIsNone(next(i for i in self.queue('Admin')['items'] if i['view']=='dispatch')['owner_id'])

    def test_cost_extra_decision_visible_only_to_sales_owner(self):
        self.base.prepare()
        self.base.bill('extras', lines=[dict(label='Waiting',amount='12')])
        self.assertTrue(any(i['view']=='billing' for i in self.queue('Sales')['items']))
        self.base.bill('decision','Sales',index=0,decision='Approved',reference='Approval',confirmed=True)
        self.assertFalse(any(i['view']=='billing' for i in self.queue('Sales')['items']))


class HostTests(unittest.TestCase):
    def test_live_backup_integrity_and_daily_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'live.sqlite3'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE evidence (content BLOB)')
                db.execute('INSERT INTO evidence VALUES (?)',(b'proof of delivery',));db.commit()
                first=backups.snapshot(source,root/'backups',daily=True)
                self.assertEqual(first,backups.snapshot(source,root/'backups',daily=True))
                with closing(sqlite3.connect(first)) as restored:
                    self.assertEqual(restored.execute('PRAGMA quick_check').fetchone()[0],'ok')
                    self.assertEqual(restored.execute('SELECT content FROM evidence').fetchone()[0],b'proof of delivery')
                db.execute('INSERT INTO evidence VALUES (?)',(b'new uncommitted record',))
                second=backups.snapshot(source,root/'backups')
                with closing(sqlite3.connect(second)) as restored:
                    self.assertEqual(restored.execute('SELECT count(*) FROM evidence').fetchone()[0],1)

    def test_private_network_requires_certificate_and_safe_bind(self):
        self.assertEqual(run_host.arguments({}),['--host','127.0.0.1','--port','8879'])
        for host in ('0.0.0.0','8.8.8.8','224.0.0.1','192.168.1.5','100.80.1.5','::1'):
            with self.subTest(host=host),self.assertRaises(ValueError):run_host.arguments(dict(host=host))
        with self.assertRaises(ValueError):run_host.arguments(dict(port=True))


class WorkspaceHTTPTests(unittest.TestCase):
    def test_endpoint_requires_login_and_hides_host_details_from_operations(self):
        fixture=test_accounts.AccountTests()
        fixture.setUp()
        try:
            self.assertEqual(fixture.request('/api/workspace')[0],401)
            fixture.request('/api/setup',dict(name='Admin',username='admin',password='Test-password-123'))
            _,_,cookie=fixture.request('/api/login',dict(username='admin',password='Test-password-123'))
            status,data,_=fixture.request('/api/workspace',cookie=cookie)
            self.assertEqual(status,200)
            self.assertIn('host',data)
            fixture.request('/api/users',dict(name='Ops',username='ops',password='Test-password-123',role='Operations'),cookie=cookie)
            _,_,ops_cookie=fixture.request('/api/login',dict(username='ops',password='Test-password-123'))
            status,data,_=fixture.request('/api/workspace',cookie=ops_cookie)
            self.assertEqual(status,200)
            self.assertNotIn('host',data)
        finally:
            fixture.tearDown()
