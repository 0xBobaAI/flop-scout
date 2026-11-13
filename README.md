# Flop Scout

> **Search and page-reading that agents can pay for — billed in FLOP, per call, with a spending limit.**

Most agent tools assume a human with a credit card: sign up, attach a Visa, buy credits, hope nobody drains the account. FLOP is built for the opposite case — an agent with its own wallet paying per call for what it consumes.

Scout is a working service on the other side of that: the search, page reading and on-chain data an agent needs, priced in FLOP, no card and no subscription.

Two surfaces, one backend:

| Surface | What it is |
|---|---|
| **`/`** | Answer — 27 sources, cited answers, itemised FLOP price, on-chain receipt |
| **`/miners`** | Miners — every registered miner ranked by real settlement performance |

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
| Exa neural search | 0.010 FLOP | third-party index, we meter it through |
| Brave / Tavily | 0.008 FLOP | alternative indexes |
| Firecrawl | 0.012 FLOP | scrape + search |
| Serper | 0.006 FLOP | Google results |
| Keenable, Jina, GitHub, HN, arXiv, CoinGecko | **free** | no upstream cost, so no charge |

That last row matters: we do not invent a price where there is no cost. Free stays free.

**Two charges, never conflated.** Your wallet pays a FLOP miner for inference — that goes to them and Scout never touches it. Scout's own fee covers search, reading, routing and citations, itemised on every answer and shown separately in the UI.

### Devnet is nominal

`pricing.METERING = 'nominal'`. Every charge is computed, displayed and logged, but nothing is debited — there is no FLOP market yet to price against. Flipping to real settlement is one constant, not a migration.

### Bounded spending is the point

FLOP's `POST /connect/grants` takes `max_spend`, `rate_limit`, `max_uses` and `expiry_seconds` — a mandate, not a balance. That is what makes an autonomous agent safe to fund: the worst case is a known number, not "whatever it can reach".

Scout is designed for that flow. Today the per-call price is real and the debit is nominal; the grants integration is the next step, not a rewrite.

---

## Why the miner ranking matters

Before you can sell a service built on FLOP, you need to know which miners actually deliver. FLOP's explorer publishes the raw data as 245 unsorted rows of `settledCount` / `timeoutCount`. It does not interpret it.

Scout does. Every miner gets a score:

```
score = 0.55 × on-chain settlement rate     (explorer /miner-reliability)
      + 0.35 × locally observed success × latency factor
      + 0.10 × currently priced in the gateway
```

Unknown miners get a neutral 0.5 rather than a penalty. Refreshed every 2 minutes, surfaced on `/miners` and used before each inference call.

This is the piece with no equivalent elsewhere: a continuously updated, public read on which part of the network works.

---

## What it does

**Answer**
- 27 sources in 6 groups, run in parallel, merged and deduped by URL
- Every claim links to a numbered source; the model is instructed to cite or admit it cannot answer
- Itemised FLOP price per answer, and the miner's own cost shown separately
- sr25519 wallet in the browser — fund from the faucet, enroll a gateway key, pay for compute from your own address
- Compare mode: two models over the same sources

**Miners**
- All registered miners with GPU inventory, stake, calibration phase, settlement rate, router score
- Leaderboard of miners that actually served requests through Scout
- Network health: block height, TPS, capacity, countries, priced vs registered

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
| `GET /api/pricing` | the full service catalogue and current metering mode |
| `GET /api/catalog` | sources, presets, models |
| `GET /api/router` | live miner ranking with scores |
| `GET /api/miners` | every miner with GPU, stake, router score |
| `GET /api/stats` | per-model success rate, payer split, Scout charges accrued |
| `GET /api/health` | liveness and whether a gateway key is present |

---

## Architecture

```
agent / browser
      │
      ▼
  FastAPI ──► quote()                 price this call before doing it
      │
      ├────► 27 sources in parallel ──► web · FLOP gateway · explorer · arXiv · CoinGecko
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

## License

MIT
