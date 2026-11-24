"""Member keys and the shared daily budget for answers Scout pays for.

Two kinds of caller:

  anonymous  no key. FREE_ANSWERS_PER_DAY answers a day each, per IP
             (default 500). ANON_ANSWERS_PER_DAY can add a pool shared by all
             anonymous callers; it is off by default (0).
  member     an `X-Scout-Key: sk_...` header (or `scout_key` in an MCP call).
             No per-IP or per-day count, only a per-minute pace that stops a
             runaway loop from burning the budget in seconds.

Above both sits a wallet guard: when Scout's own spend in the last 24 hours
reaches WALLET_DAILY_FLOP, Scout stops paying for inference until the window
rolls. Search, page reading and quotes keep working.

Keys are stored as SHA-256 hashes; the plaintext is shown once, when issued.
Issue, list and revoke with `python members.py ...` on the server.
"""

import hashlib
import os
import secrets
import sqlite3
import sys
import time

ANON_DAILY = int(os.getenv('ANON_ANSWERS_PER_DAY', '0'))      # 0 = no shared pool
MEMBER_PER_MIN = int(os.getenv('MEMBER_ANSWERS_PER_MIN', '30'))
WALLET_DAILY_FLOP = float(os.getenv('WALLET_DAILY_FLOP', '10000'))
PREFIX = 'sk_'

SCHEMA = '''
create table if not exists members(
    key_hash text primary key,
    label text, created_at real, revoked_at real, last_used_at real,
    answers integer default 0, spent_flop real default 0)
'''


def _hash(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def init(db, lock) -> None:
    with lock:
        db.execute(SCHEMA)
        db.commit()


def issue(db, lock, label: str) -> str:
    key = PREFIX + secrets.token_urlsafe(24)
    with lock:
        db.execute('insert into members(key_hash, label, created_at) values (?, ?, ?)',
                   (_hash(key), label.strip()[:80], time.time()))
        db.commit()
    return key


def lookup(db, lock, key: str | None) -> dict | None:
    """The member a key belongs to, or None for no key, a bad key or a revoked one."""
    if not key or not key.startswith(PREFIX) or len(key) > 80:
        return None
    with lock:
        r = db.execute('select key_hash, label, revoked_at from members where key_hash = ?',
                       (_hash(key),)).fetchone()
    if not r or r[2]:
        return None
    return {'id': r[0][:12], 'hash': r[0], 'label': r[1]}


def record(db, lock, member: dict | None, spent_flop: float | None) -> None:
    if not member:
        return
    with lock:
        db.execute('update members set answers = answers + 1, spent_flop = spent_flop + ?, last_used_at = ? '
                   'where key_hash = ?', (float(spent_flop or 0), time.time(), member['hash']))
        db.commit()


def revoke(db, lock, prefix: str) -> int:
    with lock:
        n = db.execute('update members set revoked_at = ? where key_hash like ? and revoked_at is null',
                       (time.time(), prefix + '%')).rowcount
        db.commit()
    return n


def listing(db, lock) -> list:
    with lock:
        rows = db.execute('select key_hash, label, created_at, revoked_at, last_used_at, answers, spent_flop '
                          'from members order by created_at').fetchall()
    return [{'id': h[:12], 'label': l, 'created_at': c, 'revoked': bool(r), 'last_used_at': u,
             'answers': a, 'spent_flop': round(s or 0, 6)} for h, l, c, r, u, a, s in rows]


def scout_spend_24h(db, lock) -> float:
    """FLOP Scout's own wallet paid miners in the last 24 hours, from the answer log."""
    with lock:
        v = db.execute("select coalesce(sum(cast(json_extract(cost, '$') as real)), 0) from answers "
                       "where ts >= ? and paid_by = 'scout' and engine = 'flop'",
                       (time.time() - 86400,)).fetchone()[0]
    return float(v or 0)


def anon_answers_24h(db, lock) -> int:
    """Answers Scout produced for anonymous callers in the last 24 hours, across everyone."""
    with lock:
        return db.execute("select count(*) from answers where ts >= ? and ok = 1 and paid_by = 'scout' "
                          "and (member is null or member = '')", (time.time() - 86400,)).fetchone()[0]


def _cli() -> None:
    data = os.getenv('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
    db = sqlite3.connect(os.path.join(data, 'log.db'))
    import threading
    lock = threading.Lock()
    init(db, lock)
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    if cmd == 'issue' and len(sys.argv) > 2:
        key = issue(db, lock, ' '.join(sys.argv[2:]))
        print(f'{key}\n(shown once; store it now)')
    elif cmd == 'revoke' and len(sys.argv) > 2:
        print(f'revoked {revoke(db, lock, sys.argv[2])}')
    elif cmd == 'list':
        for m in listing(db, lock):
            used = time.strftime('%Y-%m-%d %H:%M', time.gmtime(m['last_used_at'])) if m['last_used_at'] else '-'
            print(f"{m['id']}  {'REVOKED ' if m['revoked'] else ''}{m['label']:<24} answers={m['answers']:<6} "
                  f"spent={m['spent_flop']:<10} last={used}")
    else:
        print('usage: python members.py issue <label> | revoke <id-prefix> | list')


if __name__ == '__main__':
    _cli()
