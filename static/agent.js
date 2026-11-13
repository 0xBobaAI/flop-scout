/* Flop Scout — shared wallet + agent state.
   Loaded on every page so the topbar button always works. Secrets stay in
   this browser's localStorage; the server never sees the recovery phrase. */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const FLOP_API = 'https://api.flop.network';
const FAUCET = 'https://faucet.flop.network';
const LS = 'flopScoutAgent.v1';

let agent = JSON.parse(localStorage.getItem(LS) || 'null'); // {mnemonic, address, key, expires_at}

const short = a => a ? a.slice(0, 6) + '…' + a.slice(-4) : '';
const keyValid = () => !!agent?.key && agent.expires_at > Date.now() + 60000;

function toast(msg, ms = 3200) {
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('on'), ms);
}

function saveAgent() {
  agent ? localStorage.setItem(LS, JSON.stringify(agent)) : localStorage.removeItem(LS);
  renderWalletButton();
  document.dispatchEvent(new CustomEvent('agent:change', { detail: agent }));
}

/* The pill only knows about the button; page modules render their own widgets
   in response to the agent:change event. */
function renderWalletButton() {
  const btn = $('walletBtn');
  if (!btn) return;
  btn.classList.toggle('on', !!agent);
  btn.querySelector('span').textContent = agent ? short(agent.address) : 'Connect wallet';
}

/* ── gateway key enrollment ─────────────────────────────────────────────── */
async function setupKey(log) {
  log = log || (m => { const el = $('wLog'); if (el) el.textContent = m; });
  const btn = $('wSetup');
  if (btn) btn.disabled = true;
  try {
    await FlopWallet.ready();
    const pair = FlopWallet.pair(agent.mnemonic);
    log('Requesting devnet FLOP from the faucet…');
    const f = await fetch(FAUCET + '/fund', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ address: agent.address }),
    });
    const fj = await f.json().catch(() => ({}));
    log(f.ok ? 'Funded. Waiting for the transfer to finalize…'
             : `Faucet: ${fj.error || f.status} — trying enrollment anyway…`);

    for (let i = 0; i < 8; i++) {
      await new Promise(s => setTimeout(s, i ? 10000 : 12000));
      const c = await (await fetch(`${FLOP_API}/v1/enroll/wallet/challenge?address=${agent.address}`)).json();
      const r = await fetch(FLOP_API + '/v1/enroll/wallet', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ address: agent.address, message: c.message, signature: FlopWallet.sign(pair, c.message) }),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.key) {
        agent.key = j.key;
        agent.expires_at = j.expires_at;
        saveAgent();
        toast('Gateway key enrolled');
        walletView();
        document.dispatchEvent(new CustomEvent('agent:enrolled'));
        return;
      }
      if (j.error === 'wallet_enrollment_cap_reached') {
        log('FLOP devnet reached its daily enrollment cap. Try again later — your wallet is saved.');
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

/* ── wallet dialog ──────────────────────────────────────────────────────── */
function walletView(justCreated) {
  const b = $('walletBody');
  if (!b) return;

  if (!agent) {
    b.innerHTML = `
      <p>Your agent wallet is an sr25519 FLOP account created in this browser. Scout never sees the recovery phrase.</p>
      <div class="row"><button class="btn primary" id="wCreate" type="button">Create new wallet</button></div>
      <p style="margin-top:18px">Or import an existing recovery phrase:</p>
      <textarea id="wImport" rows="2" placeholder="twelve words …"></textarea>
      <div class="row"><button class="btn" id="wImportBtn" type="button">Import</button></div>`;
    $('wCreate').onclick = async () => {
      await FlopWallet.ready();
      const m = FlopWallet.generate();
      agent = { mnemonic: m, address: FlopWallet.pair(m).address };
      saveAgent();
      walletView(true);
    };
    $('wImportBtn').onclick = async () => {
      await FlopWallet.ready();
      const m = $('wImport').value.trim().toLowerCase().replace(/\s+/g, ' ');
      if (!FlopWallet.valid(m)) return toast('Invalid recovery phrase');
      agent = { mnemonic: m, address: FlopWallet.pair(m).address };
      saveAgent();
      walletView();
    };
    return;
  }

  b.innerHTML = `
    ${justCreated ? '<p class="note" style="margin:0 0 10px"><b>Save the recovery phrase now</b> — it is the only way to restore this wallet.</p>' : ''}
    <p>Address</p><div class="seed">${esc(agent.address)}</div>
    <details style="margin-top:12px"><summary>Show recovery phrase</summary>
      <div class="seed" style="margin-top:8px">${esc(agent.mnemonic)}</div>
      <p class="note">Write it down. It is stored only in this browser — clearing site data deletes it.</p>
    </details>
    <p style="margin-top:16px"><b>Gateway key:</b> ${keyValid()
      ? 'active until ' + new Date(agent.expires_at).toLocaleString('en-GB')
      : 'not enrolled'}</p>
    <div class="row">
      <button class="btn primary" id="wSetup" type="button">${keyValid() ? 'Re-enroll key' : 'Fund &amp; enroll (devnet)'}</button>
      <button class="btn" id="wCopy" type="button">Copy address</button>
      <button class="btn ghost" id="wForget" type="button">Forget wallet</button>
    </div>
    <p class="note" id="wLog">Devnet: the faucet grants 25 test FLOP; enrollment gives a 24-hour key with up to 20,000 FLOP of quota. FLOP limits enrollments per day.</p>`;

  $('wSetup').onclick = () => setupKey();
  $('wCopy').onclick = () => navigator.clipboard.writeText(agent.address).then(() => toast('Address copied'));
  $('wForget').onclick = () => {
    if (confirm('Forget this wallet? Make sure you saved the recovery phrase.')) {
      agent = null;
      saveAgent();
      walletView();
    }
  };
}

/* ── wiring (runs on every page) ────────────────────────────────────────── */
(function bootWallet() {
  const open = () => { walletView(); const d = $('walletDlg'); if (d) d.showModal(); };
  const pill = $('walletBtn');
  if (pill) pill.onclick = open;
  const ow = $('openWallet');
  if (ow) ow.onclick = open;
  document.querySelectorAll('[data-open-wallet]').forEach(b => { b.onclick = open; });
  document.querySelectorAll('[data-close]').forEach(b => {
    b.onclick = () => { const d = b.closest('dialog'); if (d) d.close(); };
  });
  renderWalletButton();
})();
