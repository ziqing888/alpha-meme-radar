"""Read-only public projection for the local terminal; no RPC or control calls.

terminal_snapshot(root=ROOT, now=None, sdk_outputs=None) returns exactly:
updated_at, wallet, chains, positions, orders, candidates, events, balances.
updated_at is collection time; row timestamps retain their source time. Atomic
amounts are decimal strings. Missing data is None, never an estimated zero.
Chain effective_config stays None until the daemon publishes an agreed schema.
Events are a bounded view of current statuses and orders, not a durable log.
Balances are native/USDT asset rows with null amounts and timestamps until a
separate balance collector is available; snapshot requests never perform RPC.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from alpha_live_dashboard import ROOT, SDK_OUTPUTS, numeric, project_status, read_object, timestamp
from alpha_chain_strategy import STRATEGY_VERSION, StrategyPolicy


CHAINS = {'bsc': 'okx-dex-sdk-live', 'robinhood': 'okx-dex-sdk-robinhood-live'}
MAX_ROWS = 200


def _text(value, limit=240):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else None


def _time(value):
    parsed = timestamp(value) if isinstance(value, str) else None
    return parsed.isoformat() if parsed else None


def _atomic(value):
    if type(value) not in (str, int):
        return None
    value = str(value)
    return value if re.fullmatch(r'-?\d{1,90}', value, flags=re.ASCII) else None


def _unsigned(value):
    value = _atomic(value)
    return value if value is not None and not value.startswith('-') else None


def _address(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', value) else None


def _tx(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{64}', value) else None


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _boolean(value):
    return value if type(value) is bool else None


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _pick(row, *names):
    return next((row[name] for name in names if row.get(name) is not None), None)


def _project(row, fields):
    return {key: clean(row.get(key)) for key, clean in fields.items()}


RANK_COMPONENT_KEYS = frozenset({
    'timing', 'discovery_timing', 'source_evidence', 'source_count',
    'liquidity', 'transaction_acceleration', 'tx_acceleration', 'narrative',
    'smart_money', 'holder_structure', 'holders',
})
POLICY_CHECK_KEYS = frozenset({
    'identity', 'supported_chain', 'first_snapshot', 'current_metrics',
    'rank_available', 'signal_stage', 'first_mcap', 'entry_age', 'markup',
    'liquidity', 'buy_route', 'sell_route', 'round_trip_loss', 'buy_impact',
    'sell_impact', 'hard_risk', 'sellability_confirmation', 'observed_sell',
})


def _numeric_map(value):
    if not isinstance(value, dict):
        return {}
    return {str(key)[:60]: clean for key, item in value.items()
            if key in RANK_COMPONENT_KEYS and (clean := numeric(item)) is not None}


def _boolean_map(value):
    if not isinstance(value, dict):
        return {}
    return {str(key)[:60]: item for key, item in value.items()
            if key in POLICY_CHECK_KEYS and type(item) is bool}


STRATEGY_FIELDS = {
    'strategy_version': _text, 'signal_stage': _text, 'entry_route': _text,
    'execution_mode': _text, 'rank_score': numeric, 'legacy_score': numeric,
    'rank_components': _numeric_map,
    'first_seen_at': _time, 'first_price_usd': numeric, 'first_mcap_usd': numeric,
    'current_price_usd': numeric, 'current_mcap_usd': numeric,
    'entry_delay_seconds': numeric, 'markup_from_first': numeric,
    'policy_checks': _boolean_map, 'eligible': _boolean, 'reject_reason': _text,
}
TRADEABILITY_FIELDS = {
    'status': _text, 'reject_reason': _text, 'liquidity_usd': numeric,
    'buy_impact_pct': numeric, 'sell_impact_pct': numeric,
    'round_trip_loss_pct': numeric, 'quote_at': _time,
}
MODEL_FIELDS = {
    'status': _text, 'model_version': _text, 'p_2x': numeric, 'p_5x': numeric,
    'expected_net_return_pct': numeric, 'sample_count': _integer,
}


def _nested_view(row, name, fields, aliases=None):
    nested = row.get(name)
    source = nested if isinstance(nested, dict) else row
    aliases = aliases or {}
    projected = {
        key: clean(_pick(source, key, *aliases.get(key, ())))
        for key, clean in fields.items()
    }
    return projected if isinstance(nested, dict) or any(value is not None for value in projected.values()) else None


def _strategy_view(row):
    nested = row.get('strategy')
    source = nested if isinstance(nested, dict) else row
    version = _text(source.get('strategy_version'))
    if not isinstance(nested, dict) and version != STRATEGY_VERSION:
        return None
    return _project(source, STRATEGY_FIELDS)


def _chain_strategy(chain):
    policy = asdict(StrategyPolicy.for_chain(chain))
    policy['allowed_recommendation_buckets'] = sorted(
        policy['allowed_recommendation_buckets']
    )
    return {'strategy_version': STRATEGY_VERSION,
            'signal_stage': policy.pop('live_signal_stage'),
            'entry_route': policy.pop('entry_route'),
            'execution_mode': 'live_candidate', **policy}


def _manual_reconciliation(row):
    pending = _integer(row.get('unreconciled_external_sales'))
    reconciled = _integer(row.get('manual_reconciled_sales'))
    if pending is not None and pending > 0:
        return {'status': 'unreconciled', 'count': pending, 'reconciled_count': reconciled}
    if pending == 0 and row.get('realized_pnl_status') == 'verified':
        return {'status': 'clear', 'count': 0, 'reconciled_count': reconciled}
    return {'status': 'unknown', 'count': None, 'reconciled_count': reconciled}


def _status_matches_chain(status, chain):
    claims = [status.get('chain')] if isinstance(status.get('chain'), str) else []
    receipt = status.get('terminal_control')
    if isinstance(receipt, dict) and isinstance(receipt.get('chain'), str):
        claims.append(receipt['chain'])
    return bool(claims) and all(value == chain for value in claims)


CHAIN_FIELDS = {
    'state': _text, 'updated_at': _time, 'activity_status': _text,
    'activity_reason': _text, 'state_label': _text, 'activity_message': _text,
    'open_positions': numeric, 'pending_orders': numeric,
    'native_symbol': _text, 'realized_pnl_native_atomic': _atomic,
    'realized_pnl_native': numeric,
    'realized_pnl_status': _text, 'realized_pnl_pending_count': _integer,
    'unverified_sell_fills': _integer, 'unreconciled_external_sales': _integer,
    'realized_pnl_method': _text, 'manual_reconciled_sales': _integer,
    'ledger_identity_status': _text,
}
POSITION_FIELDS = {
    'token': _address, 'symbol': _text, 'chain': _text,
    'entry_price_usd': numeric, 'current_price_usd': numeric, 'return_pct': numeric,
    'exit_status': _text, 'remaining_cost_usd': numeric,
    'realized_pnl_native_atomic': _atomic, 'remaining_atomic': _unsigned,
    'decimals': _integer,
}
POSITION_EXTRA = {
    'entry_at': _time, 'last_quote_at': _time, 'entry_native_atomic': _unsigned,
    'entry_native_spent_atomic': _unsigned, 'entry_notional_usd': numeric,
    'entry_native_price_usd': numeric, 'remaining_atomic': _unsigned,
    'original_atomic': _unsigned, 'tp1_hit': _boolean, 'tp2_hit': _boolean, 'tp3_hit': _boolean,
    'entry_price_usd': numeric,
    'last_price_usd': numeric, 'last_return_pct': numeric,
}
ORDER_EXTRA = {
    'amount_atomic': _unsigned, 'native_spent_atomic': _unsigned,
    'gas_native_atomic': _unsigned, 'cost_basis_native_atomic': _unsigned,
    'native_received_atomic': _unsigned, 'net_native_received_atomic': _atomic,
    'pnl_native_atomic': _atomic, 'fraction': numeric, 'return_pct': numeric,
}
ORDER_FIELDS = {
    'token': _address, 'symbol': _text, 'chain': _text, 'side': _text,
    'status': _text, 'tx_hash': _tx, 'time': _time, 'exit_reason': _text,
    'realized_pnl_usd_estimate': numeric, 'pnl_native': numeric,
    'native_symbol': _text, **ORDER_EXTRA,
}


def _sdk_indexes(outputs, chains, read):
    positions, fills = {}, {}
    for chain in chains:
        if chains[chain].get('ledger_identity_status') != 'verified':
            continue
        raw = read(outputs / f'{CHAINS[chain]}-state.json')
        if (raw.get('version') != 1 or not isinstance(raw.get('positions'), dict)
                or not isinstance(raw.get('closed'), list)):
            continue
        for row in raw['positions'].values():
            if not isinstance(row, dict):
                continue
            token = _address(_pick(row, 'token', 'contract_address'))
            if token and row.get('chain', chain) == chain:
                positions[chain, token] = row
        for row in _rows(raw.get('fills'))[-MAX_ROWS:]:
            token = _address(_pick(row, 'token', 'contract_address'))
            tx = _tx(row.get('tx_hash'))
            side = row.get('side')
            if token and tx and side in ('buy', 'sell') and row.get('chain', chain) == chain:
                fills[chain, token, tx, side] = row
    return positions, fills


def _candidates(outputs, read):
    result = []
    for chain in CHAINS:
        raw = read(outputs / f'{chain}-execution-input.json')
        for kind in ('signals', 'shadow_signals', 'rejections'):
            for row in _rows(raw.get(kind))[:MAX_ROWS]:
                token = _address(_pick(row, 'token', 'contract_address'))
                if not token or row.get('chain', chain) != chain:
                    continue
                reason = _pick(row, 'reject_reason', 'reason', 'execution_candidate_reason')
                if isinstance(reason, list):
                    reason = '; '.join(part for value in reason[:10] if (part := _text(value)))
                count = _integer(row.get('source_count'))
                groups = row.get('source_groups')
                if count is None and isinstance(groups, list):
                    count = len({item for item in groups if isinstance(item, str) and item})
                candidate = {
                    'chain': chain, 'symbol': _text(row.get('symbol'), 40), 'token': token,
                    'candidate_kind': {
                        'signals': 'active',
                        'shadow_signals': 'shadow',
                        'rejections': 'rejected',
                    }[kind],
                    'mcap': numeric(_pick(row, 'mcap', 'current_mcap_usd', 'market_cap', 'mcap_usd')),
                    'first_mcap': numeric(_pick(row, 'first_mcap', 'first_mcap_usd', 'first_seen_mcap')),
                    'liquidity': numeric(_pick(row, 'liquidity', 'liquidity_usd')),
                    'reason': _text(reason), 'source_count': count,
                    'score': numeric(_pick(row, 'legacy_score', 'execution_candidate_score', 'score', 'entry_score')),
                    'signal_at': _time(row.get('signal_at')),
                }
                strategy = _strategy_view(row)
                if strategy is not None:
                    candidate['strategy'] = strategy
                    tradeability = _nested_view(row, 'tradeability', TRADEABILITY_FIELDS, {
                        'status': ('tradeability_status',),
                        'reject_reason': ('tradeability_reject_reason',),
                    })
                    model = _nested_view(row, 'model', MODEL_FIELDS)
                    candidate['tradeability'] = tradeability
                    candidate['model'] = model
                result.append(candidate)
    return result


def _rejections(raw):
    result = []
    for row in _rows(raw.get('candidate_rejections')):
        reason = _text(row.get('reject_reason'))
        if reason:
            result.append({'symbol': _text(row.get('symbol'), 32),
                           'reject_reason': reason, 'score': numeric(row.get('score'))})
        if len(result) >= 5:
            break
    return result


def _unknown_balances():
    return [{
        'chain': chain, 'symbol': symbol, 'token': None,
        'decimals': 18 if symbol != 'USDT' else None,
        'balance': None, 'balance_atomic': None, 'updated_at': None,
        'status': 'not_collected',
    } for chain in CHAINS for symbol in ('BNB' if chain == 'bsc' else 'ETH', 'USDT')]


def terminal_snapshot(root: Path = ROOT, now: datetime | None = None,
                      sdk_outputs: Path | None = None) -> dict:
    """Read public dashboard/config identity and SDK outputs, without side effects."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    outputs = Path(sdk_outputs) if sdk_outputs is not None else (SDK_OUTPUTS if root == ROOT else root / 'outputs')
    cache = {}

    def read(path):
        if path not in cache:
            cache[path] = read_object(path)
        return cache[path]

    try:
        base = project_status(root=root, now=now, sdk_outputs=outputs, reader=read)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        # A partially published or malformed source is not a healthy empty ledger.
        base = {'state': 'unavailable'}
    sdk_mode = isinstance(base.get('chains'), list)
    sdk_chains = {}
    for row in _rows(base.get('chains')):
        chain = row.get('chain')
        if chain not in CHAINS:
            continue
        status = read(outputs / f'{CHAINS[chain]}-status.json')
        if _status_matches_chain(status, chain):
            sdk_chains[chain] = row
    positions_index, fills_index = _sdk_indexes(outputs, sdk_chains, read)
    chains = []
    for chain in CHAINS:
        raw = sdk_chains.get(chain, {
            'state': 'not_started' if base.get('configured') else base.get('state', 'unknown'),
            'ledger_identity_status': 'unverified',
        })
        projected = {
            'chain': chain, **_project(raw, CHAIN_FIELDS),
            'native_symbol': 'BNB' if chain == 'bsc' else 'ETH',
            'candidate_rejections': _rejections(raw),
            'effective_config': None, 'config_status': 'unknown',
        }
        projected['configured_strategy'] = _chain_strategy(chain)
        projected['effective_strategy'] = None
        projected['manual_reconciliation'] = _manual_reconciliation(projected)
        chains.append(projected)
    positions = []
    for row in _rows(base.get('positions')):
        clean = _project(row, POSITION_FIELDS)
        if clean['token'] is None or (sdk_mode and clean['chain'] not in sdk_chains):
            continue
        extra = positions_index.get((clean['chain'], clean['token']), {})
        for key, convert in POSITION_EXTRA.items():
            clean[key] = convert(extra.get(key)) if key in extra else clean.get(key)
        strategy = _strategy_view(extra)
        if strategy is not None:
            clean['strategy'] = strategy
        positions.append(clean)
    orders = []
    for row in _rows(base.get('orders')):
        clean = _project(row, ORDER_FIELDS)
        if clean['token'] is None or (sdk_mode and clean['chain'] not in sdk_chains):
            continue
        fill = fills_index.get((clean['chain'], clean['token'], clean['tx_hash'], clean['side']), {})
        for key, convert in ORDER_EXTRA.items():
            if key in fill:
                clean[key] = convert(fill[key])
        # Fill timestamps/accounting supersede the position summary for the same tx.
        if fill:
            clean['time'] = _time(fill.get('time')) or clean['time']
        explorer = ('https://bscscan.com/tx/' if clean['chain'] == 'bsc' else
                    'https://robinhoodchain.blockscout.com/tx/' if clean['chain'] == 'robinhood' else None)
        clean['explorer_url'] = explorer + clean['tx_hash'] if explorer and clean['tx_hash'] else None
        orders.append(clean)
    orders.sort(key=lambda row: row['time'] or '')
    events = [{
        'type': 'status', 'chain': row['chain'], 'time': row['updated_at'],
        'status': row['activity_status'], 'reason': row['activity_reason'],
        'token': None, 'symbol': None, 'side': None, 'tx_hash': None,
    } for row in chains if row['activity_status']]
    events.extend({
        'type': 'order', 'chain': row['chain'], 'time': row['time'],
        'status': row['status'], 'reason': row['exit_reason'], 'token': row['token'],
        'symbol': row['symbol'], 'side': row['side'], 'tx_hash': row['tx_hash'],
    } for row in orders)
    events.sort(key=lambda row: row['time'] or '', reverse=True)
    return {
        'updated_at': now.isoformat(), 'wallet': _address(base.get('wallet')),
        'chains': chains, 'positions': positions, 'orders': orders,
        'candidates': _candidates(outputs, read), 'events': events[:MAX_ROWS],
        'balances': _unknown_balances(),
    }
