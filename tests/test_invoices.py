import io
import unittest
from pathlib import Path
import app
import auth
import invoices
import invoice_pdf
import test_billing
from pypdf import PdfReader


def settings_fixture():
    return dict(version=0,company='QA Logistics',address='12 Test Road\nTaipei',tax_id='TEST-ONLY',email='billing@example.com',bank='Test bank details',
        terms='Payment within 30 days.',prefix='QA-',payment_days=30,tax_mode='exclusive',tax_rate='5',tax_label='Test tax')


class InvoiceTests(unittest.TestCase):
    setUp=test_billing.BillingTests.setUp
    tearDown=test_billing.BillingTests.tearDown
    call=test_billing.BillingTests.call
    quote=test_billing.BillingTests.quote
    get=test_billing.BillingTests.get
    action=test_billing.BillingTests.action
    prepare=test_billing.BillingTests.prepare
    upload=test_billing.BillingTests.upload
    current=test_billing.BillingTests.current
    bill=test_billing.BillingTests.bill
    submit_costs=test_billing.BillingTests.submit_costs
    approve=test_billing.BillingTests.approve

    def inv(self,op,data,role='Admin'):
        with app.connect() as db:return invoices.action(db,'/api/invoices/'+op,data,self.users[role])

    def setup_invoice(self):
        self.prepare();self.submit_costs();self.approve()
        self.inv('settings',settings_fixture())
        self.iid=self.inv('create',dict(shipment_id=self.sid))['id']
        return self.invoice()

    def invoice(self):
        with app.connect() as db:return invoices.decode(db.execute('SELECT * FROM invoices WHERE id=?',(self.iid,)).fetchone())

    def change(self,op,**data):return self.inv(op,dict(id=self.iid,version=self.invoice()['version'],**data))

    def save(self):
        return self.change('save',customer_address='88 Customer Street\nTaipei',customer_tax_id='CUSTOMER-ID',issue_date='2026-10-14',due_date='2026-11-13',notes='Thank you.')

    def test_issue_pdf_snapshot_and_unique_sequence(self):
        i=self.setup_invoice();self.save()
        self.assertEqual(i['snapshot']['total'],'1666.88')
        self.assertEqual(self.inv('create',dict(shipment_id=self.sid))['id'],self.iid)
        self.change('issue',confirmed=True)
        i=self.invoice();self.assertEqual(i['number'],'QA-000001');self.assertEqual(i['status'],'Issued')
        with app.connect() as db:
            pdf,name=invoices.pdf(db,self.iid,self.users['Admin'])
            self.assertEqual(invoices.config(db)['next_number'],2)
        text=''.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        for word in ('QA-000001','1,666.88','88 Customer Street'):self.assertIn(word,text)
        for word in ('1,200.01','profit','Carrier invoice'):self.assertNotIn(word,text)
        with self.assertRaises(ValueError):self.save()
        self.change('note',note='Correction requested; arrange separately.')
        self.inv('settings',dict(settings_fixture(),version=1,company='Changed company',tax_rate='20'))
        with app.connect() as db:self.assertEqual(invoices.pdf(db,self.iid,self.users['Admin'])[0],pdf)
        app.init_db()
        self.assertEqual(self.invoice()['number'],'QA-000001')

    def test_draft_requires_settings_address_and_current_approval(self):
        self.prepare();self.submit_costs();self.approve()
        with self.assertRaises(ValueError):self.inv('create',dict(shipment_id=self.sid))
        self.inv('settings',settings_fixture());self.iid=self.inv('create',dict(shipment_id=self.sid))['id']
        with self.assertRaises(ValueError):self.change('issue',confirmed=True)
        self.save()
        self.bill('extras',lines=[])
        # A source hash can remain unchanged for a no-op list edit; change actual costs.
        self.bill('costs',lines=[dict(label='Revised',amount='1250',document_id=self.doc)])
        with self.assertRaises(ValueError):self.change('issue',confirmed=True)
        self.bill('submit',confirmed=True);self.approve()
        with self.assertRaises(ValueError):self.change('issue',confirmed=True)
        self.change('refresh');self.change('issue',confirmed=True)

    def test_tax_modes_rounding_and_settings_validation(self):
        self.setup_invoice()
        self.inv('settings',dict(settings_fixture(),version=1,tax_mode='inclusive'))
        self.change('refresh');s=self.invoice()['snapshot']
        self.assertEqual(s['total'],'1587.50');self.assertEqual(s['tax'],'75.60');self.assertEqual(s['net'],'1511.90')
        with self.assertRaises(ValueError):self.inv('settings',dict(settings_fixture(),version=2,tax_mode='none',tax_rate='5'))
        with self.assertRaises(ValueError):self.inv('settings',dict(settings_fixture(),version=2,payment_days=1.5))
        self.inv('settings',dict(settings_fixture(),version=2,tax_mode='none',tax_rate='0'))
        self.change('refresh');self.assertEqual(self.invoice()['snapshot']['tax'],'0.00')

    def test_roles_and_stale_edits(self):
        i=self.setup_invoice();self.save()
        with self.assertRaises(auth.AccessError):self.inv('note',dict(id=self.iid,version=i['version'],note='Old edit'))
        for role in ('Sales','Operations','Viewer'):
            with self.assertRaises(auth.AccessError):self.inv('settings',settings_fixture(),role)
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):invoices.state(db,self.users[role])
                with self.assertRaises(auth.AccessError):invoices.pdf(db,self.iid,self.users[role])

    def test_draft_pdf_and_long_unicode_content(self):
        i=self.setup_invoice()
        i['snapshot']['customer']='測試客戶 Test Customer'
        i['snapshot']['lines']=[dict(label='Transport charge '+str(n)+' - '+('Long description '*12),amount='1.00') for n in range(100)]
        pdf=invoice_pdf.render(i);reader=PdfReader(io.BytesIO(pdf))
        self.assertGreater(len(reader.pages),2)
        text=''.join(p.extract_text() for p in reader.pages)
        self.assertIn('DRAFT',text);self.assertIn('Transport charge 99',text)

    def test_bad_dates_rollback(self):
        self.setup_invoice();before=self.invoice()
        with self.assertRaises(ValueError):self.change('save',customer_address='X',issue_date='2026-10-14',due_date='2026-10-13')
        self.assertEqual(self.invoice()['version'],before['version'])
