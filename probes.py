"""Scout's own probes of FLOP inference: the measurements nobody else has.

Everything else Scout shows about miners can be rebuilt by anyone from the
explorer (see sessions.py). What cannot be rebuilt without spending is what a
real request experiences: how long it took, whether the answer was right, what
it cost, and which miner and channel served it. This module buys a small
number of real completions on a schedule, checks each answer against a known
result, and records the outcome. The rows are the independent half of the
ranking.

Status: disabled until the testnet. The devnet key Scout holds is expired, and
the airdrop terms credit only compute purchased in settled testnet sessions.

How it stays honest (see PROBE_POLICY, served at /api/probes):
  - Every probe is a real request with a checkable answer, sized to measure
    something. It is never sent to inflate volume.
  - Scout cannot choose the miner: the gateway assigns one. Probes therefore
    measure the network as a buyer meets it, and per-miner figures accumulate
    only from the miners the gateway happens to pick.
  - Scout runs no miner and probes from one wallet. Spend routed to a miner
    under common control is not credited, and neither is spend split across
    wallets, so there is nothing to gain from either.
  - Hard caps on rate and daily spend, enforced here, not by convention.
  - Every result is published, including failures.
"""

import asyncio
import os
import random
import re
import time

import httpx

ENABLED = os.getenv('PROBES_ENABLED', '0') == '1'
INTERVAL_S = max(600, int(os.getenv('PROBE_INTERVAL_S', '3600')))      # >= 10 min apart
MAX_PER_DAY = max(1, int(os.getenv('PROBE_MAX_PER_DAY', '48')))
MAX_COST_FLOP = float(os.getenv('PROBE_MAX_COST_FLOP', '5'))             # per request, passed to the gateway
DAILY_BUDGET_FLOP = float(os.getenv('PROBE_DAILY_BUDGET_FLOP', '100'))
MAX_WAIT_MS = 120_000

PROBE_POLICY = {
    'purpose': 'Measure what a buyer of FLOP inference experiences: latency, correctness, cost and the '
               'serving miner. Results feed the public miner ranking.',
    'status': 'enabled' if ENABLED else 'disabled until the FLOP testnet opens',
    'requests': 'Real completions with a known, checkable answer. Length varies by task because latency '
                'and failure modes depend on output length; no request is sent to add volume.',
    'miner_choice': 'None. The FLOP gateway assigns the miner; Scout records which one served the request.',
    'caps': {'min_interval_s': INTERVAL_S, 'max_per_day': MAX_PER_DAY,
             'max_cost_per_request_flop': MAX_COST_FLOP, 'daily_budget_flop': DAILY_BUDGET_FLOP},
    'wallets': 'One wallet. Scout operates no miner and does not split activity across wallets.',
    'publication': 'Every probe, failed or not, is listed at GET /api/probes.',
}

SCHEMA = '''
create table if not exists probes(
    id integer primary key autoincrement,
    ts real, task text, model text, ok integer, correct integer,
    latency_s real, ttfb_s real, http_status integer, error text,
    miner text, channel_id text, request_id text,
    prompt_tokens integer, completion_tokens integer, cost_flop real,
    expected text, got text)
'''

# Each task has an answer a regular expression can check. The set covers short
# and long outputs, because a miner that settles short answers and times out on
# long ones looks perfect to anyone probing with one-word prompts.
TASKS = [
    {'id': 'arith', 'max_tokens': 16,
     'make': lambda r: (lambda a, b: (f'What is {a} + {b}? Reply with the number only.', str(a + b)))(
         r.randint(120, 989), r.randint(120, 989))},
    {'id': 'capital', 'max_tokens': 16,
     'make': lambda r: r.choice([('What is the capital of France? One word.', 'Paris'),
                                 ('What is the capital of Japan? One word.', 'Tokyo'),
                                 ('What is the capital of Canada? One word.', 'Ottawa'),
                                 ('What is the capital of Kenya? One word.', 'Nairobi')])},
    {'id': 'reverse', 'max_tokens': 24,
     'make': lambda r: (lambda w: (f'Write the word "{w}" backwards. Reply with that only.', w[::-1]))(
         r.choice(['planet', 'garden', 'silver', 'rocket', 'window']))},
    {'id': 'count_list', 'max_tokens': 220,
     'make': lambda r: (lambda n: (f'List the integers from 1 to {n}, separated by commas, nothing else.',
                                   ', '.join(str(i) for i in range(1, n + 1))))(r.randint(40, 60))},
    {'id': 'long_form', 'max_tokens': 400,
     'make': lambda r: ('Explain in about 150 words why the sky looks blue. End your reply with the exact '
                        'word DONE.', 'DONE')},
]


def init(db, lock) -> None:
    with lock:
        db.execute(SCHEMA)
        db.execute('create index if not exists probes_ts on probes(ts)')
        db.commit()


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def check(task_id: str, expected: str, got: str) -> bool:
    g = got or ''
    if task_id == 'count_list':
        nums = [int(x) for x in re.findall(r'\d+', g)]
        want = [int(x) for x in re.findall(r'\d+', expected)]
        return nums[:len(want)] == want
    if task_id == 'long_form':
        return g.rstrip().rstrip('.!').upper().endswith('DONE') and len(g.split()) >= 60
    return _norm(expected) in _norm(g)


def budget_state(db, lock, now: float | None = None) -> dict:
    now = now or time.time()
    with lock:
        n, spent, last = db.execute('select count(*), coalesce(sum(cost_flop), 0), max(ts) from probes '
                                    'where ts >= ?', (now - 86400,)).fetchone()
    return {'probes_24h': n, 'spent_24h_flop': round(spent or 0, 6), 'last_probe_at': last}


def allowed(db, lock, now: float | None = None) -> tuple[bool, str]:
    if not ENABLED:
        return False, 'probes are disabled (PROBES_ENABLED=0)'
    now = now or time.time()
    b = budget_state(db, lock, now)
    if b['last_probe_at'] and now - b['last_probe_at'] < INTERVAL_S:
        return False, 'interval not elapsed'
    if b['probes_24h'] >= MAX_PER_DAY:
        return False, 'daily probe cap reached'
    if b['spent_24h_flop'] + MAX_COST_FLOP > DAILY_BUDGET_FLOP:
        return False, 'daily budget would be exceeded'
    return True, ''


async def run_probe(c: httpx.AsyncClient, url: str, key: str, model: str, task: dict, rnd: random.Random) -> dict:
    """One probe. Never raises: a failure is a result worth recording."""
    prompt, expected = task['make'](rnd)
    body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': task['max_tokens'], 'temperature': 0,
            'max_cost_flop': MAX_COST_FLOP, 'max_wait_ms': MAX_WAIT_MS}
    rec = {'ts': time.time(), 'task': task['id'], 'model': model, 'expected': expected,
           'ok': 0, 'correct': 0}
    t0 = time.monotonic()
    try:
        async with c.stream('POST', f'{url}/chat/completions', json=body, timeout=MAX_WAIT_MS / 1000 + 15,
                            headers={'Authorization': f'Bearer {key}'}) as r:
            rec['ttfb_s'] = round(time.monotonic() - t0, 3)
            raw = await r.aread()
        rec['latency_s'] = round(time.monotonic() - t0, 3)
        rec['http_status'] = r.status_code
        j = {}
        try:
            j = r.json() if raw else {}
        except ValueError:
            pass
        if r.status_code != 200:
            err = j.get('error') if isinstance(j, dict) else None
            rec['error'] = (err.get('code') or err.get('message')) if isinstance(err, dict) else (
                str(err or raw[:200].decode(errors='replace')))
            return rec
        got = ((j.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        rc, u = j.get('x_flop_receipt') or {}, j.get('usage') or {}
        rec.update(ok=1, got=got[:500], correct=int(check(task['id'], expected, got)),
                   miner=rc.get('miner'), channel_id=rc.get('channel_id') or rc.get('channelId'),
                   request_id=rc.get('request_id') or j.get('id'),
                   prompt_tokens=u.get('prompt_tokens'), completion_tokens=u.get('completion_tokens'),
                   cost_flop=u.get('cost_flop'))
    except httpx.TimeoutException:
        rec.update(latency_s=round(time.monotonic() - t0, 3), error='timeout')
    except httpx.HTTPError as e:
        rec.update(latency_s=round(time.monotonic() - t0, 3), error=type(e).__name__)
    return rec


def save(db, lock, rec: dict) -> None:
    cols = ('ts', 'task', 'model', 'ok', 'correct', 'latency_s', 'ttfb_s', 'http_status', 'error', 'miner',
            'channel_id', 'request_id', 'prompt_tokens', 'completion_tokens', 'cost_flop', 'expected', 'got')
    with lock:
        db.execute(f'insert into probes({",".join(cols)}) values ({",".join("?" * len(cols))})',
                   tuple(rec.get(k) for k in cols))
        db.commit()


def summary(db, lock, window: int = 86400) -> dict:
    """Per-miner and per-model results over the window, from probes alone."""
    start = time.time() - window
    with lock:
        by_miner = db.execute('''select miner, count(*), sum(ok), sum(correct), avg(case when ok=1 then latency_s end),
                                        sum(coalesce(cost_flop, 0))
                                 from probes where ts >= ? and miner is not null group by miner''',
                              (start,)).fetchall()
        by_model = db.execute('''select model, count(*), sum(ok), sum(correct), avg(case when ok=1 then latency_s end)
                                 from probes where ts >= ? group by model''', (start,)).fetchall()
        by_task = db.execute('''select task, count(*), sum(ok), sum(correct) from probes where ts >= ?
                                group by task''', (start,)).fetchall()
        errors = db.execute('''select coalesce(error, 'http ' || http_status), count(*) from probes
                               where ts >= ? and ok = 0 group by 1 order by 2 desc limit 8''', (start,)).fetchall()
    return {
        'window_s': window,
        'miners': [{'miner': m, 'probes': n, 'served': s or 0, 'correct': c or 0,
                    'correct_rate': round((c or 0) / s, 3) if s else None,
                    'avg_latency_s': round(l, 2) if l else None, 'spent_flop': round(f or 0, 6)}
                   for m, n, s, c, l, f in by_miner],
        'models': [{'model': m, 'probes': n, 'served': s or 0, 'correct': c or 0,
                    'success_rate': round((s or 0) / n, 3) if n else None,
                    'avg_latency_s': round(l, 2) if l else None} for m, n, s, c, l in by_model],
        'tasks': [{'task': t, 'probes': n, 'served': s or 0, 'correct': c or 0} for t, n, s, c in by_task],
        'errors': [{'error': e, 'count': n} for e, n in errors],
    }


def recent(db, lock, limit: int = 50) -> list:
    with lock:
        rows = db.execute('''select ts, task, model, ok, correct, latency_s, http_status, error, miner, channel_id,
                                    completion_tokens, cost_flop from probes order by ts desc limit ?''',
                          (max(1, min(limit, 500)),)).fetchall()
    keys = ('ts', 'task', 'model', 'ok', 'correct', 'latency_s', 'http_status', 'error', 'miner', 'channel_id',
            'completion_tokens', 'cost_flop')
    return [dict(zip(keys, r)) for r in rows]


class Prober:
    """Picks a model and a task, respects every cap, records the result."""

    def __init__(self, client, db, lock, url: str, key_fn, models_fn):
        self.client, self.db, self.lock = client, db, lock
        self.url, self.key_fn, self.models_fn = url, key_fn, models_fn
        self.rnd = random.Random()
        self.last_skip = None

    async def tick(self) -> dict | None:
        ok, why = allowed(self.db, self.lock)
        if not ok:
            self.last_skip = why
            return None
        key = (self.key_fn() or '').strip()
        if not key:
            self.last_skip = 'no FLOP key configured'
            return None
        models = [m for m in (await self.models_fn()) if not m.get('blocked')]
        if not models:
            self.last_skip = 'no live FLOP models'
            return None
        model = self.rnd.choice(models)['model']
        task = self.rnd.choice(TASKS)
        rec = await run_probe(self.client, self.url, key, model, task, self.rnd)
        save(self.db, self.lock, rec)
        return rec

    async def loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self.tick()
            except Exception as e:     # a probe bug must never take the server down
                self.last_skip = f'error: {type(e).__name__}'
            await asyncio.sleep(max(60, INTERVAL_S // 4))
