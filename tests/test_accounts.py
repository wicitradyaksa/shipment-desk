import json
import threading
import http.client
import auth
import app
import unittest
import test_workflow


class AccountTests(unittest.TestCase):
    shipment = test_workflow.WorkflowTests.shipment

    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        test_workflow.WorkflowTests.tearDown(self)

    def request(self, path, data=None, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        connection.request('POST' if data is not None else 'GET', path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), response.getheader('Set-Cookie')
        connection.close()
        return result

    def admin(self):
        data = dict(name='Test admin', username='admin', password='Test-password-123')
        self.assertEqual(self.request('/api/setup', data)[0], 200)
        self.assertEqual(self.request('/api/setup', data)[0], 403)
        return self.request('/api/login', data)[2].split(';')[0]

    def test_permissions_and_revocation(self):
        self.shipment()
        self.assertEqual(self.request('/api/state')[0], 401)
        cookie = self.admin()
        for role in ('Viewer', 'Operations'):
            data = dict(name=role, username=role.lower(), password='Test-password-456', role=role)
            self.assertEqual(self.request('/api/users', data, cookie)[0], 200)
            staff_cookie = self.request('/api/login', data)[2].split(';')[0]
            self.assertEqual(self.request('/api/state', cookie=staff_cookie)[0], 200)
            self.assertEqual(self.request('/api/users', cookie=staff_cookie)[0], 403)
            s = app.snapshot()['shipments'][0]
            update = dict(id=s['id'], status='Delivered', eta=s['eta'], version=s['version'])
            self.assertEqual(self.request('/api/update', update, staff_cookie)[0], 403 if role == 'Viewer' else 200)
            if role == 'Operations':
                self.assertEqual(self.request('/api/update', update, staff_cookie)[0], 409)
                self.assertIn('@operations', app.snapshot()['events'][0]['actor'])
            user = next(u for u in self.request('/api/users', cookie=cookie)[1]['users'] if u['role'] == role)
            self.assertEqual(self.request('/api/users/access', dict(id=user['id'], role=role, active=False), cookie)[0], 200)
            self.assertEqual(self.request('/api/state', cookie=staff_cookie)[0], 401)
        self.assertEqual(self.request('/api/logout', {}, cookie)[0], 200)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 401)

    def test_password_change_and_hash(self):
        cookie = self.admin()
        with app.connect() as db:
            stored = db.execute('SELECT password FROM users').fetchone()[0]
            self.assertNotEqual(stored, 'Test-password-123')
            self.assertTrue(auth.verify('Test-password-123', stored))
        self.assertEqual(self.request('/api/password', dict(current_password='wrong', password='New-password-123'), cookie)[0], 400)
        self.assertEqual(self.request('/api/password', dict(current_password='Test-password-123', password='New-password-123'), cookie)[0], 200)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 401)
        self.assertEqual(self.request('/api/login', dict(username='admin', password='New-password-123'))[0], 200)

    def test_sales_http_routes_and_permissions(self):
        from test_sales import pricing_fixture, specs_fixture
        admin_cookie = self.admin()
        self.assertEqual(self.request('/api/pricing', pricing_fixture(), admin_cookie)[0], 200)
        data = dict(name='Sales rep', username='sales', password='Test-password-789', role='Sales')
        self.assertEqual(self.request('/api/users', data, admin_cookie)[0], 200)
        cookie = self.request('/api/login', data)[2].split(';')[0]
        self.assertEqual(self.request('/api/pricing', dict(pricing_fixture(), version=1), cookie)[0], 403)
        response = self.request('/api/quotes', specs_fixture(), cookie)
        self.assertEqual(response[0], 200)
        qid = response[1]['id']
        self.assertEqual(self.request('/api/proposal/'+qid, cookie=cookie)[0], 200)
        workflow = self.request('/api/workflow', cookie=cookie)
        self.assertEqual(workflow[0], 200)
        self.assertEqual(workflow[1]['quotes'][0]['status'], 'Proposal ready')
        self.assertEqual(self.request('/api/shipments', {}, cookie)[0], 403)
        self.assertEqual(self.request('/api/workflow')[0], 401)

    def test_large_pricing_configuration(self):
        from test_sales import pricing_fixture
        cookie = self.admin()
        cfg = pricing_fixture()
        template = cfg['routes'][0]
        cfg['routes'] = [dict(template, id=f'route-{i}', name='Route ' + str(i) + 'x'*90) for i in range(100)]
        self.assertGreater(len(json.dumps(cfg)), 20000)
        self.assertEqual(self.request('/api/pricing', cfg, cookie)[0], 200)
