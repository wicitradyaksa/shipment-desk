"""Internal cost reconciliation and billing readiness; no invoices or payments."""
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import auth
import dispatch


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS billing_records (
      shipment_id TEXT PRIMARY KEY REFERENCES shipments(id), version INTEGER NOT NULL DEFAULT 1,
      costs TEXT NOT NULL DEFAULT '[]', extras TEXT NOT NULL DEFAULT '[]',
      cost_note TEXT NOT NULL DEFAULT '', submitted_at TEXT, submitted_by INTEGER REFERENCES users(id),
      manual_terms TEXT, approved_at TEXT, approved_by INTEGER REFERENCES users(id), approval_hash TEXT);
    CREATE TABLE IF NOT EXISTS billing_events (
      id INTEGER PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(id),
      time TEXT NOT NULL, actor TEXT NOT NULL, message TEXT NOT NULL);
    ''')


def money(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 100000000:
            raise ValueError()
        return number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('Amounts must be between 0 and 100,000,000.')


def record(db, sid):
    row = db.execute('SELECT * FROM billing_records WHERE shipment_id=?', (sid,)).fetchone()
    r = dict(row) if row else dict(shipment_id=sid,version=0,costs='[]',extras='[]',cost_note='',submitted_at=None,
                                  submitted_by=None,manual_terms=None,approved_at=None,approved_by=None,approval_hash=None)
    for k in ('costs','extras','manual_terms'):
        r[k] = json.loads(r[k]) if r[k] else None
    return r


def summary(db, s):
    r = record(db,s['id'])
    q = db.execute("SELECT * FROM quotes WHERE shipment_id=? AND status='Closed won'",(s['id'],)).fetchone()
    pricing = json.loads(q['pricing']) if q else None
    terms = dict(currency=pricing['currency'],revenue=pricing['total'],quoted_cost=pricing['cost'],acceptance=q['acceptance']) if q else r['manual_terms']
    docs = [dict(d) for d in db.execute('SELECT id,name,category,checksum FROM shipment_documents WHERE shipment_id=? AND archived_at IS NULL',(s['id'],))]
    active = {d['id'] for d in docs}
    blockers = []
    if s['status'] != 'Delivered': blockers.append('Delivery has not been confirmed.')
    if not any(d['category']=='Proof of delivery' for d in docs): blockers.append('Upload active proof of delivery in Dispatch.')
    if not terms or not terms.get('acceptance'): blockers.append('Client-approved commercial terms are missing.')
    if not r['submitted_at']: blockers.append('Operations must submit final costs.')
    if any(c['document_id'] not in active for c in r['costs']): blockers.append('Every cost needs an active supporting document.')
    if any(e['decision']=='Pending' for e in r['extras']): blockers.append('Sales must resolve every extra charge.')
    actual = sum((money(c['amount']) for c in r['costs']),Decimal(0))
    extras = sum((money(e['amount']) for e in r['extras'] if e['decision']=='Approved'),Decimal(0))
    revenue = money(terms['revenue'])+extras if terms else None
    profit = revenue-actual if revenue is not None else None
    quoted_cost = money(terms['quoted_cost']) if terms else None
    fingerprint = hashlib.sha256(json.dumps(dict(costs=r['costs'],extras=r['extras'],note=r['cost_note'],submitted=r['submitted_at'],
        terms=terms,status=s['status'],documents=sorted(d['id']+d['checksum'] for d in docs)),sort_keys=True).encode()).hexdigest()
    valid = bool(r['approval_hash'] and r['approval_hash']==fingerprint and not blockers)
    stage = 'Approved for billing' if valid else 'Approval outdated' if r['approval_hash'] else 'Needs attention' if blockers else 'Ready for approval'
    return dict(**s,record=r,terms=terms,quote_reference=q['reference'] if q else None,sales_owner=q['owner_id'] if q else None,
                documents=docs,blockers=blockers,stage=stage,approval_valid=valid,fingerprint=fingerprint,
                totals=dict(actual_cost=str(actual.quantize(Decimal('.01'))),approved_extras=str(extras.quantize(Decimal('.01'))),
                            revenue=str(revenue) if revenue is not None else None,profit=str(profit) if profit is not None else None,
                            margin=str((profit/revenue*100).quantize(Decimal('.01'))) if revenue else None,
                            quoted_cost=str(quoted_cost) if quoted_cost is not None else None,
                            cost_variance=str(actual-quoted_cost) if quoted_cost is not None else None))


def state(db,user):
    auth.require(user,('Admin','Operations','Sales'))
    rows = [summary(db,dict(s)) for s in db.execute('SELECT * FROM shipments ORDER BY created DESC')]
    if user['role']=='Sales': rows=[r for r in rows if r['sales_owner']==user['id']]
    ids={r['id'] for r in rows}
    events=[dict(e) for e in db.execute('SELECT * FROM billing_events ORDER BY id DESC') if e['shipment_id'] in ids]
    return dict(shipments=rows,events=events)


def log(db,sid,user,message):
    db.execute('INSERT INTO billing_events(shipment_id,time,actor,message) VALUES (?,?,?,?)',
               (sid,dispatch.now(),f"{user['name']} (@{user['username']})",message))


def action(db,path,data,user):
    auth.require(user,('Admin','Operations','Sales'))
    db.execute('BEGIN IMMEDIATE')
    sid=dispatch.clean(data,'shipment_id',True,80)
    s=db.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()
    if not s: raise ValueError('Shipment not found.')
    current=summary(db,dict(s)); r=current['record']
    if data.get('version') != r['version']:
        raise auth.AccessError('Billing changed. Refresh the queue before saving again.',409)
    operation=path.rsplit('/',1)[-1]
    if operation in ('costs','submit','extras'): auth.require(user,('Admin','Operations'))
    elif operation in ('approve','terms'): auth.require(user,('Admin',))
    elif operation=='decision':
        auth.require(user,('Admin','Sales'))
        if user['role']=='Sales' and current['sales_owner']!=user['id']:
            raise auth.AccessError('Only the owning Sales user can confirm these extra charges.')
    else: raise ValueError('Unknown billing action.')
    db.execute('INSERT OR IGNORE INTO billing_records(shipment_id) VALUES (?)',(sid,))
    if operation in ('costs','extras'):
        if not current['terms']: raise ValueError('Admin must record commercial terms and currency first.')
        lines=data.get('lines')
        if not isinstance(lines,list) or len(lines)>100: raise ValueError('Provide up to 100 line items.')
        clean=[]
        for line in lines:
            if not isinstance(line,dict): raise ValueError('Invalid line item.')
            label=dispatch.clean(line,'label',True,150); value=money(line.get('amount'))
            if value<=0: raise ValueError('Each line amount must be greater than zero.')
            item=dict(label=label,amount=str(value))
            if operation=='costs':
                doc=dispatch.clean(line,'document_id',False,80)
                if doc and doc not in {d['id'] for d in current['documents']}: raise ValueError('Choose an active document from this shipment.')
                item['document_id']=doc
            else: item.update(decision='Pending',reference='',confirmed_by=None,confirmed_at=None)
            clean.append(item)
        # Require bounded aggregate values as well as individual amounts.
        money(sum((money(i['amount']) for i in clean),Decimal(0)))
        if operation=='costs':
            note=dispatch.clean(data,'note',False,1500)
            db.execute('UPDATE billing_records SET costs=?,cost_note=?,submitted_at=NULL,submitted_by=NULL WHERE shipment_id=?',(json.dumps(clean),note,sid))
        else: db.execute('UPDATE billing_records SET extras=? WHERE shipment_id=?',(json.dumps(clean),sid))
        names={d['id']:d['name'] for d in current['documents']}
        details='; '.join(f"{i['label']}: {current['terms']['currency']} {i['amount']}" + (f" (document: {names.get(i.get('document_id'),'not attached')})" if operation=='costs' else '') for i in clean)
        message=f'{"Actual costs" if operation=="costs" else "Extra charges"} revised: '+(details or 'No line items.')
        if operation=='costs' and note: message+=' Notes: '+note
    elif operation=='submit':
        if r['submitted_at']: raise ValueError('Costs are already submitted. Edit them to start a revision.')
        if data.get('confirmed') is not True: raise ValueError('Confirm these are the complete final costs.')
        if not r['costs'] and not r['cost_note']: raise ValueError('Explain why this shipment has no actual costs.')
        if any(c['document_id'] not in {d['id'] for d in current['documents']} for c in r['costs']): raise ValueError('Attach an active supporting document to every cost before submitting.')
        db.execute('UPDATE billing_records SET submitted_at=?,submitted_by=? WHERE shipment_id=?',(dispatch.now(),user['id'],sid))
        message='Operations submitted complete final costs. '+r['cost_note']
    elif operation=='decision':
        index=data.get('index'); decision=data.get('decision')
        if type(index) is not int or not 0<=index<len(r['extras']) or decision not in ('Approved','Not billed'): raise ValueError('Choose a valid extra charge and decision.')
        reference=dispatch.clean(data,'reference',True,1500)
        if data.get('confirmed') is not True: raise ValueError('Confirm the client approval or the decision not to bill.')
        r['extras'][index].update(decision=decision,reference=reference,confirmed_by=user['id'],confirmed_at=dispatch.now())
        db.execute('UPDATE billing_records SET extras=? WHERE shipment_id=?',(json.dumps(r['extras']),sid))
        message=f'Extra charge {r["extras"][index]["label"]}: {decision}. {reference}'
    elif operation=='terms':
        if current['quote_reference']: raise ValueError('The accepted quote fixes the base commercial terms.')
        currency=dispatch.clean(data,'currency',True,3)
        if currency not in ('TWD','USD','EUR','GBP','CNY','HKD','SGD','AUD','CAD'): raise ValueError('Choose a supported currency.')
        if r['manual_terms'] and currency != r['manual_terms']['currency'] and (r['costs'] or r['extras']):
            raise ValueError('Currency cannot change while cost or extra-charge lines exist.')
        terms=dict(currency=currency,revenue=str(money(data.get('revenue'))),quoted_cost=str(money(data.get('quoted_cost'))),acceptance=dispatch.clean(data,'acceptance',True,1500))
        if money(terms['revenue'])<=0: raise ValueError('Agreed revenue must be greater than zero.')
        db.execute('UPDATE billing_records SET manual_terms=? WHERE shipment_id=?',(json.dumps(terms),sid))
        message=f"Admin recorded commercial terms: {currency} {terms['revenue']} accepted price; {currency} {terms['quoted_cost']} quoted cost. Client approval: {terms['acceptance']}"
    else:
        if current['blockers']: raise ValueError('Resolve billing checks first: '+' '.join(current['blockers']))
        if current['approval_valid']: raise ValueError('This shipment is already approved for billing.')
        if data.get('fingerprint') != current['fingerprint']:
            raise auth.AccessError('Shipment or documents changed. Refresh and review before approving.',409)
        if data.get('confirmed') is not True: raise ValueError('Confirm that the billing amounts and supporting documents have been reviewed.')
        db.execute('UPDATE billing_records SET approved_at=?,approved_by=?,approval_hash=? WHERE shipment_id=?',(dispatch.now(),user['id'],current['fingerprint'],sid))
        t=current['totals']; currency=current['terms']['currency']
        message=f"Admin approved for billing. Revenue: {currency} {t['revenue']}; actual cost: {currency} {t['actual_cost']}; shipment profit: {currency} {t['profit']}."
    db.execute('UPDATE billing_records SET version=version+1 WHERE shipment_id=?',(sid,))
    log(db,sid,user,message)
    return dict(ok=True)
