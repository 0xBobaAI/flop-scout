"""The single owner of the FLOP gateway /v1/models snapshot.

Why this module exists: three places used to fetch the same payload with three
different TTLs and timeouts — the model picker (llm.py, 120 s / 6 s), the miner
table (server.py, 60 s / 8 s) and the router (src_router.py, 120 s / 15 s).
On a flaky upstream that produced three different answers on one screen: the
catalog said 31 active miners while the table on the same page said 0.

One cache, one TTL, one timeout. `data is None` means "never succeeded", which
callers must render as unknown rather than zero.
"""

import asyncio
import os
import time

from netcfg import GATEWAY_URL as FLOP_URL, NETWORK

TTL = 60.0        # a snapshot this fresh is served without touching the network
# Measured on this devnet: the same GET took 0.8s, 3.7s and >12s on successive
# calls, so a short timeout produced false "gateway down" readings. Two
# attempts at 20s absorb the spikes; the retry never runs inside a page load
# that already has data.
TIMEOUT = 20.0
ATTEMPTS = 2
MIN_RETRY = 15.0  # a failing gateway is not retried on every request

_state = {'ts': 0.0, 'data': None, 'err_ts': 0.0}
_lock = None
_refreshing = False

# The last good payload is kept on disk so a restart does not begin with
# nothing: measured on the VPS, the first /api/miners after a deploy waited
# 32 s on a cold gateway call. It is the public model catalog, nothing secret.
_DISK = os.path.join(os.getenv('DATA_DIR') or os.path.dirname(os.path.abspath(__file__)), f'gateway_models.{NETWORK}.json')


def _load_disk() -> None:
    try:
        import json
        with open(_DISK, encoding='utf-8') as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            # The file's age is the payload's age, so TTL logic sees it as
            # stale and refreshes it at the first opportunity.
            _state.update(ts=os.path.getmtime(_DISK), data=payload)
    except (OSError, ValueError):
        pass


def _save_disk(payload: dict) -> None:
    try:
        import json
        tmp = _DISK + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, _DISK)
    except OSError:
        pass


_load_disk()


def _get_lock() -> asyncio.Lock:
    """Created lazily so importing this module never needs a running loop."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def cached() -> dict | None:
    """Last good payload, or None. Never touches the network."""
    return _state['data']


def known() -> bool:
    """True once /v1/models has answered at least once since start."""
    return _state['data'] is not None


def age() -> float | None:
    return (time.time() - _state['ts']) if _state['ts'] else None


def seed(payload: dict) -> None:
    """Test hook: install a snapshot without a network call."""
    _state.update(ts=time.time(), data=payload, err_ts=0.0)


async def _refresh_behind(client) -> None:
    global _refreshing
    try:
        await snapshot(client)
    finally:
        _refreshing = False


async def snapshot(client, force: bool = False, timeout: float = TIMEOUT, wait: bool = True) -> dict | None:
    """Return /v1/models JSON, or None if it has never been fetched successfully.

    Concurrent callers share one request. A failure keeps the previous payload
    (a stale price beats a broken page) and backs off for MIN_RETRY seconds.

    wait=False is for page loads: when a payload exists but is past its TTL, it
    is returned at once and refreshed in the background, instead of holding the
    page for a gateway that has taken 24 s to answer. With no payload at all
    there is nothing to return, so it fetches either way.
    """
    global _refreshing
    now = time.time()
    if not force and _state['data'] is not None and now - _state['ts'] < TTL:
        return _state['data']
    if not force and now - _state['err_ts'] < MIN_RETRY:
        return _state['data']
    if not wait and not force and _state['data'] is not None:
        if not _refreshing:
            _refreshing = True
            asyncio.create_task(_refresh_behind(client))
        return _state['data']

    async with _get_lock():
        # Re-check inside the lock: a burst of page loads must not fan out.
        now = time.time()
        if not force and _state['data'] is not None and now - _state['ts'] < TTL:
            return _state['data']
        if not force and now - _state['err_ts'] < MIN_RETRY:
            return _state['data']
        last = None
        for attempt in range(ATTEMPTS):
            try:
                r = await client.get(f'{FLOP_URL}/models', timeout=timeout)
                r.raise_for_status()
                payload = r.json()
                if not isinstance(payload, dict):
                    raise ValueError(f'unexpected /models payload: {type(payload).__name__}')
                _state.update(ts=time.time(), data=payload, err_ts=0.0)
                _save_disk(payload)
                return _state['data']
            except Exception as e:
                last = e
        _state['err_ts'] = time.time()
        # repr, not str: a ReadTimeout has an empty message and used to log as
        # "gateway /models failed:" with nothing after it.
        print('gateway /models failed:', repr(last))
        return _state['data']
