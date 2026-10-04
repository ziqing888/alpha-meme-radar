"""Read-only candidate selection for an operator-started one-trade test."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import re

from alpha_bsc_execution_policy import CN_TZ, ExecutionConfig, entry_decision

ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
MARKET_FIELDS = ('price_usd', 'price', 'liquidity_usd', 'liquidity',
                 'quote_status', 'quote_at', 'quote_observed_at')


class ProbeError(RuntimeError):
    pass


def identity(row, pool=False):
    value = (row.get('pool_address') or row.get('pair_address')) if pool else (
        row.get('contract_address') or row.get('token_address') or row.get('address'))
    return value.lower() if isinstance(value, str) and ADDRESS.fullmatch(value) and int(value[2:], 16) else ''


def select_candidate(payload, now: datetime, *, account=None, token=None, pool=None):
    """Keep producer ordering and the live entry policy; never promote a rating."""
    try:
        updated = datetime.fromisoformat(payload['updated_at'].replace('Z', '+00:00'))
        if updated.tzinfo is None:
            raise ValueError
        age = (now - updated).total_seconds()
        signals, quotes = payload['signals'], payload['quotes']
        if not isinstance(signals, list) or not isinstance(quotes, list):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ProbeError('strategy_input_unavailable') from None
    if not 0 <= age <= 30:
        raise ProbeError('strategy_input_stale')
    account = account or {}
    counts = Counter(identity(row) for row in signals if isinstance(row, dict) and row.get('chain') == 'bsc')
    traded = {str(order.get('token', '')).lower() for order in account.get('orders', {}).values()}
    day = now.astimezone(CN_TZ).date().isoformat()
    for raw in signals:
        if not isinstance(raw, dict) or raw.get('chain') != 'bsc':
            continue
        key, pair = identity(raw), identity(raw, pool=True)
        if not key or not pair or counts[key] != 1 or key in traded:
            continue
        if token is not None and (key != token or pair != pool):
            continue
        matches = [q for q in quotes if isinstance(q, dict) and q.get('chain') == 'bsc'
                   and identity(q) == key and identity(q, pool=True) == pair]
        if len(matches) != 1:
            continue
        row = {**raw, **{k: matches[0][k] for k in MARKET_FIELDS if k in matches[0]},
               'contract_address': key, 'pool_address': pair,
               'open_positions': len(account.get('positions', {})), 'current_exposure_usd': 0,
               'daily_loss_usd': account.get('daily_losses', {}).get(day, 0),
               'existing_token_keys': [f'bsc:{t}' for t in account.get('positions', {})]}
        if entry_decision(row, now, ExecutionConfig.from_env()).accepted:
            symbol = raw.get('symbol')
            row['symbol'] = ''.join(c for c in symbol[:64] if c.isprintable()) if isinstance(symbol, str) else 'MEME'
            return row
    raise ProbeError('no_strategy_candidate')
