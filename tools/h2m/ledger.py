"""SQLite record of every variant ever scored: nothing is tried twice, and
mutator success rates per diff class come from here."""
import hashlib, json, os, sqlite3, threading, time
from . import objcmp, project

_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(project.DB), exist_ok=True)
        _db = sqlite3.connect(project.DB, check_same_thread=False, timeout=60)
        _db.execute('pragma journal_mode=wal')
        _db.execute('''create table if not exists variant(
            va integer, hash text, parent text, origin text, note text, t real,
            ok integer, match integer, lock integer, dist integer, sem integer, frame integer, first integer,
            error text, path text, primary key (va, hash))''')
        _db.execute('create table if not exists fact(va integer, key text, value text, t real, primary key (va, key))')
    return _db


def digest(text):
    # salted by objcmp's TU mode: scores of the two compile modes never share a cache entry
    return hashlib.sha1((objcmp.TU_SALT + text).encode('utf-8', 'replace')).hexdigest()[:16]


def get(va, h):
    with _lock:
        r = db().execute('select ok, match, lock, dist, sem, frame, first, error from variant where va=? and hash=?',
                         (va, h)).fetchone()
    if not r:
        return None
    if not r[0]:
        return dict(error=r[7] or 'compile failed')
    return dict(match=r[1], lock=r[2], dist=r[3], sem=r[4], frame=r[5], first=r[6])


def put(va, h, v, parent=None, origin='', note='', path=None):
    with _lock:
        db().execute('insert or replace into variant values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (va, h, parent, origin, note, time.time(), 0 if 'error' in v else 1, v.get('match'),
                      v.get('lock'), v.get('dist'), v.get('sem'), v.get('frame'), v.get('first'),
                      (v.get('error') or '')[:2000] or None, path))
        db().commit()


def history(va, limit=40):
    with _lock:
        return db().execute('select hash, origin, note, match, lock, dist, sem, frame, path, t from variant '
                            'where va=? and ok=1 order by match desc, dist, lock limit ?', (va, limit)).fetchall()


def stats(va=None):
    q = 'select origin, count(*), sum(ok), min(dist), min(lock) from variant'
    with _lock:
        return db().execute(q + (' where va=?' if va else '') + ' group by origin', (va,) if va else ()).fetchall()


def set_fact(va, key, value):
    with _lock:
        db().execute('insert or replace into fact values (?,?,?,?)', (va, key, json.dumps(value), time.time()))
        db().commit()


def fact(va, key, default=None):
    with _lock:
        r = db().execute('select value from fact where va=? and key=?', (va, key)).fetchone()
    return json.loads(r[0]) if r else default
