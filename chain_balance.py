"""Live on-chain FLOP account balance via Substrate JSON-RPC (`state_getStorage`).

Why RPC rather than the explorer's `/api/accounts/{address}`:
Measured on the devnet indexer, `/api/accounts` returns `"balance": "0",
"liveBalance": false` for funded wallets because live balance indexing is off.
Querying `System::Account` directly on the node RPC returns the real free,
reserved and frozen balances in ~100 ms with zero external dependencies.
"""

import hashlib
import struct
import time

import httpx

import netcfg

B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
WEI = 10**18
TTL = 12.0   # ~2 blocks; avoids hammering the public RPC on repeated modal opens
_cache: dict[str, tuple[float, dict]] = {}


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        idx = B58.find(ch)
        if idx < 0:
            raise ValueError('bad base58 character')
        n = n * 58 + idx
    full = n.to_bytes((n.bit_length() + 7) // 8, 'big') if n else b''
    pad = len(s) - len(s.lstrip('1'))
    return b'\x00' * pad + full


def ss58_pubkey(address: str) -> bytes:
    """Decode an SS58 address and verify its 2-byte blake2b checksum."""
    addr = (address or '').strip()
    if not (45 <= len(addr) <= 50):
        raise ValueError('not a valid SS58 address')
    raw = _b58decode(addr)
    hdr_len = 1 if raw[0] < 64 else 2
    if len(raw) != hdr_len + 32 + 2:
        raise ValueError('unexpected SS58 payload length')
    body, csum = raw[:-2], raw[-2:]
    expect = hashlib.blake2b(b'SS58PRE' + body, digest_size=64).digest()[:2]
    if csum != expect:
        raise ValueError('SS58 checksum mismatch')
    return raw[hdr_len:hdr_len + 32]


def _xxhash64(data: bytes, seed: int) -> bytes:
    p1 = 11400714785074694791
    p2 = 14029467366897019727
    p3 = 1609587929392839161
    p4 = 9650029242287828579
    p5 = 2870177450012600261
    m = 0xFFFFFFFFFFFFFFFF
    rotl = lambda x, r: ((x << r) | (x >> (64 - r))) & m
    n, p = len(data), 0
    if n >= 32:
        v1, v2, v3, v4 = (seed + p1 + p2) & m, (seed + p2) & m, seed & m, (seed - p1) & m
        while p <= n - 32:
            k0, k1, k2, k3 = struct.unpack_from('<QQQQ', data, p)
            v1 = (rotl((v1 + k0 * p2) & m, 31) * p1) & m
            v2 = (rotl((v2 + k1 * p2) & m, 31) * p1) & m
            v3 = (rotl((v3 + k2 * p2) & m, 31) * p1) & m
            v4 = (rotl((v4 + k3 * p2) & m, 31) * p1) & m
            p += 32
        h = (rotl(v1, 1) + rotl(v2, 7) + rotl(v3, 12) + rotl(v4, 18)) & m
        for v in (v1, v2, v3, v4):
            v = (rotl((v * p2) & m, 31) * p1) & m
            h = (((h ^ v) * p1) + p4) & m
    else:
        h = (seed + p5) & m
    h = (h + n) & m
    while p <= n - 8:
        k = (rotl((struct.unpack_from('<Q', data, p)[0] * p2) & m, 31) * p1) & m
        h = ((rotl(h ^ k, 27) * p1) + p4) & m
        p += 8
    if p <= n - 4:
        k = struct.unpack_from('<I', data, p)[0]
        h = ((rotl(h ^ ((k * p1) & m), 23) * p2) + p3) & m
        p += 4
    while p < n:
        h = (rotl(h ^ ((data[p] * p5) & m), 11) * p1) & m
        p += 1
    h ^= h >> 33
    h = (h * p2) & m
    h ^= h >> 29
    h = (h * p3) & m
    h ^= h >> 32
    return struct.pack('<Q', h)


def _twox128(data: bytes) -> bytes:
    return _xxhash64(data, 0) + _xxhash64(data, 1)


_SYSTEM_ACCOUNT_PREFIX = _twox128(b'System') + _twox128(b'Account')


def _storage_key(pubkey: bytes) -> str:
    return '0x' + (_SYSTEM_ACCOUNT_PREFIX + hashlib.blake2b(pubkey, digest_size=16).digest() + pubkey).hex()


def _decode_account(hexval: str | None) -> dict:
    if not hexval or not hexval.startswith('0x') or len(hexval) < 2 + 64 * 2:
        return {'nonce': 0, 'free_raw': '0', 'reserved_raw': '0', 'frozen_raw': '0',
                'free_flop': 0.0, 'reserved_flop': 0.0, 'frozen_flop': 0.0, 'total_flop': 0.0}
    b = bytes.fromhex(hexval[2:])
    nonce = struct.unpack_from('<I', b, 0)[0]
    u128 = lambda off: int.from_bytes(b[off:off + 16], 'little')
    free, reserved, frozen = u128(16), u128(32), u128(48)
    return {
        'nonce': nonce,
        'free_raw': str(free),
        'reserved_raw': str(reserved),
        'frozen_raw': str(frozen),
        'free_flop': round(free / WEI, 4),
        'reserved_flop': round(reserved / WEI, 4),
        'frozen_flop': round(frozen / WEI, 4),
        'total_flop': round((free + reserved) / WEI, 4),
    }


async def get_balance(client: httpx.AsyncClient, address: str) -> dict:
    pub = ss58_pubkey(address)
    addr = address.strip()
    now = time.time()
    hit = _cache.get(addr)
    if hit and now - hit[0] < TTL and hit[1].get('source') == netcfg.RPC_URL:
        return {**hit[1], 'cached': True}

    r = await client.post(netcfg.RPC_URL, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'state_getStorage', 'params': [_storage_key(pub)],
    }, timeout=12)
    r.raise_for_status()
    j = r.json()
    if 'error' in j:
        raise RuntimeError(j['error'].get('message', 'RPC error'))
    out = {'address': addr, **_decode_account(j.get('result')), 'source': netcfg.RPC_URL, 'cached': False}
    _cache[addr] = (now, out)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
            _cache.pop(k, None)
    return out
