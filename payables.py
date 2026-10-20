"""Supplier bills matched to submitted shipment costs; recorded payments only."""
import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
import auth
import billing
import dispatch
import payments


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS suppliers(id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS supplier_bills (
      id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL REFERENCES suppliers(id), shipment_id TEXT NOT NULL REFERENCES shipments(id),
      reference TEXT NOT NULL, reference_key TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
      bill_date TEXT NOT NULL, due_date TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES shipment_documents(id),
      note TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Draft',version INTEGER NOT NULL DEFAULT 1,
      created TEXT NOT NULL,approval_hash TEXT,approved_at TEXT,approved_by INTEGER REFERENCES users(id),review_note TEXT NOT NULL DEFAULT '');
    CREATE UNIQUE INDEX IF NOT EXISTS supplier_bill_reference ON supplier_bills(supplier_id,reference_key) WHERE status!='Cancelled';
    CREATE UNIQUE INDEX IF NOT EXISTS supplier_bill_document ON supplier_bills(document_id) WHERE status!='Cancelled';
    CREATE TABLE IF NOT EXISTS supplier_payments (
      id TEXT PRIMARY KEY,bill_id TEXT NOT NULL REFERENCES supplier_bills(id),amount TEXT NOT NULL,
      payment_date TEXT NOT NULL,method TEXT NOT NULL,reference TEXT NOT NULL,reference_key TEXT NOT NULL,
      request_key TEXT UNIQUE NOT NULL,created TEXT NOT NULL,created_by INTEGER REFERENCES users(id),
      reversed_at TEXT,reversed_by INTEGER REFERENCES users(id),reason TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS supplier_payment_reference ON supplier_payments(bill_id,reference_key) WHERE reversed_at IS NULL;
    CREATE TABLE IF NOT EXISTS payable_events(id INTEGER PRIMARY KEY,bill_id TEXT REFERENCES supplier_bills(id),time TEXT NOT NULL,actor TEXT NOT NULL,message TEXT NOT NULL);
    ''')


def key(text):return ' '.join(text.split()).casefold()


def log(db,bid,user,message):
    db.execute('INSERT INTO payable_events(bill_id,time,actor,message) VALUES (?,?,?,?)',(bid,dispatch.now(),f"{user['name']} (@{user['username']})",message))


def summary(db,row):
    b=dict(row)
    b['supplier']=db.execute('SELECT name FROM suppliers WHERE id=?',(b['supplier_id'],)).fetchone()[0]
    shipment=db.execute('SELECT * FROM shipments WHERE id=?',(b['shipment_id'],)).fetchone()
    s=billing.summary(db,dict(shipment));b['shipment_reference']=s['reference']
    doc=db.execute('SELECT * FROM shipment_documents WHERE id=?',(b['document_id'],)).fetchone()
    lines=[c for c in s['record']['costs'] if c['document_id']==b['document_id']]
    expected=sum((Decimal(c['amount']) for c in lines),Decimal('0.00'))
    blockers=[]
    if not doc or doc['archived_at'] or doc['shipment_id']!=b['shipment_id']:blockers.append('Supplier invoice document is unavailable or archived.')
    if not s['record']['submitted_at']:blockers.append('Operations must submit final shipment costs.')
    if not s['terms'] or s['terms']['currency']!=b['currency']:blockers.append('Bill currency does not match the shipment cost currency.')
    if not lines:blockers.append('Link at least one actual cost line to this supplier invoice document.')
    if expected!=Decimal(b['amount']):blockers.append('Supplier bill total does not match its linked actual costs.')
    fingerprint=hashlib.sha256(json.dumps(dict(bill={k:b[k] for k in ('supplier_id','shipment_id','reference','amount','currency','bill_date','due_date','document_id','note')},
        lines=lines,submitted=s['record']['submitted_at'],currency=s['terms']['currency'] if s['terms'] else None,document=doc['checksum'] if doc and not doc['archived_at'] else None),sort_keys=True).encode()).hexdigest()
    entries=[dict(p) for p in db.execute('SELECT p.*,u.name AS actor FROM supplier_payments p JOIN users u ON u.id=p.created_by WHERE bill_id=? ORDER BY created DESC',(b['id'],))]
    paid=sum((Decimal(p['amount']) for p in entries if not p['reversed_at']),Decimal('0.00'))
    balance=Decimal(b['amount'])-paid
    valid=b['status']=='Approved' and b['approval_hash']==fingerprint and not blockers
    stage='Cancelled' if b['status']=='Cancelled' else 'Paid' if balance==0 else 'Approval outdated' if b['status']=='Approved' and not valid else 'Partially paid' if paid else b['status']
    b.update(expected=str(expected),variance=str(Decimal(b['amount'])-expected),blockers=blockers,fingerprint=fingerprint,
        approval_valid=valid,stage=stage,paid=str(paid),balance=str(balance),payments=entries,
        overdue=b['status']!='Cancelled' and balance>0 and date.fromisoformat(b['due_date'])<payments.today())
    return b


def state(db,user):
    auth.require(user,('Admin','Operations'))
    bills=[summary(db,r) for r in db.execute('SELECT * FROM supplier_bills ORDER BY created DESC')]
    shipments=[]
    for row in db.execute('SELECT * FROM shipments ORDER BY created DESC'):
        s=billing.summary(db,dict(row))
        shipments.append(dict(id=s['id'],reference=s['reference'],currency=s['terms']['currency'] if s['terms'] else None,documents=s['documents'],costs=s['record']['costs']))
    totals={}
    for b in bills:
        if b['status']=='Cancelled':continue
        t=totals.setdefault(b['currency'],dict(outstanding=Decimal(0),approved=Decimal(0),overdue=Decimal(0)))
        t['outstanding']+=Decimal(b['balance'])
        if b['approval_valid']:t['approved']+=Decimal(b['balance'])
        if b['overdue']:t['overdue']+=Decimal(b['balance'])
    return dict(bills=bills,shipments=shipments,suppliers=[dict(r) for r in db.execute('SELECT * FROM suppliers ORDER BY name')],
        totals={c:{k:str(v) for k,v in t.items()} for c,t in totals.items()},methods=payments.METHODS,today=payments.today().isoformat(),
        events=[dict(e) for e in db.execute('SELECT * FROM payable_events ORDER BY id DESC')])


def action(db,path,data,user):
    auth.require(user,('Admin','Operations'));db.execute('BEGIN IMMEDIATE');op=path.rsplit('/',1)[-1]
    if op=='supplier':
        name=dispatch.clean(data,'name',True,150);existing=db.execute('SELECT id FROM suppliers WHERE name_key=?',(key(name),)).fetchone()
        if existing:return dict(ok=True,id=existing['id'])
        sid=str(uuid.uuid4());db.execute('INSERT INTO suppliers VALUES (?,?,?)',(sid,name,key(name)));return dict(ok=True,id=sid)
    if op in ('create','save'):
        supplier_id=dispatch.clean(data,'supplier_id',True,80);shipment_id=dispatch.clean(data,'shipment_id',True,80)
        if not db.execute('SELECT 1 FROM suppliers WHERE id=?',(supplier_id,)).fetchone():raise ValueError('Select a supplier.')
        shipment=db.execute('SELECT * FROM shipments WHERE id=?',(shipment_id,)).fetchone()
        if not shipment:raise ValueError('Select a shipment.')
        s=billing.summary(db,dict(shipment))
        if not s['terms']:raise ValueError('Configure the shipment commercial terms and currency in Costs & billing first.')
        reference=dispatch.clean(data,'reference',True,150);value=payments.money(data.get('amount'))
        document_id=dispatch.clean(data,'document_id',True,80)
        if document_id not in {d['id'] for d in s['documents']}:raise ValueError('Select an active document from this shipment.')
        try:
            bill_date=date.fromisoformat(data.get('bill_date',''));due_date=date.fromisoformat(data.get('due_date',''))
            if due_date<bill_date or bill_date>payments.today() or bill_date.year<1900:raise ValueError()
        except (ValueError,TypeError):raise ValueError('Enter a valid bill date no later than today and a due date on or after it.')
        note=dispatch.clean(data,'note',False,1500)
        fields=dict(supplier_id=supplier_id,shipment_id=shipment_id,reference=reference,reference_key=key(reference),amount=str(value),currency=s['terms']['currency'],bill_date=bill_date.isoformat(),due_date=due_date.isoformat(),document_id=document_id,note=note)
        bid=dispatch.clean(data,'id',True,80) if op=='save' else str(uuid.uuid4())
        if db.execute("SELECT 1 FROM supplier_bills WHERE status!='Cancelled' AND id!=? AND ((supplier_id=? AND reference_key=?) OR document_id=?)",(bid,supplier_id,key(reference),document_id)).fetchone():raise ValueError('Duplicate supplier invoice number or invoice document. Open the existing bill instead.')
        if op=='create':
            fields.update(id=bid,created=dispatch.now())
            db.execute('INSERT INTO supplier_bills('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(fields.values()))
            log(db,bid,user,'Supplier bill draft created.');return dict(ok=True,id=bid)
    else:bid=dispatch.clean(data,'id',True,80)
    row=db.execute('SELECT * FROM supplier_bills WHERE id=?',(bid,)).fetchone()
    if not row:raise ValueError('Supplier bill not found.')
    b=summary(db,row)
    if op in ('approve','return','reopen','cancel','pay','reverse'):auth.require(user,('Admin',))
    if op=='pay':
        request_key=dispatch.clean(data,'request_key',True,80)
        try:uuid.UUID(request_key)
        except ValueError:raise ValueError('Invalid payment request. Refresh and retry.')
        value=payments.money(data.get('amount'));reference=dispatch.clean(data,'reference',True,150);method=dispatch.clean(data,'method',True,40)
        try:
            paid_date=date.fromisoformat(data.get('payment_date',''))
            if paid_date>payments.today() or paid_date.year<1900:raise ValueError()
        except (ValueError,TypeError):raise ValueError('Payment date must be a valid actual date no later than today.')
        if method not in payments.METHODS or data.get('confirmed') is not True:raise ValueError('Select a method and confirm the completed payment against the bank record.')
        old=db.execute('SELECT * FROM supplier_payments WHERE request_key=?',(request_key,)).fetchone()
        if old:
            expected=dict(bill_id=bid,amount=str(value),payment_date=paid_date.isoformat(),method=method,reference=reference,created_by=user['id'])
            if any(old[k]!=v for k,v in expected.items()):raise auth.AccessError('Payment request already used with different details. Refresh.',409)
            return dict(ok=True,id=bid,duplicate=True)
    if data.get('version')!=b['version']:raise auth.AccessError('Supplier bill changed. Refresh before continuing.',409)
    if op=='save':
        if b['status'] not in ('Draft','Changes requested'):raise ValueError('Only drafts or returned bills may be edited.')
        db.execute('UPDATE supplier_bills SET '+','.join(k+'=?' for k in fields)+",status='Draft',approval_hash=NULL WHERE id=?",(*fields.values(),bid))
        message='Supplier bill draft revised.'
    elif op=='submit':
        if b['status'] not in ('Draft','Changes requested'):raise ValueError('Only drafts or returned bills may be submitted.')
        db.execute("UPDATE supplier_bills SET status='Submitted',approval_hash=NULL WHERE id=?",(bid,));message='Supplier bill submitted for Admin review.'
    elif op=='approve':
        if b['status'] not in ('Submitted','Approved'):raise ValueError('Submit the bill before approval.')
        if b['approval_valid']:raise ValueError('This bill is already approved.')
        if b['blockers']:raise ValueError('Resolve the cost match first: '+' '.join(b['blockers']))
        if data.get('fingerprint')!=b['fingerprint']:raise auth.AccessError('Costs or documents changed. Refresh and review again.',409)
        if data.get('confirmed') is not True:raise ValueError('Confirm review of the supplier invoice and matched costs.')
        db.execute("UPDATE supplier_bills SET status='Approved',approval_hash=?,approved_at=?,approved_by=? WHERE id=?",(b['fingerprint'],dispatch.now(),user['id'],bid))
        message=f"Admin approved {b['currency']} {b['amount']} against matching submitted shipment costs."
    elif op in ('return','reopen','cancel'):
        reason=dispatch.clean(data,'reason',True,1500)
        if Decimal(b['paid'])>0:raise ValueError('Reverse incorrect recorded payments before changing or cancelling the bill. This does not refund money.')
        if b['status']=='Cancelled':raise ValueError('This bill is cancelled.')
        if op=='return' and b['status']!='Submitted':raise ValueError('Only submitted bills may be returned.')
        if op=='reopen' and b['status']!='Approved':raise ValueError('Only approved bills need reopening.')
        status={'return':'Changes requested','reopen':'Draft','cancel':'Cancelled'}[op]
        db.execute('UPDATE supplier_bills SET status=?,approval_hash=NULL,review_note=? WHERE id=?',(status,reason,bid));message=status+': '+reason
    elif op=='pay':
        if not b['approval_valid']:raise ValueError('Current matching Admin approval is required before recording payment.')
        if value>Decimal(b['balance']):raise ValueError('Payment exceeds the remaining supplier bill balance.')
        if db.execute('SELECT 1 FROM supplier_payments WHERE bill_id=? AND reference_key=? AND reversed_at IS NULL',(bid,key(reference))).fetchone():raise ValueError('This bill already has a payment with that reference.')
        db.execute('INSERT INTO supplier_payments(id,bill_id,amount,payment_date,method,reference,reference_key,request_key,created,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()),bid,str(value),paid_date.isoformat(),method,reference,key(reference),request_key,dispatch.now(),user['id']))
        message=f'Completed supplier payment recorded: {b["currency"]} {value}; {method}; {reference}; {paid_date}. No money moved by this app.'
    elif op=='reverse':
        pid=dispatch.clean(data,'payment_id',True,80);reason=dispatch.clean(data,'reason',True,1500)
        p=db.execute('SELECT * FROM supplier_payments WHERE id=? AND bill_id=?',(pid,bid)).fetchone()
        if not p or p['reversed_at'] or data.get('confirmed') is not True:raise ValueError('Select an active payment and confirm reversing its ledger entry.')
        db.execute('UPDATE supplier_payments SET reversed_at=?,reversed_by=?,reason=? WHERE id=?',(dispatch.now(),user['id'],reason,pid))
        message=f'Payment entry reversed: {b["currency"]} {p["amount"]}; {p["reference"]}. {reason}. No money moved.'
    else:raise ValueError('Unknown supplier bill action.')
    db.execute('UPDATE supplier_bills SET version=version+1 WHERE id=?',(bid,));log(db,bid,user,message)
    return dict(ok=True,id=bid)
