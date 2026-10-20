"""CSV order intake into the existing Sales opportunity workflow."""
import csv
import hashlib
import io
import json
import auth
import sales

FIELDS=('order_reference','customer','email','description','origin','destination','route_id','cargo_id','weight_kg','volume_cbm','packages','pickup','eta','requirements','outbound_km','return_km','charter_days','tolls','fuel','other','extras_note')
REQUIRED=set(FIELDS[:6]+FIELDS[7:13])


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS intake_orders(customer_key TEXT NOT NULL,reference_key TEXT NOT NULL,
      reference TEXT NOT NULL,quote_id TEXT NOT NULL REFERENCES quotes(id),owner_id INTEGER NOT NULL REFERENCES users(id),
      PRIMARY KEY(customer_key,reference_key));
    CREATE TABLE IF NOT EXISTS intake_batches(owner_id INTEGER NOT NULL REFERENCES users(id),digest TEXT NOT NULL,
      result TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(owner_id,digest));
    ''')


def key(value):return ' '.join(value.casefold().split())
def text(data):
    value=data.get('csv')
    if not isinstance(value,str) or not value.strip() or len(value.encode('utf-8'))>100000:raise ValueError('Choose a UTF-8 CSV file up to 100 KB.')
    return value.lstrip('\ufeff')


def state(db,user):
    auth.require(user,('Admin','Sales'))
    cfg=sales.config(db)
    return dict(version=cfg['version'],currency=cfg['currency'],routes=cfg['routes'],cargo=cfg['cargo'],fields=FIELDS)


def preview(db,data,user):
    auth.require(user,('Admin','Sales'));source=text(data);cfg=sales.config(db)
    if not cfg['version']:raise ValueError('Admin must configure pricing before order intake.')
    try:
        reader=csv.DictReader(io.StringIO(source,newline=''),strict=True)
        headers=reader.fieldnames
        if not headers or len(headers)!=len(set(headers)) or not REQUIRED.issubset(headers) or set(headers)-set(FIELDS):
            raise ValueError('CSV headers must match the template. Missing: '+', '.join(sorted(REQUIRED-set(headers or []))))
        rows=[];seen=set()
        for number,row in enumerate(reader,2):
            if len(rows)>=100:raise ValueError('Import at most 100 orders per file.')
            result=dict(row=number,reference='',customer='',status='',total=None,currency=cfg['currency'],specs=None)
            try:
                if None in row or any(v is None for v in row.values()):raise ValueError('Row has a different number of fields from its header.')
                reference=sales.text(row,'order_reference',True,150);customer=sales.text(row,'customer',True,300)
                result.update(reference=reference,customer=customer)
                identity=(key(customer),key(reference))
                if identity in seen:raise ValueError('Repeated customer/order reference within this file.')
                seen.add(identity)
                existing=db.execute('SELECT quote_id FROM intake_orders WHERE customer_key=? AND reference_key=?',identity).fetchone()
                if existing:result.update(status='Already imported',message='This customer/order reference was imported previously.')
                else:
                    for name in ('outbound_km','return_km','charter_days','tolls','fuel','other'):
                        if not row.get(name):row[name]='0'
                    specs=sales.specs(row,cfg);pricing=sales.calculate(specs,cfg)
                    result.update(status='Ready',message='Proposal ready' if pricing['generic'] else 'Operations review',total=pricing['total'],specs=specs)
            except ValueError as error:result.update(status='Error',message=str(error))
            rows.append(result)
    except csv.Error as error:raise ValueError('Malformed CSV: '+str(error))
    if not rows:raise ValueError('CSV must contain at least one order.')
    body=dict(version=cfg['version'],rows=rows)
    body['preview_hash']=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    return body


def action(db,path,data,user,create):
    auth.require(user,('Admin','Sales'))
    if path.endswith('/preview'):return preview(db,data,user)
    if not path.endswith('/commit'):raise ValueError('Unknown intake action.')
    source=text(data);digest=hashlib.sha256(source.encode()).hexdigest()
    db.execute('BEGIN IMMEDIATE')
    old=db.execute('SELECT result FROM intake_batches WHERE owner_id=? AND digest=?',(user['id'],digest)).fetchone()
    if old:return dict(json.loads(old['result']),retry=True)
    current=preview(db,data,user)
    if data.get('preview_hash')!=current['preview_hash']:raise auth.AccessError('Pricing, orders or import history changed. Preview this file again.',409)
    if any(r['status']=='Error' for r in current['rows']):raise ValueError('Fix every row error before importing. No orders were created.')
    if data.get('confirmed') is not True:raise ValueError('Confirm the preview before importing.')
    quotes=[]
    for r in current['rows']:
        if r['status']!='Ready':continue
        result=sales.action(db,'/api/quotes',r['specs'],user,create,begin=False)
        db.execute('INSERT INTO intake_orders VALUES (?,?,?,?,?)',(key(r['customer']),key(r['reference']),r['reference'],result['id'],user['id']))
        sales.log(db,result['id'],user,'Order imported from CSV: '+r['reference'])
        quotes.append(result['id'])
    result=dict(ok=True,created=len(quotes),skipped=sum(r['status']=='Already imported' for r in current['rows']),quote_ids=quotes)
    db.execute('INSERT INTO intake_batches VALUES (?,?,?,?)',(user['id'],digest,json.dumps(result),sales.stamp()))
    return result
