/* Flop Scout — Analytics page.
   Reads /api/analytics (aggregates only). Depends on agent.js for $, esc. */

const fmtN = n => n == null ? '—' : Number(n).toLocaleString('en-US');
const fmtPct = v => v == null ? '—' : Math.round(v * 100) + '%';

function drawChart(rows) {
  const box = document.getElementById('chart');
  if (!rows.length) { box.innerHTML = '<p class="note">No answers in this window yet.</p>'; return; }
  // One line, one scale, values written on the points. Client counts live in the
  // table: with a handful of clients, bars only draw stripes.
  const W = 960, H = 200, padL = 40, padR = 16, padT = 24, padB = 34;
  const top = Math.max(1, ...rows.map(r => r.answers));
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const x = i => rows.length === 1 ? padL + innerW / 2 : padL + (i * innerW) / (rows.length - 1);
  const y = v => padT + innerH - (v / top) * innerH;
  const pts = rows.map((r, i) => [x(i), y(r.answers)]);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${padT + innerH} L${pts[0][0].toFixed(1)},${padT + innerH} Z`;
  const grid = [0, 0.5, 1].map(f => {
    const v = Math.round(top * f), gy = y(v);
    return `<line x1="${padL}" x2="${W - padR}" y1="${gy.toFixed(1)}" y2="${gy.toFixed(1)}" class="an-grid"/>` +
           `<text x="${padL - 8}" y="${(gy + 4).toFixed(1)}" text-anchor="end" class="an-tick">${v}</text>`;
  }).join('');
  const dots = rows.map((r, i) => `<circle cx="${pts[i][0].toFixed(1)}" cy="${pts[i][1].toFixed(1)}" r="4" class="an-dot"><title>${esc(r.day)}: ${r.answers} answers, ${r.clients} clients</title></circle>` +
    `<text x="${pts[i][0].toFixed(1)}" y="${(pts[i][1] - 10).toFixed(1)}" text-anchor="middle" class="an-val">${r.answers}</text>`).join('');
  const labels = rows.map((r, i) => (rows.length > 10 && i % Math.ceil(rows.length / 6)) ? '' :
    `<text x="${x(i).toFixed(1)}" y="${H - 10}" text-anchor="middle" class="an-tick">${esc(r.day.slice(5))}</text>`).join('');
  box.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Answers per day">
      <defs><linearGradient id="anFill" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="var(--teal)" stop-opacity=".18"/><stop offset="1" stop-color="var(--teal)" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}
      <path d="${area}" fill="url(#anFill)"/>
      <path d="${line}" fill="none" stroke="var(--teal)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      ${dots}${labels}
    </svg>`;
  document.getElementById('legend').textContent = 'answers per day';
}

function fillTable(sel, rowsHtml) {
  document.querySelector(sel + ' tbody').innerHTML = rowsHtml || '<tr><td colspan="6" class="c-muted">No data in this window.</td></tr>';
}

function render(d) {
  const t = d.totals;
  $('kAnswers').textContent = fmtN(t.answers);
  $('kAnswered').textContent = fmtN(t.answered);
  $('kClients').textContent = fmtN(t.unique_clients);
  $('kReturn').textContent = fmtPct(d.retention.returning_share);
  $('kQuoted').textContent = t.quoted_flop ? t.quoted_flop.toFixed(3) : '0';
  $('kUser').textContent = fmtPct(t.paid_by_user_share);
  $('anNote').textContent = `last ${d.window_days} days · ${d.metering}`;
  drawChart(d.daily);
  fillTable('#dailyTable', d.daily.slice().reverse().map(r =>
    `<tr><td class="mono">${esc(r.day)}</td><td>${fmtN(r.answers)}</td><td>${fmtN(r.answered)}</td>` +
    `<td>${fmtN(r.clients)}</td><td>${fmtN(r.paid_by_user)}</td><td class="mono">${r.quoted_flop}</td></tr>`).join(''));
  fillTable('#srcTable', d.sources.map(s =>
    `<tr><td class="mono">${esc(s.id)}</td><td>${fmtN(s.calls)}</td><td>${fmtN(s.ok)}</td><td>${fmtPct(s.success_rate)}</td></tr>`).join(''));
  $('engines').textContent = 'engines: ' + Object.entries(d.engines).map(([k, v]) => `${k} ${v}`).join(' · ');
  $('anDot').classList.add('ok');
}

async function load(days) {
  $('anNote').textContent = 'loading…';
  try {
    const r = await fetch(`/api/analytics?days=${days}`);
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
  } catch (e) {
    $('anNote').textContent = 'could not load analytics';
    $('anDot').classList.remove('ok');
  }
}

document.querySelectorAll('#winChips .chip').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#winChips .chip').forEach(x => x.classList.toggle('on', x === b));
  load(Number(b.dataset.days));
}));

load(30);
