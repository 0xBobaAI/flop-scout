"""Per-session settlement history, rebuilt from the FLOP explorer.

Why this replaces the snapshot table as the source of history
--------------------------------------------------------------
Scout used to store each miner's lifetime settled/timeout counters every 15-25
minutes and diff them. That history began the day Scout started, sampled at
whatever interval the refresh happened to run, and could not be checked by
anyone else. The explorer exposes every compute channel individually
(GET /api/compute-channels: agent, miner, status, paymentStatus, openedAt,
updatedAt, block numbers), newest first, 100 rows per page. From that anyone
can rebuild every miner's settlement record for any window since the network
began, so this module does exactly that and keeps a local, verifiable copy.

What the copy is for
--------------------
Measured on the devnet (2026-11-16): one agent wallet opened about 95% of all
sessions and accounted for about 98% of all timeouts, while the network as a
whole settled ~23% of sessions. A miner's raw settle rate therefore says as
much about which agents happened to reach it as about the miner. Scout reports
the raw rate next to an "independent demand" rate that excludes agents above a
share threshold, so a reader can see both and tell the difference.

Nothing here is invented: every number is a count of channels the explorer
returned, and `GET /api/sessions/coverage` says how far back the copy reaches.
"""

import asyncio
import time

import httpx

EXPLORER_API = 'https://explorer.devnet.flop.network/api'
PAGE = 100                 # the explorer caps limit at 100
SETTLED = 'settled'
TIMEDOUT = 'timedout'
FINAL = (SETTLED, TIMEDOUT)
# Non-final states seen on the devnet: 'open' and 'settling' (both LOCKED).
# Some channels have stayed open for 10+ days, so 'not final' is not 'recent'.
PENDING = ('open', 'settling')
# An agent that opens more than this share of sessions in a window is treated
# as a dominant load generator, and its sessions are reported separately.
DOMINANT_SHARE = 0.25

SCHEMA = '''
create table if not exists sessions(
    channel_id text primary key,
    agent text, miner text, status text, payment_status text,
    opened_at real, updated_at real, opened_block integer, status_block integer,
    escrow text, model_hash text, seen_at real, miner_paid text, aggregate_gn text)
'''
INDEXES = (
    'create index if not exists sessions_opened on sessions(opened_at)',
    'create index if not exists sessions_miner_opened on sessions(miner, opened_at)',
    'create index if not exists sessions_pending on sessions(status) where status in (\'open\', \'settling\')',
)


def init(db, lock) -> None:
    with lock:
        db.execute(SCHEMA)
        for col in ('miner_paid text', 'aggregate_gn text'):
            try:
                db.execute(f'alter table sessions add column {col}')
            except Exception:
                pass
        for ix in INDEXES:
            db.execute(ix)
        db.execute('create table if not exists sync_state(key text primary key, value text)')
        db.commit()


def _row(c: dict, now: float) -> tuple:
    return (c.get('channelId'), c.get('agent'), c.get('miner'), c.get('status'), c.get('paymentStatus'),
            (c.get('openedAt') or 0) / 1000, (c.get('updatedAt') or 0) / 1000,
            c.get('openedBlock'), c.get('statusBlockNumber'), str(c.get('escrow') or ''),
            c.get('modelHash'), now, str(c.get('minerPaid') or ''), str(c.get('aggregateGn') or ''))


def _state(db, lock, key, default=None):
    with lock:
        r = db.execute('select value from sync_state where key = ?', (key,)).fetchone()
    return r[0] if r else default


def _set_state(db, lock, key, value) -> None:
    with lock:
        db.execute('insert into sync_state(key, value) values (?, ?) '
                   'on conflict(key) do update set value = excluded.value', (key, str(value)))
        db.commit()


def _upsert(db, lock, channels: list) -> int:
    now = time.time()
    rows = [_row(c, now) for c in channels if c.get('channelId')]
    if not rows:
        return 0
    with lock:
        db.executemany('''insert into sessions(channel_id, agent, miner, status, payment_status, opened_at,
                              updated_at, opened_block, status_block, escrow, model_hash, seen_at,
                              miner_paid, aggregate_gn)
                          values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                          on conflict(channel_id) do update set
                              status = excluded.status, payment_status = excluded.payment_status,
                              updated_at = excluded.updated_at, status_block = excluded.status_block,
                              seen_at = excluded.seen_at, miner_paid = excluded.miner_paid,
                              aggregate_gn = excluded.aggregate_gn''', rows)
        db.commit()
    return len(rows)


async def _page(c: httpx.AsyncClient, offset: int, **params) -> dict:
    r = await c.get(f'{EXPLORER_API}/compute-channels',
                    params={'limit': PAGE, 'offset': offset, **params}, timeout=30)
    r.raise_for_status()
    return r.json()


async def sync_recent(c, db, lock, max_pages: int = 60) -> dict:
    """Pull the newest sessions until reaching ones already stored.

    The explorer lists newest first, so new sessions land on page 0 and push
    older ones to higher offsets. Paging until a page holds nothing new, plus
    one more page for overlap, catches everything opened since the last sync.
    """
    with lock:
        known_max = db.execute('select max(opened_at) from sessions').fetchone()[0] or 0
    fetched = 0
    for p in range(max_pages):
        j = await _page(c, p * PAGE)
        data = j.get('data') or []
        fetched += _upsert(db, lock, data)
        if not data or not j.get('pagination', {}).get('hasMore'):
            break
        if min((x.get('openedAt') or 0) / 1000 for x in data) < known_max - 60:
            break
        await asyncio.sleep(0.2)
    return {'fetched': fetched}


async def sync_open(c, db, lock, max_pages: int = 20) -> dict:
    """Re-read sessions that were pending, so they get their final state.

    The explorer filter `status=open` / `status=settling` returns only channels
    still in that state; a stored pending channel missing from both lists has
    moved on, and its final state is read back individually. Bounded so a
    backlog cannot stall a cycle.
    """
    still = set()
    for st in PENDING:
        for p in range(max_pages):
            j = await _page(c, p * PAGE, status=st)
            data = j.get('data') or []
            _upsert(db, lock, data)
            still.update(x.get('channelId') for x in data)
            if not j.get('pagination', {}).get('hasMore'):
                break
            await asyncio.sleep(0.2)
    with lock:
        stored = [r[0] for r in db.execute(
            "select channel_id from sessions where status in ('open', 'settling') "
            "order by opened_at limit 400").fetchall()]
    moved = [cid for cid in stored if cid not in still]
    updated = 0
    for cid in moved[:200]:
        try:
            r = await c.get(f'{EXPLORER_API}/compute-channels/{cid}', timeout=20)
            if r.status_code == 200:
                updated += _upsert(db, lock, [r.json()])
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    return {'still_pending': len(still), 'finalised': updated}


async def backfill(c, db, lock, pages: int = 25) -> dict:
    """Walk older pages from where the last backfill stopped, a bounded number per call.

    Offsets shift as new sessions arrive, so the walk can revisit a page; upserts
    are idempotent, so a revisit costs a request and nothing else.
    """
    off = int(_state(db, lock, 'backfill_offset', 0) or 0)
    done = _state(db, lock, 'backfill_done') == '1'
    if done:
        return {'done': True}
    got = 0
    for _ in range(pages):
        j = await _page(c, off)
        data = j.get('data') or []
        got += _upsert(db, lock, data)
        off += PAGE
        _set_state(db, lock, 'backfill_offset', off)
        # The walk is gap-free (new sessions push older ones to higher offsets,
        # so a later page overlaps rather than skips), which makes the oldest
        # session it has reached the start of the unbroken history.
        if data:
            oldest = min((x.get('openedAt') or 0) / 1000 for x in data)
            prev = float(_state(db, lock, 'walk_oldest', 0) or 0)
            if oldest and (not prev or oldest < prev):
                _set_state(db, lock, 'walk_oldest', oldest)
        if not data or not j.get('pagination', {}).get('hasMore'):
            _set_state(db, lock, 'backfill_done', '1')
            return {'done': True, 'fetched': got}
        await asyncio.sleep(0.3)
    return {'done': False, 'fetched': got, 'offset': off}


def coverage(db, lock) -> dict:
    """How much of the record is held.

    `contiguous_from` is where the unbroken run of history starts: the oldest
    session the backfill walk has reached, walking back from the newest. Before
    the walk finishes, `first_opened_at` can be much older, because a session
    still open after weeks is fetched by the pending-session sync on its own;
    that does not mean the weeks between are covered. Windows reaching further
    back than `contiguous_from` are partial.
    """
    with lock:
        n, first, last, miners, agents = db.execute(
            'select count(*), min(opened_at), max(opened_at), count(distinct miner), count(distinct agent) '
            'from sessions').fetchone()
        by = dict(db.execute('select status, count(*) from sessions group by status').fetchall())
    done = _state(db, lock, 'backfill_done') == '1'
    walk = float(_state(db, lock, 'walk_oldest', 0) or 0) or None
    contiguous = first if done else walk
    return {'sessions': n, 'first_opened_at': first, 'last_opened_at': last,
            'contiguous_from': contiguous,
            'miners': miners, 'agents': agents, 'by_status': by,
            'backfill_done': done,
            'backfill_offset': int(_state(db, lock, 'backfill_offset', 0) or 0),
            'last_sync_at': float(_state(db, lock, 'last_sync_at', 0) or 0) or None,
            'source': f'{EXPLORER_API}/compute-channels'}


def _dominant_agents(db, lock, start: float, end: float) -> dict:
    with lock:
        rows = db.execute('select agent, count(*) from sessions where opened_at >= ? and opened_at < ? '
                          'group by agent', (start, end)).fetchall()
    total = sum(n for _, n in rows)
    if not total:
        return {}
    return {a: round(n / total, 3) for a, n in rows if n / total > DOMINANT_SHARE}


def miner_stats(db, lock, window: int = 86400, end: float | None = None) -> dict:
    """Settlement per miner over [end - window, end), from stored sessions.

    Only finished sessions count: an open channel has no outcome yet, and
    counting it as either result would bias fresh windows. Returns, per miner,
    the raw rate and the rate over independent demand (sessions from agents
    below DOMINANT_SHARE of the window), with the counts behind both.
    """
    end = end or time.time()
    start = end - window
    dom = _dominant_agents(db, lock, start, end)
    with lock:
        rows = db.execute('select miner, agent, status, count(*) from sessions '
                          'where opened_at >= ? and opened_at < ? and status in (?, ?) '
                          'group by miner, agent, status', (start, end, SETTLED, TIMEDOUT)).fetchall()
    out: dict = {}
    for miner, agent, status, n in rows:
        m = out.setdefault(miner, {'settled': 0, 'timeouts': 0, 'ind_settled': 0, 'ind_timeouts': 0,
                                   'agents': set()})
        key = 'settled' if status == SETTLED else 'timeouts'
        m[key] += n
        m['agents'].add(agent)
        if agent not in dom:
            m['ind_' + key] += n
    res = {}
    for miner, m in out.items():
        s, t, si, ti = m['settled'], m['timeouts'], m['ind_settled'], m['ind_timeouts']
        res[miner] = {
            'sessions': s + t, 'settled': s, 'timeouts': t,
            'settle_rate': round(s / (s + t), 3) if s + t else None,
            'independent_sessions': si + ti,
            'independent_settle_rate': round(si / (si + ti), 3) if si + ti else None,
            'distinct_agents': len(m['agents']),
        }
    return res


def network(db, lock, window: int = 86400, buckets: int = 24, end: float | None = None) -> dict:
    end = end or time.time()
    start = end - window
    step = window / buckets
    dom = _dominant_agents(db, lock, start, end)
    with lock:
        rows = db.execute('select opened_at, agent, status from sessions '
                          'where opened_at >= ? and opened_at < ? and status in (?, ?)',
                          (start, end, SETTLED, TIMEDOUT)).fetchall()
    series = [{'ts': round(start + (i + 1) * step), 'settled': 0, 'timeouts': 0,
               'ind_settled': 0, 'ind_timeouts': 0} for i in range(buckets)]
    for ts, agent, status in rows:
        b = series[min(int((ts - start) / step), buckets - 1)]
        k = 'settled' if status == SETTLED else 'timeouts'
        b[k] += 1
        if agent not in dom:
            b['ind_' + k] += 1
    for b in series:
        n, ni = b['settled'] + b['timeouts'], b['ind_settled'] + b['ind_timeouts']
        b['settle_rate'] = round(b['settled'] / n, 3) if n else None
        b['independent_settle_rate'] = round(b['ind_settled'] / ni, 3) if ni else None
    tot_s = sum(b['settled'] for b in series)
    tot_t = sum(b['timeouts'] for b in series)
    ind_s = sum(b['ind_settled'] for b in series)
    ind_t = sum(b['ind_timeouts'] for b in series)
    return {
        'window_s': window, 'sessions': tot_s + tot_t,
        'settle_rate': round(tot_s / (tot_s + tot_t), 3) if tot_s + tot_t else None,
        'independent_sessions': ind_s + ind_t,
        'independent_settle_rate': round(ind_s / (ind_s + ind_t), 3) if ind_s + ind_t else None,
        'dominant_agents': [{'agent': a, 'share_of_sessions': s} for a, s in
                            sorted(dom.items(), key=lambda kv: -kv[1])],
        'series': series,
    }


def miner_history(db, lock, address: str, window: int = 604800, points: int = 96) -> dict:
    """One miner's sessions over the window, bucketed. Raises LookupError if none."""
    end = time.time()
    window = max(3600, min(window, 90 * 86400))
    points = max(2, min(points, 500))
    start = end - window
    dom = _dominant_agents(db, lock, start, end)
    with lock:
        rows = db.execute('select opened_at, agent, status from sessions where miner = ? and opened_at >= ? '
                          'order by opened_at', (address, start)).fetchall()
    if not rows:
        raise LookupError('No sessions for this miner in the window.')
    step = window / points
    series = [{'ts': round(start + (i + 1) * step), 'settled': 0, 'timeouts': 0, 'open': 0, 'independent': 0}
              for i in range(points)]
    for ts, agent, status in rows:
        b = series[min(int((ts - start) / step), points - 1)]
        k = {SETTLED: 'settled', TIMEDOUT: 'timeouts'}.get(status, 'open')
        b[k] += 1
        if agent not in dom and status in FINAL:
            b['independent'] += 1
    series = [b for b in series if b['settled'] + b['timeouts'] + b['open']]
    for b in series:
        n = b['settled'] + b['timeouts']
        b['settle_rate'] = round(b['settled'] / n, 3) if n else None
    st = miner_stats(db, lock, window, end).get(address, {})
    return {'address': address, 'window_s': window, **st,
            'open': sum(b['open'] for b in series),
            'first_session_at': round(rows[0][0]), 'last_session_at': round(rows[-1][0]),
            'series': series, 'source': 'explorer compute-channels, one row per session'}


def board(db, lock, window: int = 86400, points: int = 12) -> tuple[dict, dict, list]:
    """Everything the miners page needs from sessions, in one pass.

    Returns (history, sparklines, network_series):
      history[miner]  - window counts and rates, raw and independent, plus
                        settle_rate_change: this window's rate minus the
                        previous window's, over the same kind of demand.
      sparklines[miner] - cumulative rate inside the window at each step, so
                        the line ends on the number the card prints.
      network_series  - per-step network rate, raw and independent.
    Only finished sessions count.
    """
    end = time.time()
    start, prev_start = end - window, end - 2 * window
    dom = _dominant_agents(db, lock, start, end)
    dom_prev = _dominant_agents(db, lock, prev_start, start)
    with lock:
        rows = db.execute('select miner, agent, status, opened_at from sessions '
                          'where opened_at >= ? and opened_at < ? and status in (?, ?) order by opened_at',
                          (prev_start, end, SETTLED, TIMEDOUT)).fetchall()
    step = window / points
    cur: dict = {}
    prev: dict = {}
    spark: dict = {}
    net = [[0, 0, 0, 0] for _ in range(points)]     # settled, timeouts, ind_settled, ind_timeouts
    for miner, agent, status, ts in rows:
        ok = status == SETTLED
        if ts < start:
            p = prev.setdefault(miner, [0, 0, 0, 0])
            p[0 if ok else 1] += 1
            if agent not in dom_prev:
                p[2 if ok else 3] += 1
            continue
        c = cur.setdefault(miner, [0, 0, 0, 0, set()])
        c[0 if ok else 1] += 1
        ind = agent not in dom
        if ind:
            c[2 if ok else 3] += 1
        c[4].add(agent)
        i = min(int((ts - start) / step), points - 1)
        net[i][0 if ok else 1] += 1
        if ind:
            net[i][2 if ok else 3] += 1
        sp = spark.setdefault(miner, [[0, 0] for _ in range(points)])
        sp[i][0 if ok else 1] += 1

    def rate(s, t):
        return round(s / (s + t), 3) if s + t else None

    history = {}
    for miner, (s, t, si, ti, agents) in cur.items():
        p = prev.get(miner)
        # Compare like with like: independent against independent when both
        # windows have enough of it, raw against raw otherwise.
        use_ind = si + ti >= 3 and p is not None and p[2] + p[3] >= 3
        now_r = rate(si, ti) if use_ind else rate(s, t)
        then_r = (rate(p[2], p[3]) if use_ind else rate(p[0], p[1])) if p else None
        history[miner] = {
            'sessions': s + t, 'settled': s, 'timeouts': t, 'settle_rate': rate(s, t),
            'independent_sessions': si + ti, 'independent_settle_rate': rate(si, ti),
            'distinct_agents': len(agents),
            'previous_settle_rate': then_r,
            'settle_rate_change': (round(now_r - then_r, 3) if now_r is not None and then_r is not None
                                   else None),
            'change_basis': 'independent' if use_ind else 'raw',
        }
    sparklines = {}
    for miner, buckets in spark.items():
        acc_s = acc_t = 0
        pts = []
        for s, t in buckets:
            acc_s += s
            acc_t += t
            if acc_s + acc_t:
                pts.append(round(acc_s / (acc_s + acc_t), 3))
        if len(pts) >= 2:
            sparklines[miner] = pts
    network_series = [{'ts': round(start + (i + 1) * step), 'settled': a, 'timeouts': b, 'sessions': a + b,
                       'settle_rate': rate(a, b), 'independent_settle_rate': rate(c, d)}
                      for i, (a, b, c, d) in enumerate(net) if a + b]
    return history, sparklines, network_series


class Syncer:
    """Runs the three sync jobs on a schedule and records what happened."""

    def __init__(self, client, db, lock, every: int = 300):
        self.client, self.db, self.lock, self.every = client, db, lock, every
        self.last = {}

    async def run_once(self) -> dict:
        res = {}
        for name, job in (('recent', sync_recent), ('open', sync_open), ('backfill', backfill)):
            t0 = time.time()
            try:
                res[name] = await job(self.client, self.db, self.lock)
            except Exception as e:     # the explorer is one small indexer; a miss is not fatal
                res[name] = {'error': f'{type(e).__name__}: {str(e)[:120]}'}
            res[name]['seconds'] = round(time.time() - t0, 1)
            self.last = {**res, 'running': name != 'backfill'}
        _set_state(self.db, self.lock, 'last_sync_at', time.time())
        self.last = {**res, 'running': False, 'finished_at': round(time.time())}
        return res

    async def loop(self) -> None:
        await asyncio.sleep(20)                # let startup settle first
        while True:
            await self.run_once()
            await asyncio.sleep(self.every)
