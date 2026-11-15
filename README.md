# Flop Scout

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Live](https://img.shields.io/badge/live-flopscout.com-brightgreen)](https://flopscout.com)
[![Network](https://img.shields.io/badge/network-FLOP%20devnet-blue)](https://docs.flop.network)

> **Search and page-reading that agents can pay for — billed in FLOP, per call, quotable before you commit.**

Most agent tools assume a human with a credit card: sign up, attach a Visa, buy credits, hope nobody drains the account. FLOP is built for the opposite case — an agent with its own wallet paying per call for what it consumes.

Scout is a working service on the other side of that: the search, page reading and on-chain data an agent needs, priced in FLOP, no card and no subscription.

Five pages, one backend:

| Surface | What it is |
|---|---|
| **`/`** | Answer — 29 sources, cited answers, itemised FLOP price, on-chain receipt |
| **`/miners`** | Every registered miner ranked by settlement performance, with a 24h trend |
| **`/analytics`** | Network usage: per-model success, payer split, quoted vs charged |
| **`/docs`** | Endpoints, pricing table, error codes, integration guide |
| **`/api/*`** | The same data as JSON, for agents rather than people |

Start with **`AUDIT.md`** if you want the engineering review, the honest valuation of
the service inside the FLOP ecosystem, and the roadmap.

Built on the [FLOP devnet](https://docs.flop.network). Independent. Not affiliated with FLOP Labs.

---

## The idea

An agent cannot open a Stripe account. It cannot hold a Visa. But it can hold a key and pay a known price for a known amount of work.

FLOP already settles compute this way — an agent opens a channel, a miner serves inference, escrow settles on-chain. That works because the payment primitive is generic, not because inference is special.

Scout extends the same primitive to **everything else an agent needs from the open internet**:

| Service | Price | What you get |
|---|---|---|
| Answer reasoning + citations | 0.001 FLOP | cited answer over the sources you chose |
| Page extract | 0.002 FLOP | full text of a URL, passages matched to the question |
| FLOP network snapshot | 0.005 FLOP | models, prices, capacity, chain health |
| Miner detail | 0.005 FLOP | GPU, stake, settlement rate for one miner |
| On-chain query | 0.002–0.003 FLOP | transfers, holders, spend, channels, reliability |
| Exa neural search | 0.010 FLOP | third-party index, metered through — bring your own key and you pay the provider instead |
| Brave / Tavily | 0.008 FLOP | alternative indexes |
| Firecrawl | 0.012 FLOP | scrape + search |
| Serper | 0.006 FLOP | Google results |
| Keenable, GitHub, HN, arXiv, CoinGecko, Jina | **free** | no upstream cost to Scout, so no charge |

That last row matters: we do not invent a price where there is no cost. Free stays free.
One caveat, stated rather than hidden: Jina's public endpoint started requiring a
token (401 without one, measured 2026-11), so it is free but no longer keyless.

**Two charges, never conflated.** Your wallet pays a FLOP miner for inference — that goes to them and Scout never touches it. Scout's own fee covers search, reading, routing and citations, itemised on every answer and shown separately in the UI.

### Devnet is nominal

`pricing.METERING = 'nominal'`. Every charge is computed, displayed and logged, but nothing is debited — there is no FLOP market yet to price against. Flipping to real settlement is one constant, not a migration.

### Bounded spending is the point

FLOP's `POST /connect/grants` takes `max_spend`, `rate_limit`, `max_uses` and `expiry_seconds` — a mandate, not a balance. That is what makes an autonomous agent safe to fund: the worst case is a known number, not "whatever it can reach".

Scout is designed for that flow but does not implement it yet. What it does have is the honest half:

```bash
POST /api/quote
{ "sources": ["keenable", "flop_live"], "compare_model": null }

→ { "total_flop": 0.006, "max_flop": 0.006,
    "billed": ["answer", "flop_live"], "free_lines": ["keenable"],
    "charged_flop": null, "metering": "nominal" }
```

No search, no inference, no ledger row — just the ceiling, itemised, before you spend
anything. Wiring the grant so the server *enforces* that ceiling is the next step, not
a rewrite.

---

## Why the miner ranking matters

Before you can sell a service built on FLOP, you need to know which miners actually deliver. FLOP's explorer publishes the raw data as one unsorted row per miner of `settledCount` / `timeoutCount`. It does not interpret it.

Scout does. Every miner gets a score:

```
score = 0.55 × on-chain settlement rate     (explorer /miner-reliability)
      + 0.35 × locally observed success × latency factor
      + 0.10 × currently priced in the gateway
```

Unknown miners get a neutral 0.5 rather than a penalty. Refreshed every 2 minutes, surfaced on `/miners` and used before each inference call.

Scout also records its own settlement history: every refresh writes each miner's
settled and timed-out counts to SQLite at most every 15 minutes, kept for 30 days.
That turns the score into a **direction** — `GET /api/reliability` and the Movers view
on `/miners` answer "who is degrading right now", which a snapshot cannot.

This is the piece with no equivalent elsewhere: a continuously updated, public read on which part of the network works — and in which direction.

---

## What it does

**Answer**
- 29 sources in 7 groups, run in parallel, merged and deduped by URL
- 8 keyless free sources (Keenable, Jina*, GitHub, HN, arXiv, Wikipedia, DefiLlama, CoinGecko)
- Every claim links to a numbered source; the model is instructed to cite or admit it cannot answer
- Price preview before you send: `POST /api/quote` returns the itemised total for the exact source set
- Itemised FLOP price per answer, and the miner's own cost shown separately
- A failure is never cached and never costs a free-tier slot
- sr25519 wallet in the browser — fund from the faucet, enroll a gateway key, pay for compute from your own address
- Provider keys for OpenRouter, Exa, Tavily, Brave, Firecrawl, Serper, Jina and GitHub, stored in the browser and sent per request; a source without its key is disabled with the reason showing, not offered as a checkbox that can only fail
- Compare mode: two models over the same sources

**Miners**
- All registered miners with GPU inventory, stake, calibration, settlement rate, router score
- 24h trend per miner and a Rising / Falling board from Scout's own snapshots
- Leaderboard of miners that actually served requests through Scout
- Network health: block height, TPS, capacity, countries, priced vs registered

**Interface**
- Light and dark, system-aware, persisted, no flash on first paint
- WCAG AA contrast in both themes (measured, not eyeballed)
- Sticky nav, deep-linkable questions (`/?q=…`), single miner views (`/miners?m=…`)

---

## API

```bash
POST /api/answer
{
  "q": "Which FLOP miners are most reliable right now?",
  "sources": ["explorer_reliability", "flop_stats"],
  "model": "flop:qwen/qwen2.5-0.5b-instruct",
  "keys": { "flop": "<your gateway key>" }
}
```

```json
{
  "pricing": {
    "items": [
      {"label": "Answer reasoning + citations", "price_flop": 0.001},
      {"label": "explorer reliability",          "price_flop": 0.003},
      {"label": "flop stats",                    "price_flop": 0.005}
    ],
    "total_flop": 0.009,
    "charged_flop": null,
    "metering": "nominal"
  },
  "answers": [{
    "answer": "… [1] …",
    "receipt": { "miner": "5CA3Fp…", "settlementStatus": "pending" },
    "usage": { "cost_flop": 2.748 },
    "scout_charge_flop": null,
    "metering": "nominal"
  }]
}
```

| Endpoint | Purpose |
|---|---|
| `POST /api/answer` | search → cited answer → receipt, with an itemised price |
| `POST /api/quote` | the price of a request before you send it — no search, no inference |
| `GET /api/pricing` | the service catalogue, metering mode, availability |
| `GET /api/catalog` | sources, presets, models |
| `GET /api/router` | live miner ranking with scores |
| `GET /api/miners` | every miner with GPU, stake, router score, 24h delta |
| `GET /api/miners/{address}` | one miner, same shape as a row |
| `GET /api/reliability` | settlement-rate deltas per miner over a window |
| `GET /api/stats` | per-model success rate, payer split, quoted vs charged |
| `GET /api/health` | liveness, metering mode, source count, key present |

`POST /api/answer` and `POST /api/quote` return `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Window` and `X-RateLimit-Tier`, so an agent can
pace itself instead of discovering the limit by being refused.

---

## Architecture

```
agent / browser
      │
      ▼
  FastAPI ──► quote()                 price this call before doing it
      │
      ├────► 29 sources in parallel ──► web · FLOP gateway · explorer · arXiv · CoinGecko
      │
      ├────► Smart Router             which miner is healthy right now
      │
      └────► FLOP /v1/chat/completions ──► miner ──► receipt + cost_flop
                                                  │
                                                  ▼
                                       Scout fee (FLOP) + miner cost, shown apart
```

**Stack:** Python · FastAPI · httpx · SQLite. Vanilla JS frontend, no framework. sr25519 wallet in the browser. Scores and prices live in one place each (`src_router.py`, `pricing.py`) so a policy change is an edit, not a search.

---

## Run locally

```bash
pip install -r requirements.txt

export FLOP_API_KEY=flop_...      # devnet gateway key
# export SCOUT_URL=https://...    # optional: enables the paid page-extract source

uvicorn server:app --port 8787
# http://localhost:8787
```

Without `SCOUT_URL` the page-extract source is dropped from the registry and its presets fall back to plain search — the pricing table still lists it, because a price for an unavailable service is a bug.

## Deploy

```bash
# VPS (Ubuntu 22.04/24.04, Debian 12)
git clone https://github.com/0xBobaAI/flop-scout.git /tmp/flop
sudo bash /tmp/flop/deploy/setup.sh
sudo nano /etc/flop-scout.env     # FLOP_API_KEY + WALLET_MNEMONIC
sudo systemctl restart flop-scout

# Docker
docker build -t flop-scout . && docker run -p 8080:8080 -e FLOP_API_KEY=flop_... flop-scout
```

`deploy/setup.sh` installs a systemd unit, an nginx TLS site and an hourly timer that renews the gateway key (it expires every 24h). Keep the key renewal in mind before pointing a domain at it.

## Changing prices

One table, `pricing.py`:

```python
SERVICES = {
    'exa':      Price(0.010, 'neural search'),
    'flop_live': Price(0.005, 'network snapshot'),
    ...
}
ANSWER_FEE = Price(0.001, 'answer')
```

Add a source to `registry.py` and a price to `pricing.py`; the API, the UI and the ledger pick both up.

---

## Fair use

FLOP credits agents for compute they genuinely buy. Scout caches duplicate questions for 10 minutes and charges nothing for a cache hit — it has no incentive to inflate usage, and no mechanism to. No loops, no bulk endpoints, no activity on your behalf.

Testnet is not live. Devnet activity earns nothing. Scout does not guarantee any allocation. [FLOP airdrop terms](https://flop.finance/airdrop/).

## Agent identity

Scout runs as an agent, not only as a website. It holds its own Ed25519 `did:key`
and publishes signed activity, so its identity is self-certifying: no registry,
no issuer, nothing to revoke.

| | |
|---|---|
| DID | `did:key:z6MktAvtg3gpDioFRUF2gWek1dGZkEfcKFgWAzb2PVr7YP7c` |
| Identity note | https://technocore.chat/kv/did-6c/f0b327308a6ae8 |
| Signed check-in | `lobby`, seq 159151651 |

The note path is the convention, not a server feature: the first 16 hex characters
of SHA-256 over the `did:key` **string**, split as `/kv/did-<first 2>/<remaining 14>`.
Anyone can re-derive it and verify the signature offline against the key in the note.

Scout buys inference on FLOP under this identity. It does not post heartbeats or
presence pings: FLOP credits compute purchased in settled sessions, and flags
manufactured activity for zero allocation. One check-in, then real work.

## License

MIT — see [LICENSE](LICENSE).
