"""Receipt/trace/FIFO fixtures; no live wallet or endpoint access."""
import copy
import importlib
import json
import threading
import time

import pytest


WALLET = '0x' + '1' * 40
TOKEN = '0x' + '2' * 40
ROUTER = '0x' + '3' * 40
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


def hash_of(number):
    return '0x' + f'{number:064x}'


def topic(address):
    return '0x' + address[2:].rjust(64, '0')


class RpcFixture:
    def __init__(self):
        self.transactions = {}
        self.receipts = {}
        self.traces = {}
        self.logs = []
        self.calls = []
        self.chain_id = 56
        self.opening = 0
        self.fail = set()

    def add(self, side, quantity, value=0, received=0, block=None, index=0):
        number = len(self.transactions) + 1
        tx_hash = hash_of(number)
        block = block or 100 + number
        block_hash = hash_of(block + 10000)
        tx = {'hash': tx_hash, 'from': WALLET, 'to': ROUTER, 'chainId': hex(self.chain_id),
              'value': hex(value), 'blockNumber': hex(block), 'blockHash': block_hash,
              'transactionIndex': hex(index), 'input': '0x1234'}
        log = {'address': TOKEN, 'transactionHash': tx_hash, 'blockHash': block_hash,
               'blockNumber': hex(block), 'transactionIndex': hex(index), 'logIndex': hex(index * 10),
               'removed': False, 'topics': [TRANSFER, topic(ROUTER if side == 'buy' else WALLET),
                                            topic(WALLET if side == 'buy' else ROUTER)],
               'data': hash_of(quantity)}
        self.transactions[tx_hash] = tx
        self.receipts[tx_hash] = {'transactionHash': tx_hash, 'from': WALLET, 'to': ROUTER,
                                 'status': '0x1', 'blockHash': block_hash, 'blockNumber': hex(block),
                                 'transactionIndex': hex(index), 'gasUsed': '0xa',
                                 'effectiveGasPrice': '0x1', 'l1Fee': '0x2', 'logs': [log]}
        self.logs.append(log)
        trace = {'type': 'CALL', 'from': WALLET, 'to': ROUTER,
                 'value': hex(value), 'input': '0x1234', 'calls': []}
        if received:
            trace['calls'].append({'type': 'CALL', 'from': ROUTER, 'to': WALLET,
                                   'value': hex(received), 'calls': []})
        self.traces[tx_hash] = trace
        return {'chain': 'bsc', 'wallet': WALLET, 'token': TOKEN,
                'tx_hash': tx_hash, 'side': side, 'status': 'filled'}

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method in self.fail:
            raise TimeoutError('DO_NOT_EXPORT_CREDENTIAL')
        if method == 'eth_chainId':
            return hex(self.chain_id)
        if method == 'eth_getTransactionByHash':
            return copy.deepcopy(self.transactions[params[0]])
        if method == 'eth_getTransactionReceipt':
            return copy.deepcopy(self.receipts[params[0]])
        if method == 'debug_traceTransaction':
            assert params[1]['tracer'] == 'callTracer'
            assert params[1]['tracerConfig']['onlyTopCall'] is False
            return copy.deepcopy(self.traces[params[0]])
        if method == 'eth_getBlockByNumber':
            block = int(params[0], 16)
            return {'hash': hash_of(block + 10000), 'number': hex(block),
                    'timestamp': hex(1_700_000_000 + block)}
        if method == 'eth_getLogs':
            spec = params[0]
            assert spec['address'] == TOKEN
            start, end = int(spec['fromBlock'], 16), int(spec['toBlock'], 16)
            return copy.deepcopy([log for log in self.logs
                if start <= int(log['blockNumber'], 16) <= end
                and all(t is None or t == log['topics'][i] for i, t in enumerate(spec['topics']))])
        if method == 'eth_call':
            assert params[0]['to'] == TOKEN
            assert params[0]['data'] == '0x70a08231' + WALLET[2:].rjust(64, '0')
            block = int(params[1], 16)
            balance = self.opening
            for log in self.logs:
                if int(log['blockNumber'], 16) <= block:
                    qty = int(log['data'], 16)
                    balance += qty * ((log['topics'][2] == topic(WALLET)) - (log['topics'][1] == topic(WALLET)))
            return hash_of(balance)
        raise AssertionError('unexpected or mutating RPC method: ' + method)

    def fetch(self, url, payload, timeout):
        assert url == 'https://fixture.invalid/'
        return {'jsonrpc': '2.0', 'id': payload['id'],
                'result': self.rpc(payload['method'], payload['params'])}


@pytest.fixture
def module():
    return importlib.import_module('alpha_terminal_accounting')


@pytest.fixture
def local(tmp_path):
    root, outputs = tmp_path / 'config', tmp_path / 'outputs'
    root.mkdir()
    (root / 'okx-live-config.json').write_text(json.dumps({
        'bsc_rpc_url': 'https://fixture.invalid/', 'robinhood_rpc_url': 'https://fixture.invalid/',
        'api_key_dpapi_path': 'DO_NOT_READ',
    }))
    return root, outputs


def eventually(collector, snapshot, predicate):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        result = collector.enrich_snapshot(snapshot)
        if predicate(result):
            return result
        time.sleep(.01)
    pytest.fail('fixture accounting did not converge')


def test_receipt_gas_includes_l1_and_buy_value(module):
    rpc = RpcFixture()
    order = rpc.add('buy', 100, value=1000)
    record = module.verify_transaction(order, rpc.rpc)
    assert record['receipt_status'] == 'verified'
    assert record['execution_gas_native_atomic'] == '10'
    assert record['l1_fee_native_atomic'] == '2'
    assert record['gas_native_atomic'] == '12'
    assert record['tx_value_native_atomic'] == '1000'
    assert record['native_spent_atomic'] == '1012'
    assert record['buy_cost_native_atomic'] == '1012'
    assert record['amount_atomic'] == '100'


def test_fifo_partial_sales_include_buy_and_sell_fees_and_rounding(module):
    rpc = RpcFixture()
    orders = [rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700),
              rpc.add('sell', 60, received=900)]
    records = [module.verify_transaction(row, rpc.rpc) for row in reversed(orders)]
    module.apply_fifo(records, rpc.rpc)
    by_tx = {row['tx_hash']: row for row in records}
    first, final = by_tx[orders[1]['tx_hash']], by_tx[orders[2]['tx_hash']]
    assert first['cost_basis_native_atomic'] == '404'
    assert first['net_native_received_atomic'] == '688'
    assert first['pnl_native_atomic'] == '284'
    assert final['cost_basis_native_atomic'] == '608'
    assert final['pnl_native_atomic'] == '280'
    assert first['fifo_status'] == final['fifo_status'] == 'verified'
    assert not any(method == 'eth_getBalance' for method, _ in rpc.calls)


def test_fifo_uses_multiple_lots_in_chain_order_not_input_time(module):
    rpc = RpcFixture()
    orders = [rpc.add('buy', 100, value=1000, block=101, index=0),
              rpc.add('buy', 50, value=2000, block=101, index=1),
              rpc.add('sell', 120, received=3000, block=102)]
    records = [module.verify_transaction(row, rpc.rpc) for row in reversed(orders)]
    module.apply_fifo(records, rpc.rpc)
    assert records[0]['cost_basis_native_atomic'] == str(1012 + 2012 * 20 // 50)


def test_trace_unsupported_keeps_gas_and_payment_but_no_net_or_pnl(module):
    rpc = RpcFixture()
    buy, sell = rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700)
    rpc.fail.add('debug_traceTransaction')
    records = [module.verify_transaction(row, rpc.rpc) for row in (buy, sell)]
    module.apply_fifo(records, rpc.rpc)
    assert records[0]['native_spent_atomic'] == '1012'
    assert records[0]['buy_cost_native_atomic'] is None
    assert records[1]['gas_native_atomic'] == '12'
    assert records[1]['net_native_received_atomic'] is None
    assert records[1]['pnl_native_atomic'] is None
    assert records[1]['status'] == 'partial'
    assert 'DO_NOT_EXPORT' not in json.dumps(records)


def test_trace_skips_reverted_subtrees_and_delegatecall_value(module):
    rpc = RpcFixture()
    sell = rpc.add('sell', 5, received=100)
    trace = rpc.traces[sell['tx_hash']]
    trace['calls'].extend([
        {'type': 'CALL', 'from': ROUTER, 'to': WALLET, 'value': '0x999', 'error': 'reverted',
         'calls': [{'type': 'CALL', 'from': ROUTER, 'to': WALLET, 'value': '0x999'}]},
        {'type': 'DELEGATECALL', 'from': ROUTER, 'to': WALLET, 'value': '0x999'},
    ])
    result = module.verify_transaction(sell, rpc.rpc)
    assert result['native_received_atomic'] == '100'
    assert result['net_native_received_atomic'] == '88'


def test_buy_refund_changes_cost_not_gross_payment(module):
    rpc = RpcFixture()
    order = rpc.add('buy', 100, value=1000, received=100)
    result = module.verify_transaction(order, rpc.rpc)
    assert result['native_spent_atomic'] == '1012'
    assert result['buy_cost_native_atomic'] == '912'


@pytest.mark.parametrize('field,value', [('from', ROUTER), ('hash', hash_of(999)),
                                       ('chainId', '0x1'), ('blockHash', hash_of(999))])
def test_tx_identity_mismatch_rejected(module, field, value):
    rpc = RpcFixture()
    row = rpc.add('buy', 10, value=100)
    rpc.transactions[row['tx_hash']][field] = value
    with pytest.raises(module.EvidenceError):
        module.verify_transaction(row, rpc.rpc)


@pytest.mark.parametrize('field,value', [('status', '0x0'), ('transactionHash', hash_of(999)),
                                       ('from', ROUTER), ('blockHash', hash_of(999))])
def test_receipt_identity_or_failure_rejected(module, field, value):
    rpc = RpcFixture()
    row = rpc.add('buy', 10, value=100)
    rpc.receipts[row['tx_hash']][field] = value
    with pytest.raises(module.EvidenceError):
        module.verify_transaction(row, rpc.rpc)


def test_unknown_opening_inventory_does_not_get_zero_cost(module):
    rpc = RpcFixture()
    rpc.opening = 50
    rows = [rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700)]
    records = [module.verify_transaction(row, rpc.rpc) for row in rows]
    module.apply_fifo(records, rpc.rpc)
    assert records[1]['cost_basis_native_atomic'] is None
    assert records[1]['pnl_native_atomic'] is None
    assert records[1]['fifo_status'] == 'incomplete'


def test_missing_buy_in_snapshot_is_unknown_lot_not_zero(module):
    rpc = RpcFixture()
    rpc.add('buy', 100, value=1000)
    sell = rpc.add('sell', 40, received=700)
    record = module.verify_transaction(sell, rpc.rpc)
    module.apply_fifo([record], rpc.rpc)
    assert record['cost_basis_native_atomic'] is None


def test_archive_or_log_coverage_failure_leaves_fifo_unknown(module):
    rpc = RpcFixture()
    rows = [rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700)]
    records = [module.verify_transaction(row, rpc.rpc) for row in rows]
    rpc.fail.add('eth_getLogs')
    module.apply_fifo(records, rpc.rpc)
    assert records[1]['cost_basis_native_atomic'] is None
    assert records[1]['native_received_atomic'] == '700'


def test_collector_nonblocking_atomic_sidecar_and_input_unchanged(module, local):
    root, outputs = local
    rpc = RpcFixture()
    orders = [rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700)]
    snapshot = {'wallet': WALLET, 'orders': orders, 'positions': []}
    original = copy.deepcopy(snapshot)
    with module.Collector(root=root, outputs=outputs, interval=.05,
                          fetch_json=rpc.fetch, batch_size=3) as collector:
        result = eventually(collector, snapshot, lambda s: s['accounting']['pnl_covered'] == 1)
        assert result['orders'][1]['accounting']['cost_basis_native_atomic'] == '404'
        assert result['orders'][1]['cost_basis_native_atomic'] == '404'
        assert snapshot == original
        path = outputs / 'terminal-accounting.json'
        end = time.monotonic() + 2
        while not path.exists() and time.monotonic() < end:
            time.sleep(.01)
        stored = json.loads(path.read_text())
        assert stored['version'] == 1 and len(stored['records']) == 2
        assert 'DO_NOT_READ' not in json.dumps(stored)
        assert '_evidence' not in json.dumps(stored)
        assert not list(outputs.glob('*.tmp'))


def test_slow_rpc_does_not_block_snapshot_or_spawn_duplicate_workers(module, local):
    root, outputs = local
    rpc = RpcFixture()
    row = rpc.add('buy', 100, value=1000)
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        release.wait(2)
        return rpc.fetch(*args)

    collector = module.Collector(root=root, outputs=outputs, fetch_json=slow)
    snapshot = {'wallet': WALLET, 'orders': [row]}
    try:
        collector.enrich_snapshot(snapshot)
        assert entered.wait(1)
        started = time.monotonic()
        for _ in range(100):
            assert collector.enrich_snapshot(snapshot)['orders'][0]['accounting']['status'] == 'pending'
        assert time.monotonic() - started < .3
    finally:
        release.set()
        collector.close(timeout=3)
    assert not collector.is_alive()


def test_batch_limit_and_error_backoff(module, local):
    root, outputs = local
    rpc = RpcFixture()
    rows = [rpc.add('buy', 10, value=100) for _ in range(5)]
    rpc.fail.add('eth_chainId')
    snapshot = {'wallet': WALLET, 'orders': rows}
    with module.Collector(root=root, outputs=outputs, interval=.2, batch_size=2,
                          fetch_json=rpc.fetch) as collector:
        eventually(collector, snapshot, lambda s: s['accounting']['attempted'] == 2)
        count = len(rpc.calls)
        for _ in range(30):
            collector.enrich_snapshot(snapshot)
        assert len(rpc.calls) == count
        assert count <= 2


def test_persisted_evidence_is_stale_not_current_coverage(module, local):
    root, outputs = local
    rpc = RpcFixture()
    order = rpc.add('buy', 10, value=100)
    record = module.verify_transaction(order, rpc.rpc)
    collector = module.Collector(root=root, outputs=outputs)
    collector._save([record])
    collector._load()
    collector.close()
    snapshot = collector.enrich_snapshot({'wallet': WALLET, 'orders': [order]})
    assert snapshot['orders'][0]['accounting']['status'] == 'stale'
    assert snapshot['accounting']['gas_covered'] == 0
    assert snapshot['accounting']['payment_covered'] == 0


def test_failed_atomic_replace_preserves_previous_sidecar(module, local, monkeypatch):
    root, outputs = local
    collector = module.Collector(root=root, outputs=outputs)
    collector._save([])
    before = collector.path.read_bytes()

    def fail(*args):
        raise OSError('DO_NOT_EXPORT')

    monkeypatch.setattr(module.os, 'replace', fail)
    collector._save([])
    assert collector.path.read_bytes() == before
    assert collector._persistence == 'error'
    assert not list(outputs.glob('*.tmp'))


def test_rpc_budget_caps_all_methods_and_blocks_non_read_methods(module):
    fixture = RpcFixture()
    budget = {'left': 1, 'deadline': time.monotonic() + 2, 'stop': threading.Event()}
    rpc = module._Rpc(fixture.fetch, 'https://fixture.invalid/', 1, budget)
    assert rpc('eth_chainId', []) == '0x38'
    with pytest.raises(module.EvidenceError):
        rpc('eth_chainId', [])
    with pytest.raises(module.EvidenceError):
        rpc('eth_sendRawTransaction', ['DO_NOT_SEND'])
    assert len(fixture.calls) == 1


@pytest.mark.parametrize('change', [{'type': 'UNKNOWN'}, {'from': ROUTER}, {'value': '0x999'},
                                  {'input': '0x999'}, {'error': 'reverted'}])
def test_unreliable_trace_never_produces_sell_settlement(module, change):
    rpc = RpcFixture()
    row = rpc.add('sell', 10, received=100)
    rpc.traces[row['tx_hash']].update(change)
    result = module.verify_transaction(row, rpc.rpc)
    assert result['gas_native_atomic'] == '12'
    assert result['native_received_atomic'] is None
    assert result['net_native_received_atomic'] is None


@pytest.mark.parametrize('field', ['gasUsed', 'effectiveGasPrice', 'l1Fee'])
def test_missing_or_malformed_fee_not_coerced_to_zero(module, field):
    rpc = RpcFixture()
    row = rpc.add('buy', 10, value=100)
    rpc.receipts[row['tx_hash']][field] = None
    result = module.verify_transaction(row, rpc.rpc)
    assert result['tx_value_native_atomic'] == '100'
    assert result['gas_native_atomic'] is None
    assert result['buy_cost_native_atomic'] is None


def test_l1_fee_absent_uses_only_receipt_execution_gas(module):
    rpc = RpcFixture()
    row = rpc.add('buy', 10, value=100)
    del rpc.receipts[row['tx_hash']]['l1Fee']
    result = module.verify_transaction(row, rpc.rpc)
    assert result['gas_native_atomic'] == '10'


def test_fee_on_transfer_wallet_debit_uses_all_transfer_logs(module):
    rpc = RpcFixture()
    row = rpc.add('sell', 40, received=100)
    extra = copy.deepcopy(rpc.receipts[row['tx_hash']]['logs'][0])
    extra.update(logIndex='0x1', data=hash_of(2))
    rpc.receipts[row['tx_hash']]['logs'].append(extra)
    result = module.verify_transaction(row, rpc.rpc)
    assert result['amount_atomic'] == '42'


def test_cropped_or_inconsistent_log_response_blocks_fifo(module):
    rpc = RpcFixture()
    orders = [rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700)]
    rows = [module.verify_transaction(row, rpc.rpc) for row in orders]

    def missing_logs(method, params):
        result = rpc.rpc(method, params)
        return [] if method == 'eth_getLogs' else result

    module.apply_fifo(rows, missing_logs)
    assert rows[1]['cost_basis_native_atomic'] is None
    assert rows[1]['pnl_native_atomic'] is None


def test_bounded_window_never_becomes_full_chain_scan(module):
    rpc = RpcFixture()
    orders = [rpc.add('buy', 100, value=1000, block=1), rpc.add('sell', 40, received=700, block=20000)]
    rows = [module.verify_transaction(row, rpc.rpc) for row in orders]
    rpc.calls.clear()
    module.apply_fifo(rows, rpc.rpc, max_block_span=100)
    assert rpc.calls == []
    assert rows[1]['cost_basis_native_atomic'] is None


def test_other_token_consideration_cannot_claim_native_only_pnl(module):
    rpc = RpcFixture()
    orders = [rpc.add('buy', 100, value=1000), rpc.add('sell', 40, received=700)]
    other_asset = copy.deepcopy(rpc.receipts[orders[1]['tx_hash']]['logs'][0])
    other_asset.update(address='0x' + '4' * 40, logIndex='0x1')
    rpc.receipts[orders[1]['tx_hash']]['logs'].append(other_asset)
    records = [module.verify_transaction(row, rpc.rpc) for row in orders]
    module.apply_fifo(records, rpc.rpc)
    assert records[1]['net_native_received_atomic'] == '688'
    assert records[1]['cost_basis_native_atomic'] == '404'
    assert records[1]['pnl_native_atomic'] is None


def test_wrong_chain_rpc_is_rejected_before_transaction_queries(module):
    rpc = RpcFixture()
    row = rpc.add('buy', 10, value=100)
    rpc.chain_id = 1
    with pytest.raises(module.EvidenceError, match='chain_mismatch'):
        module.verify_transaction(row, rpc.rpc)
    assert [method for method, _ in rpc.calls] == ['eth_chainId']


def test_robinhood_chain_id_and_selfdestruct_transfer_supported(module):
    rpc = RpcFixture()
    rpc.chain_id = 4663
    row = rpc.add('sell', 10, received=100)
    row['chain'] = 'robinhood'
    rpc.traces[row['tx_hash']]['calls'][0]['type'] = 'SELFDESTRUCT'
    assert module.verify_transaction(row, rpc.rpc)['native_received_atomic'] == '100'
