"""Answer models: FLOP gateway (live catalog) + any OpenAI-compatible provider with the user's own key."""
import asyncio
import json
import os
import time
from pathlib import Path

import flop_gateway
from flop_gateway import FLOP_URL

BASE = Path(__file__).parent
_wallet = BASE / '.wallet.json'

OPENROUTER_URL = 'https://openrouter.ai/api/v1'

# The devnet gateway key expires every 24h and is refreshed by renew_key.py,
# which rewrites .wallet.json. Caching the resolved key at import would pin the
# server to a dead key until a restart, so re-resolve it (cheaply) per request.
_key_cache = {'mtime': None, 'value': '', 'expires': 0}


def flop_key() -> str:
    """Current gateway key.

    .wallet.json is authoritative because renew_key.py rewrites it hourly; it
    wins whenever it holds a non-expired key. FLOP_API_KEY is the bootstrap
    fallback for hosts that never run the renewer (e.g. a read-only container).
    """
    try:
        mtime = _wallet.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _key_cache['mtime']:
        try:
            data = json.loads(_wallet.read_text()) if mtime else {}
            _key_cache.update(mtime=mtime,
                              value=data.get('gateway_key', ''),
                              expires=int(data.get('key_expires_at') or 0))
        except Exception:
            _key_cache.update(mtime=None, value='', expires=0)
    if _key_cache['value'] and _key_cache['expires'] > time.time() * 1000:
        return _key_cache['value']
    return os.getenv('FLOP_API_KEY', '').strip() or _key_cache['value']

# The devnet key is scoped to this model. If the live catalogue is unreachable
# at page load, the picker must still offer something the caller can run —
# otherwise it fills with OpenRouter models that need a key nobody has entered.
DEFAULT_FLOP_MODEL = 'qwen/qwen2.5-0.5b-instruct'
FALLBACK_MODELS = [{'id': f'flop:{DEFAULT_FLOP_MODEL}', 'provider': 'flop', 'model': DEFAULT_FLOP_MODEL,
                    'label': 'Qwen2.5 0.5B Instruct (devnet)', 'price': 'live price unavailable',
                    'capacity_tpm': None, 'miners': 0, 'blocked': False}]

# Curated OpenRouter models shown in the picker (user brings own key).
OPENROUTER_MODELS = [
    ('openai/gpt-4.1-mini', 'GPT-4.1 mini'),
    ('anthropic/claude-haiku-4.5', 'Claude Haiku 4.5'),
    ('google/gemini-2.5-flash', 'Gemini 2.5 Flash'),
    ('deepseek/deepseek-chat-v3.1', 'DeepSeek V3.1'),
    ('qwen/qwen3-8b', 'Qwen3 8B'),
]

SYSTEM = ('Answer the question using only the numbered sources. Cite sources as [n]. '
          'If the sources do not contain the answer, say so. Answer in the language of the question. 2-5 sentences.')

_catalog = {'ts': 0, 'models': []}
BLOCKED = set()  # models the FLOP key is not allowed to use (learned from 401 model_not_allowed)


async def flop_models(c):
    if time.time() - _catalog['ts'] < 120 and _catalog['models']:
        return _catalog['models']
    out = []
    try:
        # Shared cache: this runs inside /api/catalog, which renders on every
        # page load, so it must agree with the miner table and the router.
        payload = await flop_gateway.snapshot(c)
        if payload is None:
            raise RuntimeError('no gateway snapshot')
        for m in payload.get('data', []):
            xf = m.get('x-flop', {})
            out.append({'id': f"flop:{m['id']}", 'provider': 'flop', 'model': m['id'], 'label': m.get('name') or m['id'],
                        'price': f"{m['pricing']['prompt']} FLOP/token", 'capacity_tpm': xf.get('capacity_tpm'),
                        'miners': len(xf.get('price_sources', [])),
                        'blocked': f"flop:{m['id']}" in BLOCKED})
    except Exception as e:
        # Keep the last good catalog rather than emptying the picker. The
        # bare `except Exception` used to hide a NameError here for weeks:
        # flop_gateway was never imported, so every call raised, returned the
        # empty catalog, and the picker silently fell back to one hardcoded
        # stub. A swallowed exception in a catalog loader is indistinguishable
        # from an empty network, so say which it was.
        print('flop_models failed:', repr(e))
        return [dict(m, blocked=m['id'] in BLOCKED) for m in _catalog['models']]
    _catalog.update(ts=time.time(), models=out)
    return out


def cached_models():
    """Last good model list, without touching the network.

    /api/catalog renders on every navigation and a cold gateway costs seconds.
    Serving the cached list immediately and refreshing in the background keeps
    the picker instant while still going live within one request.
    """
    if not _catalog['models']:
        return []
    return _with_providers([dict(m, blocked=m['id'] in BLOCKED) for m in _catalog['models']])


def _with_providers(flop):
    other = [{'id': f'openrouter:{m}', 'provider': 'openrouter', 'model': m, 'label': l,
              'price': 'OpenRouter pricing', 'needs_key': 'openrouter'} for m, l in OPENROUTER_MODELS]
    # Always keep a runnable FLOP option first; a slow gateway is not a reason to
    # hand the caller a picker full of models they cannot use.
    if not any(m['provider'] == 'flop' for m in flop):
        flop = [dict(m) for m in FALLBACK_MODELS]
    return flop + other


async def list_models(c):
    flop = [dict(m, blocked=m['id'] in BLOCKED) for m in await flop_models(c)]
    return _with_providers(flop)


def build_messages(q, sources):
    ctx = '\n\n'.join(f"[{s['n']}] {s['title']} ({s['url']}) — {s['source']}\n{s['snippet']}" for s in sources)
    return [{'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': f'Sources:\n{ctx}\n\nQuestion: {q}'}]


async def complete(c, model_id, messages, max_tokens, keys, tries=4, budget=200):
    """Returns dict(answer, engine, model, receipt, usage, latency_s, error, attempts)."""
    provider, model = model_id.split(':', 1)
    if provider == 'flop':
        url, key = FLOP_URL, keys.get('flop') or flop_key()
    elif provider == 'openrouter':
        url, key = OPENROUTER_URL, keys.get('openrouter')
    else:
        return {'error': f'unknown provider {provider}'}
    if not key:
        return {'error': f'no key for {provider}', 'engine': provider, 'model': model}

    err, t0, attempt = '', time.time(), 0
    for attempt in range(1, tries + 1):
        # Never start a new attempt we cannot finish inside the budget
        remaining = budget - (time.time() - t0)
        if remaining <= 8:
            break
        try:
            r = await c.post(f'{url}/chat/completions', headers={'Authorization': f'Bearer {key}'},
                             json={'model': model, 'messages': messages, 'max_tokens': max_tokens},
                             timeout=min(90, remaining))
        except Exception:
            err = 'FLOP miners timed out — the network is congested right now. Sources are still shown below.'
            continue

        if r.is_success:
            j = r.json()
            rc = j.get('x_flop_receipt') or {}
            try:
                import src_router as _r
                _r.record(rc.get('miner'), True, time.time() - t0)
            except Exception:
                pass
            return {'answer': j['choices'][0]['message']['content'].strip(), 'engine': provider, 'model': model,
                    'receipt': rc, 'usage': j.get('usage'),
                    'latency_s': round(time.time() - t0, 1), 'attempts': attempt}

        body = r.text
        err = f'{provider} {r.status_code}: {body[:200]}'

        # Hard failures — retrying will not help
        if 'model_not_allowed' in body:
            BLOCKED.add(model_id)
            err = 'this FLOP devnet key is not allowed to use this model (new keys get qwen2.5-0.5b only)'
            break
        if r.status_code in (400, 401, 403, 404):
            break

        # Transient congestion — short backoff, capacity frees up in seconds
        if 'miner_capacity_reserved' in body or 'capacity fully reserved' in body:
            err = 'All FLOP miners are at capacity — the network is busy right now.'
            wait = 5
        elif r.status_code in (503, 524) or 'provider_error' in body or 'timeout' in body.lower():
            err = 'FLOP miners timed out — the network is congested right now. Sources are still shown below.'
            wait = 5
        else:
            try:
                h = r.headers.get('retry-after', '')
                wait = int(h) if h.isdigit() else int(r.json().get('retry_after', 0))
            except Exception:
                wait = 0
            wait = wait or 4 * attempt

        if time.time() - t0 + wait > budget:
            break
        await asyncio.sleep(min(wait, 30))

    return {'error': err, 'engine': provider, 'model': model,
            'latency_s': round(time.time() - t0, 1), 'attempts': attempt}
