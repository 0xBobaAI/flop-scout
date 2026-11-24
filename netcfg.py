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
FAUCET_URL = os.getenv('FLOP_FAUCET_URL', 'https://faucet.flop.network').rstrip('/')
