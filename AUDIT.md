# Flop Scout — audit and product assessment

Written after a full read of the code, a smoke test against the live devnet, and
the design pass in this iteration. Two things live here:

1. **What was broken** — defects found in the code, with the fix.
2. **How valuable this is** — an honest valuation of the service inside the FLOP
   ecosystem, and the features that would raise it.

Read this with `PROJECT.md`. That file is the brief; this one is the review.

---

## Part 1 — Code defects found and fixed

Everything below was real, reproducible, and is now fixed unless marked
**open**. Ordered by how much damage it was doing.

### Critical

**1. The free tier was unlimited.** `client_id()` read `X-Forwarded-For` itself:

```python
ip = request.headers.get('x-forwarded-for', request.client.host).split(',')[0]
```

Any caller could rotate that header per request and get a fresh 20-answer daily
budget every time — and a fresh duplicate-cache namespace with it, which also
defeated the cache. Verified: the header is fully attacker-controlled behind
nginx as well, because nginx *appends* to it.

Fixed: identity now comes from `request.client`, which uvicorn sets from
`X-Forwarded-For` only because it runs with `--proxy-headers
--forwarded-allow-ips=127.0.0.1`. The header is read only as a fallback for
loopback peers. Rate limiting, billing buckets and the duplicate cache all key
off that value.

**2. Every answer was logged as free.** `log()` inserted `res['scout_charge_flop']`,
which is `None` while `METERING = 'nominal'`. So the ledger recorded zero for
every request, `/api/stats` reported `total_flop: 0` forever, and the stated
promise — "every charge is computed, displayed **and logged**" — was false.
The UI showed a fee the database said did not exist.

Fixed: a `scout_quote_flop` column stores the computed price, `scout_flop` keeps
meaning "actually debited". `/api/stats` now reports `quoted_flop` and
`charged_flop` separately, which is the only honest way to present nominal
metering.

**3. `POST /api/answer` returned 500 on a model id without a provider prefix.**
`llm.complete()` does `model_id.split(':', 1)` — unpacking a single element
raises `ValueError` inside the handler. Any client sending `"model": "gpt-4o"`
got an unhandled 500 and a stack trace in the journal.

Fixed: `AnswerReq` validates the provider prefix and returns 422 with a message
that says what the shape should be.

### Correctness

**4. The duplicate cache ignored which sources were used.** The key was
`(client, question, model, compare_model)`. Ask the same question with
`explorer_reliability` and then with `keenable` and the second request silently
replayed the first answer, citing sources it never searched.

Fixed: the key covers sources, `n` and `max_tokens`.

**4b. A congestion failure was cached for ten minutes.** `_recent[dup]` was
written unconditionally, so after "all miners at capacity" the next request with
the same question returned `{"cached": true}` carrying the same error — while the
API page tells callers to "retry shortly". Verified against a stubbed gateway.

Fixed: only a response containing a real answer is cached.

**4c. Congestion burned the caller's free tier.** `rate_limit()` consumed a slot
before any work happened, and devnet congestion (measured: 3 successes in 17
answer attempts on this box) burned a user's 20/day budget without producing a
single answer.

Fixed: when no answer is produced the slot is refunded and the response's
`X-RateLimit-*` headers report the restored count. `POST /api/quote` reports quota
with `peek_quota()` and never consumes it.

**5. A cache hit was billed.** `_recent` stored the full response including
`pricing`, and the hit path returned it unchanged — so the UI showed the full
fee for a request that did no work, directly contradicting the fair-use text
("cached answers are free"). `pricing.quote(cache_hit=True)` existed and was
never called.

Fixed: the hit path re-quotes with `cache_hit=True`, zeroes the answer's quoted
fee, and marks the answer card "cached · free".

**6. The router double-counted every successful call.** `llm.complete()`
called `_r.record(miner, True, latency)` on success, then `server.answer()`
called `router.record(...)` again for every FLOP response. Local call counters
and average latency were both distorted — latency was measured from different
start points in the two places.

Fixed: the router is fed once, in `llm.complete()`, where the real per-call
timing lives.

**7. `/api/miners` blocked on the explorer with no stampede control.** On a cold
cache, four paginated explorer requests ran *inside* the request — measured
8-15 s on this box, and the page calls it on every load. Concurrent loads each
started their own four-request fan-out at a 1 vCPU VPS.

Fixed: stale-while-revalidate plus an `asyncio.Lock`. First load waits, later
loads serve the snapshot and refresh in the background. Measured: 14.6 s cold →
0.03-0.08 s warm.

**8. `/api/router` awaited a full refresh on every call.** Same problem, plus it
turned the miners page into two serialised network waits.

Fixed: cached snapshot, background refresh, blocking only when nothing is
cached at all.

**9. A failed router refresh was retried on every single request.** `refresh()`
only advanced `_chain_ts` on success, so an explorer outage meant every request
paid for a 15 s timeout — a self-inflicted request storm during exactly the
outage you least want one.

Fixed: a 45 s failure backoff, tracked per source so a dead gateway does not
block chain refreshes.

**10. The model filter in `ranked_miners()` was inverted.** The condition was
`if model_id and ms.models and model_id not in ms.models: continue`. A miner
with an *empty* price list — i.e. not serving anything right now — passed the
filter and could rank first on chain history alone.

Fixed: when a model is requested, only currently-priced miners that offer it
qualify.

**11. `Best_price` defaulted to `0.003` for miners with no price.** That is a
fabricated quote for a miner that is not selling. Now `None`, and the UI shows
"—" instead of a number.

**12. SQLite had one connection, no lock, two threadpool routes.** `/api/stats`
and `/api/miners` are sync handlers → threadpool, `db` is shared and
`check_same_thread=False`. Under concurrent load that raises
`ProgrammingError: SQLite objects created in a thread can only be used in that
same thread` — or corrupts an insert under `isolation_level=''` autocommit.

Fixed: one `threading.Lock` around every statement, including the schema
migration.

**13. `@app.on_event('startup')`** is deprecated since FastAPI 0.109 and emits a
`DeprecationWarning` on every boot; it will be removed. Moved to a `lifespan`
context manager, which also gave a place to close the shared `httpx` client and
warm the model catalog.

### Small but sharp

**14.** `_hits` (per-IP rate buckets) was an unbounded `defaultdict` — a slow
memory leak driven by traffic. Now pruned above 20 000 entries.

**15.** `/api/pricing` listed `web_read` at 0.002 FLOP even when `SCOUT_URL` is
unset and the registry has dropped the source. A price for something you cannot
buy. Now every service carries `available`, the row is marked "not configured",
and the header counts them.

**16.** Every unknown URL — including `/favicon.ico`, which browsers request
unconditionally — returned FastAPI's JSON `{"detail": "Not Found"}`. Fixed:
`/favicon.ico` 308s to the SVG, unknown API paths return structured JSON,
unknown pages return a real 404 page.

**17.** No `robots.txt`, no `sitemap.xml`, no `og:image`. A public tool with five
indexable pages and no sitemap, and every share link rendered as a bare URL.

**18.** Hardcoded "288 registered miners" and "245 unsorted rows" in copy while
the live numbers were 331-333 and 278, and the explorer's `total` grows. Copy
now says "every registered miner"; the real count is rendered from the API.

**18b. Two data sources were silently dead.** Measured directly against the
upstreams (2026-11):

- **Jina** — `s.jina.ai` now answers **401** without a token, so the source
  registered as "free, no key required" could only ever return an empty list.
  Now key-gated like the other Builder sources, with the token sent as a bearer
  header, and the query path-escaped (an unescaped `#` truncated the question —
  "C# vs Java" searched for "C").
- **`flop_live`** — three uncached round trips per call (gateway `/models`,
  explorer `/health`, faucet `/limits`), measured **16.9 s**. The `flop` preset
  therefore could not finish inside the 25 s per-source budget alongside its
  four siblings. Now a 60 s TTL cache on all three, with a slow faucet no longer
  able to fail the source.

**18c. The explorer sources timed out under load, not under latency.**
Individually every endpoint answers in 0.7-3 s. Nine of them firing at once from
one worker made **five of nine time out at 15 s** — the indexer serialises.
Measured before: `explorer` preset 5/5 sources but 15.8 s wall on several;
`flop_full` 7.1 s. After a 3-permit semaphore: `explorer` 1.2 s, `flop_full`
1.1 s. `flop_miners_live` also capped at four `/miners/{addr}` lookups (was six)
with a 5-minute per-address cache.

**19.** `sortKeys` in `miners.js` treated `parseFloat(undefined) || 9` as a price
tie-breaker while `best_price` is a string — `"0.003" || 9` is truthy, so the
fallback silently never fired for real prices but did for missing ones, sorting
unpriced miners ahead of cheap ones. Tightened to explicit null handling.

**19b. Five key-gated sources and five OpenRouter models were selectable with no
way to enter a key.** The wallet dialog stored only the sr25519 wallet; the
`keys` object sent to `/api/answer` only ever contained `flop`. Exa, Tavily,
Brave, Firecrawl, Serper and every OpenRouter model could be picked and would
fail 100% of the time. Verified by reading the request builder.

Fixed: a **Provider keys** panel inside the wallet dialog (works with or without
a wallet), stored in this browser only and sent per request. Sources whose key is
missing are disabled with the reason inline, the picker defaults to a model the
caller can actually run, and choosing an OpenRouter model without its key shows a
one-click route to the panel. Also: the answer page fetched `/api/catalog` twice
on load, and `network()` re-fetched what `init()` already had.

**20.** Contrast failures in the old palette: muted text on paper was 3.95:1,
muted on white 4.24:1, footer fine print 2.69:1, amber `c-warn` 3.15:1 — all
below WCAG AA for body text. The new token set measures 4.66-16.96:1 in light
and 6.48-16.35:1 in dark, verified numerically. The primary button's gradient
originally had a mid-stop at 3.86:1 for white text — also fixed, all three stops
now 4.97:1 or better.

### Open, and deliberately so

- **`connect/grants` is still not wired.** The mandate flow (max spend, rate
  limit, max uses, expiry) is the thing that makes an autonomous agent safe to
  fund. Until it exists, `/api/quote` is the honest half-measure: a price you can
  read before committing, but not one the server enforces.
- **The router cannot pin a miner.** The gateway chooses. Scoring, retrying and
  reporting is all Scout can do. Do not let any copy drift into "routes to".
- **`_recent` is process-local.** Two uvicorn workers means two caches and two
  rate-limit maps. Single worker today, fine; document it before scaling out.
- **A failed answer is still quoted a Scout fee.** The search did run and the
  sources were returned, so the fee is arguably earned — but on a congested
  devnet most requests produce no answer. Deciding whether search-only responses
  should be billed is a product call, not a bug. Today the quote is attached; the
  free-tier slot is refunded.
- **`src_data.py` was dead code** — nothing imported it. Deleted; the two
  keyless sources it prototyped (`wikipedia`, `defillama`) are now registered,
  priced at zero, and wired into the `research` and `crypto` presets.
- **Three gateway caches disagreed on screen** (fixed). `llm.py`, `server.py`
  and `src_router.py` each fetched `/v1/models` with their own TTL and timeout,
  measured as 0.8s / 3.7s / >12s on successive calls. One page could show
  "31 active miners" in the sidebar and "0 priced" in the table. Now a single
  `flop_gateway.py` owns the snapshot, and "unknown" is `None`, never `0`.
- **`crypto` could not answer multi-token questions** (fixed). CoinGecko's
  `/search` is a name lookup, not a relevance search: "bitcoin ethereum" returned
  zero results. Terms are now queried one at a time and interleaved.
- **Empty exception text in logs** (fixed). `ReadTimeout` stringifies to `''`,
  so `router refresh error:` printed nothing after the colon. Now `%r`, and the
  same bug in `_models_snapshot` is gone with it.
- **`renew_key.py` exits non-zero on the expected daily enrollment cap.** It
  works, but the systemd unit logs a failure every hour for a normal condition.

---

## Part 2 — How valuable is this inside the FLOP ecosystem?

Blunt version: **the miner ranking is the asset, not the search.** Search is a
commodity that a dozen APIs sell cheaper. What does not exist anywhere else is a
continuously updated, public judgement about which part of the FLOP network
actually works.

### What genuinely has value

**The settlement ranking.** The explorer publishes raw `settledCount` /
`timeoutCount` per miner and interprets nothing. Scout turns 278-333 rows into
one comparable score, adds its own observed latency, and now records history so
it can say *"this miner dropped from 99% to 61% in 24 hours"*. Anyone
considering a FLOP integration needs exactly this and cannot compute it from a
snapshot. It is also the only part of Scout with a moat: it needs time and
traffic to accumulate, so a competitor cannot clone it by copying code.

**The pricing surface.** A published, itemised, per-call price list in FLOP with
`/api/quote` is a small but real contribution: it demonstrates what the FLOP
payment primitive looks like for a non-inference service. That is the positioning
claim in `PROJECT.md`, and it is credible now in a way "we sell search" is not.

**The honesty as a differentiator.** Two separate charges, an explicit "nothing
is debited", and a "what is not finished" section. In a space full of
airdrop-farming dashboards this reads as engineering. It is underrated as
positioning.

### What is commodity

- **The answer engine.** 27 sources, cited answers — this is a wrapper around
  Keenable/Jina/Exa with a prompt. Quality is capped by a 0.5B devnet model, and
  the brief already concedes: "answer quality is not the moat".
- **The 13 explorer sources.** Useful plumbing, no differentiation.
- **Compare mode.** Nice demo, no buyer.

### Who would actually pay, and why

Ranked by willingness to pay:

1. **Miner operators.** They want to know their own rank and why it moved. This
   is the most commercially obvious product in the repo — a paid, continuous
   miner-reputation feed, with alerts on degradation. Nobody else can sell it.
2. **Agent developers on FLOP.** They need one endpoint that answers "is the
   network healthy enough to ship today". Less about search, more about
   reliability telemetry.
3. **Researchers and traders watching FLOP.** The price index, HH I, capacity and
   trend data, packaged as an API.
4. **General web-search-for-agents buyers.** The weakest segment: they compare
   against Exa, Tavily, Brave, and Scout loses on latency, model quality and
   brand.

### The honest revenue estimate

Nominal prices are 0.001-0.012 FLOP per call. With no FLOP market rate, the
revenue number does not exist yet; what exists is a *shape*. If a FLOP is ever
worth ~$0.10 and a typical answer costs 0.009 FLOP of Scout fee ($0.0009),
scout needs roughly 100 000 calls to earn $90. **The search product will never
be a business at these prices.** The ranking/telemetry product, sold as a
subscription or a data feed at $50-500/month to a handful of miner operators and
integrators, is the only plausible revenue in this repo. That is the strategic
finding of this audit.

### Verdict

| Dimension | Grade | Note |
|---|---|---|
| Differentiation | B+ | The ranking is unique; the search is not |
| Technical execution | B | Fixed this iteration; was a C+ on security and ledger honesty |
| Design / credibility | A- | Tokenised, light/dark, AA-contrast, real OG card — consistent with funded dev-tool products. The remaining gap is product imagery and a brand beyond a generated identicon |
| Disclosure honesty | A | Quote-before-spend, per-line pricing, an availability flag on things you cannot buy, and a "what is not finished" list that is actually true |
| Path to revenue | C | Real, but a data business wearing a SaaS costume |
| Ecosystem value to FLOP | B | Demonstrates the payment primitive beyond inference — genuinely useful to FLOP Labs as a reference |

---

## Part 3 — What to add next, in order of value per week of work

### Shipped in this iteration

- **`POST /api/quote`** — price a request without spending anything. Turns
  "bounded spending" from an aspiration into a callable fact, and is the single
  most agent-relevant addition here.
- **`GET /api/reliability` + settlement history** — the router's own snapshots,
  so 24h deltas exist. This is the first half of the moat.
- **`GET /api/miners/{address}`** — one miner instead of a 200 KB board.
- **`X-RateLimit-*` headers and `Retry-After`** — an agent can pace itself
  instead of discovering limits by being refused.
- **Movers view + 24h trend chips** on `/miners`, with a "no history yet" state
  instead of a fake number.
- **Light/dark design system**, OG image, real 404, robots, sitemap, favicon
  redirect.
- **Provider-key panel** — the key-gated half of the catalogue became reachable,
  which turns five sources and five models from decoration into inventory.
- **Explorer concurrency control** — the `explorer` and `flop_full` presets went
  from ~7-20 s to ~1.2 s, and stopped silently dropping sources to timeouts.

### Next, highest value first

**1. Wire `connect/grants`.** Everything else is cosmetic next to this. A
mandate-issued, server-enforced ceiling is the product promise; today it is a
paragraph. Concretely: `POST /api/grants` proxies the gateway's grant creation,
`/api/answer` sends the grant id, and the response reports remaining uses and
spend. Then flip `METERING` for grant-backed calls only.

**2. A miner reputation feed as a product.** `GET /api/miners/{addr}/history`
with a week of snapshots, plus email/webhook alerts on a settle-rate drop.
Charge for it in FLOP. This is the first thing in the repo that can be sold to
someone who is not farming an airdrop.

**3. Self-critique loop on answers.** One extra cheap FLOP call that checks
whether each `[n]` citation actually supports the sentence it is attached to,
and drops or flags unsupported claims. The brief says answer quality is not the
moat — but *verifiable* citation quality is a different thing from answer
fluency, and no one in this market does it.

**4. `POST /api/extract`** as a standalone, priced endpoint. Page reading is
already implemented (`web_read`); exposing it directly is nearly free and it is
the highest-value single call in `pricing.py` at 0.002 FLOP.

**5. Point the router at something.** Look at
`/models/estimate`, `/connect/preflight` and `POST /keys/ephemeral` in
`openapi_flop.json`: preflight prices a model *without spending*, and an
ephemeral scope-locked token can carry a per-request ceiling. Combining those
gets Scout from "advisory" to "actually constrains the gateway", which removes
the largest caveat in the whole product.

**6. Persist and expose cost per answer as public telemetry.** "Median FLOP cost
of a cited answer over the last 24h, by source set" is the kind of number that
gets quoted in other people's decks. Cheap to compute from the existing ledger.

### Worth doing, lower priority

- Answer caching keyed on URL content hash, so a changed page invalidates.
- Streaming answers (SSE) — the 200 s budget is invisible today; a token stream
  would make congestion feel like progress.
- `n` and `max_tokens` in the UI. The API supports them, the form does not.
- A `/status` page from `/api/health` + router freshness — trust through
  transparency, and it is one template.
- Structured errors everywhere (`{"error": "...", "hint": "..."}`), matching the
  style already used on the API page.

---

## Before the next change

1. `python -c "import ast; ast.parse(...)"` with `feature_version=(3,10)` on every
   touched `.py`; `node --check` on every touched `.js`.
2. Load all five pages plus a 404 in light and dark, at 1440 and 390 px.
3. `POST /api/answer` and `POST /api/quote`; confirm `scout_quote_flop` is
   non-zero in `log.db` while `scout_flop` stays `NULL`.
4. `git grep -E "flop_[A-Za-z0-9_-]{25,}|5[A-Za-z0-9]{47}"` — no secret in git.
5. Two snapshot cycles (15 min apart) before trusting the movers view.
