"""Renew FLOP devnet gateway key.

Runs every hour (or manually). Tries to enroll a fresh gateway key for the
wallet in .wallet.json. On success writes the new key + expiry.

The FLOP faucet has a 24h address cooldown and a daily wallet enrollment cap,
so this often fails with wallet_enrollment_cap_reached — that's expected.
It will succeed once the daily cap resets.

Usage:  python renew_key.py
Env:    WALLET_MNEMONIC (12-word phrase) — required to sign the challenge.
        FLOP_ENROLL_PURPOSES — comma-separated enrollment purposes to try in
        order (default `hermes-tools-v1,` — the trailing empty entry is the
        legacy default allowance). The gateway scopes each key to one model;
        that model is stored as `key_model` so the server sends it.

Exit codes: 0 = key valid or a transient gateway condition (daily cap,
enrollment temporarily unavailable) that the next hourly run will retry;
2 = this wallet's allowance is permanently expired/exhausted — a human must
act (new wallet or new purpose); 1 = anything else.
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
FAUCET = os.getenv('FLOP_FAUCET_URL', 'https://faucet.devnet.flop.network').rstrip('/')
GATEWAY = os.getenv('FLOP_BASE_URL', 'https://api.flop.network/v1').rstrip('/')
PURPOSES = [p.strip() for p in os.getenv('FLOP_ENROLL_PURPOSES', 'hermes-tools-v1,').split(',')]

# Gateway conditions that clear on their own; the hourly timer retries them.
TRANSIENT = {'wallet_enrollment_cap_reached', 'wallet_enrollment_unavailable', 'indexer_unavailable'}
# Conditions retrying cannot fix for this wallet + purpose.
PERMANENT = {'devnet_enrollment_expired', 'devnet_allowance_exhausted', 'invalid_enrollment_purpose'}


def _err(r) -> str:
    try:
        return str(r.json().get('error') or r.status_code)
    except Exception:
        return str(r.status_code)


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

        # 2-4) Challenge → sign → enroll, per purpose, until one issues a key
        outcomes = []
        for purpose in PURPOSES:
            label = purpose or 'default'
            params = {'address': addr, **({'purpose': purpose} if purpose else {})}
            r = await c.get(f'{GATEWAY}/enroll/wallet/challenge', params=params)
            if not r.is_success:
                print(f'[{label}] challenge failed: {r.status_code} {r.text[:150]}')
                outcomes.append(_err(r))
                continue
            msg = r.json()['message']
            sig = '0x' + pair.sign(msg).hex()
            r = await c.post(f'{GATEWAY}/enroll/wallet',
                             json={'address': addr, 'message': msg, 'signature': sig})
            if not r.is_success:
                print(f'[{label}] enroll failed: {r.status_code} {r.text[:200]}')
                outcomes.append(_err(r))
                continue

            j = r.json()
            w['gateway_key'] = j['key']
            w['key_expires_at'] = j.get('expires_at', 0)
            w['key_model'] = j.get('model') or ''
            w['key_purpose'] = purpose
            save_wallet(w)
            exp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(w['key_expires_at'] / 1000))
            print(f'OK [{label}] · model {w["key_model"] or "?"} · key {j["key"][:10]}… expires {exp}')
            return

    if any(o in TRANSIENT or o.startswith('5') for o in outcomes):
        # A normal devnet condition, not a failure of this unit: the next hourly
        # run retries. Exiting 1 here filled the journal with red every hour.
        print('Gateway enrollment temporarily unavailable or capped; next hourly run retries.')
        return
    if outcomes and all(o in PERMANENT for o in outcomes):
        print(f'Wallet {addr} cannot enroll for any of {PURPOSES}: {outcomes}. '
              'A new wallet or enrollment purpose is needed.')
        sys.exit(2)
    sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
