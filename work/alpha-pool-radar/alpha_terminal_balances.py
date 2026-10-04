"""Read-only, single-wallet background cache for the terminal server.

Create one Collector per server. snapshot(wallet) returns two independent row
copies immediately and selects the active wallet. First results are unavailable
until collected. close() stops refreshes; no network or threads start on import.
Only public RPC URLs are selected from okx-live-config.json. No signing,
credentials, environment RPC fallbacks, file writes, or trading APIs are used.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path.home() / '.config' / 'alpha-radar'
BSC_USDT = '0x55d398326f99059ff775485246999027b3197955'
CHAINS = {'bsc': 'BNB', 'robinhood': 'ETH'}
CHAIN_IDS = {'bsc': 56, 'robinhood': 4663}
MAX_RESPONSE = 128 * 1024


def _fetch_json(url, payload, timeout):
    data = json.dumps(payload).encode('ascii') if payload is not None else None
    request = Request(url, data=data, headers={
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'User-Agent': 'alpha-terminal-balances/1',
    })
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError('response_too_large')
    return json.loads(body)


def _public_url(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
        if (parsed.scheme in {'https', 'http'} and parsed.hostname and not
                (parsed.username or parsed.password or parsed.query or parsed.fragment)):
            return value
    except ValueError:
        pass
    return None


def _rpc_urls(root):
    try:
        with (root / 'okx-live-config.json').open('rb') as stream:
            body = stream.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            return {}
        raw = json.loads(body.decode('utf-8-sig'))
        if isinstance(raw, dict):
            return {chain: _public_url(raw.get(f'{chain}_rpc_url')) for chain in CHAINS}
    except (OSError, ValueError):
        pass
    return {}


def _wallet(value):
    if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        value = value.lower()
        return value if value != '0x' + '0' * 40 else None
    return None


class Collector:
    """snapshot(wallet) -> list[dict]; close(timeout=1) -> None.

    interval is refresh start-to-start seconds (default 20), timeout is per HTTP
    request (default 4), stale_after is maximum cached age (default 60).
    fetch_json(url, payload_or_none, timeout) and clock() are injectable for tests.
    Native USD values use the public USDT ticker as a USD estimate, not an FX feed.
    """

    def __init__(self, root=ROOT, *, interval=20.0, timeout=4.0, stale_after=60.0,
                 fetch_json=None, clock=time.time):
        for value in (interval, timeout, stale_after):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('timings_must_be_positive_finite')
        self.root = Path(root)
        self.interval = interval
        self.timeout = timeout
        self.stale_after = stale_after
        self._fetch = fetch_json or _fetch_json
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.thread_name = f'alpha-terminal-balances-{id(self):x}'
        self._wallet = None
        self._generation = 0
        self._cache = {chain: {} for chain in CHAINS}
        self._failed = {chain: True for chain in CHAINS}

    def snapshot(self, wallet):
        """Select one wallet; never wait for network, disk I/O, or refresh completion."""
        wallet = _wallet(wallet)
        with self._lock:
            if wallet != self._wallet:
                self._wallet = wallet
                self._generation += 1
                self._cache = {chain: {} for chain in CHAINS}
                self._failed = {chain: True for chain in CHAINS}
                self._wake.set()
            if wallet and self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
                self._thread.start()
            now = self._clock()
            return [self._row(chain, now) for chain in CHAINS]

    def _row(self, chain, now):
        cached = self._cache[chain]
        values = {key: value for key, (value, _) in cached.items()}
        oldest = min((stamp for _, stamp in cached.values()), default=None)
        required = {'native_balance', 'native_price_usd'}
        if chain == 'bsc':
            required.add('usdt_balance')
        stale = (self._failed[chain] or self._stop.is_set() or not required.issubset(values)
                 or oldest is None or not 0 <= now - oldest <= self.stale_after)
        native, price = values.get('native_balance'), values.get('native_price_usd')
        usd = native * price if native is not None and price is not None else None
        if usd is not None and not math.isfinite(usd):
            usd = None
            stale = True
        return {
            'chain': chain, 'native_symbol': CHAINS[chain],
            'native_balance': native, 'native_price_usd': price, 'native_balance_usd': usd,
            'usdt_balance': values.get('usdt_balance'),
            'updated_at': datetime.fromtimestamp(oldest, timezone.utc).isoformat() if oldest is not None else None,
            'status': 'unavailable' if not cached else 'stale' if stale else 'fresh',
        }

    def _rpc_balance(self, url, wallet, token=False):
        params = [{'to': BSC_USDT, 'data': '0x70a08231' + wallet[2:].rjust(64, '0')}, 'latest'] if token else [wallet, 'latest']
        raw = self._fetch(url, {'jsonrpc': '2.0', 'id': 1,
                               'method': 'eth_call' if token else 'eth_getBalance',
                               'params': params}, self.timeout)
        if not isinstance(raw, dict) or raw.get('error') is not None or raw.get('id') != 1:
            raise ValueError('rpc_unavailable')
        value = raw.get('result')
        pattern = r'0x[0-9a-fA-F]{64}' if token else r'0x[0-9a-fA-F]{1,64}'
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError('invalid_balance')
        return int(value, 16) / 10 ** 18, self._clock()

    def _price(self, symbol):
        instrument = symbol + '-USDT'
        raw = self._fetch('https://www.okx.com/api/v5/market/ticker?instId=' + instrument,
                          None, self.timeout)
        if not isinstance(raw, dict) or raw.get('code') != '0':
            raise ValueError('ticker_unavailable')
        rows = raw.get('data')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('invalid_ticker')
        row = rows[0]
        if row.get('instId') != instrument or type(row.get('last')) not in (str, int, float):
            raise ValueError('invalid_ticker')
        price = float(row['last'])
        stamp = float(row['ts']) / 1000
        age = self._clock() - stamp
        if not math.isfinite(price) or price <= 0 or not math.isfinite(stamp) or not 0 <= age <= self.stale_after:
            raise ValueError('stale_or_invalid_ticker')
        return price, stamp

    def _collect(self, chain, wallet, url):
        if not url:
            return {}, True
        try:
            raw = self._fetch(url, {'jsonrpc': '2.0', 'id': 1,
                                    'method': 'eth_chainId', 'params': []}, self.timeout)
            value = raw.get('result') if isinstance(raw, dict) else None
            if (not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value)
                    or raw.get('error') is not None or type(raw.get('id')) is not int
                    or raw['id'] != 1 or int(value, 16) != CHAIN_IDS[chain]):
                return {}, True
        except Exception:
            return {}, True
        values, failed = {}, False
        readers = [('native_balance', lambda: self._rpc_balance(url, wallet)),
                   ('native_price_usd', lambda: self._price(CHAINS[chain]))]
        if chain == 'bsc':
            readers.append(('usdt_balance', lambda: self._rpc_balance(url, wallet, token=True)))
        for name, read in readers:
            if self._stop.is_set():
                return values, True
            try:
                values[name] = read()
            except Exception:
                # Exceptions may include endpoint credentials or provider bodies.
                failed = True
        return values, failed

    def _run(self):
        delay = None
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                break
            started = time.monotonic()
            with self._lock:
                wallet, generation = self._wallet, self._generation
            if wallet is None:
                delay = None
                continue
            urls = _rpc_urls(self.root)
            for chain in CHAINS:
                with self._lock:
                    if generation != self._generation or self._stop.is_set():
                        break
                values, failed = self._collect(chain, wallet, urls.get(chain))
                with self._lock:
                    if generation == self._generation and not self._stop.is_set():
                        self._cache[chain].update(values)
                        self._failed[chain] = failed
            delay = max(.01, self.interval - (time.monotonic() - started))

    def close(self, timeout=1.0):
        """Stop further requests; bound shutdown waiting for the current request."""
        with self._lock:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
