"""Read-only management reports, using existing financial source calculations."""
import csv
import json
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal
import auth
import billing
import invoices
import payments
import payables
import adjustments

BANDS=('current','days_1_30','days_31_60','days_61_90','days_90_plus')
AGE_COLUMNS=[('current','Current'),('days_1_30','1–30 days'),('days_31_60','31–60 days'),('days_61_90','61–90 days'),('days_90_plus','90+ days')]
PROFIT_COLUMNS=[('basis','Basis'),('group','Group'),('currency','Currency'),('shipments','Shipments'),('revenue','Revenue'),('actual_cost','Actual cost'),('profit','Profit'),('margin','Margin %')]
KPI_COLUMNS=[('currency','Currency'),('shipments','Shipments'),('delivered','Delivered'),('delivery_samples','Delivery timestamps available'),('missing_delivery_time','Delivered without timestamp'),('on_time','On-time deliveries'),('on_time_percent','On-time delivery %'),('exceptions','Shipments with exceptions'),('delays','Shipments with delays'),('exceptions_or_delays','Exceptions or delays (unique shipments)'),('delivery_invoice_samples','Delivery-to-issue samples'),('delivery_invoice_days','Average delivery-to-invoice-issued days'),('fully_paid_samples','Fully paid invoice samples'),('invoice_paid_days','Average invoice-date-to-fully-paid days')]


def instant(value):
    if not value:return None
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError,ValueError):return None


def calendar_day(value):
    if not value:return None
    if len(value)==10:
        try:return date.fromisoformat(value)
        except ValueError:return None
    dt=instant(value)
    return dt.date() if dt else None


def filters(params):
    result={k:params.get(k,'') for k in ('start','end','currency')}
    for key in ('start','end'):
        value=result[key]
        if value:
            try:
                if date.fromisoformat(value).isoformat()!=value:raise ValueError()
            except (TypeError,ValueError):raise ValueError('Use dates in YYYY-MM-DD format.')
    if result['start'] and result['end'] and result['start']>result['end']:raise ValueError('Start date must not follow end date.')
    if not isinstance(result['currency'],str) or result['currency'] and not re.fullmatch('[A-Z]{3}',result['currency']):raise ValueError('Choose a three-letter currency code or all currencies.')
    return result


def included(value,currency,f):
    if f['currency'] and f['currency']!=currency:return False
    day=calendar_day(value)
    if not day:return not (f['start'] or f['end'])
    return (not f['start'] or day>=date.fromisoformat(f['start'])) and (not f['end'] or day<=date.fromisoformat(f['end']))


def cash(value):return str(value.quantize(Decimal('.01')))
def average(values):return cash(sum(values,Decimal(0))/len(values)) if values else None
def table(title,columns,rows):return dict(title=title,columns=[dict(key=k,label=l) for k,l in columns],rows=rows)
def band(days):return BANDS[0 if days<=0 else 1 if days<=30 else 2 if days<=60 else 3 if days<=90 else 4]


def aging(rows,party):
    groups={}
    for name,currency,balance,days in rows:
        r=groups.setdefault((name,currency),dict(party=name,currency=currency,documents=0,total=Decimal(0),**{k:Decimal(0) for k in BANDS}))
        r['documents']+=1;r['total']+=balance;r[band(days)]+=balance
    return [dict(r,**{k:cash(r[k]) for k in (*BANDS,'total')}) for _,r in sorted(groups.items())]


def state(db,user,params=None):
    auth.require(user,('Admin',))
    f=filters(params or {})
    # Keep all source reads on one SQLite snapshot. No schema or workflow writes.
    if not db.in_transaction:db.execute('BEGIN')
    today=payments.today();now=datetime.now(timezone.utc)
    sources=[billing.summary(db,dict(s)) for s in db.execute('SELECT * FROM shipments ORDER BY id')]
    issued={i['shipment_id']:invoices.decode(i) for i in db.execute("SELECT * FROM invoices WHERE status='Issued'")}
    supplier_bills=[payables.summary(db,b) for b in db.execute('SELECT * FROM supplier_bills')]
    currencies={s['terms']['currency'] for s in sources if s['terms']}
    currencies.update(i['snapshot']['currency'] for i in issued.values())
    currencies.update(b['currency'] for b in supplier_bills)
    groups={key:{} for key in ('customer','lane','carrier','month')}
    kpis={};money_moves={};receivable_rows=[];payable_rows=[]
    plans={p['shipment_id']:dict(p) for p in db.execute('SELECT * FROM dispatch_plans')}
    flags={}
    for a in db.execute('SELECT shipment_id,kind FROM alerts'):
        flags.setdefault(a['shipment_id'],set()).add(a['kind'])
    for s in sources:
        currency=s['terms']['currency'] if s['terms'] else None
        inv=issued.get(s['id'])
        credit=adjustments.totals(db,inv['id']) if inv else dict(credit_net=Decimal(0),credited=Decimal(0),refunded=Decimal(0))
        kpi_currency=currency or (inv['snapshot']['currency'] if inv else None)
        if included(s['created'],currency,f):
            basis='Approved' if s['approval_valid'] else 'Provisional'
            dims=dict(customer=s['customer'],lane=(s['origin'],s['destination']),carrier=s['carrier'] or 'Unassigned',month=(calendar_day(s['created']).strftime('%Y-%m') if calendar_day(s['created']) else 'Unknown'))
            for dimension,value in dims.items():
                r=groups[dimension].setdefault((basis,value,currency or 'Unconfigured'),dict(basis=basis,group=(' → '.join(value) if dimension=='lane' else value),currency=currency or 'Unconfigured',shipments=0,revenue=Decimal(0),actual_cost=Decimal(0),unknown=False))
                r['shipments']+=1;r['actual_cost']+=Decimal(s['totals']['actual_cost'])
                if s['totals']['revenue'] is None or (inv and credit['credited'] and inv['snapshot']['currency']!=currency):r['unknown']=True
                else:r['revenue']+=Decimal(s['totals']['revenue'])-credit['credit_net']
        if not included(s['created'],kpi_currency,f):continue
        k=kpis.setdefault(kpi_currency or 'Unconfigured',dict(shipments=0,delivered=0,delivery_samples=0,missing_delivery_time=0,on_time=0,exceptions=0,delays=0,exceptions_or_delays=0,issue_days=[],paid_days=[]))
        k['shipments']+=1;p=plans.get(s['id'],{});delivered=instant(p.get('delivered_at'));eta=instant(s['eta'])
        if s['status']=='Delivered':
            k['delivered']+=1
            if delivered and eta:
                k['delivery_samples']+=1;k['on_time']+=int(delivered<=eta)
            else:k['missing_delivery_time']+=1
        history=flags.get(s['id'],set());exception=s['status']=='Exception' or 'exception' in history
        delay='overdue' in history or bool(eta and ((delivered and delivered>eta) or (s['status']!='Delivered' and now>eta)))
        k['exceptions']+=int(exception);k['delays']+=int(delay);k['exceptions_or_delays']+=int(exception or delay)
        if inv:
            issued_at=instant(inv['issued_at'])
            if delivered and issued_at and issued_at>=delivered:
                k['issue_days'].append(Decimal(str((issued_at-delivered).total_seconds()))/Decimal(86400))
            entries=[dict(p) for p in db.execute('SELECT * FROM payments WHERE invoice_id=? AND reversed_at IS NULL ORDER BY payment_date,created,id',(inv['id'],))]
            total=Decimal(inv['snapshot']['total']);running=Decimal(0);settled=None
            for entry in entries:
                running+=Decimal(entry['amount'])
                if running>=total and total>0:settled=calendar_day(entry['payment_date']);break
            issued_day=calendar_day(inv['snapshot']['issue_date'])
            if settled and issued_day and settled>=issued_day and not credit['credited'] and not credit['refunded']:k['paid_days'].append(Decimal((settled-issued_day).days))

    for row in db.execute("SELECT * FROM invoices WHERE status='Issued'"):
        i=invoices.decode(row);snapshot=i['snapshot'];s=payments.summary(db,row)
        if included(snapshot['issue_date'],s['currency'],f) and Decimal(s['balance'])>0:
            receivable_rows.append((s['customer'],s['currency'],Decimal(s['balance']),s['overdue_days']))
    for b in supplier_bills:
        if b['approval_valid'] and included(b['bill_date'],b['currency'],f) and Decimal(b['balance'])>0:
            payable_rows.append((b['supplier'],b['currency'],Decimal(b['balance']),(today-date.fromisoformat(b['due_date'])).days))
    for row in db.execute('SELECT amount,currency,payment_date FROM payments WHERE reversed_at IS NULL'):
        if included(row['payment_date'],row['currency'],f):
            r=money_moves.setdefault(row['currency'],dict(received=Decimal(0),supplier_paid=Decimal(0)));r['received']+=Decimal(row['amount'])
    for row in db.execute('SELECT p.amount,b.currency,p.payment_date FROM supplier_payments p JOIN supplier_bills b ON b.id=p.bill_id WHERE p.reversed_at IS NULL'):
        if included(row['payment_date'],row['currency'],f):
            r=money_moves.setdefault(row['currency'],dict(received=Decimal(0),supplier_paid=Decimal(0)));r['supplier_paid']+=Decimal(row['amount'])
    refund_moves={}
    for row in db.execute('SELECT r.amount,r.refund_date,i.snapshot FROM customer_refunds r JOIN invoices i ON i.id=r.invoice_id WHERE r.reversed_at IS NULL'):
        currency=json.loads(row['snapshot'])['currency']
        if included(row['refund_date'],currency,f):
            refund_moves[currency]=refund_moves.get(currency,Decimal(0))+Decimal(row['amount'])
            money_moves.setdefault(currency,dict(received=Decimal(0),supplier_paid=Decimal(0)))
    tables={}
    for dimension,buckets in groups.items():
        rows=[]
        for _,r in sorted(buckets.items()):
            profit=None if r['unknown'] else r['revenue']-r['actual_cost']
            rows.append(dict(basis=r['basis'],group=r['group'],currency=r['currency'],shipments=r['shipments'],
                revenue=None if r['unknown'] else cash(r['revenue']),actual_cost=cash(r['actual_cost']),profit=cash(profit) if profit is not None else None,
                margin=cash(profit/r['revenue']*100) if profit is not None and r['revenue'] else None))
        tables['profit_'+dimension]=table('Profit by '+{'lane':'route / lane','month':'shipment creation month'}.get(dimension,dimension),PROFIT_COLUMNS,rows)
    columns=[('party','Customer'),('currency','Currency'),('documents','Open invoices'),*AGE_COLUMNS,('total','Outstanding')]
    tables['receivables']=table('Receivables aging',columns,aging(receivable_rows,'customer'))
    columns=[('party','Supplier'),('currency','Currency'),('documents','Approved open bills'),*AGE_COLUMNS,('total','Outstanding')]
    tables['payables']=table('Payables aging · current approvals',columns,aging(payable_rows,'supplier'))
    tables['cash']=table('Recorded net cash movement',[('currency','Currency'),('received','Customer receipts'),('supplier_paid','Supplier payments'),('net','Net movement')],
        [dict(currency=c,received=cash(r['received']),supplier_paid=cash(r['supplier_paid']),net=cash(r['received']-r['supplier_paid']-refund_moves.get(c,Decimal(0)))) for c,r in sorted(money_moves.items())])
    tables['refunds']=table('Customer refund outflows',[('currency','Currency'),('refunded','Customer refunds')],[dict(currency=c,refunded=cash(v)) for c,v in sorted(refund_moves.items())])
    kpi_rows=[]
    for currency,k in sorted(kpis.items()):
        issue_days=k.pop('issue_days');paid_days=k.pop('paid_days')
        kpi_rows.append(dict(currency=currency,**k,on_time_percent=cash(Decimal(k['on_time'])/k['delivery_samples']*100) if k['delivery_samples'] else None,
            delivery_invoice_samples=len(issue_days),delivery_invoice_days=average(issue_days),fully_paid_samples=len(paid_days),invoice_paid_days=average(paid_days)))
    tables['operations']=table('Operations KPIs · shipment creation cohort',KPI_COLUMNS,kpi_rows)
    return dict(filters=f,as_of=today.isoformat(),generated_at=now.isoformat(),currencies=sorted(currencies),tables=tables)


def export(db,user,params,table_name):
    data=state(db,user,params)
    if table_name not in data['tables']:raise ValueError('Choose a report table.')
    t=data['tables'][table_name];out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow([c['label'] for c in t['columns']])
    for row in t['rows']:
        values=[]
        for column in t['columns']:
            value=row.get(column['key']);value='' if value is None else str(value)
            # Text supplied by customers/carriers must not become spreadsheet formulas.
            if column['key'] in ('group','party','basis','currency') and value.lstrip().startswith(('=','+','-','@')):value="'"+value
            values.append(value)
        writer.writerow(values)
    return ('\ufeff'+out.getvalue()).encode('utf-8'),f'report-{table_name}-{data["as_of"]}.csv'
