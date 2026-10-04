"""Read-only chain evidence and bounded FIFO accounting in a separate sidecar.

One Collector per server: enrich_snapshot(snapshot) never performs disk/network
I/O. Only confirmed snapshot orders are scheduled. Persistent output is confined
to outputs/terminal-accounting.json, never the execution ledger. Native balance
differences are never used. All atomic amounts are decimal strings.

native_spent_atomic is gross wallet native debits plus this transaction's fees;
buy_cost_native_atomic subtracts trace-proven refunds. FIFO includes swap receipt
fees, not separate approval transactions or historical USD exchange rates. Token
inventory reconciliation uses ERC20 logs/balanceOf, not native balance deltas.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from alpha_terminal_balances import ROOT, _rpc_urls
from alpha_terminal_data import SDK_OUTPUTS


CHAIN_IDS = {'bsc': 56, 'robinhood': 4663}
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
MAX_RESPONSE = 1024 * 1024
MAX_RECORDS = 2000
IDENTITY = ('chain', 'wallet', 'token', 'tx_hash', 'side')
ATOMIC_FIELDS = (
    'tx_value_native_atomic', 'execution_gas_native_atomic', 'l1_fee_native_atomic',
    'gas_native_atomic', 'native_spent_atomic', 'native_received_atomic',
    'net_native_received_atomic', 'amount_atomic', 'buy_cost_native_atomic',
    'cost_basis_native_atomic', 'pnl_native_atomic',
)
PUBLIC_FIELDS = (*IDENTITY, *ATOMIC_FIELDS, 'status', 'reason', 'receipt_status',
                 'trace_status', 'fifo_status', 'verified_at', 'time',
                 'block_number', 'transaction_index', 'block_hash', 'fee_scope')
OVERLAY_FIELDS = ('gas_native_atomic', 'native_spent_atomic', 'native_received_atomic',
                  'net_native_received_atomic', 'amount_atomic',
                  'cost_basis_native_atomic', 'pnl_native_atomic')
READ_METHODS = {'eth_chainId', 'eth_getTransactionByHash', 'eth_getTransactionReceipt',
                'eth_getBlockByNumber', 'debug_traceTransaction', 'eth_getLogs', 'eth_call'}


class EvidenceError(ValueError):
    """Static public reason code, never a provider response or endpoint URL."""


def _address(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', value) else None


def _hash(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{64}', value) else None


def _quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value):
        raise EvidenceError('invalid_quantity')
    return int(value, 16)


def _fee(value):
    if type(value) is int and 0 <= value < 2 ** 256:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,78}', value):
        return int(value)
    return _quantity(value)


def _topic(address):
    return '0x' + address[2:].rjust(64, '0')


def _iso(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def _identity(order, wallet=None):
    if not isinstance(order, dict):
        return None
    wallet = _address(wallet if wallet is not None else order.get('wallet'))
    token, tx = _address(order.get('token')), _hash(order.get('tx_hash'))
    chain, side = order.get('chain'), order.get('side')
    if (not wallet or wallet == '0x' + '0' * 40 or not token or not tx
            or not isinstance(chain, str) or chain not in CHAIN_IDS or side not in ('buy', 'sell')):
        return None
    return dict(chain=chain, wallet=wallet, token=token, tx_hash=tx, side=side)


def _key(row):
    return ':'.join(row[field] for field in IDENTITY)


def _empty(identity, status='pending', reason='awaiting_verification'):
    return {**identity, **dict.fromkeys(ATOMIC_FIELDS), 'status': status, 'reason': reason,
            'receipt_status': 'unknown', 'trace_status': 'unknown', 'fifo_status': 'unknown',
            'verified_at': None, 'time': None, 'block_number': None,
            'transaction_index': None, 'block_hash': None, 'fee_scope': 'transaction_only'}


def _public(row):
    return {field: row.get(field) for field in PUBLIC_FIELDS}


def _transfer(log, wallet):
    if not isinstance(log, dict):
        raise EvidenceError('invalid_transfer_log')
    topics = log.get('topics')
    if not isinstance(topics, list) or not topics or topics[0] != TRANSFER:
        return None
    # ERC721 Transfer has four topics; it is not fungible-token quantity evidence.
    if len(topics) != 3:
        raise EvidenceError('non_erc20_transfer')
    for value in topics[1:]:
        if not isinstance(value, str) or not re.fullmatch(r'0x0{24}[0-9a-fA-F]{40}', value):
            raise EvidenceError('invalid_transfer_log')
    source, target = '0x' + topics[1][-40:].lower(), '0x' + topics[2][-40:].lower()
    if wallet not in (source, target):
        return None
    token, tx, block_hash = _address(log.get('address')), _hash(log.get('transactionHash')), _hash(log.get('blockHash'))
    if not token or not tx or not block_hash or log.get('removed') not in (None, False):
        raise EvidenceError('invalid_transfer_log')
    if not _hash(log.get('data')):
        raise EvidenceError('invalid_transfer_amount')
    amount = int(log['data'], 16)
    return {'token': token, 'tx_hash': tx, 'block_hash': block_hash,
            'block_number': _quantity(log.get('blockNumber')),
            'transaction_index': _quantity(log.get('transactionIndex')),
            'log_index': _quantity(log.get('logIndex')),
            'delta': amount * ((target == wallet) - (source == wallet))}


def _transfer_key(log):
    return log['tx_hash'], log['log_index']


def _trace_flow(trace, tx, wallet):
    if (not isinstance(trace, dict) or trace.get('type') != 'CALL' or trace.get('error')
            or _address(trace.get('from')) != wallet or _address(trace.get('to')) != _address(tx.get('to'))
            or _quantity(trace.get('value')) != _quantity(tx.get('value'))
            or trace.get('input') != tx.get('input')):
        raise EvidenceError('trace_identity_mismatch')
    incoming = outgoing = count = 0
    stack = [trace]
    while stack:
        node = stack.pop()
        count += 1
        if count > 10000 or not isinstance(node, dict):
            raise EvidenceError('trace_limit_or_shape')
        if node.get('error'):
            continue  # Every descendant of a reverted call is also reverted.
        kind = node.get('type')
        if kind not in {'CALL', 'CREATE', 'CREATE2', 'SELFDESTRUCT', 'STATICCALL', 'DELEGATECALL', 'CALLCODE'}:
            raise EvidenceError('trace_type_unknown')
        if kind in {'CALL', 'CREATE', 'CREATE2', 'SELFDESTRUCT'}:
            value = _quantity(node.get('value', '0x0'))
            source, target = _address(node.get('from')), _address(node.get('to'))
            if not source or not target:
                raise EvidenceError('trace_address_unknown')
            outgoing += value if source == wallet else 0
            incoming += value if target == wallet else 0
        calls = node.get('calls', [])
        if not isinstance(calls, list):
            raise EvidenceError('trace_children_unknown')
        stack.extend(calls)
    return incoming, outgoing


def _finish(row):
    complete = (row['receipt_status'] == row['trace_status'] == row['fifo_status'] == 'verified'
                and row['gas_native_atomic'] is not None and row['amount_atomic'] is not None
                and (row['buy_cost_native_atomic'] is not None if row['side'] == 'buy'
                     else row['pnl_native_atomic'] is not None))
    row['status'] = 'verified' if complete else 'partial'
    if complete:
        row['reason'] = 'verified_transaction_accounting'


def verify_transaction(order, rpc):
    """Blocking worker helper. rpc(method, params) returns a JSON-RPC result."""
    identity = _identity(order)
    if identity is None:
        raise EvidenceError('invalid_order_identity')
    chain, wallet, tx_hash = identity['chain'], identity['wallet'], identity['tx_hash']
    if _quantity(rpc('eth_chainId', [])) != CHAIN_IDS[chain]:
        raise EvidenceError('chain_mismatch')
    tx = rpc('eth_getTransactionByHash', [tx_hash])
    receipt = rpc('eth_getTransactionReceipt', [tx_hash])
    if not isinstance(tx, dict) or not isinstance(receipt, dict):
        raise EvidenceError('transaction_not_confirmed')
    if (_hash(tx.get('hash')) != tx_hash or _address(tx.get('from')) != wallet
            or not _address(tx.get('to')) or _quantity(tx.get('chainId')) != CHAIN_IDS[chain]):
        raise EvidenceError('transaction_identity_mismatch')
    block_hash = _hash(receipt.get('blockHash'))
    block = _quantity(receipt.get('blockNumber'))
    index = _quantity(receipt.get('transactionIndex'))
    if (_quantity(receipt.get('status')) != 1 or _hash(receipt.get('transactionHash')) != tx_hash
            or _address(receipt.get('from')) != wallet or _address(receipt.get('to')) != _address(tx.get('to'))
            or not block_hash or _hash(tx.get('blockHash')) != block_hash
            or _quantity(tx.get('blockNumber')) != block or _quantity(tx.get('transactionIndex')) != index):
        raise EvidenceError('receipt_identity_or_status_mismatch')
    header = rpc('eth_getBlockByNumber', [hex(block), False])
    if (not isinstance(header, dict) or _hash(header.get('hash')) != block_hash
            or _quantity(header.get('number')) != block):
        raise EvidenceError('noncanonical_block')
    value = _quantity(tx.get('value'))
    row = _empty(identity, 'partial', 'fifo_not_verified')
    row.update(receipt_status='verified', verified_at=_iso(), block_number=block,
               transaction_index=index, block_hash=block_hash,
               time=_iso(_quantity(header.get('timestamp'))), tx_value_native_atomic=str(value))
    gas = None
    try:
        execution = _quantity(receipt.get('gasUsed')) * _quantity(receipt.get('effectiveGasPrice'))
        l1 = _fee(receipt['l1Fee']) if 'l1Fee' in receipt else 0
        gas = execution + l1
        row.update(execution_gas_native_atomic=str(execution), l1_fee_native_atomic=str(l1),
                   gas_native_atomic=str(gas))
        if identity['side'] == 'buy':
            row['native_spent_atomic'] = str(value + gas)
    except EvidenceError:
        row['reason'] = 'gas_evidence_missing'
    transfers, ambiguous = [], False
    try:
        logs = receipt.get('logs')
        if not isinstance(logs, list) or len(logs) > 10000:
            raise EvidenceError('receipt_logs_unavailable')
        seen = set()
        for raw in logs:
            log = _transfer(raw, wallet)
            if log is None:
                continue
            if (log['tx_hash'] != tx_hash or log['block_hash'] != block_hash
                    or log['block_number'] != block or log['transaction_index'] != index
                    or _transfer_key(log) in seen):
                raise EvidenceError('receipt_log_identity_mismatch')
            seen.add(_transfer_key(log))
            if log['token'] == identity['token']:
                transfers.append(log)
            elif log['delta']:
                ambiguous = True
        delta = sum(log['delta'] for log in transfers)
        if (identity['side'] == 'buy' and delta <= 0) or (identity['side'] == 'sell' and delta >= 0):
            raise EvidenceError('transfer_direction_unverified')
        row['amount_atomic'] = str(abs(delta))
    except EvidenceError:
        transfers = []
        row['reason'] = 'token_transfer_unverified'
    try:
        trace = rpc('debug_traceTransaction', [tx_hash, {
            'tracer': 'callTracer', 'timeout': '3s', 'tracerConfig': {'onlyTopCall': False},
        }])
        incoming, outgoing = _trace_flow(trace, tx, wallet)
        row['trace_status'] = 'verified'
        if identity['side'] == 'buy':
            if gas is not None:
                row['native_spent_atomic'] = str(outgoing + gas)
                if outgoing > incoming and not ambiguous and row['amount_atomic'] is not None:
                    row['buy_cost_native_atomic'] = str(outgoing - incoming + gas)
        else:
            row['native_received_atomic'] = str(incoming)
            if gas is not None:
                row['net_native_received_atomic'] = str(incoming - outgoing - gas)
    except Exception:
        row['trace_status'] = 'unavailable'
        row['reason'] = 'trace_unavailable_or_invalid'
    row['_evidence'] = {'transfers': transfers, 'ambiguous_assets': ambiguous}
    return row


def _token_balance(rpc, token, wallet, block):
    raw = rpc('eth_call', [{'to': token, 'data': '0x70a08231' + wallet[2:].rjust(64, '0')}, hex(block)])
    if _hash(raw) is None:
        raise EvidenceError('historical_token_balance_unavailable')
    return int(raw, 16)


def _consume(lots, quantity):
    cost, known = 0, True
    while quantity:
        if not lots:
            raise EvidenceError('inventory_gap')
        lot = lots[0]
        take = min(quantity, lot[0])
        part = None if lot[1] is None else (lot[1] if take == lot[0] else lot[1] * take // lot[0])
        if part is None:
            known = False
        else:
            cost += part
            lot[1] -= part
        quantity -= take
        lot[0] -= take
        if lot[0] == 0:
            lots.popleft()
    return cost if known else None


def apply_fifo(records, rpc, *, max_block_span=10000):
    """Worker-only FIFO for one chain/wallet/token, using a bounded log window.

    Unknown opening inventory/external acquisitions remain unknown-cost lots.
    No full-chain discovery is attempted. Archive/log failure keeps costs null.
    """
    if not records:
        return
    for row in records:
        row.update(cost_basis_native_atomic=None, pnl_native_atomic=None, fifo_status='incomplete')
    try:
        scope = {(row['chain'], row['wallet'], row['token']) for row in records}
        if len(scope) != 1 or any(not row.get('_evidence') or row['amount_atomic'] is None for row in records):
            raise EvidenceError('fifo_evidence_incomplete')
        _, wallet, token = next(iter(scope))
        start, end = min(row['block_number'] for row in records), max(row['block_number'] for row in records)
        if start < 1 or end - start + 1 > max_block_span:
            raise EvidenceError('fifo_window_exceeded')
        opening = _token_balance(rpc, token, wallet, start - 1)
        scanned = {}
        for topics in ([TRANSFER, _topic(wallet)], [TRANSFER, None, _topic(wallet)]):
            logs = rpc('eth_getLogs', [{'address': token, 'fromBlock': hex(start), 'toBlock': hex(end), 'topics': topics}])
            if not isinstance(logs, list) or len(logs) >= 2000:
                raise EvidenceError('fifo_log_limit_or_shape')
            for raw in logs:
                log = _transfer(raw, wallet)
                if log is None or log['token'] != token or not start <= log['block_number'] <= end:
                    raise EvidenceError('fifo_log_identity_mismatch')
                key = _transfer_key(log)
                if key in scanned and scanned[key] != log:
                    raise EvidenceError('fifo_conflicting_log')
                scanned[key] = log
        closing = _token_balance(rpc, token, wallet, end)
        if opening + sum(log['delta'] for log in scanned.values()) != closing:
            raise EvidenceError('fifo_inventory_not_reconciled')
        by_tx = {row['tx_hash']: row for row in records}
        if len(by_tx) != len(records):
            raise EvidenceError('fifo_duplicate_transaction')
        for row in records:
            expected = {_transfer_key(log): log for log in row['_evidence']['transfers']}
            observed = {key: log for key, log in scanned.items() if log['tx_hash'] == row['tx_hash']}
            if not expected or expected != observed:
                raise EvidenceError('fifo_receipt_log_gap')
        movements = defaultdict(list)
        for log in sorted(scanned.values(), key=lambda r: (r['block_number'], r['transaction_index'], r['log_index'])):
            movements[log['tx_hash']].append(log)
        lots = deque([[opening, None]] if opening else [])
        calculated = {}
        for tx_hash, logs in movements.items():
            delta = sum(log['delta'] for log in logs)
            row = by_tx.get(tx_hash)
            if delta > 0:
                cost = row.get('buy_cost_native_atomic') if row and row['side'] == 'buy' else None
                lots.append([delta, int(cost) if cost is not None else None])
                if row:
                    calculated[tx_hash] = ('verified' if cost is not None else 'incomplete', None, None)
            elif delta < 0:
                cost = _consume(lots, -delta)
                if row and row['side'] == 'sell':
                    net = row['net_native_received_atomic']
                    pnl = (int(net) - cost if cost is not None and net is not None
                           and not row['_evidence']['ambiguous_assets'] else None)
                    calculated[tx_hash] = ('verified' if cost is not None else 'incomplete', cost, pnl)
        for row in records:
            status, cost, pnl = calculated.get(row['tx_hash'], ('incomplete', None, None))
            row.update(fifo_status=status, cost_basis_native_atomic=str(cost) if cost is not None else None,
                       pnl_native_atomic=str(pnl) if pnl is not None else None)
            if status != 'verified':
                row['reason'] = 'fifo_unknown_cost_lot'
    except Exception:
        for row in records:
            row['reason'] = 'fifo_coverage_unavailable'
    for row in records:
        _finish(row)


def _fetch_json(url, payload, timeout):
    request = Request(url, data=json.dumps(payload).encode('ascii'), headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'alpha-terminal-accounting/1',
    })
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise EvidenceError('response_limit')
    return json.loads(body)


class _Rpc:
    def __init__(self, fetch, url, timeout, budget):
        self.fetch, self.url, self.timeout, self.budget = fetch, url, timeout, budget

    def __call__(self, method, params):
        remaining = self.budget['deadline'] - time.monotonic()
        if method not in READ_METHODS or self.budget['left'] <= 0 or remaining <= 0 or self.budget['stop'].is_set():
            raise EvidenceError('rpc_budget_exhausted')
        if not self.url:
            raise EvidenceError('public_rpc_unavailable')
        self.budget['left'] -= 1
        try:
            raw = self.fetch(self.url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                             min(self.timeout, remaining))
            if not isinstance(raw, dict) or raw.get('id') != 1 or raw.get('error') is not None or 'result' not in raw:
                raise EvidenceError('rpc_unavailable')
            return raw['result']
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError('rpc_unavailable') from None


class Collector:
    """Nonblocking enrich_snapshot(snapshot); background 20s/3-tx batches.

    timeout=3 seconds per RPC, max_rpc=30 and batch_seconds=15 per cycle.
    Partial failures use exponential retry backoff, capped at 300 seconds.
    Verified evidence is rechecked after 300 seconds. One instance owns a sidecar.
    """

    def __init__(self, root=ROOT, outputs=SDK_OUTPUTS, *, interval=20.0, batch_size=3,
                 timeout=3.0, max_rpc=30, batch_seconds=15.0, max_block_span=10000,
                 fetch_json=None):
        for value in (interval, timeout, batch_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('positive_finite_timings_required')
        for value in (batch_size, max_rpc, max_block_span):
            if type(value) is not int or value <= 0:
                raise ValueError('positive_limits_required')
        self.root, self.path = Path(root), Path(outputs) / 'terminal-accounting.json'
        self.interval, self.batch_size, self.timeout = interval, batch_size, timeout
        self.max_rpc, self.batch_seconds, self.max_block_span = max_rpc, batch_seconds, max_block_span
        self._fetch = fetch_json or _fetch_json
        self._lock, self._wake, self._stop = threading.Lock(), threading.Event(), threading.Event()
        self._thread = None
        self._wanted, self._cache, self._due, self._attempts = {}, {}, {}, {}
        self._persistence = 'not_written'

    def enrich_snapshot(self, snapshot):
        """Return a new snapshot with order.accounting and aggregate coverage.

        Existing non-null SDK accounting fields are preserved; verified values
        only fill missing fields. The nested accounting is the sidecar authority.
        """
        result = copy.deepcopy(snapshot)
        orders = result.get('orders') if isinstance(result.get('orders'), list) else []
        wanted = {}
        for row in orders:
            identity = _identity(row, result.get('wallet'))
            if identity and row.get('status') == 'filled':
                wanted[_key(identity)] = identity
        wanted = dict(list(wanted.items())[-MAX_RECORDS:])
        with self._lock:
            self._wanted = wanted
            if wanted and self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name='alpha-terminal-accounting', daemon=True)
                self._thread.start()
            self._wake.set()
            cached = {key: _public(self._cache.get(key, _empty(identity))) for key, identity in wanted.items()}
            persistence = self._persistence
        for row in orders:
            identity = _identity(row, result.get('wallet'))
            if not identity or _key(identity) not in cached:
                continue
            accounting = copy.deepcopy(cached[_key(identity)])
            row['accounting'] = accounting
            if accounting['receipt_status'] == 'verified':
                for field in OVERLAY_FIELDS:
                    if row.get(field) is None and accounting.get(field) is not None:
                        row[field] = accounting[field]
        rows = list(cached.values())
        verified = [row for row in rows if row['receipt_status'] == 'verified']
        result['accounting'] = {
            'total': len(rows), 'attempted': sum(row['status'] != 'pending' for row in rows),
            'receipt_covered': len(verified),
            'gas_covered': sum(row['gas_native_atomic'] is not None for row in verified),
            'payment_covered': sum(row['side'] == 'buy' and row['tx_value_native_atomic'] is not None for row in verified),
            'fifo_covered': sum(row['side'] == 'sell' and row['cost_basis_native_atomic'] is not None for row in verified),
            'pnl_covered': sum(row['pnl_native_atomic'] is not None for row in verified),
            'persistence_status': persistence, 'fee_scope': 'transaction_only',
        }
        return result

    def _load(self):
        try:
            with self.path.open('rb') as stream:
                body = stream.read(5 * MAX_RESPONSE + 1)
            if len(body) > 5 * MAX_RESPONSE:
                return
            data = json.loads(body)
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('records'), list):
                return
            loaded = {}
            for source in data['records'][-MAX_RECORDS:]:
                identity = _identity(source)
                if identity is None:
                    continue
                row = _empty(identity, 'stale', 'persisted_evidence_requires_recheck')
                for field in ATOMIC_FIELDS:
                    value = source.get(field)
                    if isinstance(value, str) and re.fullmatch(r'-?[0-9]{1,160}', value):
                        row[field] = value
                for field in ('verified_at', 'time'):
                    value = source.get(field)
                    if isinstance(value, str):
                        stamp = datetime.fromisoformat(value)
                        if stamp.tzinfo is not None:
                            row[field] = stamp.isoformat()
                # Stored values are viewable as stale, but cannot seed FIFO evidence.
                loaded[_key(identity)] = row
            with self._lock:
                self._cache.update(loaded)
        except (OSError, ValueError, TypeError, RecursionError):
            pass

    def _save(self, records):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='terminal-accounting-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'version': 1, 'updated_at': _iso(), 'records': [_public(row) for row in records]},
                          stream, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            state = 'ok'
        except (OSError, ValueError):
            state = 'error'
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    state = 'error'
        with self._lock:
            self._persistence = state

    def _cycle(self):
        now = time.monotonic()
        with self._lock:
            eligible = [(key, row) for key, row in self._wanted.items() if self._due.get(key, 0) <= now]
            eligible.sort(key=lambda item: (self._due.get(item[0], 0), item[0]))
            jobs = eligible[:self.batch_size]
            source = self._cache
        if not jobs:
            return
        # Published records are replaced, not mutated; large copies stay off the UI lock.
        records = copy.deepcopy(source)
        try:
            urls = _rpc_urls(self.root)
        except Exception:
            urls = {}
        budget = {'left': self.max_rpc, 'deadline': now + self.batch_seconds, 'stop': self._stop}
        clients = {chain: _Rpc(self._fetch, urls.get(chain), self.timeout, budget) for chain in CHAIN_IDS}
        touched = set()
        completed = []
        for key, identity in jobs:
            if budget['left'] <= 0 or time.monotonic() >= budget['deadline'] or self._stop.is_set():
                break
            try:
                records[key] = verify_transaction(identity, clients[identity['chain']])
                touched.add((identity['chain'], identity['wallet'], identity['token']))
            except EvidenceError as error:
                reason = str(error)
                transient = {
                    'rpc_unavailable', 'public_rpc_unavailable', 'rpc_budget_exhausted',
                    'transaction_not_confirmed'}
                known = transient | {'chain_mismatch', 'transaction_identity_mismatch',
                                     'receipt_identity_or_status_mismatch', 'noncanonical_block',
                                     'invalid_quantity', 'invalid_order_identity'}
                records[key] = _empty(identity, 'unavailable' if reason in transient else 'invalid',
                                      reason if reason in known else 'verification_rejected')
                # Reverification failure must invalidate previously derived FIFO.
                touched.add((identity['chain'], identity['wallet'], identity['token']))
            except Exception:
                records[key] = _empty(identity, 'unavailable', 'verification_unavailable')
                touched.add((identity['chain'], identity['wallet'], identity['token']))
            completed.append(key)
        for scope in touched:
            group = [row for row in records.values() if (row['chain'], row['wallet'], row['token']) == scope]
            verified = [row for row in group if row.get('_evidence')]
            for row in group:
                row.update(cost_basis_native_atomic=None, pnl_native_atomic=None, fifo_status='unknown')
            if verified:
                apply_fifo(verified, clients[scope[0]], max_block_span=self.max_block_span)
        with self._lock:
            for key in completed:
                attempts = min(self._attempts.get(key, 0) + 1, 5)
                self._attempts[key] = attempts
                delay = 300 if records[key]['status'] == 'verified' else min(300, self.interval * 2 ** (attempts - 1))
                self._due[key] = time.monotonic() + delay
            self._cache = dict(list(records.items())[-MAX_RECORDS:])
            saved = list(self._cache.values())
        if completed:
            self._save(saved)

    def _run(self):
        self._load()
        next_cycle = 0.0
        while not self._stop.is_set():
            remaining = next_cycle - time.monotonic()
            if remaining > 0:
                self._wake.wait(remaining)
                self._wake.clear()
                continue
            started = time.monotonic()
            self._cycle()
            next_cycle = started + self.interval

    def close(self, timeout=1.0):
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
