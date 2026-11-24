"""Docs content — one page per topic, grouped in a sidebar like a normal docs site.

Each entry is `slug: (group, title, summary, body_html)`. `body_html` only ever
contains `<h2>`/`<h3>` and ordinary content; the on-page TOC is built from the
`<h2>` tags automatically, so a new section needs no second place to register it.

Order here is sidebar order. Keep the file self-contained — server.py only reads
`DOCS` and `DOCS_GROUPS`.
"""

DOCS = {
    'overview': ('Start here', 'Overview', 'Miner analytics, search, page reading and OpenAI/MCP gateway on FLOP.', '''
<h2>What Scout does</h2>
<p>Flop Scout tracks every FLOP miner from individual on-chain compute sessions (raw and independent demand, 24h movers, regions), serves web search and page reading to AI agents over REST and MCP, and exposes an OpenAI-compatible gateway at <code>https://flopscout.com/v1</code>.</p>
<p>Every cited answer reports two separate numbers, never mixed:</p>
<div class="rc-split">
  <div class="rc-part miner"><b>Miner cost</b><strong>2.748 FLOP</strong><span>Paid to the miner that ran the inference.</span></div>
  <div class="rc-part scout"><b>Scout fee</b><strong>0.009 FLOP</strong><span>Search, reading, citations. Itemised.</span></div>
</div>
<h2>Three ways to connect</h2>
<ul>
  <li><strong>Miner board &amp; JSON API:</strong> <a href="/">flopscout.com</a> and <code>GET /api/miners</code>, <code>/api/sessions/*</code>.</li>
  <li><strong>OpenAI SDK / Cherry Studio / Cursor:</strong> <code>base_url = https://flopscout.com/v1</code> (API Host <code>https://flopscout.com</code> in Cherry Studio) with your FLOP key, a Scout member key (<code>sk_…</code>), or OpenRouter key.</li>
  <li><strong>MCP server (10 tools):</strong> Streamable HTTP at <code>https://flopscout.com/mcp</code>.</li>
</ul>
<h2>Scope</h2>
<p>Independent tool, not affiliated with FLOP Labs and not a competitor to FLOP's documentation assistant.</p>
'''),

    'quick-start': ('Start here', 'Quick start', 'Connect via OpenAI SDK, MCP, or REST.', '''
<h2>1. OpenAI-compatible chat (/v1)</h2>
<p>Point any OpenAI client, IDE or Cherry Studio at Scout. Pass a Scout member key (<code>sk_…</code>), your own FLOP key (<code>flop_…</code>, available in the topbar wallet panel), or an OpenRouter key.</p>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="">Copy</button>
  <pre class="code" data-lang="python">from openai import OpenAI

client = OpenAI(
    base_url="https://flopscout.com/v1",
    api_key="sk_...",   # or your flop_... / OpenRouter key
)
res = client.chat.completions.create(
    model="flop:qwen/qwen3-14b",
    messages=[{"role": "user", "content": "Explain proof of inference in 3 lines."}],
    extra_headers={"X-Scout-Miner": "auto"},  # optional: pin top-scored miner
)</pre>
</div>
<p>In <strong>Cherry Studio</strong>: Settings → Model Provider → Add (OpenAI) → API Host <code>https://flopscout.com</code> (without <code>/v1</code>) → API Key <code>sk_…</code> or <code>flop_…</code> → Sync models.</p>
<h2>2. MCP server (10 tools)</h2>
<p>Add <code>https://flopscout.com/mcp</code> (Streamable HTTP) to Claude, Cursor or Cherry Studio:</p>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="">Copy</button>
  <pre class="code" data-lang="json">{
  "mcpServers": {
    "flop-scout": {
      "type": "streamableHttp",
      "url": "https://flopscout.com/mcp"
    }
  }
}</pre>
</div>
<h2>3. Quote &amp; cited answer</h2>
<p>Price a search + answer job before running it. Adding <code>q</code> and <code>model</code> includes the FLOP gateway's zero-spend estimate of the miner's cost (floor and typical).</p>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="POST /api/quote">Copy</button>
  <pre class="code" data-lang="request">POST /api/quote
{ "sources": ["keenable"], "q": "How many miners are on FLOP?",
  "model": "flop:qwen/qwen2.5-0.5b-instruct", "max_tokens": 250 }

→ { "total_flop": 0.001,
    "inference": { "floor": { "cost_flop": 1.824 }, "typical": { "cost_flop": 4.242 } } }</pre>
</div>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="POST /api/answer">Copy</button>
  <pre class="code" data-lang="request">POST /api/answer
{ "q": "Which FLOP miners are most reliable?", "sources": ["auto"] }

→ { "answers": [ … ], "sources": [ … ], "pricing": { … } }</pre>
</div>
<h2>4. Read one page</h2>
<p><code>POST /api/read</code> fetches one public URL as clean Markdown with no search and no model. Repeat reads within 10 minutes are free from cache.</p>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="POST /api/read">Copy</button>
  <pre class="code" data-lang="request">POST /api/read
{ "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
  "max_chars": 2000 }

→ { "title": "A Light in the Attic | Books to Scrape",
    "content": "… £51.77  In stock (22 available) …", "truncated": false,
    "final_url": "…", "http_status": 200, "pricing": { "total_flop": 0.001 } }</pre>
</div>
<h2>Next</h2>
<p>See the full <a href="/docs/endpoints">endpoint list</a>, <a href="/docs/limits">limits</a> and <a href="/docs/pricing">pricing</a>.</p>
'''),

    'endpoints': ('API reference', 'Endpoints', 'Every route Scout exposes.', '''
<h2>Chat, search &amp; reading</h2>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Endpoint</th><th>Returns</th></tr></thead>
<tbody>
<tr><td class="mono">POST /v1/chat/completions</td><td>OpenAI-compatible chat (<code>base_url: https://flopscout.com/v1</code>), streaming or not, with tools and full message history. Bearer: your FLOP or OpenRouter key, a Scout member key (<code>sk_…</code>), or none. Optional <code>X-Scout-Miner: auto</code> or SS58 address when the upstream key may pin</td></tr>
<tr><td class="mono">GET /v1/models</td><td>OpenAI-shaped model list for <code>/v1</code></td></tr>
<tr><td class="mono">POST /api/answer</td><td>search → cited answer → receipt, with an itemised FLOP price. Accepts <code>X-Scout-Key: sk_…</code> or <code>keys.flop</code> / <code>keys.openrouter</code></td></tr>
<tr><td class="mono">POST /api/quote</td><td>price a request before sending it (floor + typical miner estimate when <code>q</code> and <code>model</code> are given). Spends nothing</td></tr>
<tr><td class="mono">POST /api/read</td><td>one public page as readable Markdown: <code>url</code>, <code>max_chars</code> (500–20000, default 8000). Failed reads cost nothing</td></tr>
<tr><td class="mono">POST /mcp</td><td>stateless Streamable HTTP MCP server (10 tools)</td></tr>
<tr><td class="mono">GET /api/pricing</td><td>the full service catalogue, metering mode, and availability</td></tr>
<tr><td class="mono">GET /api/catalog</td><td>30 sources, presets, models and best router miner</td></tr>
</tbody></table></div>
<h2>Miners &amp; wallet</h2>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Endpoint</th><th>Returns</th></tr></thead>
<tbody>
<tr><td class="mono">GET /api/miners</td><td>every registered miner with GPU, stake, region, router score and 24h session settlement</td></tr>
<tr><td class="mono">GET /api/miners/{address}</td><td>one miner, same shape as a row on <code>/api/miners</code></td></tr>
<tr><td class="mono">GET /api/router</td><td>top 25 miners scored on 7-day independent-demand settlement, Scout probes and gateway status</td></tr>
<tr><td class="mono">GET /api/sessions/miners</td><td>per-miner settlement rebuilt from individual explorer sessions: raw and <em>independent</em> rate (excluding agent wallets above 25% of window sessions). <code>window</code> up to 90 days</td></tr>
<tr><td class="mono">GET /api/sessions/miners/{address}</td><td>one miner's sessions over a window, bucketed: settled, timeouts, open, rate</td></tr>
<tr><td class="mono">GET /api/sessions/network</td><td>network settlement over a window, raw and independent, plus dominant agent wallets</td></tr>
<tr><td class="mono">GET /api/sessions/coverage</td><td>how much of the explorer's session record Scout holds, and when it last synced</td></tr>
<tr><td class="mono">GET /api/probes</td><td>Scout's own 4-tier streamed probe purchases: policy, caps and results (activates when testnet inference opens)</td></tr>
<tr><td class="mono">GET /api/wallet/{address}/balance</td><td>live on-chain FLOP balance (<code>free_flop</code>, <code>reserved_flop</code>, <code>frozen_flop</code>) queried from the Substrate RPC</td></tr>
<tr><td class="mono">POST /api/alerts</td><td>subscribe to a settle-rate drop. Fields: <code>address</code> (SS58 or <code>*</code>), <code>threshold</code> (0–1), <code>channel</code> (<code>site</code> | <code>telegram</code>), <code>handle</code></td></tr>
<tr><td class="mono">GET /api/alerts/check</td><td>active alerts with <code>last_rate</code> and <code>fired_at</code>. Optional <code>address</code> filter. Reading clears nothing</td></tr>
<tr><td class="mono">DELETE /api/alerts/{id}</td><td>deactivate an alert</td></tr>
<tr><td class="mono">GET /api/inference-prices</td><td>every live FLOP model priced as a whole request by the gateway's zero-spend estimator</td></tr>
</tbody></table></div>
<h2>Operations</h2>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Endpoint</th><th>Returns</th></tr></thead>
<tbody>
<tr><td class="mono">GET /api/stats</td><td>success rate per model, payer split, quoted vs charged totals</td></tr>
<tr><td class="mono">GET /api/analytics</td><td>daily demand aggregates. No question text, no client IDs</td></tr>
<tr><td class="mono">GET /api/health</td><td>liveness, version, metering mode, source count and gateway key status</td></tr>
</tbody></table></div>
'''),

    'errors': ('API reference', 'Errors', 'What comes back when it goes wrong.', '''
<h2>Status codes</h2>
<p>On <code>/v1/*</code>, errors always follow the OpenAI <code>{"error": {"message", "type", "code"}}</code> shape. On <code>/api/answer</code>, sources and prices still return even if model inference fails.</p>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Status</th><th>Meaning</th><th>What to do</th></tr></thead>
<tbody>
<tr><td class="mono">422 invalid_body</td><td>A field failed validation</td><td>q 2–500 chars, n 1–8, max_tokens 32–800, valid SS58 address</td></tr>
<tr><td class="mono">429 rate_limited</td><td>Per-IP daily cap (500/day) or per-minute pace reached</td><td>Use a Scout member key (<code>sk_…</code>), pass your own FLOP/OpenRouter key, or wait for the window to roll</td></tr>
<tr><td class="mono">503 wallet_budget_reached</td><td>Scout's wallet hit its 10,000 FLOP daily safety cap</td><td>Pass your own FLOP/OpenRouter key, or wait for the 24h rolling window</td></tr>
<tr><td class="mono">403 pin_not_allowed</td><td><code>X-Scout-Miner</code> used with a non-owner FLOP key</td><td>Omit <code>X-Scout-Miner</code> so the FLOP gateway assigns a miner</td></tr>
<tr><td class="mono">429 miner_capacity_reserved</td><td>Every miner is at capacity</td><td>Retry shortly</td></tr>
<tr><td class="mono">503 / 524</td><td>Origin timeout at the FLOP gateway</td><td>Congestion upstream. Search sources still return on <code>/api/answer</code></td></tr>
<tr><td class="mono">401 invalid_api_key / model_not_allowed</td><td>Expired key or model outside key scope</td><td>Re-enroll in the wallet panel or switch payment mode to Scout free tier</td></tr>
<tr><td class="mono">404 not_found / model_not_found</td><td>Unknown API path, model or miner address</td><td>Check <code>GET /v1/models</code> or <code>GET /api/catalog</code></td></tr>
<tr><td class="mono">422 bad_url / unresolvable</td><td><code>/api/read</code>: non-public URL or unresolvable host</td><td>Use a public <code>http(s)://</code> hostname</td></tr>
<tr><td class="mono">502 blocked_by_target / target_error</td><td><code>/api/read</code>: bot check or HTTP 4xx/5xx from target site</td><td>Try another page</td></tr>
<tr><td class="mono">502 reader_refused / 503 reader_busy</td><td><code>/api/read</code>: anonymous reader blocked or at 18 RPM limit</td><td>Retry after <code>Retry-After</code>, or pass <code>keys.jina</code></td></tr>
</tbody></table></div>
'''),

    'pricing': ('Pricing', 'Service catalogue', 'What each call costs, in FLOP.', '''
<h2>Price table</h2>
<p>Free sources have no upstream bill. Caller-key sources are listed at their notional price so budgets compare. Prices are nominal until on-chain settlement grants are enabled.</p>
<div class="pricetable" id="priceTable"><div class="skeleton" style="height:220px"></div></div>
<h2>Nominal metering</h2>
<div class="warnbox"><strong>Scout's service fee is currently nominal (computed, not debited).</strong> Miner inference cost is separate and paid by whichever wallet/key runs the model (Scout's wallet on free/member tiers, or yours when using your own key).</div>
'''),

    'limits': ('Pricing', 'Limits & fair use', 'Rate limits, member keys and wallet safety caps.', '''
<h2>Rate limits by payer</h2>
<p>Limits depend on how you authenticate. Switch payer anytime in the topbar wallet panel.</p>
<div class="limits">
  <div class="card limit">
    <p class="who">Scout free tier</p>
    <div class="num"><b>500</b><span>answers / day / IP</span></div>
    <p>No key required. Scout's wallet pays the miner. Page reads have a separate 500/day quota.</p>
  </div>
  <div class="card limit">
    <p class="who">Member key (sk_…)</p>
    <div class="num"><b>∞</b><span>no daily cap</span></div>
    <p>30 answers/min pace guard. Scout's wallet pays. Works via <code>X-Scout-Key</code> or Bearer on <code>/v1</code> and <code>/mcp</code>.</p>
  </div>
  <div class="card limit">
    <p class="who">Your own key</p>
    <div class="num"><b>6</b><span>answers / min</span></div>
    <p>Your FLOP (<code>flop_…</code>) or OpenRouter key pays directly. No daily cap and unaffected by Scout's wallet budget.</p>
  </div>
</div>
<p>Scout's own wallet has a <strong>10,000 FLOP / 24h</strong> safety ceiling across free and member traffic. Failed requests refund their quota slot automatically, and identical answers within 10 minutes are served free from cache.</p>
<h2>Fair use</h2>
<div class="warnbox"><strong>Fair use.</strong> Testnet airdrop rules credit genuine settled compute only. Scripted loops, self-dealing to your own miner and multi-wallet splitting forfeit eligibility.</div>
'''),

    'faq': ('FAQ', 'Frequently asked', 'Short answers to the questions that come up most.', '''
<div class="faq">
<details class="card faq-item"><summary>Where do I find my FLOP API key for Cherry Studio or OpenAI SDK?</summary><p>Click the wallet button in the top right. Once enrolled (or pasted), your <code>flop_…</code> key is stored there masked — click <strong>Show</strong> or <strong>Copy key</strong> anytime.</p></details>
<details class="card faq-item"><summary>How do I choose whether Scout or my wallet pays?</summary><p>Open the wallet panel and pick between <strong>Scout free tier</strong> (500/day), <strong>My FLOP key</strong>, or <strong>Scout member key (sk_…)</strong>. On <code>/v1</code>, the <code>Authorization: Bearer</code> token decides automatically.</p></details>
<details class="card faq-item"><summary>Why are there two prices on /answer?</summary><p>Miner inference cost goes to the miner that ran the model; Scout's fee covers search, page reading and citations. They are never combined into one number.</p></details>
<details class="card faq-item"><summary>What is the status of FLOP Testnet?</summary><p>The FLOP Testnet chain launched in unscored opening mode (validators producing blocks; public miner onboarding and gateway/faucet rollout in progress). Scout currently reads live devnet data and switches to testnet via config as soon as testnet miners and gateway go live.</p></details>
<details class="card faq-item"><summary>What does "independent" settle rate mean on the miner board?</summary><p>It excludes any single agent wallet responsible for more than 25% of sessions in the window, so one dominant load-generator cannot distort every miner's reliability score.</p></details>
<details class="card faq-item"><summary>What does "auto" pick in /api/answer?</summary><p>Up to 3 free sources matched from your question (including reading any URL in the question directly). Paid sources are never added unless named explicitly.</p></details>
</div>
'''),

    'about': ('About', 'About Scout', 'Architecture, current status and contacts.', '''
<h2>Why this exists</h2>
<p>Flop Scout is an independent analytics board and agent gateway for the FLOP network: session-by-session miner reliability, 4-tier streamed miner probes, search and page reading over MCP, and an OpenAI-compatible <code>/v1</code> endpoint. Not affiliated with FLOP Labs.</p>
<h2>Current status &amp; caveats</h2>
<ul class="caveats">
  <li><strong>Testnet rollout in progress.</strong> FLOP Testnet opened in unscored chain-only mode; Scout tracks live devnet sessions (~48k+ channels rebuilt from the explorer) and switches to testnet once miners and gateway are active.</li>
  <li><strong>Nominal Scout fee.</strong> Scout's own per-call fee is computed and logged, not debited, until on-chain <code>connect/grants</code> settlement is wired.</li>
  <li><strong>Miner pinning requires owner keys.</strong> <code>X-Scout-Miner: auto</code> selects the top-scored priced miner, but the FLOP gateway only honours <code>x-flop-miner</code> on owner credentials (wallet keys receive HTTP 403).</li>
  <li><strong>10,000 FLOP/day wallet guard.</strong> Shared free and member inference paid by Scout pauses cleanly if 24h spend reaches 10,000 FLOP; callers using their own key are never paused.</li>
</ul>
<div class="warnbox"><strong>Not affiliated with FLOP Labs.</strong> Independent open-source project (MIT). Answers can be wrong — verify linked sources. <a href="https://flop.finance/airdrop/" target="_blank" rel="noopener">FLOP airdrop terms</a>.</div>
<h2>Project</h2>
<ul>
  <li>X  <a href="https://x.com/Flopscoutcom" target="_blank" rel="noopener">@Flopscoutcom</a></li>
  <li>GitHub  <a href="https://github.com/0xBobaAI/flop-scout" target="_blank" rel="noopener">0xBobaAI/flop-scout</a></li>
</ul>
<h2>Team</h2>
<ul>
  <li>X  <a href="https://x.com/bingobongo_0" target="_blank" rel="noopener">@bingobongo_0</a></li>
  <li>Telegram  <a href="https://t.me/iLiaBR" target="_blank" rel="noopener">@iLiaBR</a></li>
</ul>
'''),
}

# Sidebar group order; the group's pages keep their DOCS insertion order.
DOCS_GROUPS = ['Start here', 'API reference', 'Pricing', 'FAQ', 'About']

DOCS_SCRIPTS = {'pricing': ['pricing.js']}
