"""Renew FLOP devnet gateway key.

Runs every hour (or manually). Tries to enroll a fresh gateway key for the
wallet in .wallet.json. On success writes the new key + expiry.

The FLOP faucet has a 24h address cooldown and a daily wallet enrollment cap,
so this often fails with wallet_enrollment_cap_reached — that's expected.
It will succeed once the daily cap resets.

Usage:  python renew_key.py
Env:    WALLET_MNEMONIC (12-word phrase) — required to sign the challenge.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from substrateinterface import Keypair

BASE = Path(__file__).parent
WALLET = BASE / '.wallet.json'
FAUCET = 'https://faucet.flop.network'
GATEWAY = 'https://api.flop.network/v1'


def load_wallet() -> dict:
    return json.loads(WALLET.read_text()) if WALLET.exists() else {}


def save_wallet(w: dict):
    WALLET.write_text(json.dumps(w, indent=2))

def pair_from_env() -> Keypair:
    m = os.getenv('WALLET_MNEMONIC', '').strip()
    if not m:
        print('WALLET_MNEMONIC env var is required (12-word phrase)')
        sys.exit(1)
    return Keypair.create_from_mnemonic(m, ss58_format=42)


async def main():
    pair = pair_from_env()
    addr = pair.ss58_address

    w = load_wallet()
    if w.get('address') and w['address'] != addr:
        print(f'.wallet.json holds {w["address"]} but WALLET_MNEMONIC derives {addr}.')
        print('Refusing to continue — fix the mnemonic or the file.')
        sys.exit(1)
    if not w:
        # First run on a fresh host: the wallet file is git-ignored, so create it
        # from the mnemonic. The address is public; the phrase stays in env only.
        print(f'Creating .wallet.json for {addr}')
        w = {'address': addr}
        save_wallet(w)

    # A key is only "valid" if the gateway accepts it. Local expiry alone let a
    # revoked key stay in use until its timestamp, with /api/health still green.
    if w.get('gateway_key') and w.get('key_expires_at', 0) > time.time() * 1000:
        async with httpx.AsyncClient(timeout=30) as probe:
            pr = await probe.get(f'{GATEWAY}/keys/me',
                                 headers={'Authorization': f'Bearer {w["gateway_key"]}'})
        if pr.status_code == 200:
            exp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(w['key_expires_at'] / 1000))
            print(f'Key accepted by gateway, valid until {exp}')
            return
        print(f'Key rejected by gateway ({pr.status_code}); enrolling a new one')

    async with httpx.AsyncClient(timeout=30) as c:
        # 1) Check balance — only fund if the wallet is empty (24h address cooldown)
        try:
            r = await c.get(f'https://explorer.devnet.flop.network/api/accounts/{addr}')
            balance = int(r.json().get('balance', 0))
            print(f'balance: {balance / 1e18:.2f} FLOP')
        except Exception:
            balance = None

        if balance is None or balance == 0:
            r = await c.post(f'{FAUCET}/fund', json={'address': addr})
            print(f'faucet: {r.status_code} {r.text[:120]}')
            await asyncio.sleep(12)  # let the tx land
        else:
            print('faucet: skipped (wallet already funded)')

        # 2) Challenge
        r = await c.get(f'{GATEWAY}/enroll/wallet/challenge', params={'address': addr})
        if not r.is_success:
            print(f'challenge failed: {r.status_code} {r.text[:150]}')
            sys.exit(1)
        msg = r.json()['message']

        # 3) Sign
        sig = '0x' + pair.sign(msg).hex()

        # 4) Enroll
        r = await c.post(f'{GATEWAY}/enroll/wallet',
                         json={'address': addr, 'message': msg, 'signature': sig})
        if not r.is_success:
            print(f'enroll failed: {r.status_code} {r.text[:200]}')
            print('Retrying in an hour. The daily wallet enrollment cap resets every 24h.')
            sys.exit(1)

        j = r.json()
        w['gateway_key'] = j['key']
        w['key_expires_at'] = j.get('expires_at', 0)
        save_wallet(w)
        exp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(w['key_expires_at'] / 1000))
        print(f'OK · key {j["key"][:24]}… expires {exp}')


if __name__ == '__main__':
    asyncio.run(main())
