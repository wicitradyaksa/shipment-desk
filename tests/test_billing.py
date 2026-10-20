import base64
import unittest
from datetime import datetime, timezone, timedelta
import app
import auth
import billing
import dispatch
import sales
import test_sales


class BillingTests(unittest.TestCase):
    setUp=test_sales.SalesTests.setUp
    tearDown=test_sales.SalesTests.tearDown
    call=test_sales.SalesTests.call
    quote=test_sales.SalesTests.quote
    get=test_sales.SalesTests.get
    action=test_sales.SalesTests.action

    def prepare(self):
        q=self.quote()
        q=self.action(q,'sent',note='Client email')
        q=self.action(q,'close',acceptance='Buyer approved PO-123',confirmed=True)
        self.sid=q['shipment_id']
        with app.connect() as db:
            db.execute("UPDATE shipments SET status='Delivered' WHERE id=?",(self.sid,))
        self.doc=self.upload('Proof of delivery')

    def upload(self,category):
        with app.connect() as db:
            return dispatch.action(db,'/api/documents/upload',dict(shipment_id=self.sid,name='evidence.txt',category=category,
                content=base64.b64encode(b'Test supporting evidence').decode()),self.users['Operations'],app.notify)['id']

    def current(self):
        with app.connect() as db:
            return billing.summary(db,dict(db.execute('SELECT * FROM shipments WHERE id=?',(self.sid,)).fetchone()))

    def bill(self,operation,role='Operations',**extra):
        s=self.current()
        data=dict(shipment_id=self.sid,version=s['record']['version'],**extra)
        with app.connect() as db:
            return billing.action(db,'/api/billing/'+operation,data,self.users[role])

    def submit_costs(self):
        self.bill('costs',lines=[dict(label='Carrier invoice',amount='1200.01',document_id=self.doc)],note='Final carrier invoice')
        self.bill('submit',confirmed=True)

    def approve(self):
        self.bill('approve','Admin',confirmed=True,fingerprint=self.current()['fingerprint'])

    def test_calculation_and_full_approval(self):
        self.prepare();self.submit_costs()
        self.bill('extras',lines=[dict(label='Waiting time',amount='200.05')])
        self.assertIn('Sales must resolve every extra charge.',self.current()['blockers'])
        self.bill('decision','Sales',index=0,decision='Approved',reference='Client email accepts waiting charge',confirmed=True)
        s=self.current()
        self.assertEqual(s['stage'],'Ready for approval')
        self.assertEqual(s['totals'],dict(actual_cost='1200.01',approved_extras='200.05',revenue='1787.55',profit='587.54',margin='32.87',quoted_cost='1270.00',cost_variance='-69.99'))
        self.approve()
        self.assertTrue(self.current()['approval_valid'])
        app.init_db()
        self.assertEqual(self.current()['stage'],'Approved for billing')
        with self.assertRaises(ValueError): self.approve()

    def test_blockers_and_zero_cost_note(self):
        self.prepare()
        with self.assertRaises(ValueError): self.approve()
        with self.assertRaises(ValueError): self.bill('submit',confirmed=True)
        self.bill('costs',lines=[],note='No supplier costs: owned capacity accounted elsewhere.')
        with self.assertRaises(ValueError): self.bill('submit',confirmed=False)
        self.bill('submit',confirmed=True)
        self.assertFalse(self.current()['blockers'])

    def test_changes_invalidate_approval_and_submission(self):
        self.prepare();self.submit_costs();self.approve()
        self.bill('costs',lines=[dict(label='Revised invoice',amount='1300',document_id=self.doc)],note='Correction')
        self.assertEqual(self.current()['stage'],'Approval outdated')
        self.assertIsNone(self.current()['record']['submitted_at'])
        self.bill('submit',confirmed=True);self.approve()
        with app.connect() as db:
            dispatch.action(db,'/api/documents/archive',dict(shipment_id=self.sid,id=self.doc,version=1,reason='Wrong proof'),self.users['Operations'],app.notify)
        self.assertFalse(self.current()['approval_valid'])
        self.assertIn('Upload active proof of delivery in Dispatch.',self.current()['blockers'])
        with self.assertRaises(ValueError): self.approve()

    def test_stale_versions_and_document_review_token(self):
        self.prepare();self.submit_costs()
        s=self.current()
        self.upload('Packing list')
        with self.assertRaises(auth.AccessError): self.bill('approve','Admin',confirmed=True,fingerprint=s['fingerprint'])
        self.bill('extras',lines=[])
        with app.connect() as db:
            with self.assertRaises(auth.AccessError):
                billing.action(db,'/api/billing/costs',dict(shipment_id=self.sid,version=s['record']['version'],lines=[]),self.users['Operations'])

    def test_roles_and_sales_ownership(self):
        self.prepare();self.submit_costs()
        for action,role,extra in [('costs','Sales',dict(lines=[])),('extras','Sales',dict(lines=[])),('approve','Operations',dict(confirmed=True)),('decision','Operations',dict(index=0)),('terms','Sales',{})]:
            with self.assertRaises(auth.AccessError): self.bill(action,role,**extra)
        with app.connect() as db:
            with self.assertRaises(auth.AccessError): billing.state(db,self.users['Viewer'])
            other=dict(self.users['Sales'],id=99999)
            self.assertFalse(billing.state(db,other)['shipments'])
            with self.assertRaises(auth.AccessError): billing.action(db,'/api/billing/decision',dict(shipment_id=self.sid,version=self.current()['record']['version']),other)

    def test_extra_revisions_reset_decisions(self):
        self.prepare();self.submit_costs()
        self.bill('extras',lines=[dict(label='Waiting',amount='50')])
        self.bill('decision','Sales',index=0,decision='Not billed',reference='Company absorbs this cost',confirmed=True)
        self.assertEqual(self.current()['totals']['approved_extras'],'0.00')
        self.approve()
        self.bill('extras',lines=[dict(label='Waiting',amount='60')])
        self.assertEqual(self.current()['record']['extras'][0]['decision'],'Pending')
        self.assertFalse(self.current()['approval_valid'])

    def test_invalid_amounts_and_evidence(self):
        self.prepare()
        for value in ('NaN','Infinity','-1','100000001',True,'oops'):
            with self.assertRaises(ValueError): self.bill('costs',lines=[dict(label='Bad',amount=value,document_id=self.doc)])
        with self.assertRaises(ValueError): self.bill('costs',lines=[dict(label='Wrong document',amount='10',document_id='another-shipment')])
        self.bill('costs',lines=[dict(label='Missing evidence',amount='10',document_id='')])
        with self.assertRaises(ValueError): self.bill('submit',confirmed=True)
        self.assertIsNone(self.current()['record']['submitted_at'])

    def test_manual_terms_and_currency_guard(self):
        with app.connect() as db:
            self.sid=app.create(db,dict(reference='MANUAL',customer='Manual client',email='c@example.com',origin='A',destination='B',carrier='Carrier',tracking='T',eta=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()))
        with self.assertRaises(ValueError): self.bill('costs',lines=[])
        self.bill('terms','Admin',currency='USD',revenue='1000',quoted_cost='800',acceptance='Client PO manual')
        self.bill('costs',lines=[dict(label='Supplier',amount='850',document_id='')])
        with self.assertRaises(ValueError): self.bill('terms','Admin',currency='TWD',revenue='1000',quoted_cost='800',acceptance='New terms')
        self.assertEqual(self.current()['terms']['currency'],'USD')
        self.assertEqual(self.current()['totals']['profit'],'150.00')
        self.assertIn('Delivery has not been confirmed.',self.current()['blockers'])
