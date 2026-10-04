"""Loopback ledger view with an explicitly confirmed one-shot test control."""
from __future__ import annotations

import argparse
import hmac
import json
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from alpha_bsc_execution_policy import ExecutionConfig
from alpha_live_roundtrip_control import RoundtripControl, valid_post

ROOT = Path.home() / '.config' / 'alpha-radar'
SDK_OUTPUTS = Path(__file__).resolve().parents[2] / 'outputs'
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
TX = re.compile(r'0x[0-9a-fA-F]{64}')
ATOMIC = re.compile(r'-?\d{1,90}')
SELL_SETTLEMENT_FIELDS = (
    'cost_basis_native_atomic',
    'native_received_atomic',
    'net_native_received_atomic',
    'gas_native_atomic',
    'pnl_native_atomic',
)


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except ValueError:
        return None


def _receipt_reconciliation_status(state_data: dict, reconciliation: dict,
                                   chain: str, wallet: str) -> dict | None:
    """Validate a receipt/FIFO sidecar against every known sell before using it."""
    native_symbol = 'BNB' if chain == 'bsc' else 'ETH'
    if (reconciliation.get('schema_version') != 1
            or str(reconciliation.get('wallet') or '').lower() != wallet.lower()
            or reconciliation.get('chain') != chain
            or reconciliation.get('native_symbol') != native_symbol
            or reconciliation.get('calculation') != 'receipt_fifo_native_equivalent_v1'
            or not isinstance(reconciliation.get('records'), list)):
        return None

    worker_sells = {}
    anonymous_worker_sells = 0
    for item in list(state_data.get('fills') or []) + list(state_data.get('closed') or []):
        if not isinstance(item, dict):
            continue
        is_sell = item.get('side') == 'sell' or bool(item.get('sell_tx'))
        if not is_sell:
            continue
        tx_hash = str(item.get('tx_hash') or item.get('sell_tx') or '').lower()
        token = str(item.get('token') or item.get('contract_address') or '').lower()
        if TX.fullmatch(tx_hash) and ADDRESS.fullmatch(token):
            if tx_hash in worker_sells and worker_sells[tx_hash] != token:
                return None
            worker_sells[tx_hash] = token
        elif not TX.fullmatch(tx_hash):
            anonymous_worker_sells += 1

    external_rows = []
    external_tokens = set()
    raw_external = state_data.get('external_reconciliations', [])
    if not isinstance(raw_external, list):
        return None
    for item in raw_external:
        if not isinstance(item, dict) or item.get('proceeds_status') == 'reconciled':
            continue
        token = str(item.get('token') or '').lower()
        amount = str(item.get('reduced_atomic') or '')
        if not ADDRESS.fullmatch(token) or not amount.isdigit():
            return None
        external_rows.append((token, amount))
        external_tokens.add(token)

    ledger_items = list((state_data.get('positions') or {}).values()) + list(state_data.get('closed') or [])
    for item in ledger_items:
        if not isinstance(item, dict) or item.get('accounting_status') != 'external_proceeds_unreconciled':
            continue
        token = str(item.get('token') or item.get('contract_address') or '').lower()
        if token in external_tokens:
            continue
        amount = str(item.get('external_reduction_atomic') or '')
        if not ADDRESS.fullmatch(token) or not amount.isdigit():
            return None
        external_rows.append((token, amount))
        external_tokens.add(token)

    worker_records = {}
    external_records = []
    pnl_total = 0
    valid_record_count = 0
    for item in reconciliation['records']:
        if not isinstance(item, dict):
            return None
        tx_hash = str(item.get('tx_hash') or '').lower()
        token = str(item.get('token') or '').lower()
        source = item.get('source')
        amount = str(item.get('amount_atomic') or '')
        pnl = str(item.get('pnl_native_atomic') or '')
        if (not TX.fullmatch(tx_hash) or not ADDRESS.fullmatch(token)
                or source not in {'worker', 'external'} or not amount.isdigit()
                or not ATOMIC.fullmatch(pnl)):
            return None
        valid_record_count += 1
        pnl_total += int(pnl)
        if source == 'worker':
            if tx_hash in worker_records:
                return None
            worker_records[tx_hash] = item
        else:
            external_records.append(item)

    covered_workers = {
        tx_hash for tx_hash, token in worker_sells.items()
        if tx_hash in worker_records
        and str(worker_records[tx_hash].get('token') or '').lower() == token
    }
    unmatched_worker_records = set(worker_records) - covered_workers
    remaining_external = list(external_records)
    covered_external = 0
    for token, amount in external_rows:
        match = next((item for item in remaining_external
                      if str(item.get('token') or '').lower() == token
                      and str(item.get('amount_atomic') or '') == amount), None)
        if match is not None:
            remaining_external.remove(match)
            covered_external += 1

    uncovered_workers = len(worker_sells) - len(covered_workers) + anonymous_worker_sells
    uncovered_external = len(external_rows) - covered_external
    exact_coverage = (uncovered_workers == 0 and uncovered_external == 0
                      and not unmatched_worker_records and not remaining_external
                      and valid_record_count == len(worker_sells) + len(external_rows))
    stated_total = reconciliation.get('realized_pnl_native_atomic')
    if stated_total is not None and (not ATOMIC.fullmatch(str(stated_total))
                                     or int(stated_total) != pnl_total):
        exact_coverage = False

    return {
        'realized_pnl_status': 'verified' if exact_coverage else 'pending_verification',
        'realized_pnl_pending_count': 0 if exact_coverage else uncovered_workers + uncovered_external,
        'unverified_sell_fills': 0 if exact_coverage else uncovered_workers,
        'unreconciled_external_sales': 0 if exact_coverage else uncovered_external,
        'realized_pnl_native_atomic': str(pnl_total) if exact_coverage else None,
        'realized_pnl_method': reconciliation['calculation'] if exact_coverage else None,
        'manual_reconciled_sales': len(external_rows) if exact_coverage else 0,
    }


def sell_settlement_status(state_data: dict, reconciliation: dict | None = None,
                           chain: str | None = None, wallet: str | None = None) -> dict:
    """Summarize whether the chain-level realized PnL is safe to display."""
    if reconciliation and chain and wallet:
        reconciled = _receipt_reconciliation_status(state_data, reconciliation, chain, wallet)
        if reconciled is not None:
            return reconciled
    fills = state_data.get('fills') or []
    complete_by_tx = {}
    anonymous_incomplete = 0
    for item in fills:
        if not isinstance(item, dict) or item.get('side') != 'sell':
            continue
        tx_hash = str(item.get('tx_hash') or '')
        complete = all(ATOMIC.fullmatch(str(item.get(field) or ''))
                       for field in SELL_SETTLEMENT_FIELDS)
        if TX.fullmatch(tx_hash):
            complete_by_tx[tx_hash] = complete_by_tx.get(tx_hash, False) or complete
        else:
            anonymous_incomplete += 1

    for item in state_data.get('closed') or []:
        if not isinstance(item, dict):
            continue
        tx_hash = str(item.get('sell_tx') or '')
        if TX.fullmatch(tx_hash) and tx_hash not in complete_by_tx:
            complete_by_tx[tx_hash] = False

    sell_count = anonymous_incomplete + len(complete_by_tx)
    incomplete_sells = anonymous_incomplete + sum(not complete for complete in complete_by_tx.values())

    external_rows = state_data.get('external_reconciliations', [])
    if not isinstance(external_rows, list):
        unreconciled_external = 1
        external_tokens = set()
    else:
        unresolved_rows = [item for item in external_rows if isinstance(item, dict)
                           and item.get('proceeds_status') != 'reconciled']
        unreconciled_external = len(unresolved_rows)
        external_tokens = {
            str(item.get('token') or '').lower() for item in unresolved_rows
            if ADDRESS.fullmatch(str(item.get('token') or ''))
        }

    ledger_items = list((state_data.get('positions') or {}).values()) + list(state_data.get('closed') or [])
    for item in ledger_items:
        if not isinstance(item, dict) or item.get('accounting_status') != 'external_proceeds_unreconciled':
            continue
        token = str(item.get('token') or item.get('contract_address') or '').lower()
        if token not in external_tokens:
            unreconciled_external += 1
            external_tokens.add(token)

    realized_atomic = str(state_data.get('realized_pnl_native_atomic', ''))
    aggregate_missing = sell_count > 0 and not ATOMIC.fullmatch(realized_atomic)
    # Executor-authored amounts are not independent proof. Every historical
    # sell remains pending until the receipt reconciliation sidecar covers it.
    unverified_sells = sell_count
    pending_count = sell_count + unreconciled_external + int(aggregate_missing and not incomplete_sells)
    verified = pending_count == 0 and sell_count == 0
    if verified and sell_count == 0 and not ATOMIC.fullmatch(realized_atomic):
        realized_atomic = '0'
    return {
        'realized_pnl_status': 'verified' if verified else 'pending_verification',
        'realized_pnl_pending_count': pending_count,
        'unverified_sell_fills': unverified_sells,
        'unreconciled_external_sales': unreconciled_external,
        'realized_pnl_native_atomic': realized_atomic if verified and ATOMIC.fullmatch(realized_atomic) else None,
        'realized_pnl_method': 'no_realized_sales' if verified else None,
        'manual_reconciled_sales': 0,
    }


def sdk_project_status(base: dict, outputs: Path, now: datetime,
                       chain: str = 'bsc', *, reader=None) -> dict | None:
    read = reader or read_object
    if chain not in {'bsc', 'robinhood'}:
        return None
    stem = 'okx-dex-sdk-live' if chain == 'bsc' else f'okx-dex-sdk-{chain}-live'
    chain_label = 'BSC' if chain == 'bsc' else 'Robinhood Chain'
    explorer = 'https://bscscan.com/tx/' if chain == 'bsc' else 'https://robinhoodchain.blockscout.com/tx/'
    path = outputs / f'{stem}-status.json'
    if not path.exists():
        return None
    raw = read(path)
    if raw.get('provider') != 'okx-dex-sdk':
        return None
    updated = timestamp(raw.get('updated_at'))
    age = (now - updated).total_seconds() if updated else None
    fresh = age is not None and 0 <= age <= 90
    activity = str(raw.get('status', ''))
    reason = str(raw.get('reason', ''))
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', activity):
        activity = 'sdk_status_unavailable'
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', reason):
        reason = None

    healthy = {
        'waiting_for_strategy_candidate', 'holding_existing_position',
        'position_opened', 'position_reduced', 'position_closed',
        'exit_only',
    }
    attention = {'risk_paused', 'buy_blocked', 'sell_blocked'}
    state = ('stale' if not fresh else 'running' if activity in healthy
             else 'attention' if activity in attention else 'degraded')
    activity_labels = {
        'waiting_for_strategy_candidate': '等待策略候选',
        'holding_existing_position': '持仓监控中',
        'position_opened': '买入已确认',
        'position_reduced': '部分卖出已确认',
        'position_closed': '卖出已确认',
        'exit_only': '只管理持仓',
        'risk_paused': '风控暂停',
        'buy_blocked': '买入受阻',
        'sell_blocked': '卖出受阻',
        'blocked': '执行受阻',
    }
    activity_messages = {
        'waiting_for_strategy_candidate': f'尚未成交，正在等待策略写入合格 {chain_label} 候选。',
        'holding_existing_position': '已有真实持仓，执行器正在监控退出条件。',
        'position_opened': '买入交易已由链上回执确认。',
        'position_reduced': '部分卖出交易已由链上回执确认。',
        'position_closed': '卖出交易已由链上回执确认，仓位已关闭。',
        'exit_only': '只管理现有持仓，已关闭新增买入。',
        'risk_paused': '已触发仓位限制，当前暂停新增买入。',
        'buy_blocked': '买入没有完成，请查看执行原因。',
        'sell_blocked': '卖出没有完成，请查看执行原因。',
        'blocked': ('Robinhood Chain 钱包缺少 ETH，等待入金后自动继续。'
                    if chain == 'robinhood' and reason == 'insufficient_native_balance'
                    else '执行器检查未通过，请查看执行原因。'),
    }
    reason_messages = {
        'first_discovery_mcap': '当前候选已超出首次发现市值范围，已跳过并继续等待下一条。',
        'narrative_breakout_mcap': '当前候选不在大叙事突破市值范围，已跳过。',
        'narrative_breakout_liquidity': '当前候选流动性不足，已跳过。',
        'narrative_breakout_sources': '当前候选缺少 OKX 与 GMGN/DS 的多源确认，已跳过。',
        'momentum_gate': '当前候选涨跌速度超出策略范围，已跳过追高。',
        'stale_signal': '候选信号已过期，继续等待新信号。',
        'stale_quote': '行情报价已过期，等待刷新后再判断。',
        'risk_flags': '当前候选带有严重风险标签，已跳过。',
        'buy_simulation_failed': '买入报价未通过链上模拟，未签名，等待新报价。',
        'sell_simulation_failed': '卖出报价未通过链上模拟，未签名，等待新报价。',
        'insufficient_native_balance': (f'{chain_label} 钱包缺少 '
                                        f'{"ETH" if chain == "robinhood" else "BNB"}，等待入金后自动继续。'),
        'new_entries_disabled': '只管理现有持仓，已关闭新增买入。',
        'partial_exit_cost_too_high': '分批卖出金额相对 Gas 太小，暂缓本档并继续监控。',
        'bsc_validated_first_discovery_only': 'BSC 实盘只买入回测范围内的首次发现候选。',
        'bsc_validated_first_mcap': '首次发现市值不在 1万至10万美元的实盘范围，已跳过。',
        'bsc_validated_markup': '当前市值相对首次发现已超过 2.2 倍，已跳过追高。',
    }
    state_label = ('OKX DEX SDK · 心跳已过期' if not fresh else
                   f"OKX DEX SDK · {activity_labels.get(activity, '状态待核对')}")
    result = {
        **base,
        'chain': chain,
        'engine': 'okx-dex-sdk',
        'state': state,
        'updated_at': updated.isoformat() if updated else None,
        'activity_status': activity,
        'activity_reason': reason,
        'state_label': state_label,
        'activity_message': reason_messages.get(reason, activity_messages.get(activity, '执行状态需要核对。')),
        'open_positions': None,
        'pending_orders': None,
        'ledger_identity_status': 'unverified',
        'issue_count': 0 if state == 'running' else 1,
        'cash_usd_estimate': None,
        'realized_pnl_usd_estimate': None,
        'native_symbol': 'BNB' if chain == 'bsc' else 'ETH',
        'realized_pnl_status': 'pending_verification',
        'realized_pnl_pending_count': None,
        'unverified_sell_fills': None,
        'unreconciled_external_sales': None,
        'realized_pnl_native_atomic': None,
        'realized_pnl_native': None,
        'realized_pnl_method': None,
        'manual_reconciled_sales': 0,
        'orders': [],
        'positions': [],
    }
    candidate_rejections = []
    for item in raw.get('candidate_rejections') or []:
        if not isinstance(item, dict):
            continue
        symbol = re.sub(r'[\r\n\t]+', ' ', str(item.get('symbol') or 'MEME')).strip()[:32]
        reject_reason = re.sub(r'[\r\n\t]+', ' ', str(item.get('reject_reason') or '')).strip()[:240]
        if reject_reason:
            candidate_rejections.append({
                'symbol': symbol,
                'reject_reason': reject_reason,
                'score': numeric(item.get('score')),
            })
        if len(candidate_rejections) >= 5:
            break
    result['candidate_rejections'] = candidate_rejections
    if activity == 'waiting_for_strategy_candidate' and candidate_rejections:
        top = candidate_rejections[0]
        result['activity_message'] = f"尚未成交；最近拦截：{top['symbol']}：{top['reject_reason']}。"
    holding_positions = raw.get('holding_positions') or []
    if activity == 'holding_existing_position' and holding_positions:
        holding = holding_positions[0] if isinstance(holding_positions[0], dict) else {}
        symbol = re.sub(r'[\r\n\t]+', ' ', str(holding.get('symbol') or 'MEME')).strip()[:32]
        current_return = numeric(holding.get('return_pct'))
        if current_return is not None:
            prefix = '+' if current_return >= 0 else ''
            if holding.get('tp3_hit') is True:
                rule = '尚未触发高点回撤 40% 或 24 小时 Runner 退出'
            elif holding.get('tp2_hit') is True:
                rule = '尚未触发 5x 再卖出 10%、高点回撤 40% 或 24 小时 Runner 退出'
            elif holding.get('tp1_hit') is True:
                rule = '尚未触发 3x 卖出 10%、5x 卖出 10%、高点回撤 40% 或 24 小时 Runner 退出'
            else:
                rule = '尚未触发 -22% 止损、2x 卖出 50% 或首盈前 90 分钟退出'
            result['activity_message'] = f'持仓监控中：{symbol} 当前 {prefix}{current_return:.1f}%，{rule}。'
    state_data = read(outputs / f'{stem}-state.json')
    positions = state_data.get('positions')
    closed = state_data.get('closed')
    if (state_data.get('version') != 1 or not isinstance(positions, dict)
            or not isinstance(closed, list) or not isinstance(state_data.get('fills', []), list)):
        result.update(state='degraded', issue_count=1)
        return result

    # The heartbeat describes the current worker, not the owner of historical fills.
    ledger_wallet = state_data.get('wallet')
    ledger_chain = state_data.get('chain')
    if (not isinstance(ledger_wallet, str) or not ADDRESS.fullmatch(ledger_wallet)
            or ledger_wallet.lower() == '0x' + '0' * 40 or not ledger_chain):
        identity = 'unverified'
    else:
        identity = ('verified' if ledger_wallet.lower() == base.get('wallet')
                    and ledger_chain == chain else 'mismatch')
    result['ledger_identity_status'] = identity
    if identity != 'verified':
        result.update(state='degraded', issue_count=1,
                      activity_reason='ledger_identity_' + identity,
                      state_label='Ledger identity ' + identity,
                      activity_message='Ledger wallet and chain require verification before history can be shown.')
        return result
    result.update(open_positions=0, pending_orders=0)

    reconciliation = read(outputs / f'{stem}-realized-reconciliation.json')
    settlement = sell_settlement_status(
        state_data, reconciliation, chain, str(base.get('wallet') or ''))
    result.update(settlement)
    realized_atomic = settlement['realized_pnl_native_atomic']
    if realized_atomic is not None:
        result['realized_pnl_native'] = int(realized_atomic) / 10 ** 18

    reconciled_worker_records = {}
    if settlement.get('realized_pnl_method') == 'receipt_fifo_native_equivalent_v1':
        for record in reconciliation.get('records') or []:
            if isinstance(record, dict) and record.get('source') == 'worker':
                tx_hash = str(record.get('tx_hash') or '').lower()
                if TX.fullmatch(tx_hash):
                    reconciled_worker_records[tx_hash] = record

    seen_transactions = set()

    def clean_position(item):
        if not isinstance(item, dict):
            return None
        token = str(item.get('token') or item.get('contract_address') or '').lower()
        if not ADDRESS.fullmatch(token):
            return None
        clean = {
            'token': token,
            'symbol': str(item.get('symbol', ''))[:40],
            'chain': chain,
            'entry_price_usd': numeric(item.get('entry_price_usd')),
            'current_price_usd': numeric(item.get('last_price_usd')),
            'return_pct': numeric(item.get('last_return_pct')),
            'exit_status': (str(item.get('exit_status'))
                            if re.fullmatch(r'[a-z][a-z0-9_]{0,60}', str(item.get('exit_status') or ''))
                            else None),
            'remaining_cost_usd': None,
            'realized_pnl_native_atomic': (str(item.get('realized_pnl_native_atomic'))
                                           if re.fullmatch(r'-?\d{1,90}', str(item.get('realized_pnl_native_atomic') or ''))
                                           else None),
        }
        amount = str(item.get('remaining_atomic', ''))
        clean['remaining_atomic'] = amount if re.fullmatch(r'\d{1,90}', amount) else None
        decimals = item.get('decimals')
        clean['decimals'] = decimals if type(decimals) is int and 0 <= decimals <= 36 else None
        return clean

    def add_order(item, side, tx_key, time_key):
        if not isinstance(item, dict):
            return
        token = str(item.get('token') or item.get('contract_address') or '').lower()
        tx_hash = str(item.get(tx_key) or '')
        if not ADDRESS.fullmatch(token) or not TX.fullmatch(tx_hash) or tx_hash in seen_transactions:
            return
        if side == 'sell' and tx_hash.lower() in reconciled_worker_records:
            record = reconciled_worker_records[tx_hash.lower()]
            item = dict(item)
            item.update({key: record[key] for key in (
                'amount_atomic', 'cost_basis_native_atomic', 'gas_native_atomic',
                'pnl_native_atomic',
            ) if key in record})
            gross = str(record.get('gross_native_equivalent_atomic') or '')
            gas = str(record.get('gas_native_atomic') or '')
            if gross.isdigit():
                item['native_received_atomic'] = gross
                if gas.isdigit():
                    item['net_native_received_atomic'] = str(int(gross) - int(gas))
        seen_transactions.add(tx_hash)
        time = timestamp(item.get(time_key))
        order = {
            'token': token,
            'symbol': str(item.get('symbol', ''))[:40],
            'chain': chain,
            'side': side,
            'status': 'filled',
            'tx_hash': tx_hash,
            'explorer_url': explorer + tx_hash,
            'time': time.isoformat() if time else None,
            'realized_pnl_usd_estimate': None,
        }
        exit_reason = str(item.get('exit_reason') or '')
        if re.fullmatch(r'[a-z][a-z0-9_]{0,60}', exit_reason):
            order['exit_reason'] = exit_reason
        fraction = numeric(item.get('fraction'))
        if fraction is not None and 0 < fraction <= 1:
            order['fraction'] = fraction
        amount = str(item.get('amount_atomic') or '')
        if re.fullmatch(r'\d{1,90}', amount):
            order['amount_atomic'] = amount
        return_pct = numeric(item.get('return_pct'))
        if return_pct is not None:
            order['return_pct'] = return_pct
        pnl_atomic = str(item.get('pnl_native_atomic') or '')
        if re.fullmatch(r'-?\d{1,90}', pnl_atomic):
            order['pnl_native_atomic'] = pnl_atomic
            order['pnl_native'] = int(pnl_atomic) / 10 ** 18
            order['native_symbol'] = 'BNB' if chain == 'bsc' else 'ETH'
        for field in ('net_native_received_atomic', 'native_received_atomic',
                      'gas_native_atomic', 'cost_basis_native_atomic'):
            value = str(item.get(field) or '')
            if re.fullmatch(r'-?\d{1,90}', value):
                order[field] = value
        result['orders'].append(order)

    for item in positions.values():
        clean = clean_position(item)
        if clean:
            result['positions'].append(clean)
            add_order(item, 'buy', 'buy_tx', 'entry_at')
    # Fills contain settlement accounting; consume them before summary records
    # so transaction de-duplication keeps the richer row.
    for item in (state_data.get('fills') or [])[-200:]:
        side = item.get('side') if isinstance(item, dict) else None
        if side in {'buy', 'sell'}:
            add_order(item, side, 'tx_hash', 'time')
    for item in closed[-100:]:
        add_order(item, 'buy', 'buy_tx', 'entry_at')
        add_order(item, 'sell', 'sell_tx', 'closed_at')
    result['orders'].sort(key=lambda row: row.get('time') or '')
    result['open_positions'] = len(result['positions'])
    return result


def project_status(root: Path = ROOT, now: datetime | None = None,
                   sdk_outputs: Path | None = None, *, reader=None) -> dict:
    read = reader or read_object
    now = now or datetime.now(timezone.utc)
    result = {'state': 'not_configured', 'configured': False, 'wallet': None,
              'engine': 'legacy-okx', 'activity_status': None, 'activity_reason': None,
              'state_label': None, 'activity_message': None,
              'provider': 'okx', 'updated_at': None, 'entries_paused': False,
              'open_positions': None, 'pending_orders': None, 'issue_count': 0,
              'cash_usd_estimate': None, 'realized_pnl_usd_estimate': None,
              'orders': [], 'positions': [], 'policy': asdict(ExecutionConfig.paper())}
    config = read(root / 'okx-live-config.json')
    wallet = str(config.get('wallet_address', '')).lower()
    if config.get('schema_version') != 1 or not ADDRESS.fullmatch(wallet) or wallet == '0x' + '0' * 40:
        return result
    result.update(configured=True, wallet=wallet, state='not_started')
    sdk_outputs = sdk_outputs or (SDK_OUTPUTS if root == ROOT else root / 'outputs')
    sdk_results = [item for item in (
        sdk_project_status(result, sdk_outputs, now, 'bsc', reader=read),
        sdk_project_status(result, sdk_outputs, now, 'robinhood', reader=read),
    ) if item is not None]
    if sdk_results:
        primary = next((item for item in sdk_results if item['chain'] == 'bsc'), sdk_results[0])
        primary['chains'] = [{
            key: item.get(key) for key in (
                'chain', 'state', 'updated_at', 'activity_status', 'activity_reason',
                'state_label', 'activity_message', 'open_positions', 'pending_orders',
                'native_symbol', 'realized_pnl_native_atomic', 'realized_pnl_native',
                'realized_pnl_status', 'realized_pnl_pending_count',
                'unverified_sell_fills', 'unreconciled_external_sales',
                'realized_pnl_method', 'manual_reconciled_sales',
                'candidate_rejections', 'ledger_identity_status',
            )
        } for item in sdk_results]
        primary['positions'] = [position for item in sdk_results for position in item['positions']]
        primary['orders'] = sorted(
            [order for item in sdk_results for order in item['orders']],
            key=lambda row: row.get('time') or '',
        )
        for key in ('open_positions', 'pending_orders'):
            values = [item[key] for item in sdk_results]
            primary[key] = sum(values) if all(value is not None for value in values) else None
        primary['issue_count'] = sum(item['issue_count'] for item in sdk_results)
        return primary
    account = root / 'gmgn-live' / wallet
    result['entries_paused'] = (account / 'PAUSE_ENTRIES').exists()
    path = account / 'status.json'
    if not path.exists():
        return result
    raw = read(path)
    if str(raw.get('wallet', '')).lower() != wallet or raw.get('provider') != 'okx':
        return {**result, 'state': 'identity_mismatch'}
    updated = timestamp(raw.get('updated_at'))
    age = (now - updated).total_seconds() if updated else None
    fresh = age is not None and 0 <= age <= 90
    state = str(raw.get('status'))
    if not fresh:
        state = 'stale'
    elif raw.get('enabled') is not True:
        state = 'disabled'
    elif state not in {'running', 'attention', 'degraded'}:
        state = 'attention'
    result.update(state=state, updated_at=updated.isoformat() if updated else None)
    for key in ('cash_usd_estimate', 'realized_pnl_usd_estimate', 'open_positions', 'pending_orders'):
        result[key] = numeric(raw.get(key))
    result['issue_count'] = len(raw.get('issues', [])) if isinstance(raw.get('issues'), list) else 0
    # Explicit scalar allowlists: never export raw provider responses, snapshots or credentials.
    for kind in ('orders', 'positions'):
        items = raw.get(kind)
        if not isinstance(items, list):
            continue
        for item in items[-100:]:
            if not isinstance(item, dict) or not ADDRESS.fullmatch(str(item.get('token', ''))):
                continue
            clean = {'token': item['token'], 'symbol': str(item.get('symbol', ''))[:40]}
            if kind == 'orders':
                clean['side'] = item.get('side') if item.get('side') in {'buy', 'sell'} else 'unknown'
                clean['status'] = item.get('status') if item.get('status') in {
                    'submitting', 'pending', 'unknown', 'reconcile_required', 'rejected', 'failed', 'filled'} else 'unknown'
                tx = str(item.get('tx_hash') or item.get('order_id') or '')
                clean['tx_hash'] = tx if TX.fullmatch(tx) else None
                time = timestamp(item.get('filled_at') or item.get('created_at'))
                clean['time'] = time.isoformat() if time else None
                clean['realized_pnl_usd_estimate'] = numeric(item.get('realized_pnl_usd_estimate'))
            else:
                for field in ('entry_price_usd', 'remaining_cost_usd'):
                    clean[field] = numeric(item.get(field))
                amount = str(item.get('remaining_atomic', ''))
                clean['remaining_atomic'] = amount if re.fullmatch(r'\d{1,90}', amount) else None
                decimals = item.get('decimals')
                clean['decimals'] = decimals if type(decimals) is int and 0 <= decimals <= 36 else None
            result[kind].append(clean)
    return result


def allowed_request(host: str, origin: str | None, client: str, port: int) -> bool:
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    return client == '127.0.0.1' and host in hosts and (origin is None or origin in {f'http://{h}' for h in hosts})


class Handler(BaseHTTPRequestHandler):
    def json_response(self, code, value):
        self.respond(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(),
                     'application/json; charset=utf-8')

    def respond(self, code, body, content_type):
        self.send_response(code)
        for key, value in {'Content-Type': content_type, 'Cache-Control': 'no-store',
                           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                           'Referrer-Policy': 'no-referrer', 'Cross-Origin-Resource-Policy': 'same-origin',
                           'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                           'Content-Length': str(len(body))}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/roundtrip/start':
            self.send_error(404)
            return
        control = self.server.control
        csrf = self.headers.get('X-CSRF-Token', '')
        if (any(len(self.headers.get_all(key, [])) != 1 for key in ('Host', 'Origin', 'X-CSRF-Token'))
                or not valid_post(self.headers.get('Host'), self.headers.get('Origin'),
                                  self.client_address[0], self.server.server_port,
                                  self.headers.get('Sec-Fetch-Site'))
                or not csrf.isascii() or not hmac.compare_digest(csrf, control.csrf_token)):
            self.json_response(403, {'error': 'request_forbidden'})
            return
        if (self.headers.get('Transfer-Encoding') is not None
                or len(self.headers.get_all('Content-Length', [])) != 1
                or len(self.headers.get_all('Content-Type', [])) != 1
                or not re.fullmatch(r'[0-9]{1,6}', self.headers.get('Content-Length', ''))):
            self.json_response(400, {'error': 'request_invalid'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self.json_response(415, {'error': 'json_required'})
            return
        length = int(self.headers['Content-Length'])
        if not 0 < length <= 2048:
            self.json_response(413, {'error': 'request_too_large'})
            return

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate_key')
                result[key] = value
            return result

        def invalid_constant(_):
            raise ValueError('invalid_constant')

        try:
            self.connection.settimeout(3)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete_body')
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=object_pairs,
                                 parse_constant=invalid_constant)
            if not isinstance(payload, dict):
                raise ValueError('invalid_object')
        except (OSError, ValueError, RecursionError):
            self.json_response(400, {'error': 'request_invalid'})
            return
        code, value = control.start(payload=payload, host=self.headers['Host'], origin=self.headers['Origin'],
                                    client=self.client_address[0], port=self.server.server_port,
                                    csrf=csrf, fetch_site=self.headers.get('Sec-Fetch-Site'))
        self.json_response(code, value)

    def do_GET(self):
        if (not allowed_request(self.headers.get('Host', ''), self.headers.get('Origin'),
                                self.client_address[0], self.server.server_port)
                or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            self.send_error(403)
            return
        if self.path == '/status.json':
            body = json.dumps(project_status(self.server.control.root), ensure_ascii=False, allow_nan=False).encode()
            content_type = 'application/json; charset=utf-8'
        elif self.path == '/roundtrip/status.json':
            self.json_response(200, {'roundtrip': self.server.control.status(),
                                     'csrf_token': self.server.control.csrf_token})
            return
        elif self.path == '/':
            body = Path(__file__).with_name('alpha_live_dashboard.html').read_bytes()
            content_type = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.respond(200, body, content_type)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8771)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.control = RoundtripControl(ROOT)
    print(f'Private OKX dashboard: http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
