"""Shared customer and carrier address book. Copies never change old records."""
import uuid
import auth
import dispatch


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS business_profiles(id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,name_key TEXT NOT NULL,
      contact TEXT NOT NULL,email TEXT NOT NULL,phone TEXT NOT NULL,address TEXT NOT NULL,tax_id TEXT NOT NULL,
      origin TEXT NOT NULL,destination TEXT NOT NULL,instructions TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
      version INTEGER NOT NULL DEFAULT 1,UNIQUE(kind,name_key));
    CREATE TABLE IF NOT EXISTS profile_history(id INTEGER PRIMARY KEY,profile_id TEXT REFERENCES business_profiles(id),
      time TEXT NOT NULL,actor TEXT NOT NULL,message TEXT NOT NULL);
    ''')


def state(db,user):
    auth.require(user,('Admin','Sales','Operations'))
    return dict(profiles=[dict(r) for r in db.execute('SELECT * FROM business_profiles ORDER BY kind,name')],
                history=[dict(r) for r in db.execute('SELECT * FROM profile_history ORDER BY id DESC LIMIT 200')])


def action(db,path,data,user):
    auth.require(user,('Admin','Sales','Operations'));db.execute('BEGIN IMMEDIATE')
    pid=data.get('id');old=db.execute('SELECT * FROM business_profiles WHERE id=?',(pid,)).fetchone() if pid else None
    if pid and not old:raise ValueError('Profile not found.')
    kind=old['kind'] if old else data.get('kind')
    if kind not in ('Customer','Carrier'):raise ValueError('Choose Customer or Carrier.')
    auth.require(user,('Admin','Sales') if kind=='Customer' else ('Admin','Operations'))
    if old and data.get('version')!=old['version']:raise auth.AccessError('Profile changed. Refresh first.',409)
    op=path.rsplit('/',1)[-1]
    if op=='archive':
        if not old:raise ValueError('Choose a saved profile.')
        reason=dispatch.clean(data,'reason',True,1500)
        active=data.get('active')
        if type(active)!=bool:raise ValueError('Choose archive or restore.')
        db.execute('UPDATE business_profiles SET active=?,version=version+1 WHERE id=?',(int(active),pid))
        message=('Restored: ' if active else 'Archived: ')+reason
    elif op=='save':
        fields={k:dispatch.clean(data,k,k=='name',300 if k in ('name','contact','email','phone','tax_id','origin','destination') else 1500)
                for k in ('name','contact','email','phone','address','tax_id','origin','destination','instructions')}
        if fields['email'] and ('@' not in fields['email'] or any(c.isspace() for c in fields['email'])):raise ValueError('Enter a valid email address.')
        key=' '.join(fields['name'].casefold().split())
        if db.execute('SELECT 1 FROM business_profiles WHERE kind=? AND name_key=? AND id!=?',(kind,key,pid or '')).fetchone():raise ValueError('A profile with that name already exists, including archived profiles.')
        if old:
            db.execute('UPDATE business_profiles SET '+','.join(k+'=?' for k in fields)+',name_key=?,version=version+1 WHERE id=?',(*fields.values(),key,pid))
        else:
            pid=str(uuid.uuid4())
            db.execute('INSERT INTO business_profiles(id,kind,name_key,'+','.join(fields)+') VALUES ('+','.join('?' for _ in range(len(fields)+3))+')',(pid,kind,key,*fields.values()))
        message='Profile saved. '+('; '.join(k+': '+str(old[k])+' → '+v for k,v in fields.items() if old and old[k]!=v) if old else fields['name'])
    else:raise ValueError('Unknown profile action.')
    db.execute('INSERT INTO profile_history(profile_id,time,actor,message) VALUES (?,?,?,?)',(pid,dispatch.now(),user['name'],message))
    return dict(ok=True,id=pid)
