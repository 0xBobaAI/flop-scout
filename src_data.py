"""Knowledge, dev and crypto sources."""
import re
import time
import xml.etree.ElementTree as ET

from src_web import UA, clean, keywords

_cache = {}


async def arxiv(c, q, n, keys):
    q = keywords(q)
    r = await c.get('https://export.arxiv.org/api/query', params={'search_query': f'all:{q}', 'max_results': n})
    r.raise_for_status()
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    return [{'title': clean(e.findtext('a:title', '', ns), 300), 'url': e.findtext('a:id', '', ns),
             'snippet': f"{e.findtext('a:published', '', ns)[:10]} · " + clean(e.findtext('a:summary', '', ns))}
            for e in ET.fromstring(r.text).findall('a:entry', ns)]


async def wikipedia(c, q, n, keys):
    q = keywords(q)
    r = await c.get('https://en.wikipedia.org/w/rest.php/v1/search/page', params={'q': q, 'limit': n}, headers=UA)
    r.raise_for_status()
    return [{'title': p['title'], 'url': f"https://en.wikipedia.org/wiki/{p['key']}",
             'snippet': clean(p.get('excerpt')) + (' — ' + p['description'] if p.get('description') else '')}
            for p in r.json().get('pages', [])]


async def github(c, q, n, keys):
    q = keywords(q)
    h = dict(UA, Accept='application/vnd.github+json')
    if keys.get('github'):
        h['Authorization'] = f"Bearer {keys['github']}"
    r = await c.get('https://api.github.com/search/repositories', params={'q': q, 'per_page': n, 'sort': 'stars'}, headers=h)
    r.raise_for_status()
    return [{'title': x['full_name'], 'url': x['html_url'],
             'snippet': f"★{x['stargazers_count']} · updated {x['pushed_at'][:10]} · {x.get('language') or ''} · {clean(x.get('description'))}"}
            for x in r.json().get('items', [])]


async def hackernews(c, q, n, keys):
    q = keywords(q)
    r = await c.get('https://hn.algolia.com/api/v1/search', params={'query': q, 'hitsPerPage': n, 'tags': 'story'})
    r.raise_for_status()
    return [{'title': x.get('title') or '', 'url': x.get('url') or f"https://news.ycombinator.com/item?id={x['objectID']}",
             'snippet': f"{x.get('points', 0)} points · {x.get('num_comments', 0)} comments · {x.get('created_at', '')[:10]}"}
            for x in r.json().get('hits', [])]


async def crypto(c, q, n, keys):
    q = keywords(q)
    r = await c.get('https://api.coingecko.com/api/v3/search', params={'query': q}, headers=UA)
    r.raise_for_status()
    coins = r.json().get('coins', [])[:n]
    if not coins:
        return []
    p = await c.get('https://api.coingecko.com/api/v3/simple/price', headers=UA,
                    params={'ids': ','.join(x['id'] for x in coins), 'vs_currencies': 'usd',
                            'include_market_cap': 'true', 'include_24hr_change': 'true'})
    prices = p.json() if p.is_success else {}
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    out = []
    for x in coins:
        d = prices.get(x['id'], {})
        price = f"${d['usd']:,}" if 'usd' in d else 'n/a'
        ch = f", {d['usd_24h_change']:+.1f}% 24h" if d.get('usd_24h_change') is not None else ''
        mc = f", market cap ${d['usd_market_cap'] / 1e9:,.2f}B" if d.get('usd_market_cap') else ''
        out.append({'title': f"{x['name']} ({x['symbol']})", 'url': f"https://www.coingecko.com/en/coins/{x['id']}",
                    'snippet': f"Price {price}{ch}{mc}; market-cap rank: {x.get('market_cap_rank') or 'n/a'}. CoinGecko, {now}."})
    return out


async def defillama(c, q, n, keys):
    q = keywords(q)
    if 'llama' not in _cache or time.time() - _cache['llama'][0] > 3600:
        r = await c.get('https://api.llama.fi/protocols', timeout=60)
        r.raise_for_status()
        _cache['llama'] = (time.time(), r.json())
    words = [w for w in re.findall(r'\w+', q.lower()) if len(w) > 2]
    hits = [p for p in _cache['llama'][1]
            if any(w in (p.get('name') or '').lower() or w == (p.get('symbol') or '').lower() for w in words)]
    hits.sort(key=lambda p: p.get('tvl') or 0, reverse=True)
    return [{'title': f"{p['name']} · {p.get('category', '')}", 'url': f"https://defillama.com/protocol/{p.get('slug', '')}",
             'snippet': f"TVL ${(p.get('tvl') or 0) / 1e6:,.1f}M; 7d {p.get('change_7d') or 0:+.1f}%; "
                        f"chains: {', '.join((p.get('chains') or [])[:6])}. {clean(p.get('description'), 400)}"}
            for p in hits[:n]]
