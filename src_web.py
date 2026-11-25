"""Web + FLOP live sources.
Each source: async fn(client, query, n, keys) -> list[{title, url, snippet}]
"""
import asyncio
import ipaddress
import os
import re
import time
from collections import deque
from urllib.parse import quote, urlsplit

import httpx

# Optional deep-read backend. Point SCOUT_URL at your own Scout reader deployment
# to enable the `web_read` source; leave unset and it degrades to search snippets.
SCOUT = os.getenv('SCOUT_URL', '').rstrip('/')
from netcfg import EXPLORER_API as EXPLORER
from netcfg import EXPLORER as EXPLORER_SITE
UA = {'User-Agent': 'flop-scout/0.5'}
_tag = re.compile(r'<[^>]+>')

STOP = set('''what which who whom whose when where why how is are was were be been the a an of in on
for to and or with about current currently price prices situation latest new news tell me give show
explain does do did can could should would will
что как какой какая какие какое кто где когда почему зачем сейчас текущая текущий цена цены курс
про о об и или в на по для расскажи покажи объясни есть ли это vs versus compare сравни'''.split())

_page_cache: dict = {}   # url -> (ts, result)
_PAGE_TTL = 300          # 5 min page cache

# FLOP gateway/explorer snapshots. flop_live used to do three live round trips
# per call (gateway /models, explorer /health, faucet /limits) which measured
# 16.9 s and made the `flop` preset slower than the answer itself. The data
# changes on the order of minutes, so it is cached and refreshed behind the call.
_live_cache: dict = {}
_LIVE_TTL = 60


async def _cached(c, key, url, ttl=_LIVE_TTL, **kw):
    """GET with a small TTL cache. Returns None instead of raising."""
    hit = _live_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        r = await c.get(url, timeout=kw.pop('timeout', 8), **kw)
        if not r.is_success:
            return hit[1] if hit else None
        data = r.json()
        _live_cache[key] = (now, data)
        return data
    except Exception:
        return hit[1] if hit else None


def keywords(q: str, maxw: int = 5) -> str:
    words = [w for w in re.findall(r"[\w$.-]+", q)
             if w.lower().strip('?.!,') not in STOP and len(w) > 1]
    return ' '.join(words[:maxw]) or q


def clean(s, n: int = 1200) -> str:
    return _tag.sub('', s or '').strip()[:n]


# ── Web search ────────────────────────────────────────────────────────────────

async def keenable(c, q, n, keys):
    """Public web index — free, no key required. Primary source for open questions."""
    if keys.get('keenable'):
        r = await c.post('https://api.keenable.ai/v1/search',
                         headers={'X-API-Key': keys['keenable']},
                         json={'query': q, 'max_results': n})
    else:
        r = await c.post('https://api.keenable.ai/v1/search/public',
                         headers={'X-Keenable-Title': 'flop-scout'},
                         json={'query': q, 'max_results': n})
    r.raise_for_status()
    return [{'title': x.get('title', ''), 'url': x.get('url', ''),
             'snippet': clean(x.get('snippet') or x.get('description'))}
            for x in r.json().get('results', [])]


async def exa(c, q, n, keys):
    """Neural web search — requires caller's Exa key (Builder tier)."""
    if not keys.get('exa'):
        raise RuntimeError('Exa key required')
    r = await c.post('https://api.exa.ai/search',
                     headers={'x-api-key': keys['exa']},
                     json={'query': q, 'numResults': n, 'contents': {'highlights': True}})
    r.raise_for_status()
    return [{'title': x.get('title') or '', 'url': x.get('url', ''),
             'snippet': clean(' … '.join(x.get('highlights') or []) or x.get('text', ''))}
            for x in r.json().get('results', [])]


async def web_read(c, q, n, keys):
    """Read and extract relevant passages from the top Keenable result pages.

    Needs an extraction backend (set SCOUT_URL to your own reader). Without it
    this source returns nothing and the registry hides it from presets.
    High value: gives actual page content instead of snippets.
    """
    if not SCOUT:
        return []
    # First get URLs from keenable
    try:
        search_hits = await keenable(c, q, min(n, 3), keys)
    except Exception:
        return []
    if not search_hits:
        return []

    async def extract(hit):
        url = hit['url']
        if url in _page_cache and time.time() - _page_cache[url][0] < _PAGE_TTL:
            return _page_cache[url][1]
        try:
            r = await c.post(f'{SCOUT}/api/web/extract',
                             json={'url': url, 'query': q, 'maxChars': 1800},
                             timeout=20)
            if not r.is_success:
                return None
            j = r.json()
            # API returns 'text' (full page) and optional 'extraction' with matched passages
            extraction = j.get('extraction') or {}
            matches = extraction.get('matches') or extraction.get('passages') or []
            if matches:
                text = ' … '.join(m.get('text', '') or m.get('passage', '') for m in matches[:3])
            else:
                text = j.get('text', '')[:900]
            result = {'title': j.get('title') or hit['title'], 'url': url,
                      'snippet': clean(text, 1000)}
            _page_cache[url] = (time.time(), result)
            return result
        except Exception:
            return None

    results = await asyncio.gather(*[extract(h) for h in search_hits])
    return [r for r in results if r and r.get('snippet')]


# ── Page reading ───────────────────────────────────────────────────────────────
# One URL in, the page's readable text out, through r.jina.ai. Measured from
# the VPS (2026-11): a keyless POST reads ordinary product, pricing and docs
# pages in 0.4-6.6 s and reports the target's own HTTP status and final URL.
# It refuses some domains to anonymous callers (403 AbuseAlleviationError) and
# unresolvable hosts (422). Jina publishes a keyless limit of 20 requests/min
# per IP, and every Scout caller shares this server's IP, so keyless reads go
# through one global budget. A caller's own key bypasses it.

READER_URL = 'https://r.jina.ai/'
READER_RPM = int(os.getenv('READER_RPM', '18'))   # under Jina's keyless 20/min
READ_STORE_CHARS = 20000          # what the cache keeps; callers ask for less
_READ_TTL = 600
_read_cache: dict = {}            # url -> (ts, page)
_reader_calls: deque = deque()    # timestamps of keyless upstream reads
_URL_RE = re.compile(r'https?://[^\s<>"\'\]]+', re.I)
_LOCAL_SUFFIXES = ('.localhost', '.local', '.internal', '.lan', '.home.arpa')
# Titles a site serves instead of the page when it decides the reader is a bot.
# Measured: raspberrypi.com answered 'Just a moment...' to one read in three.
_CHALLENGE_TITLES = ('just a moment', 'attention required', 'access denied', 'verify you are human')


class ReadError(Exception):
    """A read that produced no usable page. `status` is what Scout answers
    with; `code` is the stable, machine-readable reason."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def urls_in(text: str, limit: int = 3) -> list[str]:
    """http(s) URLs written in free text, in order, deduplicated."""
    out = []
    for m in _URL_RE.findall(text or ''):
        u = m.rstrip('.,;:!?\'"')
        # Keep the ')' of a Wikipedia-style path, drop the one closing a sentence.
        while u.endswith(')') and u.count(')') > u.count('('):
            u = u[:-1]
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def check_url(url: str) -> str:
    """Return the URL if Scout will read it, else raise ReadError(422).

    The fetch happens on Jina's side, so this is not what keeps a request off
    Scout's own network. It keeps the reader to public pages: no loopback,
    private or reserved addresses, no intranet names, no credentials.
    """
    u = (url or '').strip()
    if len(u) > 2000:
        raise ReadError(422, 'bad_url', 'URL is longer than 2000 characters')
    try:
        p = urlsplit(u)
        host = (p.hostname or '').rstrip('.')
        p.port                       # raises ValueError on a malformed port
    except ValueError:
        raise ReadError(422, 'bad_url', 'Not a valid URL') from None
    if p.scheme not in ('http', 'https'):
        raise ReadError(422, 'bad_url', 'Only http and https URLs can be read')
    if p.username or p.password:
        raise ReadError(422, 'bad_url', 'URLs carrying credentials are refused')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise ReadError(422, 'bad_url', 'Private, loopback and reserved addresses are refused')
    elif (not host or '.' not in host or host == 'localhost' or host.endswith(_LOCAL_SUFFIXES)
          or re.fullmatch(r'[0-9.]+', host)):   # 127.1 is loopback but not a valid ip_address()
        raise ReadError(422, 'bad_url', 'Only public hostnames can be read')
    return u


def cached_read(url: str):
    hit = _read_cache.get(url)
    if hit and time.time() - hit[0] < _READ_TTL:
        return hit[1]
    return None


async def read_page(c, url: str, keys: dict) -> dict:
    """Read one page through r.jina.ai; the result says whether it came from cache.

    Raises ReadError when no usable page came back: a bot check, a 4xx/5xx from
    the target, an empty page, or a reader that refused, failed or timed out.
    """
    url = check_url(url)
    hit = cached_read(url)
    if hit:
        return {**hit, 'cached': True}
    key = ((keys or {}).get('jina') or '').strip()
    if not key:
        now = time.time()
        while _reader_calls and now - _reader_calls[0] > 60:
            _reader_calls.popleft()
        if len(_reader_calls) >= READER_RPM:
            raise ReadError(503, 'reader_busy', 'The shared page reader is at its per-minute limit. '
                            'Retry in a minute, or pass your own Jina key in keys.jina.', retry_after=60)
        _reader_calls.append(now)
    h = {'Accept': 'application/json', 'X-Retain-Images': 'none'}
    if key:
        h['Authorization'] = f'Bearer {key}'
    try:
        r = await c.post(READER_URL, json={'url': url}, headers=h, timeout=40)
    except httpx.TimeoutException:
        raise ReadError(504, 'reader_timeout', 'The page reader did not answer within 40 s') from None
    except httpx.HTTPError as e:
        raise ReadError(502, 'reader_unreachable', f'Page reader unreachable: {type(e).__name__}') from None
    try:
        j = r.json()
    except ValueError:
        j = {}
    if not isinstance(j, dict):
        j = {}
    if r.status_code == 429:
        raise ReadError(503, 'reader_busy', 'The page reader is rate-limiting Scout. Retry in a minute, '
                        'or pass your own Jina key in keys.jina.', retry_after=60)
    d = j.get('data')
    if r.status_code >= 400 or not isinstance(d, dict):
        msg = str(j.get('readableMessage') or j.get('message') or f'HTTP {r.status_code}')[:240]
        if 'could not be resolved' in msg:
            raise ReadError(422, 'unresolvable', msg)
        if r.status_code in (401, 403) or j.get('name') == 'AbuseAlleviationError':
            raise ReadError(502, 'reader_refused', msg if key else
                            'The reader refuses this domain to anonymous callers. '
                            'Pass your own Jina key in keys.jina.')
        raise ReadError(502, 'reader_error', msg)
    title = (d.get('title') or '').strip()
    status = d.get('httpStatus')
    final = d.get('finalUrl') or d.get('url') or url
    if title.lower().startswith(_CHALLENGE_TITLES):
        raise ReadError(502, 'blocked_by_target',
                        f'The site answered with a bot check ("{title[:60]}") instead of the page',
                        final_url=final)
    if isinstance(status, int) and status >= 400:
        raise ReadError(502, 'target_error', f'The page answered HTTP {status}',
                        http_status=status, final_url=final)
    content = (d.get('content') or '').strip()
    if not content:
        raise ReadError(502, 'empty_page', 'The page has no readable text', final_url=final)
    page = {
        'url': url,
        'final_url': final,
        'redirected': final.rstrip('/') != url.rstrip('/'),
        'title': title,
        'description': (d.get('description') or '').strip()[:500],
        'published_time': d.get('publishedTime'),
        'http_status': status,
        'content': content[:READ_STORE_CHARS],
        'chars_total': len(content),
        'reader': 'r.jina.ai',
    }
    _read_cache[url] = (time.time(), page)
    if len(_read_cache) > 500:
        for k in sorted(_read_cache, key=lambda k: _read_cache[k][0])[:100]:
            _read_cache.pop(k, None)
    return {**page, 'cached': False}


async def page_source(c, q, n, keys):
    """Read the http(s) URLs written in the question, up to 3.

    Free inside an answer like every keyless source: the answer fee covers the
    service. Pages that fail are skipped; if none can be read the source fails
    with the first reason, which lands in source_report.
    """
    urls = urls_in(q, limit=min(max(n, 1), 3))
    if not urls:
        raise RuntimeError('No http(s) URL in the question to read')
    results = await asyncio.gather(*[read_page(c, u, keys) for u in urls], return_exceptions=True)
    out = [{'title': p['title'] or p['final_url'], 'url': p['final_url'], 'snippet': p['content'][:1500]}
           for p in results if isinstance(p, dict)]
    if not out:
        e = results[0]
        raise RuntimeError(e.message if isinstance(e, ReadError) else type(e).__name__)
    return out


# ── FLOP network — the core value prop ───────────────────────────────────────

async def flop_live(c, q, n, keys):
    """Live FLOP network state: models, priced miners, capacity, chain health, faucet.
    This is unique real-time data no browser search can replicate instantly.
    """
    out = []
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())

    # Cached, parallel, and failure-tolerant: a dead faucet must not cost the
    # whole source its answer.
    mods, health, faucet = await asyncio.gather(
        _cached(c, 'gw_models', 'https://api.flop.network/v1/models', timeout=10),
        _cached(c, 'ex_health', f'{EXPLORER}/health', timeout=6),
        _cached(c, 'faucet', 'https://faucet.devnet.flop.network/limits', ttl=600, timeout=6),
    )
    if mods:
        for x in mods.get('data', []):
            xf = x.get('x-flop', {})
            ps = xf.get('price_sources', [])
            countries = ', '.join({d['country_code'] for d in xf.get('datacenters', [])})
            out.append({
                'title': f"FLOP inference: {x.get('name') or x['id']}",
                'url': 'https://api.flop.network/v1/models',
                'snippet': (f"Model {x['id']} · price {x['pricing']['prompt']} FLOP/token (devnet-nominal) · "
                            f"capacity {(xf.get('capacity_tpm') or 0):,} tok/min · {len(ps)} priced miners · "
                            f"countries: {countries} · as of {now}")
            })

    if health:
        j = health
        out.append({
            'title': 'FLOP devnet: chain health',
            'url': EXPLORER_SITE,
            'snippet': (f"Status {j.get('status')}, phase {j.get('phase')}, "
                        f"block {j.get('indexedHead')}, finality lag {j.get('headLag')} blocks · {now}")
        })

    if faucet:
        j = faucet
        fc, sp = j.get('faucet', {}), j.get('sponsor', {})
        dc = fc.get('dailyCap', {})
        out.append({
            'title': 'FLOP devnet: faucet & sponsorship limits',
            'url': 'https://faucet.devnet.flop.network/limits',
            'snippet': (f"Faucet {fc.get('status')} · daily cap {dc.get('limitFlop')} FLOP, "
                        f"used {dc.get('usedFlop')}, remaining {dc.get('remainingFlop')} · "
                        f"miner sponsorship: {sp.get('miner', {}).get('status')} · "
                        f"validator sponsorship: {sp.get('validator', {}).get('status')} · {now}")
        })
    return out[:n]


async def flop_miners_live(c, q, n, keys):
    """Live miner performance from the FLOP explorer: settlement rates, GPU, stake.
    Unique signal for 'which miners are reliable?' — not available anywhere else.
    """
    kw = keywords(q)

    # Fetch top priced miners from gateway (cached — the flop preset asks for
    # this at the same time as flop_live, and both used to fetch the same URL)
    mods = await _cached(c, 'gw_models', 'https://api.flop.network/v1/models', timeout=15)
    if not mods:
        return []
    priced = []
    for x in mods.get('data', []):
        for ps in x.get('x-flop', {}).get('price_sources', []):
            addr = ps.get('miner_address')
            if addr and addr not in [p['address'] for p in priced]:
                priced.append({'address': addr, 'price': ps.get('flop_per_gn'), 'model': x['id']})

    # Fetch details for top miners in parallel. Capped at 4 and cached per
    # address: six simultaneous /miners/{addr} calls measured >15 s each on the
    # devnet indexer, which is exactly the budget the whole source has.
    sample = priced[:min(4, n)]

    async def detail(pm):
        try:
            d = await _cached(c, f'miner:{pm["address"]}', f'{EXPLORER}/miners/{pm["address"]}', ttl=300, timeout=12)
            if not d:
                return None
            w = d.get('work') or {}
            cal = d.get('calibration') or {}
            gpus = (d.get('declaredGpuInventory') or {}).get('gpus') or []
            gpu_str = ', '.join(f"{g.get('name', '?')} ×{g.get('count', 1)}" for g in gpus[:2]) or 'GPU unknown'
            settled = w.get('settled', 0)
            rs = w.get('recentSessions') or []
            closed = sum(1 for s in rs if s['status'] in ('settled', 'timedout'))
            rate = round(settled / closed, 2) if closed else None
            return {
                'title': f"FLOP miner {pm['address'][:10]}…",
                'url': f"{EXPLORER_SITE}/account/{pm['address']}",
                'snippet': (f"GPU: {gpu_str} · calibration: {cal.get('phase', '?')} "
                            f"({cal.get('empiricalCapGflopsPerSec') or cal.get('capGflopsPerSec', '?')} GFlop/s) · "
                            f"settled sessions: {settled} · "
                            f"recent settlement rate: {f'{rate:.0%}' if rate is not None else 'unknown'} · "
                            f"price: {pm['price']} FLOP/G_n · model: {pm['model']}")
            }
        except Exception:
            return None

    results = await asyncio.gather(*[detail(pm) for pm in sample])
    return [r for r in results if r]


async def flop_network_stats(c, q, n, keys):
    """Aggregate FLOP network statistics from the devnet explorer.
    Good for questions about network health, total activity, validator count.
    """
    j = await _cached(c, 'ex_stats', f'{EXPLORER}/stats', timeout=15)
    if not j:
        return []
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    n = lambda v: f'{v:,}' if isinstance(v, (int, float)) else 'n/a'
    return [{
        'title': 'FLOP devnet: network statistics',
        'url': EXPLORER_SITE,
        'snippet': (f"Block {n(j.get('latestBlockNumber'))} · TPS {j.get('tps')} · "
                    f"registered miners: {j.get('registeredMiners')} · "
                    f"active validators: {j.get('activeValidators')} · "
                    f"active agents: {n(j.get('activeAgents'))} · "
                    f"total accounts: {n(j.get('totalAccounts'))} · "
                    f"HTLC sessions: {n(j.get('totalHTLCs'))} (active: {n(j.get('activeHTLCs'))}) · "
                    f"24h tx volume: {n(j.get('txVolume24h'))} · as of {now}")
    }]


# ── Dev sources ───────────────────────────────────────────────────────────────

async def github(c, q, n, keys):
    """GitHub repo search — only relevant for 'find me a library/tool' questions."""
    kw = keywords(q)
    h = dict(UA, Accept='application/vnd.github+json')
    if keys.get('github'):
        h['Authorization'] = f"Bearer {keys['github']}"
    r = await c.get('https://api.github.com/search/repositories',
                    params={'q': kw, 'per_page': n, 'sort': 'stars'}, headers=h)
    r.raise_for_status()
    return [{'title': x['full_name'], 'url': x['html_url'],
             'snippet': (f"★{x['stargazers_count']:,} · "
                         f"updated {x['pushed_at'][:10]} · "
                         f"{x.get('language') or ''} · "
                         f"{clean(x.get('description'))}")}
            for x in r.json().get('items', [])]


async def hackernews(c, q, n, keys):
    """HN discussions — good for 'what do devs think about X' but noisy for factual questions."""
    kw = keywords(q)
    r = await c.get('https://hn.algolia.com/api/v1/search',
                    params={'query': kw, 'hitsPerPage': n, 'tags': 'story'})
    r.raise_for_status()
    return [{'title': x.get('title') or '',
             'url': x.get('url') or f"https://news.ycombinator.com/item?id={x['objectID']}",
             'snippet': f"{x.get('points', 0)} pts · {x.get('num_comments', 0)} comments · {x.get('created_at', '')[:10]}"}
            for x in r.json().get('hits', [])]


# ── Reference ────────────────────────────────────────────────────────────────

async def wikipedia(c, q, n, keys):
    """Wikipedia search — definitions, entities, background.

    Free and keyless. Rescued from the deleted src_data.py prototype; useful
    precisely because it is the opposite of a search engine: one canonical
    article instead of ten competing snippets.
    """
    kw = keywords(q)
    r = await c.get('https://en.wikipedia.org/w/rest.php/v1/search/page',
                    params={'q': kw, 'limit': n}, headers=UA)
    r.raise_for_status()
    out = []
    for p in r.json().get('pages', []):
        desc = p.get('description') or ''
        out.append({
            'title': p.get('title') or '',
            'url': f"https://en.wikipedia.org/wiki/{p.get('key', '')}",
            'snippet': clean(p.get('excerpt')) + (f' — {desc}' if desc else ''),
        })
    return out


# ── Crypto / finance ─────────────────────────────────────────────────────────

async def crypto(c, q, n, keys):
    """CoinGecko live prices — real-time, authoritative for token prices.

    CoinGecko's /search is a name search, not a relevance search: passing several
    words ("bitcoin ethereum") reliably returns zero results. Query the terms one
    at a time and concatenate, which is also what a multi-token question means.
    """
    terms = [w for w in keywords(q).split() if len(w) > 1][:4]
    per_term = max(1, n // max(1, len(terms)))   # a 3-token question shouldn't be 3 Bitcoin forks
    seen, coins = set(), []
    for term in terms:
        r = await c.get('https://api.coingecko.com/api/v3/search', params={'query': term}, headers=UA)
        if not r.is_success:
            continue
        added = 0
        for x in r.json().get('coins', []):
            if added >= per_term:
                break
            if x['id'] not in seen:
                seen.add(x['id'])
                coins.append(x)
                added += 1
    coins = coins[:n]
    if not coins:
        return []
    p = await c.get('https://api.coingecko.com/api/v3/simple/price', headers=UA,
                    params={'ids': ','.join(x['id'] for x in coins),
                            'vs_currencies': 'usd',
                            'include_market_cap': 'true',
                            'include_24hr_change': 'true'})
    prices = p.json() if p.is_success else {}
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    out = []
    for x in coins:
        d = prices.get(x['id'], {})
        price = f"${d['usd']:,}" if 'usd' in d else 'n/a'
        ch = f", {d['usd_24h_change']:+.1f}% 24h" if d.get('usd_24h_change') is not None else ''
        mc = f", mcap ${d['usd_market_cap'] / 1e9:,.2f}B" if d.get('usd_market_cap') else ''
        out.append({'title': f"{x['name']} ({x['symbol'].upper()})",
                    'url': f"https://www.coingecko.com/en/coins/{x['id']}",
                    'snippet': f"Price {price}{ch}{mc} · rank #{x.get('market_cap_rank') or '?'} · CoinGecko {now}"})
    return out


async def defillama(c, q, n, keys):
    """DeFi protocol TVL — free, keyless, hourly cache.

    /protocols is a several-megabyte payload, so it is cached for an hour and
    only fetched when this source is selected. Also rescued from src_data.py.
    """
    cached = _live_cache.get('llama')
    now = time.time()
    if not cached or now - cached[0] > 3600:
        try:
            r = await c.get('https://api.llama.fi/protocols', timeout=30)
            r.raise_for_status()
            _live_cache['llama'] = (now, r.json())
        except Exception:
            if not cached:
                return []
    data = _live_cache.get('llama', (0, []))[1]
    words = [w for w in re.findall(r'\w+', (q or '').lower()) if len(w) > 2]
    hits = [p for p in data
            if any(w in (p.get('name') or '').lower() or w == (p.get('symbol') or '').lower()
                   for w in words)]
    hits.sort(key=lambda p: p.get('tvl') or 0, reverse=True)
    return [{'title': f"{p['name']} · {p.get('category', '')}",
             'url': f"https://defillama.com/protocol/{p.get('slug', '')}",
             'snippet': (f"TVL ${(p.get('tvl') or 0) / 1e6:,.1f}M · 7d {p.get('change_7d') or 0:+.1f}% · "
                         f"chains: {', '.join((p.get('chains') or [])[:6])}. "
                         f"{clean(p.get('description'), 400)}")}
            for p in hits[:n]]


async def arxiv(c, q, n, keys):
    """arXiv research papers — only for academic / research questions."""
    import xml.etree.ElementTree as ET
    kw = keywords(q)
    r = await c.get('https://export.arxiv.org/api/query',
                    params={'search_query': f'all:{kw}', 'max_results': n})
    r.raise_for_status()
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    return [{'title': clean(e.findtext('a:title', '', ns), 300),
             'url': e.findtext('a:id', '', ns),
             'snippet': f"{e.findtext('a:published', '', ns)[:10]} · " + clean(e.findtext('a:summary', '', ns))}
            for e in ET.fromstring(r.text).findall('a:entry', ns)]


# ── Additional search providers for routing ───────────────────────────────────
# Each is a separate route. Free ones first, then key-gated.

async def jina(c, q, n, keys):
    """Jina Reader — readable page text from a search query.

    Note: s.jina.ai now answers 401 without a token (measured, 2026-11), so this
    source is key-gated in the registry rather than advertised as free. It still
    has no cost to Scout when the caller brings their own key.
    """
    h = {'Accept': 'application/json'}
    if keys.get('jina'):
        h['Authorization'] = f'Bearer {keys["jina"]}'
    try:
        # The query goes into the path, so it has to be quoted: an unescaped '#'
        # truncated the question ("C# vs Java" searched for "C").
        r = await c.get(f'https://s.jina.ai/{quote(q, safe="")}', headers=h, timeout=20)
        if not r.is_success:
            return []
        j = r.json()
        items = j.get('data') or j.get('results') or []
        out = []
        for it in items[:n]:
            url = it.get('url') or it.get('link') or ''
            title = it.get('title') or url
            # content is often the readable page text
            text = it.get('content') or it.get('text') or it.get('description') or ''
            out.append({'title': title, 'url': url, 'snippet': clean(text, 800)})
        return out
    except Exception:
        return []


async def tavily(c, q, n, keys):
    """Tavily — AI-native search, requires caller's Tavily key (Builder tier).
    Good for agent-focused queries with structured results.
    """
    if not keys.get('tavily'):
        raise RuntimeError('Tavily key required')
    r = await c.post('https://api.tavily.com/search',
                     headers={'Authorization': f'Bearer {keys["tavily"]}',
                              'Content-Type': 'application/json'},
                     json={'query': q, 'max_results': n, 'search_depth': 'advanced',
                           'include_answer': False}, timeout=25)
    r.raise_for_status()
    j = r.json()
    return [{'title': x.get('title', ''), 'url': x.get('url', ''),
             'snippet': clean(x.get('content') or x.get('raw_content') or '')}
            for x in j.get('results', [])]


async def brave(c, q, n, keys):
    """Brave Search API — requires caller's Brave key (Builder tier).
    Independent index, good privacy-focused alternative to Keenable.
    """
    if not keys.get('brave'):
        raise RuntimeError('Brave key required')
    r = await c.get('https://api.search.brave.com/res/v1/web/search',
                    headers={'X-Subscription-Token': keys['brave'], 'Accept': 'application/json'},
                    params={'q': q, 'count': min(n, 20)}, timeout=20)
    r.raise_for_status()
    j = r.json()
    results = (j.get('web') or {}).get('results') or []
    return [{'title': x.get('title', ''), 'url': x.get('url', ''),
             'snippet': clean(x.get('description') or x.get('extra_snippets', [None])[0] or '')}
            for x in results[:n]]


async def firecrawl(c, q, n, keys):
    """Firecrawl search — requires caller's Firecrawl key (Builder tier).
    Returns full page content, not just snippets. Good for deep research.
    """
    if not keys.get('firecrawl'):
        raise RuntimeError('Firecrawl key required')
    r = await c.post('https://api.firecrawl.dev/v1/search',
                     headers={'Authorization': f'Bearer {keys["firecrawl"]}',
                              'Content-Type': 'application/json'},
                     json={'query': q, 'limit': n, 'scrapeOptions': {'formats': ['markdown']}},
                     timeout=30)
    r.raise_for_status()
    j = r.json()
    items = j.get('data') or []
    return [{'title': x.get('metadata', {}).get('title', x.get('url', '')),
             'url': x.get('url', ''),
             'snippet': clean(x.get('markdown') or x.get('content') or '', 800)}
            for x in items]


async def serper(c, q, n, keys):
    """Serper.dev — Google Search API, requires caller's Serper key (Builder tier).
    Good when you specifically need Google's ranking.
    """
    if not keys.get('serper'):
        raise RuntimeError('Serper key required')
    r = await c.post('https://google.serper.dev/search',
                     headers={'X-API-KEY': keys['serper'], 'Content-Type': 'application/json'},
                     json={'q': q, 'num': min(n, 10)}, timeout=15)
    r.raise_for_status()
    j = r.json()
    out = []
    for x in j.get('organic', [])[:n]:
        out.append({'title': x.get('title', ''), 'url': x.get('link', ''),
                    'snippet': clean(x.get('snippet') or '')})
    return out
