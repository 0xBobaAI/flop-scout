"""OpenAI-compatible gateway: POST /v1/chat/completions and GET /v1/models.

Any OpenAI client works with `base_url = https://flopscout.com/v1`. A request is
forwarded unchanged to the provider its model names:

  flop:<model>         the FLOP network gateway
  openrouter:<model>   OpenRouter (caller's own key)
  <model>              looked up in the catalog; FLOP models win a bare name

Who pays, decided by the Authorization bearer:

  Bearer sk_...        a Scout member key: Scout's FLOP wallet pays, no daily cap
  Bearer <other>       the caller's own key for that provider, passed through
  none                 Scout's FLOP wallet pays, per-IP daily cap

Choosing the miner (FLOP models only; the gateway assigns one otherwise):

  `X-Scout-Miner: auto`        Scout pins the best-ranked priced miner
  `X-Scout-Miner: <ss58>`      pin that miner
  or `"miner": ...` in the JSON body, which is not forwarded upstream.

A pin needs an upstream key the FLOP gateway lets pin (owner keys); otherwise it
answers 403 and Scout returns that error unchanged rather than silently
dropping the pin.

The response is the provider's, plus `x_scout` (who paid, which miner, how it
was picked). Streaming is passed through as it arrives.
"""

import json
import time

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import flop_gateway
import llm
import members
import src_router as router

PREFIXES = ('flop:', 'openrouter:')
MAX_BODY = 512 * 1024          # bytes; ~120k tokens of text, above any model's context here
# Below nginx's proxy_read_timeout (240 s), so Scout returns a clean JSON 504
# instead of nginx cutting the connection off.
UPSTREAM_TIMEOUT = 220.0

server = None                  # the server module, set by attach(); avoids an import cycle


def attach(srv) -> None:
    global server
    server = srv


def _err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    """An OpenAI-shaped error, so client libraries surface it instead of crashing on the shape."""
    return JSONResponse({'error': {'message': message, 'type': 'invalid_request_error' if status < 500 else 'server_error',
                                   'code': code}}, status_code=status, headers=headers)


def resolve_model(name: str) -> tuple[str, str] | None:
    """(provider, upstream model id) for a requested model name, or None."""
    name = (name or '').strip()
    for p in PREFIXES:
        if name.startswith(p):
            return p[:-1], name[len(p):]
    flop_ids = {m['model'] for m in llm.cached_models() if m.get('provider') == 'flop'}
    if name in flop_ids:
        return 'flop', name
    or_ids = {m for m, _ in llm.OPENROUTER_MODELS}
    if name in or_ids:
        return 'openrouter', name
    return None


def priced_miners(model_id: str) -> list[str]:
    for m in (flop_gateway.cached() or {}).get('data', []):
        if m.get('id') == model_id:
            return sorted({ps.get('miner_address') for ps in (m.get('x-flop') or {}).get('price_sources') or []
                           if ps.get('miner_address')})
    return []


def pick_miner(model_id: str) -> tuple[str | None, str]:
    """The best-ranked miner currently pricing the model, and why."""
    priced = set(priced_miners(model_id))
    if not priced:
        return None, 'no miner is pricing this model right now'
    ranked = [m for m in router.STATE.ranked_miners(model_id) if m.address in priced]
    if not ranked:
        return sorted(priced)[0], 'no ranking data; first priced miner'
    best = ranked[0]
    return best.address, f'top Scout score {best.score:.2f} among {len(priced)} priced miners'


def _bearer(request: Request) -> str:
    a = request.headers.get('authorization') or ''
    return a.split(None, 1)[1].strip() if a.lower().startswith('bearer ') else ''


async def models_list() -> dict:
    out = []
    now = int(time.time())
    for m in llm.cached_models() or await llm.list_models(server.client):
        if m.get('blocked'):
            continue
        out.append({'id': m['id'], 'object': 'model', 'created': now, 'owned_by': m['provider'],
                    'name': m.get('label'), 'x_scout': {'provider': m['provider'], 'upstream_id': m['model'],
                                                        'price': m.get('price'), 'miners': m.get('miners'),
                                                        'needs_key': m.get('needs_key')}})
    return {'object': 'list', 'data': out}


async def chat(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _err(413, 'body_too_large', f'Request body is over {MAX_BODY // 1024} KB.')
    try:
        body = json.loads(raw or b'{}')
    except ValueError:
        return _err(400, 'invalid_json', 'Body is not valid JSON.')
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list) or not body['messages']:
        return _err(400, 'invalid_request', '`messages` must be a non-empty array.')
    resolved = resolve_model(body.get('model', ''))
    if not resolved:
        return _err(404, 'model_not_found', f"Unknown model {body.get('model')!r}. GET /v1/models lists them.")
    provider, upstream_model = resolved

    # ── who pays ────────────────────────────────────────────────────────────
    bearer = _bearer(request)
    member = members.lookup(server.db, server.db_lock, bearer) if bearer.startswith(members.PREFIX) else None
    if bearer.startswith(members.PREFIX) and not member:
        return _err(401, 'invalid_scout_key', 'This Scout key is not valid or has been revoked.')
    own_key = bearer if bearer and not member else ''
    if provider == 'openrouter' and not own_key:
        return _err(401, 'key_required', 'OpenRouter models need your own OpenRouter key as the bearer token.')
    upstream_key = own_key or (llm.flop_key() if provider == 'flop' else '')
    if not upstream_key:
        return _err(503, 'no_scout_key', 'Scout has no FLOP gateway key right now. Pass your own as the bearer token.')
    paid_by = 'user' if own_key else 'scout'

    cid = server.client_id(request)
    try:
        quota = server.answer_quota(request, cid, bool(own_key), member)
    except HTTPException as e:
        return _err(e.status_code, 'rate_limited', str(e.detail), dict(e.headers or {}))
    if paid_by == 'scout' and server.wallet_exhausted():
        server.refund_quota(cid, quota)
        return _err(503, 'wallet_budget_reached',
                    f'Scout has paid its daily budget of {members.WALLET_DAILY_FLOP:g} FLOP. Answers resume as the '
                    '24-hour window rolls, or pass your own FLOP key.', {'Retry-After': '3600'})

    # ── miner choice ────────────────────────────────────────────────────────
    want = (request.headers.get('x-scout-miner') or body.pop('miner', None) or '').strip()
    pin, why = None, 'assigned by the FLOP gateway'
    if want and provider == 'flop':
        if want == 'auto':
            pin, why = pick_miner(upstream_model)
            if not pin:
                why = f'auto requested; {why}; assigned by the FLOP gateway'
        else:
            pin, why = want, 'requested by the caller'

    up = dict(body)
    up['model'] = upstream_model
    stream = bool(up.get('stream'))
    if stream and provider == 'flop':
        up.setdefault('stream_options', {'include_usage': True})
    url = (llm.FLOP_URL if provider == 'flop' else llm.OPENROUTER_URL) + '/chat/completions'
    headers = {'Authorization': f'Bearer {upstream_key}', 'Content-Type': 'application/json'}
    if pin:
        headers['x-flop-miner'] = pin
    scout_meta = {'paid_by': paid_by, 'provider': provider, 'pinned_miner': pin, 'miner_choice': why,
                  'member': (member or {}).get('label')}
    t0 = time.time()

    def account(ok: bool, usage: dict | None, receipt: dict | None, error: str | None = None):
        """Log the call the way /api/answer does, so spend, stats and the wallet guard see it."""
        res = {'answer': 'chat' if ok else None, 'engine': provider, 'model': upstream_model,
               'receipt': receipt or {}, 'usage': usage or {}, 'latency_s': round(time.time() - t0, 2),
               'attempts': 1, 'error': error, 'paid_by': paid_by, 'scout_charge_flop': None,
               'metering': 'nominal', 'scout_quote_flop': 0.0}
        server.log('[chat]', ['v1'], res, cid, member)
        if ok:
            members.record(server.db, server.db_lock, member, (usage or {}).get('cost_flop'))
            if provider == 'flop' and (receipt or {}).get('miner'):
                router.record((receipt or {}).get('miner'), True, time.time() - t0)
        else:
            server.refund_quota(cid, quota)

    quota_headers = {'X-RateLimit-Limit': str(quota['limit']), 'X-RateLimit-Remaining': str(quota['remaining']),
                     'X-RateLimit-Window': quota['window'], 'X-RateLimit-Tier': quota['tier']}

    if not stream:
        try:
            r = await server.client.post(url, json=up, headers=headers, timeout=UPSTREAM_TIMEOUT)
        except httpx.HTTPError as e:
            account(False, None, None, type(e).__name__)
            return _err(504, 'upstream_unreachable', f'{provider} did not answer: {type(e).__name__}.')
        try:
            data = r.json()
        except ValueError:
            data = {'error': {'message': r.text[:300], 'code': f'http_{r.status_code}'}}
        if r.status_code >= 400:
            account(False, None, None, f'{provider} {r.status_code}')
            if isinstance(data, dict):
                data['x_scout'] = scout_meta
            keep = {k: v for k, v in r.headers.items() if k.lower() == 'retry-after'}
            return JSONResponse(data, status_code=r.status_code, headers=keep)
        receipt = data.get('x_flop_receipt') or {}
        scout_meta['miner'] = receipt.get('miner')
        account(True, data.get('usage'), receipt)
        data['x_scout'] = scout_meta
        return JSONResponse(data, headers=quota_headers)

    # ── streaming: pass SSE lines through as they arrive ──────────────────
    req = server.client.build_request('POST', url, json=up, headers=headers,
                                      timeout=httpx.Timeout(UPSTREAM_TIMEOUT, read=UPSTREAM_TIMEOUT))
    try:
        r = await server.client.send(req, stream=True)
    except httpx.HTTPError as e:
        account(False, None, None, type(e).__name__)
        return _err(504, 'upstream_unreachable', f'{provider} did not answer: {type(e).__name__}.')
    if r.status_code >= 400:
        raw_err = (await r.aread())[:2000]
        await r.aclose()
        account(False, None, None, f'{provider} {r.status_code}')
        try:
            data = json.loads(raw_err)
        except ValueError:
            data = {'error': {'message': raw_err.decode(errors='replace')[:300], 'code': f'http_{r.status_code}'}}
        if isinstance(data, dict):
            data['x_scout'] = scout_meta
        keep = {k: v for k, v in r.headers.items() if k.lower() == 'retry-after'}
        return JSONResponse(data, status_code=r.status_code, headers=keep)

    async def relay():
        usage, receipt, done = None, None, False
        try:
            async for line in r.aiter_lines():
                if line.startswith('data:'):
                    data = line[5:].strip()
                    if data == '[DONE]':
                        done = True
                        meta = dict(scout_meta, miner=(receipt or {}).get('miner'))
                        # Shaped as an ordinary empty-choices chunk, like the OpenAI usage
                        # chunk: strict SDKs validate `object == "chat.completion.chunk"`
                        # and reject a custom object name, while preserving extra fields.
                        chunk = {'id': 'scout', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                                 'model': upstream_model, 'choices': [], 'x_scout': meta}
                        yield f'data: {json.dumps(chunk)}\n\n'.encode()
                    else:
                        try:
                            ev = json.loads(data)
                            usage = ev.get('usage') or usage
                            receipt = ev.get('x_flop_receipt') or receipt
                        except ValueError:
                            pass
                yield (line + '\n').encode()
        finally:
            await r.aclose()
            account(done, usage, receipt, None if done else 'stream interrupted')

    return StreamingResponse(relay(), media_type='text/event-stream',
                             headers={**quota_headers, 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
