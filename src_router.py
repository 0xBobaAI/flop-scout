"""Smart Router for FLOP inference.

Strategy:
  1. Build a scored ranking of active miners from two signals:
       a) explorer_reliability (on-chain settled/timeout ratio) — weight 0.6
       b) local perf log (our own observed latency + success) — weight 0.4
  2. Attempt inference sequentially on top-ranked miners.
     The FLOP gateway picks the actual miner from price_sources; we can't pin.
     But we can:
       a) Use `max_cost_flop` to prevent sending tokens to known bad miners
          (gateway skips miners over the cap — if all skip, 503 → retry)
       b) Track per-call outcomes and feed them back into the local score
  3. Expose `pick_model()` — returns the best available model+strategy for a request.
  4. Expose `route()` — wraps llm.complete() with retry logic and miner feedback.

Cache TTL: reliability data refreshed every 2 min, local scores always current.
"""

import asyncio
import logging
import time

import flop_gateway
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger('flop.router')

from netcfg import EXPLORER_API
FLOP_GATEWAY = 'https://api.flop.network/v1'

# How long to cache reliability from explorer
_REL_TTL = 120  # 2 min
# How long to wait before retrying a FAILED refresh. Without this a down explorer
# was retried on every single request, turning one outage into a request storm.
_FAIL_TTL = 45
# Min sessions before we trust local stats
_LOCAL_MIN_CALLS = 3
# Reliability signal weights
_W_CHAIN = 0.55   # settlement: independent demand from sessions when known, lifetime counters otherwise
_W_LOCAL = 0.35   # local latency-adjusted success rate
_W_RECENCY = 0.10  # has the miner been seen recently (priced + not expired)

# Per-miner settlement over independent demand, set by server.py from the
# explorer session record: {address: (settled, timeouts)}. On the devnet one
# agent wallet opens ~77% of sessions and causes most timeouts, so the
# explorer's lifetime counters rank miners by which agents reached them. This
# replaces them whenever a miner has independent sessions.
INDEPENDENT: dict = {}
# Bayesian prior: a miner with few sessions is pulled towards the network's
# independent rate instead of jumping to 0% or 100% on two or three sessions.
PRIOR_RATE = 0.8
PRIOR_WEIGHT = 10


@dataclass
class MinerScore:
    address: str
    # from chain
    chain_settled: int = 0
    chain_timeout: int = 0
    chain_rate: float = 0.0   # settled / (settled + timeout), None → 0.5 (unknown)
    volume_gn: int = 0
    # from gateway (priced, active)
    is_priced: bool = False
    is_active: bool = False
    best_price: Optional[float] = None   # FLOP per G_n; None = not currently priced
    models: list = field(default_factory=list)
    # from local log
    local_calls: int = 0
    local_ok: int = 0
    local_latency: Optional[float] = None
    local_last_seen: Optional[float] = None
    # independent demand (sessions), when known
    ind_settled: int = 0
    ind_timeout: int = 0
    # computed
    score: float = 0.0
    settle_basis: str = 'unknown'

    @property
    def chain_total(self): return self.chain_settled + self.chain_timeout

    @property
    def local_rate(self):
        if self.local_calls < _LOCAL_MIN_CALLS:
            return None
        return self.local_ok / self.local_calls

    @property
    def ind_total(self): return self.ind_settled + self.ind_timeout

    def compute_score(self):
        # 1. Settlement signal. Independent demand first, smoothed towards the
        #    network rate; the explorer's lifetime counters only when a miner
        #    has no independent sessions at all.
        #    Both are smoothed the same way: a lifetime 6/6 used to outrank a
        #    week of 163/173 independent sessions.
        if self.ind_total > 0:
            cs = (self.ind_settled + PRIOR_RATE * PRIOR_WEIGHT) / (self.ind_total + PRIOR_WEIGHT)
            self.settle_basis = 'independent'
        elif self.chain_total > 0:
            cs = (self.chain_settled + PRIOR_RATE * PRIOR_WEIGHT) / (self.chain_total + PRIOR_WEIGHT)
            self.settle_basis = 'lifetime'
        else:
            # No sessions yet: the network rate, i.e. the same smoothing with
            # n = 0. A fixed 0.5 put every untested miner above proven ones
            # whenever the network settled below 50% (Testnet opening: 24%).
            cs = PRIOR_RATE
            self.settle_basis = 'unknown'
        # 2. Local signal
        lr = self.local_rate
        if lr is None:
            ls = cs  # fall back to chain
        else:
            # Penalise high latency (> 60s hurts; < 30s is good)
            lat = self.local_latency or 45
            lat_factor = max(0.3, 1.0 - (lat - 30) / 120)
            ls = lr * lat_factor
        # 3. Recency — is the miner actually in the gateway right now?
        recency = 1.0 if self.is_active else 0.0
        self.score = _W_CHAIN * cs + _W_LOCAL * ls + _W_RECENCY * recency
        return self.score


# ── Cache state ───────────────────────────────────────────────────────────────

class _RouterState:
    def __init__(self):
        self._chain_ts = 0.0
        self._chain_err_ts = 0.0
        self._gateway_err_ts = 0.0
        self._chain: dict[str, dict] = {}     # addr -> reliability row
        self._gateway_ts = 0.0
        self._gateway: dict[str, dict] = {}   # addr -> {models, price, active}
        self._lock = asyncio.Lock()
        # Per-call local stats (in memory — also in SQLite via server.py)
        self._local: dict[str, dict] = defaultdict(lambda: {
            'calls': 0, 'ok': 0, 'sum_lat': 0.0, 'last_seen': None
        })

    # ── Refresh helpers ───────────────────────────────────────────────────────

    async def _refresh_chain(self, client):
        """Fetch ALL miner-reliability rows in parallel pages."""
        first = await client.get(f'{EXPLORER_API}/miner-reliability',
                                 params={'limit': 100, 'offset': 0}, timeout=15)
        first.raise_for_status()
        j = first.json()
        rows = list(j['data'])
        total = j['pagination']['total']
        if total > 100:
            coros = [client.get(f'{EXPLORER_API}/miner-reliability',
                                params={'limit': 100, 'offset': o}, timeout=15)
                     for o in range(100, total, 100)]
            pages = await asyncio.gather(*coros, return_exceptions=True)
            for p in pages:
                if isinstance(p, Exception): continue
                rows.extend(p.json().get('data', []))
        self._chain = {r['miner']: r for r in rows}
        self._chain_ts = time.time()
        log.debug('chain reliability refreshed: %d miners', len(self._chain))
        # Hand the raw rows to whoever wants to persist a history snapshot.
        cb = on_chain_refresh
        if cb:
            try:
                cb(rows)
            except Exception as e:      # a ledger problem must never break routing
                log.warning('chain snapshot hook failed: %s', e)

    async def _refresh_gateway(self, client):
        """Snapshot active price_sources from /v1/models.

        Reads the shared flop_gateway cache instead of its own copy, so the
        router and the miner table can never disagree about how many miners are
        priced and active right now.
        """
        payload = await flop_gateway.snapshot(client)
        if payload is None:
            raise RuntimeError('no gateway snapshot available')
        now = datetime.now(timezone.utc)
        gw = {}
        for m in payload.get('data', []):
            for ps in m.get('x-flop', {}).get('price_sources', []):
                addr = ps.get('miner_address')
                if not addr: continue
                entry = gw.setdefault(addr, {'models': [], 'price': float('inf'), 'active': False})
                entry['models'].append(m['id'])
                price = float(ps.get('flop_per_gn', 999))
                if price < entry['price']:
                    entry['price'] = price
                try:
                    exp = datetime.fromisoformat(ps['expires_at'].replace('Z', '+00:00'))
                    if exp > now:
                        entry['active'] = True
                except Exception:
                    pass
        self._gateway = gw
        self._gateway_ts = time.time()
        log.debug('gateway refreshed: %d priced miners', len(gw))

    async def refresh(self, client, force=False):
        now = time.time()
        async with self._lock:
            jobs = {}
            # A failed refresh backs off for _FAIL_TTL instead of retrying on the
            # next request, and a failed gateway refresh must not wipe prices.
            if (force or now - self._chain_ts > _REL_TTL) and now - self._chain_err_ts > _FAIL_TTL:
                jobs['chain'] = self._refresh_chain(client)
            if (force or now - self._gateway_ts > _REL_TTL) and now - self._gateway_err_ts > _FAIL_TTL:
                jobs['gateway'] = self._refresh_gateway(client)
            if not jobs:
                return
            names = list(jobs)
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
            for name, res in zip(names, results):
                if not isinstance(res, Exception):
                    continue
                # Stamp the failure so the next requests do not pile on.
                if name == 'chain':
                    self._chain_err_ts = time.time()
                else:
                    self._gateway_err_ts = time.time()
                # repr, not str: some transport errors stringify to ''.
                log.warning('router refresh error (%s): %r', name, res)

    # ── Score all miners ──────────────────────────────────────────────────────

    def ranked_miners(self, model_id: Optional[str] = None) -> list[MinerScore]:
        """Return miners sorted by score, optionally filtered by model support."""
        # Union of all known addresses
        all_addrs = set(self._chain.keys()) | set(self._gateway.keys()) | set(self._local.keys())
        result = []
        for addr in all_addrs:
            cr = self._chain.get(addr, {})
            gw = self._gateway.get(addr, {})
            lo = self._local.get(addr, {})
            ms = MinerScore(
                address=addr,
                chain_settled=cr.get('settledCount', 0),
                chain_timeout=cr.get('timeoutCount', 0),
                volume_gn=int(cr.get('settledVolumeGn') or 0),
                is_priced=bool(gw),
                is_active=gw.get('active', False),
                best_price=gw.get('price'),
                models=gw.get('models', []),
                local_calls=lo.get('calls', 0),
                local_ok=lo.get('ok', 0),
                local_latency=(lo['sum_lat'] / lo['calls']) if lo.get('calls', 0) >= _LOCAL_MIN_CALLS else None,
                local_last_seen=lo.get('last_seen'),
                ind_settled=(INDEPENDENT.get(addr) or (0, 0))[0],
                ind_timeout=(INDEPENDENT.get(addr) or (0, 0))[1],
            )
            if ms.chain_total > 0:
                ms.chain_rate = ms.chain_settled / ms.chain_total
            ms.compute_score()
            # Only a miner that is currently offering the model can serve it. An
            # unpriced miner used to pass this filter (empty models list) and win
            # the ranking on chain history alone, which it cannot honour.
            if model_id and (not ms.is_priced or model_id not in ms.models):
                continue
            result.append(ms)
        result.sort(key=lambda m: (not m.is_active, -m.score))
        return result

    def record_outcome(self, miner: Optional[str], ok: bool, latency_s: float):
        if not miner: return
        lo = self._local[miner]
        lo['calls'] += 1
        lo['ok'] += int(ok)
        lo['sum_lat'] += latency_s
        lo['last_seen'] = time.time()

    def snapshot(self, top_n=20) -> list[dict]:
        """Human-readable routing snapshot for /api/router endpoint."""
        miners = self.ranked_miners()
        return [
            {
                'rank': i + 1,
                'address': m.address,
                'score': round(m.score, 3),
                'chain_rate': round(m.chain_rate, 3) if m.chain_total else None,
                'chain_sessions': m.chain_total,
                'independent_rate': round(m.ind_settled / m.ind_total, 3) if m.ind_total else None,
                'independent_sessions': m.ind_total,
                'settle_basis': m.settle_basis,
                'local_calls': m.local_calls,
                'local_rate': round(m.local_rate, 3) if m.local_rate is not None else None,
                'local_latency_s': round(m.local_latency, 1) if m.local_latency else None,
                'is_active': m.is_active,
                'is_priced': m.is_priced,
                'best_price': m.best_price,
                'models': m.models[:3],
            }
            for i, m in enumerate(miners[:top_n])
        ]


STATE = _RouterState()

# Set by server.py: on_chain_refresh(rows) persists a settlement snapshot so the
# router can answer "what changed this week" instead of only "right now".
on_chain_refresh = None


# ── Public API ────────────────────────────────────────────────────────────────

async def startup(client):
    """Call once at server start to pre-warm the router cache."""
    await STATE.refresh(client, force=True)


async def refresh(client, force=False):
    """Refresh router cache if stale (called from hot paths)."""
    await STATE.refresh(client, force=force)


async def ranked(client, model_id: Optional[str] = None, top_n: int = 10) -> list[MinerScore]:
    """Return top-ranked miners, refreshing cache if stale."""
    await STATE.refresh(client)
    return STATE.ranked_miners(model_id)[:top_n]


def record(miner: Optional[str], ok: bool, latency_s: float):
    """Call after every FLOP inference attempt to update local stats."""
    STATE.record_outcome(miner, ok, latency_s)


async def routing_snapshot(client, top_n: int = 20) -> dict:
    """Full snapshot for /api/router — used by UI and debugging."""
    await STATE.refresh(client)
    return cached_snapshot(top_n)


def state_has_gateway() -> bool:
    """True once /v1/models has answered at least once since start.

    A timestamp of 0 is the honest marker: no snapshot, no opinion.
    """
    return bool(STATE._gateway_ts)


def cached_snapshot(top_n: int = 20) -> dict:
    """Same shape as routing_snapshot but never touches the network.

    /api/catalog renders on every page load; waiting on an explorer round trip
    there costs tens of seconds on a small VPS. Callers that need fresh data
    should schedule STATE.refresh() in the background.
    """
    miners = STATE.ranked_miners()
    active = [m for m in miners if m.is_active]
    # No successful gateway snapshot yet => prices are unknown, not zero. The
    # UI prints "—" for null instead of claiming the network has no miners.
    prices_known = state_has_gateway()
    return {
        'refreshed_at': int(STATE._chain_ts),
        'chain_miners': len(STATE._chain),
        'gateway_miners': len(STATE._gateway),
        'prices_known': prices_known,
        'active_priced': len(active) if prices_known else None,
        'top': STATE.snapshot(top_n),
        'best': {
            'address': active[0].address if active else None,
            'score': round(active[0].score, 3) if active else None,
            'chain_rate': round(active[0].chain_rate, 3) if active and active[0].chain_total else None,
        }
    }


async def best_miner(client, model_id: Optional[str] = None) -> Optional[MinerScore]:
    """Return the single best active miner for a model, or None."""
    top = await ranked(client, model_id=model_id, top_n=1)
    if top and top[0].is_active:
        return top[0]
    return None
