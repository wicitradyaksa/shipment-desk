import csv
import hashlib
import hmac
import io
import json
import os
import time
import unittest
from unittest.mock import patch,MagicMock
import app,auth,integrations
import test_sales
from test_reports import seed_reports


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base=test_sales.SalesTests();self.base.setUp();self.users=self.base.users;self.admin=self.users['Admin']
    def tearDown(self):self.base.tearDown()
    def call(self,op,data,role='Admin'):
        with app.connect() as db:return integrations.action(db,'/api/integrations/'+op,data,self.users[role])
    def state(self):
        with app.connect() as db:return integrations.state(db,self.admin)
    def settings(self,**changes):
        c=dict(self.state()['config'],**changes);return self.call('settings',c)
    def draft(self):
        q=self.base.quote();eid=self.call('prepare',dict(kind='proposal',source_id=q['id']))['id'];return q,eid
    def mail(self,eid):return next(m for m in self.state()['outbox'] if m['id']==eid)

    def test_unconfigured_test_mode_never_uses_network_and_preserves_workflow(self):
        q,eid=self.draft();m=self.mail(eid)
        self.assertNotIn('Estimated cost',m['body']);self.assertNotIn('margin',m['body'].lower())
        with patch('integrations.smtp_send') as send:
            self.call('send',dict(id=eid,version=m['version'],confirmed=True));send.assert_not_called()
        self.assertEqual(self.mail(eid)['status'],'Test captured')
        self.assertEqual(self.base.get(q['id'])['status'],'Proposal ready')
        self.call('reset_test',dict(id=eid,version=self.mail(eid)['version']))
        self.assertEqual(self.mail(eid)['status'],'Draft')

    def test_duplicate_drafts_confirmation_staleness_and_versions(self):
        q,eid=self.draft()
        self.assertTrue(self.call('prepare',dict(kind='proposal',source_id=q['id']))['duplicate'])
        with self.assertRaises(ValueError):self.call('send',dict(id=eid,version=1,confirmed=False))
        with self.assertRaises(auth.AccessError):self.call('send',dict(id=eid,version=0,confirmed=True))
        self.base.action(q,'revise',**test_sales.specs_fixture(description='Changed cargo'))
        with self.assertRaises(auth.AccessError):self.call('send',dict(id=eid,version=1,confirmed=True))

    def test_live_delivery_success_and_uncertain_no_resend(self):
        self.settings(email_mode='Live',confirmed=True,smtp_host='smtp.example.com',smtp_user='demo',from_email='demo@example.com')
        _,eid=self.draft()
        with patch.dict(os.environ,{'SHIPMENT_SMTP_PASSWORD':'test-secret'}),patch('integrations.smtp_send') as send:
            self.call('send',dict(id=eid,version=1,confirmed=True));self.assertEqual(send.call_count,1)
        self.assertEqual(self.mail(eid)['status'],'Sent')
        _,eid2=self.draft()
        with patch.dict(os.environ,{'SHIPMENT_SMTP_PASSWORD':'test-secret'}),patch('integrations.smtp_send',side_effect=RuntimeError('Private SMTP failure')):
            self.call('send',dict(id=eid2,version=1,confirmed=True))
        self.assertEqual(self.mail(eid2)['status'],'Uncertain')
        with self.assertRaises(ValueError):self.call('send',dict(id=eid2,version=self.mail(eid2)['version'],confirmed=True))
        self.assertNotIn('Private SMTP failure',json.dumps(self.state()))
        data=dict(id=eid2,version=self.mail(eid2)['version'],decision='not_sent',reason='Server log checked; no accepted message')
        with self.assertRaises(ValueError):self.call('resolve_delivery',data)
        self.call('resolve_delivery',dict(data,confirmed=True));self.assertEqual(self.mail(eid2)['status'],'Draft')

    def test_settings_roles_secret_redaction_and_validation(self):
        for role in ('Sales','Operations','Viewer'):
            with self.assertRaises(auth.AccessError):self.call('settings',dict(version=0),role)
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):integrations.state(db,self.users[role])
                with self.assertRaises(auth.AccessError):integrations.accounting(db,self.users[role],{})
        with self.assertRaises(ValueError):self.settings(email_mode='Live')
        with self.assertRaises(ValueError):self.settings(password='do-not-store')
        with self.assertRaises(ValueError):self.settings(smtp_host='https://smtp.example.com')
        self.settings()
        with self.assertRaises(auth.AccessError):self.call('settings',dict(integrations.DEFAULT))
        with patch.dict(os.environ,{'SHIPMENT_SMTP_PASSWORD':'private-test-password','SHIPMENT_TRACKING_SECRET':'x'*32}):
            state=self.state();self.assertTrue(state['secrets']['smtp_password']);self.assertNotIn('private-test-password',json.dumps(state))

    def event(self):
        with app.connect() as db:
            records=seed_reports(db,self.admin)
            ref=db.execute('SELECT reference FROM shipments WHERE id=?',(records[0]['sid'],)).fetchone()[0]
        return dict(event_id='carrier-1',shipment_reference=ref,status='in_transit',event_time='2026-10-01T09:00:00+08:00')

    def test_signed_tracking_disabled_auth_replay_and_no_shipment_changes(self):
        event=self.event();raw=json.dumps(event).encode();timestamp=str(int(time.time()));secret='t'*32
        headers={'X-Shipment-Timestamp':timestamp,'X-Shipment-Signature':hmac.new(secret.encode(),timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()}
        with app.connect() as db:
            with self.assertRaises(auth.AccessError):integrations.webhook(db,raw,headers)
        self.settings(tracking_enabled=True)
        with patch.dict(os.environ,{'SHIPMENT_TRACKING_SECRET':secret}):
            with app.connect() as db:first=integrations.webhook(db,raw,headers)
            with app.connect() as db:self.assertTrue(integrations.webhook(db,raw,headers)['duplicate'])
            for bad in ({**headers,'X-Shipment-Signature':'bad'},{**headers,'X-Shipment-Timestamp':'1'}):
                with app.connect() as db:
                    with self.assertRaises(auth.AccessError):integrations.webhook(db,raw,bad)
            event['status']='delivered';different=json.dumps(event).encode();headers['X-Shipment-Signature']=hmac.new(secret.encode(),timestamp.encode()+b'.'+different,hashlib.sha256).hexdigest()
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):integrations.webhook(db,different,headers)
                self.assertEqual(db.execute('SELECT status FROM shipments WHERE reference=?',(event['shipment_reference'],)).fetchone()[0],'Delivered')
                self.assertEqual(db.execute('SELECT count(*) FROM carrier_inbox').fetchone()[0],1)
        self.call('review',dict(id=first['id']));self.assertEqual(self.state()['carrier_events'][0]['reviewed'],1)

    def test_local_tracking_test_requires_no_configuration(self):
        event=self.event();self.call('tracking_test',event)
        self.assertFalse(self.state()['config']['tracking_enabled'])
        event['event_id']='new';event['status']='bad'
        with self.assertRaises(ValueError):self.call('tracking_test',event)

    def test_accounting_and_bank_preview_exclude_reversals_and_no_money_changes(self):
        with app.connect() as db:seed_reports(db,self.admin)
        with app.connect() as db:
            db.execute('PRAGMA query_only=ON');before=db.total_changes
            rows=list(csv.DictReader(io.StringIO(integrations.accounting(db,self.admin,{}).decode('utf-8-sig'))))
            receipts=sum(float(r['Amount']) for r in rows if r['Type']=='Customer receipt')
            self.assertEqual(receipts,550);self.assertEqual(db.total_changes,before)
            p=db.execute('SELECT * FROM payments WHERE reversed_at IS NULL LIMIT 1').fetchone()
            f=io.StringIO();w=csv.writer(f);w.writerow(['date','reference','currency','amount','direction']);w.writerow([p['payment_date'],p['reference'],p['currency'],p['amount'],'in']);w.writerow([p['payment_date'],p['reference'],p['currency'],p['amount'],'in']);w.writerow([p['payment_date'],'not-found',p['currency'],'1','out'])
            result=integrations.bank_preview(db,dict(csv=f.getvalue()))
            self.assertEqual([r['status'] for r in result['rows']],['Matched','Duplicate statement line','Unmatched']);self.assertEqual(db.total_changes,before)

    def test_smtp_tls_transport_uses_secret_only_in_transport(self):
        _,eid=self.draft();c=dict(integrations.DEFAULT,smtp_host='smtp.example.com',smtp_user='demo',from_email='demo@example.com')
        with app.connect() as db:row=dict(db.execute('SELECT * FROM email_outbox WHERE id=?',(eid,)).fetchone())
        client=MagicMock();client.__enter__.return_value=client
        with patch.dict(os.environ,{'SHIPMENT_SMTP_PASSWORD':'test-secret'}),patch('integrations.smtplib.SMTP',return_value=client):
            integrations.smtp_send(c,row)
        client.starttls.assert_called_once();client.login.assert_called_once_with('demo','test-secret');client.send_message.assert_called_once()

    def test_invoice_attachment_immutable_and_reminder_invalid_after_payment(self):
        with app.connect() as db:records=seed_reports(db,self.admin,with_pdfs=True)
        iid=records[0]['iid'];invoice=self.call('prepare',dict(kind='invoice',source_id=iid))['id']
        self.assertTrue(self.mail(invoice)['filename'].endswith('.pdf'))
        reminder=self.call('prepare',dict(kind='reminder',source_id=iid))['id']
        with app.connect() as db:db.execute('UPDATE payments SET reversed_at=? WHERE invoice_id=? AND reversed_at IS NULL',(dispatch_time(),iid))
        with self.assertRaises(auth.AccessError):self.call('send',dict(id=reminder,version=1,confirmed=True))

    def test_http_permissions_and_disabled_webhook(self):
        import http.client,threading
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def request(path,data=None,cookie=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,{'Content-Type':'application/json',**({'Cookie':cookie} if cookie else {})})
            response=conn.getresponse();result=(response.status,response.read(),response.getheader('Set-Cookie'));conn.close();return result
        try:
            self.assertEqual(request('/api/integrations')[0],401)
            self.assertEqual(request('/api/integrations/tracking-webhook',{})[0],403)
            for role in self.users:
                cookie=request('/api/login',dict(username=role.lower(),password='Test-only-password'))[2]
                for path in ('/api/integrations','/api/integrations/accounting'):
                    self.assertEqual(request(path,cookie=cookie)[0],200 if role=='Admin' else 403)
                if role!='Admin':self.assertEqual(request('/api/integrations/settings',{},cookie)[0],403)
        finally:server.shutdown();server.server_close();worker.join()


def dispatch_time():
    import dispatch
    return dispatch.now()
