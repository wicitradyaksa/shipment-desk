import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
import app
import auth
import dispatch
import payables
import payments
import test_billing


class PayableTests(unittest.TestCase):
    def setUp(self):
        self.base=test_billing.BillingTests();self.base.setUp();self.base.prepare();self.base.submit_costs()
        self.users=self.base.users
        self.supplier=self.call('supplier',dict(name='Test Carrier'))['id']
        self.data=dict(supplier_id=self.supplier,shipment_id=self.base.sid,reference='C-001',amount='1200.01',
                       bill_date=payments.today().isoformat(),due_date=payments.today().isoformat(),document_id=self.base.doc,note='Test invoice')
        self.bid=self.call('create',self.data)['id']

    def tearDown(self):self.base.tearDown()

    def call(self,op,data,role='Operations'):
        with app.connect() as db:return payables.action(db,'/api/payables/'+op,data,self.users[role])

    def current(self):
        with app.connect() as db:return payables.summary(db,db.execute('SELECT * FROM supplier_bills WHERE id=?',(self.bid,)).fetchone())

    def act(self,op,role='Operations',**extra):
        return self.call(op,dict(id=self.bid,version=self.current()['version'],**extra),role)

    def approve(self):
        self.act('submit');self.act('approve','Admin',confirmed=True,fingerprint=self.current()['fingerprint'])

    def payment(self,amount='500',reference='BANK-1'):
        return dict(amount=amount,reference=reference,method='Bank transfer',payment_date=payments.today().isoformat(),confirmed=True,request_key=str(uuid.uuid4()))

    def test_match_approval_partial_full_and_reverse(self):
        self.approve();self.assertTrue(self.current()['approval_valid'])
        self.act('pay','Admin',**self.payment());self.assertEqual(self.current()['balance'],'700.01')
        self.act('pay','Admin',**self.payment('700.01','BANK-2'));self.assertEqual(self.current()['stage'],'Paid')
        pid=self.current()['payments'][0]['id']
        self.act('reverse','Admin',payment_id=pid,reason='Wrong entry',confirmed=True)
        self.assertEqual(self.current()['balance'],'700.01')
        self.assertIsNotNone(self.current()['payments'][0]['reversed_at'])
        app.init_db();self.assertEqual(self.current()['paid'],'500.00')

    def test_mismatch_blocks_and_return_allows_correction(self):
        self.act('save',**dict(self.data,amount='1201'))
        self.act('submit')
        self.assertEqual(self.current()['variance'],'0.99')
        with self.assertRaises(ValueError):self.act('approve','Admin',confirmed=True,fingerprint=self.current()['fingerprint'])
        self.act('return','Admin',reason='Correct total');self.act('save',**self.data);self.approve()
        self.assertTrue(self.current()['approval_valid'])

    def test_duplicate_supplier_reference_and_document(self):
        self.assertEqual(self.call('supplier',dict(name='  TEST   Carrier '))['id'],self.supplier)
        for change in ({},{'reference':'c-001'},{'reference':'different'}):
            with self.assertRaises(ValueError):self.call('create',dict(self.data,**change))
        self.act('cancel','Admin',reason='Duplicate entry')
        replacement=self.call('create',self.data)
        self.assertNotEqual(replacement['id'],self.bid)

    def test_stale_source_blocks_payment(self):
        self.approve()
        self.base.bill('costs',lines=[dict(label='Carrier invoice',amount='1200.01',document_id=self.base.doc)])
        self.assertFalse(self.current()['approval_valid'])
        with self.assertRaises(ValueError):self.act('pay','Admin',**self.payment())
        self.base.bill('submit',confirmed=True)
        self.act('approve','Admin',confirmed=True,fingerprint=self.current()['fingerprint'])
        with app.connect() as db:
            dispatch.action(db,'/api/documents/archive',dict(shipment_id=self.base.sid,id=self.base.doc,version=1,reason='Invalid proof'),self.users['Operations'],app.notify)
        self.assertFalse(self.current()['approval_valid'])

    def test_roles_and_paid_edit_protection(self):
        for role in ('Sales','Viewer'):
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):payables.state(db,self.users[role])
            with self.assertRaises(auth.AccessError):self.call('supplier',dict(name='No access'),role)
        self.act('submit')
        with self.assertRaises(auth.AccessError):self.act('approve',confirmed=True,fingerprint=self.current()['fingerprint'])
        self.act('approve','Admin',confirmed=True,fingerprint=self.current()['fingerprint'])
        with self.assertRaises(ValueError):self.act('save',**self.data)
        with self.assertRaises(auth.AccessError):self.act('pay',**self.payment())
        self.act('pay','Admin',**self.payment())
        for op in ('cancel','reopen'):
            with self.assertRaises(ValueError):self.act(op,'Admin',reason='Cannot change paid bill')

    def test_payment_retry_duplicate_and_overpayment(self):
        self.approve();p=self.payment();version=self.current()['version']
        data=dict(id=self.bid,version=version,**p)
        self.call('pay',data,'Admin');self.assertTrue(self.call('pay',data,'Admin')['duplicate'])
        with self.assertRaises(ValueError):self.act('pay','Admin',**self.payment())
        with self.assertRaises(ValueError):self.act('pay','Admin',**self.payment('700.02','BANK-2'))
        with self.assertRaises(auth.AccessError):self.call('pay',dict(data,amount='501'),'Admin')

    def test_concurrent_payment_rejects_stale_version(self):
        self.approve();version=self.current()['version']
        def run(n):
            try:self.call('pay',dict(id=self.bid,version=version,**self.payment('800',str(n))),'Admin');return 'saved'
            except auth.AccessError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,[1,2]))
        self.assertCountEqual(result,['saved','conflict']);self.assertEqual(self.current()['balance'],'400.01')

    def test_invalid_document_and_review_token(self):
        with self.assertRaises(ValueError):self.act('save',**dict(self.data,document_id='foreign-document'))
        self.act('submit')
        with self.assertRaises(auth.AccessError):self.act('approve','Admin',confirmed=True,fingerprint='stale')
        with self.assertRaises(ValueError):self.act('approve','Admin',confirmed=False,fingerprint=self.current()['fingerprint'])
