"""Flop Scout — search chosen sources, answer with a chosen model, return citations + FLOP receipt.

Pages:  /        Answer  — ask, get a cited answer computed on FLOP
        /miners  Miners  — every miner ranked by real settlement performance

Run:  python -m uvicorn server:app --port 8787
API:  GET  /api/catalog   sources, presets, models
      GET  /api/pricing   the service catalogue, with availability
      POST /api/quote     price a request without spending anything
      POST /api/answer    {q, sources[], model, compare_model?, n, max_tokens, keys{...}}
      GET  /api/reliability  settlement deltas per miner over a window
      GET  /api/stats     success rate and latency per model, FLOP sessions by miner
      GET  /api/health
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                              RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import layout
import flop_gateway
import flop_estimate
import llm
import pricing
import src_router as router
from registry import PRESETS, SOURCES

BASE = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', BASE))
FREE_PER_DAY = int(os.getenv('FREE_ANSWERS_PER_DAY', '20'))       # answers/day/IP on Scout's wallet
USER_PER_MIN = int(os.getenv('USER_ANSWERS_PER_MIN', '6'))         # answers/min/IP with own key
DUP_WINDOW_S = int(os.getenv('DUPLICATE_WINDOW_S', '600'))         # same question again within 10 min -> cached
SITE = 'https://flopscout.com'


@asynccontextmanager
async def lifespan(_app):
    try:
        await router.startup(client)
    except Exception as e:
        print('router warm-up failed:', e)
    # Warm the model picker once so the first page load is not the one that pays
    # for a cold gateway round trip.
    asyncio.create_task(_warm_models())
    asyncio.create_task(_alert_loop())
    yield
    await client.aclose()


app = FastAPI(title='Flop Scout', version='0.5.0', lifespan=lifespan,
              docs_url='/api/swagger', openapi_url='/api/openapi.json', redoc_url=None)
# The UI is same-origin, so CORS only matters for agents calling the API from a
# browser context. Default to our own site rather than '*' — the API is public
# and readable by any client anyway, but a wildcard invites needless abuse.
app.add_middleware(CORSMiddleware,
                   allow_origins=[o.strip() for o in os.getenv(
                       'CORS_ORIGINS',
                       'https://flopscout.com,https://www.flopscout.com,http://127.0.0.1:8787,http://localhost:8787',
                   ).split(',') if o.strip()],
                   allow_methods=['GET', 'POST', 'DELETE'], allow_headers=['*'])

# The generated schema is an agent-facing contract, and FastAPI puts every
# route in it by default: the HTML pages, /sitemap.xml, /favicon.ico. A client
# that loads the schema to learn the API would find twelve entries that are
# web pages and try to parse HTML out of them. Keep /api/ only, and attach the
# llms.txt pointer so the two discovery surfaces agree.
def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version,
                         routes=app.routes,
                         description=('Web search, page reading and FLOP network data for AI agents, '
                                      'priced per call in FLOP. Miner reliability ranking with '
                                      'settle-rate alerts. Human guide: https://flopscout.com/llms.txt'))
    keep = {p: ops for p, ops in schema['paths'].items() if p.startswith('/api/')}
    schema['paths'] = keep
    schema['servers'] = [{'url': SITE}]
    schema['externalDocs'] = {'description': 'Guide for agents', 'url': f'{SITE}/llms.txt'}
    app.openapi_schema = schema
    return app.openapi_schema

app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
# An honest, identifiable User-Agent. Some upstreams answer 403 to Python's
# default agent (observed on api.flop.network for urllib); being explicit also
# means a rate-limited operator can see who is calling.
UA = {'User-Agent': f'flop-scout/{os.getenv("SCOUT_UA_VERSION", "0.5")} (+https://flopscout.com)'}
client = httpx.AsyncClient(timeout=30, follow_redirects=True, headers=UA)

DATA.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA / 'log.db', check_same_thread=False)
# One connection shared by async handlers and the two sync (threadpool) routes,
# so every statement goes through one lock. Without it SQLite raises under
# concurrent /api/answer + /api/stats traffic.
db_lock = threading.Lock()

with db_lock:
    db.execute('''create table if not exists answers(ts real, q text, sources text, engine text, model text, ok int,
                  miner text, prompt_tokens int, completion_tokens int, cost text, latency real, attempts int, error text)''')
    for col in ('paid_by text', 'client text', 'scout_flop real', 'metering text', 'scout_quote_flop real'):
        try:
            db.execute(f'alter table answers add column {col}')
        except sqlite3.OperationalError:
            pass
    # Settlement snapshots. This is what lets Scout say "this miner degraded over
    # the past week" instead of showing a single point in time.
    db.execute('create table if not exists reliability(ts real, miner text, settled int, timeout int, volume_gn int)')
    db.execute('create index if not exists reliability_miner_ts on reliability(miner, ts)')
    # Miner reliability alerts. address can be one specific SS58 address or the
    # special value '*' meaning "every miner in the registry". channel is
    # 'telegram' (message @handle) or 'twitter' (mention @handle) or 'site'
    # (shown only via GET /api/alerts/check, no outbound). threshold is the
    # settle-rate drop (0–1) that triggers. fired_at is the last trigger time.
    db.execute('''create table if not exists miner_alerts(
        id integer primary key autoincrement,
        created_at real, address text, threshold real, channel text, handle text,
        last_rate real, fired_at real, active int default 1)''')
    db.execute('create index if not exists miner_alerts_address on miner_alerts(address)')
    db.commit()

_hits = defaultdict(deque)       # ip -> timestamps
_recent = {}                     # hash(ip,q,model,sources,...) -> (ts, response)

_ROW_COLS = ('ts', 'q', 'sources', 'engine', 'model', 'ok', 'miner', 'prompt_tokens', 'completion_tokens',
             'cost', 'latency', 'attempts', 'error', 'paid_by', 'client', 'scout_flop', 'metering', 'scout_quote_flop')


class AnswerReq(BaseModel):
    q: str = Field(min_length=2, max_length=500)
    sources: list[str] = ['keenable', 'flop_eco']
    model: str = 'flop:qwen/qwen2.5-0.5b-instruct'
    compare_model: str | None = None
    n: int = Field(3, ge=1, le=8)
    max_tokens: int = Field(250, ge=32, le=800)
    keys: dict[str, str] = {}    # caller's own keys; never stored/logged
    # Supported key names: flop, openrouter, exa, keenable, github, tavily, brave, firecrawl, serper

    @field_validator('model', 'compare_model')
    @classmethod
    def _provider_prefix(cls, v):
        # llm.complete() splits on ':' — a model id without a known provider used
        # to raise inside the request and surface as a 500 instead of a 422.
        if v is None or v == '':
            return v
        if ':' not in v:
            raise ValueError('model must be "flop:<id>" or "openrouter:<id>"')
        if v.split(':', 1)[0] not in ('flop', 'openrouter'):
            raise ValueError('provider must be flop or openrouter')
        return v


# Stand-in for "what search usually returns", used only to size a quote.
# Calibrated against prod on 2026-11-15: three keenable results made a 655-token
# prompt against 58 for the bare question, so roughly 200 tokens per source.
# These are not shown to anyone and never reach a model; they only give the
# gateway's estimator a prompt of realistic length to count.
_TYPICAL_SNIPPET = ('The FLOP network settles inference between agents and miners on chain. '
                    'Miners register a GPU, stake FLOP, and are scored by how many sessions '
                    'they settle versus time out. Agents pay per request from their own wallet. ') * 2
_TYPICAL_SOURCES = [{'n': i + 1, 'title': 'FLOP network overview and miner reliability',
                     'url': 'https://docs.flop.network/docs/overview', 'source': 'Keenable',
                     'snippet': _TYPICAL_SNIPPET} for i in range(3)]


class QuoteReq(BaseModel):
    sources: list[str] = ['keenable', 'flop_eco']
    compare_model: str | None = None
    cache_hit: bool = False
    keys: dict[str, str] = {}    # only presence is read, to report the right quota tier
    # Optional: adds the *miner's* cost to the quote, from the gateway's own
    # zero-spend estimator. Without a question there is nothing to count tokens
    # over, so this stays None rather than 0 — see flop_estimate.
    q: str | None = Field(None, max_length=500)
    model: str = 'flop:qwen/qwen2.5-0.5b-instruct'
    max_tokens: int = Field(250, ge=32, le=800)


# ── Auto source choice ──────────────────────────────────────────────────────
# sources: ["auto"] lets Scout pick sources from the question. It only ever picks
# free sources (no upstream bill, no caller key), so an auto request costs exactly
# the answer fee. Paid sources are never added implicitly.
AUTO_KEYWORDS = [
    (('crypto', 'coin', 'token', 'price', 'defi', 'tvl', 'market cap', 'bitcoin', 'ethereum'), ['crypto', 'defillama']),
    (('github', 'repo', 'library', 'framework', 'sdk', 'open-source', 'open source', 'package'), ['github', 'hn']),
    (('paper', 'arxiv', 'research', 'study', 'benchmark', 'what is', 'who is'), ['arxiv', 'wikipedia']),
]


def _is_free_source(sid: str) -> bool:
    spec = SOURCES.get(sid)
    return bool(spec) and not spec.get('key') and pricing.price_for(sid).price_flop == 0


def auto_sources(q: str) -> list[str]:
    ql = (q or '').lower()
    picked = ['keenable'] if _is_free_source('keenable') else []
    for words, ids in AUTO_KEYWORDS:
        if any(w in ql for w in words):
            for sid in ids:
                if sid not in picked and _is_free_source(sid):
                    picked.append(sid)
    return picked[:3] or ['keenable']


def client_id(request: Request):
    """Stable per-caller id for rate limiting and the duplicate cache.

    request.client is already the real peer: uvicorn runs with --proxy-headers
    and trusts 127.0.0.1, so nginx's X-Forwarded-For is applied there. Reading
    the header directly here meant any caller could send a fresh
    `X-Forwarded-For` per request and get an unlimited free tier.
    """
    peer = (request.client.host if request.client else '') or '?'
    ip = peer
    if peer in ('127.0.0.1', '::1', 'localhost'):
        # Only reached if uvicorn did not apply proxy headers. nginx appends
        # $remote_addr as the LAST entry; everything before it is whatever the
        # client chose to send, so the first entry must never be trusted.
        fwd = request.headers.get('x-forwarded-for', '')
        if fwd:
            ip = fwd.split(',')[-1].strip() or peer
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def rate_limit(cid: str, own_key: bool) -> dict:
    '''Consume one slot and return the caller's quota state.

    The state is echoed in X-RateLimit-* headers so an agent can pace itself
    instead of discovering the limit by being refused.
    '''
    now = time.time()
    dq = _hits[cid]
    while dq and now - dq[0] > 86400:
        dq.popleft()
    if own_key:
        used = sum(1 for t in dq if now - t < 60)
        if used >= USER_PER_MIN:
            raise HTTPException(429, f'Limit: {USER_PER_MIN} answers per minute. Slow down — FLOP counts genuine use, not volume.',
                                headers={'Retry-After': '60'})
        dq.append(now)
        return {'limit': USER_PER_MIN, 'remaining': max(0, USER_PER_MIN - used - 1), 'window': '60s',
                'tier': 'own-key', 'slot': now}
    if len(dq) >= FREE_PER_DAY:
        raise HTTPException(429, f'Free tier: {FREE_PER_DAY} answers per day. Connect your agent wallet to continue.',
                            headers={'Retry-After': '3600'})
    dq.append(now)
    # The counter map is keyed by client and would otherwise grow forever.
    if len(_hits) > 20000:
        for k in [k for k, d in _hits.items() if not d or now - d[-1] > 86400]:
            _hits.pop(k, None)
    return {'limit': FREE_PER_DAY, 'remaining': max(0, FREE_PER_DAY - len(dq)), 'window': '24h',
            'tier': 'free', 'slot': now}


def peek_quota(cid: str, own_key: bool) -> dict:
    """Same shape as rate_limit() without consuming anything."""
    now = time.time()
    dq = _hits.get(cid) or ()
    if own_key:
        used = sum(1 for t in dq if now - t < 60)
        return {'limit': USER_PER_MIN, 'remaining': max(0, USER_PER_MIN - used), 'window': '60s', 'tier': 'own-key'}
    used = sum(1 for t in dq if now - t < 86400)
    return {'limit': FREE_PER_DAY, 'remaining': max(0, FREE_PER_DAY - used), 'window': '24h', 'tier': 'free'}


def refund(cid: str, slot: float) -> None:
    """Give a rate-limit slot back. Used when no answer was produced: devnet
    congestion is not the caller's fault and must not burn their free tier."""
    try:
        _hits[cid].remove(slot)
    except (KeyError, ValueError):
        pass


def _quota_headers(response, quota: dict) -> None:
    response.headers['X-RateLimit-Limit'] = str(quota['limit'])
    response.headers['X-RateLimit-Remaining'] = str(quota['remaining'])
    response.headers['X-RateLimit-Window'] = quota['window']
    response.headers['X-RateLimit-Tier'] = quota['tier']


def log(q, srcs, res, cid):
    u, rc = res.get('usage') or {}, res.get('receipt') or {}
    row = (time.time(), q, ','.join(srcs), res.get('engine'), res.get('model'), int(bool(res.get('answer'))),
           rc.get('miner'), u.get('prompt_tokens'), u.get('completion_tokens'),
           json.dumps(u.get('cost_flop') or u.get('cost')), res.get('latency_s'), res.get('attempts'),
           (res.get('error') or '')[:300], res.get('paid_by'), cid, res.get('scout_charge_flop'),
           res.get('metering'), res.get('scout_quote_flop'))
    with db_lock:
        db.execute(f'insert into answers ({",".join(_ROW_COLS)}) values ({",".join("?" * len(_ROW_COLS))})', row)
        db.commit()


async def run_source(key, q, n, keys):
    t = time.time()
    try:
        items = await asyncio.wait_for(SOURCES[key]['fn'](client, q, n, keys), 25)
        return key, items, None, round(time.time() - t, 2)
    except Exception as e:
        return key, [], f'{type(e).__name__}: {str(e)[:120]}', round(time.time() - t, 2)


@app.middleware('http')
async def _responses(request, call_next):
    resp = await call_next(request)
    # Answers depend on live network state: never let a proxy or browser replay one.
    if request.url.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return resp


# The key probe is a network round trip (0.8 s warm, 6 s cold). A monitor that
# polls /api/health must not turn into one gateway call per poll, so the verdict
# is cached. It is keyed on the key itself: a rotated key is re-checked at once.
# A failure is kept for less time so recovery shows up quickly.
_HEALTH_OK_TTL = 60
_HEALTH_FAIL_TTL = 15
_health_cache = {'key': None, 'ts': 0.0, 'accepted': False}
_health_lock = asyncio.Lock()


async def _key_accepted(key: str) -> tuple[bool, float]:
    """(accepted, age of that verdict in seconds)."""
    def fresh():
        if _health_cache['key'] != key:
            return False
        ttl = _HEALTH_OK_TTL if _health_cache['accepted'] else _HEALTH_FAIL_TTL
        return time.time() - _health_cache['ts'] < ttl

    if not fresh():
        async with _health_lock:
            if not fresh():          # another request may have probed while we waited
                accepted = False
                try:
                    async with httpx.AsyncClient(timeout=8) as probe:
                        pr = await probe.get(f'{llm.FLOP_URL}/keys/me',
                                             headers={'Authorization': f'Bearer {key}'})
                    accepted = pr.status_code == 200
                except Exception:
                    accepted = False
                _health_cache.update(key=key, ts=time.time(), accepted=accepted)
    return _health_cache['accepted'], time.time() - _health_cache['ts']


@app.get('/api/health')
async def health():
    """Liveness plus whether the gateway actually accepts the key.

    `flop_key` used to mean "a key string is configured". `flop_key_accepted`
    means the gateway answered 200 to an authenticated request, so a revoked key
    shows red. `flop_key_checked_s_ago` says how old that verdict is.
    """
    key = llm.flop_key()
    accepted, age = (await _key_accepted(key)) if key else (False, 0.0)
    return {'status': 'ok' if accepted else 'degraded', 'version': app.version,
            'flop_key': bool(key), 'flop_key_accepted': accepted,
            'flop_key_checked_s_ago': round(age, 1),
            'metering': pricing.METERING, 'sources': len(SOURCES)}


@app.get('/api/catalog')
async def catalog():
    # Page-load endpoints answer from cache and refresh behind the response: a
    # cold gateway costs seconds, and this renders on every navigation.
    asyncio.create_task(router.refresh(client))
    snap = router.cached_snapshot(top_n=5)
    models = llm.cached_models()
    if not models:
        models = await llm.list_models(client)   # first paint only
    else:
        asyncio.create_task(llm.list_models(client))
    return {'sources': [{'id': k, **{x: v for x, v in s.items() if x != 'fn'}} for k, s in SOURCES.items()],
            'presets': PRESETS, 'models': models,
            'router': {'best_miner': snap['best'], 'active_priced': snap['active_priced'],
                       'prices_known': snap.get('prices_known', True),
                       'chain_miners': snap.get('chain_miners')}}


@app.post('/api/quote')
async def quote_preview(req: QuoteReq, request: Request, response: Response):
    """Price a request before spending anything — no search, no inference, no ledger row.

    An agent that cannot read a price before committing is not autonomous, it is
    optimistic. This is the cheapest endpoint on the box.

    With `q` supplied it also reports the *miner's* inference cost, from the
    gateway's own estimator. That call needs no key and spends nothing, so the
    two numbers Scout has always kept apart are shown here side by side before
    anything happens at all.
    """
    # auto quotes as the keenable floor: auto only adds free sources, so the total is exact either way
    explicit = [s for s in req.sources if s in SOURCES][:6]
    srcs = ['keenable'] if ('auto' in req.sources and not explicit) else (explicit or ['keenable'])
    compare = bool(req.compare_model)
    q = pricing.quote(srcs, compare=compare, cache_hit=req.cache_hit)
    own = bool((req.keys.get('flop') or '').strip() or (req.keys.get('openrouter') or '').strip())
    quota = peek_quota(client_id(request), own)
    _quota_headers(response, quota)

    inference = None
    if req.q and req.q.strip() and req.model.startswith('flop:'):
        # A quote cannot know what search will return, and the prompt size drives
        # the cost. Measured on prod: 0 sources = 58 input tokens, 1 = 138,
        # 3 = 655, 9 = 965 — and it varies ~2.5x per source by provider. One
        # number would be wrong by a factor of three either way, so report both
        # ends and say what each assumes.
        mid = req.model.split(':', 1)[1]
        floor_est, typ_est = await asyncio.gather(
            flop_estimate.estimate(client, mid, llm.build_messages(req.q, []), req.max_tokens),
            flop_estimate.estimate(client, mid, llm.build_messages(req.q, _TYPICAL_SOURCES), req.max_tokens))
        floor_s, typ_s = flop_estimate.summarise(floor_est), flop_estimate.summarise(typ_est)
        if floor_s or typ_s:
            inference = {'floor': floor_s, 'typical': typ_s,
                         'floor_assumes': 'no sources in the prompt (lower bound)',
                         'typical_assumes': '3 sources of ordinary snippet length'}

    return {**q, 'sources': srcs, 'quota': quota,
            'inference': inference,
            'inference_note': (None if inference is None else
                               'Miner cost, estimated by the FLOP gateway for this prompt. '
                               'Separate from Scout\'s fee above, and spent from the caller wallet to the miner.'),
            'limits': {'sources': 6, 'n': {'min': 1, 'max': 8}, 'max_tokens': {'min': 32, 'max': 800},
                       'q_chars': {'min': 2, 'max': 500}},
            'note': ('Charges are nominal on devnet: computed, displayed and logged, never debited.'
                     if pricing.METERING == 'nominal' else 'Charges settle on-chain.')}


@app.post('/api/answer')
async def answer(req: AnswerReq, request: Request, response: Response):
    keys = {k: v.strip() for k, v in req.keys.items() if v and v.strip()}
    cid = client_id(request)
    own = bool(keys.get('flop') or keys.get('openrouter'))
    srcs = auto_sources(req.q) if 'auto' in req.sources else ([s for s in req.sources if s in SOURCES][:6] or ['keenable'])

    # The cache key has to cover everything that changes the work: two questions
    # with different sources or limits are not the same request.
    dup = hashlib.sha256('|'.join([cid, req.q.strip().lower(), req.model, req.compare_model or '',
                                   ','.join(sorted(srcs)), str(req.n), str(req.max_tokens)]).encode()).hexdigest()
    if dup in _recent and time.time() - _recent[dup][0] < DUP_WINDOW_S:
        hit = _recent[dup][1]
        # A cache hit is free: nothing was searched, nothing was computed.
        return {**hit, 'cached': True, 'pricing': pricing.quote([], cache_hit=True), 'answers': [
            {**a, 'scout_charge_flop': None, 'scout_quote_flop': 0.0, 'cached': True}
            for a in hit.get('answers', [])]}
    quota = rate_limit(cid, own)

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
        models = [req.model] + ([req.compare_model] if compare else [])
        # Price the inference from the gateway's zero-spend estimator, and run it
        # *concurrently* with the completion. It is advisory — it belongs on the
        # receipt next to Scout's own fee — so making the caller wait for it
        # twice over bought nothing. Only flop models are estimated; gather()
        # refuses bare None, so the non-flop slots go through a coroutine.
        async def _price(m):
            if not m.startswith('flop:'):
                return None
            return await flop_estimate.estimate(client, m.split(':', 1)[1], msgs, req.max_tokens)
        answers, ests = await asyncio.gather(
            asyncio.gather(*[llm.complete(client, m, msgs, req.max_tokens, keys) for m in models]),
            asyncio.gather(*[_price(m) for m in models]),
        )
        for res, est in zip(answers, ests):
            res['inference_estimate'] = flop_estimate.summarise(est)
            res['paid_by'] = 'user' if (res.get('engine') == 'flop' and keys.get('flop')) or res.get('engine') == 'openrouter' else 'scout'
            # Scout's own charge for the service, separate from the miner's
            # inference cost. `quoted` is always the computed price; `charge` is
            # what would actually be debited, which is None while metering is
            # nominal. Logging only the second one left the ledger permanently
            # at zero and made /api/stats contradict the UI.
            res['scout_quote_flop'] = quote['total_flop']
            res['scout_charge_flop'] = quote['charged_flop']
            res['metering'] = quote['metering']
            # llm.complete() already feeds the router on success; recording here
            # as well silently doubled every local call counter.
            log(req.q, srcs, res, cid)
        out['answers'] = answers

    answered = any(a.get('answer') for a in out['answers'])
    if answered:
        # Only a real answer is worth replaying. Caching a congestion failure
        # used to hand the caller the same error, marked "cached", for ten
        # minutes — directly against the advice to retry shortly.
        _recent[dup] = (time.time(), out)
        if len(_recent) > 5000:
            for k in sorted(_recent, key=lambda k: _recent[k][0])[:1000]:
                _recent.pop(k, None)
    else:
        refund(cid, quota['slot'])
        quota = peek_quota(cid, own)
    _quota_headers(response, quota)
    out['quota'] = {k: v for k, v in quota.items() if k != 'slot'}
    return out


@app.get('/api/pricing')
def price_list():
    """Public price list — what Scout charges per service, in FLOP.

    Services the registry dropped (no backend configured) are still listed but
    flagged unavailable: a price for something you cannot buy is a bug, and
    silently hiding it is a different bug.
    """
    cat = pricing.catalogue()
    for s in cat['services']:
        s['available'] = s['id'] in SOURCES
    cat['available'] = sum(1 for s in cat['services'] if s['available'])
    cat['total'] = len(cat['services'])
    return cat


@app.get('/api/router')
async def router_state():
    """Live miner ranking from the smart router — chain reliability + local perf."""
    snap = router.cached_snapshot(top_n=25)
    if not snap['refreshed_at']:
        await router.refresh(client)          # nothing cached yet: pay for it once
        snap = router.cached_snapshot(top_n=25)
    else:
        asyncio.create_task(router.refresh(client))
    return snap


@app.get('/api/inference-prices')
async def inference_prices(max_tokens: int = 250):
    """What every FLOP model costs to run, from the gateway's own estimator.

    What this adds over `/v1/models` is the arithmetic a unit price cannot
    express: whole-request FLOP cost, the token split, GFLOPs and the gateway's
    latency estimate. Measured, `estimated_cost_flop` is exactly
    `pricing.prompt x total_tokens` — no hidden floor or multiplier — so the
    number a caller budgets against is the number that will be charged.

    And it costs nothing to ask. The operation needs no key and spends nothing,
    so this answers while the devnet allowance is dead, which no other FLOP call
    here does.

    `available_miners` is included but is *not* an independent capacity signal:
    measured against every live model it equals the count of `price_sources`
    exactly. It is here as a cross-check on Scout's own parse, and the UI should
    not present it as a second opinion.

    `max_tokens` is clamped to the same 32–800 range `/api/answer` accepts, so a
    quote here cannot describe a request that endpoint would refuse.
    """
    max_tokens = max(32, min(max_tokens, 800))
    models = await llm.flop_models(client)
    probe = [{'role': 'system', 'content': llm.SYSTEM},
             {'role': 'user', 'content': 'How many miners are online on the FLOP network right now?'}]
    ests = await asyncio.gather(*[
        flop_estimate.estimate(client, m['model'], probe, max_tokens) for m in models])
    out = []
    for m, est in zip(models, ests):
        s = flop_estimate.summarise(est)
        out.append({'model': m['model'], 'label': m.get('label'),
                    'price_per_token': m.get('price'), 'capacity_tpm': m.get('capacity_tpm'),
                    'miners_priced': m.get('miners'), 'blocked': m.get('blocked'),
                    'estimate': s})
    priced = [o for o in out if o['estimate']]
    return {'max_tokens': max_tokens,
            'metering': pricing.METERING,
            'models': out,
            'estimated': len(priced),
            'total': len(out),
            # Equals price_sources per model — a cross-check on Scout's parse,
            # not an independent capacity reading. Documented as such above.
            'available_miners_by_model': flop_estimate.capacity_by_model(
                {o['model']: o['estimate'] for o in priced}),
            'note': ('Costs are the FLOP gateway\'s estimate for a probe prompt at this '
                     'max_tokens, spent from the caller wallet to the miner. Equal to '
                     'unit price x total tokens. Scout\'s own fee is separate — see '
                     '/api/pricing.')}


@app.get('/api/reliability')
def reliability_history(window: int = 86400, limit: int = 50):
    """How each miner's settlement rate moved over the window.

    The router has always been snapshot-only. This is the history it lacked:
    deltas between Scout's own settled/timeout snapshots, not the chain total.
    """
    window = max(3600, min(window, 30 * 86400))
    limit = max(1, min(limit, 500))
    start = time.time() - window
    with db_lock:
        rows = db.execute('''select miner, min(ts), max(ts), min(settled), max(settled),
                                    min(timeout), max(timeout), min(volume_gn), max(volume_gn), count(*)
                             from reliability where ts >= ? group by miner''', (start,)).fetchall()
        samples = db.execute('select count(distinct ts) from reliability where ts >= ?', (start,)).fetchone()[0]
    out = []
    for miner, t0, t1, s0, s1, to0, to1, v0, v1, n in rows:
        new_settled, new_timeout = s1 - s0, to1 - to0
        if new_settled + new_timeout <= 0 or n < 2:
            continue
        # Two different questions, two different numbers: what happened inside
        # the window, and the miner's lifetime rate. Reporting the lifetime rate
        # next to windowed session counts was quietly misleading.
        window_rate = new_settled / (new_settled + new_timeout)
        life_now = s1 / max(s1 + to1, 1)
        # A miner with zero prior sessions has no baseline. Reporting +100% for
        # "was new, is now perfect" is a fabricated delta.
        life_then = (s0 / (s0 + to0)) if (s0 + to0) > 0 else None
        out.append({'miner': miner, 'span_s': round(t1 - t0), 'settled': new_settled, 'timeouts': new_timeout,
                    'sessions': new_settled + new_timeout,
                    'settle_rate': round(window_rate, 3),
                    'settle_rate_change': None if life_then is None else round(life_now - life_then, 3),
                    'lifetime_rate': round(life_now, 3),
                    'volume_gn': max(0, (v1 or 0) - (v0 or 0)), 'samples': n})
    out.sort(key=lambda m: (m['settle_rate'], m['sessions']), reverse=True)
    return {'window_s': window, 'snapshots': samples, 'miners': out[:limit],
            'note': ('Deltas between Scout snapshots, taken at most every 15 minutes. '
                     'History starts after the second snapshot.')}


@app.get('/api/stats')
def stats():
    with db_lock:
        rows = db.execute('''select engine, model, count(*), sum(ok), avg(case when ok=1 then latency end)
                             from answers group by engine, model''').fetchall()
        miners = db.execute("select miner, count(*) from answers where engine='flop' and ok=1 group by miner").fetchall()
        paid = db.execute("select paid_by, count(*) from answers where ok=1 group by paid_by").fetchall()
        quoted, charged, answers = db.execute(
            'select coalesce(sum(scout_quote_flop),0), coalesce(sum(scout_flop),0), count(*) from answers').fetchone()
        ok_answers = db.execute('select count(*) from answers where ok=1').fetchone()[0]
    return {'models': [{'engine': e, 'model': m, 'calls': c, 'ok': s or 0, 'success_rate': round((s or 0) / c, 2),
                        'avg_latency_s': round(l, 1) if l else None} for e, m, c, s, l in rows],
            'answers': answers,
            'answers_paid_by': {k or 'unknown': v for k, v in paid},
            'scout_charges': {'quoted_flop': round(quoted, 4), 'charged_flop': round(charged, 4),
                              'metering': pricing.METERING},
            'answered': ok_answers,
            'flop_sessions_by_miner': dict(miners)}


@app.get('/api/analytics')
def analytics(days: int = 30):
    """Demand aggregates for the public dashboard.

    Never returns question text (`q`) or client identifiers (`client` is the
    caller's IP). Only counts, shares and per-day totals leave this function.
    """
    days = max(1, min(days, 90))
    since = time.time() - days * 86400
    with db_lock:
        daily = db.execute('''select date(ts,'unixepoch') d, count(*), sum(ok), count(distinct client),
                                     sum(case when paid_by='user' then 1 else 0 end),
                                     coalesce(sum(scout_quote_flop),0)
                              from answers where ts>=? group by d order by d''', (since,)).fetchall()
        clients = db.execute('''select client, count(distinct date(ts,'unixepoch')) from answers
                                where ts>=? and client is not null group by client''', (since,)).fetchall()
        srcs = db.execute('select sources, ok from answers where ts>=?', (since,)).fetchall()
        engines = db.execute('select engine, count(*) from answers where ts>=? group by engine', (since,)).fetchall()
    from collections import Counter
    src_count: Counter = Counter()
    src_ok: Counter = Counter()
    for s, ok in srcs:
        for name in filter(None, (s or '').split(',')):
            src_count[name] += 1
            src_ok[name] += int(bool(ok))
    total_clients = len(clients)
    returning = sum(1 for _, n in clients if n >= 2)
    total_answers = sum(r[1] for r in daily)
    return {
        'window_days': days,
        'metering': pricing.METERING,
        'totals': {
            'answers': total_answers,
            'answered': sum(r[2] or 0 for r in daily),
            'unique_clients': total_clients,
            'paid_by_user_share': round(sum(r[4] or 0 for r in daily) / total_answers, 3) if total_answers else None,
            'quoted_flop': round(sum(r[5] or 0 for r in daily), 4),
        },
        # Clients active on two or more distinct days in the window. This is a
        # within-window proxy, not cohort retention: the log has no cohort anchor.
        'retention': {
            'returning_clients': returning,
            'returning_share': round(returning / total_clients, 3) if total_clients else None,
        },
        'daily': [{'day': d, 'answers': n, 'answered': o or 0, 'clients': c, 'paid_by_user': pu or 0,
                   'quoted_flop': round(q or 0, 4)} for d, n, o, c, pu, q in daily],
        'sources': [{'id': k, 'calls': v, 'ok': src_ok[k],
                     'success_rate': round(src_ok[k] / v, 2) if v else None}
                    for k, v in src_count.most_common(12)],
        'engines': {e or 'unknown': c for e, c in engines},
        'note': ('Aggregates over the log. Question text and client identifiers are never exposed. '
                 'Quoted FLOP is computed, not debited, while metering is nominal.'),
    }


def _parse_dt(s):
    try:
        return datetime.fromisoformat((s or '').replace('Z', '+00:00'))
    except Exception:
        return datetime.fromtimestamp(0, timezone.utc)


EXPLORER = 'https://explorer.devnet.flop.network/api'
_miners_cache: dict = {'ts': 0, 'data': None}
_MINERS_TTL = 90  # seconds
_miners_lock = asyncio.Lock()

# Gateway model catalog. Cached because /api/catalog and /api/miners both need
# it and neither can afford a 15-25s round trip per request on a 1 vCPU box.

_SNAP_EVERY = 900   # write a reliability snapshot at most every 15 minutes


def persist_chain(rows: list) -> None:
    """Persist one settlement snapshot. Wired to the router's refresh callback."""
    now = time.time()
    clean = [(now, r['miner'], int(r.get('settledCount') or 0), int(r.get('timeoutCount') or 0),
              int(r.get('settledVolumeGn') or 0)) for r in rows if r.get('miner')]
    if not clean:
        return
    with db_lock:
        last = db.execute('select max(ts) from reliability').fetchone()[0] or 0
        if now - last < _SNAP_EVERY:
            return
        db.executemany('insert into reliability(ts,miner,settled,timeout,volume_gn) values (?,?,?,?,?)', clean)
        db.execute('delete from reliability where ts < ?', (now - 30 * 86400,))
        db.commit()


router.on_chain_refresh = persist_chain


async def _fire_alerts() -> None:
    """Check all active alerts against the latest reliability snapshot.

    Called once per hour from the lifespan background task. Reads the last
    15-minute settlement delta for every miner, compares to each alert's
    threshold, and fires outbound notifications.

    Telegram delivery uses the Bot API `sendMessage` call. If the token is
    not set, the trigger is recorded in `fired_at` anyway so site-channel
    pollers see it via GET /api/alerts/check.
    """
    rmap = _reliability_map(window=7200)      # 2 h: snapshots are 15 min apart, need >= 2 points
    if not rmap:
        return
    now = time.time()
    with db_lock:
        active = db.execute(
            'select id, address, threshold, channel, handle, last_rate, fired_at '
            'from miner_alerts where active = 1').fetchall()

    for alert_id, addr, threshold, channel, handle, last_rate, fired_at in active:
        # Gather the miners this alert covers
        targets = {addr: rmap[addr]} if addr != '*' else rmap
        for miner_addr, stats in targets.items():
            rate = stats.get('settle_rate')
            if rate is None:
                continue
            crossed = rate < threshold
            # Hysteresis: if already fired, require recovery above threshold + gap
            if fired_at and not crossed:
                recovered = rate >= threshold + _ALERT_HYSTERESIS
                if not recovered:
                    continue   # still below recovery line, do not re-arm
            if not crossed:
                # Update last_rate but do not fire
                with db_lock:
                    db.execute('update miner_alerts set last_rate=? where id=?',
                               (rate, alert_id))
                    db.commit()
                continue
            # --- FIRE ---
            msg = (f'⚠️ FLOP miner alert\n'
                   f'Miner: {miner_addr[:12]}…\n'
                   f'Settle rate: {rate:.1%} (threshold {threshold:.0%})\n'
                   f'Via flopscout.com/miners')
            if channel == 'telegram' and _TG_TOKEN and handle:
                try:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=15) as _c:
                        await _c.post(
                            f'https://api.telegram.org/bot{_TG_TOKEN}/sendMessage',
                            json={'chat_id': handle, 'text': msg, 'parse_mode': 'Markdown'})
                except Exception as e:
                    print('alert telegram failed:', repr(e))
            with db_lock:
                db.execute('update miner_alerts set last_rate=?, fired_at=? where id=?',
                           (rate, now, alert_id))
                db.commit()


_ALERT_LAST_RUN = 0.0
_ALERT_INTERVAL = 3600   # check once per hour


async def _alert_loop() -> None:
    """Long-running background coroutine that checks alerts once per hour."""
    global _ALERT_LAST_RUN
    while True:
        await asyncio.sleep(60)        # wake up every minute, check if an hour has passed
        if time.time() - _ALERT_LAST_RUN < _ALERT_INTERVAL:
            continue
        _ALERT_LAST_RUN = time.time()
        try:
            await _fire_alerts()
        except Exception as e:
            print('_fire_alerts error:', repr(e))


def _reliability_series(window: int = 86400, max_points: int = 24) -> tuple[dict, list]:
    """Sparkline data from Scout's own settlement snapshots.

    Returns (per_miner, network).

    per_miner[address] is the settle rate inside the window *up to* each snapshot:
    (settled since window start) / (sessions since window start). It ends on the
    same number the card prints as the 24h settle rate, so the line and the label
    can never disagree. A per-interval rate was tried first and rejected: most
    miners see a handful of sessions per interval, which draws noise, not a trend.

    network is one point per snapshot interval, summed over every miner present
    in both snapshots. Hundreds of sessions per interval, so that one is plotted
    per interval and does show short-term swings.
    """
    start = time.time() - window
    with db_lock:
        rows = db.execute('select miner, ts, settled, timeout from reliability where ts >= ? order by miner, ts',
                          (start,)).fetchall()
    per_miner: dict = {}
    net: dict = {}
    first = prev = None
    for miner, ts, settled, timeout in rows:
        if first is None or first[0] != miner:
            first = prev = (miner, ts, settled, timeout)
            continue
        ds, dt = settled - first[2], timeout - first[3]
        if ds >= 0 and dt >= 0 and ds + dt > 0:
            per_miner.setdefault(miner, []).append(round(ds / (ds + dt), 3))
        ps, pt = settled - prev[2], timeout - prev[3]
        if ps >= 0 and pt >= 0:
            agg = net.setdefault(ts, [0, 0])
            agg[0] += ps
            agg[1] += pt
        prev = (miner, ts, settled, timeout)
    network = [{'ts': round(ts), 'settled': a, 'timeouts': b, 'sessions': a + b,
                'settle_rate': round(a / (a + b), 3)}
               for ts, (a, b) in sorted(net.items()) if a + b > 0]
    return ({m: pts[-max_points:] for m, pts in per_miner.items() if len(pts) >= 2},
            network[-max_points:])


def _reliability_map(window: int = 86400) -> dict:
    """Cheap per-miner delta lookup for the miners page."""
    start = time.time() - window
    out = {}
    with db_lock:
        rows = db.execute('''select miner, min(settled), max(settled), min(timeout), max(timeout), count(*)
                             from reliability where ts >= ? group by miner''', (start,)).fetchall()
    for miner, s0, s1, to0, to1, n in rows:
        new_settled, new_timeout = s1 - s0, to1 - to0
        sessions = new_settled + new_timeout
        if n < 2 or sessions <= 0:
            continue
        life_now = s1 / max(s1 + to1, 1)
        life_then = (s0 / (s0 + to0)) if (s0 + to0) > 0 else None
        out[miner] = {'sessions': sessions, 'settled': new_settled, 'timeouts': new_timeout,
                      'settle_rate': round(new_settled / sessions, 3),
                      'lifetime_rate': round(life_now, 3),
                      'settle_rate_change': None if life_then is None else round(life_now - life_then, 3)}
    return out


async def _warm_models() -> None:
    try:
        await llm.list_models(client)
    except Exception:
        pass


async def _models_snapshot() -> dict | None:
    """Gateway /v1/models via the one shared cache in flop_gateway.

    Returns None only when no snapshot has *ever* succeeded, which callers must
    render as unknown rather than zero.
    """
    return await flop_gateway.snapshot(client)


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
            if isinstance(p, Exception):
                continue
            out.extend(p.json().get('data', []))
    return out


async def _refresh_miners() -> bool:
    """Serialised refresh so a burst of page loads cannot fan out into a stampede."""
    async with _miners_lock:
        try:
            rows = await _fetch_all_miners()
        except Exception as e:
            print('miner refresh failed:', e)
            return False
        _miners_cache.update(ts=time.time(), data=rows)
        return True


@app.get('/api/miners')
async def miner_explorer():
    """Aggregate FLOP miners: explorer registry + gateway price_sources + local perf log."""
    now = datetime.now(timezone.utc)
    now_ms = time.time()

    # 1) miner list — stale-while-revalidate. Waiting on four explorer pages
    #    (~2 000 rows on a cold cache) was the slowest thing on the site.
    stale = now_ms - _miners_cache['ts'] > _MINERS_TTL
    if not _miners_cache['data']:
        if not await _refresh_miners():
            raise HTTPException(503, 'the FLOP explorer is unreachable right now')
    elif stale:
        asyncio.create_task(_refresh_miners())
    all_registry: list = _miners_cache['data'] or []
    # A refresh started inside this request makes _miners_cache['ts'] newer than
    # now_ms, which rendered as "-7s old". Recompute after the cache settled.
    age_s = max(0, round(time.time() - (_miners_cache['ts'] or time.time())))

    # 2) live price snapshot — cached, and never fatal: on a slow box the gateway
    #    can exceed 15s and a hard failure here would take the whole page down.
    models_json = await _models_snapshot()
    # False means "no price data at all", not "no miners are priced".
    prices_known = models_json is not None
    priced: dict[str, dict] = {}   # addr -> {models, best_price, active, version, capacity_tpm}
    models_out = []
    total_cap = 0
    gateway_countries: set[str] = set()
    for x in (models_json or {}).get('data', []):
        xf = x.get('x-flop', {})
        cap = xf.get('capacity_tpm') or 0
        total_cap += cap
        for dc in xf.get('datacenters', []) or []:
            gateway_countries.add(dc.get('country_code'))
        ps_list = xf.get('price_sources', [])
        active_cnt = sum(1 for ps in ps_list if _parse_dt(ps.get('expires_at')) > now)
        models_out.append({'id': x['id'], 'name': x.get('name') or x['id'],
                           'miners': len(ps_list), 'active_miners': active_cnt,
                           'capacity_tpm': cap,
                           'price_prompt': (x.get('pricing') or {}).get('prompt')})
        for ps in ps_list:
            addr = ps.get('miner_address')
            if not addr:
                continue
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
    with db_lock:
        perf_rows = db.execute(
            "select miner, count(*), sum(ok), avg(case when ok=1 then latency end), "
            "sum(cast(json_extract(cost,'$') as real)), max(ts) from answers "
            "where engine='flop' and miner group by miner").fetchall()
    for miner, calls, ok, lat, spend, last in perf_rows:
        perf[miner] = {'calls': calls, 'ok': ok or 0,
                       'latency': round(lat, 1) if lat else None,
                       'spend': round(spend or 0, 2), 'last_seen': last}

    hist = _reliability_map(86400)
    series, net_series = _reliability_series(86400)

    # 4) merge
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
            'gpus': [{'name': g.get('name'), 'count': g.get('count'),
                      'mem_gb': round((g.get('memoryMib') or 0) / 1024)} for g in gpus],
            'worker_version': (rec.get('workerBuild') or {}).get('version'),
            # local perf
            'calls': calls,
            'success_rate': round(pf.get('ok', 0) / calls, 2) if calls else None,
            'avg_latency_s': pf.get('latency'),
            'flop_spent': pf.get('spend', 0),
            'last_seen': pf.get('last_seen'),
            # 24h settlement delta from our own snapshots
            'history_24h': hist.get(addr),
            'settle_series': series.get(addr),
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
        'prices_known': prices_known,
        'network': {
            'total_miners': len(out),
            # null, not 0, when unknown — the client prints "—".
            'priced_miners': len(priced) if prices_known else None,
            'active_priced': active_priced if prices_known else None,
            'registered_active': sum(1 for m in out if m['is_active']),
            'capacity_tpm': total_cap if prices_known else None,
            'models': len(models_out) if prices_known else None,
            'countries': sorted(c for c in gateway_countries if c),
            # Network settle rate per snapshot interval, from Scout's own history.
            'settle_series': net_series,
        },
        'stale': age_s > _MINERS_TTL,
        'age_s': age_s,
        'models': models_out,
        'miners': out,
    }


@app.get('/api/miners/{address}/history')
def miner_history(address: str, window: int = 604800, points: int = 96):
    """One miner's settlement history from Scout's own snapshots.

    Reads the local `reliability` table only, so it never touches the gateway and
    answers in milliseconds. `window` defaults to 7 days and is capped at the 30
    days the table keeps. `settle_rate` at each point is the rate over the window
    *up to* that snapshot, the same definition the miners page prints, so the
    last point always matches the 24h figure for that miner.

    Returns the raw cumulative counters too, so a caller can compute its own
    rates instead of trusting ours.
    """
    window = max(3600, min(window, 30 * 86400))
    points = max(2, min(points, 500))
    start = time.time() - window
    with db_lock:
        rows = db.execute('select ts, settled, timeout from reliability where miner = ? and ts >= ? order by ts',
                          (address, start)).fetchall()
    if not rows:
        raise HTTPException(404, {'error': 'no_history', 'message': 'No snapshots for this miner yet. '
                                 'Snapshots are written every 15 minutes while the miner is in the registry.'})
    # Thin the series to at most `points` samples, always keeping the first and last.
    step = max(1, (len(rows) + points - 1) // points)
    picked = rows[::step]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    first_s, first_t = rows[0][1], rows[0][2]
    series = []
    for ts, s, t in picked:
        sessions = (s - first_s) + (t - first_t)
        series.append({
            'ts': round(ts),
            'settled': s, 'timeouts': t,
            'window_sessions': sessions,
            'settle_rate': round((s - first_s) / sessions, 3) if sessions > 0 else None,
        })
    last_ts, last_s, last_t = rows[-1]
    sessions_total = (last_s - first_s) + (last_t - first_t)
    return {
        'address': address,
        'window_s': window,
        'snapshots': len(rows),
        'span_s': round(last_ts - rows[0][0]),
        'first_ts': round(rows[0][0]),
        'last_ts': round(last_ts),
        'settle_rate': round((last_s - first_s) / sessions_total, 3) if sessions_total > 0 else None,
        'sessions': sessions_total,
        'series': series,
        'note': 'settle_rate at each point is measured from the first snapshot in the window. '
                'Snapshots are 15 minutes apart; a short span means a short history, not a stable rate.',
    }


@app.get('/api/miners/{address}')
async def miner_detail(address: str):
    """One miner, same shape as a row on /api/miners. Agents should not have to
    download 300 rows to read one."""
    payload = await miner_explorer()
    for m in payload['miners']:
        if m['address'] == address:
            return m
    raise HTTPException(404, 'no such miner in the current registry snapshot')


# ── Miner reliability alerts ─────────────────────────────────────────────────
# Callers subscribe to a settle-rate drop below a threshold. The service fires
# one alert per crossing: once a miner has been marked "fired" it must recover
# above threshold + 0.05 hysteresis before another alert can go out, so a
# miner stuck at a low rate does not spam on every check.
#
# Channels supported today:
#   site     — silent; visible only via GET /api/alerts/check. Good for agents
#              that poll rather than receive pushes.
#   telegram — posts a message in the Telegram group/channel whose admin handle
#              is in SCOUT_TG_BOT_TOKEN; the `handle` field is the chat_id or
#              @username. Requires SCOUT_TG_BOT_TOKEN in the env.
#   twitter  — posts a mention. Requires SCOUT_TW_* env vars. Not wired yet;
#              stored and returned, but no outbound call is made.
#
# METERING note: alerting is a free-tier service. No FLOP is charged.

_TG_TOKEN = os.getenv('SCOUT_TG_BOT_TOKEN', '')
_TW_BEARER = os.getenv('SCOUT_TW_BEARER_TOKEN', '')
_ALERT_HYSTERESIS = 0.05   # must recover this much above threshold before re-firing


class AlertReq(BaseModel):
    address: str = Field('*', description="SS58 miner address, or '*' for every miner")
    threshold: float = Field(0.5, ge=0.0, le=1.0, description='Fire when settle_rate drops below this')
    channel: str = Field('site', description="'site', 'telegram', or 'twitter'")
    handle: str = Field('', description='Chat id / @username for the channel; leave blank for site')


@app.post('/api/alerts')
def create_alert(req: AlertReq, request: Request):
    """Subscribe to a settle-rate alert for one miner (or all).

    Returns the alert id. Use GET /api/alerts/check to poll, or the service
    will push to `channel` when the threshold is crossed.

    Channel `site` requires no credentials and suits agents that poll.
    Channel `telegram` delivers to the given handle when SCOUT_TG_BOT_TOKEN
    is set server-side; callers supply only the destination handle.
    """
    channel = req.channel.strip().lower()
    if channel not in ('site', 'telegram', 'twitter'):
        raise HTTPException(422, {'error': 'bad_channel',
                                   'message': "channel must be 'site', 'telegram', or 'twitter'"})
    handle = req.handle.strip()
    if channel in ('telegram', 'twitter') and not handle:
        raise HTTPException(422, {'error': 'handle_required',
                                   'message': f"channel '{channel}' requires a handle"})
    with db_lock:
        cur = db.execute(
            'insert into miner_alerts(created_at,address,threshold,channel,handle,last_rate,fired_at,active) '
            'values (?,?,?,?,?,null,null,1)',
            (time.time(), req.address, req.threshold, channel, handle))
        alert_id = cur.lastrowid
        db.commit()
    return {'id': alert_id, 'address': req.address, 'threshold': req.threshold,
            'channel': channel, 'handle': handle,
            'channel_live': (channel == 'site') or
                            (channel == 'telegram' and bool(_TG_TOKEN)) or
                            (channel == 'twitter' and bool(_TW_BEARER)),
            'note': ('site channel: poll GET /api/alerts/check to read triggers.'
                     if channel == 'site' else f'fires when settle_rate < {req.threshold:.2f}')}


@app.get('/api/alerts/check')
def check_alerts(address: str = '*', limit: int = 100):
    """Pending alert triggers. Returns events since they were last seen.

    Each entry has the miner address, the threshold that was crossed and the
    settle rate at the time. Calling this endpoint marks the returned events as
    seen (sets `fired_at` to the current time for site-channel alerts).
    Records older than 7 days are auto-purged.
    """
    limit = max(1, min(limit, 1000))
    cutoff = time.time() - 7 * 86400
    with db_lock:
        db.execute('delete from miner_alerts where created_at < ? and active = 0', (cutoff,))
        if address == '*':
            rows = db.execute(
                'select id, address, threshold, channel, handle, last_rate, fired_at, active '
                'from miner_alerts order by created_at desc limit ?', (limit,)).fetchall()
        else:
            rows = db.execute(
                'select id, address, threshold, channel, handle, last_rate, fired_at, active '
                'from miner_alerts where address = ? or address = "*" order by created_at desc limit ?',
                (address, limit)).fetchall()
        db.commit()
    keys = ('id', 'address', 'threshold', 'channel', 'handle', 'last_rate', 'fired_at', 'active')
    return {'alerts': [dict(zip(keys, r)) for r in rows]}


@app.delete('/api/alerts/{alert_id}')
def delete_alert(alert_id: int):
    """Deactivate an alert by id."""
    with db_lock:
        n = db.execute('update miner_alerts set active = 0 where id = ?', (alert_id,)).rowcount
        db.commit()
    if not n:
        raise HTTPException(404, {'error': 'not_found', 'id': alert_id})
    return {'deleted': alert_id}


PAGES = {
    '/': dict(file='answer.html', active='/', title='Flop Scout — search and page reading that agents pay for in FLOP',
               description=f'{len(SOURCES)} sources, cited answers and live on-chain FLOP data, priced per call and paid from your own wallet. No card, no account.',
               scripts=['answer.js']),
    '/miners': dict(file='miners.html', active='/miners', title='Flop Scout — every FLOP miner ranked by settlement performance',
                    description='All registered FLOP miners with GPU, stake, calibration, capacity and an on-chain settlement score refreshed every two minutes.',
                    scripts=['miners.js']),
    '/analytics': dict(file='analytics.html', active='/analytics', title='Flop Scout — demand analytics',
                       description='Aggregate demand for Scout: daily answers, unique clients, returning share and source success. No question text or client addresses.',
                       scripts=['analytics.js']),
}
# The API page has no interactive behaviour beyond copy buttons and the shared
# wallet button, so api.js only adds the copy handler.

async def _render(path: str, status_code: int = 200):
    spec = PAGES[path]
    body = (BASE / 'pages' / spec['file']).read_text(encoding='utf-8')
    html = layout.page(title=spec['title'], description=spec['description'], path=path,
                       body=body, scripts=spec['scripts'], active=spec['active'],
                       head_extra=spec.get('head_extra', ''))
    return HTMLResponse(html, status_code=status_code)


@app.exception_handler(404)
async def _not_found(request: Request, exc):
    if request.url.path.startswith('/api/'):
        detail = getattr(exc, 'detail', None)
        body = {'error': 'not_found', 'path': request.url.path, 'hint': 'GET /api/catalog lists what exists'}
        if detail and detail != 'Not Found':
            body['detail'] = detail
        return JSONResponse(body, status_code=404)
    body = (BASE / 'pages' / '404.html').read_text(encoding='utf-8')
    html = layout.page(title='Flop Scout — page not found',
                       description='That page does not exist. The catalogue, the price list and the miner board do.',
                       path='/', body=body, scripts=[], active='')
    return HTMLResponse(html, status_code=404)


# Cloudflare caches .txt by default and keeps a stale copy for hours. These files
# describe the site to crawlers and agents, so a wrong one costs more than a
# few extra origin hits.
_SHORT_CACHE = {'Cache-Control': 'public, max-age=300'}


@app.get('/robots.txt')
def robots():
    # /api/ used to be disallowed, which hid the API from exactly the readers it
    # is for. Crawlers only issue GETs, and every GET under /api/ is a cheap,
    # cached read; the paid endpoints are POST and rate-limited per caller.
    return PlainTextResponse(
        'User-agent: *\nAllow: /\n\n'
        f'Sitemap: {SITE}/sitemap.xml\n',
        headers=_SHORT_CACHE)


_LLMS_TXT = f'''# Flop Scout

> Web search, page reading and FLOP network data for AI agents, priced per call in FLOP.
> Also a live ranking of FLOP inference miners by on-chain settlement rate, with alerts.

Runs on the FLOP devnet. Charges are nominal: computed, shown and logged, never debited.
Scout's own fee and the miner's inference cost are always reported as two separate numbers.

## API

- [OpenAPI schema]({SITE}/openapi.json): every endpoint, request and response shape
- [Quick start]({SITE}/docs/quick-start): quote a request, then ask it
- [Endpoints]({SITE}/docs/endpoints): full list with descriptions
- [Errors]({SITE}/docs/errors): status codes and what to do
- [Pricing]({SITE}/docs/pricing): price per source, in FLOP

## Main calls

- POST {SITE}/api/quote: price a request before running it. With `q` and `model` it also returns the gateway's estimate of the miner's cost. Spends nothing
- POST {SITE}/api/answer: search the chosen sources and return a cited answer with an itemised price. `sources: ["auto"]` picks free sources only
- GET {SITE}/api/catalog: sources, presets and models
- GET {SITE}/api/pricing: the price list
- GET {SITE}/api/inference-prices: every FLOP model priced as a whole request. No key needed

## Miner data

- GET {SITE}/api/router: miners ranked by settlement rate and local success
- GET {SITE}/api/miners: every registered miner with GPU, stake and 24h settlement delta
- GET {SITE}/api/miners/{{address}}/history: one miner's settlement history, up to 30 days
- POST {SITE}/api/alerts: get notified when a miner's settle rate drops below a threshold

## Limits

- Free tier is rate-limited per caller; limits are returned in X-RateLimit-* headers
- FLOP answers need a FLOP gateway key; pass your own in `keys.flop`, or `keys.openrouter` for OpenRouter models
- Source code: https://github.com/0xBobaAI/flop-scout (MIT)
'''


@app.get('/llms.txt')
def llms_txt():
    return PlainTextResponse(_LLMS_TXT, headers=_SHORT_CACHE)


@app.get('/openapi.json', include_in_schema=False)
def openapi_alias():
    """The conventional path. FastAPI serves the same schema at /api/openapi.json."""
    return JSONResponse(app.openapi())


@app.get('/sitemap.xml')
def sitemap():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    urls = ''.join(f'  <url><loc>{SITE}{p}</loc><lastmod>{today}</lastmod></url>\n'
                   for p in list(PAGES) + [f'/docs/{k}' for k in DOCS])
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                    f'{urls}</urlset>\n', media_type='application/xml')


@app.get('/favicon.ico')
def favicon():
    # Browsers still request this before reading the <link> tag; without it every
    # load logs a 404 and some crawlers cache it as "broken".
    return RedirectResponse('/static/favicon.svg', status_code=308)


@app.get('/miners')
async def miners_page():
    return await _render('/miners')


# ── Docs: one page per topic, sidebar tree, on-page TOC ──────────────────────
import re as _re
from docs_content import DOCS, DOCS_GROUPS, DOCS_SCRIPTS

_DOC_ORDER = list(DOCS)


def _slug(text: str) -> str:
    return _re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _docs_body(slug: str) -> str:
    group, title, summary, body = DOCS[slug]
    # Give every <h2> an id and collect them for the right-hand "On this page".
    heads = []

    def _h2(m):
        text = _re.sub(r'<[^>]+>', '', m.group(1)).strip()
        hid = _slug(text)
        heads.append((hid, text))
        return f'<h2 id="{hid}">{m.group(1)}</h2>'

    body = _re.sub(r'<h2>(.*?)</h2>', _h2, body, flags=_re.S)

    tree = ''
    for g in DOCS_GROUPS:
        items = ''.join(
            '<a href="/docs/%s"%s>%s</a>' % (k, ' class="on" aria-current="page"' if k == slug else '', v[1])
            for k, v in DOCS.items() if v[0] == g)
        if items:
            tree += f'<div class="dn-group"><div class="dn-title">{g}</div>{items}</div>'

    toc = ''.join(f'<a href="#{hid}">{text}</a>' for hid, text in heads)
    toc_html = f'<aside class="docs-otp" aria-label="On this page"><div class="dn-title">On this page</div>{toc}</aside>' if toc else ''

    i = _DOC_ORDER.index(slug)
    prev_s = _DOC_ORDER[i - 1] if i > 0 else None
    next_s = _DOC_ORDER[i + 1] if i < len(_DOC_ORDER) - 1 else None
    pager = '<nav class="docs-pager" aria-label="Pagination">'
    pager += f'<a class="card pager-prev" href="/docs/{prev_s}"><span>Previous</span><b>{DOCS[prev_s][1]}</b></a>' if prev_s else '<span></span>'
    pager += f'<a class="card pager-next" href="/docs/{next_s}"><span>Next</span><b>{DOCS[next_s][1]}</b></a>' if next_s else '<span></span>'
    pager += '</nav>'

    return f'''<div class="docs-layout">
  <nav class="docs-nav" aria-label="Documentation">
    <details class="docs-nav-toggle" open><summary>Menu</summary>{tree}</details>
  </nav>
  <article class="docs-article">
    <p class="docs-crumb">{group}</p>
    <h1>{title}</h1>
    <p class="docs-summary">{summary}</p>
    <div class="docs-body">{body}</div>
    {pager}
  </article>
  {toc_html}
</div>'''


def _render_doc(slug: str):
    group, title, summary, _ = DOCS[slug]
    html = layout.page(title=f'{title} — Flop Scout docs', description=summary, path=f'/docs/{slug}',
                       body=_docs_body(slug), scripts=DOCS_SCRIPTS.get(slug, []) + ['docs.js'], active='/docs',
                       body_class='docs-page')
    return HTMLResponse(html)


@app.get('/docs')
async def docs_index():
    return _render_doc(_DOC_ORDER[0])


@app.get('/docs/{slug}')
async def docs_page(slug: str):
    if slug not in DOCS:
        raise HTTPException(404)
    return _render_doc(slug)


# Retired pages keep their links alive: they now point at the matching Docs page.
_OLD_PAGES = {'/pricing': '/docs/pricing', '/api': '/docs/endpoints', '/about': '/docs/about'}


@app.get('/pricing')
@app.get('/api')
@app.get('/about')
async def retired_page(request: Request):
    return RedirectResponse(_OLD_PAGES[request.url.path], status_code=301)


@app.get('/analytics')
async def analytics_page():
    return await _render('/analytics')


@app.get('/')
async def index():
    return await _render('/')


# Bound last: the filter walks app.routes, so every route must exist first.
app.openapi = _custom_openapi
