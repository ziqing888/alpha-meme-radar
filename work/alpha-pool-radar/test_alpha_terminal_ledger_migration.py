import json
from contextlib import contextmanager

import pytest

from alpha_terminal_ledger_migration import migrate, inventory, MigrationError

WALLET = '0x' + '1' * 40
TOKEN = '0x' + '2' * 40
TX = '0x' + '3' * 64


class Control:
    def __init__(self, root):
        self.repo = root
        self.root = root
        self.busy = False

    def _config(self):
        return {'wallet_address': WALLET}

    @contextmanager
    def _locked(self):
        yield

    def _process_snapshot(self):
        return self.busy

    def _has_worker(self, snapshot):
        return snapshot


def setup(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    path = outputs / 'okx-dex-sdk-live-state.json'
    state = dict(version=1, positions={}, fills=[dict(chain='bsc', token=TOKEN,
        side='buy', tx_hash=TX)], closed=[], seen={'keep': True},
        realized_pnl_native_atomic='123')
    path.write_text(json.dumps(state))
    return Control(tmp_path), path, state


def rpc(method, params):
    if method == 'eth_getBlockByNumber':
        return {'hash': TX, 'number': '0x1'}
    assert method in ('eth_chainId', 'eth_getTransactionCount')
    return '0x38' if method == 'eth_chainId' else '0x1'


def verifier(order, client):
    return dict(receipt_status='verified', amount_atomic='2', block_hash=TX,
                block_number=1, verified_at='2026-09-09T00:00:00Z')


def test_dry_check_never_changes_ledger(tmp_path):
    c, path, state = setup(tmp_path)
    raw = path.read_bytes()
    result = migrate(c, 'bsc', WALLET, rpc, verifier=verifier)
    assert result['status'] == 'verified'
    assert path.read_bytes() == raw
    assert not list(path.parent.glob('*.identity-backup-*'))


def test_apply_only_adds_identity_and_preserves_original_backup(tmp_path):
    c, path, state = setup(tmp_path)
    raw = path.read_bytes()
    result = migrate(c, 'bsc', WALLET, rpc, apply=True, verifier=verifier)
    assert result['status'] == 'migrated'
    assert json.loads(path.read_text()) == dict(state, wallet=WALLET, chain='bsc')
    assert (path.parent / result['backup']).read_bytes() == raw
    assert not list(path.parent.glob('*daemon.lock'))


@pytest.mark.parametrize('change,reason', [
    ({'wallet': '0x' + '4' * 40}, 'wallet_mismatch'),
    ({'chain': 'robinhood'}, 'chain_mismatch'),
    ({'terminal_pending': 1}, 'unresolved_state'),
    ({'pending_orders': [{}]}, 'unresolved_state'),
    ({'execution_intents': {'x': {'state': 'pending'}}}, 'unresolved_state'),
    ({'positions': {'x': {}}}, 'open_positions'),
])
def test_rejects_conflicts_and_pending(tmp_path, change, reason):
    c, path, state = setup(tmp_path)
    state.update(change)
    path.write_text(json.dumps(state))
    raw = path.read_bytes()
    with pytest.raises(MigrationError, match=reason):
        migrate(c, 'bsc', WALLET, rpc, apply=True, verifier=verifier)
    assert path.read_bytes() == raw


def test_inventory_includes_closed_buy_and_deduplicates(tmp_path):
    _, _, state = setup(tmp_path)
    state['closed'] = [dict(chain='bsc', token=TOKEN, buy_tx=TX, sell_tx='0x'+'5'*64,
                           last_sell_tx='0x'+'5'*64, remaining_atomic='0')]
    assert len(inventory(state, 'bsc', WALLET)) == 2


def test_missing_receipt_or_transfer_blocks(tmp_path):
    c, path, _ = setup(tmp_path)
    with pytest.raises(MigrationError, match='transaction_evidence_incomplete'):
        migrate(c, 'bsc', WALLET, rpc, apply=True, verifier=lambda *_: {})
    assert 'wallet' not in json.loads(path.read_text())


def test_file_changed_during_verification_blocks(tmp_path):
    c, path, _ = setup(tmp_path)
    def changed(*args):
        path.write_text('{}')
        return verifier(*args)
    with pytest.raises(MigrationError, match='ledger_changed'):
        migrate(c, 'bsc', WALLET, rpc, apply=True, verifier=changed)
    assert path.read_text() == '{}'


def test_daemon_lock_blocks_and_is_not_deleted(tmp_path):
    c, path, _ = setup(tmp_path)
    lock = path.parent / 'okx-dex-sdk-daemon.lock'
    lock.write_text('existing')
    with pytest.raises(MigrationError, match='daemon_locked'):
        migrate(c, 'bsc', WALLET, rpc, apply=True, verifier=verifier)
    assert lock.read_text() == 'existing'


def test_nonce_pending_blocks(tmp_path):
    c, _, _ = setup(tmp_path)
    def pending(method, params):
        return '0x2' if params and params[-1] == 'pending' else rpc(method, params)
    with pytest.raises(MigrationError, match='pending_transactions'):
        migrate(c, 'bsc', WALLET, pending, verifier=verifier)


def test_rpc_wrong_chain_blocks(tmp_path):
    c, _, _ = setup(tmp_path)
    with pytest.raises(MigrationError, match='chain_mismatch'):
        migrate(c, 'bsc', WALLET, lambda *_: '0x1', verifier=verifier)


def test_worker_started_during_check_blocks(tmp_path):
    c, path, _ = setup(tmp_path)
    def changed(*args):
        c.busy = True
        return verifier(*args)
    with pytest.raises(MigrationError, match='worker_running'):
        migrate(c, 'bsc', WALLET, rpc, apply=True, verifier=changed)
    assert 'wallet' not in json.loads(path.read_text())


def test_reorg_before_commit_blocks(tmp_path):
    c, path, _ = setup(tmp_path)
    def reorg(method, params):
        if method == 'eth_getBlockByNumber':
            return {'hash': '0x' + '6' * 64, 'number': '0x1'}
        return rpc(method, params)
    with pytest.raises(MigrationError, match='noncanonical_block'):
        migrate(c, 'bsc', WALLET, reorg, apply=True, verifier=verifier)
    assert 'wallet' not in json.loads(path.read_text())
