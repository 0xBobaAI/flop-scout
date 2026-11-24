"""Scout's own probes of FLOP inference: the measurements nobody else has.

Everything else Scout shows about miners can be rebuilt by anyone from the
explorer (see sessions.py). What cannot be rebuilt without spending is what a
real request experiences: how long the miner took to start, how steadily it
kept generating, whether it finished, whether the answer was right, and what it
cost. This module buys a small number of real completions on a schedule, checks
each answer against a known result, and records the outcome.

Four tiers, because a miner that answers one-word questions well can still
stall or drop a long generation, and a buyer with long prompts is the one that
gets hurt:

  short   one-line answer, <= 32 tokens          start-up latency
  medium  50-100 item list, ~400 tokens          steady generation
  long    ~550-word structured text, ~1200       holding a long stream
  context ~1,500-word document in the prompt,    prompt processing, then
          answer depends on its end               recall across the input

Every probe streams (SSE), so each records time to first token, generation
speed, the longest pause between chunks, and whether the stream ended cleanly
with [DONE] or was cut off. Long and context probes are where those differ.

Status: disabled until the testnet. The devnet key Scout holds is expired, and
the airdrop terms credit only compute purchased in settled testnet sessions.

How it stays honest (see PROBE_POLICY, served at /api/probes):
  - Every probe is a real request with a checkable answer. Sizes are chosen to
    measure something, never to add volume.
  - Without a pinning key, the FLOP gateway assigns the miner. With one
    (PROBE_PIN_MINERS=1 and an owner key), probes rotate across priced miners
    so each gets the same mix of tiers.
  - Scout runs no miner and probes from one wallet.
  - Hard caps on rate and daily spend, enforced here, not by convention.
  - Every result is published, including failures.
"""

import asyncio
import json
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
PIN_MINERS = os.getenv('PROBE_PIN_MINERS', '0') == '1'
STALL_S = 10.0     # a gap between chunks this long counts as a stall

# Share of probes per tier. Short probes are cheap and catch a dead miner fast;
# long and context probes are the point of the exercise but cost more.
TIER_WEIGHTS = {'short': 4, 'medium': 3, 'long': 2, 'context': 1}
# Per-tier wait. A long generation at a slow miner can legitimately take minutes;
# a short one that takes that long is the finding.
TIER_WAIT_S = {'short': 60, 'medium': 120, 'long': 300, 'context': 300}

PROBE_POLICY = {
    'purpose': 'Measure what a buyer of FLOP inference experiences: time to first token, generation speed, '
               'stalls, whether long answers finish, correctness and cost, per miner and per model.',
    'status': 'enabled' if ENABLED else 'disabled until the FLOP testnet opens',
    'tiers': {
        'short': 'one-line answer, up to 32 output tokens',
        'medium': 'a 50-100 item list, about 400 output tokens',
        'long': 'a ~550-word structured text, about 1,200 output tokens',
        'context': 'a ~1,500-word prompt whose answer depends on its end',
    },
    'mix': TIER_WEIGHTS,
    'requests': 'Real streamed completions with a known, checkable answer. No request is sent to add volume.',
    'miner_choice': ('Rotates across priced miners with a pinning key.' if PIN_MINERS else
                     'None. The FLOP gateway assigns the miner; Scout records which one served the request.'),
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
# Columns added with streamed, tiered probes. Old rows keep nulls.
EXTRA_COLS = ('tier text', 'max_tokens integer', 'ttft_s real', 'tokens_per_s real', 'max_gap_s real',
              'stalls integer', 'finished integer', 'finish_reason text', 'pinned_miner text',
              'prompt_chars integer')

_WORDS = ('planet', 'garden', 'silver', 'rocket', 'window', 'harbor', 'lantern', 'meadow', 'copper', 'falcon')
_CITIES = [('France', 'Paris'), ('Japan', 'Tokyo'), ('Canada', 'Ottawa'), ('Kenya', 'Nairobi'),
           ('Norway', 'Oslo'), ('Peru', 'Lima'), ('Egypt', 'Cairo'), ('Chile', 'Santiago')]
_TOPICS = ['how a refrigerator keeps food cold', 'why the sky looks blue', 'how a bicycle stays upright',
           'how rain forms', 'why bread rises', 'how a compass works']
_FILLER = ('The quarterly operations report covers routine maintenance, staffing rotations, supplier '
           'deliveries and facility inspections across the regional sites. ')


def _short(r):
    kind = r.randrange(3)
    if kind == 0:
        a, b = r.randint(120, 989), r.randint(120, 989)
        return f'What is {a} + {b}? Reply with the number only.', str(a + b), 'number'
    if kind == 1:
        country, city = r.choice(_CITIES)
        return f'What is the capital of {country}? One word.', city, 'contains'
    w = r.choice(_WORDS)
    return f'Write the word "{w}" backwards. Reply with that only.', w[::-1], 'contains'


def _medium(r):
    n = r.randint(50, 100)
    return (f'List the integers from 1 to {n}, separated by commas, nothing else.',
            ','.join(str(i) for i in range(1, n + 1)), 'sequence')


def _long(r):
    topic = r.choice(_TOPICS)
    return (f'Write exactly 5 numbered sections explaining {topic}. Each section starts on its own line with '
            '"Section N:" and has about 110 words. After the last section, write the exact word END on its own '
            'line.', '5', 'sections')


def _context(r):
    # A long document with one fact near the end. The model has to carry the
    # whole prompt to answer, which is the prompt-processing cost a buyer with
    # long inputs pays.
    code = f'{r.randint(1000, 9999)}-{r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")}{r.randint(10, 99)}'
    paras = [f'Section {i + 1}. ' + _FILLER * 4 for i in range(17)]
    paras.insert(15, f'Section 16 note: the vault access code for the northern site is {code}. ')
    doc = '\n\n'.join(paras)
    return (f'{doc}\n\nQuestion: according to the document above, what is the vault access code for the '
            'northern site? Reply with the code only.', code, 'contains')


TASKS = {
    'short': {'max_tokens': 32, 'make': _short},
    'medium': {'max_tokens': 420, 'make': _medium},
    'long': {'max_tokens': 1200, 'make': _long},
    'context': {'max_tokens': 32, 'make': _context},
}


def init(db, lock) -> None:
    with lock:
        db.execute(SCHEMA)
        for col in EXTRA_COLS:
            try:
                db.execute(f'alter table probes add column {col}')
            except Exception:
                pass
        db.execute('create index if not exists probes_ts on probes(ts)')
        db.commit()


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def check(kind: str, expected: str, got: str) -> bool:
    """Is `got` a correct answer? Strict where the task allows it."""
    g = got or ''
    if kind == 'number':
        nums = re.findall(r'-?\d+', g)
        return bool(nums) and nums[0] == expected
    if kind == 'sequence':
        nums = [int(x) for x in re.findall(r'\d+', g)]
        want = [int(x) for x in expected.split(',')]
        return nums[:len(want)] == want
    if kind == 'sections':
        found = {int(n) for n in re.findall(r'(?im)^\s*section\s+(\d+)\s*:', g)}
        words = len(g.split())
        ends = bool(re.search(r'(?m)^\s*END\s*\.?\s*$', g.rstrip()[-40:] + '\n'))
        return found >= {1, 2, 3, 4, 5} and words >= 300 and ends
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


def pick_tier(db, lock, rnd: random.Random) -> str:
    """The tier furthest below its share over the last 7 days, ties broken at random.

    Deterministic catch-up rather than a weighted coin: with ~48 probes a day a
    coin can skip the context tier for days, and that tier is the rare one.
    """
    with lock:
        rows = dict(db.execute('select tier, count(*) from probes where ts >= ? and tier is not null '
                               'group by tier', (time.time() - 7 * 86400,)).fetchall())
    total_w = sum(TIER_WEIGHTS.values())
    n = sum(rows.values()) + 1
    deficit = {t: w / total_w * n - rows.get(t, 0) for t, w in TIER_WEIGHTS.items()}
    best = max(deficit.values())
    return rnd.choice([t for t, d in deficit.items() if d == best])


def pick_miner(db, lock, miners: list[str]) -> str | None:
    """The priced miner probed least recently; None if no list or pinning is off."""
    if not PIN_MINERS or not miners:
        return None
    with lock:
        last = dict(db.execute('select pinned_miner, max(ts) from probes where pinned_miner is not null '
                               'group by pinned_miner').fetchall())
    return min(miners, key=lambda m: (last.get(m, 0), m))


async def run_probe(c: httpx.AsyncClient, url: str, key: str, model: str, tier: str,
                    rnd: random.Random, pin: str | None = None) -> dict:
    """One streamed probe. Never raises: a failure is a result worth recording."""
    task = TASKS[tier]
    prompt, expected, kind = task['make'](rnd)
    wait_s = TIER_WAIT_S[tier]
    body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': task['max_tokens'], 'temperature': 0,
            'stream': True, 'stream_options': {'include_usage': True},
            'max_cost_flop': MAX_COST_FLOP, 'max_wait_ms': wait_s * 1000}
    headers = {'Authorization': f'Bearer {key}', 'Accept': 'text/event-stream'}
    if pin:
        headers['x-flop-miner'] = pin
    rec = {'ts': time.time(), 'task': kind, 'tier': tier, 'model': model, 'expected': expected[:200],
           'max_tokens': task['max_tokens'], 'prompt_chars': len(prompt), 'pinned_miner': pin,
           'ok': 0, 'correct': 0, 'finished': 0, 'stalls': 0}
    t0 = time.monotonic()
    first = last = None
    gaps = []
    parts = []
    usage = receipt = None
    finish = None
    try:
        async with c.stream('POST', f'{url}/chat/completions', json=body, headers=headers,
                            timeout=httpx.Timeout(wait_s + 15, read=wait_s + 15)) as r:
            rec['ttfb_s'] = round(time.monotonic() - t0, 3)
            rec['http_status'] = r.status_code
            if r.status_code != 200:
                raw = (await r.aread())[:400].decode(errors='replace')
                try:
                    err = json.loads(raw).get('error')
                    rec['error'] = (err.get('code') or err.get('message')) if isinstance(err, dict) else str(err)
                except ValueError:
                    rec['error'] = raw[:200]
                rec['latency_s'] = round(time.monotonic() - t0, 3)
                return rec
            async for line in r.aiter_lines():
                if not line.startswith('data:'):
                    continue            # comments are keepalives, not progress
                data = line[5:].strip()
                if data == '[DONE]':
                    rec['finished'] = 1
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if ev.get('error'):
                    rec['error'] = str(ev.get('error'))[:120]
                    break
                for ch in ev.get('choices') or []:
                    piece = (ch.get('delta') or {}).get('content') or ''
                    if piece:
                        now = time.monotonic()
                        if first is None:
                            first = now
                        elif last is not None:
                            gaps.append(now - last)
                        last = now
                        parts.append(piece)
                    finish = ch.get('finish_reason') or finish
                usage = ev.get('usage') or usage
                receipt = ev.get('x_flop_receipt') or receipt
    except httpx.TimeoutException:
        rec['error'] = 'timeout'
    except httpx.HTTPError as e:
        rec['error'] = type(e).__name__
    end = time.monotonic()
    got = ''.join(parts)
    rec['latency_s'] = round(end - t0, 3)
    if first is not None:
        rec['ttft_s'] = round(first - t0, 3)
    rec['max_gap_s'] = round(max(gaps), 3) if gaps else None
    rec['stalls'] = sum(1 for g in gaps if g >= STALL_S)
    rec['finish_reason'] = finish
    rc, u = receipt or {}, usage or {}
    rec.update(miner=rc.get('miner'), channel_id=rc.get('channel_id') or rc.get('channelId'),
               request_id=rc.get('request_id'), prompt_tokens=u.get('prompt_tokens'),
               completion_tokens=u.get('completion_tokens'), cost_flop=u.get('cost_flop'), got=got[:500])
    ct = u.get('completion_tokens')
    if ct and first is not None and last is not None and last > first:
        rec['tokens_per_s'] = round(ct / (last - first), 2)
    if got and not rec.get('error'):
        rec['ok'] = 1
        rec['correct'] = int(rec['finished'] == 1 and check(kind, expected, got))
    return rec


COLS = ('ts', 'task', 'tier', 'model', 'ok', 'correct', 'latency_s', 'ttfb_s', 'ttft_s', 'tokens_per_s',
        'max_gap_s', 'stalls', 'finished', 'finish_reason', 'http_status', 'error', 'miner', 'pinned_miner',
        'channel_id', 'request_id', 'prompt_tokens', 'completion_tokens', 'max_tokens', 'prompt_chars',
        'cost_flop', 'expected', 'got')


def save(db, lock, rec: dict) -> None:
    with lock:
        db.execute(f'insert into probes({",".join(COLS)}) values ({",".join("?" * len(COLS))})',
                   tuple(rec.get(k) for k in COLS))
        db.commit()


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[len(xs) // 2], 2) if xs else None


def summary(db, lock, window: int = 86400) -> dict:
    """Per tier, per miner and per model over the window, from probes alone."""
    start = time.time() - window
    with lock:
        rows = db.execute('select tier, miner, model, ok, correct, finished, ttft_s, tokens_per_s, max_gap_s, '
                          'stalls, latency_s, cost_flop, error, http_status from probes '
                          'where ts >= ? and tier is not null', (start,)).fetchall()

    def agg(group):
        n = len(group)
        served = [g for g in group if g[3]]
        return {'probes': n, 'served': len(served),
                'finished': sum(1 for g in group if g[5]),
                'correct': sum(1 for g in group if g[4]),
                'success_rate': round(sum(1 for g in group if g[4]) / n, 3) if n else None,
                'median_ttft_s': _med([g[6] for g in served]),
                'median_tokens_per_s': _med([g[7] for g in served]),
                'worst_gap_s': max((g[8] for g in served if g[8] is not None), default=None),
                'stalls': sum(g[9] or 0 for g in group),
                'median_latency_s': _med([g[10] for g in served]),
                'spent_flop': round(sum(g[11] or 0 for g in group), 6)}

    def by(idx):
        out = {}
        for r in rows:
            out.setdefault(r[idx], []).append(r)
        return out

    miners = []
    for m, g in by(1).items():
        if not m:
            continue
        tiers = {}
        for r in g:
            tiers.setdefault(r[0], []).append(r)
        miners.append({'miner': m, **agg(g), 'tiers': {t: agg(x) for t, x in tiers.items()}})
    miners.sort(key=lambda x: (-(x['success_rate'] or 0), -x['probes']))
    errors = {}
    for r in rows:
        if not r[3]:
            k = r[12] or f'http {r[13]}'
            errors[k] = errors.get(k, 0) + 1
    return {
        'window_s': window,
        'tiers': {t: agg(g) for t, g in by(0).items()},
        'models': [{'model': m, **agg(g)} for m, g in by(2).items()],
        'miners': miners,
        'errors': [{'error': e, 'count': n} for e, n in sorted(errors.items(), key=lambda kv: -kv[1])[:8]],
    }


def recent(db, lock, limit: int = 50) -> list:
    keys = ('ts', 'tier', 'model', 'ok', 'correct', 'finished', 'ttft_s', 'tokens_per_s', 'max_gap_s', 'stalls',
            'latency_s', 'http_status', 'error', 'miner', 'pinned_miner', 'completion_tokens', 'max_tokens',
            'cost_flop')
    with lock:
        rows = db.execute(f'select {",".join(keys)} from probes order by ts desc limit ?',
                          (max(1, min(limit, 500)),)).fetchall()
    return [dict(zip(keys, r)) for r in rows]


class Prober:
    """Picks a tier, a model and (with pinning) a miner; respects every cap; records the result."""

    def __init__(self, client, db, lock, url: str, key_fn, models_fn, miners_fn=None):
        self.client, self.db, self.lock = client, db, lock
        self.url, self.key_fn, self.models_fn = url, key_fn, models_fn
        self.miners_fn = miners_fn
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
        tier = pick_tier(self.db, self.lock, self.rnd)
        pin = None
        if PIN_MINERS and self.miners_fn:
            try:
                pin = pick_miner(self.db, self.lock, list(self.miners_fn(model) or []))
            except Exception:
                pin = None
        rec = await run_probe(self.client, self.url, key, model, tier, self.rnd, pin)
        # A pin the gateway refuses (403 for a wallet key, 409 for an ineligible
        # miner) is recorded, and the next tick goes unpinned for that miner's turn.
        save(self.db, self.lock, rec)
        self.last_skip = None
        return rec

    async def loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self.tick()
            except Exception as e:     # a probe bug must never take the server down
                self.last_skip = f'error: {type(e).__name__}'
            await asyncio.sleep(max(60, INTERVAL_S // 4))
