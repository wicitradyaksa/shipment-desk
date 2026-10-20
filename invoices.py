"""Internal invoice preparation, issuance and immutable PDF snapshots."""
import json
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import auth
import billing
import dispatch


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS invoice_settings (
      id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, config TEXT NOT NULL,
      next_number INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS invoices (
      id TEXT PRIMARY KEY, shipment_id TEXT UNIQUE NOT NULL REFERENCES shipments(id),
      number TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'Draft', version INTEGER NOT NULL DEFAULT 1,
      source_hash TEXT NOT NULL, snapshot TEXT NOT NULL, created TEXT NOT NULL,
      issued_at TEXT, issued_by INTEGER REFERENCES users(id), pdf BLOB);
    CREATE TABLE IF NOT EXISTS invoice_events (
      id INTEGER PRIMARY KEY, invoice_id TEXT REFERENCES invoices(id), time TEXT NOT NULL,
      actor TEXT NOT NULL, message TEXT NOT NULL);
    ''')


def config(db):
    row=db.execute('SELECT * FROM invoice_settings WHERE id=1').fetchone()
    return dict(json.loads(row['config']),version=row['version'],next_number=row['next_number']) if row else dict(version=0,next_number=1,company='',address='',tax_id='',email='',bank='',terms='',prefix='INV-',payment_days=30,tax_mode='',tax_rate='',tax_label='Tax')


def log(db,user,message,iid=None):
    db.execute('INSERT INTO invoice_events(invoice_id,time,actor,message) VALUES (?,?,?,?)',
               (iid,dispatch.now(),f"{user['name']} (@{user['username']})",message))


def source(db,sid):
    row=db.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()
    if not row: raise ValueError('Shipment not found.')
    return billing.summary(db,dict(row))


def snapshot(s,c):
    if not c['version']: raise ValueError('Save invoice settings first.')
    if not s['approval_valid']: raise ValueError('This shipment needs current Admin billing approval.')
    lines=[dict(label='Transport: '+s['origin']+' to '+s['destination'],amount=s['terms']['revenue'])]
    lines += [dict(label=e['label'],amount=e['amount']) for e in s['record']['extras'] if e['decision']=='Approved']
    base=sum((Decimal(l['amount']) for l in lines),Decimal(0))
    rate=Decimal(c['tax_rate'])/100
    rounded=lambda v: v.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    tax=rounded(base*rate) if c['tax_mode']=='exclusive' else rounded(base-base/(1+rate)) if c['tax_mode']=='inclusive' else Decimal('0.00')
    net=base-tax if c['tax_mode']=='inclusive' else base
    total=base+tax if c['tax_mode']=='exclusive' else base
    return dict(company=c,customer=s['customer'],email=s['email'],customer_address='',customer_tax_id='',
        reference=s['reference'],currency=s['terms']['currency'],lines=lines,net=str(net),tax=str(tax),total=str(total),
        issue_date=date.today().isoformat(),due_date=(date.today()+timedelta(days=c['payment_days'])).isoformat(),notes='')


def decode(row):
    d={k:row[k] for k in row.keys() if k!='pdf'}; d['snapshot']=json.loads(d['snapshot']); return d


def state(db,user):
    auth.require(user,('Admin',))
    rows=[]
    for row in db.execute('SELECT * FROM invoices ORDER BY created DESC'):
        d=decode(row); s=source(db,d['shipment_id'])
        d['source_current']=s['approval_valid'] and s['fingerprint']==d['source_hash']
        rows.append(d)
    existing={r['shipment_id'] for r in rows}
    ready=[]
    for row in db.execute('SELECT * FROM shipments ORDER BY created DESC'):
        s=billing.summary(db,dict(row))
        if s['approval_valid'] and s['id'] not in existing: ready.append(dict(id=s['id'],reference=s['reference'],customer=s['customer']))
    return dict(config=config(db),invoices=rows,ready=ready,events=[dict(e) for e in db.execute('SELECT * FROM invoice_events ORDER BY id DESC')])


def action(db,path,data,user):
    auth.require(user,('Admin',)); db.execute('BEGIN IMMEDIATE')
    op=path.rsplit('/',1)[-1]
    if op=='settings':
        c=config(db)
        if data.get('version')!=c['version']: raise auth.AccessError('Settings changed. Reload before saving.',409)
        fields={k:dispatch.clean(data,k,k in ('company','address','terms'),1500) for k in ('company','address','tax_id','email','bank','terms')}
        fields['prefix']=dispatch.clean(data,'prefix',True,20)
        if not re.fullmatch(r'[A-Za-z0-9-]+',fields['prefix']): raise ValueError('Invoice prefix may contain letters, numbers and hyphens.')
        days=data.get('payment_days')
        if type(days) is not int or not 0<=days<=365: raise ValueError('Payment terms must be 0 to 365 whole days.')
        mode=data.get('tax_mode'); rate=billing.money(data.get('tax_rate'))
        if mode not in ('none','exclusive','inclusive') or rate>100: raise ValueError('Choose tax treatment and a rate between 0 and 100%.')
        if mode=='none' and rate!=0: raise ValueError('No-tax treatment requires a zero tax rate.')
        fields.update(payment_days=days,tax_mode=mode,tax_rate=str(rate),tax_label=dispatch.clean(data,'tax_label',True,80))
        db.execute('INSERT INTO invoice_settings(id,version,config,next_number) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,config=excluded.config',
                   (c['version']+1,json.dumps(fields),c['next_number']))
        log(db,user,'Invoice settings updated. Existing invoice snapshots retain their terms.')
        return dict(ok=True)
    if op=='create':
        sid=dispatch.clean(data,'shipment_id',True,80)
        existing=db.execute('SELECT id FROM invoices WHERE shipment_id=?',(sid,)).fetchone()
        if existing: return dict(ok=True,id=existing['id'])
        s=source(db,sid); snap=snapshot(s,config(db)); iid=str(uuid.uuid4())
        db.execute('INSERT INTO invoices(id,shipment_id,source_hash,snapshot,created) VALUES (?,?,?,?,?)',(iid,sid,s['fingerprint'],json.dumps(snap),dispatch.now()))
        log(db,user,'Invoice draft created from approved billing.',iid);return dict(ok=True,id=iid)
    iid=dispatch.clean(data,'id',True,80)
    row=db.execute('SELECT * FROM invoices WHERE id=?',(iid,)).fetchone()
    if not row: raise ValueError('Invoice not found.')
    inv=decode(row); snap=inv['snapshot']
    if data.get('version')!=inv['version']: raise auth.AccessError('Invoice changed. Refresh before saving.',409)
    if op=='note':
        note=dispatch.clean(data,'note',True,1500)
        log(db,user,'Correction / follow-up note: '+note,iid)
    else:
        if inv['status']!='Draft': raise ValueError('Issued invoices are fixed. Record a correction note separately.')
        if op=='save':
            for k in ('customer_address','customer_tax_id','notes'): snap[k]=dispatch.clean(data,k,False,1500)
            try:
                issued=date.fromisoformat(data.get('issue_date','')); due=date.fromisoformat(data.get('due_date',''))
                if due<issued: raise ValueError()
            except (ValueError,TypeError): raise ValueError('Choose valid dates, with due date on or after invoice date.')
            snap.update(issue_date=issued.isoformat(),due_date=due.isoformat())
            db.execute('UPDATE invoices SET snapshot=? WHERE id=?',(json.dumps(snap),iid))
            log(db,user,'Draft billing address, dates and notes saved.',iid)
        elif op=='refresh':
            s=source(db,inv['shipment_id']); new=snapshot(s,config(db))
            for k in ('customer_address','customer_tax_id','notes','issue_date','due_date'):new[k]=snap[k]
            db.execute('UPDATE invoices SET snapshot=?,source_hash=? WHERE id=?',(json.dumps(new),s['fingerprint'],iid))
            log(db,user,'Draft refreshed from current approved billing and invoice settings.',iid)
        elif op=='issue':
            s=source(db,inv['shipment_id'])
            if not s['approval_valid'] or s['fingerprint']!=inv['source_hash']: raise ValueError('Billing changed. Reapprove billing and refresh this draft before issuing.')
            if not snap['customer_address']: raise ValueError('Save the customer billing address before issuing.')
            if data.get('confirmed') is not True: raise ValueError('Confirm review of customer details, tax treatment and total.')
            c=config(db); sequence=c['next_number']; prefix=snap['company']['prefix']
            number=f'{prefix}{sequence:06d}'
            while db.execute('SELECT 1 FROM invoices WHERE number=?',(number,)).fetchone():
                sequence+=1;number=f'{prefix}{sequence:06d}'
            from invoice_pdf import render
            pdf=render(dict(inv,number=number,status='Issued'))
            db.execute("UPDATE invoices SET number=?,status='Issued',issued_at=?,issued_by=?,pdf=? WHERE id=?",(number,dispatch.now(),user['id'],pdf,iid))
            db.execute('UPDATE invoice_settings SET next_number=? WHERE id=1',(sequence+1,))
            log(db,user,f'Invoice {number} issued. Sending remains manual.',iid)
        else: raise ValueError('Unknown invoice action.')
    db.execute('UPDATE invoices SET version=version+1 WHERE id=?',(iid,))
    return dict(ok=True,id=iid)


def pdf(db,iid,user):
    auth.require(user,('Admin',))
    row=db.execute('SELECT * FROM invoices WHERE id=?',(iid,)).fetchone()
    if not row: raise ValueError('Invoice not found.')
    if row['status']=='Issued': return row['pdf'],row['number']+'.pdf'
    from invoice_pdf import render
    return render(decode(row)),'DRAFT-'+iid[:8]+'.pdf'
