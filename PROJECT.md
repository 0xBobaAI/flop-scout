# Flop Scout — project brief

Read this before changing anything. It exists so a different model can pick up the
work without re-deriving decisions that were already made and paid for.

---

## What this is

**Flop Scout** — a web service that sells search and page-reading to AI agents, priced
in FLOP and paid per call from the agent's own wallet. Second surface: a live ranking of
FLOP inference miners by real settlement performance.

**Live:** https://flopscout.com (VPS, Ubuntu 22.04)
**Audit:** `AUDIT.md` — defects found and fixed, an honest valuation, and the
feature roadmap. Read it after this file.
**Positioning:** an agent cannot open a Stripe account or hold a Visa. It can hold a key
and pay a known price for a known amount of work. FLOP's payment primitive is generic —
inference was just the first market. Scout extends it to everything else an agent needs
from the open internet.

**Not this:** a chat UI, a general-purpose assistant, or a competitor to `ask.flop.finance`
(FLOP's own documentation assistant). Answer quality is not the moat — pricing, routing
and the miner ranking are.

---

## The two charges — never mix them

This distinction is load-bearing. Every answer displays two separate numbers:

| | Who is paid | For what | Where it goes |
|---|---|---|---|
| **Miner cost** | the FLOP miner | inference tokens | straight to the miner, Scout never touches it. From `usage.cost_flop` |
| **Scout fee** | us | search, page reading, routing, citations, the receipt | itemised per source, from `pricing.py` |

If you ever find yourself adding these together or showing one as "the cost", stop.

---

## Pricing (`pricing.py`)

One table. `Price(price_flop, unit, free=False)` per service id, plus `ANSWER_FEE` and
`COMPARE_FEE`. `quote()` returns itemised line items for one request — `total_flop`
(computed), `max_flop` (the same ceiling, for budgeting), `billed` and `free_lines`
(which ids produced the total). Cache hits are free.

`POST /api/quote` exposes that to callers without running a search or an inference.
It is the honest half of bounded spending until `connect/grants` is wired: a price
you can read before you commit, but not one the server enforces.

`GET /api/pricing` marks each service `available: false` when the registry dropped
it (e.g. `web_read` without `SCOUT_URL`). A price for something you cannot buy is
a bug; hiding the row is a different bug.

Current shape:
- answer 0.001 · page extract 0.002 · FLOP network data 0.002–0.005
- metered third-party (Exa 0.010, Firecrawl 0.012, Brave/Tavily 0.008, Serper 0.006)
- **free when there is no upstream bill**: Keenable, GitHub, HN, arXiv, CoinGecko
- `jina` is priced free but (since 2026-11) needs the caller's own Jina token —
  s.jina.ai answers 401 without one. `web_read` stays priced even though it is
  unlisted when `SCOUT_URL` is unset; `/api/pricing` marks it `available: false`

**`METERING = 'nominal'`** — every charge is computed, displayed and logged; nothing is
debited. There is no FLOP market rate yet. Do not remove this without replacing it with
real settlement; do not quietly pretend charges are real.

Changing a price is an edit to one table. Adding a source is a function in
`src_web.py`/`src_explorer.py` plus an entry in `registry.py` plus (optionally) a price.

---

## Smart Router (`src_router.py`)

```python
score = 0.55 × on-chain settlement rate   # explorer /miner-reliability
      + 0.35 × local success × latency factor
      + 0.10 × currently priced in the gateway
```

- Miners with no history get a neutral **0.5**, not a penalty — a new operator must not be
  buried on day one.
- Cache refreshes every 2 minutes, with a 45 s failure backoff per source so an
  explorer outage is not retried on every request. `cached_snapshot()` never touches
  the network and is what page-load endpoints use.
- **Settlement history.** Every chain refresh calls `router.on_chain_refresh`, which
  `server.py` points at `persist_chain()`. It writes settled/timeout per miner into
  the `reliability` table at most every 15 minutes, keeps 30 days, and powers
  `GET /api/reliability` plus the 24h trend on `/miners`. Two snapshots are the
  minimum for a delta; before that the UI says "no history yet" rather than 0%.
- **Honest limitation:** the FLOP gateway chooses the miner, so Scout cannot pin one. The
  router scores, retries and reports. Never describe it as "routing to" a specific miner.

---

## Layout of the code

```
server.py          FastAPI app, all routes, SQLite log, miner aggregation, reliability history
layout.py          page shell: topbar, nav, footer, wallet dialog, <head>
pages/*.html       page bodies (answer, miners, pricing, api, about)
pricing.py         the price table and quote()
registry.py        source registry + presets; drops unavailable sources
src_web.py         web search sources (Keenable, Exa, Brave, Tavily, Firecrawl, Serper, Jina)
src_explorer.py    FLOP explorer sources (13 endpoints)
src_router.py      miner scoring and cache
llm.py             FLOP gateway + OpenRouter clients, retry budget, key resolution
renew_key.py       rotates the devnet gateway key (hourly systemd timer)
static/app.css     one stylesheet, tokens first, light + dark
static/agent.js    shared wallet state (localStorage), dialog, enrollment, provider keys
static/answer.js   Answer page: catalog, presets, request, rendering
static/miners.js   Miners page: board, leaderboard, router box
static/pricing.js  Pricing page: renders /api/pricing
static/og.png      social card, 1200x630 — regenerate if the positioning changes
static/wallet.js   sr25519 bundle (generated, do not hand-edit)
deploy/            setup.sh, systemd units, nginx site, env template
```

**Conventions that matter:**
- Every page body is wrapped by `layout.page()` in `<main id="main">`. Page fragments
  must not contain their own `<main>` — use `<section>` with an `aria-label`.
- Identity for rate limits and the duplicate cache is `client_id()`, which uses
  `request.client`. nginx/uvicorn already resolve `X-Forwarded-For`
  (`--proxy-headers --forwarded-allow-ips=127.0.0.1`). Never read the header
  directly: anyone can send it and gets a fresh daily budget.
- All SQLite access goes through `db_lock`. The connection is shared by async
  handlers and two threadpool routes.
- Design tokens live in `:root` and `[data-theme="dark"]`, plus a
  `prefers-color-scheme` block for visitors who never chose. Never add a raw hex
  below the token block. Contrast is checked against WCAG AA in both themes.
- Every page is a `pages/*.html` fragment rendered through `layout.page()`. Do not
  reintroduce standalone `index.html` files — the nav active state drifted that way.
- `agent.js` owns wallet state and emits `agent:change`, `agent:enrolled`,
  `agent:answered`. Page scripts listen; they must not re-implement wallet logic.
- `$`, `esc`, `short`, `toast`, `agent`, `keyValid`, `FLOP_API`, `LS`,
  `providerKeys`, `activeKeys` come from `agent.js`. Page scripts must not
  redeclare them.
- Upstream credentials (OpenRouter, Exa, Tavily, Brave, Firecrawl, Serper, Jina,
  GitHub) live in `localStorage` under `flopScoutKeys.v1` and are sent in
  `keys{}` on each request. The server never stores or logs them. A source whose
  key is missing is disabled in the UI with the reason inline — never offered as
  a checkbox that can only fail.
- Absolute paths in fetch (`/api/...`), not relative — pages are served at several depths.

---

## Hard-won constraints — do not regress these

**Inference retry (`llm.complete`)**
- Hard **200 s budget**, checked *before* each attempt. A request must never exceed it.
- `miner_capacity_reserved` (429) → 5 s backoff, capacity frees in seconds.
- `503` / `524` / `provider_error` → congestion, not a bug in our code.
- `400/401/403/404` and `model_not_allowed` → fail immediately, retrying cannot help.
- Per-request timeout = `min(90s, remaining budget)`.

**Gateway key lifecycle**
- The devnet key expires every 24 h. `llm.flop_key()` re-resolves it **per request**:
  `.wallet.json` wins while unexpired, `FLOP_API_KEY` is the bootstrap fallback.
- Rotating without a restart is the whole point. Do not cache it at import.
- `renew_key.py` derives the address from `WALLET_MNEMONIC` and creates `.wallet.json`
  on a fresh host (the file is git-ignored and never deployed).

**Upstream concurrency**
- The devnet explorer serialises. Nine concurrent queries from one worker made
  five time out at 15 s while the same endpoints answered in ~1 s alone. All
  explorer calls go through `src_explorer._get()`, which caps concurrency at 3.
- Gateway/faucet snapshots used by sources go through `src_web._cached()` (60 s,
  faucet 600 s). `flop_live` was 16.9 s of uncached round trips before this.
- Do not raise the semaphore to "make it faster" without re-measuring. The
  constraint is the indexer, not the socket.

**Page-load endpoints must not block on the network**
- `/api/catalog` and `/api/miners` answer from cache and refresh in the background.
- The model catalog is cached 60 s with an 8 s timeout; a slow gateway is "no live
  prices", never a 500. This was measured: 25 s and a 500 before, 1.3 s and 200 after.

**nginx**
- Ubuntu 22.04 ships nginx 1.18, which has **no `http2 on;` directive** (that is 1.25+).
  Use `listen 443 ssl http2;`.
- `proxy_read_timeout 240s` must stay above the app's own 200 s budget, or nginx becomes
  the thing that cuts the answer off.

**FastAPI lifecycle**
- Use the `lifespan` context manager, not `@app.on_event('startup')` — the latter is
  deprecated and warns on every boot.

**Python version**
- Dev box runs 3.14, the VPS runs 3.10. `requirements.txt` uses floors, not exact pins —
  an exact `starlette==1.10.0` pin requires Python 3.11 and silently breaks the deploy.

**Deploy files and CRLF**
- `deploy/setup.sh` and the nginx config must stay LF. Git for Windows converts them;
  `sed -i 's/\r$//'` on the server, or commit with `.gitattributes`.

---

## What is deliberately unfinished

State these plainly in the UI and README; do not paper over them.

1. **Nominal metering.** Prices real, debits not. The ledger keeps both numbers:
   `scout_quote_flop` (computed) and `scout_flop` (debited, `NULL` while nominal).
2. **`connect/grants` not wired.** FLOP's `max_spend` / `rate_limit` / `max_uses` /
   `expiry` mandate is the design target, not implemented. It is what makes an autonomous
   agent safe to fund — the worst case is a known number, not "whatever it can reach".
   `/api/quote` gives the ceiling but does not enforce it.
3. **Router is advisory.** Cannot pin a miner. `openapi_flop.json` has
   `/connect/preflight` and `POST /keys/ephemeral` (scope-locked, max 5 uses) — that
   pair is the likely way out.
4. **History is young.** The `reliability` table starts when the host first refreshes;
   "degraded over the past week" is untrue until a week has actually passed.
5. **Devnet.** Activity earns nothing. Testnet not live. No allocation promised.
6. **Single process.** `_hits` and `_recent` are in memory. Two uvicorn workers would
   mean two rate-limit maps and two caches.

---

## Copy and disclosure rules

- Never promise an airdrop or an amount.
- Always keep: "Independent tool. Not affiliated with FLOP Labs." and "Devnet activity
  earns nothing."
- The fair-use box stays: FLOP credits compute genuinely bought; scripted loops,
  self-dealing and multi-wallets are forfeited. Scout caches duplicates for 10 minutes
  and charges nothing for a cache hit — it has no mechanism to inflate usage.
- Answer text renders citation markers `[n]` as links to source cards. Keep that.

---

## Environment

| Variable | Required | Notes |
|---|---|---|
| `FLOP_API_KEY` | yes | devnet gateway key, `flop_…` |
| `WALLET_MNEMONIC` | for renewals | 12-word sr25519 phrase; env only, never committed |
| `FLOP_BASE_URL` | no | default `https://api.flop.network/v1` |
| `SCOUT_URL` | no | enables the deep page-read source; without it the source and its preset entries are dropped |
| `CORS_ORIGINS` | no | defaults to flopscout.com + localhost |
| `FREE_ANSWERS_PER_DAY` | no | 20 |
| `USER_ANSWERS_PER_MIN` | no | 6 |
| `DUPLICATE_WINDOW_S` | no | 600 |
| `DATA_DIR` | no | where `log.db` lives |

---

## Deploy

```bash
# server (Ubuntu 22.04, 1 vCPU, 1 GB)
sudo bash /opt/flop-scout/deploy/setup.sh
sudo nano /etc/flop-scout.env          # FLOP_API_KEY + WALLET_MNEMONIC
sudo systemctl restart flop-scout

# certificate
snap install --classic certbot
certbot certonly --webroot -w /var/www/certbot -d flopscout.com -d www.flopscout.com

# every deploy
systemctl restart flop-scout
```

`deploy/setup.sh` is idempotent. It excludes `.wallet.json` from `rsync --delete` on
purpose — that file holds the live key the hourly timer wrote on this host.

---

## Before you ship a change

1. `python -c "import ast; …"` with `feature_version=(3, 10)` on every touched `.py`,
   `node --check` on every touched `.js`.
2. Load all five pages plus a 404: `/`, `/miners`, `/pricing`, `/api`, `/about`, `/nope`
   — correct status, nav active state, favicon. Check light and dark at 1440 and 390 px.
3. Hit `POST /api/answer` with one source and confirm `pricing.items` is itemised,
   `metering` is present, and `scout_quote_flop` is non-zero while `scout_flop` is
   `NULL`. Hit `POST /api/quote` for the same set and compare totals.
4. Confirm a repeat of the same request returns `cached: true` with
   `total_flop: 0`, and that changing only `sources` does **not** return the cache.
4. Confirm no secret reached git: `git grep -E "flop_[A-Za-z0-9_-]{25,}|5[A-Za-z0-9]{47}"`.
5. If you touched nginx, `nginx -t` before reload.
