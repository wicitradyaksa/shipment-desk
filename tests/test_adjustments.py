import csv
import io
import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import app,auth,adjustments,billing,invoices,payments,reports,integrations
import test_payments
import invoice_pdf
from pypdf import PdfReader


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.base=test_payments.PaymentTests();self.base.setUp();self.iid=self.base.iid;self.user=self.base.user
    def tearDown(self):self.base.tearDown()
    def current(self):return self.base.current()
    def call(self,op,data,user=None):
        with app.connect() as db:return adjustments.action(db,'/api/adjustments/'+op,data,user or self.user)
    def credit(self,amount='100',request=None,preview=None):
        payload=dict(invoice_id=self.iid,version=self.current()['version'],amount=amount,reason='Customer adjustment',credit_date=payments.today().isoformat())
        preview=preview or self.call('preview',payload)
        return self.call('issue',dict(payload,fingerprint=preview['fingerprint'],request_key=request or str(uuid.uuid4()),confirmed=True))
    def refund(self,amount='100',reference='REFUND-1',request=None):
        return self.call('refund',dict(invoice_id=self.iid,version=self.current()['version'],amount=amount,refund_date=payments.today().isoformat(),method='Bank transfer',reference=reference,request_key=request or str(uuid.uuid4()),confirmed=True))
    def paid(self,amount='500'):
        return self.base.call('record',self.base.receipt(amount))

    def test_credit_reduces_balance_and_original_pdf_is_immutable(self):
        with app.connect() as db:original=invoices.pdf(db,self.iid,self.user)[0]
        self.paid();self.credit()
        s=self.current();self.assertEqual(s['credited'],'100.00');self.assertEqual(s['balance'],'1066.88')
        with app.connect() as db:
            self.assertEqual(invoices.pdf(db,self.iid,self.user)[0],original)
            c=db.execute('SELECT * FROM credit_notes').fetchone();pdf,name=adjustments.pdf(db,c['id'],self.user)
            self.assertEqual(name,'CN-000001.pdf')
            text=''.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
            self.assertIn('CREDIT NOTE',text);self.assertIn(self.current()['number'],text);self.assertIn('100.00',text)

    def test_full_credit_cumulative_tax_and_refund_due(self):
        self.paid();self.credit('0.01');self.credit('1666.87')
        s=self.current();self.assertEqual(s['balance'],'0.00');self.assertEqual(s['refund_due'],'500.00');self.assertEqual(s['status'],'Refund due')
        with app.connect() as db:
            notes=list(db.execute('SELECT net,tax FROM credit_notes'))
            self.assertEqual(sum(Decimal(n['tax']) for n in notes),Decimal('79.38'))
            self.assertEqual(sum(Decimal(n['net']) for n in notes),Decimal('1587.50'))
        self.refund('500');s=self.current();self.assertEqual(s['refund_due'],'0.00');self.assertEqual(s['status'],'Credited')

    def test_credit_retry_concurrent_and_changed_request_rejected(self):
        data=dict(invoice_id=self.iid,version=self.current()['version'],amount='100',reason='Adjustment',credit_date=payments.today().isoformat())
        p=self.call('preview',data);payload=dict(data,request_key=str(uuid.uuid4()),confirmed=True,fingerprint=p['fingerprint'])
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(lambda _:self.call('issue',payload),range(2)))
        self.assertEqual(result[0]['id'],result[1]['id']);self.assertTrue(any(r.get('duplicate') for r in result))
        with self.assertRaises(auth.AccessError):self.call('issue',dict(payload,amount='101'))
        self.assertEqual(self.current()['credited'],'100.00')

    def test_credit_limits_roles_confirmation_and_stale_preview(self):
        data=dict(invoice_id=self.iid,version=self.current()['version'],amount='2000',reason='Adjustment',credit_date=payments.today().isoformat())
        with self.assertRaises(ValueError):self.call('preview',data)
        data['amount']='100';p=self.call('preview',data)
        for role in ('Sales','Operations','Viewer'):
            with self.assertRaises(auth.AccessError):self.call('preview',data,self.base.base.users[role])
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):adjustments.state(db,self.base.base.users[role])
        with self.assertRaises(ValueError):self.call('issue',dict(data,request_key=str(uuid.uuid4()),fingerprint=p['fingerprint'],confirmed=False))
        self.paid()
        with self.assertRaises(auth.AccessError):self.call('issue',dict(data,request_key=str(uuid.uuid4()),fingerprint=p['fingerprint'],confirmed=True))

    def test_partial_refund_reversal_and_receipt_reversal_guard(self):
        receipt=self.paid();self.credit('1400');refund=self.refund('100')
        s=self.current();self.assertEqual(s['refunded'],'100.00');self.assertEqual(s['refund_due'],'133.12')
        with self.assertRaises(ValueError):self.base.call('reverse',dict(payment_id=receipt['id'],reason='Correction',confirmed=True))
        self.call('refund_reverse',dict(invoice_id=self.iid,version=s['version'],refund_id=refund['id'],reason='Wrong ledger entry',confirmed=True))
        self.assertEqual(self.current()['refund_due'],'233.12')
        self.base.call('reverse',dict(payment_id=receipt['id'],reason='Correction',confirmed=True))
        self.assertEqual(self.current()['balance'],'266.88')

    def test_refund_retry_limits_and_historical_date_guard(self):
        with self.assertRaises(ValueError):self.refund('1')
        self.paid();self.credit('1400');request=str(uuid.uuid4());first=self.refund('100',request=request);second=self.refund('100',request=request)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['duplicate'])
        with self.assertRaises(ValueError):self.refund('200','REFUND-2')
        with self.assertRaises(ValueError):self.refund('1','REFUND-1')
        with self.assertRaises(ValueError):self.call('refund',dict(invoice_id=self.iid,version=self.current()['version'],amount='1',refund_date=(payments.today()-timedelta(days=1)).isoformat(),method='Bank transfer',reference='OLD',request_key=str(uuid.uuid4()),confirmed=True))

    def test_reports_accounting_and_bank_reconcile_credit_and_refund(self):
        self.paid();self.credit('1400');self.refund('100')
        with app.connect() as db:
            report=reports.state(db,self.user)['tables'];cash=report['cash']['rows'][0]
            self.assertEqual(cash['received'],'500.00');self.assertEqual(cash['net'],'400.00');self.assertEqual(report['refunds']['rows'][0]['refunded'],'100.00')
            self.assertEqual(report['receivables']['rows'],[])
            r=report['profit_customer']['rows'][0];source=billing.summary(db,dict(db.execute('SELECT * FROM shipments').fetchone()))
            expected=Decimal(source['totals']['revenue'])-adjustments.totals(db,self.iid)['credit_net']
            self.assertEqual(Decimal(r['revenue']),expected)
            rows=list(csv.DictReader(io.StringIO(integrations.accounting(db,self.user,{}).decode('utf-8-sig'))))
            self.assertTrue(any(r['Type']=='Customer credit tax' for r in rows));self.assertTrue(any(r['Type']=='Customer refund' for r in rows))
            result=integrations.bank_preview(db,dict(csv=f'date,reference,currency,amount,direction\n{payments.today()},REFUND-1,TWD,100,out\n'))
            self.assertEqual(result['rows'][0]['status'],'Matched')
            self.assertEqual(report['operations']['rows'][0]['fully_paid_samples'],0)

    def test_http_admin_only_and_preview_is_read_only(self):
        import http.client,threading
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def request(path,data=None,cookie=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port);conn.request('POST' if data else 'GET',path,json.dumps(data) if data else None,{'Content-Type':'application/json',**({'Cookie':cookie} if cookie else {})});r=conn.getresponse();result=(r.status,r.read(),r.getheader('Set-Cookie'));conn.close();return result
        try:
            self.assertEqual(request('/api/adjustments')[0],401)
            for role in self.base.base.users:
                cookie=request('/api/login',dict(username=role.lower(),password='Test-only-password'))[2]
                for path in ('/api/adjustments','/api/adjustments/1/pdf'):
                    status=request(path,cookie=cookie)[0];self.assertEqual(status,403 if role!='Admin' else 200 if path=='/api/adjustments' else 400)
            with app.connect() as db:
                db.execute('PRAGMA query_only=ON');before=db.total_changes
                adjustments.credit_preview(db,dict(invoice_id=self.iid,version=self.current()['version'],amount='100',reason='Test',credit_date=payments.today().isoformat()),self.user)
                self.assertEqual(db.total_changes,before)
        finally:server.shutdown();server.server_close();worker.join()
