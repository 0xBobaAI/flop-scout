/* Flop Scout — shared wallet + agent state.
   Loaded on every page so the topbar button always works. Secrets stay in
   this browser's localStorage; the server never sees the recovery phrase. */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const FLOP_API = window.FLOP_API || 'https://api.flop.network';
const FAUCET = window.FLOP_FAUCET || 'https://faucet.flop.network';
const LS = 'flopScoutAgent.v1';
const LS_PAY = 'flopScoutPayMode.v1'; // 'scout' | 'wallet' | 'member'

let agent = JSON.parse(localStorage.getItem(LS) || 'null'); // {mnemonic, address, key, expires_at}
let payMode = localStorage.getItem(LS_PAY) || '';
let walletBal = null;   // {free_flop, reserved_flop, frozen_flop, total_flop, loading, error, quota}
let showFlopKey = false;

/* Provider keys, separate from the wallet: these are API credentials for
   upstream services, plus optional pasted FLOP (`flop_...`) and Scout member
   (`sk_...`) keys. Stored in this browser only, sent per request, never logged. */
const LSK = 'flopScoutKeys.v1';
const KEY_FIELDS = [
  ['scout', 'Scout member (sk_…)', 'unlimited daily answers on Scout\'s wallet'],
  ['flop', 'FLOP gateway (flop_…)', 'paste an external FLOP key if not enrolled above'],
  ['openrouter', 'OpenRouter', 'runs the OpenRouter models in the picker'],
  ['exa', 'Exa', 'neural search source'],
  ['tavily', 'Tavily', 'AI-native search source'],
  ['brave', 'Brave', 'independent index source'],
  ['firecrawl', 'Firecrawl', 'scrape + search source'],
  ['serper', 'Serper', 'Google results source'],
  ['github', 'GitHub', 'raises the repo-search rate limit'],
  ['jina', 'Jina Reader', 'optional higher rate limit on /api/read'],
];
let providerKeys = JSON.parse(localStorage.getItem(LSK) || '{}');

function effectiveFlopKey() {
  return agent?.key || providerKeys.flop || '';
}

const short = a => a ? a.slice(0, 6) + '…' + a.slice(-4) : '';
const keyValid = () => {
  if (walletBal?.key_live === false) return false;
  if (walletBal?.key_live === true) return true;
  return (!!agent?.key && (!agent.expires_at || agent.expires_at > Date.now() + 60000)) || !!providerKeys.flop;
};

function currentPayMode() {
  if (payMode === 'scout' || payMode === 'wallet' || payMode === 'member') {
    if (payMode === 'member' && !providerKeys.scout) return keyValid() ? 'wallet' : 'scout';
    return payMode;
  }
  if (providerKeys.scout) return 'member';
  if (keyValid()) return 'wallet';
  return 'scout';
}

function setPayMode(mode) {
  payMode = mode;
  localStorage.setItem(LS_PAY, mode);
  renderWalletButton();
  document.dispatchEvent(new CustomEvent('agent:change', { detail: agent }));
  document.dispatchEvent(new CustomEvent('keys:change', { detail: providerKeys }));
}

function saveProviderKeys() {
  providerKeys = Object.fromEntries(Object.entries(providerKeys).filter(([, v]) => v));
  localStorage.setItem(LSK, JSON.stringify(providerKeys));
  renderWalletButton();
  document.dispatchEvent(new CustomEvent('keys:change', { detail: providerKeys }));
  document.dispatchEvent(new CustomEvent('agent:change', { detail: agent }));
}

function activeKeys() {
  // Respects the user's chosen payment mode: 'scout' omits keys.flop so Scout's
  // free tier pays; 'member' sends keys.scout; 'wallet' sends the FLOP key.
  const out = { ...providerKeys };
  delete out.flop;
  delete out.scout;
  const mode = currentPayMode();
  if (mode === 'wallet') {
    const fk = effectiveFlopKey();
    if (fk) out.flop = fk;
  } else if (mode === 'member' && providerKeys.scout) {
    out.scout = providerKeys.scout;
  }
  return out;
}

function fmtBal(v) {
  if (v == null || Number.isNaN(v)) return '—';
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + 'M';
  if (v >= 10_000) return (v / 1_000).toFixed(1) + 'k';
  if (v >= 100) return v.toFixed(1);
  if (v === 0) return '0';
  return v.toFixed(2).replace(/\.00$/, '');
}

function maskKey(k) {
  if (!k) return 'not enrolled yet';
  if (k.length <= 14) return '•'.repeat(k.length);
  return k.slice(0, 9) + '••••••••••••' + k.slice(-4);
}

function toast(msg, ms = 3200) {
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('on'), ms);
}

function saveAgent() {
  if (agent) localStorage.setItem(LS, JSON.stringify(agent));
  else { localStorage.removeItem(LS); walletBal = null; }
  renderWalletButton();
  document.dispatchEvent(new CustomEvent('agent:change', { detail: agent }));
}

/* Topbar pill shows short address + live on-chain FLOP balance when connected. */
function renderWalletButton() {
  const btn = $('walletBtn');
  if (!btn) return;
  const connected = !!agent || !!providerKeys.scout || !!providerKeys.flop;
  btn.classList.toggle('on', connected);
  const span = btn.querySelector('span');
  if (!span) return;
  if (agent) {
    const balTxt = walletBal && walletBal.free_flop != null ? ` · ${fmtBal(walletBal.free_flop)} FLOP` : '';
    span.textContent = short(agent.address) + balTxt;
  } else if (providerKeys.scout) {
    span.textContent = 'Member key';
  } else if (providerKeys.flop) {
    span.textContent = 'FLOP key';
  } else {
    span.textContent = 'Connect wallet';
  }
}

/* ── live balance + gateway quota ───────────────────────────────────────── */
async function refreshBalance(silent) {
  if (!agent?.address) { walletBal = null; renderWalletButton(); return; }
  walletBal = { ...(walletBal || {}), loading: true, error: null };
  updateBalanceDom();
  try {
    const reqs = [
      fetch(`/api/wallet/${encodeURIComponent(agent.address)}/balance`).then(async r => {
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail?.message || j.error || r.status);
        return j;
      }),
    ];
    const fk = effectiveFlopKey();
    if (fk) {
      reqs.push(
        fetch(FLOP_API + '/v1/keys/me', { headers: { Authorization: 'Bearer ' + fk } })
          .then(r => r.ok ? r.json() : null).catch(() => null)
      );
    }
    const [bal, me] = await Promise.all(reqs);
    walletBal = {
      ...bal,
      loading: false,
      error: null,
      quota: me && me.remaining_quota != null ? me.remaining_quota : null,
      key_live: me ? true : (fk ? false : null),
    };
  } catch (e) {
    walletBal = { ...(walletBal || {}), loading: false, error: e.message || 'RPC unavailable' };
    if (!silent) toast('Could not load balance: ' + walletBal.error);
  }
  renderWalletButton();
  updateBalanceDom();
  const dlg = $('walletDlg');
  if (dlg?.open) walletView();
  document.dispatchEvent(new CustomEvent('wallet:balance', { detail: walletBal }));
}

function updateBalanceDom() {
  const freeEl = $('wBalFree');
  if (!freeEl) return;
  if (!walletBal || walletBal.loading && walletBal.free_flop == null) {
    freeEl.textContent = 'loading…';
    return;
  }
  if (walletBal.error && walletBal.free_flop == null) {
    freeEl.textContent = 'unavailable';
    const sub = $('wBalSub');
    if (sub) sub.textContent = walletBal.error;
    return;
  }
  freeEl.textContent = `${fmtBal(walletBal.free_flop)} FLOP`;
  const parts = [];
  if (walletBal.reserved_flop > 0) parts.push(`${fmtBal(walletBal.reserved_flop)} reserved`);
  if (walletBal.frozen_flop > 0) parts.push(`${fmtBal(walletBal.frozen_flop)} frozen`);
  if (walletBal.quota != null) parts.push(`gateway allowance: ${fmtBal(walletBal.quota)} FLOP`);
  else if (walletBal.key_live === false) parts.push('gateway key expired or rejected');
  const sub = $('wBalSub');
  if (sub) sub.textContent = parts.length ? parts.join(' · ') : 'On-chain free balance (live via node RPC)';
}

/* ── gateway key enrollment ─────────────────────────────────────────────── */
async function setupKey(log) {
  log = log || (m => { const el = $('wLog'); if (el) el.textContent = m; });
  const btn = $('wSetup');
  if (btn) btn.disabled = true;
  try {
    await FlopWallet.ready();
    const pair = FlopWallet.pair(agent.mnemonic);
    log('Requesting test FLOP from the faucet…');
    const f = await fetch(FAUCET + '/fund', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ address: agent.address }),
    });
    const fj = await f.json().catch(() => ({}));
    log(f.ok ? 'Funded. Waiting for the transfer to finalize…'
             : `Faucet: ${fj.error || f.status} — trying enrollment anyway…`);

    for (let i = 0; i < 8; i++) {
      await new Promise(s => setTimeout(s, i ? 10000 : 12000));
      refreshBalance(true);
      const c = await (await fetch(`${FLOP_API}/v1/enroll/wallet/challenge?address=${agent.address}`)).json();
      const r = await fetch(FLOP_API + '/v1/enroll/wallet', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ address: agent.address, message: c.message, signature: FlopWallet.sign(pair, c.message) }),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.key) {
        agent.key = j.key;
        agent.expires_at = j.expires_at;
        if (!payMode) setPayMode('wallet');
        saveAgent();
        refreshBalance(true);
        toast('Gateway key enrolled');
        walletView();
        document.dispatchEvent(new CustomEvent('agent:enrolled'));
        return;
      }
      if (j.error === 'wallet_enrollment_cap_reached') {
        log('FLOP reached its rolling 24h enrollment cap. Try again later — your wallet is saved.');
        break;
      }
      if (j.error && !/balance|fund|insufficient/i.test(j.error + (j.message || ''))) {
        log(`Enrollment failed: ${j.message || j.error}`);
        break;
      }
      log(`Waiting for funds to arrive… (${i + 1}/8)`);
    }
  } catch (e) {
    log('Error: ' + (e.message || e));
  }
  if (btn) btn.disabled = false;
}

/* ── payment mode + integration key UI ──────────────────────────────────── */
function payModeSection() {
  const mode = currentPayMode();
  const hasWalletKey = !!effectiveFlopKey();
  const hasMember = !!providerKeys.scout;
  return `
    <div class="w-sec">
      <div class="w-sec-head"><b>Who pays for FLOP inference</b></div>
      <div class="pay-modes" role="radiogroup" aria-label="Payment mode">
        <label class="pay-opt ${mode === 'scout' ? 'on' : ''}">
          <input type="radio" name="payMode" value="scout" ${mode === 'scout' ? 'checked' : ''}>
          <div><b>Scout free tier</b><small>500 answers/day per IP · Scout's wallet pays</small></div>
        </label>
        <label class="pay-opt ${mode === 'wallet' ? 'on' : ''}">
          <input type="radio" name="payMode" value="wallet" ${mode === 'wallet' ? 'checked' : ''}>
          <div><b>My FLOP key</b><small>${hasWalletKey
            ? (keyValid() ? 'Your wallet/allowance pays · counts toward your activity' : 'Key saved, but expired — re-enroll or paste a fresh one')
            : 'Enroll a wallet above or paste flop_… below'}</small></div>
        </label>
        <label class="pay-opt ${mode === 'member' ? 'on' : ''}">
          <input type="radio" name="payMode" value="member" ${mode === 'member' ? 'checked' : ''} ${hasMember ? '' : 'disabled'}>
          <div><b>Scout member key (sk_…)</b><small>${hasMember ? 'Active · no daily cap on Scout\'s wallet' : 'Add your sk_… key in Keys below to unlock'}</small></div>
        </label>
      </div>
    </div>`;
}

function flopKeyBox() {
  const fk = effectiveFlopKey();
  const sk = providerKeys.scout || '';
  const shownKey = fk || '';
  const apiKeyForSnippet = (currentPayMode() === 'member' && sk) ? sk : (fk || 'sk_your_member_or_flop_key');
  const statusBadge = fk
    ? (keyValid()
        ? `<span class="kind" style="font-size:10.5px">active${agent?.expires_at ? ' · until ' + new Date(agent.expires_at).toLocaleString('en-GB') : ''}</span>`
        : `<span class="kind or" style="font-size:10.5px">expired</span>`)
    : `<span class="kind grey" style="font-size:10.5px">not enrolled</span>`;

  return `
    <div class="w-sec">
      <div class="w-sec-head">
        <b>Your FLOP API key</b> ${statusBadge}
      </div>
      <div class="key-reveal">
        <code class="mono" id="wFlopKeyVal">${shownKey ? esc(showFlopKey ? shownKey : maskKey(shownKey)) : 'Enroll wallet or paste flop_… key below to view'}</code>
        <div class="key-actions">
          <button class="btn small ghost" id="wToggleKey" type="button" ${shownKey ? '' : 'disabled'}>${showFlopKey ? 'Hide' : 'Show'}</button>
          <button class="btn small" id="wCopyKey" type="button" ${shownKey ? '' : 'disabled'}>Copy key</button>
        </div>
      </div>
      <details style="margin-top:9px">
        <summary style="font-size:12px">Use in Cherry Studio, Cursor or OpenAI SDK</summary>
        <p class="note" style="margin:6px 0">API Host: <code>https://flopscout.com</code> (Cherry Studio) or Base URL: <code>https://flopscout.com/v1</code> · MCP: <code>https://flopscout.com/mcp</code></p>
        <div class="codewrap"><button class="btn small copybtn" type="button" data-copy="">Copy</button><pre class="code" style="font-size:11px;padding:10px 12px">base_url = "https://flopscout.com/v1"
api_key  = "${esc(showFlopKey ? apiKeyForSnippet : (fk ? maskKey(fk) : (sk ? maskKey(sk) : 'sk_... or flop_...')))}"</pre></div>
      </details>
    </div>`;
}

/* ── wallet dialog ──────────────────────────────────────────────────────── */
function walletView(justCreated) {
  const b = $('walletBody');
  if (!b) return;

  if (!agent) {
    b.innerHTML = `
      <p>Create a local sr25519 FLOP wallet in this browser, or choose how your requests are paid. Scout never sees your recovery phrase.</p>
      <div class="row"><button class="btn primary" id="wCreate" type="button">Create new wallet</button></div>
      <details style="margin-top:12px"><summary>Import existing 12-word recovery phrase</summary>
        <textarea id="wImport" rows="2" placeholder="twelve words …" style="margin-top:8px"></textarea>
        <div class="row"><button class="btn small" id="wImportBtn" type="button">Import wallet</button></div>
      </details>
      ${payModeSection()}
      ${flopKeyBox()}
      ${providerKeyFields()}`;
    $('wCreate').onclick = async () => {
      await FlopWallet.ready();
      const m = FlopWallet.generate();
      agent = { mnemonic: m, address: FlopWallet.pair(m).address };
      saveAgent();
      refreshBalance(true);
      walletView(true);
    };
    const imp = $('wImportBtn');
    if (imp) imp.onclick = async () => {
      await FlopWallet.ready();
      const m = $('wImport').value.trim().toLowerCase().replace(/\s+/g, ' ');
      if (!FlopWallet.valid(m)) return toast('Invalid recovery phrase');
      agent = { mnemonic: m, address: FlopWallet.pair(m).address };
      saveAgent();
      refreshBalance(true);
      walletView();
    };
    wireWalletControls();
    wireProviderKeyInputs();
    wireCopyButtons();
    return;
  }

  b.innerHTML = `
    ${justCreated ? '<p class="note" style="margin:0 0 10px"><b>Save the recovery phrase now</b> — it is the only way to restore this wallet.</p>' : ''}
    <div class="w-bal-card">
      <div>
        <span class="mlabel">Wallet balance</span>
        <strong class="w-bal-num" id="wBalFree">${walletBal?.free_flop != null ? fmtBal(walletBal.free_flop) + ' FLOP' : 'loading…'}</strong>
        <small id="wBalSub" class="c-muted">On-chain free balance (live via node RPC)</small>
      </div>
      <div class="row" style="margin:0">
        <button class="btn small ghost" id="wRefreshBal" type="button" title="Refresh balance">↻ Refresh</button>
        <a class="btn small ghost" href="${esc(window.FLOP_EXPLORER || 'https://explorer.devnet.flop.network')}/account/${esc(agent.address)}" target="_blank" rel="noopener">Explorer ↗</a>
      </div>
    </div>
    <div style="margin-top:12px">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span class="mlabel">SS58 Address</span>
        <button class="btn small ghost" id="wCopy" type="button" style="min-height:24px;padding:2px 8px">Copy address</button>
      </div>
      <div class="seed" style="margin-top:4px;padding:9px 11px">${esc(agent.address)}</div>
    </div>
    <details style="margin-top:10px"><summary>Show recovery phrase (12 words)</summary>
      <div class="seed" style="margin-top:8px">${esc(agent.mnemonic)}</div>
      <p class="note">Stored only in this browser — clearing site data deletes it.</p>
    </details>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="wSetup" type="button">${agent.key ? 'Re-enroll gateway key' : 'Fund &amp; enroll key'}</button>
      <button class="btn ghost" id="wForget" type="button">Forget wallet</button>
    </div>
    <p class="note" id="wLog">Requests test FLOP from the faucet and signs a gateway enrollment challenge locally.</p>
    ${payModeSection()}
    ${flopKeyBox()}
    ${providerKeyFields()}`;

  updateBalanceDom();
  if (!walletBal || (!walletBal.loading && walletBal.free_flop == null)) refreshBalance(true);

  $('wSetup').onclick = () => setupKey();
  $('wRefreshBal').onclick = () => refreshBalance(false);
  $('wCopy').onclick = () => navigator.clipboard.writeText(agent.address).then(() => toast('Address copied'));
  $('wForget').onclick = () => {
    if (confirm('Forget this wallet? Make sure you saved the recovery phrase.')) {
      agent = null;
      showFlopKey = false;
      saveAgent();
      walletView();
    }
  };
  wireWalletControls();
  wireProviderKeyInputs();
  wireCopyButtons();
}

function wireWalletControls() {
  document.querySelectorAll('input[name="payMode"]').forEach(r => {
    r.onchange = () => {
      if (r.value === 'wallet' && !effectiveFlopKey()) {
        toast('Enroll a wallet key or paste a flop_… key below so requests can use it');
      }
      setPayMode(r.value);
      walletView();
    };
  });
  const tog = $('wToggleKey');
  if (tog) tog.onclick = () => { showFlopKey = !showFlopKey; walletView(); };
  const cp = $('wCopyKey');
  if (cp) cp.onclick = () => {
    const fk = effectiveFlopKey();
    if (fk) navigator.clipboard.writeText(fk).then(() => toast('FLOP API key copied'));
  };
}

/* The key panel is rendered on both wallet states — it is not tied to having a
   wallet, and someone who only wants Exa or a Scout member key needs no wallet. */
function providerKeyFields() {
  const count = Object.keys(providerKeys).length;
  return `<details class="pk" ${count ? 'open' : ''}>
    <summary>API &amp; member keys <span class="c-muted">(${count} set)</span></summary>
    <p class="note" style="margin-top:6px">Stored in this browser only, sent per request, never logged by Scout.</p>
    ${KEY_FIELDS.map(([id, label, hint]) => `
      <label class="pk-row"><span>${esc(label)}<small>${esc(hint)}</small></span>
        <input type="password" data-pk="${id}" autocomplete="off" spellcheck="false"
               placeholder="${providerKeys[id] ? '•••• saved' : 'not set'}" value="${esc(providerKeys[id] || '')}">
      </label>`).join('')}
    <div class="row"><button class="btn small" id="pkSave" type="button">Save keys</button>
      <button class="btn small ghost" id="pkClear" type="button">Clear all</button></div>
  </details>`;
}

function wireProviderKeyInputs() {
  const save = $('pkSave');
  if (save) save.onclick = () => {
    const hadScout = !!providerKeys.scout;
    const hadFlop = !!providerKeys.flop;
    document.querySelectorAll('[data-pk]').forEach(i => {
      const v = i.value.trim();
      if (v) providerKeys[i.dataset.pk] = v; else delete providerKeys[i.dataset.pk];
    });
    if (!hadScout && providerKeys.scout) payMode = 'member';
    else if (!hadFlop && providerKeys.flop && !payMode) payMode = 'wallet';
    if (payMode) localStorage.setItem(LS_PAY, payMode);
    saveProviderKeys();
    toast('Keys saved in this browser');
    walletView();
  };
  const clear = $('pkClear');
  if (clear) clear.onclick = () => {
    if (!confirm('Clear all saved API keys from this browser?')) return;
    providerKeys = {};
    if (payMode === 'member') setPayMode('scout');
    saveProviderKeys();
    walletView();
  };
}

/* ── copy buttons (shared: used on /docs and inside wallet snippets) ────── */
function wireCopyButtons() {
  document.querySelectorAll('[data-copy]').forEach(btn => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('click', async () => {
      const pre = btn.parentElement?.querySelector('.code');
      const text = (pre ? pre.textContent : btn.dataset.copy) || '';
      try {
        await navigator.clipboard.writeText(text.trim());
        const old = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = old; }, 1600);
      } catch {
        toast('Copy failed — select the text instead');
      }
    });
  });
}

/* ── wiring (runs on every page) ────────────────────────────────────────── */
(function bootWallet() {
  const open = () => {
    showFlopKey = false;
    walletView();
    if (agent?.address) refreshBalance(true);
    const d = $('walletDlg');
    if (d) d.showModal();
  };
  const pill = $('walletBtn');
  if (pill) pill.onclick = open;
  const ow = $('openWallet');
  if (ow) ow.onclick = open;
  document.querySelectorAll('[data-open-wallet]').forEach(b => { b.onclick = open; });
  document.querySelectorAll('[data-close]').forEach(b => {
    b.onclick = () => { showFlopKey = false; const d = b.closest('dialog'); if (d) d.close(); };
  });
  renderWalletButton();
  if (agent?.address) refreshBalance(true);
  wireCopyButtons();
})();
