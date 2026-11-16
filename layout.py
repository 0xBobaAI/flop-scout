"""Shared page shell.

Every page is a section of one product, so the topbar, footer and wallet dialog
live here instead of being copy-pasted into each template. A page module only
supplies its own head metadata and body markup.

Keeping the nav in one place means adding a section is a one-line change and the
"active" state can never drift between pages.
"""

SITE = 'https://flopscout.com'

# One number, derived once. Hardcoded "27 sources" was wrong within one release
# in three separate files; every page now renders this instead.
try:
    from registry import SOURCES as _SOURCES
    SOURCE_COUNT = len(_SOURCES)
except Exception:          # a page must still render if the registry breaks
    SOURCE_COUNT = 0

NAV = [
    ('/', 'Miners', 'Miners'),
    ('/answer', 'Answer', 'Answers'),
    ('/analytics', 'Analytics', 'Analytics'),
    ('/docs', 'Docs', 'Docs'),
]

# Inline SVG so the logo needs no extra request and inherits currentColor.
LOGO = (
    '<svg viewBox="0 0 40 40" fill="none" aria-hidden="true">'
    '<path d="M20 3 36 12v17L20 38 4 29V12L20 3Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>'
    '<path d="m26.5 12-4.2 11-10.8 4.8 4.3-11.2L26.5 12Z" fill="#0b918c"/>'
    '<path d="m26.5 12-10.7 4.6 6.5 6.4 4.2-11Z" fill="currentColor"/>'
    '<circle cx="20" cy="20" r="2" fill="var(--paper)"/>'
    '</svg>'
)

# Applied before first paint so a dark-mode visitor never sees a white flash.
# Separate from app.css on purpose: an inline script in <head> runs before the
# stylesheet is even requested.
# Applied before first paint so a saved theme never flashes white. ?theme=dark
# also works without JS, which makes dark mode screenshot-testable and linkable.
THEME_BOOT = (
    '<script>(function(){try{var u=new URLSearchParams(location.search).get("theme");'
    'if(u==="dark"||u==="light"){document.documentElement.dataset.theme=u;'
    'localStorage.setItem("flopScoutTheme",u);return;}'
    'var t=localStorage.getItem("flopScoutTheme");'
    'if(t==="dark"||t==="light")document.documentElement.dataset.theme=t;}catch(e){}})();</script>'
)


def head(title: str, description: str, path: str = '/') -> str:
    """Everything inside <head> except the charset/viewport boilerplate."""
    canon = f'{SITE}{path}' if path != '/' else f'{SITE}/'
    return f'''<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canon}">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/favicon.svg">
<link rel="manifest" href="/static/site.webmanifest">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f6f8fb" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#080d13" media="(prefers-color-scheme: dark)">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:site_name" content="Flop Scout">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{canon}">
<meta property="og:type" content="website">
<meta property="og:image" content="{SITE}/static/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Flop Scout — search and page reading that agents pay for in FLOP">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{SITE}/static/og.png">
<link rel="preload" href="{asset('app.css')}" as="style">
<link rel="stylesheet" href="{asset('app.css')}">'''


def topbar(active: str = '') -> str:
    # The class attribute is built outside the f-string: Python 3.10 (the VPS)
    # rejects a backslash inside an f-string expression.
    cls = ' class="on"'
    items = ''.join(
        '<a href="%s"%s>%s</a>' % (href, cls if href == active else '', label)
        for href, label, _ in NAV
    )
    return f'''<a class="skip" href="#main">Skip to content</a>
<header class="topbar">
  <a class="brand" href="/" aria-label="Flop Scout home">
    {LOGO}
    <span>flop</span><span class="lite">scout</span><span class="alpha">alpha</span>
  </a>
  <nav class="nav" aria-label="Sections">{items}</nav>
  <button class="icon-btn theme-btn" id="themeBtn" type="button" aria-label="Switch colour theme">
    <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8Z"/></svg>
    <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>
  </button>
  <button class="btn wallet-pill" id="walletBtn" type="button"><i></i><span>Connect wallet</span></button>
</header>'''


def footer() -> str:
    return f'''<footer class="foot">
  <div>
    <div class="fbrand">{LOGO}<span>flop<span style="font-weight:400;color:var(--teal)">scout</span></span></div>
    <p>Search and page reading for AI agents. Priced per call in FLOP.</p>
    <p>Independent tool built on the FLOP devnet. Not affiliated with FLOP Labs.</p>
    <p class="fine">Devnet activity earns nothing. Answers can be wrong — check the sources.</p>
  </div>
  <div>
    <div class="fnav-title">Product</div>
    <nav aria-label="Product">
      <a href="/">Miners</a>
      <a href="/answer">Answer</a>
      <a href="/analytics">Analytics</a>
      <a href="/docs">Docs</a>
    </nav>
  </div>
  <div>
    <div class="fnav-title">Elsewhere</div>
    <nav aria-label="External">
      <a href="https://docs.flop.network" target="_blank" rel="noopener">FLOP docs ↗</a>
      <a href="https://explorer.devnet.flop.network" target="_blank" rel="noopener">Explorer ↗</a>
      <a href="https://flop.finance/airdrop/" target="_blank" rel="noopener">Airdrop terms ↗</a>
    </nav>
  </div>
  <div>
    <div class="fnav-title">Project</div>
    <nav aria-label="Project">
      <a href="https://x.com/Flopscoutcom" target="_blank" rel="noopener">X  @Flopscoutcom ↗</a>
      <a href="https://github.com/0xBobaAI/flop-scout" target="_blank" rel="noopener">GitHub ↗</a>
    </nav>
    <div class="fnav-title" style="margin-top:18px">Team</div>
    <nav aria-label="Team">
      <a href="https://x.com/bingobongo_0" target="_blank" rel="noopener">X  @bingobongo_0 ↗</a>
      <a href="https://t.me/iLiaBR" target="_blank" rel="noopener">Telegram  @iLiaBR ↗</a>
    </nav>
  </div>
</footer>'''


def wallet_dialog() -> str:
    """The wallet modal exists on every page; the page's script fills it in."""
    return '''<dialog id="walletDlg">
  <div class="dhead"><h2>Agent wallet</h2><button class="x" data-close aria-label="Close" type="button">×</button></div>
  <div class="dbody" id="walletBody"></div>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>'''


def theme_script() -> str:
    """Toggle persists the choice; the CSS honours it over the system default."""
    return '''<script>(function(){
  var b=document.getElementById('themeBtn');if(!b)return;
  b.addEventListener('click',function(){
    var root=document.documentElement;
    var sys=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
    var cur=root.dataset.theme||sys;
    var next=cur==='dark'?'light':'dark';
    root.dataset.theme=next;
    try{localStorage.setItem('flopScoutTheme',next)}catch(e){}
    document.querySelectorAll('meta[name="theme-color"]').forEach(function(m){
      m.setAttribute('content',next==='dark'?'#080d13':'#f6f8fb');
    });
  });
})();</script>'''


_STATIC_DIR = __import__('pathlib').Path(__file__).parent / 'static'
_ASSET_V: dict = {}


def asset(name: str) -> str:
    """/static/<name>?v=<content hash>.

    Cloudflare and browsers keep static files for hours, so a deploy used to
    serve the new page with the old script until the cache expired. The hash
    changes exactly when the file does, so a cached copy is only ever reused
    while it is still current. Hashed once per process: deploys restart it.
    """
    v = _ASSET_V.get(name)
    if v is None:
        try:
            import hashlib
            v = hashlib.sha256((_STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        except OSError:
            v = ''
        _ASSET_V[name] = v
    return f'/static/{name}?v={v}' if v else f'/static/{name}'


def page(*, title: str, description: str, path: str, body: str, scripts: list[str],
         active: str = '', body_class: str = '', head_extra: str = '') -> str:
    """Assemble a complete document.

    `scripts` are page-specific modules loaded after wallet.js, which every page
    needs because the topbar exposes the wallet button.
    """
    tags = '\n'.join(f'<script src="{asset(s)}" defer></script>' for s in scripts)
    body_cls = ' class="' + body_class + '"' if body_class else ''
    # A page body may write {{sources}} wherever the live count belongs.
    body = body.replace('{{sources}}', str(SOURCE_COUNT))
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{THEME_BOOT}
{head(title, description, path)}
{head_extra}
</head>
<body{body_cls}>
{topbar(active)}
<main id="main">
{body}
</main>
{footer()}
{wallet_dialog()}
{theme_script()}
<script src="{asset('wallet.js')}" defer></script>
<script src="{asset('agent.js')}" defer></script>
{tags}
</body>
</html>'''
