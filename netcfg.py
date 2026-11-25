"""Which FLOP network Scout reads. One place, set from the environment.

The devnet is the default. When the testnet opens, set FLOP_EXPLORER_URL (and
FLOP_BASE_URL for the gateway, read in flop_gateway.py) and restart: the
session history notices the change and starts over, so devnet and testnet
sessions are never mixed in one record.
"""
import os

EXPLORER = os.getenv('FLOP_EXPLORER_URL', 'https://explorer.devnet.flop.network').rstrip('/')
EXPLORER_API = f'{EXPLORER}/api'
RPC_URL = os.getenv('FLOP_RPC_URL', 'https://rpc.devnet.flop.network').rstrip('/')
# Devnet faucet (POST /fund) moved to its own host when Testnet opened.
FAUCET_URL = os.getenv('FLOP_FAUCET_URL', 'https://faucet.devnet.flop.network').rstrip('/')
# Testnet faucet: signed agent claims, 10 FLOP per wallet per rolling 24h.
TESTNET_FAUCET_URL = os.getenv('FLOP_TESTNET_FAUCET_URL', 'https://faucet.flop.network').rstrip('/')
TESTNET_GENESIS = '0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800'

# Balances are read per network. The faucet now serves Testnet while the
# gateway and miners still run on Devnet, so wallets show both.
RPC_URLS = {
    'devnet': os.getenv('FLOP_DEVNET_RPC_URL', 'https://rpc.devnet.flop.network').rstrip('/'),
    'testnet': os.getenv('FLOP_TESTNET_RPC_URL', 'https://testnet.flop.network').rstrip('/'),
}
