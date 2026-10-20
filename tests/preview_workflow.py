"""Disposable browser-QA workspace. Never uses the real database."""
import sys
import json
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import auth
import sales
import dispatch
import base64
import billing
import invoices
from datetime import date, timedelta
from test_sales import pricing_fixture, specs_fixture

if __name__ == '__main__':
    if '--adjustments' in sys.argv and '--payments' not in sys.argv:sys.argv.append('--payments')
    with tempfile.TemporaryDirectory(prefix='shipment-qa-') as directory:
        app.DB = Path(directory) / 'qa.sqlite3'
        app.init_db()
        with app.connect() as db:
            for role in ('Admin', 'Sales', 'Operations', 'Viewer'):
                auth.add_user(db, dict(username=role.lower(), name='QA '+role, role=role, password='Browser-test-only-2026'))
            admin = auth.public(db.execute('SELECT * FROM users WHERE role="Admin"').fetchone())
            seller = auth.public(db.execute('SELECT * FROM users WHERE role="Sales"').fetchone())
        with app.connect() as db:
            sales.action(db, '/api/pricing', pricing_fixture(), admin, app.create)
        with app.connect() as db:
            sales.action(db, '/api/quotes', specs_fixture(customer='QA Standard freight'), seller, app.create)
        with app.connect() as db:
            sales.action(db, '/api/quotes', specs_fixture(customer='QA Cold chain', cargo_id='cold', route_id='', destination='Custom port', outbound_km='100', return_km='80', charter_days='2'), seller, app.create)
        with app.connect() as db:
            q = db.execute("SELECT * FROM quotes WHERE status='Proposal ready'").fetchone()
            sales.action(db, '/api/quotes/sent', dict(id=q['id'],version=q['version'],note='QA client email'), seller, app.create)
        with app.connect() as db:
            q = db.execute("SELECT * FROM quotes WHERE status='Awaiting client'").fetchone()
            sales.action(db, '/api/quotes/close', dict(id=q['id'],version=q['version'],acceptance='QA client approval',confirmed=True), seller, app.create)
        if any(flag in sys.argv for flag in ('--billing','--invoices','--payments')):
            with app.connect() as db:
                sid=db.execute('SELECT id FROM shipments').fetchone()[0]
                db.execute("UPDATE shipments SET status='Delivered' WHERE id=?",(sid,))
            for category,name in [('Proof of delivery','qa-delivery-proof.txt'),('Other','qa-carrier-invoice.txt')]:
                with app.connect() as db:
                    dispatch.action(db,'/api/documents/upload',dict(shipment_id=sid,name=name,category=category,
                        content=base64.b64encode(('Fictional QA evidence: '+category).encode()).decode()),admin,app.notify)
        if '--invoices' in sys.argv or '--payments' in sys.argv:
            from test_invoices import settings_fixture
            with app.connect() as db:
                doc=db.execute("SELECT id FROM shipment_documents WHERE category='Other'").fetchone()[0]
                billing.action(db,'/api/billing/costs',dict(shipment_id=sid,version=0,lines=[dict(label='Carrier invoice',amount='1200',document_id=doc)]),admin)
            with app.connect() as db:
                r=billing.record(db,sid)
                billing.action(db,'/api/billing/submit',dict(shipment_id=sid,version=r['version'],confirmed=True),admin)
            with app.connect() as db:
                s=billing.summary(db,dict(db.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()))
                billing.action(db,'/api/billing/approve',dict(shipment_id=sid,version=s['record']['version'],fingerprint=s['fingerprint'],confirmed=True),admin)
            with app.connect() as db:
                invoices.action(db,'/api/invoices/settings',settings_fixture(),admin)
        if '--payments' in sys.argv:
            with app.connect() as db:
                iid=invoices.action(db,'/api/invoices/create',dict(shipment_id=sid),admin)['id']
            with app.connect() as db:
                invoices.action(db,'/api/invoices/save',dict(id=iid,version=1,customer_address='88 QA Customer Street',
                    issue_date=(date.today()-timedelta(days=40)).isoformat(),due_date=(date.today()-timedelta(days=10)).isoformat()),admin)
            with app.connect() as db:
                invoices.action(db,'/api/invoices/issue',dict(id=iid,version=2,confirmed=True),admin)
        if '--reports' in sys.argv or '--integrations' in sys.argv:
            from test_reports import seed_reports
            with app.connect() as db:
                seed_reports(db,admin,with_pdfs=True)
        if '--adjustments' in sys.argv:
            import adjustments, payments, uuid
            from decimal import Decimal,ROUND_HALF_UP
            with app.connect() as db:
                invoice=db.execute("SELECT * FROM invoices WHERE status='Issued' LIMIT 1").fetchone()
                iid=invoice['id'];total=Decimal(json.loads(invoice['snapshot'])['total'])
            def adjust_call(op,data):
                with app.connect() as db:return adjustments.action(db,'/api/adjustments/'+op,data,admin)
            with app.connect() as db:
                payments.action(db,'/api/payments/record',dict(invoice_id=iid,version=invoice['version'],amount=str((total/2).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),currency=json.loads(invoice['snapshot'])['currency'],payment_date=payments.today().isoformat(),method='Bank transfer',reference='FICTIONAL-RECEIPT',request_key=str(uuid.uuid4()),confirmed=True),admin)
            with app.connect() as db:version=db.execute('SELECT version FROM invoices WHERE id=?',(iid,)).fetchone()[0]
            payload=dict(invoice_id=iid,version=version,amount=str((total*Decimal('.9')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),reason='Fictional demo service adjustment',credit_date=payments.today().isoformat())
            preview=adjust_call('preview',payload)
            adjust_call('issue',dict(payload,request_key=str(uuid.uuid4()),fingerprint=preview['fingerprint'],confirmed=True))
            with app.connect() as db:version=db.execute('SELECT version FROM invoices WHERE id=?',(iid,)).fetchone()[0]
            adjust_call('refund',dict(invoice_id=iid,version=version,amount=str((total*Decimal('.1')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),refund_date=payments.today().isoformat(),method='Bank transfer',reference='FICTIONAL-REFUND',request_key=str(uuid.uuid4()),confirmed=True))
        server = app.ThreadingHTTPServer(('127.0.0.1', 8881), app.Handler)
        print('Disposable QA workspace at http://127.0.0.1:8881', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
