"""Immutable customer credit notes and manually verified refund ledger."""
import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal,ROUND_HALF_UP
import auth
import dispatch
import invoices


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS credit_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,number TEXT UNIQUE,
      invoice_id TEXT NOT NULL REFERENCES invoices(id),amount TEXT NOT NULL,net TEXT NOT NULL,tax TEXT NOT NULL,
      credit_date TEXT NOT NULL,reason TEXT NOT NULL,snapshot TEXT NOT NULL,pdf BLOB NOT NULL,
      request_key TEXT UNIQUE NOT NULL,created TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS customer_refunds(id TEXT PRIMARY KEY,invoice_id TEXT NOT NULL REFERENCES invoices(id),
      amount TEXT NOT NULL,refund_date TEXT NOT NULL,method TEXT NOT NULL,reference TEXT NOT NULL,reference_key TEXT NOT NULL,
      request_key TEXT UNIQUE NOT NULL,created TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),
      reversed_at TEXT,reversed_by INTEGER REFERENCES users(id),reason TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS active_refund_reference ON customer_refunds(invoice_id,reference_key) WHERE reversed_at IS NULL;
    ''')


def totals(db,iid):
    credits=list(db.execute('SELECT amount,net,tax FROM credit_notes WHERE invoice_id=?',(iid,)))
    refunds=list(db.execute('SELECT amount FROM customer_refunds WHERE invoice_id=? AND reversed_at IS NULL',(iid,)))
    return dict(credited=sum((Decimal(r['amount']) for r in credits),Decimal('0.00')),
                credit_net=sum((Decimal(r['net']) for r in credits),Decimal('0.00')),
                credit_tax=sum((Decimal(r['tax']) for r in credits),Decimal('0.00')),
                refunded=sum((Decimal(r['amount']) for r in refunds),Decimal('0.00')))


def credit_preview(db,data,user):
    import payments
    auth.require(user,('Admin',))
    row=db.execute("SELECT * FROM invoices WHERE id=? AND status='Issued'",(data.get('invoice_id'),)).fetchone()
    if not row:raise ValueError('Choose an issued invoice.')
    if data.get('version')!=row['version']:raise auth.AccessError('Invoice or payment records changed. Refresh first.',409)
    amount=payments.money(data.get('amount'));reason=dispatch.clean(data,'reason',True,1500)
    s=json.loads(row['snapshot']);existing=totals(db,row['id'])
    try:
        day=date.fromisoformat(data.get('credit_date',''))
        if not date.fromisoformat(s['issue_date'])<=day<=payments.today():raise ValueError()
    except (ValueError,TypeError):raise ValueError('Credit date must be from the invoice date through today.')
    if amount+existing['credited']>Decimal(s['total']):raise ValueError('Credits cannot exceed the original invoice total.')
    tax=((existing['credited']+amount)*Decimal(s['tax'])/Decimal(s['total'])).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)-existing['credit_tax']
    net=amount-tax
    snapshot=json.loads(row['snapshot'])
    snapshot.update(net=str(net),tax=str(tax),total=str(amount),issue_date=day.isoformat(),due_date=day.isoformat(),
      lines=[dict(label='Credit against invoice '+row['number'],amount=str(net))],
      notes='Original invoice: '+row['number']+'\nReason: '+reason+'\nThis credit reduces the invoice amount. Any refund is recorded separately.')
    snapshot['company']=dict(snapshot['company'],bank='',terms='Credit note against the original invoice; no payment is requested.')
    result=dict(invoice_id=row['id'],version=row['version'],original_invoice=row['number'],amount=str(amount),net=str(net),tax=str(tax),credit_date=day.isoformat(),reason=reason,snapshot=snapshot)
    result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def state(db,user):
    import payments
    auth.require(user,('Admin',))
    return dict(today=payments.today().isoformat(),methods=payments.METHODS,
      invoices=[payments.summary(db,r) for r in db.execute("SELECT * FROM invoices WHERE status='Issued' ORDER BY created DESC")],
      credits=[dict(r) for r in db.execute('SELECT id,number,invoice_id,amount,net,tax,credit_date,reason,created FROM credit_notes ORDER BY id DESC')],
      refunds=[dict(r) for r in db.execute('SELECT * FROM customer_refunds ORDER BY created DESC')])


def pdf(db,cid,user):
    auth.require(user,('Admin',));row=db.execute('SELECT number,pdf FROM credit_notes WHERE id=?',(cid,)).fetchone()
    if not row:raise ValueError('Credit note not found.')
    return bytes(row['pdf']),row['number']+'.pdf'


def action(db,path,data,user):
    import payments
    auth.require(user,('Admin',));op=path.rsplit('/',1)[-1]
    if op=='preview':return credit_preview(db,data,user)
    db.execute('BEGIN IMMEDIATE')
    iid=dispatch.clean(data,'invoice_id',True,80)
    row=db.execute("SELECT * FROM invoices WHERE id=? AND status='Issued'",(iid,)).fetchone()
    if not row:raise ValueError('Choose an issued invoice.')
    if op in ('issue','refund'):
        request=dispatch.clean(data,'request_key',True,80)
        try:uuid.UUID(request)
        except ValueError:raise ValueError('Invalid request key. Refresh first.')
        table='credit_notes' if op=='issue' else 'customer_refunds'
        old=db.execute('SELECT * FROM '+table+' WHERE request_key=?',(request,)).fetchone()
        if old:
            fields=('invoice_id','amount','credit_date','reason') if op=='issue' else ('invoice_id','amount','refund_date','method','reference')
            expected={k:str(data.get(k)) for k in fields};expected['amount']=str(payments.money(data.get('amount')))
            for key in ('reason','reference'):
                if key in expected:expected[key]=expected[key].strip()
            if old['created_by']!=user['id'] or any(str(old[k])!=expected[k] for k in fields):raise auth.AccessError('Request key already used for different details.',409)
            return dict(ok=True,id=old['id'],duplicate=True)
    if data.get('version')!=row['version']:raise auth.AccessError('Invoice or ledger changed. Refresh first.',409)
    if data.get('confirmed') is not True:raise ValueError('Confirm review before recording this action.')
    if op=='issue':
        p=credit_preview(db,data,user)
        if data.get('fingerprint')!=p['fingerprint']:raise auth.AccessError('Credit preview changed. Review it again.',409)
        # Reserve the number in the same transaction that stores the immutable PDF.
        cid=db.execute('INSERT INTO credit_notes(invoice_id,amount,net,tax,credit_date,reason,snapshot,pdf,request_key,created,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          (iid,p['amount'],p['net'],p['tax'],p['credit_date'],p['reason'],json.dumps(p['snapshot']),b'',request,dispatch.now(),user['id'])).lastrowid
        number=f'CN-{cid:06d}'
        from invoice_pdf import render
        document=render(dict(number=number,status='Issued',document_type='Credit note',snapshot=p['snapshot']))
        db.execute('UPDATE credit_notes SET number=?,pdf=? WHERE id=?',(number,document,cid))
        message=f"Credit note {number} issued: {p['snapshot']['currency']} {p['amount']}; net {p['net']}, tax {p['tax']}. {p['reason']}"
    elif op=='refund':
        s=payments.summary(db,row);amount=payments.money(data.get('amount'))
        if amount>Decimal(s['refund_due']):raise ValueError('Refund exceeds the customer credit available.')
        reference=dispatch.clean(data,'reference',True,150);method=data.get('method')
        if method not in payments.METHODS:raise ValueError('Choose a refund method.')
        try:
            day=date.fromisoformat(data.get('refund_date',''))
            if day>payments.today() or day.year<1900:raise ValueError()
        except (ValueError,TypeError):raise ValueError('Enter the actual completed refund date through today.')
        original=json.loads(row['snapshot'])
        credits_asof=sum((Decimal(r[0]) for r in db.execute('SELECT amount FROM credit_notes WHERE invoice_id=? AND credit_date<=?',(iid,day.isoformat()))),Decimal(0))
        receipts_asof=sum((Decimal(r[0]) for r in db.execute('SELECT amount FROM payments WHERE invoice_id=? AND reversed_at IS NULL AND payment_date<=?',(iid,day.isoformat()))),Decimal(0))
        refunds_asof=sum((Decimal(r[0]) for r in db.execute('SELECT amount FROM customer_refunds WHERE invoice_id=? AND reversed_at IS NULL AND refund_date<=?',(iid,day.isoformat()))),Decimal(0))
        if day<date.fromisoformat(original['issue_date']) or amount>receipts_asof-refunds_asof-(Decimal(original['total'])-credits_asof):raise ValueError('Refund date precedes the available credit and receipts.')
        key=' '.join(reference.casefold().split())
        if db.execute('SELECT 1 FROM customer_refunds WHERE invoice_id=? AND reference_key=? AND reversed_at IS NULL',(iid,key)).fetchone():raise ValueError('Refund reference is already recorded on this invoice.')
        cid=str(uuid.uuid4())
        db.execute('INSERT INTO customer_refunds(id,invoice_id,amount,refund_date,method,reference,reference_key,request_key,created,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)',
          (cid,iid,str(amount),day.isoformat(),method,reference,key,request,dispatch.now(),user['id']))
        message=f"Completed customer refund recorded: {s['currency']} {amount}; {reference}. No money moved by this app."
    elif op=='refund_reverse':
        refund=db.execute('SELECT * FROM customer_refunds WHERE id=? AND invoice_id=?',(data.get('refund_id'),iid)).fetchone()
        if not refund or refund['reversed_at']:raise ValueError('Choose an active refund entry.')
        reason=dispatch.clean(data,'reason',True,1500);cid=refund['id']
        db.execute('UPDATE customer_refunds SET reversed_at=?,reversed_by=?,reason=? WHERE id=?',(dispatch.now(),user['id'],reason,cid))
        message='Refund ledger entry reversed: '+cid+'. '+reason+'. No money moved.'
    else:raise ValueError('Unknown credit/refund action.')
    payments.log(db,iid,user,message)
    db.execute('UPDATE invoices SET version=version+1 WHERE id=?',(iid,))
    return dict(ok=True,id=cid)
