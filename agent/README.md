# Flop Scout test agent

A small agent that reads product and pricing pages through Scout's MCP server and
checks what it gets back. It is a client of Scout, not part of it: it connects to
`POST /mcp` exactly as an external agent would, so a run tests the whole public
path, from the MCP handshake to the page reader to Scout's error codes.

Standard library only. Python 3.10+.

## Run

```bash
# against production
python agent/scrape_agent.py agent/jobs.example.json

# against a local server
python agent/scrape_agent.py agent/jobs.example.json --base http://127.0.0.1:8787

# also ask each job's question through scout_answer (needs a working FLOP key)
python agent/scrape_agent.py agent/jobs.example.json --answer --flop-key "$FLOP_KEY"
```

Exit status is `0` only when every job passes, so a run can gate a deploy.

## What one job does

1. `tools/call scout_read` with the job's URL. No search, no model, no FLOP key.
2. Extracts the requested fields from the page text with regular expressions:
   `price`, `token_price`, `availability`, `rating`.
3. Compares the result with the job's `expect` block.
4. With `--answer`, sends the job's `question` through `scout_answer`, restricted
   to the `page` source. That result is reported on its own line, with its own fee:
   a read and an answer are two services and two charges.

Extraction is deterministic on purpose. Reading works without inference, so the
agent runs while the devnet FLOP allowance is unavailable, and a pass or fail
means the same thing on every run.

## Job format

```json
{
  "label": "books-product",
  "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
  "extract": ["price", "availability"],
  "expect": {"title_contains": "A Light in the Attic", "price": "£51.77"},
  "question": "What does this book cost and is it in stock?"
}
```

| Field | Meaning |
|---|---|
| `url` | Required. The page to read |
| `extract` | Fields to pull from the page text |
| `expect.title_contains` | Text the page title must contain |
| `expect.content_contains` | Text the page body must contain |
| `expect.<field>` | A value that must be among the extracted ones |
| `expect.error` | The read must fail with this Scout error code (tests the guards) |
| `question` | Used only with `--answer` |

A job with no `expect` passes when the page reads and every requested field is found.

## Output

One JSON line per job, appended to `agent-run.jsonl` (change with `--out`):
status, title, final URL, characters read, whether it came from cache, the
extracted fields, Scout's read fee, and the reasons for any failure. Page text is
not stored unless `--verbose` is set, and then only its first 400 characters.

## Being a good client

Keyless reads go through Jina's public reader, which allows about 20 requests a
minute per IP, and every Scout caller shares that budget. The agent waits 4 s
between jobs (`--pause`). Scout answers `503 reader_busy` with `Retry-After` when
the shared budget is spent. A URL read in the last 10 minutes is served from
cache and costs nothing.

Some sites serve a bot check instead of the page; Scout reports that as
`blocked_by_target` rather than returning the check page as content. Whether a
site may be read at all is between you and that site: check its terms first.
