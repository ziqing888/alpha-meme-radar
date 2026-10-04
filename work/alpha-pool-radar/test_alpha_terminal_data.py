"""Local fixtures only: never contact an RPC or a live daemon."""
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
TOKEN = '0x' + '2' * 40
WALLET = '0x' + '1' * 40
BUY = '0x' + 'a' * 64
SELL = '0x' + 'b' * 64
STEMS = {'bsc': 'okx-dex-sdk-live', 'robinhood': 'okx-dex-sdk-robinhood-live'}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def adapter():
    return importlib.import_module('alpha_terminal_data')


@pytest.fixture
def local(tmp_path):
    root = tmp_path / 'config'
    outputs = root / 'outputs'
    outputs.mkdir(parents=True)
    write(root / 'okx-live-config.json', {'schema_version': 1, 'wallet_address': WALLET})
    return root, outputs


def sdk(outputs, chain='bsc', position=None, fills=None, **status):
    write(outputs / f'{STEMS[chain]}-status.json', {
        'provider': 'okx-dex-sdk', 'chain': chain, 'status': 'holding_existing_position',
        'updated_at': NOW.isoformat(), **status,
    })
    write(outputs / f'{STEMS[chain]}-state.json', {
        'version': 1, 'wallet': WALLET, 'chain': chain,
        'positions': {TOKEN: position} if position else {},
        'closed': [], 'fills': fills or [],
    })


def position(**changes):
    return {
        'token': TOKEN, 'symbol': 'TEST', 'buy_tx': BUY,
        'entry_at': NOW.isoformat(), 'last_quote_at': NOW.isoformat(),
        'entry_native_atomic': '1000', 'entry_native_spent_atomic': '1010',
        'entry_notional_usd': 5, 'entry_native_price_usd': 500,
        'remaining_atomic': '80', 'original_atomic': '100', 'tp1_hit': False, 'tp2_hit': False,
        'tp3_hit': False,
        'entry_price_usd': .05, 'last_price_usd': .06, 'last_return_pct': 20,
        'private_key': 'DO_NOT_EXPORT', 'raw': {'api_key': 'DO_NOT_EXPORT'},
        **changes,
    }


def test_empty_snapshot_contract_and_no_files_created(adapter, tmp_path):
    root = tmp_path / 'missing'
    result = adapter.terminal_snapshot(root=root, now=NOW)
    assert set(result) == {'updated_at', 'wallet', 'chains', 'positions', 'orders',
                           'candidates', 'events', 'balances'}
    assert result['updated_at'] == NOW.isoformat()
    assert result['wallet'] is None
    assert isinstance(result['balances'], list)
    assert [(row['chain'], row['symbol']) for row in result['balances']] == [
        ('bsc', 'BNB'), ('bsc', 'USDT'), ('robinhood', 'ETH'), ('robinhood', 'USDT')]
    assert all(row['balance'] is None and row['balance_atomic'] is None
               and row['updated_at'] is None and row['status'] == 'not_collected'
               for row in result['balances'])
    assert result['positions'] == result['orders'] == result['candidates'] == []
    assert [row['chain'] for row in result['chains']] == ['bsc', 'robinhood']
    assert all(row['effective_config'] is None and row['config_status'] == 'unknown'
               for row in result['chains'])
    assert not root.exists()
    json.dumps(result, allow_nan=False)


def test_two_chains_enriched_without_paper_policy(adapter, local):
    root, outputs = local
    sdk(outputs, position=position())
    sdk(outputs, 'robinhood', position(entry_native_atomic='2000'))
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['wallet'] == WALLET
    rows = {row['chain']: row for row in result['positions']}
    assert rows['bsc']['entry_native_atomic'] == '1000'
    assert rows['robinhood']['entry_native_atomic'] == '2000'
    for name in ('entry_at', 'last_quote_at', 'entry_native_spent_atomic',
                 'entry_notional_usd', 'entry_native_price_usd', 'remaining_atomic',
                 'original_atomic', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'entry_price_usd', 'last_price_usd',
                 'last_return_pct'):
        assert rows['bsc'][name] == position()[name]
    serialized = json.dumps(result, allow_nan=False)
    assert 'DO_NOT_EXPORT' not in serialized
    assert 'policy' not in serialized
    assert all(row['effective_config'] is None for row in result['chains'])


def test_fills_enrich_buy_summary_and_sell_by_chain(adapter, local):
    root, outputs = local
    for chain, spent in [('bsc', '1010'), ('robinhood', '2020')]:
        sdk(outputs, chain, position(), [
            {'token': TOKEN, 'tx_hash': BUY, 'side': 'buy', 'time': NOW.isoformat(),
             'native_spent_atomic': spent, 'gas_native_atomic': '0', 'amount_atomic': '100'},
            {'token': TOKEN, 'tx_hash': SELL, 'side': 'sell', 'time': NOW.isoformat(),
             'cost_basis_native_atomic': '202', 'native_received_atomic': '250',
             'net_native_received_atomic': '240', 'gas_native_atomic': '10',
             'pnl_native_atomic': '38', 'amount_atomic': '20', 'fraction': .2,
             'raw': {'key': 'DO_NOT_EXPORT'}},
        ])
    result = adapter.terminal_snapshot(root, NOW, outputs)
    orders = {(row['chain'], row['side']): row for row in result['orders']}
    assert len(orders) == 4
    assert orders['bsc', 'buy']['native_spent_atomic'] == '1010'
    assert orders['robinhood', 'buy']['native_spent_atomic'] == '2020'
    assert orders['bsc', 'buy']['gas_native_atomic'] == '0'
    assert orders['bsc', 'buy']['amount_atomic'] == '100'
    assert orders['bsc', 'sell']['cost_basis_native_atomic'] == '202'
    assert orders['bsc', 'sell']['net_native_received_atomic'] == '240'
    assert orders['bsc', 'sell']['pnl_native_atomic'] == '38'
    assert 'DO_NOT_EXPORT' not in json.dumps(result)


def test_candidates_aliases_zero_values_and_rejections(adapter, local):
    root, outputs = local
    write(outputs / 'bsc-execution-input.json', {
        'signals': [{'contract_address': TOKEN, 'symbol': 'TEST', 'mcap': 0,
                     'current_mcap_usd': 999, 'first_mcap_usd': 10000, 'liquidity': 2000,
                     'execution_candidate_score': 0, 'entry_score': 77,
                     'source_groups': ['okx', 'gmgn', 'okx'],
                     'execution_candidate_reason': ['fresh', 'confirmed'],
                     'signal_at': NOW.isoformat(), 'private_key': 'DO_NOT_EXPORT'}],
        'rejections': [{'contract_address': TOKEN, 'symbol': 'NO', 'score': 12,
                        'reject_reason': 'too_late', 'current_mcap_usd': 300000}],
        'quotes': [{'raw': 'DO_NOT_EXPORT'}],
    })
    write(outputs / 'robinhood-execution-input.json', {
        'signals': [{'token': TOKEN, 'symbol': 'RH', 'source_count': 0}],
    })
    result = adapter.terminal_snapshot(root, NOW, outputs)
    signal, rejected, robinhood = result['candidates']
    assert set(signal) == {'chain', 'symbol', 'token', 'mcap', 'first_mcap', 'liquidity',
                           'reason', 'source_count', 'score', 'signal_at', 'candidate_kind'}
    assert signal['candidate_kind'] == 'active'
    assert signal['mcap'] == signal['score'] == 0
    assert signal['first_mcap'] == 10000
    assert signal['source_count'] == 2
    assert signal['reason'] == 'fresh; confirmed'
    assert rejected['reason'] == 'too_late'
    assert rejected['candidate_kind'] == 'rejected'
    assert rejected['mcap'] == 300000
    assert rejected['signal_at'] is None
    assert robinhood['chain'] == 'robinhood' and robinhood['source_count'] == 0
    assert robinhood['candidate_kind'] == 'active'
    assert 'DO_NOT_EXPORT' not in json.dumps(result)


def test_v2_candidates_include_live_shadow_strategy_tradeability_and_model(adapter, local):
    root, outputs = local
    strategy = {
        'strategy_version': 'chain_v2', 'signal_stage': 'aggregate_early_bird',
        'entry_route': 'bsc_aggregate_early_bird', 'execution_mode': 'live_candidate',
        'rank_score': 95, 'rank_components': {'timing': 25, 'wallet_score': 99, 'bad': 'secret'},
        'first_seen_at': NOW.isoformat(), 'first_price_usd': .01,
        'first_mcap_usd': 20_000, 'current_price_usd': .012,
        'current_mcap_usd': 24_000, 'entry_delay_seconds': 30,
        'markup_from_first': 1.2, 'policy_checks': {
            'liquidity': True, 'arbitrary_browser_key': True, 'raw': 'drop'},
        'eligible': True, 'reject_reason': '', 'private_key': 'DO_NOT_EXPORT',
    }
    write(outputs / 'bsc-execution-input.json', {
        'signals': [{
            'token': TOKEN, 'symbol': 'LIVE', **strategy,
            'tradeability': {'status': 'passed', 'reject_reason': None,
                             'liquidity_usd': 25_000, 'buy_impact_pct': 2,
                             'sell_impact_pct': 3, 'round_trip_loss_pct': 7,
                             'quote_at': NOW.isoformat(), 'raw': 'DO_NOT_EXPORT'},
            'model': {'status': 'ready', 'model_version': 'v2-shadow', 'p_2x': .4,
                      'p_5x': .1, 'expected_net_return_pct': 20,
                      'sample_count': 30, 'raw': 'DO_NOT_EXPORT'},
        }],
        'shadow_signals': [{
            'token': '0x' + '3' * 40, 'symbol': 'SHADOW',
            **(strategy | {'signal_stage': 'aggregate_discovery',
                           'entry_route': 'bsc_aggregate_discovery_shadow',
                           'execution_mode': 'shadow', 'eligible': False,
                           'reject_reason': 'bsc_shadow_only'}),
            'tradeability_status': 'unavailable', 'p_2x': None,
            'p_5x': 0, 'expected_net_return_pct': None, 'sample_count': 0,
        }],
    })

    rows = {row['symbol']: row for row in adapter.terminal_snapshot(root, NOW, outputs)['candidates']}

    assert rows['LIVE']['strategy']['rank_score'] == 95
    assert rows['LIVE']['candidate_kind'] == 'active'
    assert rows['LIVE']['strategy']['rank_components'] == {'timing': 25}
    assert rows['LIVE']['strategy']['policy_checks'] == {'liquidity': True}
    assert rows['LIVE']['tradeability'] == {
        'status': 'passed', 'reject_reason': None, 'liquidity_usd': 25_000,
        'buy_impact_pct': 2, 'sell_impact_pct': 3, 'round_trip_loss_pct': 7,
        'quote_at': NOW.isoformat(),
    }
    assert rows['LIVE']['model']['model_version'] == 'v2-shadow'
    assert rows['SHADOW']['strategy']['execution_mode'] == 'shadow'
    assert rows['SHADOW']['candidate_kind'] == 'shadow'
    assert rows['SHADOW']['model']['p_2x'] is None
    assert rows['SHADOW']['model']['p_5x'] == 0
    assert 'DO_NOT_EXPORT' not in json.dumps(rows)


def test_position_uses_only_persisted_entry_strategy(adapter, local):
    root, outputs = local
    persisted = {
        'strategy_version': 'chain_v2', 'signal_stage': 'aggregate_discovery',
        'entry_route': 'robinhood_aggregate_discovery', 'execution_mode': 'live_candidate',
        'rank_score': 88, 'first_mcap_usd': 22_000, 'private_key': 'DO_NOT_EXPORT',
    }
    sdk(outputs, 'robinhood', position=position(strategy=persisted))

    row = adapter.terminal_snapshot(root, NOW, outputs)['positions'][0]

    assert row['strategy']['rank_score'] == 88
    assert row['strategy']['entry_route'] == 'robinhood_aggregate_discovery'
    assert 'private_key' not in row['strategy']


def test_chain_strategy_and_manual_reconciliation_are_explicit(adapter, local):
    root, outputs = local
    sdk(outputs)
    write(outputs / 'okx-dex-sdk-live-state.json', {
        'version': 1, 'wallet': WALLET, 'chain': 'bsc', 'positions': {}, 'closed': [],
        'external_reconciliations': [{
            'token': TOKEN, 'reduced_atomic': '10', 'proceeds_status': 'unreconciled',
        }],
    })

    chain = adapter.terminal_snapshot(root, NOW, outputs)['chains'][0]

    assert chain['configured_strategy']['strategy_version'] == 'chain_v2'
    assert chain['configured_strategy']['signal_stage'] == 'aggregate_early_bird'
    assert chain['configured_strategy']['order_notional_usd'] == 1
    assert chain['configured_strategy']['allowed_recommendation_buckets'] == ['ambush', 'lead']
    assert 'strategy' not in chain
    assert chain['effective_strategy'] is None
    assert chain['manual_reconciliation'] == {
        'status': 'unreconciled', 'count': 1, 'reconciled_count': 0,
    }
    assert chain['realized_pnl_native'] is None


@pytest.mark.parametrize('bad', [True, {}, [], 'NaN', 'Infinity'])
def test_nonfinite_and_container_values_do_not_escape(adapter, local, bad):
    root, outputs = local
    sdk(outputs, position=position(entry_notional_usd=bad, original_atomic=bad,
                                   entry_at=bad, tp1_hit=bad))
    write(outputs / 'bsc-execution-input.json', {
        'signals': [{'token': TOKEN, 'symbol': {'private_key': 'DO_NOT_EXPORT'},
                     'score': bad, 'reason': {'key': 'DO_NOT_EXPORT'}}],
    })
    result = adapter.terminal_snapshot(root, NOW, outputs)
    row = result['positions'][0]
    assert row['entry_notional_usd'] is None
    assert row['original_atomic'] is None
    assert row['entry_at'] is None
    assert result['candidates'][0]['score'] is None
    assert 'DO_NOT_EXPORT' not in json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('content', ['{', '[]', 'null', '\ufeff{}'])
def test_missing_or_partial_optional_files(adapter, local, content):
    root, outputs = local
    sdk(outputs)
    (outputs / 'bsc-execution-input.json').write_text(content, encoding='utf-8')
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['candidates'] == []
    assert result['chains'][0]['state'] == 'running'


def test_stale_heartbeat_events_preserve_source_time(adapter, local):
    root, outputs = local
    old = '2026-09-08T12:00:00+00:00'
    sdk(outputs, updated_at=old, reason='exit_conditions_not_met')
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['chains'][0]['state'] == 'stale'
    event = next(row for row in result['events'] if row['chain'] == 'bsc')
    assert event['time'] == old
    assert event['type'] == 'status'
    assert event['reason'] == 'exit_conditions_not_met'


def test_wrong_internal_status_chain_is_quarantined_from_projection(adapter, local):
    root, outputs = local
    sdk(outputs, position=position())
    write(outputs / 'okx-dex-sdk-live-status.json', {
        'provider': 'okx-dex-sdk', 'chain': 'robinhood',
        'status': 'holding_existing_position', 'updated_at': NOW.isoformat(),
    })

    result = adapter.terminal_snapshot(root, NOW, outputs)

    bsc = next(row for row in result['chains'] if row['chain'] == 'bsc')
    assert bsc['state'] != 'running'
    assert bsc['ledger_identity_status'] == 'unverified'
    assert not any(row['chain'] == 'bsc' for row in result['positions'])


def test_status_chain_identity_can_come_from_terminal_receipt(adapter, local):
    root, outputs = local
    sdk(outputs, position=position())
    write(outputs / 'okx-dex-sdk-live-status.json', {
        'provider': 'okx-dex-sdk', 'status': 'holding_existing_position',
        'updated_at': NOW.isoformat(),
        'terminal_control': {'chain': 'bsc', 'wallet': WALLET, 'pid': 11},
    })

    result = adapter.terminal_snapshot(root, NOW, outputs)

    assert result['chains'][0]['state'] == 'running'
    assert result['positions'][0]['chain'] == 'bsc'


def test_snapshot_reads_only_expected_public_files_and_never_writes(adapter, local, monkeypatch):
    root, outputs = local
    sdk(outputs, position=position())
    allowed = {root / 'okx-live-config.json'}
    allowed.update(outputs / f'{stem}-{kind}.json' for stem in STEMS.values()
                   for kind in ('status', 'state'))
    allowed.update(outputs / f'{stem}-realized-reconciliation.json' for stem in STEMS.values())
    allowed.update(outputs / f'{chain}-execution-input.json' for chain in STEMS)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    original = Path.read_text

    def read_only(path, *args, **kwargs):
        assert path in allowed, f'unexpected read: {path}'
        return original(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('snapshot attempted a write or network request')

    monkeypatch.setattr(Path, 'read_text', read_only)
    monkeypatch.setattr(Path, 'write_text', forbidden)
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    import socket
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    assert adapter.terminal_snapshot(root, NOW, outputs)['positions']
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('changes', [
    {'token': '0x' + '3' * 40}, {'chain': 'robinhood'}, {'side': 'sell'},
])
def test_conflicting_fill_identity_cannot_enrich_buy(adapter, local, changes):
    root, outputs = local
    sdk(outputs, position=position(), fills=[{
        'token': TOKEN, 'side': 'buy', 'tx_hash': BUY, 'time': NOW.isoformat(),
        'native_spent_atomic': '999999', **changes,
    }])
    result = adapter.terminal_snapshot(root, NOW, outputs)
    buy = next(row for row in result['orders'] if row['side'] == 'buy')
    assert buy['native_spent_atomic'] is None


def test_candidate_and_event_counts_are_bounded(adapter, local):
    root, outputs = local
    write(outputs / 'bsc-execution-input.json', {
        'signals': [{'token': TOKEN, 'symbol': 'TEST'}] * 1000,
        'rejections': 'not-a-list',
    })
    sdk(outputs, fills=[{
        'token': TOKEN, 'side': 'sell', 'tx_hash': '0x' + f'{i:064x}',
        'time': NOW.isoformat(),
    } for i in range(1000)])
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert len(result['candidates']) == 200
    assert len(result['events']) == 200


def test_path_defaults_reuse_dashboard_outputs_and_isolate_fixtures(adapter, local, monkeypatch):
    root, outputs = local
    sdk(outputs, position=position())
    assert adapter.terminal_snapshot(root, NOW)['positions']
    monkeypatch.setattr(adapter, 'ROOT', root)
    monkeypatch.setattr(adapter, 'SDK_OUTPUTS', outputs)
    assert adapter.terminal_snapshot(root, NOW)['positions']


@pytest.mark.parametrize('state', [
    {'version': 2, 'positions': {}, 'closed': []},
    {'version': 1, 'positions': [], 'closed': []},
    {'version': 1, 'positions': {}, 'closed': [], 'fills': 123},
])
def test_malformed_sdk_state_never_claims_healthy_empty_ledger(adapter, local, state):
    root, outputs = local
    sdk(outputs)
    write(outputs / 'okx-dex-sdk-live-state.json', state)
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['chains'][0]['state'] in {'degraded', 'unavailable'}
    assert result['positions'] == []
    json.dumps(result, allow_nan=False)


def test_closed_position_orders_keep_fill_accounting(adapter, local):
    root, outputs = local
    sdk(outputs)
    write(outputs / 'okx-dex-sdk-live-state.json', {
        'version': 1, 'wallet': WALLET, 'chain': 'bsc', 'positions': {},
        'closed': [{**position(), 'sell_tx': SELL, 'closed_at': NOW.isoformat()}],
        'fills': [{
            'token': TOKEN, 'side': 'sell', 'tx_hash': SELL, 'time': NOW.isoformat(),
            'cost_basis_native_atomic': '1010', 'pnl_native_atomic': '-10',
        }],
    })
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['positions'] == []
    assert len(result['orders']) == 2
    sell = next(row for row in result['orders'] if row['side'] == 'sell')
    assert sell['cost_basis_native_atomic'] == '1010'
    assert sell['pnl_native_atomic'] == '-10'


def test_naive_collection_time_rejected(adapter, local):
    with pytest.raises(ValueError, match='timezone-aware'):
        adapter.terminal_snapshot(local[0], NOW.replace(tzinfo=None))


@pytest.mark.parametrize('identity,expected', [
    ({}, 'unverified'), ({'wallet': WALLET}, 'unverified'),
    ({'wallet': '0x' + '3' * 40, 'chain': 'bsc'}, 'mismatch'),
    ({'wallet': WALLET, 'chain': 'robinhood'}, 'mismatch'),
])
@pytest.mark.parametrize('has_history', [False, True])
def test_ledger_identity_not_inferred_from_current_status(adapter, local, identity, expected, has_history):
    root, outputs = local
    sdk(outputs, wallet=WALLET, chain_id=56)
    write(outputs / 'okx-dex-sdk-live-state.json', {
        'version': 1, 'positions': {},
        'closed': [position()] if has_history else [],
        'fills': [{'token': TOKEN, 'side': 'buy', 'tx_hash': BUY,
                   'native_spent_atomic': '1000'}] if has_history else [],
        'realized_pnl_native_atomic': '100', **identity,
    })
    result = adapter.terminal_snapshot(root, NOW, outputs)
    chain = result['chains'][0]
    assert result['wallet'] == WALLET
    assert result['orders'] == result['positions'] == []
    assert chain['ledger_identity_status'] == expected
    assert chain['open_positions'] is None and chain['pending_orders'] is None
    assert chain['realized_pnl_native'] is None
    assert chain['state'] != 'running'


def test_verified_empty_ledger_is_distinct_from_missing_state(adapter, local):
    root, outputs = local
    sdk(outputs)
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['chains'][0]['ledger_identity_status'] == 'verified'
    assert result['chains'][0]['open_positions'] == 0
    (outputs / 'okx-dex-sdk-live-state.json').unlink()
    result = adapter.terminal_snapshot(root, NOW, outputs)
    assert result['chains'][0]['ledger_identity_status'] == 'unverified'
    assert result['chains'][0]['open_positions'] is None


def test_snapshot_reads_each_file_once_and_uses_one_state_generation(adapter, local, monkeypatch):
    root, outputs = local
    sdk(outputs, position=position(last_price_usd=2, last_return_pct=100))
    original, reads = Path.read_text, {}

    def changing_read(path, *args, **kwargs):
        reads[path] = reads.get(path, 0) + 1
        value = original(path, *args, **kwargs)
        if path.name == 'okx-dex-sdk-live-state.json' and reads[path] > 1:
            raw = json.loads(value)
            raw['positions'][TOKEN].update(last_price_usd=3, last_return_pct=200,
                                          remaining_atomic='60')
            return json.dumps(raw)
        return value

    monkeypatch.setattr(Path, 'read_text', changing_read)
    result = adapter.terminal_snapshot(root, NOW, outputs)
    row = result['positions'][0]
    assert row['current_price_usd'] == row['last_price_usd'] == 2
    assert row['return_pct'] == row['last_return_pct'] == 100
    assert row['remaining_atomic'] == '80'
    assert all(count == 1 for count in reads.values())


def test_chain_rejections_are_separate_public_projections(adapter, local):
    root, outputs = local
    for chain, symbol in [('bsc', 'BSC_NO'), ('robinhood', 'RH_NO')]:
        sdk(outputs, chain, candidate_rejections=[{
            'symbol': symbol, 'reject_reason': 'too_late', 'score': 12,
            'raw': {'key': 'DO_NOT_EXPORT'},
        }] * 9)
    result = adapter.terminal_snapshot(root, NOW, outputs)
    for row, symbol, native in zip(result['chains'], ['BSC_NO', 'RH_NO'], ['BNB', 'ETH']):
        assert row['native_symbol'] == native
        assert len(row['candidate_rejections']) == 5
        assert row['candidate_rejections'][0] == {
            'symbol': symbol, 'reject_reason': 'too_late', 'score': 12,
        }
    assert 'DO_NOT_EXPORT' not in json.dumps(result)


def test_chain_pnl_verification_counts_survive_terminal_projection(adapter, local):
    root, outputs = local
    sdk(outputs)
    write(outputs / 'okx-dex-sdk-live-state.json', {
        'version': 1, 'wallet': WALLET, 'chain': 'bsc',
        'positions': {}, 'closed': [],
        'realized_pnl_native_atomic': '-10',
        'fills': [{
            'token': TOKEN, 'side': 'sell', 'tx_hash': SELL,
            'time': NOW.isoformat(), 'pnl_native_atomic': '-10',
        }],
        'external_reconciliations': [{
            'token': TOKEN, 'proceeds_status': 'unreconciled',
        }],
    })

    chain = adapter.terminal_snapshot(root, NOW, outputs)['chains'][0]

    assert chain['realized_pnl_status'] == 'pending_verification'
    assert chain['realized_pnl_pending_count'] == 2
    assert chain['unverified_sell_fills'] == 1
    assert chain['unreconciled_external_sales'] == 1
    assert chain['realized_pnl_native'] is None


def test_terminal_copy_uses_long_tail_exit_rules_and_pnl_verification():
    ui = Path(__file__).with_name('terminal-ui') / 'src'
    strategy = (ui / 'components' / 'terminal-strategy.tsx').read_text(encoding='utf-8')
    terminal = (ui / 'Terminal.tsx').read_text(encoding='utf-8')

    assert '2x 卖出 50% · 3x 卖出 10% · 5x 卖出 10% · 剩余 30% 回撤 40%' in strategy
    assert '首盈前最长 90 分钟 · Runner 最长 24 小时' in strategy
    assert 'realized_pnl_status === "verified"' in terminal
    assert 'realized_pnl_pending_count' in terminal
    assert 'data.session ? "V2 已实现盈亏 · 原生币"' in terminal
    assert 'data.session ? "V2 成交记录"' in terminal
    assert '涨幅 100% 卖出 80%' not in strategy
