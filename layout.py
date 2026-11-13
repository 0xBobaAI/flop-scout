"""Shared page shell.

Every page is a section of one product, so the topbar, footer and wallet dialog
live here instead of being copy-pasted into each template. A page module only
supplies its own head metadata and body markup.

Keeping the nav in one place means adding a section is a one-line change and the
"active" state can never drift between pages.
"""

NAV = [
    ('/', 'Answer', 'Answers'),
    ('/miners', 'Miners', 'Miners'),
    ('/pricing', 'Pricing', 'Pricing'),
    ('/api', 'API', 'API'),
    ('/about', 'About', 'About'),
]

# Inline SVG so the logo needs no extra request and inherits currentColor.
LOGO = (
    '<svg viewBox="0 0 40 40" fill="none" aria-hidden="true">'
    '<path d="M20 3 36 12v17L20 38 4 29V12L20 3Z" stroke="currentColor" stroke-width="1.6"/>'
    '<path d="m26.5 12-4.2 11-10.8 4.8 4.3-11.2L26.5 12Z" fill="#007f80"/>'
    '<path d="m26.5 12-10.7 4.6 6.5 6.4 4.2-11Z" fill="currentColor"/>'
    '<circle cx="20" cy="20" r="2" fill="var(--paper)"/>'
    '</svg>'
)


def head(title: str, description: str, path: str = '/') -> str:
    """Everything inside <head> except the charset/viewport boilerplate."""
    canon = f'https://flopscout.com{path}' if path != '/' else 'https://flopscout.com/'
    return f'''<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canon}">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="manifest" href="/static/site.webmanifest">
<meta name="theme-color" content="#f5f7fb">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{canon}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="/static/app.css">'''


def topbar(active: str = '') -> str:
    # The class attribute is built outside the f-string: Python 3.10 (the VPS)
    # rejects a backslash inside an f-string expression.
    cls = ' class="on"'
    items = ''.join(
        '<a href="%s"%s>%s</a>' % (href, cls if href == active else '', label)
        for href, label, _ in NAV
    )
    return f'''<header class="topbar">
  <a class="brand" href="/" aria-label="Flop Scout home">
    {LOGO}
    <span>flop</span><span class="lite">scout</span><span class="alpha">alpha</span>
  </a>
  <nav class="nav" aria-label="Sections">{items}</nav>
  <button class="btn wallet-pill" id="walletBtn" type="button"><i></i><span>Connect wallet</span></button>
</header>'''


def footer() -> str:
    return '''<footer class="foot">
  <div>
    <p>Independent tool built on the FLOP devnet. Not affiliated with FLOP Labs.</p>
    <p class="fine">Devnet activity earns nothing. Answers can be wrong — check the sources.</p>
  </div>
  <nav aria-label="External">
    <a href="https://docs.flop.network" target="_blank" rel="noopener">FLOP docs</a>
    <a href="https://flop.finance/airdrop/" target="_blank" rel="noopener">Airdrop terms</a>
    <a href="/api">API</a>
  </nav>
</footer>'''


def wallet_dialog() -> str:
    """The wallet modal exists on every page; the page's script fills it in."""
    return '''<dialog id="walletDlg">
  <div class="dhead"><h2>Agent wallet</h2><button class="x" data-close aria-label="Close" type="button">×</button></div>
  <div class="dbody" id="walletBody"></div>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>'''


def page(*, title: str, description: str, path: str, body: str, scripts: list[str],
         active: str = '', body_class: str = '', head_extra: str = '') -> str:
    """Assemble a complete document.

    `scripts` are page-specific modules loaded after wallet.js, which every page
    needs because the topbar exposes the wallet button.
    """
    tags = '\n'.join(f'<script src="/static/{s}" defer></script>' for s in scripts)
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{head(title, description, path)}
{head_extra}
</head>
<body{' class="' + body_class + '"' if body_class else ''}>
{topbar(active)}
{body}
{footer()}
{wallet_dialog()}
<script src="/static/wallet.js" defer></script>
<script src="/static/agent.js" defer></script>
{tags}
</body>
</html>'''
