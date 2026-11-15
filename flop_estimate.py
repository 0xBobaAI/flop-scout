"""Zero-spend inference pricing from the FLOP gateway's `/models/estimate`.

Why this module exists: until now the only FLOP cost a caller could see was a
per-token string scraped off `/v1/models` ("0.000006 FLOP/token"). That is a
unit price, not a price — an agent cannot budget against it, and Scout could not
answer "what will this request cost me" without spending.

`POST /v1/models/estimate` answers exactly that, and two facts make it unusually
useful here:

  * **It needs no key.** The operation inherits the gateway's global security,
    which is `BearerAuth` *or* `X402Payment` — an unauthenticated call returns
    200. Measured 2026-11-15: 200 with no Authorization header at all. So this
    works while our devnet allowance is dead, which no other FLOP call does.  * **It reports `available_miners`.** Measured 2026-11-15 against all three
    live models: it equals the count of `x-flop.price_sources` exactly (24/24,
    6/6, 1/1). So it is *not* an independent capacity signal — it is the same
    number the router already derives. It is useful as a cross-check that our
    parse agrees with the gateway's, and that is all it is claimed for here.

What the estimate genuinely adds over `/v1/models` is the arithmetic the unit
price cannot express: whole-request FLOP cost, token split, GFLOPs and
`estimated_latency_blocks`. Measured, `estimated_cost_flop` equals
`pricing.prompt × estimated_total_tokens` to the cent on every model — so the
gateway is not applying a hidden floor or multiplier, and a caller can trust the
product. That is worth knowing precisely because it was not documented anywhere.

What it deliberately is not: `/connect/preflight` and `/keys/ephemeral` are the
endpoints that would let Scout actually constrain the gateway. Both are strictly
`BearerAuth`, and both refuse us — preflight returns 403
`unsupported_authority` ("Preflight requires an ephemeral child credential"),
ephemeral returns 401 `parent_api_key_required`. They need a live parent key, so
they are testnet work, not devnet work. This module is the part that is
reachable today.

Numbers here are the gateway's own estimate of a request shaped like ours; they
are not a quote Scout charges. Scout's fee is separate and comes from
`pricing.py`. Keep the two apart — that is the rule the whole product rests on.
"""

import asyncio
import hashlib
import json
import time

import flop_gateway

# Measured on this devnet: estimate answered in 0.6–2.4 s. It is a scheduler
# lookup, not an inference, so it is far cheaper than /chat/completions — but the
# gateway's tail latency is the same, so the timeout matches flop_gateway's.
TIMEOUT = 12.0
ATTEMPTS = 2

# An estimate is a forecast of capacity, and capacity moves. 60 s matches
# flop_gateway.TTL so the model list and its prices age at the same rate.
TTL = 60.0
MIN_RETRY = 20.0

_state: dict = {}
_lock = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _cache_key(model: str, messages: list, max_tokens: int) -> str:
    """Stable key over the things that change the answer.

    The gateway hashes tokens from the actual message text, so the cache key has
    to cover it too — an estimate for a 4-token question is wrong for a 400-token
    one. max_tokens is in the key because it drives estimated_output_tokens.
    """
    blob = json.dumps({'m': model, 'msg': messages, 't': max_tokens},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]


def cached(model: str, messages: list, max_tokens: int) -> dict | None:
    """A fresh estimate without touching the network. None if absent or stale."""
    ent = _state.get(_cache_key(model, messages, max_tokens))
    if not ent or time.time() - ent['ts'] > TTL:
        return None
    return ent['data']


async def estimate(client, model: str, messages: list, max_tokens: int = 256,
                   timeout: float = TIMEOUT) -> dict | None:
    """The gateway's own cost forecast for a request, or None if unavailable.

    Returns the raw estimate payload:
      estimated_input_tokens, estimated_output_tokens, estimated_total_tokens,
      estimated_gflops, estimated_cost_flop, recommended_payment,
      estimated_latency_blocks, available_miners

    `None` means "unknown", never "free" and never "zero miners". Callers render
    it as a dash. A failed upstream keeps the previous value for one TTL beyond
    the normal window, because a stale price beats no price — but it is marked
    `stale` so the UI can say so.
    """
    key = _cache_key(model, messages, max_tokens)
    now = time.time()
    ent = _state.get(key)
    if ent and now - ent['ts'] < TTL:
        return ent['data']
    if ent and now - ent.get('err_ts', 0) < MIN_RETRY:
        return dict(ent['data'], stale=True) if ent['data'] else None

    async with _get_lock():
        ent = _state.get(key)
        now = time.time()
        if ent and now - ent['ts'] < TTL:
            return ent['data']
        last = None
        for _ in range(ATTEMPTS):
            try:
                # messages, not prompt: llm.complete() sends a chat array with a
                # system turn, and the token split differs between the two lanes.
                r = await client.post(
                    f'{flop_gateway.FLOP_URL}/models/estimate',
                    json={'model': model, 'messages': messages, 'max_tokens': max_tokens},
                    timeout=timeout,
                )
                r.raise_for_status()
                payload = r.json()
                if not isinstance(payload, dict) or 'estimated_cost_flop' not in payload:
                    raise ValueError(f'unexpected estimate payload: {str(payload)[:120]}')
                _state[key] = {'ts': time.time(), 'data': payload, 'err_ts': 0.0}
                return payload
            except Exception as e:
                last = e
        if ent:
            ent['err_ts'] = time.time()
            return dict(ent['data'], stale=True) if ent['data'] else None
        _state[key] = {'ts': 0.0, 'data': None, 'err_ts': time.time()}
        print('gateway estimate failed:', repr(last))
        return None


async def for_question(client, model: str, question: str, sources: list,
                       max_tokens: int = 256) -> dict | None:
    """Estimate the exact request `llm.complete()` would send for this answer.

    Builds the same messages so the token count is the one we would really be
    charged for, not a guess from the bare question.
    """
    import llm
    return await estimate(client, model.replace('flop:', '', 1) if model.startswith('flop:') else model,
                          llm.build_messages(question, sources), max_tokens)


def summarise(payload: dict | None) -> dict | None:
    """The shape the API and UI consume. Keeps the gateway's names intact."""
    if not payload:
        return None
    cost = payload.get('estimated_cost_flop')
    out = {
        'cost_flop': round(cost, 6) if isinstance(cost, (int, float)) else None,
        'recommended_payment_flop': round(payload['recommended_payment'], 6)
            if isinstance(payload.get('recommended_payment'), (int, float)) else None,
        'input_tokens': payload.get('estimated_input_tokens'),
        'output_tokens': payload.get('estimated_output_tokens'),
        'total_tokens': payload.get('estimated_total_tokens'),
        'gflops': payload.get('estimated_gflops'),
        'latency_blocks': payload.get('estimated_latency_blocks'),
        'available_miners': payload.get('available_miners'),
    }
    if payload.get('stale'):
        out['stale'] = True
    return out


def capacity_by_model(payloads: dict) -> dict:
    """model id -> available_miners, from a set of cached estimates.

    Measured 2026-11-15 this equals the count of `price_sources` on every live
    model, so it is a cross-check on the router's parse rather than a new
    signal. Kept because a disagreement between the two would mean one of them
    is reading the gateway wrong.
    """
    return {m: p.get('available_miners') for m, p in payloads.items()
            if isinstance(p, dict) and p.get('available_miners') is not None}
