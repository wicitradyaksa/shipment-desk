"""Manual bank-confirmed payment ledger and unsent collection drafts."""
import json
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
import auth
import dispatch
import invoices
import adjustments

METHODS=('Bank transfer','Cash','Cheque','Card','Other')


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS payments (
      id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
      amount TEXT NOT NULL, currency TEXT NOT NULL, payment_date TEXT NOT NULL,
      method TEXT NOT NULL, reference TEXT NOT NULL, reference_key TEXT NOT NULL, note TEXT NOT NULL,
      request_key TEXT UNIQUE NOT NULL, created TEXT NOT NULL, created_by INTEGER REFERENCES users(id),
      reversed_at TEXT, reversed_by INTEGER REFERENCES users(id), reversal_reason TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS active_payment_reference ON payments(invoice_id,reference_key) WHERE reversed_at IS NULL;
    CREATE TABLE IF NOT EXISTS collection_drafts (
      id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), invoice_version INTEGER NOT NULL,
      as_of TEXT NOT NULL, balance TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL,
      body TEXT NOT NULL, created TEXT NOT NULL, created_by INTEGER REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS payment_events (
      id INTEGER PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
      time TEXT NOT NULL, actor TEXT NOT NULL, message TEXT NOT NULL);
    ''')


def today(): return date.today()


def money(value):
    try:
        if isinstance(value,bool): raise ValueError()
        result=Decimal(str(value))
        if not result.is_finite() or result<=0 or result>1000000000 or result!=result.quantize(Decimal('.01')): raise ValueError()
        return result.quantize(Decimal('.01'))
    except (ValueError,InvalidOperation,TypeError):
        raise ValueError('Payment must be positive, at most 1,000,000,000, and have no more than two decimal places.')


def summary(db,row):
    i=invoices.decode(row);s=i['snapshot']
    entries=[dict(p) for p in db.execute('''SELECT p.*,u.name AS recorded_by_name,r.name AS reversed_by_name
      FROM payments p JOIN users u ON u.id=p.created_by LEFT JOIN users r ON r.id=p.reversed_by
      WHERE p.invoice_id=? ORDER BY p.created DESC,p.id''',(i['id'],))]
    paid=sum((Decimal(p['amount']) for p in entries if not p['reversed_at']),Decimal('0.00'))
    adjustment=adjustments.totals(db,i['id'])
    adjusted_total=Decimal(s['total'])-adjustment['credited'];net_paid=paid-adjustment['refunded']
    balance=max(Decimal('0.00'),adjusted_total-net_paid)
    refund_due=max(Decimal('0.00'),net_paid-adjusted_total)
    overdue_days=max(0,(today()-date.fromisoformat(s['due_date'])).days) if balance>0 else 0
    bucket=('Refund due' if refund_due>0 else 'Credited' if adjusted_total==0 else 'Paid') if balance==0 else 'Not overdue' if not overdue_days else '1–30 days' if overdue_days<=30 else '31–60 days' if overdue_days<=60 else '61–90 days' if overdue_days<=90 else '90+ days'
    drafts=[dict(r) for r in db.execute('SELECT * FROM collection_drafts WHERE invoice_id=? ORDER BY created DESC,id DESC',(i['id'],))]
    for d in drafts:d['stale']=d['invoice_version']!=i['version'] or d['as_of']!=today().isoformat() or balance==0
    return dict(id=i['id'],version=i['version'],number=i['number'],customer=s['customer'],email=s['email'],
        currency=s['currency'],total=s['total'],paid=str(paid),balance=str(balance),due_date=s['due_date'],
        credited=str(adjustment['credited']),refunded=str(adjustment['refunded']),net_paid=str(net_paid),adjusted_total=str(adjusted_total),refund_due=str(refund_due),
        status='Refund due' if refund_due>0 else 'Credited' if adjusted_total==0 else 'Paid' if balance==0 else 'Partially paid' if net_paid else 'Unpaid',overdue=overdue_days>0,
        overdue_days=overdue_days,aging=bucket,payments=entries,drafts=drafts)


def state(db,user):
    auth.require(user,('Admin',))
    rows=[summary(db,r) for r in db.execute("SELECT * FROM invoices WHERE status='Issued' ORDER BY created DESC")]
    totals={}
    for r in rows:
        t=totals.setdefault(r['currency'],dict(outstanding=Decimal(0),overdue=Decimal(0),received=Decimal(0),refund_due=Decimal(0),credited=Decimal(0),refunded=Decimal(0)))
        t['outstanding']+=Decimal(r['balance']);t['received']+=Decimal(r['paid'])
        t['refund_due']+=Decimal(r['refund_due']);t['credited']+=Decimal(r['credited']);t['refunded']+=Decimal(r['refunded'])
        if r['overdue']: t['overdue']+=Decimal(r['balance'])
    return dict(as_of=today().isoformat(),invoices=rows,methods=METHODS,
                totals={c:{k:str(v.quantize(Decimal('.01'))) for k,v in t.items()} for c,t in totals.items()},
                events=[dict(e) for e in db.execute('SELECT * FROM payment_events ORDER BY id DESC')])


def log(db,iid,user,message):
    db.execute('INSERT INTO payment_events(invoice_id,time,actor,message) VALUES (?,?,?,?)',
               (iid,dispatch.now(),f"{user['name']} (@{user['username']})",message))


def action(db,path,data,user):
    auth.require(user,('Admin',));db.execute('BEGIN IMMEDIATE')
    iid=dispatch.clean(data,'invoice_id',True,80)
    row=db.execute('SELECT * FROM invoices WHERE id=?',(iid,)).fetchone()
    if not row or row['status']!='Issued':raise ValueError('Choose an issued invoice.')
    op=path.rsplit('/',1)[-1]
    if op=='record':
        # Idempotent retries are checked before the optimistic version check.
        request_key=dispatch.clean(data,'request_key',True,80)
        try: uuid.UUID(request_key)
        except ValueError: raise ValueError('Invalid payment request. Refresh and try again.')
        value=money(data.get('amount'));currency=dispatch.clean(data,'currency',True,3)
        method=dispatch.clean(data,'method',True,40);reference=dispatch.clean(data,'reference',True,150)
        note=dispatch.clean(data,'note',False,1500)
        try:
            payment_date=date.fromisoformat(data.get('payment_date',''))
            if payment_date>today() or payment_date.year<1900:raise ValueError()
        except (ValueError,TypeError):raise ValueError('Enter the actual receipt date, from 1900 through today.')
        if method not in METHODS:raise ValueError('Choose a payment method.')
        if data.get('confirmed') is not True:raise ValueError('Confirm the payment against the bank transaction or receipt.')
        previous=db.execute('SELECT * FROM payments WHERE request_key=?',(request_key,)).fetchone()
        if previous:
            expected=dict(invoice_id=iid,amount=str(value),currency=currency,payment_date=payment_date.isoformat(),method=method,reference=reference,note=note)
            if previous['created_by']!=user['id'] or any(previous[k]!=v for k,v in expected.items()):raise auth.AccessError('This payment request was already used with different details. Refresh before retrying.',409)
            return dict(ok=True,id=previous['id'],duplicate=True)
    if data.get('version')!=row['version']:raise auth.AccessError('Invoice or payment records changed. Refresh before continuing.',409)
    s=summary(db,row)
    if op=='record':
        if currency!=s['currency']:raise ValueError('Record payment in the invoice currency. Currency conversion is not supported.')
        if value>Decimal(s['balance']):raise ValueError('Payment exceeds the outstanding balance. Review the amount before recording.')
        key=reference.casefold()
        if db.execute('SELECT 1 FROM payments WHERE invoice_id=? AND reference_key=? AND reversed_at IS NULL',(iid,key)).fetchone():raise ValueError('This invoice already has an active payment with that reference.')
        pid=str(uuid.uuid4())
        db.execute('INSERT INTO payments(id,invoice_id,amount,currency,payment_date,method,reference,reference_key,note,request_key,created,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (pid,iid,str(value),currency,payment_date.isoformat(),method,reference,key,note,request_key,dispatch.now(),user['id']))
        log(db,iid,user,f'Payment recorded: {currency} {value}; received {payment_date}; {method}; reference {reference}.'+(' '+note if note else ''))
    elif op=='reverse':
        pid=dispatch.clean(data,'payment_id',True,80)
        p=db.execute('SELECT * FROM payments WHERE id=? AND invoice_id=?',(pid,iid)).fetchone()
        if not p or p['reversed_at']:raise ValueError('Choose an active payment entry.')
        reason=dispatch.clean(data,'reason',True,1500)
        if Decimal(s['paid'])-Decimal(p['amount'])<Decimal(s['refunded']):raise ValueError('Correct refund entries before reversing receipts that funded them.')
        if data.get('confirmed') is not True:raise ValueError('Confirm reversal of this recorded payment entry.')
        db.execute('UPDATE payments SET reversed_at=?,reversed_by=?,reversal_reason=? WHERE id=?',(dispatch.now(),user['id'],reason,pid))
        log(db,iid,user,f"Payment entry reversed: {p['currency']} {p['amount']}; reference {p['reference']}. Reason: {reason}. No money was moved.")
    elif op=='draft':
        if Decimal(s['balance'])<=0:raise ValueError('This invoice is paid. No payment reminder is needed.')
        existing=db.execute('SELECT id FROM collection_drafts WHERE invoice_id=? AND invoice_version=? AND as_of=?',(iid,row['version'],today().isoformat())).fetchone()
        if existing:return dict(ok=True,id=existing['id'])
        snap=json.loads(row['snapshot'])
        due=f"was due on {s['due_date']} and is {s['overdue_days']} day(s) overdue" if s['overdue'] else f"is due on {s['due_date']}"
        subject=f"Payment reminder — invoice {s['number']}"
        body=f"Hello {s['customer']},\n\nOur records as of {today()} show an outstanding balance of {s['currency']} {Decimal(s['balance']):,.2f} for invoice {s['number']}. The invoice {due}.\n\nInvoice total: {s['currency']} {Decimal(s['total']):,.2f}\nPayments recorded: {s['currency']} {Decimal(s['paid']):,.2f}\nCredits: {s['currency']} {s['credited']}\nRefunds recorded: {s['currency']} {s['refunded']}\nRemaining balance: {s['currency']} {Decimal(s['balance']):,.2f}\n\nPlease confirm your expected payment date. If payment has already been made, please share the transaction reference so we can reconcile it.\n\nThank you,\n{snap['company']['company']}"
        did=str(uuid.uuid4())
        db.execute('INSERT INTO collection_drafts(id,invoice_id,invoice_version,as_of,balance,recipient,subject,body,created,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (did,iid,row['version'],today().isoformat(),s['balance'],s['email'],subject,body,dispatch.now(),user['id']))
        log(db,iid,user,f"Unsent reminder draft prepared for {s['email']}; outstanding {s['currency']} {s['balance']}.")
        return dict(ok=True,id=did)
    else:raise ValueError('Unknown payment action.')
    db.execute('UPDATE invoices SET version=version+1 WHERE id=?',(iid,))
    return dict(ok=True,id=pid)
