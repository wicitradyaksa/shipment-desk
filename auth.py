"""Staff accounts and expiring sessions. No external dependencies."""
import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie

ROLES = ('Admin', 'Sales', 'Operations', 'Viewer')


class AccessError(Exception):
    def __init__(self, message, code=403):
        super().__init__(message)
        self.code = code


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
      role TEXT NOT NULL, password TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS login_attempts (
      username TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started REAL NOT NULL);
    ''')


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Use a password of 12 to 256 characters.')
    salt = secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()


def verify(password, stored):
    if not isinstance(password, str) or len(password) > 256:
        return False
    salt, digest = stored.split(':')
    return hmac.compare_digest(digest, hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex())


def public(user):
    return {key: user[key] for key in ('id', 'username', 'name', 'role', 'active')}


def add_user(db, data, setup=False):
    username = str(data.get('username', '')).strip().lower()
    name = str(data.get('name', '')).strip()
    role = 'Admin' if setup else data.get('role')
    if not username or len(username) > 80 or not all(c.isascii() and (c.isalnum() or c in '._-') for c in username):
        raise ValueError('Username must use 1–80 letters, numbers, dots, dashes, or underscores.')
    if not name or len(name) > 100 or role not in ROLES:
        raise ValueError('Provide a name and a valid staff role.')
    db.execute('INSERT INTO users(username,name,role,password) VALUES (?,?,?,?)',
               (username, name, role, password_hash(data.get('password'))))


def token_from(headers):
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get('Cookie', ''))
        token = cookie.get('shipment_session')
        return hashlib.sha256(token.value.encode()).hexdigest() if token else ''
    except Exception:
        return ''


def current(db, headers):
    user = db.execute('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=? AND s.expires>? AND u.active=1',
                      (token_from(headers), time.time())).fetchone()
    return public(user) if user else None


def require(user, roles=ROLES):
    if not user:
        raise AccessError('Please sign in.', 401)
    if user['role'] not in roles:
        raise AccessError('Your role does not allow this action.')


def login(db, data):
    username = str(data.get('username', '')).strip().lower()[:80]
    stamp = time.time()
    attempt = db.execute('SELECT * FROM login_attempts WHERE username=?', (username,)).fetchone()
    if attempt and stamp-attempt['started'] < 900 and attempt['attempts'] >= 10:
        return None, 'Too many attempts. Try again in 15 minutes.'
    user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    valid = verify(data.get('password', ''), user['password'] if user else '0'*32+':'+'0'*128)
    if not user or not user['active'] or not valid:
        if not attempt or stamp-attempt['started'] >= 900:
            db.execute('INSERT OR REPLACE INTO login_attempts VALUES (?,1,?)', (username, stamp))
        else:
            db.execute('UPDATE login_attempts SET attempts=attempts+1 WHERE username=?', (username,))
        return None, 'Incorrect username or password.'
    db.execute('DELETE FROM login_attempts WHERE username=?', (username,))
    db.execute('DELETE FROM sessions WHERE expires<?', (stamp,))
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], stamp+28800))
    return token, None
