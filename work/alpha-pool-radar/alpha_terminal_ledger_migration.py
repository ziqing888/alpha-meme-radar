"""Verify legacy ledger ownership from public RPC; opt-in metadata-only migration.

No signer, credential decryption, transaction submission or worker start. Runtime
control and daemon locks are held throughout verification and the atomic commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from uuid import uuid4

from alpha_terminal_accounting import (
    CHAIN_IDS, EvidenceError, _address, _hash, _quantity, _iso,
    _fetch_json, verify_transaction,
)
from alpha_terminal_balances import _rpc_urls
from alpha_terminal_control import TerminalControl, ControlError


class MigrationError(ValueError):
    pass


def inventory(state, chain, wallet):
    if not isinstance(state, dict) or state.get('version') != 1:
        raise MigrationError('invalid_ledger')
    if state.get('wallet') is not None and _address(state['wallet']) != wallet:
        raise MigrationError('wallet_mismatch')
    if state.get('chain') is not None and state['chain'] != chain:
        raise MigrationError('chain_mismatch')
    if not isinstance(state.get('positions'), dict) or state['positions']:
        raise MigrationError('open_positions')
    # This utility binds settled legacy accounts only, never reconciles intents.
    for field in ('terminal_pending', 'pending', 'pending_orders', 'orders', 'execution_intents'):
        value = state.get(field, 0)
        if not ((type(value) is int and value == 0) or
                (isinstance(value, (list, dict)) and not value)):
            raise MigrationError('unresolved_state')
    orders = {}

    def add(row, side, tx):
        token = _address(row.get('token'))
        if (row.get('chain') != chain or not token or not _hash(tx) or
                side not in ('buy', 'sell') or
                (row.get('wallet') is not None and _address(row['wallet']) != wallet)):
            raise MigrationError('invalid_history_identity')
        order = dict(chain=chain, wallet=wallet, token=token, side=side, tx_hash=_hash(tx))
        previous = orders.get(order['tx_hash'])
        if previous is not None and previous != order:
            raise MigrationError('conflicting_transaction_identity')
        orders[order['tx_hash']] = order

    for field in ('fills', 'closed'):
        rows = state.get(field)
        if not isinstance(rows, list) or len(rows) > 2000:
            raise MigrationError('invalid_history')
        for row in rows:
            if not isinstance(row, dict):
                raise MigrationError('invalid_history')
            if field == 'fills':
                add(row, row.get('side'), row.get('tx_hash'))
            else:
                if str(row.get('remaining_atomic')) != '0':
                    raise MigrationError('unresolved_state')
                add(row, 'buy', row.get('buy_tx'))
                add(row, 'sell', row.get('sell_tx'))
                if row.get('last_sell_tx'):
                    add(row, 'sell', row['last_sell_tx'])
    if not orders and str(state.get('realized_pnl_native_atomic', '0')) != '0':
        raise MigrationError('unproven_ledger_balance')
    return list(orders.values())


@contextmanager
def daemon_lock(path, chain, wallet):
    owner = dict(lock_id=str(uuid4()), pid=os.getpid(), chain=chain,
                 wallet=wallet, created_at=_iso(), purpose='ledger_identity_migration')
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise MigrationError('daemon_locked') from None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(owner, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        if json.loads(path.read_text()).get('lock_id') == owner['lock_id']:
            path.unlink()


def _check_account(rpc, chain, wallet):
    if _quantity(rpc('eth_chainId', [])) != CHAIN_IDS[chain]:
        raise MigrationError('chain_mismatch')
    latest = _quantity(rpc('eth_getTransactionCount', [wallet, 'latest']))
    pending = _quantity(rpc('eth_getTransactionCount', [wallet, 'pending']))
    if pending != latest:
        raise MigrationError('pending_transactions')


def _canonical(rpc, evidence):
    blocks = {}
    for row in evidence:
        number, block_hash = row['block_number'], row['block_hash']
        if number in blocks and blocks[number] != block_hash:
            raise MigrationError('noncanonical_block')
        blocks[number] = block_hash
    for number, block_hash in blocks.items():
        header = rpc('eth_getBlockByNumber', [hex(number), False])
        if (not isinstance(header, dict) or _hash(header.get('hash')) != block_hash or
                _quantity(header.get('number')) != number):
            raise MigrationError('noncanonical_block')


def _write_new(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _idle_workers(control):
    # This is not a wallet replacement: unrelated archived Python test state is
    # untouched. Only this settled SDK ledger is bound, under its daemon lock.
    if control._has_worker(control._process_snapshot()):
        raise MigrationError('worker_running')


def migrate(control, chain, expected_wallet, rpc, *, apply=False, verifier=verify_transaction):
    wallet = _address(expected_wallet)
    if chain not in CHAIN_IDS or not wallet or int(wallet[2:], 16) == 0:
        raise MigrationError('invalid_identity')
    prefix = 'okx-dex-sdk' + ('' if chain == 'bsc' else '-' + chain)
    outputs = control.repo / 'outputs'
    path = outputs / (prefix + '-live-state.json')
    with control._locked(), daemon_lock(outputs / (prefix + '-daemon.lock'), chain, wallet):
        config = control._config()
        if _address(config.get('wallet_address')) != wallet:
            raise MigrationError('configured_wallet_mismatch')
        _idle_workers(control)
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise MigrationError('ledger_too_large')
        state = json.loads(raw.decode('utf-8-sig'))
        orders = inventory(state, chain, wallet)
        _check_account(rpc, chain, wallet)
        evidence = []
        for order in orders:
            row = verifier(order, rpc)
            if (row.get('receipt_status') != 'verified' or
                    not str(row.get('amount_atomic', '')).isdigit() or
                    int(row['amount_atomic']) <= 0):
                raise MigrationError('transaction_evidence_incomplete')
            evidence.append({**order, 'block_hash': row['block_hash'],
                             'block_number': row['block_number'], 'verified_at': row['verified_at']})

        def unchanged():
            current_config = control._config()
            if current_config != config:
                raise MigrationError('config_changed')
            _idle_workers(control)
            if path.read_bytes() != raw:
                raise MigrationError('ledger_changed')

        _check_account(rpc, chain, wallet)
        unchanged()
        result = dict(status='verified', chain=chain, wallet=wallet,
                      transactions=len(evidence), fills=len(state['fills']), closed=len(state['closed']),
                      source_sha256=hashlib.sha256(raw).hexdigest(), evidence=evidence,
                      verified_at=_iso(), live_started=False)
        if not apply:
            _canonical(rpc, evidence)
            return result
        if state.get('wallet') == wallet and state.get('chain') == chain:
            result['status'] = 'already_bound'
            return result
        backup = path.with_name(path.name + '.identity-backup-' + uuid4().hex)
        _write_new(backup, raw)
        report = outputs / (prefix + '-identity-verification-' + uuid4().hex + '.json')
        _write_new(report, json.dumps(result, indent=2).encode('utf-8'))
        fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=outputs)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(dict(state, wallet=wallet, chain=chain), stream, ensure_ascii=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            _canonical(rpc, evidence)
            _check_account(rpc, chain, wallet)
            unchanged()
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        result.update(status='migrated', backup=backup.name, report=report.name)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chain', choices=CHAIN_IDS, required=True)
    parser.add_argument('--expected-wallet', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    control = TerminalControl()
    allowed = {'eth_chainId', 'eth_getTransactionCount', 'eth_getTransactionByHash',
               'eth_getTransactionReceipt', 'eth_getBlockByNumber'}
    try:
        url = _rpc_urls(control.root).get(args.chain)
        if not url:
            raise MigrationError('rpc_unavailable')

        def rpc(method, params):
            # Ownership requires receipts and transfers, not privileged trace APIs.
            if method not in allowed:
                raise EvidenceError('method_not_allowed')
            response = _fetch_json(url, dict(jsonrpc='2.0', id=1, method=method, params=params), 8)
            if (not isinstance(response, dict) or response.get('error') or
                    response.get('id') != 1 or 'result' not in response):
                raise EvidenceError('rpc_failed')
            return response['result']

        result = migrate(control, args.chain, args.expected_wallet, rpc, apply=args.apply)
        result.pop('evidence', None)
        print(json.dumps(result))
        return 0
    except (MigrationError, EvidenceError, ControlError) as error:
        print(json.dumps(dict(status='blocked', reason=str(error), live_started=False)))
    except Exception as error:
        print(json.dumps(dict(status='blocked', reason='verification_failed',
                              error_type=type(error).__name__, live_started=False)))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
