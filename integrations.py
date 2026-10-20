"""Optional, configuration-ready integrations. No automatic external actions."""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import smtplib
import ssl
import time
import uuid
from datetime import date
from email.message import EmailMessage
from decimal import Decimal, InvalidOperation
import auth
import dispatch
import invoices
import payments
import reports
import sales

DEFAULT=dict(version=0,email_mode='Test',smtp_host='',smtp_port=587,smtp_security='STARTTLS',smtp_user='',from_email='',
             tracking_enabled=False,status_map={'booked':'Booked','in_transit':'In transit','out_for_delivery':'Out for delivery','delivered':'Delivered','exception':'Exception'},
             revenue_account='',receivable_account='',payable_account='',bank_account='',tax_account='',expense_account='')
STATUSES=('Booked','In transit','Out for delivery','Delivered','Exception')


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS integration_config(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS email_outbox(id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_id TEXT NOT NULL,source_hash TEXT NOT NULL,
      recipient TEXT NOT NULL,subject TEXT NOT NULL,body TEXT NOT NULL,attachment BLOB,filename TEXT,
      status TEXT NOT NULL DEFAULT 'Draft',version INTEGER NOT NULL DEFAULT 1,created TEXT NOT NULL,actor TEXT NOT NULL,
      delivered TEXT, UNIQUE(kind,source_id,source_hash));
    CREATE TABLE IF NOT EXISTS carrier_inbox(id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,shipment_id TEXT NOT NULL REFERENCES shipments(id),
      reference TEXT NOT NULL,status TEXT NOT NULL,event_time TEXT NOT NULL,received TEXT NOT NULL,reviewed INTEGER NOT NULL DEFAULT 0,
      payload_hash TEXT NOT NULL,actor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS integration_history(id INTEGER PRIMARY KEY,time TEXT NOT NULL,actor TEXT NOT NULL,message TEXT NOT NULL);
    ''')


def config(db):
    row=db.execute('SELECT * FROM integration_config WHERE id=1').fetchone()
    return dict(json.loads(row['body']),version=row['version']) if row else json.loads(json.dumps(DEFAULT))


def audit(db,actor,message):
    db.execute('INSERT INTO integration_history(time,actor,message) VALUES (?,?,?)',(dispatch.now(),actor,message))


def email(value):
    if not isinstance(value,str) or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',value) or len(value)>300:
        raise ValueError('Enter a valid email address.')
    return value


def source(db,kind,sid,user):
    attachment=None;filename=None
    if kind=='proposal':
        p=sales.proposal(db,sid,user)
        recipient=email(p['email']);subject='Transport proposal '+p['reference']
        body='Transport proposal\n\n'+'\n'.join(f'{k.replace("_"," ").title()}: {v}' for k,v in p.items() if k!='email')+'\n\nPlease reply with your acceptance and proposal reference.'
        fingerprint=p
    elif kind=='invoice':
        row=db.execute("SELECT * FROM invoices WHERE id=? AND status='Issued'",(sid,)).fetchone()
        if not row or not row['pdf']:raise ValueError('Choose an issued invoice with a stored PDF.')
        p=json.loads(row['snapshot']);recipient=email(p['email']);subject='Invoice '+row['number']
        body=f"Hello {p['customer']},\n\nAttached is invoice {row['number']} for {p['currency']} {p['total']}, due {p['due_date']}.\n\n{p['company']['terms']}\n{p['company']['bank']}"
        attachment=bytes(row['pdf']);filename=row['number']+'.pdf';fingerprint=dict(snapshot=p,number=row['number'],pdf=hashlib.sha256(attachment).hexdigest())
    elif kind=='notification':
        row=db.execute('SELECT * FROM notifications WHERE id=?',(sid,)).fetchone()
        if not row:raise ValueError('Notification preview not found.')
        recipient=email(row['recipient']);subject=row['subject'];body=row['body'];fingerprint=dict(row)
    elif kind=='reminder':
        row=db.execute("SELECT * FROM invoices WHERE id=? AND status='Issued'",(sid,)).fetchone()
        if not row:raise ValueError('Choose an issued invoice.')
        p=payments.summary(db,row)
        if Decimal(p['balance'])<=0:raise ValueError('This invoice is fully paid.')
        recipient=email(p['email']);subject='Payment reminder '+p['number']
        body=f"Hello {p['customer']},\n\nAs of {payments.today()}, invoice {p['number']} has a remaining balance of {p['currency']} {p['balance']}, due {p['due_date']}.\nPlease confirm your expected payment date, or share the transaction reference if paid.\n\nThank you."
        fingerprint={k:p[k] for k in ('version','number','email','balance','due_date')};fingerprint['as_of']=payments.today().isoformat()
    else:raise ValueError('Choose proposal, invoice, reminder or notification.')
    if any(c in subject for c in '\r\n') or len(subject)>500:raise ValueError('Source subject contains unsupported characters.')
    digest=hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest()
    return dict(recipient=recipient,subject=subject,body=body,source_hash=digest,attachment=attachment,filename=filename)


def state(db,user):
    auth.require(user,('Admin',))
    return dict(config=config(db),secrets=dict(smtp_password=bool(os.environ.get('SHIPMENT_SMTP_PASSWORD')),tracking_secret=bool(os.environ.get('SHIPMENT_TRACKING_SECRET'))),
      outbox=[dict(r) for r in db.execute('SELECT id,kind,source_id,recipient,subject,body,filename,status,version,created,actor,delivered FROM email_outbox ORDER BY created DESC LIMIT 100')],
      carrier_events=[dict(r) for r in db.execute('SELECT * FROM carrier_inbox ORDER BY received DESC LIMIT 100')],
      quotes=[dict(r) for r in db.execute("SELECT id,reference,status FROM quotes WHERE status IN ('Proposal ready','Awaiting client','Closed won') ORDER BY created DESC")],
      invoices=[dict(r) for r in db.execute("SELECT id,number FROM invoices WHERE status='Issued' ORDER BY created DESC")],
      notifications=[dict(r) for r in db.execute('SELECT id,subject FROM notifications ORDER BY id DESC LIMIT 100')],
      history=[dict(r) for r in db.execute('SELECT * FROM integration_history ORDER BY id DESC LIMIT 100')])


def receive(db,data,actor):
    c=config(db)
    ref=dispatch.clean(data,'shipment_reference',True,300);eid=dispatch.clean(data,'event_id',True,150)
    status=c['status_map'].get(data.get('status'))
    if status not in STATUSES:raise ValueError('Carrier status has no configured mapping.')
    timestamp=dispatch.timestamp(data.get('event_time'))
    normalized=dict(shipment_reference=ref,event_id=eid,status=status,event_time=timestamp)
    digest=hashlib.sha256(json.dumps(normalized,sort_keys=True).encode()).hexdigest()
    old=db.execute('SELECT id,payload_hash FROM carrier_inbox WHERE event_id=?',(eid,)).fetchone()
    if old:
        if old['payload_hash']!=digest:raise auth.AccessError('Event ID was already used for different data.',409)
        return dict(ok=True,id=old['id'],duplicate=True)
    row=db.execute('SELECT id FROM shipments WHERE reference=?',(ref,)).fetchone()
    if not row:raise ValueError('Shipment reference not found.')
    eid2=str(uuid.uuid4())
    db.execute('INSERT INTO carrier_inbox(id,event_id,shipment_id,reference,status,event_time,received,payload_hash,actor) VALUES (?,?,?,?,?,?,?,?,?)',
      (eid2,eid,row['id'],ref,status,timestamp,dispatch.now(),digest,actor))
    audit(db,actor,'Carrier event queued for human review: '+ref+'; '+status)
    return dict(ok=True,id=eid2)


def webhook(db,raw,headers):
    c=config(db);secret=os.environ.get('SHIPMENT_TRACKING_SECRET','')
    if not c['tracking_enabled'] or len(secret)<32:raise auth.AccessError('Tracking connection is disabled.',403)
    timestamp=headers.get('X-Shipment-Timestamp','');signature=headers.get('X-Shipment-Signature','')
    try:
        if abs(time.time()-int(timestamp))>300:raise ValueError()
    except ValueError:raise auth.AccessError('Invalid or expired webhook timestamp.',403)
    expected=hmac.new(secret.encode(),timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature,expected):raise auth.AccessError('Invalid webhook signature.',403)
    data=json.loads(raw)
    if not isinstance(data,dict):raise ValueError('Expected a JSON object.')
    db.execute('BEGIN IMMEDIATE')
    return receive(db,data,'Signed carrier webhook')


def smtp_send(c,row):
    password=os.environ.get('SHIPMENT_SMTP_PASSWORD','')
    if not c['smtp_host'] or not c['from_email'] or not c['smtp_user'] or not password:raise ValueError('Configure SMTP and its password environment variable first.')
    message=EmailMessage();message['From']=email(c['from_email']);message['To']=email(row['recipient']);message['Subject']=row['subject']
    message['Message-ID']='<'+row['id']+'@shipment-desk.local>';message.set_content(row['body'])
    if row['attachment']:message.add_attachment(bytes(row['attachment']),maintype='application',subtype='pdf',filename=row['filename'])
    context=ssl.create_default_context()
    client=smtplib.SMTP_SSL(c['smtp_host'],c['smtp_port'],timeout=20,context=context) if c['smtp_security']=='TLS' else smtplib.SMTP(c['smtp_host'],c['smtp_port'],timeout=20)
    with client:
        if c['smtp_security']=='STARTTLS':client.starttls(context=context)
        client.login(c['smtp_user'],password);client.send_message(message)


def action(db,path,data,user):
    auth.require(user,('Admin',));op=path.rsplit('/',1)[-1];db.execute('BEGIN IMMEDIATE')
    if op=='settings':
        c=config(db)
        if data.get('version')!=c['version']:raise auth.AccessError('Settings changed. Refresh first.',409)
        if any(k in data for k in ('password','secret','smtp_password','tracking_secret')):raise ValueError('Secrets belong in host environment variables, not this form.')
        fields={k:dispatch.clean(data,k,False,300) for k in ('smtp_host','smtp_user','from_email','revenue_account','receivable_account','payable_account','bank_account','tax_account','expense_account')}
        if fields['from_email']:email(fields['from_email'])
        if any(ch in fields['smtp_host'] for ch in '/\\\r\n:@ '):raise ValueError('SMTP host must be a hostname without a URL, port or whitespace.')
        try:
            port=int(data.get('smtp_port',587))
            if not 1<=port<=65535:raise ValueError()
        except (ValueError,TypeError):raise ValueError('Choose a valid SMTP port.')
        mode=data.get('email_mode');security=data.get('smtp_security');mapping=data.get('status_map');tracking=data.get('tracking_enabled')
        if mode not in ('Test','Live') or security not in ('TLS','STARTTLS') or type(tracking)!=bool:raise ValueError('Choose test/live mode, TLS security and tracking enabled/disabled.')
        if not isinstance(mapping,dict) or len(mapping)>50 or any(not isinstance(k,str) or not k or len(k)>80 or v not in STATUSES for k,v in mapping.items()):raise ValueError('Map carrier statuses to existing shipment statuses.')
        if mode=='Live' and data.get('confirmed') is not True:raise ValueError('Confirm enabling live email delivery.')
        body=dict(fields,smtp_port=port,smtp_security=security,email_mode=mode,tracking_enabled=tracking,status_map=mapping)
        db.execute('INSERT INTO integration_config VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,body=excluded.body',(c['version']+1,json.dumps(body)))
        audit(db,user['name'],'Integration configuration updated; email mode '+mode+'; tracking '+str(tracking))
    elif op=='prepare':
        kind=data.get('kind');sid=dispatch.clean(data,'source_id',True,100);p=source(db,kind,sid,user)
        old=db.execute('SELECT id FROM email_outbox WHERE kind=? AND source_id=? AND source_hash=?',(kind,sid,p['source_hash'])).fetchone()
        if old:return dict(ok=True,id=old['id'],duplicate=True)
        eid=str(uuid.uuid4())
        db.execute('INSERT INTO email_outbox(id,kind,source_id,source_hash,recipient,subject,body,attachment,filename,created,actor) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          (eid,kind,sid,p['source_hash'],p['recipient'],p['subject'],p['body'],p['attachment'],p['filename'],dispatch.now(),user['name']))
        audit(db,user['name'],'Email draft prepared: '+kind+'; '+eid);return dict(ok=True,id=eid)
    elif op in ('send','cancel','reset_test','resolve_delivery'):
        row=db.execute('SELECT * FROM email_outbox WHERE id=?',(data.get('id'),)).fetchone()
        if not row or row['version']!=data.get('version'):raise auth.AccessError('Email changed. Refresh first.',409)
        if op=='resolve_delivery':
            reason=dispatch.clean(data,'reason',True,1500);decision=data.get('decision')
            if row['status'] not in ('Sending','Uncertain') or decision not in ('not_sent','sent') or data.get('confirmed') is not True:raise ValueError('Verify the mail server result and confirm the resolution.')
            status='Draft' if decision=='not_sent' else 'Sent'
            db.execute('UPDATE email_outbox SET status=?,delivered=?,version=version+1 WHERE id=?',(status,dispatch.now() if status=='Sent' else None,row['id']))
            audit(db,user['name'],'Email delivery manually resolved: '+row['id']+'; '+decision+'; '+reason);return dict(ok=True)
        if op=='reset_test':
            if row['status'] not in ('Test captured','Cancelled'):raise ValueError('Only a test capture or cancelled draft may return to Draft.')
            db.execute("UPDATE email_outbox SET status='Draft',version=version+1 WHERE id=?",(row['id'],));audit(db,user['name'],'Message returned to Draft: '+row['id']);return dict(ok=True)
        if row['status']!='Draft':raise ValueError('Only a draft can be sent or cancelled. Uncertain deliveries require checking the mail server.')
        if op=='cancel':db.execute("UPDATE email_outbox SET status='Cancelled',version=version+1 WHERE id=?",(row['id'],))
        else:
            if data.get('confirmed') is not True:raise ValueError('Confirm recipient and message before delivery.')
            current=source(db,row['kind'],row['source_id'],user)
            if current['source_hash']!=row['source_hash']:raise auth.AccessError('Source changed. Prepare a new email draft.',409)
            c=config(db)
            if c['email_mode']=='Test':
                db.execute("UPDATE email_outbox SET status='Test captured',version=version+1 WHERE id=?",(row['id'],))
            else:
                if not all((c['smtp_host'],c['smtp_user'],c['from_email'],os.environ.get('SHIPMENT_SMTP_PASSWORD'))):raise ValueError('Complete SMTP configuration before live delivery.')
                # Commit Sending before network I/O: a crash must never automatically resend.
                db.execute("UPDATE email_outbox SET status='Sending',version=version+1 WHERE id=?",(row['id'],));db.commit()
                try:smtp_send(c,row);status='Sent'
                except Exception:status='Uncertain'
                db.execute('BEGIN IMMEDIATE');db.execute('UPDATE email_outbox SET status=?,delivered=?,version=version+1 WHERE id=?',(status,dispatch.now() if status=='Sent' else None,row['id']))
        audit(db,user['name'],'Email action '+op+': '+row['id'])
    elif op=='tracking_test':return receive(db,data,user['name']+' (local test)')
    elif op=='review':
        row=db.execute('SELECT * FROM carrier_inbox WHERE id=?',(data.get('id'),)).fetchone()
        if not row:raise ValueError('Carrier event not found.')
        db.execute('UPDATE carrier_inbox SET reviewed=1 WHERE id=?',(row['id'],));audit(db,user['name'],'Carrier event marked reviewed: '+row['event_id'])
    elif op=='bank_preview':return bank_preview(db,data)
    else:raise ValueError('Unknown integration action.')
    return dict(ok=True)


def csv_bytes(columns,rows):
    stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow(columns)
    for row in rows:
        writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','@')) or (isinstance(v,str) and v.lstrip().startswith('-')) else v for v in row])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def accounting(db,user,params):
    auth.require(user,('Admin',));f=reports.filters(params);c=config(db);rows=[]
    for row in db.execute("SELECT * FROM invoices WHERE status='Issued'"):
        s=json.loads(row['snapshot'])
        if reports.included(s['issue_date'],s['currency'],f):
            rows.append(['Customer invoice net',row['number'],s['issue_date'],s['currency'],s['net'],c['receivable_account'],c['revenue_account'],s['customer']])
            if Decimal(s['tax']):rows.append(['Customer invoice tax',row['number'],s['issue_date'],s['currency'],s['tax'],c['receivable_account'],c['tax_account'],s['customer']])
    for row in db.execute('SELECT p.*,i.number,i.snapshot FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE p.reversed_at IS NULL'):
        if reports.included(row['payment_date'],row['currency'],f):rows.append(['Customer receipt',row['reference'],row['payment_date'],row['currency'],row['amount'],c['bank_account'],c['receivable_account'],json.loads(row['snapshot'])['customer']])
    for row in db.execute('SELECT c.*,i.snapshot AS invoice_snapshot FROM credit_notes c JOIN invoices i ON i.id=c.invoice_id'):
        invoice=json.loads(row['invoice_snapshot']);currency=invoice['currency']
        if reports.included(row['credit_date'],currency,f):
            rows.append(['Customer credit net',row['number'],row['credit_date'],currency,row['net'],c['revenue_account'],c['receivable_account'],invoice['customer']])
            if Decimal(row['tax']):rows.append(['Customer credit tax',row['number'],row['credit_date'],currency,row['tax'],c['tax_account'],c['receivable_account'],invoice['customer']])
    for row in db.execute('SELECT r.*,i.snapshot FROM customer_refunds r JOIN invoices i ON i.id=r.invoice_id WHERE r.reversed_at IS NULL'):
        invoice=json.loads(row['snapshot']);currency=invoice['currency']
        if reports.included(row['refund_date'],currency,f):rows.append(['Customer refund',row['reference'],row['refund_date'],currency,row['amount'],c['receivable_account'],c['bank_account'],invoice['customer']])
    import payables
    for row in db.execute('SELECT * FROM supplier_bills'):
        b=payables.summary(db,row)
        if b['approval_valid'] and reports.included(b['bill_date'],b['currency'],f):rows.append(['Supplier bill',b['reference'],b['bill_date'],b['currency'],b['amount'],c['expense_account'],c['payable_account'],b['supplier']])
    for row in db.execute('SELECT p.*,b.currency,s.name FROM supplier_payments p JOIN supplier_bills b ON b.id=p.bill_id JOIN suppliers s ON s.id=b.supplier_id WHERE p.reversed_at IS NULL'):
        if reports.included(row['payment_date'],row['currency'],f):rows.append(['Supplier payment',row['reference'],row['payment_date'],row['currency'],row['amount'],c['payable_account'],c['bank_account'],row['name']])
    return csv_bytes(['Type','Reference','Date','Currency','Amount','Debit account','Credit account','Party'],rows)


def bank_preview(db,data):
    source=data.get('csv')
    if not isinstance(source,str) or len(source.encode())>100000:raise ValueError('Use a CSV up to 100 KB.')
    reader=csv.DictReader(io.StringIO(source.lstrip('\ufeff')),strict=True)
    if reader.fieldnames!=['date','reference','currency','amount','direction']:raise ValueError('Use headers: date,reference,currency,amount,direction.')
    results=[];seen=set()
    try:
        for n,row in enumerate(reader,2):
            if len(results)>=200:raise ValueError('Use up to 200 statement lines.')
            r=dict(row=n,reference=row.get('reference',''),status='Invalid',matches=[])
            try:
                if None in row or any(v is None for v in row.values()):raise ValueError()
                date.fromisoformat(row['date']);value=payments.money(row['amount'])
                if not re.fullmatch('[A-Z]{3}',row['currency']) or row['direction'] not in ('in','out') or not row['reference'].strip():raise ValueError()
                identity=tuple(row.values())
                if identity in seen:r['status']='Duplicate statement line'
                else:
                    seen.add(identity)
                    if row['direction']=='in':candidates=db.execute('SELECT id,amount,payment_date,reference FROM payments WHERE reversed_at IS NULL AND currency=?',(row['currency'],))
                    else:
                        candidates=list(db.execute('SELECT p.id,p.amount,p.payment_date,p.reference FROM supplier_payments p JOIN supplier_bills b ON b.id=p.bill_id WHERE p.reversed_at IS NULL AND b.currency=?',(row['currency'],)))
                        candidates += [dict(id=p['id'],amount=p['amount'],payment_date=p['refund_date'],reference=p['reference']) for p in db.execute('SELECT r.*,i.snapshot FROM customer_refunds r JOIN invoices i ON i.id=r.invoice_id WHERE r.reversed_at IS NULL') if json.loads(p['snapshot'])['currency']==row['currency']]
                    r['matches']=[p['id'] for p in candidates if Decimal(p['amount'])==value and p['payment_date']==row['date'] and ' '.join(p['reference'].casefold().split())==' '.join(row['reference'].casefold().split())]
                    r['status']='Matched' if len(r['matches'])==1 else 'Ambiguous' if r['matches'] else 'Unmatched'
            except (ValueError,InvalidOperation):pass
            results.append(r)
    except csv.Error:raise ValueError('Malformed statement CSV.')
    return dict(rows=results,note='Preview only. No receipts or supplier payments were created or changed.')
