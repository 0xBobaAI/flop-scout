/* Flop Scout — pricing page. Fetches /api/pricing and renders the catalogue. */
async function loadPricing() {
  const el = document.getElementById('priceTable');
  if (!el) return;
  try {
    const d = await fetch('/api/pricing').then(r => r.json());
    const groups = {};
    d.services.forEach(s => {
      const g = s.id.startsWith('explorer') ? 'FLOP Explorer'
        : s.id.startsWith('flop') ? 'FLOP Gateway'
        : ['exa','tavily','brave','firecrawl','serper'].includes(s.id) ? 'Web (key required)'
        : ['keenable','jina','web_read'].includes(s.id) ? 'Web (free)'
        : ['github','hn'].includes(s.id) ? 'Developers'
        : ['arxiv','crypto'].includes(s.id) ? 'Research & Finance' : 'Other';
      (groups[g] = groups[g] || []).push(s);
    });

    const fmtP = p => p === 0 ? '<span class="pt-price free">free</span>'
      : `<span class="pt-price">${p.toFixed(3)} FLOP</span>`;

    let html = `<div class="pt-head"><b>Service catalogue</b>
      <span>metering: ${d.metering === 'nominal' ? 'computed only · not debited on devnet' : d.metering}</span></div>
      <div class="pt-row"><span class="pt-name">Answer reasoning + citations</span>
        <span class="pt-unit">${d.answer_fee.unit}</span>${fmtP(d.answer_fee.price_flop)}</div>
      <div class="pt-row"><span class="pt-name">Compare (second model)</span>
        <span class="pt-unit">${d.compare_fee.unit}</span>${fmtP(d.compare_fee.price_flop)}</div>`;

    for (const [group, items] of Object.entries(groups)) {
      html += `<div class="pt-group">${group}</div>`;
      items.sort((a,b) => b.price_flop - a.price_flop).forEach(s => {
        html += `<div class="pt-row">
          <span class="pt-name">${s.id.replace(/_/g,' ')}</span>
          <span class="pt-unit">${s.unit}</span>
          ${fmtP(s.price_flop)}
        </div>`;
      });
    }
    html += `<div class="pt-foot">Prices are devnet-nominal. There is no FLOP market rate yet — these numbers will be updated for mainnet. All charges are logged per-answer in Scout's ledger.</div>`;
    el.innerHTML = html;
  } catch (e) {
    if (el) el.innerHTML = `<p class="note">Could not load pricing data: ${e.message}</p>`;
  }
}
loadPricing();
