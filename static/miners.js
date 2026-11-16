/* Flop Scout: miners page. Depends on agent.js for $, esc, short, toast. */

const EXPLORER = window.FLOP_EXPLORER || 'https://explorer.devnet.flop.network';
document.documentElement.classList.add('js-tips');
const fmtTpm = t => t == null ? '—' : t >= 1e6 ? (t / 1e6).toFixed(1) + 'M' : t >= 1e3 ? (t / 1e3).toFixed(0) + 'k' : String(t);
const fmtNum = v => v == null ? '—' : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : String(Math.round(v));
const pct = (v, d = 0) => v == null ? '—' : (v * 100).toFixed(d) + '%';
const FLAG = cc => cc ? String.fromCodePoint(...[...cc.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : '';
const COUNTRY = (() => { try { return new Intl.DisplayNames(['en'], { type: 'region' }); } catch { return null; } })();
const cname = cc => (COUNTRY && cc ? COUNTRY.of(cc) : cc) || cc;

let DATA = null;
let ROUTER = {};            // address -> router row
let SHOW = 'priced';
let F_MODEL = '';
let F_COUNTRY = '';
let F_ADDR = '';
let SORT = { key: 'score', dir: -1 };

/* ---- per-miner derived values ---- */
function h24(m) { return m.history_24h || null; }
function settled24(m) { const h = h24(m); return h && h.sessions ? h.settle_rate : null; }
function indep24(m) { const h = h24(m); return h && h.independent_sessions ? h.independent_settle_rate : null; }
function change24(m) { const h = h24(m); return h ? h.settle_rate_change : null; }
function score(m) { return m.router_score ?? null; }
function country(m) { return m.country || null; }

const COLS = [
  { key: 'rank',    label: '#',          sort: null },
  { key: 'miner',   label: 'Miner',      sort: null },
  { key: 'country', label: 'Region',     sort: m => country(m) || (m.country_candidates?.length ? '~~' : '~~~'), tip: 'region', asc: true },
  { key: 'score',   label: 'Score',      sort: m => score(m) ?? -1, tip: 'score' },
  { key: 'settled', label: 'Settled 24h', sort: m => settled24(m) ?? -1 },
  { key: 'indep',   label: 'Independent', sort: m => indep24(m) ?? -1 },
  { key: 'change',  label: 'Δ 24h',      sort: m => change24(m) ?? -9 },
  { key: 'sessions', label: 'Sessions',  sort: m => h24(m)?.sessions ?? 0 },
  { key: 'price',   label: 'Price',      sort: m => m.best_price ? -parseFloat(m.best_price) : -99 },
  { key: 'gpu',     label: 'GPU',        sort: null },
  { key: 'stake',   label: 'Stake',      sort: m => m.stake_flop || 0 },
];

const tip = html => `<span class="tip" tabindex="0" aria-label="Explain">?<span class="tip-box" role="tooltip">${html}</span></span>`;
const REGION_TIP = tip(`FLOP reports datacenter countries per model, not per miner. A miner's country is shown when every model it serves points to one country; otherwise up to three candidates, or a dash. Hover a dash for the list.`);
const SCORE_TIP = `<span class="tip" tabindex="0" aria-label="How the score is computed">?<span class="tip-box" role="tooltip">
  <b>Score</b>, 0–100:<br>
  55% &nbsp;settle rate over independent demand, last 7 days, smoothed so a few sessions cannot read as 0% or 100%<br>
  35% &nbsp;Scout's own success and latency (from probes, once the testnet opens)<br>
  10% &nbsp;offering inference in the gateway now<br><br>
  A miner with no independent sessions is scored on its lifetime explorer counters instead.
</span></span>`;
const TIPS = { score: SCORE_TIP, region: REGION_TIP };

/* ---- filters ---- */
function filtered() {
  let ms = DATA.miners;
  if (F_ADDR) {
    const q = F_ADDR.toLowerCase();
    return ms.filter(m => m.address.toLowerCase().includes(q));
  }
  if (SHOW === 'priced') ms = ms.filter(m => m.priced);
  if (SHOW === 'sessions') ms = ms.filter(m => h24(m)?.sessions);
  if (F_MODEL) ms = ms.filter(m => (m.models || []).includes(F_MODEL));
  if (F_COUNTRY) ms = ms.filter(m => m.country === F_COUNTRY || (m.country_candidates || []).includes(F_COUNTRY));
  return ms;
}

function sorted(ms) {
  const col = COLS.find(c => c.key === SORT.key) || COLS[3];
  const f = col.sort || (m => score(m) ?? -1);
  return [...ms].sort((a, b) => {
    const x = f(a), y = f(b);
    if (x < y) return -SORT.dir;
    if (x > y) return SORT.dir;
    return (score(b) ?? -1) - (score(a) ?? -1);
  });
}

/* ---- cells ---- */
function scoreCell(m) {
  const s = score(m);
  if (s == null) return '<span class="c-muted">—</span>';
  const cls = s >= 0.75 ? 'good' : s >= 0.5 ? 'mid' : 'low';
  return `<span class="sc ${cls}"><i style="width:${Math.round(s * 100)}%"></i><b>${Math.round(s * 100)}</b></span>`;
}

function rateCell(v, n, total) {
  if (v == null) return '<span class="c-muted">—</span>';
  const few = (total ?? n ?? 99) < 3;
  const cls = few ? 'c-muted' : v >= 0.8 ? 'c-ok' : v >= 0.5 ? '' : 'c-err';
  return `<span class="mono ${cls}"${few ? ' title="Fewer than 3 sessions"' : ''}>${pct(v)}</span>${n != null ? `<small class="n"> ${n}</small>` : ''}`;
}

function changeCell(m) {
  const d = change24(m);
  if (d == null) return '<span class="c-muted">—</span>';
  const p = d * 100;
  if (Math.abs(p) < 0.5) return '<span class="mono c-muted">±0</span>';
  return `<span class="mono ${p > 0 ? 'c-ok' : 'c-err'}">${p > 0 ? '▲' : '▼'} ${Math.abs(p).toFixed(1)}</span>`;
}

function regionCell(m) {
  if (m.country) return `<span class="cc" title="${esc(cname(m.country))}">${esc(m.country)}</span>`;
  const c = m.country_candidates || [];
  if (c.length && c.length <= 3) return `<span class="c-muted" title="One of: ${esc(c.map(cname).join(', '))}">${c.map(esc).join(' / ')}</span>`;
  if (c.length) return `<span class="c-muted" title="One of ${c.length} countries: ${esc(c.map(cname).join(', '))}">—</span>`;
  return '<span class="c-muted">—</span>';
}

function statusDot(m) {
  if (m.active) return '<i class="sd on" title="Offering inference now"></i>';
  if (m.priced) return '<i class="sd mid" title="Priced, offer window closed"></i>';
  return '<i class="sd" title="Registered, not offering inference"></i>';
}

function row(m, i) {
  const h = h24(m);
  const gpu = m.gpus?.[0] ? esc((m.gpus[0].name || 'GPU').replace('NVIDIA ', '')) + (m.gpus[0].count > 1 ? ` ×${m.gpus[0].count}` : '') : '<span class="c-muted">—</span>';
  return `<tr class="${m.router_rank === 1 ? 'best' : ''}">
    <td class="mono c-muted">${i + 1}</td>
    <td><span class="mcell">${statusDot(m)}<a class="mono" href="${EXPLORER}/account/${esc(m.address)}" target="_blank" rel="noopener" title="${esc(m.address)}">${esc(short(m.address))}</a>
      <button class="copy" data-addr="${esc(m.address)}" type="button" aria-label="Copy address">⧉</button></span></td>
    <td>${regionCell(m)}</td>
    <td>${scoreCell(m)}</td>
    <td>${rateCell(settled24(m), null, h?.sessions || 0)}</td>
    <td>${rateCell(indep24(m), h?.independent_sessions || null)}</td>
    <td>${changeCell(m)}</td>
    <td class="mono">${h?.sessions || '<span class="c-muted">0</span>'}</td>
    <td class="mono">${m.best_price ?? '<span class="c-muted">—</span>'}</td>
    <td class="gpu">${gpu}</td>
    <td class="mono">${m.stake_flop > 0 ? fmtNum(m.stake_flop) : '<span class="c-muted">—</span>'}</td>
  </tr>`;
}

/* ---- board ---- */
const PAGE = 50;
let SHOWN = PAGE;

function renderBoard() {
  if (!DATA) return;
  const ms = sorted(filtered());
  if (!ms.length) {
    $('board').innerHTML = `<div class="empty compact"><h2>No miners match.</h2><p>${
      DATA.prices_known === false && SHOW === 'priced' ? 'The FLOP gateway is not answering, so no miner has a live price. Choose <b>All</b>.' : 'Change the filters.'}</p></div>`;
    return;
  }
  const head = COLS.map(c => {
    const on = SORT.key === c.key;
    const arrow = on ? (SORT.dir < 0 ? ' ↓' : ' ↑') : '';
    const lab = `${c.label}${arrow}`;
    return c.sort
      ? `<th class="${on ? 'on' : ''}"><button class="th" data-sort="${c.key}" type="button">${lab}</button>${c.tip ? TIPS[c.tip] : ''}</th>`
      : `<th>${lab}</th>`;
  }).join('');
  const body = ms.slice(0, SHOWN).map(row).join('');
  const more = ms.length > SHOWN
    ? `<button class="btn small ghost more" id="moreBtn" type="button">Show ${Math.min(PAGE, ms.length - SHOWN)} more of ${ms.length - SHOWN}</button>` : '';
  $('board').innerHTML = `<div class="table-wrap board-table"><table class="ltable mtable">
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
    <div class="board-foot"><span class="c-muted">${ms.length} miners${F_ADDR ? ' matching' : ''}</span>${more}
      <span class="legend"><i class="sd on"></i>offering now <i class="sd mid"></i>priced, expired <i class="sd"></i>registered</span></div>`;

  $('board').querySelectorAll('button.th').forEach(b => b.onclick = () => {
    const k = b.dataset.sort;
    const col = COLS.find(c => c.key === k);
    SORT = SORT.key === k ? { key: k, dir: -SORT.dir } : { key: k, dir: col?.asc ? 1 : -1 };
    renderBoard();
  });
  $('board').querySelectorAll('button.copy').forEach(b => b.onclick = () =>
    navigator.clipboard.writeText(b.dataset.addr).then(() => toast('Address copied')));
  const mb = $('moreBtn');
  if (mb) mb.onclick = () => { SHOWN += PAGE; renderBoard(); };
}

/* ---- movers ---- */
function renderMovers() {
  const el = $('movers');
  const ms = DATA.miners.filter(m => (h24(m)?.sessions || 0) >= 3 && change24(m) != null && Math.abs(change24(m)) >= 0.005);
  $('tMovers').textContent = ms.length || '';
  if (!ms.length) {
    el.innerHTML = '<div class="empty compact"><h2>No movers.</h2><p>No miner with 3+ sessions changed its settle rate by more than half a point against the previous 24 hours.</p></div>';
    return;
  }
  const up = [...ms].filter(m => change24(m) > 0).sort((a, b) => change24(b) - change24(a)).slice(0, 10);
  const down = [...ms].filter(m => change24(m) < 0).sort((a, b) => change24(a) - change24(b)).slice(0, 10);
  const t = (title, list) => `<div class="mv"><h3>${title}</h3><div class="table-wrap"><table class="ltable">
    <thead><tr><th>Miner</th><th>Region</th><th>Settled 24h</th><th>Previous 24h</th><th>Δ</th><th>Sessions</th></tr></thead>
    <tbody>${list.length ? list.map(m => `<tr>
      <td><a class="mono" href="${EXPLORER}/account/${esc(m.address)}" target="_blank" rel="noopener">${esc(short(m.address))}</a></td>
      <td>${regionCell(m)}</td>
      <td>${rateCell(m.history_24h.settle_rate)}</td>
      <td>${rateCell(m.history_24h.previous_settle_rate)}</td>
      <td>${changeCell(m)}</td>
      <td class="mono">${m.history_24h.sessions}</td></tr>`).join('') : '<tr><td colspan="6" class="c-muted">None.</td></tr>'}</tbody>
    </table></div></div>`;
  el.innerHTML = `<div class="mv-grid">${t('Improving', up)}${t('Slipping', down)}</div>`;
}

/* ---- regions ---- */
function renderRegions() {
  const el = $('regions');
  const rows = DATA.network.country_detail || [];
  $('tRegions').textContent = rows.length || '';
  if (!rows.length) {
    el.innerHTML = '<div class="empty compact"><h2>No region data.</h2><p>The FLOP gateway did not report datacenter countries.</p></div>';
    return;
  }
  rows.sort((a, b) => b.priced_miners - a.priced_miners || a.country.localeCompare(b.country));
  el.innerHTML = `<div class="table-wrap"><table class="ltable">
    <thead><tr><th>Country</th><th>Models served</th><th>Priced miners on those models</th><th>Located here ${REGION_TIP}</th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td>${FLAG(r.country)} ${esc(cname(r.country))} <span class="mono c-muted">${esc(r.country)}</span></td>
      <td>${r.models.map(esc).join(', ')}</td>
      <td class="mono">${r.priced_miners}</td>
      <td class="mono">${r.exact_miners || '<span class="c-muted">—</span>'}</td></tr>`).join('')}</tbody></table></div>`;
}

/* ---- models ---- */
function renderModels() {
  const el = $('models');
  const ms = DATA.models || [];
  $('tModels').textContent = ms.length || '';
  if (!ms.length) {
    el.innerHTML = '<div class="empty compact"><h2>No model list.</h2><p>The FLOP gateway did not answer.</p></div>';
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table class="ltable">
    <thead><tr><th>Model</th><th>Priced miners</th><th>Offering now</th><th>Capacity, tok/min</th><th>Price, FLOP/token</th><th>Countries</th></tr></thead>
    <tbody>${ms.map(m => `<tr>
      <td>${esc(m.name)}</td>
      <td class="mono">${m.miners}</td>
      <td class="mono">${m.active_miners}</td>
      <td class="mono">${fmtTpm(m.capacity_tpm)}</td>
      <td class="mono">${m.price_prompt ?? '—'}</td>
      <td>${(m.countries || []).map(c => `<span title="${esc(cname(c))}">${FLAG(c)}</span>`).join(' ') || '—'}</td></tr>`).join('')}</tbody></table></div>`;
}

/* ---- served via Scout ---- */
function renderLeader() {
  const el = $('leader');
  const top = DATA.miners.filter(m => m.calls > 0).sort((a, b) => b.calls - a.calls).slice(0, 25);
  if (!top.length) {
    el.innerHTML = '<div class="empty compact"><h2>No requests through Scout yet.</h2><p>Scout\'s own requests and probes resume when the FLOP testnet opens.</p></div>';
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table class="ltable">
    <thead><tr><th>Miner</th><th>Requests</th><th>Success</th><th>Avg latency</th><th>FLOP spent</th></tr></thead>
    <tbody>${top.map(m => `<tr>
      <td><a class="mono" href="${EXPLORER}/account/${esc(m.address)}" target="_blank" rel="noopener">${esc(short(m.address))}</a></td>
      <td class="mono">${m.calls}</td><td>${rateCell(m.success_rate)}</td>
      <td class="mono">${m.avg_latency_s != null ? m.avg_latency_s + ' s' : '—'}</td>
      <td class="mono">${(m.flop_spent || 0).toFixed(2)}</td></tr>`).join('')}</tbody></table></div>`;
}

/* ---- KPIs ---- */
function renderKpis() {
  const n = DATA.network;
  const s = n.sessions_24h || {};
  $('kMiners').textContent = n.total_miners ?? '—';
  $('kPriced').textContent = n.priced_miners ?? '—';
  $('kSessions').textContent = s.sessions != null ? fmtNum(s.sessions) : '—';
  $('kSettle').textContent = pct(s.settle_rate);
  $('kSettleInd').textContent = s.independent_settle_rate != null ? `${pct(s.independent_settle_rate)} independent` : '';
  const d = (s.dominant_agents || [])[0];
  $('kDomTip').innerHTML = d ? `right now that is one wallet, <span class="mono">${esc(short(d.agent))}</span>, with ${pct(d.share_of_sessions)} of the sessions` : 'none do right now';
  $('kCap').textContent = fmtTpm(n.capacity_tpm);
  $('kCountries').textContent = (n.countries || []).length || '—';
  $('tMiners').textContent = DATA.miners.length;
}

/* ---- filters ---- */
function buildFilters() {
  const ms = $('modelFilter');
  ms.innerHTML = '<option value="">All</option>' + DATA.models.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  ms.value = F_MODEL;
  const cs = $('countryFilter');
  const cc = (DATA.network.countries || []).slice().sort((a, b) => cname(a).localeCompare(cname(b)));
  cs.innerHTML = '<option value="">All</option>' + cc.map(c => `<option value="${esc(c)}">${FLAG(c)} ${esc(cname(c))}</option>`).join('');
  cs.value = F_COUNTRY;
  const all = DATA.miners;
  $('cntAll').textContent = all.length;
  $('cntPriced').textContent = all.filter(m => m.priced).length;
  $('cntSessions').textContent = all.filter(m => h24(m)?.sessions).length;
}

/* ---- network status ---- */
async function renderNet() {
  try {
    const h = await fetch('https://api.flop.network/health').then(r => r.json());
    $('netDot').classList.toggle('ok', h.status === 'ok');
    $('netName').textContent = `FLOP ${h.network?.network || ''} · ${h.status === 'ok' ? 'live' : 'degraded'}`;
  } catch { $('netName').textContent = 'FLOP network · status unavailable'; }
}

/* ---- tooltips ----
   Placed with position:fixed so a tooltip in the table header is not clipped by
   the table's scroll box, and kept inside the viewport on narrow screens. */
let OPEN_TIP = null;
let FLOAT = null;   // the open tooltip's copy, attached to <body> so no stacking context can hide it
function hideTip() { if (FLOAT) { FLOAT.remove(); FLOAT = null; } OPEN_TIP = null; }
function placeTip(t) {
  const src = t.querySelector('.tip-box');
  if (!src) return;
  if (!FLOAT || OPEN_TIP !== t) {
    if (FLOAT) FLOAT.remove();
    FLOAT = src.cloneNode(true);
    FLOAT.classList.add('tip-float');
    document.body.appendChild(FLOAT);
  }
  const box = FLOAT;
  const r = t.getBoundingClientRect();
  const w = Math.min(320, window.innerWidth - 24);
  Object.assign(box.style, { position: 'fixed', width: w + 'px', transform: 'none', right: 'auto' });
  box.style.left = Math.max(12, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 12)) + 'px';
  const h = box.offsetHeight || 170;
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);
  box.style.top = top + 'px';
}
document.addEventListener('mouseover', e => {
  const t = e.target.closest?.('.tip');
  if (t && t !== OPEN_TIP) { placeTip(t); OPEN_TIP = t; }
  if (!t && OPEN_TIP) hideTip();
});
document.addEventListener('focusin', e => { const t = e.target.closest?.('.tip'); if (t) { placeTip(t); OPEN_TIP = t; } });
document.addEventListener('focusout', e => { if (e.target.closest?.('.tip')) hideTip(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });
window.addEventListener('scroll', () => { if (OPEN_TIP) placeTip(OPEN_TIP); }, true);

/* ---- tabs ---- */
function openTab(name) {
  document.querySelectorAll('.tab').forEach(t => {
    const on = t.dataset.tab === name;
    t.classList.toggle('on', on);
    t.setAttribute('aria-selected', on);
  });
  document.querySelectorAll('.panel').forEach(p => {
    const on = p.id === 'p-' + name;
    p.classList.toggle('on', on);
    p.hidden = !on;
  });
  const u = new URL(location.href);
  if (name === 'board') u.searchParams.delete('tab'); else u.searchParams.set('tab', name);
  history.replaceState(null, '', u);
}
document.querySelectorAll('.tab').forEach(t => t.onclick = () => openTab(t.dataset.tab));

/* ---- wiring ---- */
document.querySelectorAll('#showChips .chip').forEach(ch => ch.onclick = () => {
  document.querySelectorAll('#showChips .chip').forEach(x => x.classList.remove('on'));
  ch.classList.add('on');
  SHOW = ch.dataset.show;
  SHOWN = PAGE;
  renderBoard();
});
$('modelFilter').onchange = e => { F_MODEL = e.target.value; SHOWN = PAGE; renderBoard(); };
$('countryFilter').onchange = e => { F_COUNTRY = e.target.value; SHOWN = PAGE; renderBoard(); };
$('addrFilter').oninput = e => { F_ADDR = e.target.value.trim(); SHOWN = PAGE; renderBoard(); };

/* ---- load ---- */
async function load() {
  try {
    const r = await fetch('/api/miners');
    if (!r.ok) throw new Error('miners endpoint ' + r.status);
    DATA = await r.json();
    $('updated').textContent = 'updated ' + new Date().toLocaleTimeString('en-GB');
    const q = new URLSearchParams(location.search);
    if (q.get('m')) { F_ADDR = q.get('m'); $('addrFilter').value = F_ADDR; }
    buildFilters();
    renderKpis();
    renderBoard();
    renderMovers();
    renderRegions();
    renderModels();
    renderLeader();
    renderNet();
    if (q.get('tab')) openTab(q.get('tab'));
  } catch (e) {
    if (!DATA) $('board').innerHTML = `<div class="empty compact"><h2>Could not load miners.</h2><p>${esc(e.message)}</p></div>`;
    else $('updated').textContent = 'refresh failed, showing last data';
  }
}

load();
setInterval(load, 120000);
