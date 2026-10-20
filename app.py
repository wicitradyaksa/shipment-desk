"""Local shipment desk. Python standard library only."""
import argparse
import json
import sqlite3
import threading
import uuid
import ssl
import auth
import sales
import dispatch
import billing
import invoices
import payments
import payables
import workspace
import productivity
import profiles
import reports
import intake
import integrations
import adjustments
import backups
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, quote, parse_qs

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data' / 'shipments.sqlite3'
STATUSES = ('Booked', 'In transit', 'Out for delivery', 'Delivered', 'Exception')


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS shipments (
          id TEXT PRIMARY KEY, reference TEXT UNIQUE NOT NULL, customer TEXT NOT NULL,
          email TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
          carrier TEXT NOT NULL, tracking TEXT NOT NULL, eta TEXT NOT NULL,
          status TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, shipment_id TEXT REFERENCES shipments(id),
          time TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY, shipment_id TEXT REFERENCES shipments(id),
          time TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY, shipment_id TEXT REFERENCES shipments(id), kind TEXT NOT NULL,
          message TEXT NOT NULL, created TEXT NOT NULL, resolved TEXT,
          UNIQUE(shipment_id, kind));
        ''')
        auth.init(db)
        sales.init(db)
        dispatch.init(db)
        billing.init(db)
        invoices.init(db)
        payments.init(db)
        payables.init(db)
        productivity.init(db)
        profiles.init(db)
        intake.init(db)
        integrations.init(db)
        adjustments.init(db)
        for table, column, definition in [('shipments', 'version', 'INTEGER NOT NULL DEFAULT 1'), ('events', 'actor', "TEXT NOT NULL DEFAULT 'System / legacy'")]:
            if column not in [r['name'] for r in db.execute(f'PRAGMA table_info({table})')]:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def event(db, sid, message, actor="System"):
    db.execute('INSERT INTO events(shipment_id,time,message,actor) VALUES (?,?,?,?)', (sid, now(), message, actor))


def notify(db, s, message):
    db.execute('INSERT INTO notifications(shipment_id,time,recipient,subject,body) VALUES (?,?,?,?,?)',
               (s['id'], now(), s['email'], f"Shipment {s['reference']}: {s['status']}",
                f"Hello {s['customer']},\n\n{message}\n\nRoute: {s['origin']} to {s['destination']}\nCarrier: {s['carrier']}\nTracking reference: {s['tracking']}\nExpected delivery: {s['eta']}\n\nYour logistics team"))


def evaluate(db):
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    for s in db.execute('SELECT * FROM shipments').fetchall():
        conditions = {'overdue': s['status'] != 'Delivered' and datetime.fromisoformat(s['eta']) < datetime.now(timezone.utc),
                      'exception': s['status'] == 'Exception'}
        for kind, active in conditions.items():
            alert = db.execute('SELECT * FROM alerts WHERE shipment_id=? AND kind=?', (s['id'], kind)).fetchone()
            message = 'Delivery is overdue. Contact the carrier and confirm a revised ETA.' if kind == 'overdue' else 'Carrier exception. Investigate and contact the customer.'
            if active and (not alert or alert['resolved']):
                db.execute('INSERT INTO alerts(shipment_id,kind,message,created) VALUES (?,?,?,?) ON CONFLICT(shipment_id,kind) DO UPDATE SET resolved=NULL,created=excluded.created,message=excluded.message', (s['id'], kind, message, now()))
                event(db, s['id'], 'Operations alert opened: ' + message)
                if kind == 'overdue':
                    notify(db, s, 'Your shipment has passed its expected delivery time. Our operations team has been alerted to follow up.')
            elif not active and alert and not alert['resolved']:
                db.execute('UPDATE alerts SET resolved=? WHERE id=?', (now(), alert['id']))
                event(db, s['id'], f'{kind.capitalize()} alert automatically resolved.')


def clean(data, key, required=True):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value.strip()) > 300:
        raise ValueError(f'{key.capitalize()} must be text of 300 characters or fewer.')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{key.capitalize()} is required.')
    return value


def parse_eta(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise ValueError('Provide a valid delivery date and time with a timezone.')


def create(db, data, actor="System"):
    fields = {k: clean(data, k) for k in ('reference', 'customer', 'email', 'origin', 'destination', 'carrier', 'tracking')}
    if '@' not in fields['email'] or '.' not in fields['email'].split('@')[-1] or any(c.isspace() for c in fields['email']):
        raise ValueError('Provide a valid customer email address.')
    fields.update(id=str(uuid.uuid4()), eta=parse_eta(clean(data, 'eta')), status='Booked', created=now())
    fields['reference'] = fields['reference'].upper()
    db.execute('INSERT INTO shipments (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')', tuple(fields.values()))
    event(db, fields['id'], 'Shipment booked.', actor)
    notify(db, fields, 'Your shipment has been booked. We will keep you updated as it moves.')
    return fields['id']


def mutate(path, data, user=None):
    if user is not None:
        auth.require(user, ('Admin', 'Operations'))
    actor = f"{user['name']} (@{user['username']})" if user else "System"
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if path == '/api/shipments':
            sid = create(db, data, actor)
        elif path == '/api/update':
            sid = clean(data, 'id')
            s = db.execute('SELECT * FROM shipments WHERE id=?', (sid,)).fetchone()
            if not s:
                raise ValueError('Shipment not found.')
            if user is not None and data.get('version') != s['version']:
                raise auth.AccessError('Someone else updated this shipment. Reload its latest details before saving again.', 409)
            status = clean(data, 'status')
            if status not in STATUSES:
                raise ValueError('Choose a valid shipment status.')
            eta = parse_eta(clean(data, 'eta'))
            note = clean(data, 'note', False)
            carrier = clean(data, 'carrier') if 'carrier' in data else s['carrier']
            tracking = clean(data, 'tracking') if 'tracking' in data else s['tracking']
            changed = status != s['status'] or eta != s['eta'] or carrier != s['carrier'] or tracking != s['tracking']
            if changed:
                db.execute('UPDATE shipments SET status=?,eta=?,carrier=?,tracking=? WHERE id=?', (status, eta, carrier, tracking, sid))
                message = f'Status changed to {status}.' if status != s['status'] else 'Shipment details updated.'
                if carrier != s['carrier'] or tracking != s['tracking']:
                    message += f' Carrier: {carrier}; tracking: {tracking}.'
                if eta != s['eta']:
                    message += ' Revised expected delivery: ' + eta + '.'
                event(db, sid, message, actor)
                updated = dict(s, status=status, eta=eta, carrier=carrier, tracking=tracking)
                notify(db, updated, message)
            if note:
                event(db, sid, 'Operations note: ' + note, actor)
            if changed or note:
                db.execute('UPDATE shipments SET version=version+1 WHERE id=?', (sid,))
        elif path == '/api/demo':
            if db.execute('SELECT COUNT(*) FROM shipments').fetchone()[0]:
                raise ValueError('Sample shipments can only be loaded into an empty workspace.')
            for i, (customer, destination, hours, status) in enumerate([
                ('Sample / Formosa Coffee', 'Taichung', -3, 'In transit'),
                ('Sample / Harbor Supply', 'Kaohsiung', 8, 'Out for delivery'),
                ('Sample / Pacific Parts', 'Tainan', 24, 'Booked')]):
                sid = create(db, dict(reference=f'DEMO-{1001+i}', customer=customer, email=f'demo{i+1}@example.com', origin='Taipei', destination=destination, carrier='Demo carrier', tracking=f'TEST-{1001+i}', eta=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()))
                db.execute('UPDATE shipments SET status=? WHERE id=?', (status, sid))
                if status != 'Booked':
                    event(db, sid, f'Sample status set to {status}.')
        else:
            raise ValueError('Unknown action.')
        evaluate(db)
    return {'ok': True}


def snapshot():
    with connect() as db:
        evaluate(db)
        return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY ' + ('created DESC' if table == 'shipments' else 'id DESC'))]
                for table in ('shipments', 'events', 'notifications', 'alerts')}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, mime='application/json', filename=None):
        if mime == 'application/json':
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'")
        if getattr(self, 'session_cookie', None):
            self.send_header('Set-Cookie', self.session_cookie)
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        host = self.headers.get('Host', '')
        allowed = getattr(self.server, 'allowed_hosts', {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'})
        scheme = 'https' if getattr(self.server, 'secure', False) else 'http'
        return host in allowed and self.headers.get('Origin') in (None, f'{scheme}://{host}')

    def cookie(self, token='', clear=False):
        secure = '; Secure' if getattr(self.server, 'secure', False) else ''
        self.session_cookie = f'shipment_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={0 if clear else 28800}{secure}'

    def account_action(self, path, data):
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user = auth.current(db, self.headers)
            if path == '/api/setup':
                if self.client_address[0] not in ('127.0.0.1', '::1'):
                    raise auth.AccessError('Create the first admin on the server computer.')
                if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
                    raise auth.AccessError('Setup is already complete.')
                auth.add_user(db, data, setup=True)
            elif path == '/api/login':
                token, error = auth.login(db, data)
                if token:
                    self.cookie(token)
                return {'error': error} if error else {'ok': True}
            else:
                auth.require(user)
                if path == '/api/logout':
                    db.execute('DELETE FROM sessions WHERE token=?', (auth.token_from(self.headers),))
                    self.cookie(clear=True)
                elif path == '/api/password':
                    stored = db.execute('SELECT password FROM users WHERE id=?', (user['id'],)).fetchone()[0]
                    if not auth.verify(data.get('current_password', ''), stored):
                        raise ValueError('Current password is incorrect.')
                    db.execute('UPDATE users SET password=? WHERE id=?', (auth.password_hash(data.get('password')), user['id']))
                    db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
                    self.cookie(clear=True)
                elif path == '/api/users':
                    auth.require(user, ('Admin',))
                    auth.add_user(db, data)
                elif path == '/api/users/access':
                    auth.require(user, ('Admin',))
                    target = db.execute('SELECT * FROM users WHERE id=?', (data.get('id'),)).fetchone()
                    if not target:
                        raise ValueError('Staff account not found.')
                    if target['id'] == user['id']:
                        raise ValueError('You cannot change your own access here.')
                    role, active = data.get('role'), data.get('active')
                    if role not in auth.ROLES or type(active) is not bool:
                        raise ValueError('Choose a role and account status.')
                    db.execute('UPDATE users SET role=?,active=? WHERE id=?', (role, int(active), target['id']))
                    db.execute('DELETE FROM sessions WHERE user_id=?', (target['id'],))
                else:
                    raise ValueError('Unknown account action.')
        return {'ok': True}

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {'error': 'Local access only.'})
        path = urlparse(self.path).path
        if path.startswith('/api/'):
            with connect() as db:
                user = auth.current(db, self.headers)
                if path == '/api/me':
                    return self.send(200, {'user': user, 'setup': db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0})
                if not user:
                    return self.send(401, {'error': 'Please sign in.'})
                if path in ('/api/integrations','/api/integrations/accounting'):
                    try:
                        if path.endswith('/accounting'):
                            query=parse_qs(urlparse(self.path).query,keep_blank_values=True)
                            if any(len(v)!=1 for v in query.values()):raise ValueError('Use each filter once.')
                            content=integrations.accounting(db,user,{k:v[0] for k,v in query.items()})
                            return self.send(200,content,'text/csv; charset=utf-8','accounting-transactions.csv')
                        return self.send(200,integrations.state(db,user))
                    except auth.AccessError as error:return self.send(error.code,{'error':str(error)})
                    except ValueError as error:return self.send(400,{'error':str(error)})
                if path in ('/api/intake','/api/intake/template'):
                    try:
                        result=intake.state(db,user)
                        if path.endswith('/template'):
                            return self.send(200,(','.join(intake.FIELDS)+'\r\n').encode('utf-8-sig'),'text/csv; charset=utf-8','order-intake-template.csv')
                        return self.send(200,result)
                    except auth.AccessError as error:return self.send(error.code,{'error':str(error)})
                if path in ('/api/reports','/api/reports/export'):
                    try:
                        auth.require(user,('Admin',))
                        query=parse_qs(urlparse(self.path).query,keep_blank_values=True)
                        if any(len(v)!=1 for v in query.values()):raise ValueError('Use each report filter once.')
                        params={k:v[0] for k,v in query.items()}
                        db.execute('PRAGMA query_only=ON')
                        if path.endswith('/export'):
                            content,filename=reports.export(db,user,params,params.get('table',''))
                            return self.send(200,content,'text/csv; charset=utf-8',filename)
                        return self.send(200,reports.state(db,user,params))
                    except auth.AccessError as error:return self.send(error.code,{'error':str(error)})
                    except ValueError as error:return self.send(400,{'error':str(error)})
                if path=='/api/adjustments' or (path.startswith('/api/adjustments/') and path.endswith('/pdf')):
                    try:
                        if path=='/api/adjustments':return self.send(200,adjustments.state(db,user))
                        content,filename=adjustments.pdf(db,path.split('/')[-2],user)
                        return self.send(200,content,'application/pdf',filename)
                    except auth.AccessError as error:return self.send(error.code,{'error':str(error)})
                    except ValueError as error:return self.send(400,{'error':str(error)})
                if path == '/api/workspace':
                    result = workspace.state(db, user)
                    if user['role'] == 'Admin':
                        result['host'] = dict(secure=bool(getattr(self.server, 'secure', False)),
                            backup=getattr(self.server, 'backup_status', {'last_backup': None, 'error': 'Backup worker is not running.'}))
                    return self.send(200, result)
                if path == '/api/profiles':
                    try:return self.send(200,profiles.state(db,user))
                    except auth.AccessError as error:return self.send(error.code,{'error':str(error)})
                if path == '/api/dispatch':
                    return self.send(200, dispatch.state(db, user))
                if path == '/api/billing':
                    try:
                        return self.send(200, billing.state(db,user))
                    except auth.AccessError as error:
                        return self.send(error.code, {'error': str(error)})
                if path == '/api/payments':
                    try:
                        return self.send(200,payments.state(db,user))
                    except auth.AccessError as error:
                        return self.send(error.code,{'error':str(error)})
                if path == '/api/payables':
                    try:
                        return self.send(200,payables.state(db,user))
                    except auth.AccessError as error:
                        return self.send(error.code,{'error':str(error)})
                if path == '/api/invoices' or (path.startswith('/api/invoices/') and path.endswith('/pdf')):
                    try:
                        if path == '/api/invoices': return self.send(200,invoices.state(db,user))
                        content,filename=invoices.pdf(db,path.split('/')[-2],user)
                        return self.send(200,content,'application/pdf',filename)
                    except auth.AccessError as error:
                        return self.send(error.code,{'error':str(error)})
                    except ValueError as error:
                        return self.send(400,{'error':str(error)})
                if path.startswith('/api/documents/') and path.endswith('/download'):
                    doc = db.execute('SELECT name,content FROM shipment_documents WHERE id=? AND archived_at IS NULL', (path.split('/')[-2],)).fetchone()
                    if not doc:
                        return self.send(404, {'error': 'Document unavailable or archived.'})
                    return self.send(200, doc['content'], 'application/octet-stream', doc['name'])
                if path == '/api/workflow' or path.startswith('/api/proposal/'):
                    try:
                        result = sales.state(db, user) if path == '/api/workflow' else sales.proposal(db, path.rsplit('/', 1)[-1], user)
                        return self.send(200, result)
                    except auth.AccessError as error:
                        return self.send(error.code, {'error': str(error)})
                    except ValueError as error:
                        return self.send(400, {'error': str(error)})
                if path == '/api/users':
                    if user['role'] != 'Admin':
                        return self.send(403, {'error': 'Admin access required.'})
                    return self.send(200, {'users': [auth.public(r) for r in db.execute('SELECT * FROM users ORDER BY name')]})
            if path in ('/api/state', '/api/export'):
                return self.send(200, snapshot())
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/workflow.js': ('workflow.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        files['/dispatch.js'] = ('dispatch.js', 'text/javascript; charset=utf-8')
        files['/billing.js'] = ('billing.js', 'text/javascript; charset=utf-8')
        files['/invoices.js'] = ('invoices.js', 'text/javascript; charset=utf-8')
        files['/payments.js'] = ('payments.js', 'text/javascript; charset=utf-8')
        files['/payables.js'] = ('payables.js', 'text/javascript; charset=utf-8')
        files['/workspace.js'] = ('workspace.js', 'text/javascript; charset=utf-8')
        files['/productivity.js'] = ('productivity.js', 'text/javascript; charset=utf-8')
        files['/profiles.js'] = ('profiles.js', 'text/javascript; charset=utf-8')
        files['/reports.js'] = ('reports.js', 'text/javascript; charset=utf-8')
        files['/intake.js'] = ('intake.js', 'text/javascript; charset=utf-8')
        files['/integrations.js'] = ('integrations.js', 'text/javascript; charset=utf-8')
        files['/adjustments.js'] = ('adjustments.js', 'text/javascript; charset=utf-8')
        if path not in files:
            return self.send(404, {'error': 'Not found.'})
        filename, mime = files[path]
        self.send(200, (ROOT / 'public' / filename).read_bytes(), mime)

    def do_POST(self):
        if not self.allowed() or self.headers.get('Content-Type') != 'application/json':
            return self.send(403, {'error': 'Local JSON requests only.'})
        try:
            path = urlparse(self.path).path
            length = int(self.headers.get('Content-Length', '0'))
            limit = ((dispatch.MAX_FILE+2)//3)*4+4096 if path == '/api/documents/upload' else 262144
            if length < 1 or length > limit:
                raise ValueError('Invalid request size.')
            raw=self.rfile.read(length)
            if path=='/api/integrations/tracking-webhook':
                with connect() as db:result=integrations.webhook(db,raw,self.headers)
                return self.send(200,result)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            path = urlparse(self.path).path
            if path in ('/api/setup', '/api/login', '/api/logout', '/api/password', '/api/users', '/api/users/access'):
                result = self.account_action(path, data)
                return self.send(401 if result.get('error') else 200, result)
            with connect() as db:
                user = auth.current(db, self.headers)
                if path.startswith('/api/quotes') or path in ('/api/pricing', '/api/notices/read'):
                    result = sales.action(db, path, data, user, create)
                elif path.startswith('/api/workspace/'):
                    result = workspace.action(db,path,data,user)
                elif path.startswith('/api/adjustments/'):
                    result = adjustments.action(db,path,data,user)
                elif path.startswith('/api/integrations/'):
                    result = integrations.action(db,path,data,user)
                elif path.startswith('/api/intake/'):
                    result = intake.action(db,path,data,user,create)
                elif path.startswith('/api/profiles/'):
                    result = profiles.action(db,path,data,user)
                elif path.startswith('/api/billing/'):
                    result = billing.action(db,path,data,user)
                elif path.startswith('/api/invoices/'):
                    result = invoices.action(db,path,data,user)
                elif path.startswith('/api/payments/'):
                    result = payments.action(db,path,data,user)
                elif path.startswith('/api/payables/'):
                    result = payables.action(db,path,data,user)
                elif path.startswith('/api/dispatch/') or path.startswith('/api/documents/'):
                    result = dispatch.action(db, path, data, user, notify)
                    evaluate(db)
                else:
                    result = None
                if result is not None and path not in ('/api/intake/preview','/api/adjustments/preview') and not path.startswith('/api/integrations/'):
                    workspace.reconcile(db)
            if result is not None:
                return self.send(200, result)
            auth.require(user, ('Admin', 'Operations'))
            self.send(200, mutate(path, data, user))
        except auth.AccessError as error:
            self.send(error.code, {'error': str(error)})
        except sqlite3.IntegrityError:
            self.send(409, {'error': 'That shipment reference or username already exists. Use a unique value.'})
        except (ValueError, TypeError) as error:
            self.send(400, {'error': str(error)})
        except Exception as error:
            self.log_error('Request failed: %s', error)
            self.send(500, {'error': 'Could not save. Check the server window and try again.'})


def monitor(stop):
    while not stop.wait(30):
        try:
            with connect() as db:
                evaluate(db)
                workspace.state(db,dict(id=0,role='Admin'))
        except Exception as error:
            print(f'Delay check failed: {error}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--cert')
    parser.add_argument('--key')
    args = parser.parse_args()
    if args.host != '127.0.0.1' and not (args.cert and args.key):
        parser.error('Office-network access requires --cert and --key for HTTPS.')
    init_db()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.allowed_hosts = {f'127.0.0.1:{args.port}', f'localhost:{args.port}', f'{args.host}:{args.port}'}
    server.secure = bool(args.cert and args.key)
    if server.secure:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    stop = threading.Event()
    server.backup_status = {'last_backup': None, 'error': None}
    threading.Thread(target=backups.monitor, args=(stop, DB, ROOT/'backups', server.backup_status), daemon=True).start()
    threading.Thread(target=monitor, args=(stop,), daemon=True).start()
    scheme = 'https' if server.secure else 'http'
    print(f'Shipment Desk is running at {scheme}://{args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
