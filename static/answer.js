/* Flop Scout — Answer page.
   Depends on agent.js for wallet state (agent, short, keyValid, toast, esc, $).
   This file only handles: catalog, presets, the request, and rendering results. */

let CAT = null;

/* ── catalog ────────────────────────────────────────────────────────────── */
async function init() {
  CAT = await (await fetch('/api/catalog')).json();

  const groups = {};
  CAT.sources.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
  $('sourcePick').innerHTML = Object.entries(groups).map(([g, list]) =>
    `<div class="g">${esc(g)}</div>` + list.map(s =>
      `<label><input type="checkbox" value="${s.id}" ${s.default ? 'checked' : ''}>${esc(s.label)}<small>${esc(s.hint || '')}</small></label>`
    ).join('')).join('');

  $('presets').innerHTML = Object.entries(CAT.presets)
    .map(([k, p]) => `<button class="chip" data-p="${k}" type="button">${esc(p.label)}</button>`).join('');
  document.querySelectorAll('#presets .chip').forEach(ch => ch.onclick = () => setPreset(ch.dataset.p));

  const opt = m => `<option value="${m.id}"${m.blocked ? ' disabled' : ''}>${m.provider === 'flop' ? 'FLOP · ' : 'OpenRouter · '}${esc(m.label)}${m.blocked ? ' (not on your key)' : ''}</option>`;
  $('model').innerHTML = CAT.models.map(opt).join('');
  $('compare').innerHTML = '<option value="">Off</option>' + CAT.models.map(opt).join('');

  network();
  const u = new URLSearchParams(location.search);
  if (u.get('p')) setPreset(u.get('p'));
  if (u.get('q')) { $('q').value = u.get('q'); ask(); }
}

function setPreset(p) {
  if (!CAT?.presets[p]) return;
  document.querySelectorAll('#presets .chip').forEach(x => x.classList.toggle('on', x.dataset.p === p));
  const set = CAT.presets[p].sources;
  document.querySelectorAll('#sourcePick input').forEach(i => { i.checked = set.includes(i.value); });
}

/* ── network strip ──────────────────────────────────────────────────────── */
async function network() {
  try {
    const [h, m, cat] = await Promise.all([
      fetch(FLOP_API + '/health').then(r => r.json()),
      fetch(FLOP_API + '/v1/models').then(r => r.json()),
      fetch('/api/catalog').then(r => r.json()).catch(() => ({})),
    ]);
    const ok = h.status === 'ok';
    $('netDot').classList.toggle('ok', ok);
    $('netName').textContent = `FLOP ${h.network?.network || ''}`.trim();
    const tpm = (m.data || []).reduce((a, x) => a + (x['x-flop']?.capacity_tpm || 0), 0);
    const r = cat.router;
    const best = r?.best_miner?.address
      ? ` · best miner ${r.best_miner.address.slice(0, 8)}… (${(r.best_miner.score * 100).toFixed(0)}%)`
      : '';
    $('netNote').textContent =
      `${ok ? 'live' : 'degraded'} · ${(m.data || []).length} models · ${tpm.toLocaleString('en')} tok/min · ${r?.active_priced ?? '?'} active miners${best} · devnet activity earns nothing`;
  } catch {
    $('netNote').textContent = 'status unavailable';
  }
}

/* ── answer rendering ───────────────────────────────────────────────────── */
function answerCard(a) {
  const or = a.engine === 'openrouter';
  const body = a.answer
    ? `<div class="answer-body">${esc(a.answer).replace(/\[(\d+)\]/g, '<a href="#s$1">$1</a>')}</div>`
    : `<div class="err">${esc(a.error || 'No answer')}</div>`;

  const meta = [];
  if (a.latency_s != null) meta.push(`${a.latency_s}s`);
  if (a.attempts > 1) meta.push(`${a.attempts} attempts`);
  if (a.usage?.completion_tokens) meta.push(`${a.usage.prompt_tokens}→${a.usage.completion_tokens} tokens`);
  if (a.usage?.cost_flop != null) meta.push(`miner ${a.usage.cost_flop} FLOP`);
  if (a.receipt?.miner) meta.push(`miner ${short(a.receipt.miner)}`);
  if (a.receipt?.settlementStatus) meta.push(`settlement ${a.receipt.settlementStatus}`);
  if (a.paid_by) meta.push(`compute paid by ${a.paid_by === 'user' ? 'your wallet' : 'Scout'}`);

  const fee = a.scout_charge_flop != null
    ? `<div class="answer-fee"><span class="mono">${a.scout_charge_flop} FLOP</span> to Scout for search, routing and citations${a.metering === 'nominal' ? ' · <em>not charged on devnet</em>' : ''}</div>`
    : '';

  return `<article class="card answer">
    <div class="answer-top"><span class="kind ${or ? 'or' : ''}">${or ? 'OpenRouter' : 'FLOP'} · ${esc(a.model)}</span></div>
    ${body}
    <div class="answer-meta">${meta.map(esc).join('<span>·</span>')}</div>
    ${fee}
  </article>`;
}

function sourceCard(s) {
  return `<a class="src" href="${esc(s.url)}" target="_blank" rel="noopener" id="s${s.n}">
    <div class="src-top"><span class="src-n">${s.n}</span><span class="src-from">${esc(s.source)}</span></div>
    <h4>${esc(s.title)}</h4>
    <p>${esc(s.snippet)}</p>
  </a>`;
}

function priceLine(p) {
  const rows = p.items.map(i =>
    `<li><span>${esc(i.label)}</span><span class="mono">${i.price_flop ? i.price_flop.toFixed(3) + ' FLOP' : 'free'}</span></li>`
  ).join('');
  const total = p.cache_hit
    ? 'cached — 0 FLOP'
    : `${p.total_flop.toFixed(3)} FLOP${p.metering === 'nominal' ? ' <em>(not charged on devnet)</em>' : ''}`;
  return `<details class="pricebox"><summary><span class="mono">${total}</span> · this answer's Scout fee</summary><ul>${rows}</ul></details>`;
}

/* ── the request ────────────────────────────────────────────────────────── */
async function ask(e) {
  if (e?.preventDefault) e.preventDefault();
  const q = $('q').value.trim();
  if (q.length < 2) return toast('Ask a longer question');
  const sources = [...document.querySelectorAll('#sourcePick input:checked')].map(i => i.value);
  if (!sources.length) return toast('Pick at least one source');

  const keys = {};
  if (keyValid()) keys.flop = agent.key;
  const body = { q, sources, model: $('model').value, compare_model: $('compare').value || null, keys };

  $('goBtn').disabled = true;
  const t0 = Date.now();
  $('results').innerHTML = `<div class="progress"><i></i><span id="prog">Searching ${sources.length} sources…</span></div>
    <div class="skeleton"></div><div class="skeleton" style="margin-top:10px;height:80px"></div>`;
  const tick = setInterval(() => {
    const p = $('prog');
    if (p) p.textContent = `Computing on FLOP… ${Math.round((Date.now() - t0) / 1000)}s (devnet miners can take 1–3 min)`;
  }, 1500);

  try {
    const r = await fetch('/api/answer', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : r.status);

    let h = j.answers.length
      ? `<div class="answers ${j.answers.length > 1 ? 'two' : ''}">${j.answers.map(answerCard).join('')}</div>`
      : `<div class="empty"><h2>No sources found.</h2><p>Try other sources or rephrase the question.</p></div>`;

    if (j.sources.length) {
      h += `<div class="sources"><h3>Sources · search ${j.search_s}s · ${
        j.source_report.map(x => `${esc(x.label)} ${x.error ? '✕' : x.count}`).join(' · ')
      }</h3>${j.sources.map(sourceCard).join('')}</div>`;
    }
    if (j.pricing?.items?.length) h += priceLine(j.pricing);
    $('results').innerHTML = h;
    document.dispatchEvent(new CustomEvent('agent:answered'));
  } catch (err) {
    $('results').innerHTML = `<div class="empty"><h2>Something went wrong.</h2><p>${esc(err.message || err)}</p></div>`;
  } finally {
    clearInterval(tick);
    $('goBtn').disabled = false;
  }
}

/* ── sidebar agent panel ────────────────────────────────────────────────── */
function renderAgentPanel() {
  const stats = $('agentStats');
  if (stats) stats.hidden = !agent;

  if (!agent) {
    $('agentTitle').textContent = 'No wallet connected';
    $('agentSub').textContent = "Answers run on Scout's devnet wallet. Connect your own to pay for compute yourself, keep the receipts and have sessions credited to your address.";
    $('paidBy').textContent = 'Compute paid by: Scout (free tier)';
    return;
  }
  $('agentTitle').textContent = short(agent.address);
  $('agentSub').textContent = keyValid()
    ? `Gateway key active until ${new Date(agent.expires_at).toLocaleString('en-GB')}. Your wallet pays for every FLOP answer.`
    : 'Wallet ready, gateway key missing or expired — open the wallet to enroll.';
  $('paidBy').textContent = keyValid()
    ? `Compute paid by: your wallet ${short(agent.address)}`
    : 'Compute paid by: Scout (enroll a key to use your wallet)';
}

async function refreshAgentStats() {
  if (!keyValid()) { renderAgentPanel(); return; }
  try {
    const h = { Authorization: 'Bearer ' + agent.key };
    const [me, rc] = await Promise.all([
      fetch(FLOP_API + '/v1/keys/me', { headers: h }).then(r => r.json()),
      fetch(FLOP_API + '/v1/keys/me/receipts', { headers: h }).then(r => r.json()),
    ]);
    const list = rc.receipts || [];
    const settled = list.filter(x => x.state === 'settled');
    const spend = settled.reduce((a, x) => a + (x.settled_spend || x.reserved_spend || 0), 0);
    $('stSessions').textContent = settled.length;
    $('stSpend').textContent = spend.toFixed(2);
    $('stQuota').textContent = me.remaining_quota != null ? Math.round(me.remaining_quota).toLocaleString('en') : '—';
    $('stNote').textContent = `${list.length} requests on this key · ${list.filter(x => x.state === 'released').length} released without charge (busy miners). Data: FLOP gateway ledger.`;
  } catch {
    $('stNote').textContent = 'Could not reach the FLOP gateway ledger.';
  }
  renderAgentPanel();
}

/* ── wiring ─────────────────────────────────────────────────────────────── */
$('askForm').onsubmit = ask;

document.querySelectorAll('.examples button').forEach(b => {
  b.onclick = () => { $('q').value = b.dataset.q; setPreset(b.dataset.p); ask(); };
});

$('refreshAgent').onclick = refreshAgentStats;

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); $('q').focus(); }
});

// agent.js emits these after the shared wallet changes state
document.addEventListener('agent:change', () => { renderAgentPanel(); if (keyValid()) refreshAgentStats(); });
document.addEventListener('agent:enrolled', () => refreshAgentStats());
document.addEventListener('agent:answered', () => { if (keyValid()) refreshAgentStats(); });

renderAgentPanel();
if (keyValid()) refreshAgentStats();
init();
