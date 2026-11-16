"""Registry of search sources and task presets.

Design principles:
  - Default sources must give real value with zero keys.
  - Sources that require keys are gated (shown in UI, grayed until key provided).
  - FLOP live + explorer data = core differentiator — always present in FLOP preset.
  - Multiple web routes = smart router can pick fastest/best available.
  - Adding a source = one function + one entry here. UI picks it up automatically.
"""
import src_web as w
import src_explorer as ex

SOURCES = {
    # ── Web: free routes ──────────────────────────────────────────────────────
    'keenable': {
        'label': 'Keenable',
        'group': 'Web',
        'fn': w.keenable,
        'default': True,
        'hint': 'Public web index · no key required · primary route',
    },
    'jina': {
        'label': 'Jina Reader',
        'group': 'Web',
        'fn': w.jina,
        'key': 'jina',
        'hint': 'Readable page text · Jina token required · fallback route',
    },
    'web_read': {
        'label': 'Deep page read',
        'group': 'Web',
        'fn': w.web_read,
        'hint': 'Reads full page content via Scout reader · passage extraction',
    },
    'page': {
        'label': 'Page reader',
        'group': 'Web',
        'fn': w.page_source,
        'hint': 'Reads the URLs written in your question · no key required',
    },

    # ── Web: key-gated routes (Builder tier) ─────────────────────────────────
    'exa': {
        'label': 'Exa',
        'group': 'Web (Builder)',
        'fn': w.exa,
        'key': 'exa',
        'hint': 'Neural search · highlights · own Exa key',
    },
    'tavily': {
        'label': 'Tavily',
        'group': 'Web (Builder)',
        'fn': w.tavily,
        'key': 'tavily',
        'hint': 'AI-native search, structured results · own Tavily key',
    },
    'brave': {
        'label': 'Brave Search',
        'group': 'Web (Builder)',
        'fn': w.brave,
        'key': 'brave',
        'hint': 'Independent Google-free index · own Brave key',
    },
    'firecrawl': {
        'label': 'Firecrawl',
        'group': 'Web (Builder)',
        'fn': w.firecrawl,
        'key': 'firecrawl',
        'hint': 'Full-page scraping and search · own Firecrawl key',
    },
    'serper': {
        'label': 'Serper (Google)',
        'group': 'Web (Builder)',
        'fn': w.serper,
        'key': 'serper',
        'hint': 'Google Search results · own Serper key',
    },

    # ── FLOP network live (gateway) ───────────────────────────────────────────
    'flop_live': {
        'label': 'FLOP network live',
        'group': 'FLOP',
        'fn': w.flop_live,
        'default': True,
        'hint': 'Models, prices, capacity, chain health · real-time',
    },
    'flop_miners': {
        'label': 'FLOP miner performance',
        'group': 'FLOP',
        'fn': w.flop_miners_live,
        'hint': 'GPU, stake, settlement rate per miner · devnet explorer',
    },
    'flop_stats': {
        'label': 'FLOP network stats',
        'group': 'FLOP',
        'fn': w.flop_network_stats,
        'hint': 'Block height, TPS, validator/agent/miner counts',
    },

    # ── FLOP Explorer (on-chain data) ─────────────────────────────────────────
    'explorer_overview': {
        'label': 'Explorer: overview',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_overview,
        'hint': 'Blocks, TPS, FLOP locked, finality, participants count',
    },
    'explorer_transfers': {
        'label': 'Explorer: FLOP transfers',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_transfers,
        'hint': 'Who sent FLOP to whom — recent on-chain transfers',
    },
    'explorer_richlist': {
        'label': 'Explorer: rich list',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_richlist,
        'hint': 'Top FLOP holders by balance with % of supply',
    },
    'explorer_spend': {
        'label': 'Explorer: inference spend',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_spend,
        'hint': 'Total compute purchased, top agent spenders',
    },
    'explorer_reliability': {
        'label': 'Explorer: miner reliability',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_reliability,
        'hint': 'Settled vs timeout per miner — the routing signal',
    },
    'explorer_channels': {
        'label': 'Explorer: compute channels',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_channels,
        'hint': 'Active agent→miner payment channels right now',
    },
    'explorer_agents': {
        'label': 'Explorer: agent wallets',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_agents,
        'hint': 'Registered agent wallets with caps and status',
    },
    'explorer_capacity': {
        'label': 'Explorer: GPU capacity',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_capacity,
        'hint': 'Total GPU inventory by model (A100, RTX 4090…)',
    },
    'explorer_priceindex': {
        'label': 'Explorer: price index',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_priceindex,
        'hint': 'VWAP inference price, trades, HHI concentration',
    },
    'explorer_rewards': {
        'label': 'Explorer: rewards',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_rewards,
        'hint': 'Total FLOP distributed to miners and validators',
    },
    'explorer_staking': {
        'label': 'Explorer: staking',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_staking,
        'hint': 'Staking ratio, validators, delegators, 24h rewards',
    },
    'explorer_address': {
        'label': 'Explorer: address lookup',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_address,
        'hint': 'Balance, role, reliability for a specific FLOP address',
    },
    'explorer_transactions': {
        'label': 'Explorer: transactions',
        'group': 'FLOP Explorer',
        'fn': ex.explorer_transactions,
        'hint': 'Recent on-chain transactions and extrinsics',
    },

    # ── Dev ───────────────────────────────────────────────────────────────────
    'github': {
        'label': 'GitHub',
        'group': 'Developers',
        'fn': w.github,
        'hint': 'Repository search · find libraries and tools',
    },
    'hn': {
        'label': 'Hacker News',
        'group': 'Developers',
        'fn': w.hackernews,
        'hint': 'Developer discussions · opinions, not facts',
    },

    # ── Crypto ────────────────────────────────────────────────────────────────
    'defillama': {
        'label': 'DefiLlama',
        'group': 'Crypto',
        'fn': w.defillama,
        'hint': 'DeFi protocol TVL, chains, 7d change · free',
    },
    'crypto': {
        'label': 'CoinGecko',
        'group': 'Crypto',
        'fn': w.crypto,
        'hint': 'Live token prices, market cap, 24h change',
    },

    # ── Research ─────────────────────────────────────────────────────────────
    'wikipedia': {
        'label': 'Wikipedia',
        'group': 'Research',
        'fn': w.wikipedia,
        'hint': 'Canonical article for an entity or term · free',
    },
    'arxiv': {
        'label': 'arXiv',
        'group': 'Research',
        'fn': w.arxiv,
        'hint': 'Academic papers · research and ML questions only',
    },
}

PRESETS = {
    'auto': {
        'label': 'Auto',
        'icon': '✦',
        'sources': ['auto'],
        'description': 'Scout picks free sources from your question. Never adds a paid one.',
    },
    'flop': {
        'label': 'FLOP',
        'icon': '⬡',
        'sources': ['flop_live', 'flop_miners', 'flop_stats', 'explorer_overview', 'keenable'],
        'description': 'Full FLOP network picture: live gateway + on-chain explorer + web.',
    },
    'crypto': {
        'label': 'Crypto',
        'icon': '◈',
        'sources': ['crypto', 'defillama', 'keenable'],
        'description': 'Live token prices from CoinGecko plus web context.',
    },
    'dev': {
        'label': 'Code',
        'icon': '{ }',
        'sources': ['github', 'keenable', 'hn'],
        'description': 'GitHub repos, dev discussions and web search.',
    },
    'research': {
        'label': 'Research',
        'icon': '∂',
        'sources': ['arxiv', 'wikipedia', 'keenable', 'web_read'],
        'description': 'Academic papers + deep web reading.',
    },
}


# ── Optional sources ─────────────────────────────────────────────────────────
# `web_read` needs an extraction backend (SCOUT_URL). Drop it from the registry
# and from every preset when it is not configured, so the UI never offers a
# source that can only return an empty list.
if not w.SCOUT:
    SOURCES.pop('web_read', None)
    for _p in PRESETS.values():
        _p['sources'] = [s for s in _p['sources'] if s != 'web_read']
