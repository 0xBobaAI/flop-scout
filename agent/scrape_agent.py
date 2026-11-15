#!/usr/bin/env python3
"""
Flop Scout — scrape agent
~~~~~~~~~~~~~~~~~~~~~~~~~
Autonomous agent that sends jobs to the Scout API, records results, and reports
a simple cost/quality summary.  Requires no LLM orchestrator: it drives the
Scout MCP endpoints directly over HTTP.

Usage
-----
  # basic — read jobs from a JSON file, write results to results.jsonl
  python scrape_agent.py jobs.example.json

  # custom output file
  python scrape_agent.py jobs.example.json --out /tmp/run1.jsonl

  # live Scout instance (default: flopscout.com)
  python scrape_agent.py jobs.example.json --base https://flopscout.com

  # local dev instance
  python scrape_agent.py jobs.example.json --base http://127.0.0.1:8787

  # verbose: print each answer to stdout as it arrives
  python scrape_agent.py jobs.example.json --verbose

Job file format
---------------
A JSON array of job objects.  Each job must have either `q` (a question for
scout_answer) or `url` (a page to read and summarise).  Optional fields:

  {
    "q":        "What is the price of GPT-4o on OpenAI?",   // natural language query
    "url":      "https://openai.com/api/pricing",           // read this page
    "sources":  ["web", "read"],    // override sources (default: ["auto"])
    "model":    "flop:qwen/qwen3-14b",   // override model (default: server default)
    "label":    "openai-pricing"    // tag in the output (default: auto)
  }

Either `q` or `url` is required; both can be present (url is appended to q).

Output format (one JSON object per line)
-----------------------------------------
  {
    "label":          "openai-pricing",
    "q":              "...",
    "answer":         "...",
    "citations":      [...],
    "charged_flop":   0.003,
    "inference_flop": 1.824,   // null when no FLOP model was used
    "latency_s":      2.1,
    "error":          null      // set to a string on failure
  }
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def _post(base: str, path: str, payload: dict, timeout: float = 90) -> dict:
    url = base.rstrip('/') + path
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json', 'User-Agent': 'flop-scout-agent/1.0'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read(2048).decode(errors='replace')
        raise RuntimeError(f'HTTP {exc.code} from {path}: {body}') from exc


def _get(base: str, path: str, timeout: float = 30) -> dict:
    url = base.rstrip('/') + path
    req = urllib.request.Request(
        url, headers={'User-Agent': 'flop-scout-agent/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# ── preflight ─────────────────────────────────────────────────────────────────

def preflight(base: str) -> dict:
    """Check the Scout instance is reachable and return its pricing + catalog."""
    try:
        pricing = _get(base, '/api/pricing')
    except Exception as exc:
        print(f'[preflight] /api/pricing failed: {exc}', file=sys.stderr)
        pricing = {}

    try:
        catalog = _get(base, '/api/catalog')
    except Exception as exc:
        print(f'[preflight] /api/catalog failed (non-fatal): {exc}', file=sys.stderr)
        catalog = {}

    mode = pricing.get('metering', {}).get('mode', 'unknown')
    sources = [s['id'] for s in catalog.get('sources', [])]
    models  = [m['model'] for m in catalog.get('models', [])]

    print(f'[preflight] metering={mode}  sources={len(sources)}  models={len(models)}')
    if mode == 'nominal':
        print('[preflight] NOTE: devnet — charges computed but nothing debited')
    return {'pricing': pricing, 'catalog': catalog}


# ── quote ─────────────────────────────────────────────────────────────────────

def quote_job(base: str, job: dict) -> dict:
    """Get a price estimate for a job without running it."""
    q = _build_q(job)
    payload = {
        'sources': job.get('sources', ['auto']),
        'q': q,
    }
    if 'model' in job:
        payload['model'] = job['model']
    return _post(base, '/api/quote', payload, timeout=60)


# ── run ───────────────────────────────────────────────────────────────────────

def run_job(base: str, job: dict) -> dict:
    """Run one job through scout_answer and return a normalised result dict."""
    q       = _build_q(job)
    label   = job.get('label') or _auto_label(q)
    t0      = time.monotonic()

    payload: dict = {
        'q':       q,
        'sources': job.get('sources', ['auto']),
    }
    if 'model' in job:
        payload['model'] = job['model']

    try:
        resp = _post(base, '/api/answer', payload, timeout=200)
    except Exception as exc:
        return {
            'label':          label,
            'q':              q,
            'answer':         None,
            'citations':      [],
            'charged_flop':   None,
            'inference_flop': None,
            'latency_s':      round(time.monotonic() - t0, 2),
            'error':          str(exc),
        }

    inf = resp.get('inference_estimate') or {}
    inf_flop = (inf.get('floor') or {}).get('cost_flop')

    return {
        'label':          label,
        'q':              q,
        'answer':         resp.get('answer', ''),
        'citations':      resp.get('citations', []),
        'charged_flop':   resp.get('charged_flop'),
        'inference_flop': inf_flop,
        'latency_s':      round(time.monotonic() - t0, 2),
        'error':          resp.get('error'),
    }


# ── utils ─────────────────────────────────────────────────────────────────────

def _build_q(job: dict) -> str:
    """Compose the question string from job fields."""
    parts = []
    if 'q' in job:
        parts.append(job['q'])
    if 'url' in job:
        # append the URL so the read source knows what to fetch
        parts.append(f'Read and summarise: {job["url"]}')
    if not parts:
        raise ValueError(f'Job has neither "q" nor "url": {job}')
    return '  '.join(parts)


def _auto_label(q: str) -> str:
    return q[:40].replace(' ', '-').lower().rstrip('-')


# ── summary ───────────────────────────────────────────────────────────────────

def print_summary(results: list[dict]) -> None:
    ok      = [r for r in results if not r['error']]
    failed  = [r for r in results if r['error']]
    total_charged   = sum(r['charged_flop'] or 0 for r in ok)
    total_inference = sum(r['inference_flop'] or 0 for r in ok)
    avg_lat = (sum(r['latency_s'] for r in ok) / len(ok)) if ok else 0

    print('\n── Summary ─────────────────────────────────────────')
    print(f'  jobs:            {len(results)}  ({len(ok)} ok, {len(failed)} failed)')
    print(f'  charged FLOP:    {total_charged:.4f}')
    print(f'  inference est.:  {total_inference:.4f}')
    print(f'  avg latency:     {avg_lat:.1f}s')
    if failed:
        print(f'  failed jobs:')
        for r in failed:
            print(f'    [{r["label"]}] {r["error"]}')
    print('────────────────────────────────────────────────────')


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description='Flop Scout scrape agent')
    parser.add_argument('jobs', help='path to jobs JSON file')
    parser.add_argument('--out',     default='results.jsonl', help='output JSONL file')
    parser.add_argument('--base',    default='https://flopscout.com', help='Scout base URL')
    parser.add_argument('--dry-run', action='store_true', help='quote only, do not run')
    parser.add_argument('--verbose', action='store_true', help='print answers to stdout')
    args = parser.parse_args()

    jobs_path = Path(args.jobs)
    if not jobs_path.exists():
        print(f'error: jobs file not found: {jobs_path}', file=sys.stderr)
        sys.exit(1)

    jobs = json.loads(jobs_path.read_text(encoding='utf-8'))
    if not isinstance(jobs, list):
        print('error: jobs file must be a JSON array', file=sys.stderr)
        sys.exit(1)

    print(f'[agent] {len(jobs)} jobs  base={args.base}  out={args.out}')
    preflight(args.base)

    out_path = Path(args.out)
    results  = []

    for i, job in enumerate(jobs, 1):
        label = job.get('label') or _auto_label(_build_q(job))
        print(f'[{i}/{len(jobs)}] {label} ...', end=' ', flush=True)

        if args.dry_run:
            try:
                q = quote_job(args.base, job)
                print(f'quote: {q.get("total_flop")} FLOP')
            except Exception as exc:
                print(f'quote failed: {exc}')
            continue

        result = run_job(args.base, job)
        results.append(result)

        if result['error']:
            print(f'ERROR: {result["error"]}')
        else:
            flop_str = f'{result["charged_flop"]:.4f}' if result["charged_flop"] is not None else 'n/a'
            print(f'ok  {result["latency_s"]}s  {flop_str} FLOP  {len(result["citations"])} citations')

        if args.verbose and result.get('answer'):
            print(f'\n--- {label} ---')
            print(result['answer'][:800])
            if len(result['answer']) > 800:
                print('  [truncated]')
            print()

        # append immediately so a crash doesn't lose earlier results
        with out_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(result, ensure_ascii=False) + '\n')

    if not args.dry_run and results:
        print_summary(results)
        print(f'[agent] results written to {out_path}')


if __name__ == '__main__':
    main()
