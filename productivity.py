"""Persisted workflow tasks, business-time targets and auditable follow-ups."""
import json
from datetime import datetime, timezone, timedelta
import auth
import dispatch

TEAMS = ('Sales', 'Operations', 'Admin')
WAITING = ('Working', 'Waiting for client', 'Waiting for carrier', 'Waiting internally')
DEFAULT = dict(version=0, offset_minutes=480, start_hour=9, end_hour=18, weekdays=[0,1,2,3,4],
               targets={t:dict(hours=None,on_time_percent=None) for t in TEAMS})


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS productivity_config(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS work_tasks(
      id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, generation INTEGER NOT NULL,
      view TEXT NOT NULL, record_id TEXT NOT NULL, title TEXT NOT NULL, reference TEXT NOT NULL, team TEXT NOT NULL,
      owner_id INTEGER REFERENCES users(id), version INTEGER NOT NULL DEFAULT 1,
      opened TEXT NOT NULL, completed TEXT, due TEXT, calendar TEXT NOT NULL,
      waiting TEXT NOT NULL DEFAULT 'Working', waiting_since TEXT, wait_seconds REAL NOT NULL DEFAULT 0,
      followup TEXT, note TEXT NOT NULL DEFAULT '', UNIQUE(source_key,generation));
    CREATE UNIQUE INDEX IF NOT EXISTS work_one_open ON work_tasks(source_key) WHERE completed IS NULL;
    CREATE TABLE IF NOT EXISTS work_history(id INTEGER PRIMARY KEY,task_id INTEGER REFERENCES work_tasks(id),time TEXT NOT NULL,actor TEXT NOT NULL,message TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS productivity_history(id INTEGER PRIMARY KEY,time TEXT NOT NULL,actor TEXT NOT NULL,body TEXT NOT NULL);
    ''')


def config(db):
    row=db.execute('SELECT * FROM productivity_config WHERE id=1').fetchone()
    return dict(json.loads(row['body']),version=row['version']) if row else json.loads(json.dumps(DEFAULT))


def moment(value):
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        if d.tzinfo is None: raise ValueError()
        return d.astimezone(timezone.utc)
    except (ValueError,TypeError,AttributeError): raise ValueError('Enter a date and time with a timezone.')


def business_seconds(start,end,c):
    zone=timezone(timedelta(minutes=c['offset_minutes']))
    a,b=moment(start).astimezone(zone),moment(end).astimezone(zone)
    if b<=a:return 0
    day=a.replace(hour=0,minute=0,second=0,microsecond=0);total=0
    while day<b:
        if day.weekday() in c['weekdays']:
            lo=max(a,day+timedelta(hours=c['start_hour']));hi=min(b,day+timedelta(hours=c['end_hour']))
            total+=max(0,(hi-lo).total_seconds())
        day+=timedelta(days=1)
    return total


def deadline(start,hours,c):
    if hours is None:return None
    zone=timezone(timedelta(minutes=c['offset_minutes']));cursor=moment(start).astimezone(zone);remaining=hours*3600
    while remaining>0:
        day=cursor.replace(hour=0,minute=0,second=0,microsecond=0)
        lo=max(cursor,day+timedelta(hours=c['start_hour']));hi=day+timedelta(hours=c['end_hour'])
        if day.weekday() in c['weekdays'] and hi>lo:
            span=(hi-lo).total_seconds()
            if span>=remaining:return (lo+timedelta(seconds=remaining)).astimezone(timezone.utc).isoformat()
            remaining-=span
        cursor=day+timedelta(days=1)


def audit(db,tid,actor,message):
    db.execute('INSERT INTO work_history(task_id,time,actor,message) VALUES (?,?,?,?)',(tid,dispatch.now(),actor,message))


def sync(db,items):
    now=dispatch.now();c=config(db);active={i['id']:i for i in items}
    rows={r['source_key']:dict(r) for r in db.execute('SELECT * FROM work_tasks WHERE completed IS NULL')}
    users={u['id']:dict(u) for u in db.execute('SELECT id,role,active FROM users')}
    for key,r in rows.items():
        if key not in active:
            waited=r['wait_seconds']+(business_seconds(r['waiting_since'],now,json.loads(r['calendar'])) if r['waiting_since'] else 0)
            db.execute('UPDATE work_tasks SET completed=?,wait_seconds=?,waiting_since=NULL,version=version+1 WHERE id=?',(now,waited,r['id']))
            audit(db,r['id'],'System','Source step completed or no longer requires action.')
        elif r['owner_id'] is not None and (not users.get(r['owner_id'],{}).get('active') or users[r['owner_id']]['role'] not in (r['team'],'Admin')):
            db.execute('UPDATE work_tasks SET owner_id=NULL,version=version+1 WHERE id=?',(r['id'],))
            audit(db,r['id'],'System','Owner access changed; returned to shared queue.')
    for key,i in active.items():
        person=users.get(i['owner_id'])
        if not person or not person['active'] or person['role'] not in (i['team'],'Admin'):
            i=dict(i,owner_id=None)
        if key in rows and i['team']=='Sales' and rows[key]['owner_id']!=i['owner_id']:
            db.execute('UPDATE work_tasks SET owner_id=?,version=version+1 WHERE id=?',(i['owner_id'],rows[key]['id']))
            audit(db,rows[key]['id'],'System','Task ownership follows the Sales opportunity owner.')
        if key not in rows:
            generation=db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM work_tasks WHERE source_key=?',(key,)).fetchone()[0]
            due=deadline(now,c['targets'][i['team']]['hours'],c)
            tid=db.execute('INSERT INTO work_tasks(source_key,generation,view,record_id,title,reference,team,owner_id,opened,due,calendar) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (key,generation,i['view'],i['record_id'],i['title'],i['reference'],i['team'],i['owner_id'],now,due,json.dumps(c))).lastrowid
            audit(db,tid,'System','Task opened from workflow; timer starts when first observed.')


def enrich(db,result,user):
    c=config(db);now=dispatch.now()
    staff=[dict(r) for r in db.execute('SELECT id,name,role FROM users WHERE active=1 AND role!=? ORDER BY name',('Viewer',))]
    names={r['id']:r['name'] for r in staff}
    records={r['source_key']:dict(r) for r in db.execute('SELECT * FROM work_tasks WHERE completed IS NULL')}
    for i in result['items']:
        r=records.get(i['id'])
        if not r:continue
        i['source_owner_id']=i['owner_id'];i['owner_id']=r['owner_id'];i['owner_name']=names.get(r['owner_id'],'Shared team queue')
        i['task']=r
        i['task_overdue']=bool(r['due'] and moment(r['due'])<moment(now))
        i['followup_due']=bool(r['followup'] and moment(r['followup'])<=moment(now))
        i['urgent']=i['urgent'] or i['task_overdue'] or i['followup_due']
    cutoff=(moment(now)-timedelta(days=30)).isoformat()
    history=[dict(r) for r in db.execute('SELECT * FROM work_tasks WHERE completed>=? ORDER BY completed DESC',(cutoff,))
             if user['role']=='Admin' or r['owner_id']==user['id']]
    scores=[]
    for team in TEAMS:
        if user['role'] not in ('Admin',team):continue
        done=[r for r in history if r['team']==team];timed=[r for r in done if r['due']]
        durations=[max(0,business_seconds(r['opened'],r['completed'],json.loads(r['calendar']))-r['wait_seconds'])/3600 for r in done]
        scores.append(dict(team=team,completed=len(done),timed=len(timed),
            on_time_percent=round(100*sum(moment(r['completed'])<=moment(r['due']) for r in timed)/len(timed),1) if timed else None,
            active_hours=round(sum(durations)/len(durations),2) if durations else None,
            waiting_hours=round(sum(r['wait_seconds'] for r in done)/3600,2),target=c['targets'][team]['on_time_percent']))
    visible_ids={i['task']['id'] for i in result['items'] if 'task' in i}
    task_history=[dict(h) for h in db.execute('SELECT * FROM work_history ORDER BY id DESC') if h['task_id'] in visible_ids]
    staff_scores=[]
    if user['role']=='Admin':
        for person in staff:
            done=[r for r in history if r['owner_id']==person['id']];timed=[r for r in done if r['due']]
            pending=[i for i in result['items'] if i['owner_id']==person['id']]
            staff_scores.append(dict(name=person['name'],role=person['role'],open=len(pending),overdue=sum(i['task_overdue'] for i in pending),
                completed=len(done),on_time_percent=round(100*sum(moment(r['completed'])<=moment(r['due']) for r in timed)/len(timed),1) if timed else None))
    result.update(staff=staff,targets=c,scores=scores,staff_scores=staff_scores,task_history=task_history)
    result['items'].sort(key=lambda i:(not i['urgent'],i['task'].get('followup') or i['task'].get('due') or i.get('due') or '9999',i['id']))
    return result


def action(db,path,data,user,visible):
    auth.require(user,TEAMS)
    if path.endswith('/settings'):
        auth.require(user,('Admin',));old=config(db)
        if data.get('version')!=old['version']:raise auth.AccessError('Targets changed. Refresh first.',409)
        try:
            start=int(data['start_hour']);end=int(data['end_hour']);offset=int(data['offset_minutes']);days=data['weekdays']
            if not 0<=start<end<=24 or not -720<=offset<=840 or not isinstance(days,list) or not days or any(type(d)!=int or not 0<=d<=6 for d in days):raise ValueError()
            targets={}
            for team in TEAMS:
                t=data['targets'][team];h=None if t['hours'] in ('',None) else float(t['hours']);p=None if t['on_time_percent'] in ('',None) else float(t['on_time_percent'])
                if h is not None and not 0<h<=720 or p is not None and not 0<=p<=100:raise ValueError()
                targets[team]=dict(hours=h,on_time_percent=p)
        except (KeyError,TypeError,ValueError,OverflowError):raise ValueError('Check working hours, weekdays, timezone offset and targets (up to 720 hours; 0–100%).')
        c=dict(start_hour=start,end_hour=end,offset_minutes=offset,weekdays=sorted(set(days)),targets=targets)
        db.execute('INSERT INTO productivity_config VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,body=excluded.body',(old['version']+1,json.dumps(c)))
        db.execute('INSERT INTO productivity_history(time,actor,body) VALUES (?,?,?)',(dispatch.now(),user['name'],json.dumps(c)))
        return {'ok':True}
    item=next((i for i in visible['items'] if i.get('task',{}).get('id')==data.get('task_id')),None)
    if not item:raise auth.AccessError('This task is complete or is not available to your role.',409)
    r=item['task'];op=path.rsplit('/',1)[-1]
    if data.get('version')!=r['version']:raise auth.AccessError('Task changed. Refresh before saving.',409)
    if op=='claim':
        if r['owner_id'] is not None:raise auth.AccessError('This task already has an owner.',409)
        if user['role'] not in ('Admin',r['team']):raise auth.AccessError('Claim a task for your own team.')
        owner=user['id'];db.execute('UPDATE work_tasks SET owner_id=? WHERE id=?',(owner,r['id']));message='Task claimed.'
    elif op=='assign':
        auth.require(user,('Admin',));owner=data.get('owner_id')
        person=db.execute('SELECT * FROM users WHERE id=? AND active=1',(owner,)).fetchone() if owner is not None else None
        if owner is not None and (not person or person['role'] not in (r['team'],'Admin')):raise ValueError('Choose an active member of the responsible team.')
        # Sales access follows the opportunity owner, so transferring this task also transfers the opportunity.
        if r['team']=='Sales' and owner is not None:
            q=db.execute('SELECT * FROM quotes WHERE '+('id' if r['view']=='quotes' else 'shipment_id')+'=?',(r['record_id'],)).fetchone()
            if q:
                db.execute('UPDATE quotes SET owner_id=?,version=version+1 WHERE id=?',(owner,q['id']))
                db.execute('INSERT INTO quote_events(quote_id,time,actor,message) VALUES (?,?,?,?)',(q['id'],dispatch.now(),user['name'],'Sales ownership transferred with work assignment.'))
        db.execute('UPDATE work_tasks SET owner_id=? WHERE id=?',(owner,r['id']));message='Task reassigned to '+(person['name'] if person else 'shared queue')+'.'
    elif op=='followup':
        if user['role']!='Admin' and r['owner_id']!=user['id']:raise auth.AccessError('Claim this task before recording follow-up.')
        waiting=data.get('waiting');note=dispatch.clean(data,'note',True,1500)
        if waiting not in WAITING:raise ValueError('Choose a waiting status.')
        followup=data.get('followup') or None
        if followup:
            followup=moment(followup).isoformat()
            if not moment(dispatch.now())<moment(followup)<=moment(dispatch.now())+timedelta(days=366):raise ValueError('Follow-up must be in the next 366 days.')
        if waiting!='Working' and not followup:raise ValueError('Waiting tasks require a next follow-up time.')
        waited=r['wait_seconds']+(business_seconds(r['waiting_since'],dispatch.now(),json.loads(r['calendar'])) if r['waiting_since'] else 0)
        db.execute('UPDATE work_tasks SET waiting=?,waiting_since=?,wait_seconds=?,followup=?,note=? WHERE id=?',
            (waiting,dispatch.now() if waiting!='Working' else None,waited,followup,note,r['id']))
        message=f'{waiting}; next follow-up: {followup or "none"}. {note}'
    elif op=='deadline':
        auth.require(user,('Admin',));reason=dispatch.clean(data,'note',True,1500);due=moment(data.get('due')).isoformat()
        if not moment(r['opened'])<moment(due)<=moment(dispatch.now())+timedelta(days=366):raise ValueError('Deadline must follow task creation and be within the next 366 days.')
        db.execute('UPDATE work_tasks SET due=? WHERE id=?',(due,r['id']));message=f'Deadline changed from {r["due"] or "unconfigured"} to {due}. {reason}'
    else:raise ValueError('Unknown work action.')
    db.execute('UPDATE work_tasks SET version=version+1 WHERE id=?',(r['id'],));audit(db,r['id'],user['name'],message)
    return {'ok':True}
