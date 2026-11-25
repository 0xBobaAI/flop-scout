"""Claim Testnet FLOP for Scout's own server wallet (run daily by systemd).

Scout's wallet pays for anonymous answers. On Testnet the faucet grants
10 FLOP per wallet per rolling 24h and only after the previous grant was
spent (`rolloverRequiresSpend`), so a refusal while nothing was spent is the
normal case and is logged, not retried.

Wallet: /etc/flop-scout-testnet-wallet.json (chmod 600, never in git).
"""
import json
import os
import sys
import time

import httpx
from substrateinterface import Keypair

WALLET = os.getenv('FLOP_TESTNET_WALLET', '/etc/flop-scout-testnet-wallet.json')
FAUCET = os.getenv('FLOP_TESTNET_FAUCET_URL', 'https://faucet.flop.network').rstrip('/')
GENESIS = '0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800'
UA = {'User-Agent': 'flop-scout/0.8 (+https://flopscout.com)'}


def main() -> int:
    w = json.load(open(WALLET))
    kp = Keypair.create_from_mnemonic(w['mnemonic'], ss58_format=42)
    if kp.ss58_address != w.get('address', kp.ss58_address):
        print('wallet file address mismatch', file=sys.stderr)
        return 2
    with httpx.Client(timeout=45, headers=UA) as c:
        ch = c.get(f'{FAUCET}/chain').json()
        if ch.get('network') != 'testnet' or ch.get('genesisHash') != GENESIS:
            print(f'faucet serves {ch.get("network")} {ch.get("genesisHash")}; stop')
            return 1
        st = (c.get(f'{FAUCET}/limits').json().get('faucet') or {}).get('status')
        if st != 'open':
            print(f'faucet status {st}; skip')
            return 0
        nonce = str(int(time.time() * 1000))
        msg = f'flop-faucet-agent/v1|{GENESIS}|{kp.ss58_address}|{nonce}'
        sig = '0x' + kp.sign(msg.encode('utf-8')).hex()
        r = c.post(f'{FAUCET}/v1/testnet/agent-claims',
                   json={'address': kp.ss58_address, 'nonce': nonce, 'signature': sig})
        try:
            j = r.json()
        except Exception:
            j = {'error': r.text[:200]}
        if r.status_code < 300 and j.get('success'):
            print(f'OK +{j.get("amount")} FLOP to {kp.ss58_address} tx {j.get("txHash")}')
        else:
            print(f'refused {r.status_code}: {j.get("reason") or ""} {j.get("error") or ""}'.strip())
    return 0


if __name__ == '__main__':
    sys.exit(main())
