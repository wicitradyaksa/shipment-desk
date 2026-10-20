"""Reconcile read-only reports with source financial records and HTTP permissions."""
import base64
import csv
import io
import json
import sqlite3
import threading
import unittest
import uuid
from datetime import datetime,time,timedelta,timezone
from decimal import Decimal
import app
import auth
import billing
import dispatch
import invoices
import payments
import payables
import reports
import test_sales


def seed_reports(db,admin,with_pdfs=False):
    """Fictional records for isolated tests and --reports, never the live database."""
    today=payments.today()
    def stamp(days,hour=12):return datetime.combine(today+timedelta(days=days),time(hour),timezone.utc).isoformat()
    def day(days):return (today+timedelta(days=days)).isoformat()
    records=[]
    for index,(currency,revenue,cost,created,delivered,issue,due,late,approved) in enumerate([
        ('TWD','1000','600',-60,-58,-56,-45,False,True),
        ('USD','200','120',-40,-38,-35,-10,True,True),
        ('TWD','100','70',-10,None,None,10,False,False),
        ('TWD','300','100',-5,None,-3,0,False,True),
        ('USD','200','80',-3,-2,-1,10,False,True),
    ],1):
        ref=f'REPORT-DEMO-{index}';sid=str(uuid.uuid4());doc=str(uuid.uuid4());supplier=str(uuid.uuid4());bid=str(uuid.uuid4());iid=str(uuid.uuid4())
        customer='Example Client '+str(index);carrier='Example Carrier '+str(index%2)
        db.execute('INSERT INTO shipments(id,reference,customer,email,origin,destination,carrier,tracking,status,eta,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (sid,ref,customer,'demo@example.com','Taipei','Taichung' if index%2 else 'Tainan',carrier,ref,'Delivered' if approved else 'Booked',stamp(delivered if delivered is not None else 2,11 if late else 13),stamp(created)))
        for category in ('Proof of delivery','Other'):
            did=doc if category=='Other' else str(uuid.uuid4())
            content=b'Fictional reports evidence only'
            db.execute('INSERT INTO shipment_documents(id,shipment_id,name,category,mime,size,checksum,content,uploaded_at,uploaded_by) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (did,sid,'report-demo.txt',category,'text/plain',len(content),'reports-demo-checksum',content,stamp(created),admin['id']))
        if delivered is not None:
            db.execute('INSERT INTO dispatch_plans(shipment_id,delivered_at) VALUES (?,?)',(sid,stamp(delivered)))
        terms=dict(currency=currency,revenue=revenue,quoted_cost=cost,acceptance='Fictional client approval')
        costs=[dict(label='Fictional carrier cost',amount=cost,document_id=doc)]
        db.execute('INSERT INTO billing_records(shipment_id,costs,manual_terms,submitted_at,submitted_by) VALUES (?,?,?,?,?)',
            (sid,json.dumps(costs),json.dumps(terms),stamp(created+1) if approved else None,admin['id'] if approved else None))
        s=billing.summary(db,dict(db.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()))
        if approved:
            db.execute('UPDATE billing_records SET approval_hash=?,approved_at=?,approved_by=? WHERE shipment_id=?',(s['fingerprint'],stamp(created+2),admin['id'],sid))
        db.execute('INSERT INTO suppliers VALUES (?,?,?)',(supplier,'Example Supplier '+str(index),'example supplier '+str(index)))
        db.execute('INSERT INTO supplier_bills(id,supplier_id,shipment_id,reference,reference_key,amount,currency,bill_date,due_date,document_id,note,status,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (bid,supplier,sid,ref+'-B',ref.lower()+'-b',cost,currency,day(created),day(-95 if index==4 else due),doc,'Fictional demo bill','Approved' if approved else 'Draft',stamp(created)))
        b=payables.summary(db,db.execute('SELECT * FROM supplier_bills WHERE id=?',(bid,)).fetchone())
        if approved:db.execute('UPDATE supplier_bills SET approval_hash=?,approved_at=?,approved_by=? WHERE id=?',(b['fingerprint'],stamp(created+2),admin['id'],bid))
        if issue is not None:
            from test_invoices import settings_fixture
            company=dict(settings_fixture(),version=1,tax_mode='none',tax_rate='0')
            current=billing.summary(db,dict(db.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()))
            snapshot=invoices.snapshot(current,company)
            snapshot.update(issue_date=day(issue),due_date=day(due),customer_address='88 Fictional Customer Road',customer_tax_id='DEMO-ONLY')
            pdf=None
            if with_pdfs:
                from invoice_pdf import render
                pdf=render(dict(id=iid,number=ref+'-INV',status='Issued',snapshot=snapshot))
            db.execute('INSERT INTO invoices(id,shipment_id,number,status,source_hash,snapshot,created,issued_at,issued_by,pdf) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (iid,sid,ref+'-INV','Issued',s['fingerprint'],json.dumps(snapshot),stamp(issue),stamp(issue),admin['id'],pdf))
        if late:
            db.execute('INSERT INTO alerts(shipment_id,kind,message,created,resolved) VALUES (?,?,?,?,?)',(sid,'overdue','Fictional past delay',stamp(delivered),stamp(delivered)))
            db.execute('INSERT INTO alerts(shipment_id,kind,message,created,resolved) VALUES (?,?,?,?,?)',(sid,'exception','Fictional past exception',stamp(delivered),stamp(delivered)))
        records.append(dict(sid=sid,iid=iid if issue is not None else None,bid=bid,doc=doc,currency=currency))

    def receipt(r,amount,days,reverse=False,supplier=False):
        pid=str(uuid.uuid4());ref='DEMO-'+pid
        if supplier:
            db.execute('INSERT INTO supplier_payments(id,bill_id,amount,payment_date,method,reference,reference_key,request_key,created,created_by,reversed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (pid,r['bid'],amount,day(days),'Bank transfer',ref,ref,str(uuid.uuid4()),stamp(days),admin['id'],stamp(0) if reverse else None))
        else:
            db.execute('INSERT INTO payments(id,invoice_id,amount,currency,payment_date,method,reference,reference_key,note,request_key,created,created_by,reversed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (pid,r['iid'],amount,r['currency'],day(days),'Bank transfer',ref,ref,'Fictional receipt',str(uuid.uuid4()),stamp(days),admin['id'],stamp(0) if reverse else None))
    receipt(records[0],'100',-30);receipt(records[0],'200',-15);receipt(records[0],'50',-10,True)
    receipt(records[0],'100',-10,supplier=True);receipt(records[0],'50',-8,True,True)
    receipt(records[1],'50',-4);receipt(records[1],'150',-2)
    receipt(records[1],'20',-5,supplier=True);receipt(records[1],'20',-3,supplier=True)
    receipt(records[4],'50',0)
    return records


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.base=test_sales.SalesTests();self.base.setUp();self.users=self.base.users;self.admin=self.users['Admin']
        with app.connect() as db:self.records=seed_reports(db,self.admin)
    def tearDown(self):self.base.tearDown()
    def report(self,**params):
        with app.connect() as db:return reports.state(db,self.admin,params)
    def rows(self,key,**params):return self.report(**params)['tables'][key]['rows']

    def test_profit_reconciles_current_approvals_and_provisional_in_every_dimension(self):
        with app.connect() as db:
            expected={}
            for row in db.execute('SELECT * FROM shipments'):
                s=billing.summary(db,dict(row));key=('Approved' if s['approval_valid'] else 'Provisional',s['terms']['currency'])
                totals=expected.setdefault(key,[Decimal(0),Decimal(0)])
                totals[0]+=Decimal(s['totals']['revenue']);totals[1]+=Decimal(s['totals']['actual_cost'])
        for key in ('profit_customer','profit_lane','profit_carrier','profit_month'):
            actual={}
            for row in self.rows(key):
                totals=actual.setdefault((row['basis'],row['currency']),[Decimal(0),Decimal(0)])
                totals[0]+=Decimal(row['revenue']);totals[1]+=Decimal(row['actual_cost'])
                self.assertEqual(Decimal(row['profit']),Decimal(row['revenue'])-Decimal(row['actual_cost']))
            self.assertEqual(actual,expected)
        self.assertEqual(expected[('Approved','TWD')],[Decimal(1300),Decimal(700)])
        self.assertEqual(expected[('Provisional','TWD')],[Decimal(100),Decimal(70)])

    def test_outdated_billing_moves_only_profit_to_provisional(self):
        with app.connect() as db:
            db.execute('UPDATE shipment_documents SET archived_at=? WHERE id=?',(dispatch.now(),self.records[0]['doc']))
        rows=self.rows('profit_customer')
        self.assertEqual(next(r for r in rows if r['group']=='Example Client 1')['basis'],'Provisional')
        self.assertEqual(sum(Decimal(r['total']) for r in self.rows('receivables',currency='TWD')),Decimal(1000))

    def test_receivables_match_payments_partial_reversed_and_currency_totals(self):
        with app.connect() as db:source=payments.state(db,self.admin)
        rows=self.rows('receivables')
        for currency,totals in source['totals'].items():
            self.assertEqual(sum((Decimal(r['total']) for r in rows if r['currency']==currency),Decimal(0)),Decimal(totals['outstanding']))
        self.assertEqual(next(r for r in rows if r['party']=='Example Client 1')['days_31_60'],'700.00')
        self.assertEqual(next(r for r in rows if r['party']=='Example Client 4')['current'],'300.00')
        self.assertEqual(next(r for r in rows if r['party']=='Example Client 5')['current'],'150.00')

    def test_aging_boundaries_use_same_today_and_bands(self):
        iid=self.records[0]['iid']
        for days,key in [(0,'current'),(-2,'current'),(1,'days_1_30'),(30,'days_1_30'),(31,'days_31_60'),(60,'days_31_60'),(61,'days_61_90'),(90,'days_61_90'),(91,'days_90_plus')]:
            with self.subTest(days=days):
                with app.connect() as db:
                    row=db.execute('SELECT snapshot FROM invoices WHERE id=?',(iid,)).fetchone();snapshot=json.loads(row[0]);snapshot['due_date']=(payments.today()-timedelta(days=days)).isoformat()
                    db.execute('UPDATE invoices SET snapshot=? WHERE id=?',(json.dumps(snapshot),iid))
                result=next(r for r in self.rows('receivables') if r['party']=='Example Client 1')
                self.assertEqual(result[key],'700.00')

    def test_payables_only_current_approved_balances_and_reversals(self):
        rows=self.rows('payables')
        with app.connect() as db:source=payables.state(db,self.admin)
        for currency,total in source['totals'].items():
            self.assertEqual(sum((Decimal(r['total']) for r in rows if r['currency']==currency),Decimal(0)),Decimal(total['approved']))
        self.assertEqual(next(r for r in rows if r['party']=='Example Supplier 1')['days_31_60'],'500.00')
        self.assertEqual(next(r for r in rows if r['party']=='Example Supplier 4')['days_90_plus'],'100.00')
        with app.connect() as db:db.execute("UPDATE supplier_bills SET approval_hash='outdated' WHERE id=?",(self.records[0]['bid'],))
        self.assertFalse(any(r['party']=='Example Supplier 1' for r in self.rows('payables')))

    def test_payables_boundary_bands_and_fully_paid_exclusion(self):
        bid=self.records[0]['bid']
        for days,key in [(0,'current'),(1,'days_1_30'),(30,'days_1_30'),(31,'days_31_60'),(60,'days_31_60'),(61,'days_61_90'),(90,'days_61_90'),(91,'days_90_plus')]:
            with self.subTest(days=days):
                with app.connect() as db:
                    db.execute('UPDATE supplier_bills SET due_date=? WHERE id=?',((payments.today()-timedelta(days=days)).isoformat(),bid))
                    b=payables.summary(db,db.execute('SELECT * FROM supplier_bills WHERE id=?',(bid,)).fetchone())
                    db.execute('UPDATE supplier_bills SET approval_hash=? WHERE id=?',(b['fingerprint'],bid))
                self.assertEqual(next(r for r in self.rows('payables') if r['party']=='Example Supplier 1')[key],'500.00')
        with app.connect() as db:db.execute('UPDATE supplier_payments SET amount=? WHERE bill_id=? AND reversed_at IS NULL',('600',bid))
        self.assertFalse(any(r['party']=='Example Supplier 1' for r in self.rows('payables')))

    def test_weighted_margin_zero_revenue_and_missing_terms(self):
        with app.connect() as db:
            db.execute("UPDATE shipments SET carrier='Combined carrier' WHERE id IN (?,?)",(self.records[0]['sid'],self.records[3]['sid']))
        combined=next(r for r in self.rows('profit_carrier') if r['group']=='Combined carrier')
        self.assertEqual(combined['margin'],'46.15')
        with app.connect() as db:
            sid=app.create(db,dict(reference='UNCONFIGURED',customer='No terms',email='demo@example.com',origin='A',destination='B',carrier='Unknown',tracking='Pending',eta=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()))
        r=next(r for r in self.rows('profit_customer') if r['group']=='No terms')
        self.assertIsNone(r['profit']);self.assertIsNone(r['margin']);self.assertEqual(r['currency'],'Unconfigured')
        with app.connect() as db:
            terms=dict(currency='TWD',revenue='0',quoted_cost='0',acceptance='Zero-charge test')
            db.execute('INSERT INTO billing_records(shipment_id,manual_terms) VALUES (?,?)',(sid,json.dumps(terms)))
        r=next(r for r in self.rows('profit_customer') if r['group']=='No terms')
        self.assertEqual(r['profit'],'0.00');self.assertIsNone(r['margin'])

    def test_cash_date_range_is_payment_dates_excluding_both_reversal_types(self):
        rows={r['currency']:r for r in self.rows('cash')}
        self.assertEqual(rows['TWD'],dict(currency='TWD',received='300.00',supplier_paid='100.00',net='200.00'))
        self.assertEqual(rows['USD'],dict(currency='USD',received='250.00',supplier_paid='40.00',net='210.00'))
        day=(payments.today()-timedelta(days=10)).isoformat()
        self.assertEqual(self.rows('cash',start=day,end=day,currency='TWD'),[dict(currency='TWD',received='0.00',supplier_paid='100.00',net='-100.00')])
        # Invoice cohort filtering retains payments outside that date range in balances.
        day=(payments.today()-timedelta(days=56)).isoformat()
        self.assertEqual(self.rows('receivables',start=day,end=day)[0]['total'],'700.00')

    def test_operations_denominators_delays_duration_and_fully_paid(self):
        rows={r['currency']:r for r in self.rows('operations')}
        self.assertEqual(rows['TWD']['delivery_samples'],1);self.assertEqual(rows['TWD']['missing_delivery_time'],1)
        self.assertEqual(rows['TWD']['on_time_percent'],'100.00');self.assertEqual(rows['TWD']['delivery_invoice_days'],'2.00')
        self.assertEqual(rows['USD']['on_time_percent'],'50.00');self.assertEqual(rows['USD']['exceptions_or_delays'],1)
        self.assertEqual(rows['USD']['fully_paid_samples'],1);self.assertEqual(rows['USD']['invoice_paid_days'],'33.00')
        with app.connect() as db:db.execute('UPDATE payments SET reversed_at=? WHERE invoice_id=? AND amount=?',(dispatch.now(),self.records[1]['iid'],'150'))
        self.assertEqual(next(r for r in self.rows('operations') if r['currency']=='USD')['fully_paid_samples'],0)

    def test_query_only_no_mutations_and_csv_reconciles_all_tables(self):
        with app.connect() as db:
            db.execute('PRAGMA query_only=ON');before=db.total_changes
            report=reports.state(db,self.admin)
            for key,t in report['tables'].items():
                raw,name=reports.export(db,self.admin,{},key)
                rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
                self.assertEqual(len(rows),len(t['rows']));self.assertTrue(name.endswith('.csv'))
                if rows:
                    first=t['rows'][0]
                    for col in t['columns']:self.assertEqual(rows[0][col['label']],'' if first.get(col['key']) is None else str(first[col['key']]))
            self.assertEqual(db.total_changes,before)

    def test_filters_validation_read_permissions_and_formula_safe_csv(self):
        self.assertTrue(all(r['currency']=='USD' for r in self.rows('profit_customer',currency='USD')))
        self.assertEqual(self.rows('profit_customer',start='2099-01-01'),[])
        for params in (dict(start='bad'),dict(start='20261001'),dict(start='2026-10-02',end='2026-10-01'),dict(currency='usd')):
            with self.assertRaises(ValueError):self.report(**params)
        with app.connect() as db:
            for role in ('Viewer','Sales','Operations'):
                with self.assertRaises(auth.AccessError):reports.state(db,self.users[role])
                with self.assertRaises(auth.AccessError):reports.export(db,self.users[role],{},'cash')
            db.execute("UPDATE shipments SET customer='=DANGEROUS()' WHERE id=?",(self.records[0]['sid'],))
            raw,_=reports.export(db,self.admin,{},'profit_customer')
            self.assertIn("'=DANGEROUS()",raw.decode('utf-8-sig'))

    def test_http_admin_only_json_csv_and_reject_invalid_filters(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        import http.client
        def request(path,cookie=None,data=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST' if data else 'GET',path,json.dumps(data) if data else None,{'Content-Type':'application/json',**({'Cookie':cookie} if cookie else {})})
            r=conn.getresponse();result=(r.status,r.read(),r.getheader('Set-Cookie'),r.getheader('Content-Type'));conn.close();return result
        try:
            self.assertEqual(request('/api/reports')[0],401)
            for role in self.users:
                login=request('/api/login',data=dict(username=role.lower(),password='Test-only-password'));cookie=login[2]
                for path in ('/api/reports','/api/reports/export?table=cash'):
                    response=request(path,cookie)
                    self.assertEqual(response[0],200 if role=='Admin' else 403)
                    if role=='Admin' and 'export' in path:self.assertIn('text/csv',response[3])
                if role=='Admin':
                    self.assertEqual(request('/api/reports?start=bad',cookie)[0],400)
                    self.assertEqual(request('/api/reports/export?table=unknown',cookie)[0],400)
        finally:server.shutdown();server.server_close();worker.join()
