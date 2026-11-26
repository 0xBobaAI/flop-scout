"""FLOP Explorer sources — live on-chain data from explorer.devnet.flop.network.

Every fn: async (client, query, n, keys) -> list[{title, url, snippet}]

Endpoints used (all public, CORS *):
  /stats               — overview: blocks, TPS, miners, agents, FLOP locked
  /top-holders         — rich list with % of supply
  /inference-spend     — total compute spend, top agent spenders
  /miner-reliability   — settled/timeout/ghost per miner (245 entries)
  /compute-channels    — agent→miner channels with escrow (33K total)
  /transactions        — on-chain txs; section=balances gives actual FLOP transfers
  /agent-wallets       — 13K agent wallets with caps and status
  /activity            — daily blocks/transactions history
  /rewards/summary     — total distributed rewards
  /staking/stats       — total staked, validator count
  /mining-capacity     — GPU inventory by model
  /price-index         — VWAP price index, trades, distinct agents/miners
  /settlement-prices   — actual settlement prices per channel
  /reliability-events   — failure events (force_open_expired etc)
  /accounts/{addr}     — balance, nonce, tx count
  /miners/{addr}       — full miner detail incl. work, calibration
  /miner-reliability?miner={addr} — reliability for one miner
"""

import re
import time

from netcfg import EXPLORER, LABEL
API = f'{EXPLORER}/api'

_FLOP = 1e18
_cache: dict = {}
_TTL = 60

# The explorer is a single small VPS-side indexer: nine parallel queries from one
# worker made several of them time out at 15 s while the same endpoints answered
# in ~1 s serially. Cap concurrency across this module rather than per-request.
_EXPLORER_SEM = None


def _sem():
    global _EXPLORER_SEM
    if _EXPLORER_SEM is None:
        import asyncio
        _EXPLORER_SEM = asyncio.Semaphore(3)
    return _EXPLORER_SEM


async def _get(c, path, **kw):
    """Every explorer call goes through here: 3 concurrent requests, 20 s timeout.

    Measured on the devnet indexer: nine simultaneous queries from one client made
    five of them time out at 15 s, while the same endpoints answered in ~1 s
    serialised. A semaphore is cheaper than losing five sources per answer.
    """
    kw.setdefault('timeout', 20)
    async with _sem():
        return await c.get(f'{API}/{path}', **kw)


def _c(key):
    if key in _cache and time.time() - _cache[key][0] < _TTL:
        return _cache[key][1]
    return None


def _s(key, val):
    _cache[key] = (time.time(), val)
    return val


def _flop(raw) -> str:
    try:
        v = int(raw)
        return f"{v / _FLOP:,.2f} FLOP" if abs(v) >= _FLOP else f"{v:,} raw"
    except Exception:
        return str(raw)


def _short(addr: str) -> str:
    return addr[:8] + '…' + addr[-4:] if addr and len(addr) > 12 else addr or '?'


def _ts(ms) -> str:
    try:
        return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(ms) / 1000))
    except Exception:
        return ''


def _addr_from_q(q: str) -> str | None:
    m = re.search(r'5[A-Za-z0-9]{47}', q or '')
    return m.group(0) if m else None


# ── Overview ──────────────────────────────────────────────────────────────────

async def explorer_overview(c, q, n, keys):
    """Network snapshot: blocks, TPS, FLOP locked, validators, agents, miners."""
    cached = _c('overview')
    if cached:
        return cached
    r = await _get(c, 'stats')
    if not r.is_success:
        return []
    j = r.json()
    fin = j.get('finality', {})
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    result = [{
        'title': f'FLOP {LABEL}: live network overview',
        'url': EXPLORER,
        'snippet': (
            f"Block {j.get('latestBlockNumber', 0):,} · "
            f"TPS {j.get('tps', 0)} · "
            f"avg block time {j.get('avgBlockTime', 0)} ms · "
            f"finality: {fin.get('state', '?')} (lag {fin.get('finalizedLag', '?')}) · "
            f"total accounts: {j.get('totalAccounts', 0):,} · "
            f"24h tx volume: {j.get('txVolume24h', 0):,} · "
            f"total transactions: {j.get('totalTransactions', 0):,}"
        )
    }, {
        'title': f'FLOP {LABEL}: participants snapshot',
        'url': f'{EXPLORER}/miners',
        'snippet': (
            f"Registered miners: {j.get('registeredMiners', 0)} · "
            f"active validators: {j.get('activeValidators', 0)} · "
            f"active agents: {j.get('activeAgents', 0):,} · "
            f"active HTLCs: {j.get('activeHTLCs', 0):,} · "
            f"FLOP locked: {_flop(j.get('flopLocked', 0))} · {now}"
        )
    }]
    return _s('overview', result[:n])


# ── Transfers (actual FLOP movements) ─────────────────────────────────────────

async def explorer_transfers(c, q, n, keys):
    """Recent FLOP transfers — who sent FLOP to whom, and how much.
    Parses balances.transfer events from on-chain transactions.
    """
    addr = _addr_from_q(q)
    params = {'section': 'balances', 'limit': min(n, 10)}
    if addr:
        params['address'] = addr

    r = await _get(c, 'transactions', params=params)
    if not r.is_success:
        return []
    out = []
    for tx in r.json().get('data', [])[:n]:
        # Find Transfer event to get from/to/amount
        transfer = None
        for ev in (tx.get('events') or []):
            if ev.get('method') == 'Transfer' and ev.get('section') == 'balances':
                d = ev.get('data', {})
                transfer = d
                break
        if transfer:
            frm = _short(transfer.get('from', ''))
            to = _short(transfer.get('to', ''))
            amt = _flop(transfer.get('amount', 0))
            out.append({
                'title': f"Transfer: {frm} → {to}",
                'url': f"{EXPLORER}/transactions/{tx.get('hash', '')}",
                'snippet': f"Amount: {amt} · block {tx.get('blockHash', '')[:10]}… · {_ts(tx.get('timestamp', 0))}"
            })
        else:
            method = tx.get('method', '')
            sender = _short(tx.get('sender', ''))
            out.append({
                'title': f"TX: balances.{method}",
                'url': f"{EXPLORER}/transactions/{tx.get('hash', '')}",
                'snippet': f"From: {sender or 'validator'} · {'✓' if tx.get('success') else '✗'} · {_ts(tx.get('timestamp', 0))}"
            })
    return out


# ── Rich list ─────────────────────────────────────────────────────────────────

async def explorer_richlist(c, q, n, keys):
    """Top FLOP holders by balance — rich list with % of supply."""
    cached = _c('richlist')
    if cached:
        return cached
    r = await _get(c, 'top-holders', params={'limit': min(n, 10)})
    if not r.is_success:
        return []
    out = []
    holders = r.json().get('holders', [])[:n]
    for h in holders:
        roles = ', '.join(h.get('roles') or []) or 'none'
        out.append({
            'title': f"Top holder {_short(h.get('address', ''))}",
            'url': f"{EXPLORER}/account/{h.get('address', '')}",
            'snippet': (
                f"Balance: {_flop(h.get('total', 0))} ({h.get('percent', 0):.2f}% of supply) · "
                f"free: {_flop(h.get('free', 0))} · "
                f"frozen: {_flop(h.get('frozen', 0))} · "
                f"roles: {roles}"
            )
        })
    return _s('richlist', out)


# ── Inference spend ───────────────────────────────────────────────────────────

async def explorer_spend(c, q, n, keys):
    """Total inference spend and top agent spenders — who is buying the most compute."""
    r = await _get(c, 'inference-spend')
    if not r.is_success:
        return []
    j = r.json()
    spenders = j.get('topSpenders', [])[:n]
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    out = [{
        'title': f'FLOP {LABEL}: inference spend summary',
        'url': EXPLORER,
        'snippet': (
            f"Total spent: {_flop(j.get('totalSpentRaw', 0))} · "
            f"settled channels: {j.get('settledChannels', 0):,} · "
            f"unique agents: {j.get('uniqueAgents', 0)} · {now}"
        )
    }]
    for sp in spenders:
        out.append({
            'title': f"Top spender {_short(sp.get('agent', ''))}",
            'url': f"{EXPLORER}/account/{sp.get('agent', '')}",
            'snippet': f"Spent: {_flop(sp.get('spent', 0))} on inference"
        })
    return out


# ── Miner reliability ─────────────────────────────────────────────────────────

async def explorer_reliability(c, q, n, keys):
    """Miner reliability: settled vs timeout vs ghost tasks.
    The core signal for smart routing — which miners actually deliver.
    """
    addr = _addr_from_q(q)
    if addr:
        r = await _get(c, 'miner-reliability', params={'miner': addr})
        if r.is_success:
            m = r.json()
            rate = m['settledCount'] / max(m['settledCount'] + m['timeoutCount'], 1)
            return [{
                'title': f"Reliability: {_short(addr)}",
                'url': f"{EXPLORER}/account/{addr}",
                'snippet': (
                    f"Settled: {m.get('settledCount', 0)} · "
                    f"timeout: {m.get('timeoutCount', 0)} · "
                    f"settlement rate: {rate:.1%} · "
                    f"voided: {m.get('voidedCount', 0)} · "
                    f"force-open expired: {m.get('forceOpenExpiredCount', 0)} · "
                    f"ghost issued/completed: {m.get('ghost', {}).get('issued', 0)}/{m.get('ghost', {}).get('completed', 0)} · "
                    f"disputes: {m.get('disputesOpened', 0)}"
                )
            }]
        return []

    # Top reliable miners
    r = await _get(c, 'miner-reliability', params={'limit': min(n * 2, 20)})
    if not r.is_success:
        return []
    rows = r.json().get('data', [])
    # sort by settled count desc
    rows.sort(key=lambda m: m.get('settledCount', 0), reverse=True)
    out = []
    for m in rows[:n]:
        rate = m['settledCount'] / max(m['settledCount'] + m['timeoutCount'], 1)
        out.append({
            'title': f"Reliable miner {_short(m.get('miner', ''))}",
            'url': f"{EXPLORER}/account/{m.get('miner', '')}",
            'snippet': (
                f"Settled: {m.get('settledCount', 0)} · "
                f"timeout: {m.get('timeoutCount', 0)} · "
                f"rate: {rate:.1%} · "
                f"volume: {m.get('settledVolumeGn', 0)} G_n"
            )
        })
    return out


# ── Compute channels ──────────────────────────────────────────────────────────

async def explorer_channels(c, q, n, keys):
    """Active compute channels: agent paying miner for inference right now."""
    r = await _get(c, 'compute-channels', params={'limit': min(n, 8)})
    if not r.is_success:
        return []
    out = []
    for ch in r.json().get('data', [])[:n]:
        out.append({
            'title': f"Channel: {_short(ch.get('agent', ''))} → {_short(ch.get('miner', ''))}",
            'url': EXPLORER,
            'snippet': (
                f"Escrow: {_flop(ch.get('escrow', 0))} · "
                f"status: {ch.get('status')} / {ch.get('paymentStatus')} · "
                f"evidence: {ch.get('evidenceStatus')} · "
                f"cap: {ch.get('capGflopsPerSec', '?')} GFlop/s"
            )
        })
    return out


# ── Agent wallets ──────────────────────────────────────────────────────────────

async def explorer_agents(c, q, n, keys):
    """Agent wallets registered on FLOP — daily caps, spending limits, status."""
    r = await _get(c, 'agent-wallets', params={'limit': min(n, 8)})
    if not r.is_success:
        return []
    out = []
    for aw in r.json().get('data', [])[:n]:
        out.append({
            'title': f"Agent wallet {_short(aw.get('agent', ''))}",
            'url': f"{EXPLORER}/account/{aw.get('agent', '')}",
            'snippet': (
                f"Owner: {_short(aw.get('owner', ''))} · "
                f"daily cap: {_flop(aw.get('daily_cap', 0))} · "
                f"per-tx limit: {_flop(aw.get('per_tx_limit', 0))} · "
                f"status: {aw.get('status')} · "
                f"funded: {_flop(aw.get('total_funded', 0))}"
            )
        })
    return out


# ── GPU mining capacity ────────────────────────────────────────────────────────

async def explorer_capacity(c, q, n, keys):
    """Total GPU mining capacity on FLOP — by model (A100, RTX 4090, etc)."""
    cached = _c('capacity')
    if cached:
        return cached
    r = await _get(c, 'mining-capacity')
    if not r.is_success:
        return []
    j = r.json()
    cur = j.get('current', {})
    skus = (cur.get('declaredInventory') or {}).get('value', {}).get('skus', [])
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    gpu_lines = [f"{s['sku']} ×{s['count']}" for s in skus[:n]]
    return _s('capacity', [{
        'title': f'FLOP {LABEL}: total GPU mining capacity',
        'url': EXPLORER,
        'snippet': f"GPUs: {'; '.join(gpu_lines)} · source block {cur.get('sourceBlock', '?')} · {now}"
    }])


# ── Price index ────────────────────────────────────────────────────────────────

async def explorer_priceindex(c, q, n, keys):
    """FLOP inference price index — VWAP, trades, distinct agents/miners per day."""
    r = await _get(c, 'price-index')
    if not r.is_success:
        return []
    days = r.json() if isinstance(r.json(), list) else [r.json()]
    out = []
    for d in days[:n]:
        out.append({
            'title': f"FLOP price index: {d.get('day', '?')}",
            'url': EXPLORER,
            'snippet': (
                f"Trades: {d.get('trades', 0)} · "
                f"volume: {d.get('volumeGn', 0)} G_n · "
                f"distinct agents: {d.get('distinctAgents', 0)} · "
                f"distinct miners: {d.get('distinctMiners', 0)} · "
                f"top miner share: {d.get('topMinerVolumeShare', 0):.1%} · "
                f"HHI: {d.get('hhi', 0):.4f}"
            )
        })
    return out


# ── Rewards ────────────────────────────────────────────────────────────────────

async def explorer_rewards(c, q, n, keys):
    """Total rewards distributed to miners and validators."""
    r = await _get(c, 'rewards/summary')
    if not r.is_success:
        return []
    j = r.json()
    return [{
        'title': f'FLOP {LABEL}: rewards summary',
        'url': EXPLORER,
        'snippet': (
            f"Total distributed: {_flop(j.get('totalDistributed', 0))} · "
            f"miner rewards: {_flop(j.get('totalMinerRewards', 0))} · "
            f"validator rewards: {_flop(j.get('totalValidatorRewards', 0))} · "
            f"distributions: {j.get('distributionCount', 0)}"
        )
    }]


# ── Staking stats ──────────────────────────────────────────────────────────────

async def explorer_staking(c, q, n, keys):
    """FLOP staking: total staked, validators, delegators, commission."""
    r = await _get(c, 'staking/stats')
    if not r.is_success:
        return []
    j = r.json()
    vals = j.get('validators', {})
    return [{
        'title': f'FLOP {LABEL}: staking stats',
        'url': EXPLORER,
        'snippet': (
            f"Total staked: {_flop(j.get('totalStaked', 0))} · "
            f"staking ratio: {j.get('stakingRatio', 0)} · "
            f"active validators: {vals.get('active', 0)}/{vals.get('total', 0)} · "
            f"24h rewards: {_flop(j.get('totalRewards24h', 0))} · "
            f"delegators: {j.get('totalDelegators', 0)}"
        )
    }]


# ── Address lookup (enhanced) ─────────────────────────────────────────────────

async def explorer_address(c, q, n, keys):
    """Look up a specific FLOP address: balance + role + reliability."""
    addr = _addr_from_q(q)
    if not addr:
        return []
    r = await _get(c, f'accounts/{addr}')
    out = []
    if r.is_success:
        a = r.json()
        out.append({
            'title': f"FLOP address {_short(addr)}",
            'url': f"{EXPLORER}/account/{addr}",
            'snippet': (
                f"Balance: {_flop(a.get('balance', 0))} · "
                f"nonce: {a.get('nonce', 0)} · "
                f"transactions: {a.get('txCount', 0)} · "
                f"created: {_ts(a.get('createdAt', 0))}"
            )
        })

    # Check if miner + reliability
    r_m = await _get(c, f'miners/{addr}')
    if r_m.is_success:
        m = r_m.json()
        cal = m.get('calibration') or {}
        w = m.get('work') or {}
        gpus = (m.get('declaredGpuInventory') or {}).get('gpus') or []
        gpu_str = ', '.join(f"{g.get('name')} ×{g.get('count')}" for g in gpus[:2]) or 'unknown'
        out.append({
            'title': f"{_short(addr)} is a FLOP miner",
            'url': f'{EXPLORER}/account/{addr}',
            'snippet': (
                f"GPU: {gpu_str} · calibration: {cal.get('phase', '?')} "
                f"({cal.get('empiricalCapGflopsPerSec', '?')} GFlop/s) · "
                f"settled: {w.get('settled', 0)} · "
                f"stake: {_flop(int(m.get('selfStake', 0)) + int(m.get('sponsoredStake', 0)))}"
            )
        })

    r_r = await _get(c, 'miner-reliability', params={'miner': addr})
    if r_r.is_success:
        rl = r_r.json()
        if isinstance(rl, dict) and rl.get('settledCount', 0) + rl.get('timeoutCount', 0) > 0:
            rate = rl['settledCount'] / max(rl['settledCount'] + rl['timeoutCount'], 1)
            out.append({
                'title': f"Reliability: {_short(addr)}",
                'url': f'{EXPLORER}/account/{addr}',
                'snippet': (
                    f"Settled: {rl.get('settledCount', 0)} · "
                    f"timeout: {rl.get('timeoutCount', 0)} · "
                    f"rate: {rate:.1%} · "
                    f"disputes: {rl.get('disputesOpened', 0)}"
                )
            })
    return out[:n]


# ── Recent transactions (all types) ──────────────────────────────────────────

async def explorer_transactions(c, q, n, keys):
    """Recent on-chain transactions of any type."""
    addr = _addr_from_q(q)
    if addr:
        return await explorer_address(c, q, n, keys)

    r = await _get(c, 'transactions', params={'limit': min(n, 10)})
    if not r.is_success:
        return []
    out = []
    for tx in r.json().get('data', [])[:n]:
        section = tx.get('section', ''); method = tx.get('method', '')
        sender = tx.get('sender', '')
        success = '✓' if tx.get('success') else '✗'
        ts = _ts(tx.get('timestamp', 0))
        events = [f"{e.get('section')}.{e.get('method')}" for e in (tx.get('events') or [])[:3]]
        out.append({
            'title': f"TX: {section}.{method} {success}",
            'url': f"{EXPLORER}/transactions/{tx.get('hash', '')}",
            'snippet': f"{'from ' + _short(sender) if sender else 'validator'} · events: {', '.join(events)} · {ts}"
        })
    return out
