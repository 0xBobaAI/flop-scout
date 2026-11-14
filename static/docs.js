/* Flop Scout — docs pages.
   Collapses the sidebar on phones and highlights the section being read. */
(function () {
  const toggle = document.querySelector('.docs-nav-toggle');
  if (toggle && window.matchMedia('(max-width: 860px)').matches) toggle.open = false;

  const links = [...document.querySelectorAll('.docs-otp a')];
  if (!links.length || !('IntersectionObserver' in window)) return;
  const byId = Object.fromEntries(links.map(a => [a.getAttribute('href').slice(1), a]));
  const seen = new Set();
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => e.isIntersecting ? seen.add(e.target.id) : seen.delete(e.target.id));
    const first = Object.keys(byId).find(id => seen.has(id));
    if (first) links.forEach(a => a.classList.toggle('on', a === byId[first]));
  }, { rootMargin: '-80px 0px -60% 0px' });
  Object.keys(byId).forEach(id => { const h = document.getElementById(id); if (h) io.observe(h); });
})();
