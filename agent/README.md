# Flop Scout — scrape agent

A standalone Python script that drives the Scout API to parse product pages and
answer research questions.  No LLM orchestrator, no extra dependencies — only
the standard library.

## What it does

1. Reads a list of jobs from a JSON file (questions, URLs, or both)
2. Calls `GET /api/pricing` and `GET /api/catalog` to verify the Scout instance
3. Runs each job through `POST /api/answer`
4. Writes one JSON object per line to a results file
5. Prints a cost/latency summary

## Quickstart

```bash
# run the example job list against flopscout.com
python scrape_agent.py jobs.example.json --verbose

# dry-run: quote every job without running it
python scrape_agent.py jobs.example.json --dry-run

# local dev instance
python scrape_agent.py jobs.example.json --base http://127.0.0.1:8787

# custom output file
python scrape_agent.py jobs.example.json --out /tmp/my-run.jsonl
```

## Job file format

A JSON array.  Each job needs at least `q` or `url` (or both):

```json
[
  {
    "q":      "What is the current price of GPT-4o?",
    "url":    "https://openai.com/api/pricing",
    "sources": ["web", "read"],
    "model":  "flop:qwen/qwen3-14b",
    "label":  "openai-pricing"
  }
]
```

| Field | Required | Description |
|---|---|---|
| `q` | one of `q`/`url` | Natural language question |
| `url` | one of `q`/`url` | Page to read and summarise |
| `sources` | no | Source ids from `/api/catalog`; default `["auto"]` |
| `model` | no | Model id; default is Scout's server default |
| `label` | no | Tag in the output; auto-derived from `q` if omitted |

## Output format

One JSON object per line (`results.jsonl`):

```json
{
  "label":          "openai-pricing",
  "q":              "...",
  "answer":         "GPT-4o costs $2.50/M input tokens...",
  "citations":      [{"url": "...", "title": "...", "snippet": "..."}],
  "charged_flop":   0.003,
  "inference_flop": 1.824,
  "latency_s":      3.1,
  "error":          null
}
```

## Notes

- On devnet `charged_flop` is computed but nothing is debited (`metering=nominal`)
- `inference_flop` is the FLOP gateway's estimate of the miner cost — separate from the Scout fee
- Results are appended immediately: a crash does not lose earlier output
- The agent respects the Scout rate limit; slow down with `--dry-run` first to estimate cost
