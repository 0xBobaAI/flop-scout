/* Flop Scout — Answer page.
   Depends on agent.js for wallet state (agent, short, keyValid, toast, esc, $).
   This file only handles: catalog, presets, the request, and rendering results. */

let CAT = null;
let LAST_QUOTE = null;

/* ── catalog ────────────────────────────────────────────────────────────── */
async function init() {
  CAT = await (await fetch('/api/catalog')).json();

  const manual = CAT.sources.filter(s => s.id !== 'auto');
  $('sourcePick').innerHTML = manual.map(s => {
      // A key-gated source without the key cannot work. Say so instead of
      // offering a checkbox that can only return an empty list.
      const gate = s.key ? ` · needs ${esc(s.key)} key` : '';
      return `<label title="${esc(s.label + gate)}"><input type="checkbox" value="${s.id}" ${s.default ? 'checked' : ''}><span class="srcname">${esc(s.label)}</span><small>${esc(s.hint || '')}</small></label>`;
    }).join('');
  if ($('srcCount')) $('srcCount').textContent = `${manual.length} available`;  // live count, never hardcoded

  $('presets').innerHTML = Object.entries(CAT.presets)
    .map(([k, p]) => `<button class="chip" data-p="${k}" type="button">${esc(p.label)}</button>`).join('');
  setPreset('auto');
  document.querySelectorAll('#presets .chip').forEach(ch => ch.onclick = () => setPreset(ch.dataset.p));

  const opt = m => `<option value="${m.id}"${m.blocked ? ' disabled' : ''}>${m.provider === 'flop' ? 'FLOP · ' : 'OpenRouter · '}${esc(m.label)}${m.blocked ? ' (not on your key)' : ''}</option>`;
  $('model').innerHTML = CAT.models.map(opt).join('');
  $('compare').innerHTML = '<option value="">Off</option>' + CAT.models.map(opt).join('');
  // Default to a model the caller can actually run: a FLOP one, unless they
  // asked for OpenRouter by having a key.
  const firstFlop = CAT.models.find(m => m.provider === 'flop' && !m.blocked);
  if (firstFlop && !providerKeys.openrouter) $('model').value = firstFlop.id;
  markKeyState();
  previewQuote(true);

  network();
  const u = new URLSearchParams(location.search);
  if (u.get('p')) setPreset(u.get('p'));
  if (u.get('q')) { $('q').value = u.get('q'); ask(); }
}

let PRESET = 'auto';

function setPreset(p) {
  if (!CAT?.presets[p]) return;
  PRESET = p;
  document.querySelectorAll('#presets .chip').forEach(x => x.classList.toggle('on', x.dataset.p === p));
  const set = CAT.presets[p].sources;
  document.querySelectorAll('#sourcePick input').forEach(i => { i.checked = set.includes(i.value) && !i.disabled; });
  // Auto means Scout chooses from the question, so the manual list is hidden.
  const manual = document.getElementById('manualSources');
  if (manual) manual.open = p !== 'auto';
  previewQuote(true);
}

/* Auto sends the single token; Scout expands it server-side. A manual pick
   after a preset switches to explicit sources. */
function selectedSources() {
  if (PRESET === 'auto') return ['auto'];
  return [...document.querySelectorAll('#sourcePick input:checked')].map(i => i.value);
}

/* A source that needs a key you have not entered can only fail. Grey it, say
   why, and open the key panel in one click rather than letting the run fail.
   Also flag an OpenRouter model picked without an OpenRouter key. */
function markKeyState() {
  document.querySelectorAll('#sourcePick label').forEach(label => {
    const cb = label.querySelector('input');
    const src = CAT?.sources.find(s => s.id === cb.value);
    const need = src?.key;
    const ok = !need || !!providerKeys[need];
    label.classList.toggle('nokey', !ok);
    cb.disabled = !ok;
    if (!ok) cb.checked = false;
    const small = label.querySelector('small');
    if (small && need) small.textContent = ok ? (src.hint || '') : `needs ${need} key — add it in the wallet panel`;
  });
  const m = $('model').value || '';
  const needsOR = m.startsWith('openrouter:') || ($('compare').value || '').startsWith('openrouter:');
  if ($('modelWarn')) {
    $('modelWarn').hidden = !(needsOR && !providerKeys.openrouter);
  }
  if (LAST_QUOTE) previewQuote(true);
}

function openKeyPanel() {
  const d = $('walletDlg');
  if (d && !d.open) { walletView(); d.showModal(); }
  const det = d?.querySelector('details.pk');
  if (det) det.open = true;
}

/* ── network strip ──────────────────────────────────────────────────────── */
async function network() {
  try {
    // CAT is already loaded by init(); re-fetching it here doubled the
    // cold-start cost of the page for no new information.
    const [h, m] = await Promise.all([
      fetch(FLOP_API + '/health').then(r => r.json()),
      fetch(FLOP_API + '/v1/models').then(r => r.json()),
    ]);
    const cat = CAT || {};
    const ok = h.status === 'ok';
    $('netDot').classList.toggle('ok', ok);
    $('netName').textContent = `FLOP ${h.network?.network || ''}`.trim();
    const tpm = (m.data || []).reduce((a, x) => a + (x['x-flop']?.capacity_tpm || 0), 0);
    const r = cat.router;
    const best = r?.best_miner?.address
      ? ` · best miner ${r.best_miner.address.slice(0, 8)}… (${(r.best_miner.score * 100).toFixed(0)}%)`
      : '';
    // prices_known === false means Scout never got a price snapshot; saying
    // "0 active miners" there would be a lie, so say nothing about them.
    const minerTxt = r?.prices_known === false
      ? 'prices unavailable'
      : `${r?.active_priced ?? '—'} active miners`;
    $('netNote').textContent =
      `${ok ? 'live' : 'degraded'} · ${(m.data || []).length} models · ${tpm.toLocaleString('en')} tok/min · ${minerTxt}${best} · devnet activity earns nothing`;
  } catch {
    $('netNote').textContent = 'status unavailable';
  }
}

/* ── price preview ──────────────────────────────────────────────────────── */
async function previewQuote(silent) {
  const sources = selectedSources();
  if (!sources.length) { if (!silent) toast('Pick at least one source'); return null; }
  try {
    const r = await fetch('/api/quote', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sources, compare_model: $('compare').value || null }),
    });
    const q = await r.json();
    LAST_QUOTE = q;
    const billed = q.items.filter(i => i.price_flop > 0).length;
    const total = q.cache_hit ? '0' : q.total_flop.toFixed(3);
    const label = `${total} FLOP quoted · ${billed} billed line${billed === 1 ? '' : 's'}${q.metering === 'nominal' ? ' · not charged on devnet' : ''}`;
    if (!silent) toast(label, 4600);
    if ($('quoteChip')) {
      $('quoteChip').textContent = `≈ ${total} FLOP`;
      $('quoteChip').hidden = false;
    }
    return q;
  } catch {
    if (!silent) toast('Could not price this request');
    return null;
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

  // Two numbers, never mixed: the miner's own cost above, Scout's fee here.
  const quoted = a.scout_quote_flop;
  const fee = quoted != null
    ? `<div class="answer-fee"><span class="mono">${quoted} FLOP</span> quoted to Scout for search, routing and citations${a.metering === 'nominal' ? ' · <em>not charged on devnet</em>' : ''}</div>`
    : '';

  return `<article class="card answer">
    <div class="answer-top"><span class="kind ${or ? 'or' : ''}">${or ? 'OpenRouter' : 'FLOP'} · ${esc(a.model)}</span>${a.cached ? '<span class="kind grey">cached · free</span>' : ''}</div>
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
  return `<details class="pricebox"${p.cache_hit ? '' : ' open'}><summary><span class="mono">${total}</span> · this answer's Scout fee</summary><ul>${rows}</ul></details>`;
}

/* ── the request ────────────────────────────────────────────────────────── */
async function ask(e) {
  if (e?.preventDefault) e.preventDefault();
  const q = $('q').value.trim();
  if (q.length < 2) return toast('Ask a longer question');
  const sources = selectedSources();
  if (!sources.length) return toast('Pick at least one source');

  const body = { q, sources, model: $('model').value, compare_model: $('compare').value || null, keys: activeKeys() };

  $('goBtn').disabled = true;
  const t0 = Date.now();
  $('results').innerHTML = `<div class="progress"><i></i><span id="prog">Searching ${sources.length} sources…</span></div>
    <div class="skeleton"></div><div class="skeleton" style="margin-top:10px;height:80px"></div>`;
  const tick = setInterval(() => {
    const p = $('prog');
    if (p) p.textContent = `Computing on FLOP… ${Math.round((Date.now() - t0) / 1000)}s · devnet can take up to 3 min`;
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

    if (j.sources?.length) {
      h += `<div class="sources"><h3>Sources · search ${j.search_s}s · ${
        j.source_report.map(x => `${esc(x.label)} ${x.error ? '✕' : x.count}`).join(' · ')
      }</h3>${j.sources.map(sourceCard).join('')}</div>`;
    }
    if (j.pricing?.items?.length) h += priceLine(j.pricing);
    $('results').innerHTML = h;
    document.dispatchEvent(new CustomEvent('agent:answered'));
    // Deep-link the question so a result can be shared or re-opened.
    const u = new URL(location.href);
    u.searchParams.set('q', q);
    history.replaceState(null, '', u);
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
    $('agentSub').textContent = "Free answers on Scout's wallet, shared by everyone. Connect your wallet to pay from your own FLOP.";
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
$('quoteBtn').onclick = () => previewQuote(false);
$('sourcePick').addEventListener('change', () => { if (PRESET === 'auto') { PRESET = ''; document.querySelectorAll('#presets .chip').forEach(x => x.classList.remove('on')); } previewQuote(true); });
$('compare').addEventListener('change', () => previewQuote(true));

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); $('q').focus(); $('q').select(); }
});

// agent.js emits these after the shared wallet changes state
document.addEventListener('agent:change', () => { renderAgentPanel(); if (keyValid()) refreshAgentStats(); });
document.addEventListener('agent:enrolled', () => refreshAgentStats());
document.addEventListener('agent:answered', () => { if (keyValid()) refreshAgentStats(); });
document.addEventListener('keys:change', () => { markKeyState(); });
$('model').addEventListener('change', markKeyState);
const mw = $('modelWarn');
if (mw) mw.querySelector('button').onclick = openKeyPanel;

renderAgentPanel();
if (keyValid()) refreshAgentStats();
init();
