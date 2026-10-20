"""Pricing snapshots and Sales -> Operations workflow, all within SQLite transactions."""
import json
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
import auth

DEFAULT_CONFIG = dict(currency='', margin='', validity_days=14,
                      per_km='0', charter_day='0', terms='', routes=[], cargo=[])


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS pricing_config (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS quotes (
      id TEXT PRIMARY KEY, reference TEXT UNIQUE NOT NULL, owner_id INTEGER REFERENCES users(id),
      specs TEXT NOT NULL, pricing TEXT NOT NULL, status TEXT NOT NULL,
      version INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, updated TEXT NOT NULL,
      valid_until TEXT, shipment_id TEXT UNIQUE REFERENCES shipments(id), acceptance TEXT);
    CREATE TABLE IF NOT EXISTS quote_events (
      id INTEGER PRIMARY KEY, quote_id TEXT REFERENCES quotes(id), time TEXT NOT NULL,
      actor TEXT NOT NULL, message TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS team_notices (
      id INTEGER PRIMARY KEY, quote_id TEXT REFERENCES quotes(id), user_id INTEGER REFERENCES users(id),
      audience TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS notice_reads (
      notice_id INTEGER REFERENCES team_notices(id), user_id INTEGER REFERENCES users(id),
      PRIMARY KEY(notice_id,user_id));
    CREATE TABLE IF NOT EXISTS pricing_history (
      id INTEGER PRIMARY KEY, time TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
    ''')
    db.execute('INSERT OR IGNORE INTO pricing_config VALUES (1,0,?)', (json.dumps(DEFAULT_CONFIG),))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def actor(user):
    return f"{user['name']} (@{user['username']})"


def text(data, key, required=True, limit=1500):
    if not isinstance(data, dict):
        raise ValueError('Each cost or pricing item must be an object with named fields.')
    value = data.get(key, '')
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError(f'Provide {key.replace("_", " ")} (up to {limit} characters).')
    return value.strip()


def number(value, label, maximum='100000000', positive=False):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label} must be a number.')
    if not n.is_finite() or n < 0 or n > Decimal(maximum) or (positive and n == 0):
        raise ValueError(f'{label} must be {"greater than zero and " if positive else ""}between 0 and {maximum}.')
    return n


def money(n):
    return str(n.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def config(db):
    row = db.execute('SELECT * FROM pricing_config WHERE id=1').fetchone()
    return dict(json.loads(row['body']), version=row['version'])


def validate_config(data):
    currency = text(data, 'currency').upper()
    if currency not in ('TWD', 'USD', 'EUR', 'GBP', 'CNY', 'HKD', 'SGD', 'AUD', 'CAD'):
        raise ValueError('Choose a supported currency.')
    validity = number(data.get('validity_days'), 'Validity days', '365', True)
    if validity != validity.to_integral_value():
        raise ValueError('Proposal validity must be a whole number of days.')
    result = dict(currency=currency, margin=str(number(data.get('margin'), 'Gross margin', '95')),
                  validity_days=int(validity), terms=text(data, 'terms'))
    if result['validity_days'] < 1:
        raise ValueError('Validity must be at least one day.')
    for key in ('per_km', 'charter_day'):
        result[key] = money(number(data.get(key), key))
    for group in ('routes', 'cargo'):
        rows = data.get(group)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('Configure up to 100 routes and cargo types.')
        result[group] = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid pricing row.')
            key = text(row, 'id', limit=80)
            if key in seen or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
                raise ValueError('Each pricing row needs a unique ID using letters, numbers, dashes or underscores.')
            seen.add(key)
            item = dict(id=key, name=text(row, 'name', limit=120))
            keys = ('base', 'max_kg', 'max_cbm') if group == 'routes' else ('per_kg', 'per_cbm', 'handling', 'max_kg', 'max_cbm')
            for field in keys:
                item[field] = str(number(row.get(field), field, positive=field.startswith('max_')))
            if group == 'routes':
                item.update(origin=text(row, 'origin', limit=300), destination=text(row, 'destination', limit=300))
                if Decimal(item['base']) <= 0:
                    raise ValueError('Standard route base cost must be greater than zero.')
            else:
                if type(row.get('generic')) is not bool:
                    raise ValueError('Specify whether each cargo type is generic.')
                item['generic'] = row['generic']
            result[group].append(item)
    if not result['cargo']:
        raise ValueError('Add at least one cargo type.')
    return result


def specs(data, cfg):
    out = {k: text(data, k, limit=300) for k in ('customer', 'email', 'description', 'origin', 'destination')}
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', out['email']):
        raise ValueError('Provide a valid customer email.')
    out.update(route_id=text(data, 'route_id', False, 80), cargo_id=text(data, 'cargo_id', True, 80),
               requirements=text(data, 'requirements', False), extras_note=text(data, 'extras_note', False))
    for key in ('weight_kg', 'volume_cbm', 'packages', 'outbound_km', 'return_km', 'charter_days', 'tolls', 'fuel', 'other'):
        out[key] = str(number(data.get(key, '0'), key, positive=key in ('weight_kg', 'volume_cbm', 'packages')))
    if Decimal(out['packages']) != Decimal(out['packages']).to_integral_value():
        raise ValueError('Packages must be a whole number.')
    for key in ('pickup', 'eta'):
        try:
            value = datetime.fromisoformat(text(data, key).replace('Z', '+00:00'))
            if value.tzinfo is None:
                raise ValueError()
            out[key] = value.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise ValueError('Pickup and delivery must include valid dates and times.')
    if datetime.fromisoformat(out['pickup']) >= datetime.fromisoformat(out['eta']) or datetime.fromisoformat(out['pickup']) <= datetime.now(timezone.utc):
        raise ValueError('Pickup must be in the future and before expected delivery.')
    if Decimal(out['other']) > 0 and not out['extras_note']:
        raise ValueError('Describe the other costs.')
    if not any(c['id'] == out['cargo_id'] for c in cfg['cargo']):
        raise ValueError('Choose a configured cargo type; ask Admin to add missing types.')
    return out


def totals(lines, margin, currency):
    cost = sum((Decimal(x['amount']) for x in lines), Decimal(0))
    margin = number(margin, 'Gross margin', '95')
    if cost <= 0:
        raise ValueError('Total cost must be greater than zero. Configure rates or enter the actual costs.')
    price = cost / (Decimal(1)-margin/100)
    return dict(lines=lines, cost=money(cost), margin=str(margin), total=money(price),
                profit=money(Decimal(money(price))-cost), currency=currency)


def calculate(s, cfg):
    cargo = next(c for c in cfg['cargo'] if c['id'] == s['cargo_id'])
    route = next((r for r in cfg['routes'] if r['id'] == s['route_id']), None)
    if s['route_id'] and not route:
        raise ValueError('That standard route is no longer configured.')
    if route and (s['origin'] != route['origin'] or s['destination'] != route['destination']):
        raise ValueError('The selected standard route does not match the origin and destination.')
    reasons = []
    if not route:
        reasons.append('Nonstandard route')
    if not cargo['generic']:
        reasons.append('Special cargo')
    for item in [cargo] + ([route] if route else []):
        if Decimal(s['weight_kg']) > Decimal(item['max_kg']) or Decimal(s['volume_cbm']) > Decimal(item['max_cbm']):
            reasons.append('Exceeds configured size or weight limits')
    if s['requirements'] or any(Decimal(s[k]) > 0 for k in ('outbound_km', 'return_km', 'charter_days', 'tolls', 'fuel', 'other')):
        reasons.append('Additional service or cost requirements')
    generic = not reasons
    lines = []
    def line(label, quantity, rate):
        lines.append(dict(label=label, quantity=str(quantity), rate=str(rate), amount=money(Decimal(quantity)*Decimal(rate))))
    if generic:
        line('Standard route: ' + route['name'], 1, route['base'])
    else:
        # A selected route still supplies its base if distance/charter is not used.
        if route and not any(Decimal(s[k]) > 0 for k in ('outbound_km', 'return_km', 'charter_days')):
            line('Route base: ' + route['name'], 1, route['base'])
        line('Outbound distance (km)', s['outbound_km'], cfg['per_km'])
        line('Return distance (km)', s['return_km'], cfg['per_km'])
        line('Truck charter (days)', s['charter_days'], cfg['charter_day'])
    line('Cargo weight (kg): ' + cargo['name'], s['weight_kg'], cargo['per_kg'])
    line('Cargo volume (m³)', s['volume_cbm'], cargo['per_cbm'])
    line('Cargo handling', 1, cargo['handling'])
    if not generic:
        for key, label in [('tolls', 'Tolls'), ('fuel', 'Fuel surcharge'), ('other', 'Other costs')]:
            line(label, 1, s[key])
    # Zero estimates may be captured for Operations, but never produce proposals.
    cost = sum(Decimal(x['amount']) for x in lines)
    if generic and cost <= 0:
        raise ValueError('Standard pricing rounds to zero. Ask Admin to correct the rates before generating a proposal.')
    pricing = totals(lines, cfg['margin'], cfg['currency']) if cost > 0 else dict(lines=lines, cost='0.00', total='0.00', profit='0.00', margin=cfg['margin'], currency=cfg['currency'])
    pricing.update(generic=generic, reasons=list(dict.fromkeys(reasons)), rule_version=cfg['version'],
                   cargo_name=cargo['name'], route_name=route['name'] if route else 'Custom route', validity_days=cfg['validity_days'],
                   terms=cfg['terms'], proposal_version=1)
    return pricing


def log(db, qid, user, message):
    db.execute('INSERT INTO quote_events(quote_id,time,actor,message) VALUES (?,?,?,?)', (qid, stamp(), actor(user), message))


def notice(db, qid, audience, message, user_id=None):
    db.execute('INSERT INTO team_notices(quote_id,audience,message,user_id,created) VALUES (?,?,?,?,?)', (qid, audience, message, user_id, stamp()))


def visible(user, q):
    return user['role'] in ('Admin', 'Operations') or (user['role'] == 'Sales' and q['owner_id'] == user['id'])


def decode(q):
    return dict(q, specs=json.loads(q['specs']), pricing=json.loads(q['pricing']))


def state(db, user):
    auth.require(user, ('Admin', 'Sales', 'Operations'))
    rows = [decode(q) for q in db.execute('SELECT q.*,u.name AS owner_name FROM quotes q JOIN users u ON q.owner_id=u.id ORDER BY q.created DESC') if visible(user, q)]
    ids = {q['id'] for q in rows}
    notices = [dict(n) for n in db.execute('SELECT n.*,r.user_id IS NOT NULL AS is_read FROM team_notices n LEFT JOIN notice_reads r ON r.notice_id=n.id AND r.user_id=? ORDER BY n.id DESC', (user['id'],))
               if (n['audience'] == 'Operations' and user['role'] in ('Operations', 'Admin')) or n['user_id'] == user['id']]
    return dict(config=config(db), quotes=rows, events=[dict(e) for e in db.execute('SELECT * FROM quote_events ORDER BY id DESC') if e['quote_id'] in ids], notices=notices)


def action(db, path, data, user, create_shipment, begin=True):
    auth.require(user, ('Admin', 'Sales', 'Operations'))
    if begin: db.execute('BEGIN IMMEDIATE')
    if path == '/api/pricing':
        auth.require(user, ('Admin',))
        current = config(db)
        if data.get('version') != current['version']:
            raise auth.AccessError('Pricing changed. Reload before saving.', 409)
        cfg = validate_config(data)
        body = json.dumps(cfg)
        db.execute('UPDATE pricing_config SET body=?,version=version+1 WHERE id=1', (body,))
        db.execute('INSERT INTO pricing_history(time,actor,body) VALUES (?,?,?)', (stamp(), actor(user), body))
        return {'ok': True}
    if path == '/api/notices/read':
        allowed = {n['id'] for n in state(db, user)['notices']}
        if data.get('id') not in allowed:
            raise auth.AccessError('Notification not available.')
        db.execute('INSERT OR IGNORE INTO notice_reads VALUES (?,?)', (data['id'], user['id']))
        return {'ok': True}
    if path == '/api/quotes':
        auth.require(user, ('Admin', 'Sales'))
        cfg = config(db)
        if not cfg['version']:
            raise ValueError('Admin must configure pricing before Sales submits a shipment.')
        s = specs(data, cfg)
        pricing = calculate(s, cfg)
        qid = str(uuid.uuid4())
        reference = 'Q-' + uuid.uuid4().hex[:10].upper()
        status = 'Proposal ready' if pricing['generic'] else 'Operations review'
        valid = (datetime.now(timezone.utc)+timedelta(days=cfg['validity_days'])).isoformat() if pricing['generic'] else None
        db.execute('INSERT INTO quotes(id,reference,owner_id,specs,pricing,status,created,updated,valid_until) VALUES (?,?,?,?,?,?,?,?,?)',
                   (qid, reference, user['id'], json.dumps(s), json.dumps(pricing), status, stamp(), stamp(), valid))
        log(db, qid, user, 'Potential shipment submitted. ' + ('Standard pricing generated a proposal.' if pricing['generic'] else 'Cost estimate sent to Operations for review.'))
        if not pricing['generic']:
            notice(db, qid, 'Operations', reference + ': review cargo, route and estimated costs.')
        return {'ok': True, 'id': qid}
    q = db.execute('SELECT * FROM quotes WHERE id=?', (data.get('id'),)).fetchone()
    if not q or not visible(user, q):
        raise auth.AccessError('Quote not available.')
    if data.get('version') != q['version']:
        raise auth.AccessError('This quote changed. Reload its latest details before continuing.', 409)
    q = decode(q)
    if path == '/api/quotes/review':
        auth.require(user, ('Admin', 'Operations'))
        if q['status'] != 'Operations review':
            raise ValueError('This quote is not awaiting Operations review.')
        note = text(data, 'note')
        if data.get('decision') == 'return':
            status = 'Changes requested'
            log(db, q['id'], user, 'Changes requested: ' + note)
        elif data.get('decision') == 'approve':
            lines = data.get('lines')
            if not isinstance(lines, list) or not 1 <= len(lines) <= 30:
                raise ValueError('Provide 1–30 cost lines.')
            clean_lines = [dict(label=text(item, 'label', limit=150), amount=money(number(item.get('amount'), 'Cost')), quantity='1', rate=money(number(item.get('amount'), 'Cost'))) for item in lines]
            pricing = dict(q['pricing'], **totals(clean_lines, q['pricing']['margin'], q['pricing']['currency']))
            pricing['proposal_version'] = q['version'] + 1
            valid = (datetime.now(timezone.utc)+timedelta(days=pricing['validity_days'])).isoformat()
            db.execute('UPDATE quotes SET pricing=?,valid_until=? WHERE id=?', (json.dumps(pricing), valid, q['id']))
            status = 'Proposal ready'
            log(db, q['id'], user, 'Operations approved costs and released the proposal. ' + note)
        else:
            raise ValueError('Choose approve or request changes.')
        notice(db, q['id'], 'Sales', q['reference'] + ': ' + status + '.', q['owner_id'])
    elif path == '/api/quotes/revise':
        auth.require(user, ('Admin', 'Sales'))
        if q['status'] == 'Closed won':
            raise ValueError('A confirmed shipment cannot be revised as a quote.')
        cfg = config(db)
        s = specs(data, cfg)
        pricing = calculate(s, cfg)
        pricing['proposal_version'] = q['version'] + 1
        status = 'Proposal ready' if pricing['generic'] else 'Operations review'
        valid = (datetime.now(timezone.utc)+timedelta(days=cfg['validity_days'])).isoformat() if pricing['generic'] else None
        db.execute('UPDATE quotes SET specs=?,pricing=?,valid_until=? WHERE id=?', (json.dumps(s), json.dumps(pricing), valid, q['id']))
        log(db, q['id'], user, 'Specifications revised and repriced. Any previous proposal and approval are superseded.')
        if not pricing['generic']:
            notice(db, q['id'], 'Operations', q['reference'] + ': revised shipment needs review.')
    elif path == '/api/quotes/sent':
        auth.require(user, ('Admin', 'Sales'))
        check_proposal(q)
        if q['status'] != 'Proposal ready':
            raise ValueError('This proposal is already marked as sent.')
        status = 'Awaiting client'
        log(db, q['id'], user, 'Sales recorded that the proposal was forwarded to the client. ' + text(data, 'note'))
    elif path == '/api/quotes/close':
        auth.require(user, ('Admin', 'Sales'))
        check_proposal(q)
        if q['status'] != 'Awaiting client':
            raise ValueError('Record that the proposal was sent before confirming the deal.')
        acceptance = text(data, 'acceptance')
        if data.get('confirmed') is not True:
            raise ValueError('Confirm the client accepted the proposal.')
        if datetime.fromisoformat(q['specs']['pickup']) <= datetime.now(timezone.utc):
            raise ValueError('Pickup time has passed. Revise the quote and obtain client acceptance again.')
        s = q['specs']
        sid = create_shipment(db, dict(reference='S-' + q['reference'][2:], customer=s['customer'], email=s['email'],
                                      origin=s['origin'], destination=s['destination'], carrier='Awaiting Operations assignment',
                                      tracking='Not assigned', eta=s['eta']), actor(user))
        db.execute('UPDATE quotes SET shipment_id=?,acceptance=? WHERE id=?', (sid, acceptance, q['id']))
        status = 'Closed won'
        log(db, q['id'], user, 'Client acceptance recorded: ' + acceptance + '. Shipment created for Operations.')
        notice(db, q['id'], 'Operations', q['reference'] + ': deal confirmed. Assign carrier and arrange pickup.')
    else:
        raise ValueError('Unknown quote action.')
    db.execute('UPDATE quotes SET status=?,version=version+1,updated=? WHERE id=?', (status, stamp(), q['id']))
    return {'ok': True, 'id': q['id']}


def check_proposal(q):
    if q['status'] not in ('Proposal ready', 'Awaiting client', 'Closed won'):
        raise ValueError('Operations must approve this quote before a client proposal is available.')
    if q['status'] != 'Closed won' and (not q['valid_until'] or datetime.fromisoformat(q['valid_until']) <= datetime.now(timezone.utc)):
        raise ValueError('This proposal expired. Revise and reprice it before proceeding.')
    if q['status'] != 'Closed won' and datetime.fromisoformat(q['specs']['pickup']) <= datetime.now(timezone.utc):
        raise ValueError('Pickup time has passed. Revise the specifications before using this proposal.')


def proposal(db, qid, user):
    q = db.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
    if not q or not visible(user, q):
        raise auth.AccessError('Quote not available.')
    q = decode(q)
    check_proposal(q)
    s, p = q['specs'], q['pricing']
    # Deliberately excludes internal costs, margin, profit and review notes.
    return dict(reference=q['reference'], version=p['proposal_version'], customer=s['customer'], email=s['email'],
                origin=s['origin'], destination=s['destination'], description=s['description'], cargo=p['cargo_name'],
                weight_kg=s['weight_kg'], volume_cbm=s['volume_cbm'], packages=s['packages'],
                pickup=s['pickup'], eta=s['eta'], requirements=s['requirements'], currency=p['currency'],
                total=p['total'], valid_until=q['valid_until'], terms=p['terms'])
