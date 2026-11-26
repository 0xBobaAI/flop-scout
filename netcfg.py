"""Which FLOP network Scout reads. One place, set from the environment.

FLOP_NETWORK picks the defaults for every endpoint (explorer, RPC, gateway,
faucet). Individual FLOP_*_URL variables still override one endpoint each.
When the explorer changes, the session history notices and archives the old
record, so devnet and testnet sessions are never mixed.
"""
import os

NETWORK = os.getenv('FLOP_NETWORK', 'devnet').strip().lower()
if NETWORK not in ('devnet', 'testnet'):
    NETWORK = 'devnet'
LABEL = 'Testnet' if NETWORK == 'testnet' else 'devnet'

_DEFAULTS = {
    'devnet': {
        'explorer': 'https://explorer.devnet.flop.network',
        'rpc': 'https://rpc.devnet.flop.network',
        'gateway': 'https://api.flop.network/v1',
    },
    'testnet': {
        'explorer': 'https://testnet.flopscan.io',
        'rpc': 'https://testnet.flop.network',
        'gateway': 'https://gateway.testnet.flop.network/v1',
    },
}[NETWORK]

EXPLORER = os.getenv('FLOP_EXPLORER_URL', _DEFAULTS['explorer']).rstrip('/')
EXPLORER_API = f'{EXPLORER}/api'
RPC_URL = os.getenv('FLOP_RPC_URL', _DEFAULTS['rpc']).rstrip('/')
GATEWAY_URL = os.getenv('FLOP_BASE_URL', _DEFAULTS['gateway']).rstrip('/')

# Devnet faucet: POST /fund, 25 FLOP. Its own host since Testnet opened.
FAUCET_URL = os.getenv('FLOP_FAUCET_URL', 'https://faucet.devnet.flop.network').rstrip('/')
# Testnet faucet: signed agent claims, 10 FLOP per wallet per rolling 24h.
TESTNET_FAUCET_URL = os.getenv('FLOP_TESTNET_FAUCET_URL', 'https://faucet.flop.network').rstrip('/')
TESTNET_GENESIS = '0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800'

# Wallet balances are read on both networks.
RPC_URLS = {
    'devnet': os.getenv('FLOP_DEVNET_RPC_URL', 'https://rpc.devnet.flop.network').rstrip('/'),
    'testnet': os.getenv('FLOP_TESTNET_RPC_URL', 'https://testnet.flop.network').rstrip('/'),
}
