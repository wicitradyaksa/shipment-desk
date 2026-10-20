"""Role-scoped next actions derived from the authoritative workflow records."""
from datetime import datetime, timezone
from decimal import Decimal
import auth
import sales
import dispatch
import billing
import invoices
import payments
import payables
import productivity


def raw_state(db, user):
    auth.require(user)
    role = user['role']
    items = []
    now = datetime.now(timezone.utc)

    def add(view, record_id, kind, reference, title, team, owner=None, urgent=False, due=None):
        items.append(dict(id=f'{view}:{record_id}:{kind}', view=view, record_id=record_id,
                          reference=reference, title=title, team=team, owner_id=owner,
                          urgent=bool(urgent), due=due))

    if role == 'Viewer':
        return dict(items=[], notices=[], generated_at=now.isoformat())
    flow = sales.state(db, user)
    for q in flow['quotes']:
        status = q['status']
        if role in ('Admin', 'Operations') and status == 'Operations review':
            add('quotes', q['id'], 'review', q['reference'], 'Review custom shipment costing', 'Operations')
        if role in ('Admin', 'Sales') and status in ('Proposal ready', 'Awaiting client', 'Changes requested'):
            expired = bool(q['valid_until'] and datetime.fromisoformat(q['valid_until']) < now)
            expired = expired or datetime.fromisoformat(q['specs']['pickup']) < now
            title = 'Revise expired proposal or pickup date' if expired else {
                'Proposal ready': 'Forward proposal to client', 'Awaiting client': 'Follow up on client approval',
                'Changes requested': 'Revise shipment specifications'}[status]
            add('quotes', q['id'], 'sales-'+status, q['reference'], title, 'Sales', q['owner_id'], expired, q['valid_until'])

    dispatch_state = dispatch.state(db, user)
    plans = {s['id']:s['plan'] for s in dispatch_state['shipments']}
    live_owners = {o['id'] for o in dispatch_state['owners']}
    if role in ('Admin', 'Operations'):
        for s in dispatch_state['shipments']:
            p = s['plan']
            owner = p['owner_id'] if p['owner_id'] in live_owners else None
            if s['status'] != 'Delivered':
                late = datetime.fromisoformat(s['eta']) < now
                title = ('Resolve delivery exception' if s['status'] == 'Exception' else
                         'Review overdue delivery' if late else 'Accept shipment handoff' if not p['accepted_at'] else
                         'Complete dispatch assignment' if s['unassigned'] or not p['pickup'] else
                         'Confirm collection' if s['status'] == 'Booked' else 'Update delivery progress')
                add('dispatch', s['id'], 'dispatch-'+s['stage'], s['reference'], title, 'Operations', owner,
                    late or s['pickup_overdue'] or s['status'] == 'Exception', p['pickup'] if s['status']=='Booked' and p['pickup'] else s['eta'])
            elif not s['has_pod']:
                add('dispatch', s['id'], 'pod', s['reference'], 'Upload proof of delivery', 'Operations', owner, True)

    bills = billing.state(db, user)['shipments']
    for s in bills:
        r = s['record']
        owner = plans[s['id']]['owner_id']
        owner = owner if owner in live_owners else None
        if role in ('Admin', 'Operations') and s['status'] == 'Delivered':
            active_docs = {d['id'] for d in s['documents']}
            if not r['submitted_at'] or any(c['document_id'] not in active_docs for c in r['costs']):
                add('billing', s['id'], 'costs', s['reference'], 'Complete costs and supporting documents', 'Operations', owner)
        if role in ('Admin', 'Sales') and any(e['decision'] == 'Pending' for e in r['extras']):
            add('billing', s['id'], 'extras', s['reference'], 'Record client decision on extra charges', 'Sales', s['sales_owner'])
        if role == 'Admin':
            if not s['terms']:
                add('billing', s['id'], 'terms', s['reference'], 'Record accepted commercial terms', 'Admin')
            elif not s['approval_valid'] and not s['blockers']:
                add('billing', s['id'], 'approve', s['reference'], 'Review billing approval', 'Admin')

    if role in ('Admin', 'Operations'):
        for b in payables.state(db, user)['bills']:
            if b['status'] in ('Draft', 'Changes requested'):
                owner = plans[b['shipment_id']]['owner_id']
                add('payables', b['id'], 'submit', b['reference'], 'Complete supplier bill for review', 'Operations',
                    owner if owner in live_owners else None)
            elif role == 'Admin' and b['status'] != 'Cancelled':
                if not b['approval_valid']:
                    add('payables', b['id'], 'approve', b['reference'], 'Review supplier bill and cost match', 'Admin', urgent=b['overdue'])
                elif Decimal(b['balance']) > 0:
                    add('payables', b['id'], 'pay', b['reference'], 'Arrange payment and record bank confirmation', 'Admin', urgent=b['overdue'], due=b['due_date'])
    if role == 'Admin':
        invoice_state = invoices.state(db, user)
        for s in invoice_state['ready']:
            add('invoices', s['id'], 'create', s['reference'], 'Prepare customer invoice', 'Admin')
        for i in invoice_state['invoices']:
            if i['status'] == 'Draft':
                add('invoices', i['id'], 'issue', i['snapshot']['reference'], 'Review and issue customer invoice', 'Admin')
        for i in payments.state(db, user)['invoices']:
            if Decimal(i['refund_due'])>0:
                add('adjustments',i['id'],'refund',i['number'],'Record verified customer refund','Admin',urgent=True)
            if Decimal(i['balance']) > 0:
                add('payments', i['id'], 'collect', i['number'],
                    'Follow up overdue customer payment' if i['overdue'] else 'Track customer payment', 'Admin', urgent=i['overdue'], due=i['due_date'])
    items.sort(key=lambda i: (not i['urgent'], i['due'] or '9999', i['reference'], i['id']))
    return dict(items=items, notices=[n for n in flow['notices'] if not n['is_read']], generated_at=now.isoformat())


def reconcile(db):
    productivity.sync(db,raw_state(db,dict(id=0,role='Admin'))['items'])


def state(db,user):
    auth.require(user)
    if user['role']=='Viewer':return raw_state(db,user)
    if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
    reconcile(db)
    return productivity.enrich(db,raw_state(db,user),user)


def action(db,path,data,user):
    auth.require(user,('Admin','Sales','Operations'))
    db.execute('BEGIN IMMEDIATE')
    visible=state(db,user)
    return productivity.action(db,path,data,user,visible)
