"""Pricing — what a Scout service costs in FLOP.

Two numbers matter here and they are deliberately separate:

  price_flop   What the caller is charged for the service.
  cost_flop    What the inference itself cost, reported by the FLOP gateway
               in usage.cost_flop and paid to the miner.

Scout's price covers search, page reading, routing, citations and the receipt
ledger. The miner's cost is passed through untouched. A caller who brings their
own gateway key pays the miner directly and only pays Scout for the service.

Devnet: nothing is actually debited. `METERING` says how a charge is recorded so
the UI and the ledger can be honest about it, and so switching to real payment
later is a one-line change rather than a migration.

Prices are placeholder-small until there is a real FLOP market to price against.
They live in one table so that decision is a single edit.
"""

from dataclasses import dataclass, asdict

# How charges are handled right now.
#   nominal  — computed, displayed, logged; no transfer is attempted
#   onchain  — reserved through the gateway's connect/grants flow and settled
METERING = 'nominal'

# 1 FLOP is 1e18 wei-like base units on the devnet chain.
WEI = 10**18


@dataclass(frozen=True)
class Price:
    price_flop: float      # what Scout charges for this service
    unit: str              # what one charge buys
    free: bool = False     # available without any charge being computed

    def flop(self, qty: int = 1) -> float:
        return round(self.price_flop * qty, 6)

    def wei(self, qty: int = 1) -> int:
        return int(self.flop(qty) * WEI)


# ── Service catalogue ────────────────────────────────────────────────────────
# Keys match source ids in registry.py where a source is billed per call.
# Anything not listed is free.

SERVICES: dict[str, Price] = {
    # Free routes — no external API cost, so no reason to charge.
    'keenable':     Price(0.0,    'search', free=True),
    'jina':         Price(0.0,    'page read', free=True),
    'page':         Price(0.0,    'URL read', free=True),
    'github':       Price(0.0,    'repo search', free=True),
    'hn':           Price(0.0,    'discussion search', free=True),
    'arxiv':        Price(0.0,    'paper search', free=True),
    'crypto':       Price(0.0,    'price lookup', free=True),
    'defillama':    Price(0.0,    'protocol TVL', free=True),
    'wikipedia':    Price(0.0,    'reference article', free=True),
    'web_read':     Price(0.002,  'page extract'),

    # FLOP-native data — our own indexer work, priced but nominally charged.
    'flop_live':          Price(0.005, 'network snapshot'),
    'flop_miners':        Price(0.005, 'miner detail'),
    'flop_stats':         Price(0.005, 'network stats'),
    'explorer_overview':  Price(0.002, 'on-chain overview'),
    'explorer_transfers': Price(0.002, 'transfer query'),
    'explorer_richlist':  Price(0.002, 'holder query'),
    'explorer_spend':     Price(0.002, 'spend query'),
    'explorer_reliability': Price(0.003, 'reliability query'),
    'explorer_channels':  Price(0.002, 'channel query'),
    'explorer_agents':    Price(0.002, 'agent query'),
    'explorer_capacity':  Price(0.002, 'capacity query'),
    'explorer_priceindex': Price(0.002, 'price index query'),
    'explorer_rewards':   Price(0.002, 'rewards query'),
    'explorer_staking':   Price(0.002, 'staking query'),
    'explorer_address':   Price(0.003, 'address lookup'),
    'explorer_transactions': Price(0.002, 'transaction query'),

    # Paid third-party APIs — these cost us real money upstream.
    'exa':          Price(0.010,  'neural search'),
    'tavily':       Price(0.008,  'AI search'),
    'brave':        Price(0.008,  'index search'),
    'firecrawl':    Price(0.012,  'scrape + search'),
    'serper':       Price(0.006,  'Google results'),
}

# Per-answer overhead: reasoning, citation assembly, receipt ledger.
ANSWER_FEE = Price(0.001, 'answer')

# Compare mode runs a second model over the same sources.
COMPARE_FEE = Price(0.002, 'second model')

# A standalone page read (POST /api/read): no search, no model. The service fee
# for reading, like ANSWER_FEE is for answering. Inside an answer the `page`
# source is free and the answer fee covers it.
READ_FEE = Price(0.001, 'page read')


def price_for(source_id: str) -> Price:
    return SERVICES.get(source_id, FREE)


FREE = Price(0.0, 'source', free=True)


def quote(sources: list[str], compare: bool = False, cache_hit: bool = False) -> dict:
    """Itemised quote for one answer request.

    Returns the line items, the total, and what of that is actually charged
    given the current metering mode. Cached answers are free — the duplicate
    window exists so repeat questions do not manufacture revenue.
    """
    items = []
    if not cache_hit:
        items.append({'id': 'answer', 'label': 'Answer reasoning + citations',
                      'unit': ANSWER_FEE.unit, 'price_flop': ANSWER_FEE.price_flop, 'free': False})
        for s in sources:
            p = price_for(s)
            items.append({'id': s, 'label': s.replace('_', ' '), 'unit': p.unit,
                          'price_flop': p.price_flop, 'free': p.free})
        if compare:
            items.append({'id': 'compare', 'label': 'Compare (second model)',
                          'unit': COMPARE_FEE.unit, 'price_flop': COMPARE_FEE.price_flop, 'free': False})

    total = round(sum(i['price_flop'] for i in items), 6)
    billed = sorted({i['id'] for i in items if i['price_flop'] > 0})
    charged = None if METERING == 'nominal' else total
    return {
        'items': items,
        'total_flop': total,
        'charged_flop': charged,
        'metering': METERING,
        'cache_hit': cache_hit,
        # What an agent needs to budget: the ceiling, and which lines produced it.
        'max_flop': total,
        'billed': billed,
        'free_lines': sorted({i['id'] for i in items if i['price_flop'] == 0}),
    }


def read_quote(cache_hit: bool = False) -> dict:
    """Quote for one standalone page read. A cache hit is free, as with answers."""
    items = [] if cache_hit else [{'id': 'read', 'label': 'Page read', 'unit': READ_FEE.unit,
                                   'price_flop': READ_FEE.price_flop, 'free': False}]
    total = round(sum(i['price_flop'] for i in items), 6)
    return {'items': items, 'total_flop': total,
            'charged_flop': None if METERING == 'nominal' else total,
            'metering': METERING, 'cache_hit': cache_hit}


def preset_quote(presets: dict, key: str, compare: bool = False) -> dict | None:
    """One number for a whole preset, so an agent can budget a task, not a call."""
    p = presets.get(key)
    if not p:
        return None
    q = quote(list(p['sources']), compare=compare)
    return {'preset': key, 'label': p['label'], 'sources': list(p['sources']), **q}


def catalogue() -> dict:
    """Public price list, used by the UI and /api/pricing."""
    return {
        'metering': METERING,
        'answer_fee': asdict(ANSWER_FEE) | {'id': 'answer'},
        'compare_fee': asdict(COMPARE_FEE) | {'id': 'compare'},
        'read_fee': asdict(READ_FEE) | {'id': 'read'},
        'services': [
            {'id': k, **asdict(v)} for k, v in sorted(
                SERVICES.items(), key=lambda kv: (-kv[1].price_flop, kv[0]))
        ],
    }
