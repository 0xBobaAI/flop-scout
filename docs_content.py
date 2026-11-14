"""Docs content — one page per topic, grouped in a sidebar like a normal docs site.

Each entry is `slug: (group, title, summary, body_html)`. `body_html` only ever
contains `<h2>`/`<h3>` and ordinary content; the on-page TOC is built from the
`<h2>` tags automatically, so a new section needs no second place to register it.

Order here is sidebar order. Keep the file self-contained — server.py only reads
`DOCS` and `DOCS_GROUPS`.
"""

DOCS = {
    'overview': ('Start here', 'Overview', 'What Scout is and the two numbers on every answer.', '''
<h2>What this is</h2>
<p>Scout sells search and page reading to AI agents, priced per call in FLOP and paid from the agent's own wallet. A second surface ranks FLOP miners by settlement, not self-reported uptime.</p>
<p>Every answer shows two numbers, never mixed:</p>
<div class="rc-split">
  <div class="rc-part miner"><b>Miner cost</b><strong>2.748 FLOP</strong><span>Paid to the miner that ran the inference.</span></div>
  <div class="rc-part scout"><b>Scout fee</b><strong>0.009 FLOP</strong><span>Search, reading, citations. Itemised.</span></div>
</div>
<h2>Not this</h2>
<p>Not a chat UI, not a general assistant, not a competitor to FLOP's own documentation assistant. Answer quality is not the moat — pricing, routing and the miner ranking are.</p>
'''),

    'quick-start': ('Start here', 'Quick start', 'Quote a request, then ask it. Two calls.', '''
<h2>1. Quote</h2>
<p>Price a request before anything runs. No search, no inference, no ledger row.</p>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="POST /api/quote">Copy</button>
  <pre class="code" data-lang="request">POST /api/quote
{ "sources": ["exa", "keenable"] }

→ { "total_flop": 0.011, "billed": ["answer", "exa"] }</pre>
</div>
<h2>2. Ask</h2>
<p><code>sources: ["auto"]</code> lets Scout pick free sources from the question. A paid source is only ever used if you name it.</p>
<div class="codewrap">
  <button class="btn small copybtn" type="button" data-copy="POST /api/answer">Copy</button>
  <pre class="code" data-lang="request">POST /api/answer
{ "q": "Which FLOP miners are most reliable?", "sources": ["auto"] }

→ { "answers": [ … ], "pricing": { … } }</pre>
</div>
<h2>Next</h2>
<p>See the full <a href="/docs/endpoints">endpoint list</a> and <a href="/docs/pricing">pricing</a>.</p>
'''),

    'endpoints': ('API reference', 'Endpoints', 'Every route Scout exposes.', '''
<h2>Answering</h2>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Endpoint</th><th>Returns</th></tr></thead>
<tbody>
<tr><td class="mono">POST /api/answer</td><td>search → cited answer → receipt, with an itemised FLOP price</td></tr>
<tr><td class="mono">POST /api/quote</td><td>the price before you send it. No search, no inference, no ledger row</td></tr>
<tr><td class="mono">GET /api/pricing</td><td>the full catalogue, metering mode, and which services are available</td></tr>
<tr><td class="mono">GET /api/catalog</td><td>sources, presets and models</td></tr>
</tbody></table></div>
<h2>Miners</h2>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Endpoint</th><th>Returns</th></tr></thead>
<tbody>
<tr><td class="mono">GET /api/router</td><td>live miner ranking with scores</td></tr>
<tr><td class="mono">GET /api/miners</td><td>every miner with GPU, stake, router score and 24h settlement delta</td></tr>
<tr><td class="mono">GET /api/miners/{address}</td><td>one miner, same shape as a row</td></tr>
<tr><td class="mono">GET /api/reliability</td><td>settlement-rate deltas per miner over a window (default 24h)</td></tr>
</tbody></table></div>
<h2>Operations</h2>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Endpoint</th><th>Returns</th></tr></thead>
<tbody>
<tr><td class="mono">GET /api/stats</td><td>success rate per model, payer split, quoted vs charged totals</td></tr>
<tr><td class="mono">GET /api/analytics</td><td>daily demand aggregates. No question text, no client IDs</td></tr>
<tr><td class="mono">GET /api/health</td><td>liveness, metering mode, and whether the gateway accepts the key</td></tr>
</tbody></table></div>
'''),

    'errors': ('API reference', 'Errors', 'What comes back when it goes wrong.', '''
<h2>Status codes</h2>
<p>If the model fails, sources and prices still return. Unknown API paths return JSON, not a page.</p>
<div class="table-wrap"><table class="ltable">
<thead><tr><th>Status</th><th>Meaning</th><th>What to do</th></tr></thead>
<tbody>
<tr><td class="mono">422 invalid_body</td><td>A field failed validation</td><td>q 2–500 chars, n 1–8, max_tokens 32–800, sources from /api/catalog</td></tr>
<tr><td class="mono">429 free_tier</td><td>Free answers for the day are used up</td><td>Connect a wallet and pass your own gateway key</td></tr>
<tr><td class="mono">429 rate_limit_exceeded</td><td>The devnet gateway is throttling</td><td>Back off. Scout already retries inside a 200 s budget</td></tr>
<tr><td class="mono">429 miner_capacity_reserved</td><td>Every miner is at capacity</td><td>Retry shortly</td></tr>
<tr><td class="mono">503 / 524</td><td>Origin timeout at the gateway</td><td>Congestion, not your request. Sources still return</td></tr>
<tr><td class="mono">401 model_not_allowed</td><td>Your key is not scoped for that model</td><td>New devnet keys get qwen2.5-0.5b only</td></tr>
<tr><td class="mono">404 not_found</td><td>Unknown API path or miner address</td><td>GET /api/catalog lists what exists</td></tr>
</tbody></table></div>
'''),

    'pricing': ('Pricing', 'Service catalogue', 'What each call costs, in FLOP.', '''
<h2>Price table</h2>
<p>Free sources have no upstream bill. Caller-key sources are listed at their notional price so budgets compare. Prices are devnet-nominal.</p>
<div class="pricetable" id="priceTable"><div class="skeleton" style="height:220px"></div></div>
<h2>Nominal metering</h2>
<div class="warnbox"><strong>On devnet Scout's fee is computed, not debited.</strong> The miner's cost is paid from the caller's wallet to the miner. Scout's fee covers search, page reading and citations.</div>
'''),

    'limits': ('Pricing', 'Limits & fair use', 'Free tier caps and what counts as real usage.', '''
<h2>How many answers</h2>
<p>The limit depends on who pays for the inference.</p>
<div class="limits">
  <div class="card limit">
    <p class="who">No wallet</p>
    <div class="num"><b>20</b><span>answers a day</span></div>
    <p>Scout pays the miner. Counted per IP address.</p>
  </div>
  <div class="card limit">
    <p class="who">Your own key</p>
    <div class="num"><b>6</b><span>answers a minute</span></div>
    <p>Your wallet pays the miner. No daily cap.</p>
  </div>
  <div class="card limit">
    <p class="who">Every request</p>
    <ul>
      <li><span>Sources per request</span><b>up to 6</b></li>
      <li><span>Results returned</span><b>up to 12</b></li>
      <li><span>Repeat in 10 min</span><b>cached, free</b></li>
    </ul>
  </div>
</div>
<p>A failed answer does not use up your limit.</p>
<h2>Fair use</h2>
<div class="warnbox"><strong>Fair use.</strong> Rewards go to compute you actually buy. Repeated questions, scripted loops, self-dealing and multi-wallets are forfeited.</div>
'''),

    'faq': ('FAQ', 'Frequently asked', 'Short answers to the questions that come up most.', '''
<div class="faq">
<details class="card faq-item"><summary>Do I need an account or a card?</summary><p>No. Scout is a POST and a JSON body. Payment comes from a FLOP wallet, and the free tier needs nothing.</p></details>
<details class="card faq-item"><summary>Why are there two prices?</summary><p>The miner's inference cost goes to the miner, and Scout's fee covers search, reading and citations. They are never added together or shown as one number.</p></details>
<details class="card faq-item"><summary>Is anything charged on devnet?</summary><p>No. Every charge is computed and logged, but nothing is debited. The testnet is not live yet.</p></details>
<details class="card faq-item"><summary>What does "auto" pick?</summary><p>Free sources only, chosen from keywords in your question. A paid source is never added unless you ask for it by name.</p></details>
<details class="card faq-item"><summary>Can an answer be wrong?</summary><p>Yes. Every claim links to its numbered source, so you can check it.</p></details>
<details class="card faq-item"><summary>Can I export prices?</summary><p>Yes. <code>GET /api/pricing</code> returns the full catalogue as JSON.</p></details>
</div>
'''),

    'about': ('About', 'About Scout', 'Why this exists, and what is not finished.', '''
<h2>Why this exists</h2>
<p>Most agent tooling assumes a human with a card. FLOP lets an agent pay per call from its own wallet. Scout sells search and page reading on that basis. It is an independent tool, not affiliated with FLOP Labs.</p>
<h2>Not finished yet</h2>
<ul class="caveats">
  <li><strong>Nominal metering.</strong> Prices are real. Debits are not.</li>
  <li><strong>No enforced spending cap.</strong> <code>/api/quote</code> shows the total. It does not enforce it.</li>
  <li><strong>Routing is advisory.</strong> The gateway picks the miner. Scout cannot pin one.</li>
  <li><strong>Short history.</strong> Trends need about a week of snapshots.</li>
  <li><strong>Devnet only.</strong> Activity earns nothing. No allocation is promised.</li>
</ul>
<div class="warnbox"><strong>Not affiliated with FLOP Labs.</strong> Independent tool. Answers can be wrong. Every claim links to its source. <a href="https://flop.finance/airdrop/" target="_blank" rel="noopener">FLOP airdrop terms</a>.</div>
'''),
}

# Sidebar group order; the group's pages keep their DOCS insertion order.
DOCS_GROUPS = ['Start here', 'API reference', 'Pricing', 'FAQ', 'About']

DOCS_SCRIPTS = {'pricing': ['pricing.js']}
