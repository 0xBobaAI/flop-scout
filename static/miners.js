/* Flop Scout — Miners page.
   Depends on agent.js for $, esc, short, toast, agent and the wallet pill. */

const fmtTpm = t => t == null ? '—' : t >= 1_000_000 ? (t / 1_000_000).toFixed(1) + 'M' : t >= 1_000 ? (t / 1_000).toFixed(0) + 'k' : String(t);
const fmtFlop = v => v ? (v >= 1_000_000 ? (v / 1_000_000).toFixed(1) + 'M' : v >= 1_000 ? (v / 1_000).toFixed(1) + 'k' : v.toFixed(1)) : '0';
const FLAG = cc => cc ? String.fromCodePoint(...[...cc.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : '';

let DATA = null;
let SORT = 'router';
let F_MODEL = '';
let F_ADDR = '';
let F_SHOW = 'priced'; // 'all' | 'priced' | 'active' | 'movers'

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

function hasHistory(m) { return m.history_24h && m.history_24h.sessions > 0; }

/* A tiny inline trend line: no axes, no labels, just shape. Height is fixed so
   a flat 60% line and a flat 95% line still read as "flat", not as noise. */
function sparkline(points, { width = 64, height = 20, stroke = 'var(--teal)' } = {}) {
  if (!points || points.length < 2) return '';
  const lo = Math.min(...points), hi = Math.max(...points);
  // Centre a narrow series instead of blowing it up: 30%->31% is not a cliff, and
  // stretching it to the full height would say it was.
  const span = Math.max(hi - lo, 0.15);
  const mid = (hi + lo) / 2, top = mid + span / 2, bottom = mid - span / 2;
  const x = i => (i / (points.length - 1)) * (width - 6) + 3;
  const y = v => height - 3 - ((v - bottom) / span) * (height - 6);
  const d = points.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const lx = x(points.length - 1), ly = y(points[points.length - 1]);
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.1" fill="${stroke}"/>
  </svg>`;
}
// null change means "no prior sessions to compare against" — not zero, and
// certainly not +100%. Keep it distinct from 0.0.
function trend(m) {
  if (!hasHistory(m)) return null;
  const c = m.history_24h.settle_rate_change;
  return c == null ? undefined : c;
}

/* A trend chip: ▲ +0.4% green, ▼ -1.2% amber, flat = neutral. Direction is the
   whole point — a rate alone cannot distinguish "always been 94%" from
   "was 99% yesterday". */
function trendBadge(m) {
  if (!hasHistory(m)) return '<span class="kind grey" title="No finished sessions in the last 24 hours">no sessions 24h</span>';
  const d = trend(m);
  if (d === undefined || d === null) {
    return `<span class="kind grey" title="${m.history_24h.sessions} finished sessions in 24h, none in the 24h before">new · ${m.history_24h.sessions} sessions</span>`;
  }
  const pct = (d * 100);
  const cls = pct > 0.5 ? 'ok' : pct < -0.5 ? 'warn' : '';
  const arrow = pct > 0.5 ? '▲' : pct < -0.5 ? '▼' : '▬';
  const basis = m.history_24h.change_basis === 'independent' ? 'independent demand' : 'all demand';
  return `<span class="kind ${cls === 'ok' ? '' : cls === 'warn' ? 'or' : 'grey'}" title="${m.history_24h.settled} settled / ${m.history_24h.timeouts} timed out in 24h. Change vs the previous 24h, over ${basis}.">${arrow} ${pct >= 0 ? '+' : ''}${pct.toFixed(1)}% · 24h</span>`;
}

/* Settlement over demand Scout counts as independent: sessions from agent
   wallets below 25% of the day's total. On devnet one wallet opens most
   sessions and causes most timeouts, so this is the fairer read of a miner. */
function independentChip(m) {
  const h = m.history_24h;
  if (!h || !h.independent_sessions) return '';
  const r = h.independent_settle_rate;
  return `<span class="kind ${r >= 0.8 ? '' : 'grey'}" title="${h.independent_sessions} sessions from independent agents in 24h (${h.distinct_agents} agents in total)">${(r * 100).toFixed(0)}% independent · ${h.independent_sessions}</span>`;
}

/* ---- miner row ---- */
function minerCard(m, rank) {
  const bar = m.priced ? Math.round(score(m) * 100) : 0;
  const priceTxt = m.best_price ? `${m.best_price} FLOP/G<sub>n</sub>` : null;

  // One status word, not three overlapping badges. Router rank folds into it
  // instead of adding a second "top miner" chip next to the status chip.
  const statusTxt = m.router_rank === 1 ? '⭐ Best miner'
    : m.router_rank && m.router_rank <= 5 ? `Top ${m.router_rank}`
    : m.active ? 'Active' : m.priced ? 'Expired' : m.cal_phase === 'Active' ? 'Calibrated' : 'Registered';
  const statusCls = m.router_rank === 1 ? 'router-best' : m.active ? '' : 'grey';

  const trendChip = trendBadge(m);
  const spark = sparkline(m.settle_series, { stroke: m.history_24h?.settle_rate_change < -0.005 ? 'var(--orange)' : 'var(--teal)' });
  const gpu = m.gpus?.[0] ? `${esc(m.gpus[0].name || 'GPU')}${m.gpus.length > 1 ? ` +${m.gpus.length - 1}` : ''}` : null;

  // Via-Scout success only when Scout has actually routed to this miner.
  const viaScout = m.calls ? `${m.calls} via Scout · ${((m.success_rate ?? 0) * 100).toFixed(0)}% ok` : null;

  const facts = [
    priceTxt && `<span class="mf"><b>${priceTxt}</b><small>price</small></span>`,
    m.cap_gflops && `<span class="mf"><b>${m.cap_gflops}</b><small>GFlop/s</small></span>`,
    m.stake_flop > 0 && `<span class="mf"><b>${fmtFlop(m.stake_flop)}</b><small>staked</small></span>`,
    gpu && `<span class="mf"><b>${gpu}</b><small>GPU</small></span>`,
  ].filter(Boolean).join('');

  const foot = [
    viaScout,
    m.strikes && `<span class="c-warn">⚠ ${m.strikes} strikes</span>`,
    m.blacklisted && '<span class="c-err">blacklisted</span>',
    m.worker_version && `v${esc(m.worker_version)}`,
  ].filter(Boolean).join(' <span class="dot-sep">·</span> ');

  return `<article class="card miner-row hoverable ${m.router_rank === 1 ? 'miner-best' : ''}" role="listitem">
  <div class="mr-top">
    <span class="miner-rank">#${rank}</span>
    <span class="kind ${statusCls}">${statusTxt}</span>
    ${trendChip}
    ${independentChip(m)}
    ${spark}
    <span class="miner-addr mono" title="${esc(m.address)}">${esc(short(m.address))}</span>
    <button class="btn small ghost miner-copy" data-addr="${esc(m.address)}" aria-label="Copy miner address" type="button">copy</button>
    <a class="mono c-teal mr-explorer" href="https://explorer.devnet.flop.network/account/${esc(m.address)}" target="_blank" rel="noopener">explorer ↗</a>
  </div>
  ${m.priced ? `<div class="miner-bar-wrap" title="Performance score ${bar}%"><div class="miner-bar" style="width:${bar}%"></div></div>` : ''}
  <div class="mr-facts">${facts || '<span class="c-muted">No pricing data</span>'}</div>
  ${foot ? `<div class="mr-foot">${foot}</div>` : ''}
</article>`;
}

/* ---- main board ---- */
function render() {
  if (!DATA) return;
  let miners = [...DATA.miners];
  if (F_ADDR) miners = miners.filter(m => m.address === F_ADDR);
  else {
    if (F_SHOW === 'priced')  miners = miners.filter(m => m.priced);
    if (F_SHOW === 'active')  miners = miners.filter(m => m.active);
    if (F_SHOW === 'movers')  miners = miners.filter(m => hasHistory(m) && trend(m) !== undefined && Math.abs(trend(m)) > 0.005);
    if (F_MODEL) miners = miners.filter(m => (m.models || []).includes(F_MODEL));
  }

  miners.sort((a, b) => {
    if (SORT === 'router')   return score(b) - score(a); // router_score with fallback
    if (SORT === 'history')  return (trend(b) ?? -9) - (trend(a) ?? -9);  // null/undefined last
    if (SORT === 'price')    return (parseFloat(a.best_price) || 9) - (parseFloat(b.best_price) || 9);
    if (SORT === 'capacity') return (b.cap_gflops || 0) - (a.cap_gflops || 0);
    if (SORT === 'stake')    return (b.stake_flop || 0) - (a.stake_flop || 0);
    if (SORT === 'activity') return (b.calls || 0) - (a.calls || 0);
    return score(b) - score(a);
  });

  if (!miners.length) {
    const why = (F_SHOW !== 'all' && DATA.prices_known === false)
      ? 'The FLOP gateway is not answering, so there are no live prices to rank. Switch <b>Show</b> to <b>All</b> to see the registry anyway.'
      : 'Try a different filter. History-based views fill in as Scout takes more settlement snapshots.';
    $('board').innerHTML = `<div class="empty"><h2>No miners match.</h2><p>${why}</p></div>`;
    return;
  }

  const activeCnt = miners.filter(m => m.active).length;
  const pricedCnt = miners.filter(m => m.priced).length;
  // The top few are worth a full card; the rest read better as a dense table —
  // 20+ near-identical cards in a row was the thing that felt cluttered.
  const TOP_CARDS = 5;
  const top = miners.slice(0, TOP_CARDS);
  const rest = miners.slice(TOP_CARDS);
  $('board').innerHTML = `
    <div class="board-head">
      <h3>${miners.length} miners shown · ${pricedCnt} priced · ${activeCnt} active now</h3>
    </div>
    <div role="list">${top.map((m, i) => minerCard(m, i + 1)).join('')}</div>
    ${rest.length ? minerTable(rest, TOP_CARDS) : ''}`;

  document.querySelectorAll('.miner-copy').forEach(btn => {
    btn.onclick = () => navigator.clipboard.writeText(btn.dataset.addr).then(() => toast('Address copied'));
  });
}

/* The long tail: one line per miner, sortable columns already applied upstream. */
function minerTable(miners, offset) {
  const row = (m, i) => {
    const chip = trendBadge(m);
    const gpu = m.gpus?.[0] ? esc(m.gpus[0].name || 'GPU').replace('NVIDIA ', '') : '—';
    return `<tr>
      <td class="mono c-muted">#${offset + i + 1}</td>
      <td><span class="miner-addr mono" title="${esc(m.address)}">${esc(short(m.address))}</span></td>
      <td>${m.active ? '<span class="c-ok">active</span>' : m.priced ? '<span class="c-muted">expired</span>' : '<span class="c-muted">registered</span>'}</td>
      <td>${chip}</td>
      <td class="mono">${m.best_price ? m.best_price : '—'}</td>
      <td class="mono">${m.cap_gflops || '—'}</td>
      <td class="mono">${m.stake_flop > 0 ? fmtFlop(m.stake_flop) : '—'}</td>
      <td style="font-size:11px">${gpu}</td>
      <td><a class="mono c-teal" href="https://explorer.devnet.flop.network/account/${esc(m.address)}" target="_blank" rel="noopener">↗</a></td>
    </tr>`;
  };
  return `<div class="table-wrap" style="margin-top:10px"><table class="ltable">
    <thead><tr><th>#</th><th>Miner</th><th>Status</th><th>24h</th><th>Price</th><th>GFlop/s</th><th>Stake</th><th>GPU</th><th></th></tr></thead>
    <tbody>${miners.map(row).join('')}</tbody>
  </table></div>`;
}

/* ---- leaderboard ---- */
function renderLeader() {
  if (!DATA) return;
  const top = DATA.miners.filter(m => m.calls > 0).sort((a, b) => (b.calls || 0) - (a.calls || 0)).slice(0, 15);
  if (!top.length) {
    $('leader').innerHTML = `<p class="note">No Scout receipts yet. <a href="/answer">Ask a question</a> to create the first one.</p>`;
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

/* ---- movers: who improved, who degraded ---- */
function renderMovers() {
  const el = $('movers');
  if (!el || !DATA) return;
  const withHist = DATA.miners.filter(m => hasHistory(m) && m.history_24h.sessions >= 3 && trend(m) != null);
  if (withHist.length < 2) {
    el.innerHTML = `<div class="empty"><h2>Not enough history yet.</h2>
      <p>Scout rebuilds settlement from the explorer's session record. Once two 24-hour windows are in, this ranks miners by how their settle rate moved.</p></div>`;
    return;
  }
  const sorted = [...withHist].sort((a, b) => (trend(b) ?? 0) - (trend(a) ?? 0));
  const up = sorted.slice(0, 6);
  const down = sorted.slice(-6).reverse().filter(m => trend(m) < 0);
  const rowT = (m, i) => {
    const d = trend(m) * 100;
    const cls = d > 0.5 ? 'c-ok' : d < -0.5 ? 'c-err' : 'c-muted';
    return `<tr>
      <td class="mono">#${i + 1}</td>
      <td><a class="mono" href="/?m=${esc(m.address)}">${esc(short(m.address))}</a></td>
      <td class="mono">${(m.history_24h.settle_rate * 100).toFixed(1)}%</td>
      <td class="mono ${cls}">${d >= 0 ? '+' : ''}${d.toFixed(1)} pp</td>
      <td class="mono">${m.history_24h.sessions}</td>
      <td class="mono" style="font-size:11px">${(m.gpus?.[0]?.name || '—').replace('NVIDIA ','')}</td>
    </tr>`;
  };
  const table = (title, rows, hint) => `<div style="flex:1;min-width:280px">
    <h3 style="margin-bottom:9px">${title}</h3>
    <div class="table-wrap"><table class="ltable">
      <thead><tr><th>#</th><th>Miner</th><th>Settle rate</th><th>24h change</th><th>Sessions</th><th>GPU</th></tr></thead>
      <tbody>${rows.length ? rows.map(rowT).join('') : `<tr><td colspan="6" class="c-muted">${hint}</td></tr>`}</tbody>
    </table></div></div>`;
  el.innerHTML = `<div style="display:flex;gap:16px;flex-wrap:wrap">
    ${table('Rising', up, 'Nothing improved measurably.')}
    ${table('Falling', down, 'Nothing degraded measurably.')}
  </div>`;
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
  const bestAddr = best?.address ? best.address.slice(0,8) + '…' + best.address.slice(-4) : (snap.prices_known === false ? 'unknown' : 'none');
  el.innerHTML = `
    <div style="margin-bottom:8px">
      <span class="mlabel">Best miner now</span>
      <span class="mono" style="font-size:12px">${bestAddr}</span>
      ${best?.score != null ? `<span class="c-ok mono"> ${(best.score*100).toFixed(0)}%</span>` : ''}
    </div>
    <div style="margin-bottom:8px">
      <span class="mlabel">Chain sessions tracked</span>
      <span class="mono">${snap.chain_miners ?? '—'}</span>
      <span class="c-muted mono" style="font-size:11px"> miners with settlement history</span>
    </div>
    <div class="table-wrap" style="border:0;box-shadow:none">
      <table class="ltable" style="font-size:12px;width:100%;min-width:0">
        <thead><tr><th>Miner</th><th>Score</th><th>Chain %</th></tr></thead>
        <tbody>${rows}</tbody></table>
    </div>
    <p class="note" style="margin-top:8px">Score = 55% on-chain settle rate + 35% local latency/success + 10% active in gateway. Updates every 2 min.</p>`;
}

/* ---- sidebar ---- */
function renderSide() {
  if (!DATA) return;
  const n = DATA.network;
  // null means "gateway unreachable", which is not the same as "nothing priced".
  const num = v => (v == null ? '—' : v);
  $('stMiners').textContent = num(n.total_miners);
  $('stActive').textContent = num(n.active_priced);
  $('stPriced').textContent = num(n.priced_miners);
  $('stCap').textContent = n.capacity_tpm == null ? '—' : fmtTpm(n.capacity_tpm);
  if (DATA.prices_known === false) {
    const note = $('priceNote');
    if (note) {
      note.textContent = 'Live prices unavailable right now — the FLOP gateway did not answer. Numbers above are unknown, not zero.';
      note.hidden = false;
    }
  }
  $('modelList').innerHTML = DATA.models.length ? DATA.models.map(m =>
    `<label><input type="checkbox" disabled checked>${esc(m.name)}
     <small>${m.active_miners}/${m.miners} priced · ${fmtTpm(m.capacity_tpm)} tok/min</small></label>`).join('')
    : '<p class="note">No live model list — the gateway did not answer.</p>';

  const ns = n.settle_series;
  const row = $('netSparkRow');
  if (row) {
    row.hidden = !ns || ns.length < 2;
    if (ns && ns.length >= 2) {
      $('netSpark').innerHTML = sparkline(ns.map(p => p.settle_rate), { width: 110, height: 24 });
      const sv = $('netSparkVal');
      const s24 = n.sessions_24h;
      if (sv) sv.textContent = s24 && s24.settle_rate != null
        ? `${(s24.settle_rate * 100).toFixed(0)}% all · ${s24.independent_settle_rate != null ? (s24.independent_settle_rate * 100).toFixed(0) + '% independent' : 'no independent demand'} · 24h`
        : `${(ns[ns.length - 1].settle_rate * 100).toFixed(0)}% last interval`;
      const dn = $('domNote');
      if (dn && s24 && s24.dominant_agents && s24.dominant_agents.length) {
        const d = s24.dominant_agents[0];
        dn.innerHTML = `One agent wallet, <span class="mono">${esc(short(d.agent))}</span>, opened ${(d.share_of_sessions * 100).toFixed(0)}% of the last 24h's sessions. Rates marked "independent" leave it out.`;
        dn.hidden = false;
      }
    }
  }
}

/* ---- network strip ---- */
async function renderNet() {
  try {
    const h = await fetch('https://api.flop.network/health').then(r => r.json());
    const ok = h.status === 'ok';
    $('netDot').classList.toggle('ok', ok);
    $('netName').textContent = `FLOP ${h.network?.network || ''}`.trim();
    const n = DATA?.network;
    const stale = DATA?.stale ? ` · snapshot ${DATA.age_s}s old, refreshing` : '';
    $('netNote').textContent = `${ok ? 'live' : 'degraded'} · ${n ? `${n.total_miners} registered · ${n.priced_miners} priced · ${fmtTpm(n.capacity_tpm)} tok/min · devnet activity earns nothing` : ''}${stale}`;
  } catch { $('netNote').textContent = 'status unavailable'; }
}

/* ---- filters ---- */
function buildFilters() {
  if (!DATA) return;
  F_ADDR = new URLSearchParams(location.search).get('m') || '';
  const ms = $('modelFilter');
  ms.innerHTML = '<option value="">All models</option>' +
    DATA.models.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  const all = DATA.miners;
  const ca = $('cntAll'); if (ca) ca.textContent = all.length;
  const cp = $('cntPriced'); if (cp) cp.textContent = all.filter(m => m.priced).length;
  const cac = $('cntActive'); if (cac) cac.textContent = all.filter(m => m.active).length;
  const cm = $('cntMovers'); if (cm) cm.textContent = all.filter(m => hasHistory(m) && trend(m) !== undefined && Math.abs(trend(m)) > 0.005).length;
}

/* ---- fetch ---- */
async function load() {
  try {
    const [minersR, routerR] = await Promise.all([
      fetch('/api/miners'),
      fetch('/api/router').catch(() => null)
    ]);
    if (!minersR.ok) throw new Error('miners endpoint ' + minersR.status);
    DATA = await minersR.json();
    if (routerR?.ok) {
      DATA.router_snap = await routerR.json();
    }
    $('updated').textContent = 'updated ' + new Date().toLocaleTimeString('en-GB');
    buildFilters();
    applyDeepLink();
    render();
    renderSide();
    renderLeader();
    renderMovers();
    renderCountries();
    renderNet();
    renderRouter(DATA);
  } catch (e) {
    if (!DATA) $('board').innerHTML = `<div class="empty"><h2>Could not load miners.</h2><p>${esc(e.message)}</p></div>`;
    else $('updated').textContent = 'refresh failed — showing last snapshot';
  }
}

/* /?m=<address> focuses one miner — the movers table links straight here. */
function applyDeepLink() {
  const addr = new URLSearchParams(location.search).get('m');
  if (!addr || !DATA) return;
  if (!DATA.miners.some(m => m.address === addr)) return;
  F_ADDR = addr;
  document.querySelectorAll('#showChips .chip').forEach(x => x.classList.remove('on'));
  const head = document.querySelector('.board-head');
  if (head && !head.querySelector('a')) {
    head.insertAdjacentHTML('beforeend', '<a class="btn small ghost" href="/">Show every miner</a>');
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
    // Leaving the focused single-miner view: drop the ?m= so a refresh is not stuck on it.
    if (F_ADDR) {
      F_ADDR = '';
      const u = new URL(location.href);
      u.searchParams.delete('m');
      history.replaceState(null, '', u);
    }
    render();
  };
});
$('modelFilter').onchange = e => { F_MODEL = e.target.value; F_ADDR = ''; render(); };

load();
setInterval(() => { if (!document.hidden) load(); }, 60_000);
