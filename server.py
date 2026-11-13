"""Flop Scout — search chosen sources, answer with a chosen model, return citations + FLOP receipt.

Pages:  /        Answer  — ask, get a cited answer computed on FLOP
        /miners  Miners  — every miner ranked by real settlement performance

Run:  python -m uvicorn server:app --port 8787
API:  GET  /api/catalog   sources, presets, models
      POST /api/answer    {q, sources[], model, compare_model?, n, max_tokens, keys{flop,openrouter,exa,keenable,github}}
      GET  /api/stats     success rate and latency per model, FLOP sessions by miner
      GET  /api/health
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import llm
import pricing
import src_router as router
from registry import PRESETS, SOURCES

BASE = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', BASE))
FREE_PER_DAY = int(os.getenv('FREE_ANSWERS_PER_DAY', '20'))       # answers/day/IP on Scout's wallet
USER_PER_MIN = int(os.getenv('USER_ANSWERS_PER_MIN', '6'))         # answers/min/IP with own key
DUP_WINDOW_S = int(os.getenv('DUPLICATE_WINDOW_S', '600'))         # same question again within 10 min -> cached

app = FastAPI(title='Flop Scout', version='0.3.0')
app.add_middleware(CORSMiddleware, allow_origins=os.getenv('CORS_ORIGINS', '*').split(','), allow_methods=['GET', 'POST'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
client = httpx.AsyncClient(timeout=30, follow_redirects=True)

DATA.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA / 'log.db', check_same_thread=False)
db.execute('''create table if not exists answers(ts real, q text, sources text, engine text, model text, ok int,
              miner text, prompt_tokens int, completion_tokens int, cost text, latency real, attempts int, error text)''')
for col in ('paid_by text', 'client text', 'scout_flop real', 'metering text'):
    try:
        db.execute(f'alter table answers add column {col}')
    except sqlite3.OperationalError:
        pass
db.commit()

_hits = defaultdict(deque)       # ip -> timestamps
_recent = {}                     # hash(ip,q,model) -> (ts, response)


class AnswerReq(BaseModel):
    q: str = Field(min_length=2, max_length=500)
    sources: list[str] = ['keenable', 'flop_eco']
    model: str = 'flop:qwen/qwen2.5-0.5b-instruct'
    compare_model: str | None = None
    n: int = Field(3, ge=1, le=8)
    max_tokens: int = Field(250, ge=32, le=800)
    keys: dict[str, str] = {}    # caller's own keys; never stored/logged
    # Supported key names: flop, openrouter, exa, keenable, github, tavily, brave, firecrawl, serper


def client_id(request: Request):
    ip = request.headers.get('x-forwarded-for', request.client.host if request.client else '?').split(',')[0].strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def rate_limit(cid: str, own_key: bool):
    now = time.time()
    dq = _hits[cid]
    while dq and now - dq[0] > 86400:
        dq.popleft()
    if own_key:
        if sum(1 for t in dq if now - t < 60) >= USER_PER_MIN:
            raise HTTPException(429, f'Limit: {USER_PER_MIN} answers per minute. Slow down — FLOP counts genuine use, not volume.')
    elif len(dq) >= FREE_PER_DAY:
        raise HTTPException(429, f'Free tier: {FREE_PER_DAY} answers per day. Connect your agent wallet to continue.')
    dq.append(now)


def log(q, srcs, res, cid):
    u, rc = res.get('usage') or {}, res.get('receipt') or {}
    db.execute('insert into answers (ts,q,sources,engine,model,ok,miner,prompt_tokens,completion_tokens,cost,latency,attempts,error,paid_by,client,scout_flop,metering) '
               'values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (time.time(), q, ','.join(srcs), res.get('engine'), res.get('model'), int(bool(res.get('answer'))), rc.get('miner'),
                u.get('prompt_tokens'), u.get('completion_tokens'), json.dumps(u.get('cost_flop') or u.get('cost')),
                res.get('latency_s'), res.get('attempts'), (res.get('error') or '')[:300], res.get('paid_by'), cid,
                res.get('scout_charge_flop'), res.get('metering')))
    db.commit()


async def run_source(key, q, n, keys):
    t = time.time()
    try:
        items = await asyncio.wait_for(SOURCES[key]['fn'](client, q, n, keys), 25)
        return key, items, None, round(time.time() - t, 2)
    except Exception as e:
        return key, [], f'{type(e).__name__}: {str(e)[:120]}', round(time.time() - t, 2)


@app.on_event('startup')
async def _start():
    try:
        await router.startup(client)
    except Exception as e:
        print('router warm-up failed:', e)


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': app.version, 'flop_key': bool(llm.flop_key())}


@app.get('/api/catalog')
async def catalog():
    # routing_snapshot() refreshes from the network when its cache is stale, which
    # can block for tens of seconds. This endpoint renders on every page load, so
    # kick a background refresh and answer from whatever we already have.
    asyncio.create_task(router.refresh(client))
    snap = router.cached_snapshot(top_n=5)
    return {'sources': [{'id': k, **{x: v for x, v in s.items() if x != 'fn'}} for k, s in SOURCES.items()],
            'presets': PRESETS, 'models': await llm.list_models(client),
            'router': {'best_miner': snap['best'], 'active_priced': snap['active_priced']}}


@app.post('/api/answer')
async def answer(req: AnswerReq, request: Request):
    keys = {k: v.strip() for k, v in req.keys.items() if v and v.strip()}
    cid = client_id(request)
    own = bool(keys.get('flop') or keys.get('openrouter'))
    dup = hashlib.sha256(f'{cid}|{req.q.strip().lower()}|{req.model}|{req.compare_model}'.encode()).hexdigest()
    if dup in _recent and time.time() - _recent[dup][0] < DUP_WINDOW_S:
        return {**_recent[dup][1], 'cached': True}
    rate_limit(cid, own)

    srcs = [s for s in req.sources if s in SOURCES][:6] or ['keenable']
    compare = bool(req.compare_model and req.compare_model != req.model)
    quote = pricing.quote(srcs, compare=compare)
    t0 = time.time()
    results = await asyncio.gather(*[run_source(s, req.q, req.n, keys) for s in srcs])
    merged, report = [], []
    for key, items, err, dt in results:
        report.append({'source': key, 'label': SOURCES[key]['label'], 'count': len(items), 'error': err, 'time_s': dt})
        for it in items:
            if it.get('url') and all(it['url'] != m['url'] for m in merged):
                merged.append({**it, 'source': SOURCES[key]['label']})
    merged = merged[:12]
    for i, m in enumerate(merged):
        m['n'] = i + 1
    out = {'query': req.q, 'sources': merged, 'source_report': report, 'search_s': round(time.time() - t0, 2),
           'pricing': quote, 'answers': []}
    if merged:
        msgs = llm.build_messages(req.q, merged)
        models = [req.model] + ([req.compare_model] if req.compare_model and req.compare_model != req.model else [])
        answers = await asyncio.gather(*[llm.complete(client, m, msgs, req.max_tokens, keys) for m in models])
        for res in answers:
            res['paid_by'] = 'user' if (res.get('engine') == 'flop' and keys.get('flop')) or res.get('engine') == 'openrouter' else 'scout'
            # Scout's own charge for the service, separate from the miner's inference cost
            res['scout_charge_flop'] = quote['charged_flop']
            res['metering'] = quote['metering']
            # feed FLOP outcomes into the smart router
            if res.get('engine') == 'flop':
                rc = res.get('receipt') or {}
                router.record(rc.get('miner'), bool(res.get('answer')), res.get('latency_s') or 0)
            log(req.q, srcs, res, cid)
        out['answers'] = answers
    _recent[dup] = (time.time(), out)
    if len(_recent) > 5000:
        for k in sorted(_recent, key=lambda k: _recent[k][0])[:1000]:
            _recent.pop(k, None)
    return out


@app.get('/api/pricing')
def price_list():
    """Public price list — what Scout charges per service, in FLOP."""
    return pricing.catalogue()


@app.get('/api/router')
async def router_state():
    """Live miner ranking from the smart router — chain reliability + local perf."""
    return await router.routing_snapshot(client, top_n=25)


@app.get('/api/stats')
def stats():
    rows = db.execute('''select engine, model, count(*), sum(ok), avg(case when ok=1 then latency end)
                         from answers group by engine, model''').fetchall()
    miners = db.execute("select miner, count(*) from answers where engine='flop' and ok=1 group by miner").fetchall()
    paid = db.execute("select paid_by, count(*) from answers where ok=1 group by paid_by").fetchall()
    return {'models': [{'engine': e, 'model': m, 'calls': c, 'ok': s or 0, 'success_rate': round((s or 0) / c, 2),
                        'avg_latency_s': round(l, 1) if l else None} for e, m, c, s, l in rows],
            'answers_paid_by': {k or 'unknown': v for k, v in paid},
            'scout_charges': {'total_flop': round(db.execute('select coalesce(sum(scout_flop),0) from answers').fetchone()[0] or 0, 4),
                              'metering': pricing.METERING},
            'flop_sessions_by_miner': dict(miners)}


def _parse_dt(s):
    try:
        return datetime.fromisoformat((s or '').replace('Z', '+00:00'))
    except Exception:
        return datetime.fromtimestamp(0, timezone.utc)


EXPLORER = 'https://explorer.devnet.flop.network/api'
_miners_cache: dict = {'ts': 0, 'data': None}
_MINERS_TTL = 90  # seconds

# Gateway model catalog. Cached because /api/catalog and /api/miners both need
# it and neither can afford a 15-25s round trip per request on a 1 vCPU box.
_models_cache: dict = {'ts': 0, 'data': {}}
_MODELS_TTL = 60  # seconds


async def _models_snapshot() -> dict:
    """Gateway /v1/models, cached. Returns {} when the gateway is slow or down,
    which callers treat as 'no live prices' rather than an error."""
    now = time.time()
    if _models_cache['data'] and now - _models_cache['ts'] < _MODELS_TTL:
        return _models_cache['data']
    try:
        r = await client.get(f'{llm.FLOP_URL}/models', timeout=8)
        r.raise_for_status()
        _models_cache.update(ts=now, data=r.json())
    except Exception:
        # Serve stale data if we have any; a stale price beats a broken page.
        pass
    return _models_cache['data']


async def _fetch_all_miners() -> list:
    """Paginate explorer /api/miners (limit 100) in parallel."""
    first = await client.get(f'{EXPLORER}/miners', params={'limit': 100, 'offset': 0}, timeout=20)
    first.raise_for_status()
    j = first.json()
    out = list(j['data'])
    total = j['pagination']['total']
    if total > 100:
        offsets = range(100, total, 100)
        pages = await asyncio.gather(
            *[client.get(f'{EXPLORER}/miners', params={'limit': 100, 'offset': o}, timeout=20)
              for o in offsets],
            return_exceptions=True,
        )
        for p in pages:
            if isinstance(p, Exception): continue
            out.extend(p.json().get('data', []))
    return out


@app.get('/api/miners')
async def miner_explorer():
    """Aggregate FLOP miners: explorer registry (288) + gateway price_sources (30 priced) + local perf log."""
    now = datetime.now(timezone.utc)
    now_ms = time.time()

    # 1) cached full miner list
    if now_ms - _miners_cache['ts'] > _MINERS_TTL or not _miners_cache['data']:
        try:
            rows = await _fetch_all_miners()
            _miners_cache.update(ts=now_ms, data=rows)
        except Exception:
            if not _miners_cache['data']:
                raise
    all_registry: list = _miners_cache['data']

    # 2) live price snapshot — cached, and never fatal: on a slow box the gateway
    # can exceed 15s and a hard failure here would take the whole page down.
    models_json = await _models_snapshot()
    priced: dict[str, dict] = {}   # addr -> {models, best_price, active, version, capacity_tpm}
    models_out = []
    total_cap = 0
    gateway_countries: set[str] = set()
    for x in models_json.get('data', []):
        xf = x.get('x-flop', {})
        cap = xf.get('capacity_tpm') or 0
        total_cap += cap
        for dc in xf.get('datacenters', []):
            gateway_countries.add(dc.get('country_code'))
        ps_list = xf.get('price_sources', [])
        active_cnt = sum(1 for ps in ps_list if _parse_dt(ps.get('expires_at')) > now)
        models_out.append({'id': x['id'], 'name': x.get('name') or x['id'],
                           'miners': len(ps_list), 'active_miners': active_cnt,
                           'capacity_tpm': cap,
                           'price_prompt': x.get('pricing', {}).get('prompt')})
        for ps in ps_list:
            addr = ps.get('miner_address')
            if not addr: continue
            pm = priced.setdefault(addr, {'models': [], 'best_price': None, 'active': False,
                                          'version': ps.get('version'), 'capacity_tpm': 0})
            pm['models'].append(x['id'])
            pm['capacity_tpm'] = (pm['capacity_tpm'] or 0) + cap
            p_val = ps.get('flop_per_gn')
            if p_val and (pm['best_price'] is None or p_val < pm['best_price']):
                pm['best_price'] = p_val
            if _parse_dt(ps.get('expires_at')) > now:
                pm['active'] = True

    # 3) local perf from receipts log
    perf: dict[str, dict] = {}
    for miner, calls, ok, lat, spend, last in db.execute(
            "select miner, count(*), sum(ok), avg(case when ok=1 then latency end), "
            "sum(cast(json_extract(cost,'$') as real)), max(ts) from answers "
            "where engine='flop' and miner group by miner"):
        perf[miner] = {'calls': calls, 'ok': ok or 0,
                       'latency': round(lat, 1) if lat else None,
                       'spend': round(spend or 0, 2), 'last_seen': last}

    # 4) merge
    reg_map = {m['address']: m for m in all_registry}
    out = []
    for rec in all_registry:
        addr = rec['address']
        pm = priced.get(addr, {})
        pf = perf.get(addr, {})
        calls = pf.get('calls', 0)
        cal = rec.get('calibration') or {}
        inv = rec.get('declaredGpuInventory') or {}
        gpus = inv.get('gpus') or []
        stake_raw = int(rec.get('selfStake') or 0) + int(rec.get('sponsoredStake') or 0)
        out.append({
            'address': addr,
            # gateway price info
            'priced': bool(pm),
            'active': pm.get('active', False),
            'models': list(set(pm.get('models') or [])),
            'best_price': pm.get('best_price'),
            'capacity_tpm': pm.get('capacity_tpm'),
            # explorer chain info
            'is_active': rec.get('isActive', False),
            'blacklisted': rec.get('blacklisted', False),
            'strikes': rec.get('strikes', 0),
            'commission': rec.get('commission'),
            'stake_flop': round(stake_raw / 1e18, 1),
            'total_rewards': round(int(rec.get('totalRewards') or 0) / 1e18, 2),
            'total_slashed': round(int(rec.get('totalSlashed') or 0) / 1e18, 2),
            'registered_block': rec.get('registeredBlock'),
            # calibration
            'cal_phase': cal.get('phase'),
            'cal_lease': cal.get('leaseState'),
            'cal_tier': cal.get('assuranceTier'),
            'cap_gflops': cal.get('empiricalCapGflopsPerSec') or cal.get('capGflopsPerSec'),
            'verified_jobs': cal.get('verifiedJobs', 0),
            # GPU
            'gpus': [{'name': g.get('name'), 'count': g.get('count'), 'mem_gb': round((g.get('memoryMib') or 0) / 1024)} for g in gpus],
            'worker_version': (rec.get('workerBuild') or {}).get('version'),
            # local perf
            'calls': calls,
            'success_rate': round(pf.get('ok', 0) / calls, 2) if calls else None,
            'avg_latency_s': pf.get('latency'),
            'flop_spent': pf.get('spend', 0),
            'last_seen': pf.get('last_seen'),
        })

    # Inject router scores from cache; refresh in the background so this page
    # never waits on the explorer's reliability pagination.
    asyncio.create_task(router.refresh(client))
    router_miners = {m.address: m for m in router.STATE.ranked_miners()}
    for item in out:
        rm = router_miners.get(item['address'])
        if rm:
            item['router_score'] = round(rm.score, 3)
            item['router_rank'] = None  # filled below
        else:
            item['router_score'] = None
            item['router_rank'] = None

    # active-priced miners first, then by router score desc
    out.sort(key=lambda m: (not m['active'], not m['is_active'], -(m['router_score'] or 0), -(m['calls'] or 0)))
    for i, item in enumerate(out):
        if item.get('active'):
            item['router_rank'] = i + 1

    active_priced = sum(1 for m in out if m['active'])
    return {
        'network': {
            'total_miners': len(out),
            'priced_miners': len(priced),
            'active_priced': active_priced,
            'registered_active': sum(1 for m in out if m['is_active']),
            'capacity_tpm': total_cap,
            'models': len(models_out),
            'countries': sorted(c for c in gateway_countries if c),
        },
        'models': models_out,
        'miners': out,
    }


@app.get('/miners')
def miners_page():
    return FileResponse(BASE / 'miners.html')


@app.get('/')
def index():
    return FileResponse(BASE / 'index.html')
