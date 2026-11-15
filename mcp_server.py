"""Remote MCP server for Flop Scout, over Streamable HTTP at POST /mcp.

Why hand-rolled and dependency-free: the production venv pins fastapi and
pydantic for Python 3.10, and the official `mcp` SDK would pull its own
constraints into it. The transport is small (JSON-RPC 2.0 over one POST
endpoint), so a few dozen lines cost less than a dependency conflict on a box
with 1 GB of RAM.

Why it adds no logic of its own: every tool is a thin call to an existing HTTP
route, made through the application's own ASGI transport. Validation, rate
limits, pricing and response shapes therefore stay identical to the REST API,
and the two cannot drift apart. The caller's real IP is forwarded so the
per-caller rate limit applies to the MCP client, not to localhost.

Scope: tools only. No resources, prompts or sampling, and no server-initiated
messages, so no SSE stream is needed; every request gets a single JSON reply.
"""

import json

import httpx

SERVER_INFO = {'name': 'flop-scout', 'title': 'Flop Scout', 'version': '0.6.0'}
# Newest first. The client's requested version is echoed when we know it,
# otherwise we answer with our newest and let the client decide.
SUPPORTED = ['2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05']

INSTRUCTIONS = (
    'Flop Scout sells web search, page reading and FLOP network data to agents, priced per call in FLOP, '
    'and ranks FLOP inference miners by on-chain settlement rate. Charges are nominal on devnet: '
    'computed and reported, never debited. Scout\'s fee and the miner\'s inference cost are always two '
    'separate numbers. Call scout_quote first to price a request without running it.')

_SOURCES = {'type': 'array', 'items': {'type': 'string'},
            'description': 'Source ids from scout_catalog. Use ["auto"] to let Scout pick free sources only.'}

TOOLS = [
    {'name': 'scout_quote',
     'description': 'Price a request before running it. Spends nothing. With q and model it also returns '
                    'the FLOP gateway\'s estimate of the miner\'s cost as a floor (no sources) and a typical '
                    '(three sources) figure. Scout\'s fee and the miner\'s cost are reported separately.',
     'inputSchema': {'type': 'object', 'properties': {
         'sources': _SOURCES,
         'q': {'type': 'string', 'description': 'The question, to size the inference estimate.'},
         'model': {'type': 'string', 'description': 'e.g. flop:qwen/qwen2.5-0.5b-instruct'},
         'max_tokens': {'type': 'integer', 'minimum': 32, 'maximum': 800}}}},
    {'name': 'scout_answer',
     'description': 'Search the chosen sources and return a cited answer with an itemised price. '
                    'FLOP answers need a FLOP gateway key in keys.flop; keys.openrouter works for OpenRouter models.',
     'inputSchema': {'type': 'object', 'required': ['q'], 'properties': {
         'q': {'type': 'string', 'minLength': 2, 'maxLength': 500},
         'sources': _SOURCES,
         'model': {'type': 'string'},
         'n': {'type': 'integer', 'minimum': 1, 'maximum': 8, 'description': 'results per source'},
         'max_tokens': {'type': 'integer', 'minimum': 32, 'maximum': 800},
         'keys': {'type': 'object', 'additionalProperties': {'type': 'string'},
                  'description': 'Your own provider keys (flop, openrouter, exa, ...). Never stored or logged.'}}}},
    {'name': 'scout_catalog',
     'description': 'List the available sources, presets and models.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'scout_pricing',
     'description': 'The full price list in FLOP, the metering mode, and which services are available.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'flop_inference_prices',
     'description': 'Every live FLOP model priced as a whole request by the gateway\'s own estimator: '
                    'FLOP cost, token split, GFLOPs, latency blocks. Needs no key and spends nothing.',
     'inputSchema': {'type': 'object', 'properties': {
         'max_tokens': {'type': 'integer', 'minimum': 32, 'maximum': 800}}}},
    {'name': 'flop_miner_ranking',
     'description': 'FLOP inference miners ranked by settlement rate on chain plus local success and latency.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'flop_miner_history',
     'description': 'One miner\'s settlement history from Scout\'s own 15-minute snapshots, up to 30 days. '
                    'A short span means a short history, not a stable rate.',
     'inputSchema': {'type': 'object', 'required': ['address'], 'properties': {
         'address': {'type': 'string', 'description': 'SS58 miner address'},
         'window': {'type': 'integer', 'description': 'seconds, default 604800 (7 days)'},
         'points': {'type': 'integer', 'minimum': 2, 'maximum': 500}}}},
    {'name': 'flop_alert_subscribe',
     'description': 'Subscribe to a settle-rate drop for one miner or all of them ("*"). '
                    'channel "site" delivers nothing on its own: poll flop_alert_check.',
     'inputSchema': {'type': 'object', 'required': ['threshold'], 'properties': {
         'address': {'type': 'string', 'description': 'SS58 address, or "*" for every miner'},
         'threshold': {'type': 'number', 'minimum': 0, 'maximum': 1},
         'channel': {'type': 'string', 'enum': ['site', 'telegram']},
         'handle': {'type': 'string', 'description': 'Telegram chat id or @username; empty for site'}}}},
    {'name': 'flop_alert_check',
     'description': 'Read back alerts and whether they have fired.',
     'inputSchema': {'type': 'object', 'properties': {
         'address': {'type': 'string', 'description': 'filter to one miner; default all'}}}},
]

# tool name -> (method, path template, how arguments are sent)
_ROUTES = {
    'scout_quote': ('POST', '/api/quote', 'json'),
    'scout_answer': ('POST', '/api/answer', 'json'),
    'scout_catalog': ('GET', '/api/catalog', None),
    'scout_pricing': ('GET', '/api/pricing', None),
    'flop_inference_prices': ('GET', '/api/inference-prices', 'query'),
    'flop_miner_ranking': ('GET', '/api/router', None),
    'flop_miner_history': ('GET', '/api/miners/{address}/history', 'path+query'),
    'flop_alert_subscribe': ('POST', '/api/alerts', 'json'),
    'flop_alert_check': ('GET', '/api/alerts/check', 'query'),
}

# Caps what one tool call can hold a worker for. The answer route has its own
# 200 s inference budget, so it gets the long timeout; the rest are cache reads.
_TIMEOUT = {'scout_answer': 230.0}


def _rpc_error(rid, code, message):
    return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}


def _tool_result(text, is_error=False):
    return {'content': [{'type': 'text', 'text': text}], 'isError': is_error}


async def _call_tool(app, name, args, caller_ip):
    method, path, mode = _ROUTES[name]
    args = dict(args or {})
    if '{address}' in path:
        addr = str(args.pop('address', '')).strip()
        if not addr:
            return _tool_result('address is required', True)
        path = path.replace('{address}', addr)
    kw = {'headers': {'x-forwarded-for': caller_ip or '127.0.0.1'}}
    if mode == 'json':
        kw['json'] = args
    elif mode in ('query', 'path+query'):
        kw['params'] = args
    transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 0))
    try:
        async with httpx.AsyncClient(transport=transport, base_url='http://scout.local',
                                     timeout=_TIMEOUT.get(name, 60.0)) as c:
            r = await c.request(method, path, **kw)
    except Exception as e:
        return _tool_result(f'{name} failed: {type(e).__name__}', True)
    body = r.text
    # 4xx/5xx still carry a useful JSON body (quota, validation); hand it to the
    # model rather than a bare status so it can correct the call.
    return _tool_result(body[:200_000], is_error=r.status_code >= 400)


async def handle(app, msg, caller_ip):
    """Handle one JSON-RPC message. Returns (http_status, body_or_None)."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
        return 400, _rpc_error(None, -32600, 'Invalid Request')
    method, rid = msg.get('method'), msg.get('id')
    params = msg.get('params') or {}

    if rid is None:  # a notification: accepted, no body
        return 202, None
    if method == 'initialize':
        want = params.get('protocolVersion')
        return 200, {'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': want if want in SUPPORTED else SUPPORTED[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': SERVER_INFO,
            'instructions': INSTRUCTIONS}}
    if method == 'ping':
        return 200, {'jsonrpc': '2.0', 'id': rid, 'result': {}}
    if method == 'tools/list':
        return 200, {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': TOOLS}}
    if method == 'tools/call':
        name = params.get('name')
        if name not in _ROUTES:
            return 200, _rpc_error(rid, -32602, f'Unknown tool: {name}')
        res = await _call_tool(app, name, params.get('arguments'), caller_ip)
        return 200, {'jsonrpc': '2.0', 'id': rid, 'result': res}
    return 200, _rpc_error(rid, -32601, f'Method not found: {method}')
