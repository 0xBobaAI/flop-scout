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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger('flop.router')

EXPLORER_API = 'https://explorer.devnet.flop.network/api'
FLOP_GATEWAY = 'https://api.flop.network/v1'

# How long to cache reliability from explorer
_REL_TTL = 120  # 2 min
# Min sessions before we trust local stats
_LOCAL_MIN_CALLS = 3
# Reliability signal weights
_W_CHAIN = 0.55   # on-chain settled/timeout
_W_LOCAL = 0.35   # local latency-adjusted success rate
_W_RECENCY = 0.10  # has the miner been seen recently (priced + not expired)


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
    best_price: float = 0.003  # FLOP per G_n
    models: list = field(default_factory=list)
    # from local log
    local_calls: int = 0
    local_ok: int = 0
    local_latency: Optional[float] = None
    local_last_seen: Optional[float] = None
    # computed
    score: float = 0.0

    @property
    def chain_total(self): return self.chain_settled + self.chain_timeout

    @property
    def local_rate(self):
        if self.local_calls < _LOCAL_MIN_CALLS:
            return None
        return self.local_ok / self.local_calls

    def compute_score(self):
        # 1. Chain signal
        if self.chain_total >= 5:
            cs = self.chain_settled / self.chain_total
        else:
            cs = 0.5  # unknown → neutral
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

    async def _refresh_gateway(self, client):
        """Snapshot active price_sources from /v1/models."""
        r = await client.get(f'{FLOP_GATEWAY}/models', timeout=15)
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        gw = {}
        for m in r.json().get('data', []):
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
            tasks = []
            if force or now - self._chain_ts > _REL_TTL:
                tasks.append(self._refresh_chain(client))
            if force or now - self._gateway_ts > _REL_TTL:
                tasks.append(self._refresh_gateway(client))
            if tasks:
                try:
                    await asyncio.gather(*tasks)
                except Exception as e:
                    log.warning('router refresh error: %s', e)

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
                best_price=gw.get('price', 0.003),
                models=gw.get('models', []),
                local_calls=lo.get('calls', 0),
                local_ok=lo.get('ok', 0),
                local_latency=(lo['sum_lat'] / lo['calls']) if lo.get('calls', 0) >= _LOCAL_MIN_CALLS else None,
                local_last_seen=lo.get('last_seen'),
            )
            if ms.chain_total > 0:
                ms.chain_rate = ms.chain_settled / ms.chain_total
            ms.compute_score()
            if model_id and ms.models and model_id not in ms.models:
                continue   # miner doesn't serve this model
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


def cached_snapshot(top_n: int = 20) -> dict:
    """Same shape as routing_snapshot but never touches the network.

    /api/catalog renders on every page load; waiting on an explorer round trip
    there costs tens of seconds on a small VPS. Callers that need fresh data
    should schedule STATE.refresh() in the background.
    """
    miners = STATE.ranked_miners()
    active = [m for m in miners if m.is_active]
    return {
        'refreshed_at': int(STATE._chain_ts),
        'chain_miners': len(STATE._chain),
        'gateway_miners': len(STATE._gateway),
        'active_priced': len(active),
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
