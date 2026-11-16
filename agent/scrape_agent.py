#!/usr/bin/env python3
"""Flop Scout test agent: reads product and pricing pages through Scout's MCP server.

It is a client of Scout, not part of it. It connects to POST /mcp exactly as an
external agent would (initialize, tools/list, tools/call), reads each job's page
with the scout_read tool, extracts what it was asked to look for, and checks the
result against what the job says it should find. That turns one run into a
regression test of the whole public path: MCP transport, the page reader, and
Scout's error codes.

Extraction is deterministic (regular expressions) on purpose. Page reading works
with no FLOP key, so the agent runs while the devnet allowance is unavailable,
and a pass or fail means the same thing on every run. When a working inference
key exists, --answer also sends each page's question through scout_answer, and
that call is reported as its own line, never mixed into the read result.

Standard library only.

Usage
-----
  python scrape_agent.py jobs.example.json
  python scrape_agent.py jobs.example.json --base http://127.0.0.1:8787
  python scrape_agent.py jobs.example.json --answer --flop-key "$KEY"
  python scrape_agent.py jobs.example.json --out run.jsonl --verbose

Each job may set `expect` to state what a correct read contains:

  {"label": "books", "url": "https://...", "extract": ["price", "availability"],
   "expect": {"title_contains": "Attic", "price": "£51.77"}}

A job without `expect` passes when the page reads and every requested field is
found. A job with `"expect": {"error": "bad_url"}` passes only if Scout refuses
it with that code, which is how the agent checks the guards. Exit status is 0
only if every job passes, so the script can gate a deploy.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = 'flop-scout-test-agent/1.0 (+https://github.com/0xBobaAI/flop-scout/tree/main/agent)'
MAX_CHARS = 20000

# What each field looks like in the Markdown the reader returns. Ordered from
# most to least specific; the first pattern with a match wins.
_PRICE = r'(?:US\$|\$|€|£|¥)\s?\d[\d,]*(?:\.\d{1,2})?'
EXTRACTORS = {
    'price': [re.compile(_PRICE)],
    'token_price': [
        re.compile(r'(?:\$|US\$)\s?\d+(?:\.\d+)?\s*(?:/|per)\s*(?:1M|1 ?million|million|M)\b(?:\s*(?:input|output)?\s*tokens?)?', re.I),
        re.compile(r'(?:\$|US\$)\s?\d+(?:\.\d+)?\s*(?:/|per)\s*(?:1K|1,000|thousand)\s*tokens?', re.I),
    ],
    'availability': [re.compile(r'\b(?:in stock(?: \(\d+ available\))?|out of stock|sold out|pre-?order|available now|unavailable)\b', re.I)],
    'rating': [re.compile(r'\b(?:One|Two|Three|Four|Five) stars?\b|\b\d(?:\.\d)?\s?(?:/|out of)\s?5\b', re.I)],
}


class MCPError(Exception):
    pass


class MCPClient:
    """Minimal Streamable HTTP client: one JSON-RPC message per POST, JSON replies."""

    def __init__(self, url: str, timeout: float = 90):
        self.url, self.timeout, self._id = url, timeout, 0

    def _post(self, payload: dict):
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'User-Agent': UA,
                     'Accept': 'application/json, text/event-stream'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            raise MCPError(f'HTTP {e.code}: {e.read(300).decode(errors="replace")}') from None
        except urllib.error.URLError as e:
            raise MCPError(f'unreachable: {e.reason}') from None

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = self._post({'jsonrpc': '2.0', 'id': self._id, 'method': method, 'params': params or {}})
        if not isinstance(msg, dict):
            raise MCPError(f'{method}: empty reply')
        if 'error' in msg:
            raise MCPError(f'{method}: {msg["error"].get("message")}')
        return msg['result']

    def initialize(self) -> dict:
        info = self.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                        'clientInfo': {'name': 'flop-scout-test-agent', 'version': '1.0'}})
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return info

    def tool(self, name: str, args: dict) -> tuple[bool, dict | str]:
        """(ok, parsed body). Tool failures come back as isError, not exceptions."""
        res = self.call('tools/call', {'name': name, 'arguments': args})
        text = ''.join(c.get('text', '') for c in res.get('content', []) if c.get('type') == 'text')
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return not res.get('isError'), body


def extract(content: str, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        found = []
        for rx in EXTRACTORS.get(f, []):
            for m in rx.findall(content):
                v = ' '.join(m.split()) if isinstance(m, str) else m
                if v not in found:
                    found.append(v)
            if found:
                break
        out[f] = found[:5]
    return out


def check(job: dict, ok: bool, body, fields: dict) -> list[str]:
    """Return the reasons a job failed; empty means it passed."""
    exp = job.get('expect') or {}
    if 'error' in exp:
        if ok:
            return [f'expected error {exp["error"]}, but the read succeeded']
        got = body.get('error') if isinstance(body, dict) else None
        return [] if got == exp['error'] else [f'expected error {exp["error"]}, got {got}']
    if not ok:
        code = body.get('error') if isinstance(body, dict) else None
        msg = body.get('message') if isinstance(body, dict) else str(body)[:120]
        return [f'read failed: {code}: {msg}']
    problems = []
    title = body.get('title') or ''
    if 'title_contains' in exp and exp['title_contains'].lower() not in title.lower():
        problems.append(f'title {title!r} lacks {exp["title_contains"]!r}')
    if 'content_contains' in exp and exp['content_contains'] not in body.get('content', ''):
        problems.append(f'content lacks {exp["content_contains"]!r}')
    for f in job.get('extract', []):
        want = exp.get(f)
        got = fields.get(f) or []
        if want is not None and want not in got:
            problems.append(f'{f}: expected {want!r}, found {got[:3]}')
        elif want is None and not got:
            problems.append(f'{f}: nothing found')
    return problems


def run_job(mcp: MCPClient, job: dict, args) -> dict:
    label = job.get('label') or job['url'][:40]
    t0 = time.monotonic()
    rec = {'label': label, 'url': job['url'], 'ts': round(time.time())}
    try:
        ok, body = mcp.tool('scout_read', {'url': job['url'], 'max_chars': MAX_CHARS})
    except MCPError as e:
        rec.update(ok=False, passed=False, problems=[f'transport: {e}'],
                   latency_s=round(time.monotonic() - t0, 2))
        return rec
    rec['latency_s'] = round(time.monotonic() - t0, 2)
    content = body.get('content', '') if ok and isinstance(body, dict) else ''
    fields = extract(content, job.get('extract', [])) if content else {}
    problems = check(job, ok, body, fields)
    rec.update(ok=ok, passed=not problems, problems=problems, fields=fields)
    if isinstance(body, dict):
        if ok:
            rec.update(title=body.get('title'), final_url=body.get('final_url'),
                       http_status=body.get('http_status'), chars=body.get('chars'),
                       chars_total=body.get('chars_total'), cached=body.get('cached'),
                       read_s=body.get('read_s'),
                       scout_fee_flop=(body.get('pricing') or {}).get('total_flop'))
        else:
            rec.update(error=body.get('error'), message=body.get('message'))
    if args.verbose and content:
        rec['content_head'] = content[:400]

    if args.answer and ok and job.get('question'):
        # A separate service and a separate charge: reported on its own, never
        # folded into the read's fee or pass/fail.
        q = f'{job["question"]} {job["url"]}'
        keys = {'flop': args.flop_key} if args.flop_key else {}
        t1 = time.monotonic()
        try:
            aok, ab = mcp.tool('scout_answer', {'q': q[:500], 'sources': ['page'], 'keys': keys})
        except MCPError as e:
            aok, ab = False, {'error': str(e)}
        first = ((ab.get('answers') or [{}])[0]) if isinstance(ab, dict) else {}
        rec['answer'] = {
            'ok': bool(aok and first.get('answer')),
            'text': (first.get('answer') or '')[:600],
            'error': first.get('error') or (ab.get('error') if isinstance(ab, dict) else None),
            'model': first.get('model'),
            'scout_fee_flop': first.get('scout_quote_flop'),
            'miner_cost_estimate_flop': ((first.get('inference_estimate') or {}).get('cost_flop')),
            'latency_s': round(time.monotonic() - t1, 2),
        }
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description='Flop Scout test agent (MCP)')
    ap.add_argument('jobs', help='JSON file with an array of jobs')
    ap.add_argument('--base', default='https://flopscout.com', help='Scout base URL')
    ap.add_argument('--out', default='agent-run.jsonl', help='append one JSON line per job here')
    ap.add_argument('--pause', type=float, default=4.0,
                    help='seconds between jobs; the shared reader allows about 18 reads a minute')
    ap.add_argument('--answer', action='store_true', help='also ask each job\'s question via scout_answer')
    ap.add_argument('--flop-key', default='', help='FLOP gateway key for --answer (never written to output)')
    ap.add_argument('--verbose', action='store_true', help='keep the first 400 chars of each page in the output')
    args = ap.parse_args()

    jobs = json.loads(Path(args.jobs).read_text(encoding='utf-8'))
    if not isinstance(jobs, list) or not all(isinstance(j, dict) and j.get('url') for j in jobs):
        print('error: the jobs file must be a JSON array of objects with a "url"', file=sys.stderr)
        return 2

    mcp = MCPClient(args.base.rstrip('/') + '/mcp')
    try:
        info = mcp.initialize()
        tools = {t['name'] for t in mcp.call('tools/list')['tools']}
    except MCPError as e:
        print(f'error: cannot reach Scout MCP at {mcp.url}: {e}', file=sys.stderr)
        return 2
    if 'scout_read' not in tools:
        print(f'error: this Scout ({info["serverInfo"].get("version")}) has no scout_read tool', file=sys.stderr)
        return 2
    print(f'Scout {info["serverInfo"].get("version")} at {args.base}: {len(tools)} tools, '
          f'protocol {info.get("protocolVersion")}. {len(jobs)} jobs.')

    out = Path(args.out)
    results = []
    for i, job in enumerate(jobs, 1):
        if i > 1 and args.pause > 0:
            time.sleep(args.pause)
        rec = run_job(mcp, job, args)
        results.append(rec)
        with out.open('a', encoding='utf-8') as f:     # append now: a crash keeps earlier jobs
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        mark = 'PASS' if rec['passed'] else 'FAIL'
        if rec.get('ok'):
            got = '; '.join(f'{k}={v[:2]}' for k, v in (rec.get('fields') or {}).items())
            detail = f'{rec.get("chars")}/{rec.get("chars_total")} chars{", cached" if rec.get("cached") else ""}. {got}'
        else:
            detail = f'{rec.get("error")}: {(rec.get("message") or "")[:70]}'
        print(f'[{i}/{len(jobs)}] {mark} {rec["label"]:<22} {rec["latency_s"]:5.1f}s  {detail}')
        for p in rec['problems']:
            print(f'        - {p}')
        if 'answer' in rec:
            a = rec['answer']
            print(f'        answer: {"ok" if a["ok"] else "no answer"} '
                  f'({a.get("error") or a.get("model")}), fee {a.get("scout_fee_flop")} FLOP, '
                  f'miner est. {a.get("miner_cost_estimate_flop")} FLOP')

    passed = sum(r['passed'] for r in results)
    reads = [r for r in results if r.get('ok')]
    fresh = [r for r in reads if not r.get('cached')]
    fee = sum(r.get('scout_fee_flop') or 0 for r in reads)
    print(f'\n{passed}/{len(results)} passed. {len(reads)} pages read ({len(fresh)} fetched, '
          f'{len(reads) - len(fresh)} from cache). Scout read fees: {fee:.3f} FLOP (nominal on devnet).')
    if fresh:
        lat = sorted(r['latency_s'] for r in fresh)
        print(f'Fetch latency: median {lat[len(lat) // 2]:.1f}s, max {lat[-1]:.1f}s.')
    print(f'Results appended to {out}')
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
