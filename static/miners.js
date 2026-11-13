/* Flop Scout · Miners explorer  v2 */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const short = a => a ? a.slice(0, 6) + '…' + a.slice(-4) : '—';
const fmtTpm = t => t == null ? '—' : t >= 1_000_000 ? (t / 1_000_000).toFixed(1) + 'M' : t >= 1_000 ? (t / 1_000).toFixed(0) + 'k' : String(t);
const fmtFlop = v => v ? (v >= 1_000_000 ? (v / 1_000_000).toFixed(1) + 'M' : v >= 1_000 ? (v / 1_000).toFixed(1) + 'k' : v.toFixed(1)) : '0';
const toast = (msg, ms = 3200) => { const t = $('toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), ms); };
const FLAG = cc => cc ? String.fromCodePoint(...[...cc.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : '';
const LS = 'flopScoutAgent.v1';
let agent = JSON.parse(localStorage.getItem(LS) || 'null');

let DATA = null;
let SORT = 'performance';
let F_MODEL = '';
let F_SHOW = 'priced'; // 'all' | 'priced' | 'active'

/* ---- performance score 0-1 (only for priced miners with actual data, fallback if no router_score) ---- */
function score(m) {
  if (m.router_score != null) return m.router_score;
  const success = m.success_rate ?? 0;
  const latPts  = m.avg_latency_s ? Math.max(0, 1 - m.avg_latency_s / 120) : 0;
  const pricePts = m.best_price ? Math.max(0, 1 - parseFloat(m.best_price) / 0.01) : 0;
  const actPts  = m.calls ? Math.min(1, m.calls / 50) : 0;
  const calPts  = m.cal_phase === 'Active' ? 0.2 : 0;
  return (success * 0.4 + latPts * 0.2 + pricePts * 0.1 + actPts * 0.15 + calPts * 0.15);
}

/* ---- miner card ---- */
function gpuTag(m) {
  if (!m.gpus?.length) return '<span class="c-muted">—</span>';
  return m.gpus.map(g => `<span class="gpu-tag">${esc(g.name || '?')} ×${g.count} · ${g.mem_gb}GB</span>`).join('');
}

function calBadge(m) {
  if (!m.cal_phase) return '<span class="kind grey">Not calibrated</span>';
  const cls = m.cal_phase === 'Active' ? '' : 'grey';
  const tier = m.cal_tier ? ` · ${m.cal_tier}` : '';
  return `<span class="kind ${cls}">${esc(m.cal_phase)}${esc(tier)}</span>`;
}

function minerCard(m, rank) {
  const bar = m.priced ? Math.round(score(m) * 100) : 0;
  const priceTxt = m.best_price ? `<span class="mono">${m.best_price} FLOP/G<sub>n</sub></span>` : '';
  const routerBadge = m.router_rank === 1
    ? '<span class="kind router-best" title="Highest router score — best miner right now">⭐ Best miner</span>'
    : m.router_score != null && m.router_rank && m.router_rank <= 5
      ? `<span class="kind router-top" title="Router score ${(m.router_score*100).toFixed(0)}%">▲ Top ${m.router_rank}</span>`
      : '';
  const routerScoreLine = m.router_score != null
    ? `<span class="mlabel">Router</span><span class="mono">${(m.router_score*100).toFixed(0)}% score</span>
       ${m.router_rank ? `<span class="c-muted mono" style="font-size:11px">rank #${m.router_rank}</span>` : ''}` : '';

  const pf = m.calls
    ? `<span>${m.calls} req</span><span class="dot-sep">·</span>
       <span class="${m.success_rate >= 0.9 ? 'c-ok' : m.success_rate >= 0.7 ? 'c-warn' : 'c-err'}">${((m.success_rate ?? 0) * 100).toFixed(0)}% ok</span>
       <span class="dot-sep">·</span><span>${m.avg_latency_s ?? '—'}s avg</span>`
    : '<span class="c-muted">No receipts via Scout yet</span>';

  const stake = m.stake_flop > 0 ? `<span class="mono">${fmtFlop(m.stake_flop)} staked</span>` : '';
  const capLine = m.cap_gflops ? `<span class="mono">${m.cap_gflops} GFlop/s</span>` : '';
  const verLine = m.worker_version ? `<span class="mono">v${esc(m.worker_version)}</span>` : '';

  const statusBadge = m.active
    ? '<span class="kind">● Priced &amp; active</span>'
    : m.priced
      ? '<span class="kind grey">○ Priced, expired</span>'
      : m.cal_phase === 'Active'
        ? '<span class="kind grey">Calibrated</span>'
        : '<span class="kind grey">Registered</span>';

  return `<article class="card miner-card ${m.active ? 'miner-live' : ''} ${m.router_rank === 1 ? 'miner-best' : ''}" role="listitem">
  <div class="miner-top">
    <span class="miner-rank">#${rank}</span>
    ${statusBadge}
    ${routerBadge}
    ${calBadge(m)}
    <span class="miner-addr mono">${esc(short(m.address))}</span>
    <button class="btn small ghost miner-copy" data-addr="${esc(m.address)}" aria-label="Copy miner address">copy</button>
    ${priceTxt ? `<span style="margin-left:auto">${priceTxt}</span>` : ''}
  </div>
  ${m.priced ? `<div class="miner-bar-wrap" title="Performance score ${bar}%"><div class="miner-bar" style="width:${bar}%"></div></div>` : ''}
  <div class="miner-meta">
    <div class="miner-col"><span class="mlabel">GPU</span><span>${gpuTag(m)}</span></div>
    <div class="miner-col"><span class="mlabel">Compute</span><span>${capLine || '<span class="c-muted">—</span>'}</span></div>
    <div class="miner-col"><span class="mlabel">Stake</span><span>${stake || '<span class="c-muted">—</span>'}</span></div>
    <div class="miner-col"><span class="mlabel">Via Scout</span><div class="miner-pf">${pf}</div></div>
    ${routerScoreLine ? `<div class="miner-col">${routerScoreLine}</div>` : ''}
  </div>
  <div class="miner-foot">
    ${verLine}
    ${m.strikes ? `<span class="c-warn">⚠ ${m.strikes} strikes</span>` : ''}
    ${m.capacity_tpm ? `<span class="mono c-muted">${fmtTpm(m.capacity_tpm)} tok/min</span>` : ''}
    <a class="mono c-teal" href="https://explorer.devnet.flop.network/account/${esc(m.address)}" target="_blank" rel="noopener">explorer ↗</a>
  </div>
</article>`;
}

/* ---- main board ---- */
function render() {
  if (!DATA) return;
  let miners = [...DATA.miners];
  if (F_SHOW === 'priced')  miners = miners.filter(m => m.priced);
  if (F_SHOW === 'active')  miners = miners.filter(m => m.active);
  if (F_MODEL) miners = miners.filter(m => (m.models || []).includes(F_MODEL));

  miners.sort((a, b) => {
    if (SORT === 'router')   return score(b) - score(a); // router_score with fallback
    if (SORT === 'price')    return (parseFloat(a.best_price) || 9) - (parseFloat(b.best_price) || 9);
    if (SORT === 'capacity') return (b.cap_gflops || 0) - (a.cap_gflops || 0);
    if (SORT === 'stake')    return (b.stake_flop || 0) - (a.stake_flop || 0);
    if (SORT === 'activity') return (b.calls || 0) - (a.calls || 0);
    return score(b) - score(a); // performance default = router score
  });

  if (!miners.length) {
    $('board').innerHTML = `<div class="empty"><h2>No miners match.</h2><p>Try a different filter.</p></div>`;
    return;
  }

  const activeCnt = miners.filter(m => m.active).length;
  const pricedCnt = miners.filter(m => m.priced).length;
  $('board').innerHTML = `
    <div class="board-head">
      <h3>${miners.length} miners shown · ${pricedCnt} priced · ${activeCnt} active now</h3>
    </div>
    <div role="list">${miners.map((m, i) => minerCard(m, i + 1)).join('')}</div>`;

  document.querySelectorAll('.miner-copy').forEach(btn => {
    btn.onclick = () => navigator.clipboard.writeText(btn.dataset.addr).then(() => toast('Address copied'));
  });
}

/* ---- leaderboard ---- */
function renderLeader() {
  if (!DATA) return;
  const top = DATA.miners.filter(m => m.calls > 0).sort((a, b) => (b.calls || 0) - (a.calls || 0)).slice(0, 15);
  if (!top.length) {
    $('leader').innerHTML = `<p class="note">No receipts recorded via Scout yet. Ask a question on the <a href="./">Answer page</a> to generate your first receipt.</p>`;
    return;
  }
  $('leader').innerHTML = `<div class="table-wrap"><table class="ltable">
    <thead><tr><th>#</th><th>Miner</th><th>Requests</th><th>Success</th><th>Avg latency</th><th>FLOP spent</th><th>GPU</th></tr></thead>
    <tbody>${top.map((m, i) => `<tr class="${m.active ? 'tr-active' : ''}">
      <td class="mono c-teal">#${i + 1}</td>
      <td><a class="mono" href="https://explorer.devnet.flop.network/account/${esc(m.address)}" target="_blank" rel="noopener">${esc(short(m.address))}</a></td>
      <td class="mono">${m.calls}</td>
      <td class="mono ${m.success_rate >= 0.9 ? 'c-ok' : 'c-warn'}">${m.success_rate != null ? (m.success_rate * 100).toFixed(0) + '%' : '—'}</td>
      <td class="mono">${m.avg_latency_s != null ? m.avg_latency_s + 's' : '—'}</td>
      <td class="mono">${(m.flop_spent || 0).toFixed(2)}</td>
      <td class="mono" style="font-size:11px">${(m.gpus?.[0]?.name || '—').replace('NVIDIA ','')}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

/* ---- country grid ---- */
function renderCountries() {
  if (!DATA) return;
  const countries = DATA.network.countries || [];
  if (!countries.length) { $('countryGrid').innerHTML = '<p class="note">Country data not available.</p>'; return; }
  $('countryGrid').innerHTML = `<div class="cg">${countries.map(cc => `
    <div class="cc-cell"><span class="cc-flag">${FLAG(cc)}</span><span class="mono">${esc(cc)}</span></div>`).join('')}</div>`;
}

/* ---- router sidebar ---- */
function renderRouter(data) {
  const el = $('routerBox');
  if (!el) return;
  const snap = data.router_snap;
  if (!snap) { el.innerHTML = '<p class="note">No router data yet.</p>'; return; }
  const best = snap.best;
  const top = snap.top || [];
  const rows = top.slice(0, 8).map((m, i) => {
    const rate = m.chain_rate != null ? (m.chain_rate * 100).toFixed(0) + '%' : 'new';
    const sc = (m.score * 100).toFixed(0);
    const cls = m.score >= 0.5 ? 'c-ok' : m.score >= 0.3 ? 'c-warn' : 'c-err';
    return `<tr><td class="mono" style="font-size:11px">#${i+1} ${esc(m.address.slice(0,8))}…</td>
      <td class="mono ${cls}">${sc}%</td>
      <td class="mono" style="font-size:11px">${rate}</td></tr>`;
  }).join('');
  const bestAddr = best?.address ? best.address.slice(0,8) + '…' + best.address.slice(-4) : 'none';
  el.innerHTML = `
    <div style="margin-bottom:8px">
      <span class="mlabel">Best miner now</span>
      <span class="mono" style="font-size:12px">${bestAddr}</span>
      ${best?.score != null ? `<span class="c-ok mono"> ${(best.score*100).toFixed(0)}%</span>` : ''}
    </div>
    <div style="margin-bottom:6px">
      <span class="mlabel">Active priced</span>
      <span class="mono">${snap.active_priced ?? '—'}</span>
      <span class="c-muted mono" style="font-size:11px"> / ${snap.chain_miners ?? '—'} known</span>
    </div>
    <table class="ltable" style="font-size:12px;width:100%">
      <thead><tr><th>Miner</th><th>Score</th><th>Chain %</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <p class="note" style="margin-top:6px">Score = 55% on-chain settle rate + 35% local latency/success + 10% active in gateway. Updates every 2 min.</p>`;
}

/* ---- sidebar ---- */
function renderSide() {
  if (!DATA) return;
  const n = DATA.network;
  $('stMiners').textContent = n.total_miners;
  $('stActive').textContent = n.active_priced;
  $('stPriced').textContent = n.priced_miners;
  $('stCap').textContent = fmtTpm(n.capacity_tpm);
  $('modelList').innerHTML = DATA.models.map(m =>
    `<label><input type="checkbox" disabled checked>${esc(m.name)}
     <small>${m.active_miners}/${m.miners} priced · ${fmtTpm(m.capacity_tpm)} tok/min</small></label>`).join('');
}

/* ---- network strip ---- */
async function renderNet() {
  try {
    const h = await fetch('https://api.flop.network/health').then(r => r.json());
    const ok = h.status === 'ok';
    $('netDot').classList.toggle('ok', ok);
    $('netName').textContent = `FLOP ${h.network?.network || ''}`.trim();
    const n = DATA?.network;
    $('netNote').textContent = `${ok ? 'live' : 'degraded'} · ${n ? `${n.total_miners} registered · ${n.priced_miners} priced · ${fmtTpm(n.capacity_tpm)} tok/min · devnet activity earns nothing` : ''}`;
  } catch { $('netNote').textContent = 'status unavailable'; }
}

/* ---- filters ---- */
function buildFilters() {
  if (!DATA) return;
  const ms = $('modelFilter');
  ms.innerHTML = '<option value="">All models</option>' +
    DATA.models.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  // update chip counters
  const all = DATA.miners;
  const ca = $('cntAll'); if (ca) ca.textContent = all.length;
  const cp = $('cntPriced'); if (cp) cp.textContent = all.filter(m => m.priced).length;
  const cac = $('cntActive'); if (cac) cac.textContent = all.filter(m => m.active).length;
}

/* ---- fetch ---- */
async function load() {
  try {
    const [minersR, routerR] = await Promise.all([
      fetch('api/miners'),
      fetch('api/router').catch(() => null)
    ]);
    if (!minersR.ok) throw new Error(minersR.status);
    DATA = await minersR.json();
    if (routerR?.ok) {
      DATA.router_snap = await routerR.json();
    }
    $('updated').textContent = 'updated ' + new Date().toLocaleTimeString('en-GB');
    buildFilters();
    render();
    renderSide();
    renderLeader();
    renderCountries();
    renderNet();
    renderRouter(DATA);
  } catch (e) {
    if (!DATA) $('board').innerHTML = `<div class="empty"><h2>Could not load miners.</h2><p>${esc(e.message)}</p></div>`;
  }
}

/* ---- sort & filter wiring ---- */
document.querySelectorAll('#sortChips .chip').forEach(ch => {
  ch.onclick = () => {
    document.querySelectorAll('#sortChips .chip').forEach(x => x.classList.remove('on'));
    ch.classList.add('on');
    SORT = ch.dataset.sort;
    render();
  };
});
document.querySelectorAll('#showChips .chip').forEach(ch => {
  ch.onclick = () => {
    document.querySelectorAll('#showChips .chip').forEach(x => x.classList.remove('on'));
    ch.classList.add('on');
    F_SHOW = ch.dataset.show;
    render();
  };
});
$('modelFilter').onchange = e => { F_MODEL = e.target.value; render(); };

/* ---- wallet pill ---- */
function renderWalletPill() {
  const btn = $('walletBtn');
  btn.classList.toggle('on', !!agent);
  btn.querySelector('span').textContent = agent
    ? agent.address.slice(0, 6) + '…' + agent.address.slice(-4)
    : 'Connect agent wallet';
}
$('walletBtn').onclick = () => toast('Open the Answer page to manage your wallet.');
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close());
renderWalletPill();

load();
setInterval(load, 60_000);
