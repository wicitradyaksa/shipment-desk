import json
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
import app
import auth
import invoices
import payments
import test_invoices


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.base=test_invoices.InvoiceTests();self.base.setUp();self.base.setup_invoice();self.base.save();self.base.change('issue',confirmed=True)
        self.iid=self.base.iid;self.user=self.base.users['Admin']

    def tearDown(self):self.base.tearDown()

    def current(self):
        with app.connect() as db:return payments.summary(db,db.execute('SELECT * FROM invoices WHERE id=?',(self.iid,)).fetchone())

    def call(self,op,data=None,user=None):
        payload=dict(invoice_id=self.iid,version=self.current()['version']);payload.update(data or {})
        with app.connect() as db:return payments.action(db,'/api/payments/'+op,payload,user or self.user)

    def receipt(self,amount='500.00',**extra):
        return dict(amount=amount,currency='TWD',payment_date=payments.today().isoformat(),method='Bank transfer',reference='BANK-001',
                    confirmed=True,note='',request_key=str(uuid.uuid4()),**extra)

    def test_partial_full_payment_and_immutable_pdf(self):
        with app.connect() as db:before=invoices.pdf(db,self.iid,self.user)[0]
        self.call('record',self.receipt())
        s=self.current();self.assertEqual(s['balance'],'1166.88');self.assertEqual(s['status'],'Partially paid')
        data=self.receipt('1166.88');data['reference']='BANK-002';self.call('record',data)
        self.assertEqual(self.current()['balance'],'0.00');self.assertEqual(self.current()['status'],'Paid')
        with self.assertRaises(ValueError):self.call('draft')
        with app.connect() as db:self.assertEqual(invoices.pdf(db,self.iid,self.user)[0],before)
        app.init_db();self.assertEqual(self.current()['paid'],'1666.88')

    def test_idempotent_retry_and_duplicate_reference(self):
        data=self.receipt();data['version']=self.current()['version']
        first=self.call('record',data);second=self.call('record',data)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['duplicate'])
        self.assertEqual(len(self.current()['payments']),1)
        with self.assertRaises(ValueError):self.call('record',self.receipt())
        with self.assertRaises(auth.AccessError):self.call('record',dict(data,amount='501.00'))

    def test_reverse_preserves_history_and_reopens_balance(self):
        p=self.call('record',self.receipt('1666.88'))
        self.call('reverse',dict(payment_id=p['id'],reason='Incorrect allocation',confirmed=True))
        s=self.current();self.assertEqual(s['balance'],'1666.88');self.assertEqual(s['status'],'Unpaid')
        self.assertEqual(s['payments'][0]['reversal_reason'],'Incorrect allocation')
        with self.assertRaises(ValueError):self.call('reverse',dict(payment_id=p['id'],reason='Again',confirmed=True))
        self.call('record',self.receipt('100'))
        self.assertEqual(len(self.current()['payments']),2)

    def test_validation_and_role_limits(self):
        for amount in ('0','-1','0.001','NaN','Infinity','1666.89'):
            with self.assertRaises(ValueError):self.call('record',self.receipt(amount))
        for changes in [dict(currency='USD'),dict(payment_date=(payments.today()+timedelta(days=1)).isoformat()),dict(confirmed=False),dict(method='Invalid')]:
            data=self.receipt();data.update(changes)
            with self.assertRaises(ValueError):self.call('record',data)
        for role in ('Sales','Operations','Viewer'):
            with self.assertRaises(auth.AccessError):self.call('record',self.receipt(),self.base.users[role])
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):payments.state(db,self.base.users[role])
        self.assertEqual(self.current()['paid'],'0.00')

    def test_draft_invoice_rejected(self):
        with app.connect() as db:db.execute("UPDATE invoices SET status='Draft' WHERE id=?",(self.iid,))
        with self.assertRaises(ValueError):self.call('record',self.receipt())
        with app.connect() as db:self.assertEqual(payments.state(db,self.user)['invoices'],[])

    def test_reminder_staleness_and_due_date_boundary(self):
        due=payments.date.fromisoformat(self.current()['due_date'])
        with patch('payments.today',return_value=due):
            self.assertFalse(self.current()['overdue'])
            d=self.call('draft');self.assertEqual(d['id'],self.call('draft')['id'])
            self.assertFalse(self.current()['drafts'][0]['stale'])
            self.call('record',self.receipt())
            self.assertTrue(self.current()['drafts'][0]['stale'])
            self.call('draft');text=self.current()['drafts'][0]['body']
            self.assertIn('1,166.88',text);self.assertIn('Payments recorded: TWD 500.00',text)
        with patch('payments.today',return_value=due+timedelta(days=31)):
            self.assertTrue(self.current()['overdue']);self.assertEqual(self.current()['aging'],'31–60 days')
            self.assertTrue(self.current()['drafts'][0]['stale'])

    def test_concurrent_payments_cannot_overpay(self):
        version=self.current()['version']
        def run(n):
            data=self.receipt('1000.00');data.update(version=version,reference=f'BANK-{n}')
            try:self.call('record',data);return 'saved'
            except auth.AccessError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
        self.assertCountEqual(results,['saved','conflict']);self.assertEqual(self.current()['balance'],'666.88')

    def test_currency_totals_are_separate(self):
        self.call('record',self.receipt('100'))
        with app.connect() as db:
            old=db.execute('SELECT * FROM invoices WHERE id=?',(self.iid,)).fetchone()
            shipment=dict(db.execute('SELECT * FROM shipments WHERE id=?',(old['shipment_id'],)).fetchone())
            shipment.update(id=str(uuid.uuid4()),reference='USD-TEST')
            db.execute('INSERT INTO shipments('+','.join(shipment)+') VALUES ('+','.join('?' for _ in shipment)+')',tuple(shipment.values()))
            snap=json.loads(old['snapshot']);snap.update(currency='USD',total='200.00')
            db.execute("INSERT INTO invoices(id,shipment_id,number,status,source_hash,snapshot,created) VALUES (?,?,?,'Issued',?,?,?)",
                       (str(uuid.uuid4()),shipment['id'],'USD-000001','fixture',json.dumps(snap),old['created']))
            totals=payments.state(db,self.user)['totals']
        self.assertEqual(totals['TWD']['outstanding'],'1566.88');self.assertEqual(totals['USD']['outstanding'],'200.00')
