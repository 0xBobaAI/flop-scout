/* Flop Scout · Answer — front-end. Wallet secrets stay in this browser (localStorage). */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const FLOP_API = 'https://api.flop.network';
const FAUCET = 'https://faucet.flop.network';
const LS = 'flopScoutAgent.v1';
let CAT = null;
let agent = JSON.parse(localStorage.getItem(LS) || 'null'); // {mnemonic, address, key, expires_at}

function toast(msg, ms = 3200) { const t = $('toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), ms); }
function saveAgent() { agent ? localStorage.setItem(LS, JSON.stringify(agent)) : localStorage.removeItem(LS); renderAgent(); }
const short = a => a ? a.slice(0, 6) + '…' + a.slice(-4) : '';
const keyValid = () => agent?.key && agent.expires_at > Date.now() + 60000;

/* ---------- catalog / controls ---------- */
async function init() {
  CAT = await (await fetch('api/catalog')).json();
  const groups = {};
  CAT.sources.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
  $('sourcePick').innerHTML = Object.entries(groups).map(([g, list]) => `<div class="g">${esc(g)}</div>` + list.map(s =>
    `<label><input type="checkbox" value="${s.id}" ${s.default ? 'checked' : ''}>${esc(s.label)}<small>${esc(s.hint || '')}</small></label>`).join('')).join('');
  $('presets').innerHTML = Object.entries(CAT.presets).map(([k, p]) => `<button class="chip" data-p="${k}">${esc(p.label)}</button>`).join('');
  document.querySelectorAll('.chip').forEach(ch => ch.onclick = () => setPreset(ch.dataset.p));
  const opt = m => `<option value="${m.id}" ${m.blocked ? 'disabled' : ''}>${m.provider === 'flop' ? 'FLOP · ' : 'OpenRouter · '}${esc(m.label)}${m.blocked ? ' (not on your key)' : ''}</option>`;
  $('model').innerHTML = CAT.models.map(opt).join('');
  $('compare').innerHTML += CAT.models.map(opt).join('');
  network();
  const u = new URLSearchParams(location.search);
  if (u.get('p')) setPreset(u.get('p'));
  if (u.get('q')) { $('q').value = u.get('q'); ask(); }
}
function setPreset(p) {
  if (!CAT?.presets[p]) return;
  document.querySelectorAll('.chip').forEach(x => x.classList.toggle('on', x.dataset.p === p));
  const set = CAT.presets[p].sources;
  document.querySelectorAll('#sourcePick input').forEach(i => i.checked = set.includes(i.value));
}
async function network() {
  try {
    const [h, m, cat] = await Promise.all([
      fetch(FLOP_API + '/health').then(r => r.json()),
      fetch(FLOP_API + '/v1/models').then(r => r.json()),
      fetch('/api/catalog').then(r => r.json()).catch(() => ({}))
    ]);
    const ok = h.status === 'ok';
    $('netDot').classList.toggle('ok', ok);
    $('netName').textContent = `FLOP ${h.network?.network || ''}`.trim();
    const tpm = (m.data || []).reduce((a, x) => a + (x['x-flop']?.capacity_tpm || 0), 0);
    const router = cat.router;
    const bestNote = router?.best_miner?.address
      ? ` · best miner ${router.best_miner.address.slice(0,8)}… (${(router.best_miner.score*100).toFixed(0)}% score)`
      : '';
    $('netNote').textContent = `${ok ? 'live' : 'degraded'} · ${(m.data || []).length} models · ${tpm.toLocaleString('en')} tokens/min · ${router?.active_priced || '?'} active miners${bestNote} · devnet activity earns nothing`;
  } catch { $('netNote').textContent = 'status unavailable'; }
}

/* ---------- ask ---------- */
function answerCard(a) {
  const or = a.engine === 'openrouter';
  const body = a.answer
    ? `<div class="answer-body">${esc(a.answer).replace(/\[(\d+)\]/g, '<a href="#s$1">$1</a>')}</div>`
    : `<div class="err">No answer: ${esc(a.error)}</div>`;
  const m = [];
  if (a.latency_s != null) m.push(`${a.latency_s}s`);
  if (a.attempts > 1) m.push(`${a.attempts} attempts`);
  if (a.usage?.completion_tokens) m.push(`${a.usage.prompt_tokens}→${a.usage.completion_tokens} tokens`);
  if (a.usage?.cost_flop != null) m.push(`miner ${a.usage.cost_flop} FLOP`);
  if (a.receipt?.miner) m.push(`miner ${short(a.receipt.miner)}`);
  if (a.receipt?.settlementStatus) m.push(`settlement ${a.receipt.settlementStatus}`);
  if (a.paid_by) m.push(`compute paid by ${a.paid_by === 'user' ? 'your wallet' : 'Scout'}`);
  // Scout's own charge sits on its own line: it is a different thing from the
  // miner's inference cost above and should never be read as the same number.
  const fee = a.scout_charge_flop != null || a.metered_flop != null
    ? `<div class="answer-fee"><span class="mono">${a.metered_flop != null ? `${a.metered_flop} FLOP` : `${a.scout_charge_flop ?? 0} FLOP`}</span> for search, routing and citations${a.metering === 'nominal' ? ' · <em>not charged on devnet</em>' : ''}</div>`
    : '';
  return `<article class="card answer"><div class="answer-top"><span class="kind ${or ? 'or' : ''}">${or ? 'OpenRouter' : 'FLOP'} · ${esc(a.model)}</span></div>${body}<div class="answer-meta">${m.map(esc).join('<span>·</span>')}</div>${fee}</article>`;
}
/* Itemised Scout charge. Renders the same table the API returns so a reader can
   see exactly what each source cost and what is free. */
function priceLine(p) {
  const rows = p.items.map(i => `<li><span>${esc(i.label)}</span><span class="mono">${i.price_flop ? i.price_flop.toFixed(3) + ' FLOP' : 'free'}</span></li>`).join('');
  const total = p.cache_hit ? 'cached — 0 FLOP'
    : `${p.total_flop.toFixed(3)} FLOP${p.metering === 'nominal' ? ' <em>(not charged on devnet)</em>' : ''}`;
  return `<details class="pricebox"><summary><span class="mono">${total}</span> · this answer's Scout fee</summary><ul>${rows}</ul></details>`;
}

function sourceCard(s) {  return `<a class="src" id="s${s.n}" href="${esc(s.url)}" target="_blank" rel="noopener"><div class="src-top"><span class="src-n">[${s.n}]</span><span class="src-from">${esc(s.source)}</span></div>
  <h4>${esc(s.title || s.url)}</h4><p>${esc(s.snippet)}</p></a>`;
}
async function ask(e) {
  e?.preventDefault();
  const q = $('q').value.trim(); if (q.length < 2) return;
  history.replaceState(null, '', '?q=' + encodeURIComponent(q));
  const sources = [...document.querySelectorAll('#sourcePick input:checked')].map(i => i.value);
  const keys = {}; if (keyValid()) keys.flop = agent.key;
  const body = { q, sources, model: $('model').value, compare_model: $('compare').value || null, keys };
  $('goBtn').disabled = true;
  const t0 = Date.now();
  $('results').innerHTML = `<div class="progress"><i></i><span id="prog">Searching ${sources.length} sources…</span></div><div class="skeleton"></div><div class="skeleton" style="margin-top:9px;height:80px"></div>`;
  const tick = setInterval(() => { const p = $('prog'); if (p) p.textContent = `Computing on FLOP… ${Math.round((Date.now() - t0) / 1000)}s (devnet miners can take 1–3 min)`; }, 1500);
  try {
    const r = await fetch('api/answer', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : r.status);
    let h = j.answers.length ? `<div class="answers ${j.answers.length > 1 ? 'two' : ''}">${j.answers.map(answerCard).join('')}</div>`
      : `<div class="empty"><h2>No sources found.</h2><p>Try other sources or rephrase the question.</p></div>`;
    if (j.sources.length) h += `<div class="sources"><h3>Sources · search ${j.search_s}s · ${j.source_report.map(x => `${esc(x.label)} ${x.error ? '✕' : x.count}`).join(' · ')}</h3>${j.sources.map(sourceCard).join('')}</div>`;
    if (j.pricing?.items?.length) h += priceLine(j.pricing);
    $('results').innerHTML = h;
    if (agent) refreshAgent();
  } catch (err) {
    $('results').innerHTML = `<div class="empty"><h2>Something went wrong.</h2><p>${esc(err.message || err)}</p></div>`;
  } finally { clearInterval(tick); $('goBtn').disabled = false; }
}

/* ---------- agent wallet ---------- */
function renderAgent() {
  const btn = $('walletBtn');
  btn.classList.toggle('on', !!agent);
  btn.querySelector('span').textContent = agent ? short(agent.address) : 'Connect agent wallet';
  $('agentStats').hidden = !agent;
  if (!agent) {
    $('agentTitle').textContent = 'No wallet connected';
    $('agentSub').textContent = "Answers are computed with Scout's devnet wallet. Connect your own agent wallet to pay for compute yourself and keep the receipts.";
    $('paidBy').textContent = 'Compute paid by: Scout (free tier)';
    return;
  }
  $('agentTitle').textContent = short(agent.address);
  $('agentSub').textContent = keyValid() ? `Gateway key active until ${new Date(agent.expires_at).toLocaleString('en-GB')}. Your wallet pays for every FLOP answer.`
    : 'Wallet ready, gateway key missing or expired — open the wallet to enroll.';
  $('paidBy').textContent = keyValid() ? `Compute paid by: your wallet ${short(agent.address)}` : 'Compute paid by: Scout (enroll a key to use your wallet)';
}
async function refreshAgent() {
  if (!keyValid()) { renderAgent(); return; }
  try {
    const h = { Authorization: 'Bearer ' + agent.key };
    const [me, rc] = await Promise.all([fetch(FLOP_API + '/v1/keys/me', { headers: h }).then(r => r.json()), fetch(FLOP_API + '/v1/keys/me/receipts', { headers: h }).then(r => r.json())]);
    const list = rc.receipts || [];
    const settled = list.filter(x => x.state === 'settled');
    const spend = settled.reduce((a, x) => a + (x.settled_spend || x.reserved_spend || 0), 0);
    $('stSessions').textContent = settled.length;
    $('stSpend').textContent = spend.toFixed(2);
    $('stQuota').textContent = me.remaining_quota != null ? Math.round(me.remaining_quota).toLocaleString('en') : '—';
    $('stNote').textContent = `${list.length} requests on this key · ${list.filter(x => x.state === 'released').length} released without charge (busy miners). Data: FLOP gateway ledger.`;
  } catch { $('stNote').textContent = 'Could not reach the FLOP gateway ledger.'; }
  renderAgent();
}

function walletView() {
  const b = $('walletBody');
  if (!agent) {
    b.innerHTML = `<p>Your agent wallet is an sr25519 FLOP account created in this browser. Scout never sees the recovery phrase.</p>
      <div class="row"><button class="btn primary" id="wCreate">Create new wallet</button></div>
      <p style="margin-top:18px">Or import an existing recovery phrase:</p><textarea id="wImport" rows="2" placeholder="twelve words …"></textarea>
      <div class="row"><button class="btn" id="wImportBtn">Import</button></div>`;
    $('wCreate').onclick = async () => { await FlopWallet.ready(); const m = FlopWallet.generate(); agent = { mnemonic: m, address: FlopWallet.pair(m).address }; saveAgent(); walletView(true); };
    $('wImportBtn').onclick = async () => { await FlopWallet.ready(); const m = $('wImport').value.trim().toLowerCase().replace(/\s+/g, ' ');
      if (!FlopWallet.valid(m)) return toast('Invalid recovery phrase'); agent = { mnemonic: m, address: FlopWallet.pair(m).address }; saveAgent(); walletView(); };
    return;
  }
  b.innerHTML = `<p>Address</p><div class="seed">${esc(agent.address)}</div>
    <details style="margin-top:12px"><summary>Show recovery phrase</summary><div class="seed" style="margin-top:8px">${esc(agent.mnemonic)}</div>
    <p class="note">Write it down. It is stored only in this browser — clearing site data deletes it.</p></details>
    <p style="margin-top:16px"><b>Gateway key:</b> ${keyValid() ? 'active until ' + new Date(agent.expires_at).toLocaleString('en-GB') : 'not enrolled'}</p>
    <div class="row"><button class="btn primary" id="wSetup">${keyValid() ? 'Re-enroll key' : 'Fund & enroll (devnet)'}</button>
    <button class="btn" id="wCopy">Copy address</button><button class="btn ghost" id="wForget">Forget wallet</button></div>
    <p class="note" id="wLog">Devnet: the faucet grants 25 test FLOP; enrollment gives a 24-hour key with up to 20,000 FLOP of quota. FLOP limits enrollments per day.</p>`;
  $('wSetup').onclick = setupKey;
  $('wCopy').onclick = () => navigator.clipboard.writeText(agent.address).then(() => toast('Address copied'));
  $('wForget').onclick = () => { if (confirm('Forget this wallet? Make sure you saved the recovery phrase.')) { agent = null; saveAgent(); walletView(); } };
}
async function setupKey() {
  const log = m => { $('wLog').textContent = m; };
  const btn = $('wSetup'); btn.disabled = true;
  try {
    await FlopWallet.ready();
    const pair = FlopWallet.pair(agent.mnemonic);
    log('Requesting devnet FLOP from the faucet…');
    const f = await fetch(FAUCET + '/fund', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ address: agent.address }) });
    const fj = await f.json().catch(() => ({}));
    log(f.ok ? 'Funded. Waiting for the transfer to finalize…' : `Faucet: ${fj.error || f.status} — trying enrollment anyway…`);
    for (let i = 0; i < 8; i++) {
      await new Promise(s => setTimeout(s, i ? 10000 : 12000));
      const c = await (await fetch(`${FLOP_API}/v1/enroll/wallet/challenge?address=${agent.address}`)).json();
      const r = await fetch(FLOP_API + '/v1/enroll/wallet', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ address: agent.address, message: c.message, signature: FlopWallet.sign(pair, c.message) }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.key) { agent.key = j.key; agent.expires_at = j.expires_at; saveAgent(); toast('Gateway key enrolled'); walletView(); refreshAgent(); return; }
      if (j.error === 'wallet_enrollment_cap_reached') { log('FLOP devnet reached its daily enrollment cap. Try again later — your wallet is saved.'); break; }
      if (j.error && !/balance|fund|insufficient/i.test(j.error + j.message)) { log(`Enrollment failed: ${j.message || j.error}`); break; }
      log(`Waiting for funds to arrive… (${i + 1}/8)`);
    }
  } catch (e) { log('Error: ' + (e.message || e)); }
  btn.disabled = false;
}

/* ---------- wiring ---------- */
$('askForm').onsubmit = ask;
document.querySelectorAll('.examples button').forEach(b => b.onclick = () => { $('q').value = b.dataset.q; setPreset(b.dataset.p); ask(); });
const openWallet = () => { walletView(); $('walletDlg').showModal(); };
$('walletBtn').onclick = openWallet; $('openWallet').onclick = openWallet;
document.querySelectorAll('[data-open-wallet]').forEach(b => b.onclick = openWallet);
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close());
$('refreshAgent').onclick = refreshAgent;
document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); $('q').focus(); } });
renderAgent(); if (agent) refreshAgent();
init();
