import base64
import http.client
import json
from datetime import datetime, timezone, timedelta
import unittest
import app
import auth
import dispatch
import test_accounts


class DispatchTests(unittest.TestCase):
    setUp = test_accounts.AccountTests.setUp
    tearDown = test_accounts.AccountTests.tearDown
    shipment = test_accounts.AccountTests.shipment
    request = test_accounts.AccountTests.request
    admin = test_accounts.AccountTests.admin

    def prepare(self):
        self.s = self.shipment()
        self.cookie = self.admin()
        with app.connect() as db:
            self.user = auth.public(db.execute('SELECT * FROM users').fetchone())

    def current(self):
        return self.request('/api/dispatch', cookie=self.cookie)[1]['shipments'][0]

    def act(self, action, **extra):
        s = self.current()
        return self.request('/api/dispatch/'+action, dict(shipment_id=s['id'],version=s['plan']['version'],shipment_version=s['version'],**extra),self.cookie)

    def upload(self, **extra):
        return self.request('/api/documents/upload',dict(shipment_id=self.s['id'],name='approval.txt',category='Client approval',content=base64.b64encode(b'Approved shipment').decode(),**extra),self.cookie)

    def test_dispatch_lifecycle_and_stale_save(self):
        self.prepare()
        self.assertEqual(self.act('collect')[0],400)
        self.assertEqual(self.act('accept')[0],200)
        stale=self.current()
        self.assertEqual(self.act('save',owner_id=self.user['id'],truck='T-01',driver='Driver',phone='123',carrier='Carrier',pickup=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),instructions='Gate 2')[0],200)
        self.assertEqual(self.current()['stage'],'Scheduled')
        self.assertTrue(self.current()['pickup_overdue'])
        self.assertEqual(self.request('/api/dispatch/collect',dict(shipment_id=stale['id'],version=stale['plan']['version'],shipment_version=stale['version']),self.cookie)[0],409)
        self.assertEqual(self.act('collect')[0],200)
        self.assertFalse(self.current()['pickup_overdue'])
        self.assertEqual(self.act('deliver')[0],200)
        self.assertEqual(self.current()['stage'],'Delivered')
        self.assertFalse(self.current()['has_pod'])
        self.assertEqual(self.act('save',carrier='Changed')[0],400)
        self.assertEqual(self.act('deliver')[0],400)

    def test_document_download_duplicate_archive_restore(self):
        self.prepare()
        code,result,_=self.upload()
        self.assertEqual(code,200)
        self.assertTrue(self.upload()[1]['duplicate'])
        url='/api/documents/'+result['id']+'/download'
        self.assertEqual(self.request(url)[0],401)
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        connection.request('GET',url,headers={'Cookie':self.cookie})
        response=connection.getresponse()
        self.assertEqual(response.status,200)
        self.assertIn('attachment',response.getheader('Content-Disposition'))
        self.assertEqual(response.read(),b'Approved shipment')
        connection.close()
        payload=dict(shipment_id=self.s['id'],id=result['id'],version=1,reason='Replaced copy')
        self.assertEqual(self.request('/api/documents/archive',payload,self.cookie)[0],200)
        self.assertEqual(self.request(url,cookie=self.cookie)[0],404)
        self.assertEqual(self.request('/api/documents/restore',payload,self.cookie)[0],409)
        payload['version']=2
        self.assertEqual(self.request('/api/documents/restore',payload,self.cookie)[0],200)
        app.init_db()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT content FROM shipment_documents').fetchone()[0],b'Approved shipment')
            self.assertNotIn('content',dispatch.documents(db)[0])

    def test_document_validation_and_permissions(self):
        self.prepare()
        for values in [dict(name='../bad.txt'),dict(name='fake.pdf'),dict(content='!invalid'),dict(content=''),dict(name='code.html')]:
            data=dict(shipment_id=self.s['id'],name='test.txt',category='Other',content=base64.b64encode(b'text').decode())
            data.update(values)
            self.assertEqual(self.request('/api/documents/upload',data,self.cookie)[0],400)
        for role in ('Sales','Viewer'):
            self.request('/api/users',dict(name=role,username=role.lower(),role=role,password='Testing-password-2026'),self.cookie)
            cookie=self.request('/api/login',dict(username=role.lower(),password='Testing-password-2026'))[2].split(';')[0]
            self.assertEqual(self.request('/api/dispatch/accept',dict(shipment_id=self.s['id']),cookie)[0],403)
            data=dict(shipment_id=self.s['id'],name='proof.txt',category='Proof of delivery',content=base64.b64encode(b'text').decode())
            self.assertEqual(self.request('/api/documents/upload',data,cookie)[0],403)
            data['category']='Client approval'
            self.assertEqual(self.request('/api/documents/upload',data,cookie)[0],200 if role=='Sales' else 403)

    def test_pod_and_large_file(self):
        self.prepare()
        data=dict(shipment_id=self.s['id'],name='proof.txt',category='Proof of delivery',content=base64.b64encode(b'x'*300000).decode())
        result=self.request('/api/documents/upload',data,self.cookie)
        self.assertEqual(result[0],200)
        self.assertTrue(self.current()['has_pod'])
        with app.connect() as db:
            with self.assertRaises(ValueError):
                dispatch.action(db,'/api/documents/upload',dict(data,content='x'*(((dispatch.MAX_FILE+2)//3)*4+1)),self.user,app.notify)

    def test_invalid_assignment_rolls_back(self):
        self.prepare()
        self.assertEqual(self.act('save',owner_id=99999,carrier='Carrier')[0],400)
        self.assertEqual(self.act('save',owner_id=None,carrier='Carrier',pickup=(datetime.now(timezone.utc)+timedelta(days=9)).isoformat())[0],400)
        self.assertEqual(self.current()['plan']['version'],0)
        self.assertEqual(self.current()['carrier'],'Test carrier')
