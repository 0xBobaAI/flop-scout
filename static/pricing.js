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
        : ['exa','tavily','brave','firecrawl','serper','jina'].includes(s.id) ? 'Web (caller key)'
        : ['keenable','web_read','page'].includes(s.id) ? 'Web (free)'
        : ['github','hn'].includes(s.id) ? 'Developers'
        : ['arxiv','crypto'].includes(s.id) ? 'Research & Finance' : 'Other';
      (groups[g] = groups[g] || []).push(s);
    });

    const fmtP = p => p === 0 ? '<span class="pt-price free">free</span>'
      : `<span class="pt-price">${p.toFixed(3)} FLOP</span>`;

    const availNote = d.available != null && d.available < d.total
      ? ` · ${d.total - d.available} of ${d.total} not configured on this host`
      : '';

    let html = `<div class="pt-head"><b>Service catalogue</b>
      <span>metering: ${d.metering === 'nominal' ? 'computed only · not debited on devnet' : d.metering}${availNote}</span></div>
      <div class="pt-row"><span class="pt-name">Answer reasoning + citations</span>
        <span class="pt-unit">${d.answer_fee.unit}</span>${fmtP(d.answer_fee.price_flop)}</div>
      <div class="pt-row"><span class="pt-name">Compare (second model)</span>
        <span class="pt-unit">${d.compare_fee.unit}</span>${fmtP(d.compare_fee.price_flop)}</div>`;
    if (d.read_fee) {
      // POST /api/read: one page, no search, no model. Inside an answer the
      // page source is free and the answer fee covers it.
      html += `<div class="pt-row${d.read_fee.available === false ? ' unavailable' : ''}">
        <span class="pt-name">Page read (POST /api/read)</span>
        <span class="pt-unit">${d.read_fee.unit}</span>${fmtP(d.read_fee.price_flop)}</div>`;
    }

    for (const [group, items] of Object.entries(groups)) {
      html += `<div class="pt-group">${group}</div>`;
      items.sort((a,b) => b.price_flop - a.price_flop).forEach(s => {
        // A price you cannot buy is a bug; a price you can read but not buy is
        // disclosure. Keep the row, mark it, say why.
        const off = s.available === false;
        html += `<div class="pt-row${off ? ' unavailable' : ''}">
          <span class="pt-name">${s.id.replace(/_/g,' ')}${off ? '<span class="pt-flag">not configured</span>' : ''}</span>
          <span class="pt-unit">${s.unit}</span>
          ${fmtP(s.price_flop)}
        </div>`;
      });
    }
    html += `<div class="pt-foot">Free sources have no upstream bill. Caller-key sources are listed at their notional price so budgets compare. Prices are devnet-nominal. <code>POST /api/quote</code> returns the same total before a request runs.</div>`;
    el.innerHTML = html;
  } catch (e) {
    if (el) el.innerHTML = `<p class="note">Could not load pricing data: ${esc(e.message)}</p>`;
  }
}
loadPricing();
