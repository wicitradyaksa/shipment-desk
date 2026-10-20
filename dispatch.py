"""Dispatch planning and authenticated shipment document storage."""
import base64
import binascii
import hashlib
import json
import uuid
from datetime import datetime, timezone
import auth

MAX_FILE = 10 * 1024 * 1024
CATEGORIES = ('Client approval', 'Packing list', 'Instructions', 'Proof of delivery', 'Other')


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS dispatch_plans (
      shipment_id TEXT PRIMARY KEY REFERENCES shipments(id), owner_id INTEGER REFERENCES users(id),
      truck TEXT NOT NULL DEFAULT '', driver TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
      pickup TEXT, accepted_at TEXT, collected_at TEXT, delivered_at TEXT,
      instructions TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS shipment_documents (
      id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(id),
      name TEXT NOT NULL, category TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
      checksum TEXT NOT NULL, content BLOB NOT NULL, uploaded_at TEXT NOT NULL,
      uploaded_by INTEGER NOT NULL REFERENCES users(id), archived_at TEXT,
      version INTEGER NOT NULL DEFAULT 1);
    CREATE INDEX IF NOT EXISTS documents_shipment ON shipment_documents(shipment_id);
    ''')


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(data, key, required=False, limit=1500):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError(f'Provide a valid {key.replace("_", " ")} (up to {limit} characters).')
    return value.strip()


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Provide a valid pickup date and time with a timezone.')


def audit(db, sid, user, message):
    db.execute('INSERT INTO events(shipment_id,time,message,actor) VALUES (?,?,?,?)',
               (sid, now(), message, f"{user['name']} (@{user['username']})"))


def plan(db, sid):
    row = db.execute('SELECT * FROM dispatch_plans WHERE shipment_id=?', (sid,)).fetchone()
    return dict(row) if row else dict(shipment_id=sid, owner_id=None, truck='', driver='', phone='', pickup=None,
                                     accepted_at=None, collected_at=None, delivered_at=None, instructions='', version=0)


def ensure_plan(db, sid):
    db.execute('INSERT OR IGNORE INTO dispatch_plans(shipment_id) VALUES (?)', (sid,))


def documents(db):
    return [dict(r) for r in db.execute('''SELECT d.id,d.shipment_id,d.name,d.category,d.mime,d.size,d.checksum,
      d.uploaded_at,d.uploaded_by,d.archived_at,d.version,u.name AS uploader
      FROM shipment_documents d JOIN users u ON u.id=d.uploaded_by ORDER BY d.uploaded_at DESC''')]


def state(db, user):
    auth.require(user)
    shipments = [dict(s) for s in db.execute('SELECT * FROM shipments ORDER BY created DESC')]
    owners = [dict(u) for u in db.execute("SELECT id,name,username,active,role FROM users WHERE role IN ('Admin','Operations') AND active=1 ORDER BY name")]
    live_owner_ids = {o['id'] for o in owners}
    docs = documents(db)
    for s in shipments:
        p = plan(db, s['id'])
        q = db.execute('SELECT reference,specs FROM quotes WHERE shipment_id=?', (s['id'],)).fetchone()
        qspec = json.loads(q['specs']) if q else None
        s['plan'] = p
        s['quote_reference'] = q['reference'] if q else None
        s['cargo'] = {k:qspec.get(k) for k in ('description','weight_kg','volume_cbm','packages','requirements')} if qspec else None
        s['requested_pickup'] = qspec['pickup'] if qspec else None
        s['has_pod'] = any(d['shipment_id'] == s['id'] and d['category'] == 'Proof of delivery' and not d['archived_at'] for d in docs)
        assigned = p['owner_id'] in live_owner_ids and bool(p['truck'] and p['driver'] and s['carrier'] not in ('', 'Awaiting Operations assignment'))
        s['unassigned'] = not assigned and s['status'] != 'Delivered'
        s['pickup_overdue'] = bool(s['status'] == 'Booked' and p['pickup'] and datetime.fromisoformat(p['pickup']) < datetime.now(timezone.utc))
        s['stage'] = ('Delivered' if s['status'] == 'Delivered' else 'In transit' if s['status'] in ('In transit','Out for delivery')
                      else 'Exception' if s['status'] == 'Exception' else 'Scheduled' if assigned and p['pickup'] and p['accepted_at']
                      else 'Planning' if p['accepted_at'] else 'New handoff')
    return dict(shipments=shipments, documents=docs, owners=owners, categories=CATEGORIES, max_file=MAX_FILE)


def action(db, path, data, user, notify):
    auth.require(user)
    db.execute('BEGIN IMMEDIATE')
    sid = clean(data, 'shipment_id', True, 80)
    s = db.execute('SELECT * FROM shipments WHERE id=?', (sid,)).fetchone()
    if not s:
        raise ValueError('Shipment not found.')
    if path == '/api/documents/upload':
        auth.require(user, ('Admin', 'Operations', 'Sales'))
        category = clean(data, 'category', True)
        if category not in CATEGORIES:
            raise ValueError('Choose a valid document category.')
        if category == 'Proof of delivery':
            auth.require(user, ('Admin','Operations'))
        name = clean(data, 'name', True, 180)
        if any(c in name for c in '/\\\r\n\x00') or any(ord(c)<32 for c in name):
            raise ValueError('Use a filename without folders or control characters.')
        encoded = data.get('content')
        if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE+2)//3)*4:
            raise ValueError('Choose a file no larger than 10 MB.')
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('The file could not be decoded. Choose it again.')
        if not content or len(content) > MAX_FILE:
            raise ValueError('Choose a nonempty file no larger than 10 MB.')
        ext = name.rsplit('.', 1)[-1].lower()
        mime = {'pdf':'application/pdf','png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','txt':'text/plain'}.get(ext)
        if not mime:
            raise ValueError('Supported files: PDF, PNG, JPG and UTF-8 TXT.')
        valid = (ext == 'pdf' and content.startswith(b'%PDF-')) or (ext == 'png' and content.startswith(b'\x89PNG\r\n\x1a\n')) or (ext in ('jpg','jpeg') and content.startswith(b'\xff\xd8\xff'))
        if ext == 'txt':
            try:
                content.decode('utf-8')
                valid = b'\x00' not in content
            except UnicodeDecodeError:
                valid = False
        if not valid:
            raise ValueError('File contents do not match the selected file type.')
        checksum = hashlib.sha256(content).hexdigest()
        duplicate = db.execute('SELECT id FROM shipment_documents WHERE shipment_id=? AND checksum=? AND category=? AND archived_at IS NULL', (sid,checksum,category)).fetchone()
        if duplicate:
            return dict(ok=True, id=duplicate['id'], duplicate=True)
        doc_id = str(uuid.uuid4())
        db.execute('INSERT INTO shipment_documents(id,shipment_id,name,category,mime,size,checksum,content,uploaded_at,uploaded_by) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (doc_id,sid,name,category,mime,len(content),checksum,content,now(),user['id']))
        audit(db,sid,user,f'Document uploaded: {name} ({category}).')
        return dict(ok=True,id=doc_id)
    if path in ('/api/documents/archive','/api/documents/restore'):
        auth.require(user, ('Admin','Operations'))
        doc = db.execute('SELECT * FROM shipment_documents WHERE id=? AND shipment_id=?',(data.get('id'),sid)).fetchone()
        if not doc:
            raise ValueError('Document not found.')
        if data.get('version') != doc['version']:
            raise auth.AccessError('This document changed. Reload before continuing.',409)
        archive = path.endswith('/archive')
        reason = clean(data,'reason',True,300)
        if bool(doc['archived_at']) == archive:
            raise ValueError('This document is already in that state.')
        db.execute('UPDATE shipment_documents SET archived_at=?,version=version+1 WHERE id=?',(now() if archive else None,doc['id']))
        audit(db,sid,user,f'Document {"archived" if archive else "restored"}: {doc["name"]}. {reason}')
        return dict(ok=True)
    auth.require(user, ('Admin','Operations'))
    p = plan(db,sid)
    if data.get('version') != p['version'] or data.get('shipment_version') != s['version']:
        raise auth.AccessError('This shipment or dispatch plan changed. Reload its latest details before saving.',409)
    if path == '/api/dispatch/accept':
        if s['status'] == 'Delivered' or p['accepted_at']:
            raise ValueError('This shipment is already accepted or delivered.')
        ensure_plan(db,sid)
        db.execute('UPDATE dispatch_plans SET accepted_at=?,owner_id=?,version=version+1 WHERE shipment_id=?',(now(),user['id'],sid))
        audit(db,sid,user,'Operations accepted the shipment and took ownership.')
    elif path == '/api/dispatch/save':
        if s['status'] == 'Delivered':
            raise ValueError('Delivered shipments retain their dispatch records. Add any correction as an operations note.')
        owner_id = data.get('owner_id')
        if owner_id is not None and type(owner_id) is not int:
            raise ValueError('Select an active Operations or Admin owner.')
        if owner_id is not None:
            owner = db.execute("SELECT name FROM users WHERE id=? AND active=1 AND role IN ('Admin','Operations')",(owner_id,)).fetchone()
            if not owner:
                raise ValueError('Select an active Operations or Admin owner.')
        truck,driver,phone = (clean(data,k,False,200) for k in ('truck','driver','phone'))
        carrier = clean(data,'carrier',True,300)
        instructions = clean(data,'instructions')
        pickup = timestamp(data['pickup']) if data.get('pickup') else None
        if pickup and datetime.fromisoformat(pickup) > datetime.fromisoformat(s['eta']):
            raise ValueError('Pickup cannot be after expected delivery. Update the delivery time first.')
        ensure_plan(db,sid)
        db.execute('UPDATE dispatch_plans SET owner_id=?,truck=?,driver=?,phone=?,pickup=?,instructions=?,version=version+1 WHERE shipment_id=?',
                   (owner_id,truck,driver,phone,pickup,instructions,sid))
        db.execute('UPDATE shipments SET carrier=?,version=version+1 WHERE id=?',(carrier,sid))
        audit(db,sid,user,f'Dispatch plan saved. Owner: {owner["name"] if owner_id else "Unassigned"}; carrier: {carrier}; truck: {truck or "Unassigned"}; driver: {driver or "Unassigned"}; pickup: {pickup or "Unscheduled"}.')
    elif path in ('/api/dispatch/collect','/api/dispatch/deliver'):
        collected = path.endswith('/collect')
        if collected:
            owner = db.execute("SELECT id FROM users WHERE id=? AND active=1 AND role IN ('Admin','Operations')",(p['owner_id'],)).fetchone()
            if s['status'] != 'Booked' or not (p['accepted_at'] and owner and p['truck'] and p['driver'] and p['pickup'] and s['carrier'] not in ('','Awaiting Operations assignment')):
                raise ValueError('Accept, assign an active owner and transport, and schedule a booked shipment before recording collection.')
        elif s['status'] not in ('In transit','Out for delivery'):
            raise ValueError('Record collection before confirming delivery.')
        status = 'In transit' if collected else 'Delivered'
        ensure_plan(db,sid)
        column = 'collected_at' if collected else 'delivered_at'
        db.execute(f'UPDATE dispatch_plans SET {column}=?,version=version+1 WHERE shipment_id=?',(now(),sid))
        db.execute('UPDATE shipments SET status=?,version=version+1 WHERE id=?',(status,sid))
        message = 'Cargo collected. Shipment is in transit.' if collected else 'Delivery confirmed. Upload proof of delivery if still missing.'
        audit(db,sid,user,message)
        notify(db,dict(s,status=status),message)
    else:
        raise ValueError('Unknown dispatch action.')
    return dict(ok=True)
