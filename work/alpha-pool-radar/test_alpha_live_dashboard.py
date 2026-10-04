import json
import io
from datetime import datetime, timezone
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pytest

from alpha_live_dashboard import Handler, project_status, allowed_request
from alpha_live_roundtrip_control import RoundtripControl

WALLET = '0x' + '1' * 40
NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def test_roundtrip_wording_uses_shared_records():
    html = Path(__file__).with_name('alpha_live_dashboard.html').read_text(encoding='utf-8')
    assert '\u6d4b\u8bd5\u6a21\u5f0f\uff08\u5171\u4eab\u6d4b\u8bd5\u8bb0\u5f55\uff09' in html
    assert '\u4e24\u4e2a\u6a21\u5f0f\u5171\u7528\u6d4b\u8bd5\u8bb0\u5f55' in html
    assert '\u673a\u4f1a' not in html
    assert '<th>当前收益</th>' in html


def fixture(tmp_path, **changes):
    root = tmp_path / 'alpha-radar'
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    (root / 'okx-live-config.json').write_text(json.dumps({'schema_version': 1, 'wallet_address': WALLET}))
    status = {'wallet': WALLET, 'provider': 'okx', 'enabled': True, 'status': 'running',
              'updated_at': NOW.isoformat(), 'open_positions': 0, 'pending_orders': 0,
              'cash_usd_estimate': '100', 'realized_pnl_usd_estimate': '0',
              'orders': [], 'positions': [], 'issues': []}
    status.update(changes)
    (account / 'status.json').write_text(json.dumps(status))
    return root, account


def test_missing_status_is_not_running_or_zero_profit(tmp_path):
    root, account = fixture(tmp_path)
    (account / 'status.json').unlink()
    result = project_status(root, NOW)
    assert result['state'] == 'not_started'
    assert result['realized_pnl_usd_estimate'] is None
    assert result['configured'] is True


def test_sdk_missing_ledger_identity_keeps_aggregate_counts_unknown(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    for chain, stem in [('bsc', 'okx-dex-sdk-live'), ('robinhood', 'okx-dex-sdk-robinhood-live')]:
        (outputs / f'{stem}-status.json').write_text(json.dumps({
            'provider': 'okx-dex-sdk', 'wallet': WALLET, 'chain': chain,
            'status': 'waiting_for_strategy_candidate', 'updated_at': NOW.isoformat(),
        }))
        state = {'version': 1, 'positions': {}, 'closed': [], 'fills': []}
        if chain == 'bsc':
            state.update(wallet=WALLET.upper().replace('0X', '0x'), chain=chain)
        (outputs / f'{stem}-state.json').write_text(json.dumps(state))
    result = project_status(root, NOW, outputs)
    assert result['open_positions'] is None and result['pending_orders'] is None
    assert result['issue_count'] > 0
    assert result['chains'][0]['ledger_identity_status'] == 'verified'
    assert result['chains'][0]['open_positions'] == 0
    assert result['chains'][1]['ledger_identity_status'] == 'unverified'
    assert result['chains'][1]['open_positions'] is None


def test_fresh_sdk_status_drives_dashboard_and_exposes_confirmed_transaction(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    buy_tx = '0x' + 'a' * 64
    (outputs / 'okx-dex-sdk-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk',
        'status': 'waiting_for_strategy_candidate',
        'reason': 'strategy_candidate_unavailable',
        'updated_at': NOW.isoformat(),
        'live_started': False,
    }))
    (outputs / 'okx-dex-sdk-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'bsc',
        'version': 1,
        'positions': {
            f'bsc:{token}': {
                'chain': 'bsc', 'symbol': 'BNC4', 'token': token,
                'entry_at': NOW.isoformat(), 'entry_price_usd': 0.001,
                'remaining_atomic': '1000000', 'buy_tx': buy_tx,
            },
        },
        'closed': [],
        'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert result['engine'] == 'okx-dex-sdk'
    assert result['state'] == 'running'
    assert result['activity_status'] == 'waiting_for_strategy_candidate'
    assert result['state_label'] == 'OKX DEX SDK · 等待策略候选'
    assert result['activity_message'] == '尚未成交，正在等待策略写入合格 BSC 候选。'
    assert result['open_positions'] == 1
    assert result['pending_orders'] == 0
    assert result['positions'][0]['symbol'] == 'BNC4'
    assert result['positions'][0]['remaining_atomic'] == '1000000'
    assert result['orders'][0] == {
        'token': token,
        'symbol': 'BNC4',
        'chain': 'bsc',
        'side': 'buy',
        'status': 'filled',
        'tx_hash': buy_tx,
        'explorer_url': f'https://bscscan.com/tx/{buy_tx}',
        'time': NOW.isoformat(),
        'realized_pnl_usd_estimate': None,
    }


def test_dashboard_exposes_robinhood_daemon_and_balance_block(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    (outputs / 'okx-dex-sdk-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'waiting_for_strategy_candidate',
        'reason': 'strategy_candidate_unavailable', 'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'bsc',
        'version': 1, 'positions': {}, 'closed': [], 'seen': {},
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'blocked',
        'reason': 'insufficient_native_balance', 'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood',
        'version': 1, 'positions': {}, 'closed': [], 'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert [item['chain'] for item in result['chains']] == ['bsc', 'robinhood']
    robinhood = result['chains'][1]
    assert robinhood['state'] == 'degraded'
    assert robinhood['activity_reason'] == 'insufficient_native_balance'
    assert robinhood['activity_message'] == 'Robinhood Chain 钱包缺少 ETH，等待入金后自动继续。'


def test_dashboard_explains_strategy_gate_reason(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'risk_paused',
        'reason': 'first_discovery_mcap', 'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood',
        'version': 1, 'positions': {}, 'closed': [], 'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert result['activity_message'] == '当前候选已超出首次发现市值范围，已跳过并继续等待下一条。'


def test_dashboard_exposes_partial_sell_fill_and_concrete_rejection(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    sell_tx = '0x' + 'b' * 64
    (outputs / 'okx-dex-sdk-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'waiting_for_strategy_candidate',
        'reason': 'strategy_candidate_unavailable', 'updated_at': NOW.isoformat(),
        'candidate_rejections': [{
            'symbol': 'BURRITO', 'reject_reason': '1h +201.5% 不在优化区间', 'score': 69,
        }],
    }))
    (outputs / 'okx-dex-sdk-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'bsc',
        'version': 1, 'positions': {}, 'closed': [], 'seen': {},
        'fills': [{
            'chain': 'bsc', 'symbol': 'BNC4', 'token': token, 'side': 'sell',
            'tx_hash': sell_tx, 'time': NOW.isoformat(),
            'exit_reason': 'take_profit_1', 'fraction': 0.8,
            'amount_atomic': '800000', 'return_pct': 110,
        }],
    }))

    result = project_status(root, NOW, outputs)

    assert result['activity_message'] == '尚未成交；最近拦截：BURRITO：1h +201.5% 不在优化区间。'
    assert result['orders'][0]['tx_hash'] == sell_tx
    assert result['orders'][0]['side'] == 'sell'
    assert result['orders'][0]['exit_reason'] == 'take_profit_1'
    assert result['orders'][0]['fraction'] == 0.8


def test_dashboard_exposes_confirmed_native_pnl_and_exit_only_mode(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    sell_tx = '0x' + 'b' * 64
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'exit_only',
        'reason': 'new_entries_disabled', 'updated_at': NOW.isoformat(),
        'live_started': True,
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood',
        'version': 1, 'positions': {}, 'closed': [{
            'chain': 'robinhood', 'symbol': 'OCAT', 'token': token,
            'sell_tx': sell_tx, 'closed_at': NOW.isoformat(), 'exit_reason': 'stop_loss',
        }], 'seen': {},
        'realized_pnl_native_atomic': '-301000000000000',
        'fills': [{
            'chain': 'robinhood', 'symbol': 'OCAT', 'token': token, 'side': 'sell',
            'tx_hash': sell_tx, 'time': NOW.isoformat(), 'exit_reason': 'stop_loss',
            'amount_atomic': '1000', 'pnl_native_atomic': '-301000000000000',
            'cost_basis_native_atomic': '1001000000000000',
            'native_received_atomic': '700100000000000',
            'net_native_received_atomic': '700000000000000', 'gas_native_atomic': '100000000000',
        }],
    }))

    result = project_status(root, NOW, outputs)

    assert result['state'] == 'running'
    assert result['activity_status'] == 'exit_only'
    assert result['activity_message'] == '只管理现有持仓，已关闭新增买入。'
    assert result['native_symbol'] == 'ETH'
    assert result['realized_pnl_status'] == 'pending_verification'
    assert result['realized_pnl_pending_count'] == 1
    assert result['unverified_sell_fills'] == 1
    assert result['unreconciled_external_sales'] == 0
    assert result['realized_pnl_native_atomic'] is None
    assert result['realized_pnl_native'] is None
    assert result['orders'][0]['pnl_native_atomic'] == '-301000000000000'
    assert result['orders'][0]['pnl_native'] == pytest.approx(-0.000301)


def test_dashboard_hides_chain_pnl_when_a_sell_settlement_is_incomplete(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    sell_tx = '0x' + 'b' * 64
    (outputs / 'okx-dex-sdk-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'waiting_for_strategy_candidate',
        'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'bsc', 'version': 1,
        'positions': {}, 'closed': [], 'seen': {},
        'realized_pnl_native_atomic': '-250000000000000',
        'fills': [{
            'chain': 'bsc', 'symbol': 'OLD', 'token': token, 'side': 'sell',
            'tx_hash': sell_tx, 'time': NOW.isoformat(),
            'pnl_native_atomic': '-250000000000000',
        }],
    }))

    result = project_status(root, NOW, outputs)

    assert result['realized_pnl_status'] == 'pending_verification'
    assert result['realized_pnl_pending_count'] == 1
    assert result['unverified_sell_fills'] == 1
    assert result['unreconciled_external_sales'] == 0
    assert result['realized_pnl_native_atomic'] is None
    assert result['realized_pnl_native'] is None


def test_dashboard_hides_chain_pnl_when_external_sale_proceeds_are_unreconciled(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    sell_tx = '0x' + 'b' * 64
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'waiting_for_strategy_candidate',
        'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood', 'version': 1,
        'positions': {}, 'closed': [], 'seen': {},
        'realized_pnl_native_atomic': '500000000000000',
        'fills': [{
            'chain': 'robinhood', 'symbol': 'MEME', 'token': token, 'side': 'sell',
            'tx_hash': sell_tx, 'time': NOW.isoformat(),
            'cost_basis_native_atomic': '1000000000000000',
            'native_received_atomic': '1501000000000000',
            'net_native_received_atomic': '1500000000000000',
            'gas_native_atomic': '1000000000000',
            'pnl_native_atomic': '500000000000000',
        }],
        'external_reconciliations': [{
            'chain': 'robinhood', 'token': token,
            'reason': 'external_wallet_reduction',
            'proceeds_status': 'unreconciled',
        }],
    }))

    result = project_status(root, NOW, outputs)

    assert result['realized_pnl_status'] == 'pending_verification'
    assert result['realized_pnl_pending_count'] == 2
    assert result['unverified_sell_fills'] == 1
    assert result['unreconciled_external_sales'] == 1
    assert result['realized_pnl_native_atomic'] is None
    assert result['realized_pnl_native'] is None


def test_dashboard_accepts_complete_receipt_reconciliation_for_legacy_and_external_sales(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    worker_tx = '0x' + 'b' * 64
    external_tx = '0x' + 'c' * 64
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'waiting_for_strategy_candidate',
        'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood', 'version': 1,
        'positions': {}, 'closed': [], 'seen': {},
        'fills': [{
            'chain': 'robinhood', 'symbol': 'MEME', 'token': token, 'side': 'sell',
            'tx_hash': worker_tx, 'time': NOW.isoformat(),
        }],
        'external_reconciliations': [{
            'chain': 'robinhood', 'token': token,
            'reduced_atomic': '400', 'reason': 'external_wallet_reduction',
            'proceeds_status': 'unreconciled',
        }],
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-realized-reconciliation.json').write_text(json.dumps({
        'schema_version': 1, 'wallet': WALLET, 'chain': 'robinhood',
        'native_symbol': 'ETH', 'calculation': 'receipt_fifo_native_equivalent_v1',
        'records': [
            {'tx_hash': worker_tx, 'token': token, 'source': 'worker',
             'amount_atomic': '600', 'pnl_native_atomic': '-300'},
            {'tx_hash': external_tx, 'token': token, 'source': 'external',
             'amount_atomic': '400', 'pnl_native_atomic': '700',
             'settlement_asset': 'USDG'},
        ],
    }))

    result = project_status(root, NOW, outputs)

    assert result['realized_pnl_status'] == 'verified'
    assert result['realized_pnl_pending_count'] == 0
    assert result['unverified_sell_fills'] == 0
    assert result['unreconciled_external_sales'] == 0
    assert result['realized_pnl_native_atomic'] == '400'
    assert result['realized_pnl_native'] == pytest.approx(4e-16)
    assert result['realized_pnl_method'] == 'receipt_fifo_native_equivalent_v1'
    assert result['manual_reconciled_sales'] == 1
    worker_order = next(row for row in result['orders'] if row['tx_hash'] == worker_tx)
    assert worker_order['pnl_native_atomic'] == '-300'
    assert worker_order['pnl_native'] == pytest.approx(-3e-16)


def test_dashboard_rejects_reconciliation_that_does_not_cover_every_worker_sale(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    first_tx = '0x' + 'b' * 64
    second_tx = '0x' + 'c' * 64
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'waiting_for_strategy_candidate',
        'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood', 'version': 1,
        'positions': {}, 'closed': [], 'seen': {},
        'fills': [
            {'chain': 'robinhood', 'symbol': 'ONE', 'token': token, 'side': 'sell',
             'tx_hash': first_tx, 'time': NOW.isoformat()},
            {'chain': 'robinhood', 'symbol': 'TWO', 'token': token, 'side': 'sell',
             'tx_hash': second_tx, 'time': NOW.isoformat()},
        ],
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-realized-reconciliation.json').write_text(json.dumps({
        'schema_version': 1, 'wallet': WALLET, 'chain': 'robinhood',
        'native_symbol': 'ETH', 'calculation': 'receipt_fifo_native_equivalent_v1',
        'records': [
            {'tx_hash': first_tx, 'token': token, 'source': 'worker',
             'amount_atomic': '600', 'pnl_native_atomic': '-300'},
        ],
    }))

    result = project_status(root, NOW, outputs)

    assert result['realized_pnl_status'] == 'pending_verification'
    assert result['realized_pnl_pending_count'] == 1
    assert result['realized_pnl_native_atomic'] is None


def test_dashboard_explains_why_a_live_position_is_still_held(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'holding_existing_position',
        'reason': 'exit_conditions_not_met', 'updated_at': NOW.isoformat(),
        'holding_positions': [{
            'symbol': 'OCAT', 'return_pct': 0, 'age_minutes': 5,
            'tp1_hit': False, 'high_return_pct': 0,
        }],
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood',
        'version': 1,
        'positions': {f'robinhood:{token}': {
            'chain': 'robinhood', 'symbol': 'OCAT', 'token': token,
            'entry_at': NOW.isoformat(), 'entry_price_usd': 0.00003019,
            'last_price_usd': 0.00003019, 'last_return_pct': 0,
            'exit_status': 'hold', 'remaining_atomic': '1000000',
        }},
        'closed': [], 'fills': [], 'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert result['activity_message'] == (
        '持仓监控中：OCAT 当前 +0.0%，尚未触发 -22% 止损、2x 卖出 50% '
        '或首盈前 90 分钟退出。'
    )
    assert result['positions'][0]['current_price_usd'] == 0.00003019
    assert result['positions'][0]['return_pct'] == 0
    assert result['positions'][0]['exit_status'] == 'hold'


def test_dashboard_describes_only_runner_rules_after_third_take_profit(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    token = '0x' + '2' * 40
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': 'holding_existing_position',
        'reason': 'exit_conditions_not_met', 'updated_at': NOW.isoformat(),
        'holding_positions': [{
            'symbol': 'RUNNER', 'return_pct': 500, 'age_minutes': 120,
            'tp1_hit': True, 'tp2_hit': True, 'tp3_hit': True, 'high_return_pct': 700,
        }],
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood', 'version': 1,
        'positions': {f'robinhood:{token}': {
            'chain': 'robinhood', 'symbol': 'RUNNER', 'token': token,
            'entry_at': NOW.isoformat(), 'entry_price_usd': 1,
            'last_price_usd': 6, 'last_return_pct': 500,
            'exit_status': 'hold', 'remaining_atomic': '300',
            'tp1_hit': True, 'tp2_hit': True, 'tp3_hit': True,
        }},
        'closed': [], 'fills': [], 'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert result['activity_message'] == (
        '持仓监控中：RUNNER 当前 +500.0%，尚未触发高点回撤 40% 或 24 小时 Runner 退出。'
    )


@pytest.mark.parametrize(('status', 'reason', 'message'), [
    ('buy_blocked', 'buy_simulation_failed', '买入报价未通过链上模拟，未签名，等待新报价。'),
    ('sell_blocked', 'sell_simulation_failed', '卖出报价未通过链上模拟，未签名，等待新报价。'),
])
def test_dashboard_explains_pre_broadcast_simulation_failure(tmp_path, status, reason, message):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    (outputs / 'okx-dex-sdk-robinhood-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk', 'status': status,
        'reason': reason, 'updated_at': NOW.isoformat(),
    }))
    (outputs / 'okx-dex-sdk-robinhood-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'robinhood',
        'version': 1, 'positions': {}, 'closed': [], 'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert result['activity_message'] == message


def test_stale_sdk_heartbeat_is_never_reported_as_running(tmp_path):
    root, _ = fixture(tmp_path)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    (outputs / 'okx-dex-sdk-live-status.json').write_text(json.dumps({
        'provider': 'okx-dex-sdk',
        'status': 'waiting_for_strategy_candidate',
        'reason': 'strategy_candidate_unavailable',
        'updated_at': '2026-09-08T11:50:00+00:00',
    }))
    (outputs / 'okx-dex-sdk-live-state.json').write_text(json.dumps({
        'wallet': WALLET, 'chain': 'bsc',
        'version': 1, 'positions': {}, 'closed': [], 'seen': {},
    }))

    result = project_status(root, NOW, outputs)

    assert result['engine'] == 'okx-dex-sdk'
    assert result['state'] == 'stale'
    assert result['open_positions'] == 0


def test_stale_and_future_heartbeat_not_running(tmp_path):
    for stamp in ['2026-09-08T11:50:00+00:00', '2026-09-08T12:01:00+00:00', 'garbage']:
        root, _ = fixture(tmp_path / stamp.replace(':', ''), updated_at=stamp)
        assert project_status(root, NOW)['state'] == 'stale'


def test_projection_drops_payload_secrets_and_untrusted_issues(tmp_path):
    root, _ = fixture(tmp_path, private_key='SECRET', issues=[{'reason': 'SECRET', 'raw': 'SECRET'}],
        orders=[{'token': WALLET, 'side': 'buy', 'status': 'filled', 'tx_hash': '0x' + 'a' * 64,
                 'created_at': NOW.isoformat(), 'snapshot': {'api_key': 'SECRET'}, 'raw': 'SECRET'}])
    result = project_status(root, NOW)
    assert result['state'] == 'running'
    assert result['orders'][0]['tx_hash'] == '0x' + 'a' * 64
    assert 'SECRET' not in json.dumps(result)


def test_wrong_account_or_provider_not_exposed(tmp_path):
    for changes in [{'wallet': '0x' + '2' * 40}, {'provider': 'gmgn'}]:
        root, _ = fixture(tmp_path / str(len(changes)) / str(next(iter(changes))), **changes)
        result = project_status(root, NOW)
        assert result['state'] == 'identity_mismatch'
        assert result['orders'] == []
        assert result['realized_pnl_usd_estimate'] is None


def test_paused_and_bad_values(tmp_path):
    root, account = fixture(tmp_path, cash_usd_estimate='NaN', realized_pnl_usd_estimate=True)
    (account / 'PAUSE_ENTRIES').touch()
    result = project_status(root, NOW)
    assert result['entries_paused'] is True
    assert result['cash_usd_estimate'] is None
    assert result['realized_pnl_usd_estimate'] is None


def test_loopback_host_origin_guards():
    assert allowed_request('127.0.0.1:8771', None, '127.0.0.1', 8771)
    assert allowed_request('127.0.0.1:8771', 'http://127.0.0.1:8771', '127.0.0.1', 8771)
    assert not allowed_request('evil.example:8771', None, '127.0.0.1', 8771)
    assert not allowed_request('127.0.0.1:8771', 'https://evil.example', '127.0.0.1', 8771)
    assert not allowed_request('127.0.0.1:8771', None, '192.168.1.2', 8771)


def request(tmp_path, *, method='POST', path='/roundtrip/start', payload=None, headers=None):
    """Exercise HTTP handlers in memory: no listening socket and no real POST."""
    control = RoundtripControl(tmp_path)
    control.start = Mock(return_value=(202, {'roundtrip': {'state': 'starting'}}))
    handler = object.__new__(Handler)
    handler.server = SimpleNamespace(server_port=8771, control=control)
    handler.client_address = ('127.0.0.1', 12345)
    handler.path = path
    handler.connection = Mock()
    body = payload if isinstance(payload, bytes) else json.dumps(payload or {'confirmed': True, 'fee_enabled': False}).encode()
    defaults = {'Host': '127.0.0.1:8771', 'Origin': 'http://127.0.0.1:8771',
                'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                'X-CSRF-Token': control.csrf_token, 'Sec-Fetch-Site': 'same-origin'}
    defaults.update(headers or {})
    handler.headers = Message()
    for key, value in defaults.items():
        if value is not None:
            handler.headers[key] = value
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.send_error = Mock()
    getattr(handler, 'do_' + method)()
    return handler, control


def test_post_routes_to_controller_only_after_http_validation(tmp_path):
    handler, control = request(tmp_path)
    assert handler.send_response.call_args.args[0] == 202
    control.start.assert_called_once()
    assert control.start.call_args.kwargs['payload'] == {'confirmed': True, 'fee_enabled': False}


@pytest.mark.parametrize('headers', [
    {'Origin': None}, {'Origin': 'http://localhost:8771'}, {'X-CSRF-Token': 'wrong'},
    {'Sec-Fetch-Site': 'cross-site'}, {'Host': 'evil.example'},
    {'Content-Type': 'text/plain'}, {'Content-Length': '9999'},
    {'Content-Length': '-1'}, {'Content-Length': None}, {'Transfer-Encoding': 'chunked'},
])
def test_invalid_http_never_reaches_controller(tmp_path, headers):
    handler, control = request(tmp_path, headers=headers)
    assert handler.send_response.call_args.args[0] in {400, 403, 413, 415}
    control.start.assert_not_called()


@pytest.mark.parametrize('payload', [b'{', b'[]', b'null', b'{"confirmed":false,"confirmed":true}', b'{"cap":NaN}'])
def test_bad_json_rejected_without_reflection(tmp_path, payload):
    handler, control = request(tmp_path, payload=payload)
    assert handler.send_response.call_args.args[0] == 400
    control.start.assert_not_called()


def test_get_status_has_csrf_but_never_starts_or_writes(tmp_path):
    handler, control = request(tmp_path, method='GET', path='/roundtrip/status.json')
    body = json.loads(handler.wfile.getvalue())
    assert body['csrf_token'] == control.csrf_token
    assert body['roundtrip']['can_start'] is False
    control.start.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_get_start_has_no_side_effect(tmp_path):
    handler, control = request(tmp_path, method='GET')
    handler.send_error.assert_called_once_with(404)
    control.start.assert_not_called()


@pytest.mark.parametrize('width,height', [(1280, 900), (390, 844), (320, 700)])
def test_browser_controls_with_fully_intercepted_requests(tmp_path, width, height):
    """Browser fixture only; every URL is intercepted, including the fake POST."""
    playwright = pytest.importorskip('playwright.sync_api')
    receipt = '0x' + '34' * 20
    ready = {'wallet': WALLET, 'state': 'not_started', 'reason': 'awaiting_confirmation',
             'test_mode': 'stablecoin', 'token': None, 'symbol': None, 'execution_arm': None,
             'can_start': True, 'attempted': False, 'existing_report': False,
             'approval_txs': [], 'process_state': 'unobserved', 'report_fresh': None,
             'historical_fee': {'recipient': receipt, 'max_trim_per_mille': 100,
                                'created_at': NOW.isoformat()}}
    requests, errors = [], []
    page_html = Path(__file__).with_name('alpha_live_dashboard.html').read_text(encoding='utf-8')
    with playwright.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip('Local Playwright Chromium unavailable; never download in tests')
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': width, 'height': height})
            page.on('pageerror', lambda error: errors.append(str(error)))

            def respond(route):
                path = route.request.url.split('dashboard.invalid', 1)[-1]
                if route.request.url == 'http://dashboard.invalid/':
                    route.fulfill(content_type='text/html', body=page_html)
                elif path == '/status.json':
                    route.fulfill(json={**project_status(tmp_path), 'configured': True,
                                        'wallet': WALLET, 'state': 'running',
                                        'engine': 'okx-dex-sdk',
                                        'activity_status': 'waiting_for_strategy_candidate',
                                        'activity_reason': 'strategy_candidate_unavailable',
                                        'activity_message': '尚未成交，正在等待策略候选。',
                                        'state_label': 'OKX DEX SDK · 等待策略候选'})
                elif path == '/roundtrip/status.json':
                    route.fulfill(json={'roundtrip': ready, 'csrf_token': 'fixture-token'})
                elif path == '/roundtrip/start':
                    requests.append(route.request.post_data_json)
                    assert route.request.headers['x-csrf-token'] == 'fixture-token'
                    route.fulfill(status=202, json={'roundtrip': {**ready, 'state': 'checking',
                        'test_mode': route.request.post_data_json.get('test_mode', 'stablecoin'),
                        'can_start': False, 'attempted': True, 'process_state': 'running'}})
                else:
                    route.abort()

            page.route('**/*', respond)
            page.goto('http://dashboard.invalid/')
            page.wait_for_function("document.getElementById('rt-confirm').disabled === false")
            assert page.locator('#state').inner_text() == 'OKX DEX SDK · 等待策略候选'
            assert '尚未成交' in page.locator('#issues').inner_text()
            assert page.locator('#rt-mode-meme').count() == 1
            assert page.locator('#rt-mode-meme').is_checked()
            assert 'MEME' in page.locator('#rt-title').inner_text()
            assert page.locator('#rt-start').is_disabled()
            assert not page.locator('#rt-fee-enabled').is_checked()
            assert page.locator('#rt-recipient').input_value() == receipt
            assert page.locator('#rt-cap').input_value() == '100'
            assert not requests
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path / f'roundtrip-{width}.png'), full_page=True)
            page.locator('#rt-confirm').check()
            page.locator('#rt-start').click()
            page.wait_for_function("document.getElementById('rt-start').disabled && !rtBusy")
            assert requests == [{'confirmed': True, 'fee_enabled': False, 'test_mode': 'strategy_meme'}]
            page.evaluate('s => rtRender(s)', ready)
            assert page.locator('#rt-confirm').is_disabled()

            page.reload()
            page.wait_for_function("document.getElementById('rt-confirm').disabled === false")
            page.locator('#rt-fee-enabled').check()
            page.locator('#rt-confirm').check()
            page.locator('#rt-mode-stablecoin').check()
            assert not page.locator('#rt-fee-enabled').is_checked()
            assert not page.locator('#rt-confirm').is_checked()
            assert 'USDT' in page.locator('#rt-title').inner_text()
            page.locator('#rt-fee-enabled').check()
            assert not page.locator('#rt-confirm').is_checked()
            page.locator('#rt-recipient').fill('invalid')
            page.locator('#rt-confirm').check()
            assert page.locator('#rt-start').is_disabled()
            page.locator('#rt-recipient').fill(receipt)
            assert not page.locator('#rt-confirm').is_checked()
            page.locator('#rt-confirm').check()
            assert page.locator('#rt-start').is_enabled()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path / f'roundtrip-fees-{width}.png'), full_page=True)
            page.locator('#rt-start').click()
            page.wait_for_function('!rtBusy')
            assert requests[-1] == {'confirmed': True, 'fee_enabled': True,
                                     'trim_recipient': receipt, 'max_trim_per_mille': 100,
                                     'test_mode': 'stablecoin'}

            stale = {**ready, 'state': 'buy_pending', 'can_start': False, 'existing_report': True,
                     'attempted': True, 'process_state': 'exited', 'report_fresh': False,
                     'updated_at': NOW.isoformat(), 'buy_tx': '0x' + 'ab' * 32}
            page.evaluate('s => rtRender(s)', stale)
            assert page.locator('#rt-start').is_disabled()
            assert page.locator('#rt-mode-meme').is_disabled()
            assert page.locator('#rt-mode-stablecoin').is_checked()
            assert '\u8fdb\u7a0b\u5df2\u9000\u51fa' in page.locator('#rt-state').inner_text()
            assert page.locator('#rt-hash-note').is_visible()
            page.evaluate('s => rtRender(s)', {**stale, 'process_state': 'unobserved'})
            assert '\u5fc3\u8df3\u8fc7\u671f' in page.locator('#rt-state').inner_text()
            page.evaluate('s => rtRender(s)', {**stale, 'state': 'completed'})
            assert 'MEME' in page.locator('#rt-message').inner_text()
            assert page.locator('#rt-start').is_disabled()
            page.evaluate('s => rtRender(s)', {**stale, 'state': 'completed', 'test_mode': 'strategy_meme',
                          'token': receipt, 'symbol': 'MEME', 'execution_arm': 'first_discovery'})
            assert page.locator('#rt-mode-meme').is_checked()
            assert 'MEME' in page.locator('#rt-state').inner_text()
            assert receipt in page.locator('#rt-candidate').inner_text()
            assert '\u9996\u6b21\u53d1\u73b0' in page.locator('#rt-entry').inner_text()
            assert page.locator('#rt-start').is_disabled()
            assert page.locator('#positions').count() == page.locator('#orders').count() == 1
            assert not errors
            print('Browser screenshots:', tmp_path)
        finally:
            browser.close()
