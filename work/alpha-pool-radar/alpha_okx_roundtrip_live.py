"""Operator-started $1 stablecoin or strategy-MEME round trip; no restart/resubmit."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from alpha_gmgn_live_worker import ROOT, account_lock
from alpha_okx_live_transport import OkxLiveTransport, OkxTransportError, NATIVE, USDT
from alpha_okx_strategy_probe import ProbeError, select_candidate

HASH = re.compile(r'0x[0-9a-fA-F]{64}')
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


class RoundtripError(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise RoundtripError(reason)


def atomic(value):
    require(not isinstance(value, bool) and re.fullmatch(r'[0-9]{1,78}', str(value)), 'invalid_amount')
    return int(value)


def number(value):
    try:
        value = Decimal(str(value))
        require(value.is_finite() and value >= 0, 'invalid_amount')
        return value
    except Exception:
        raise RoundtripError('invalid_amount') from None


def atomic_write(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05)


def eth_account_diagnostic():
    try:
        spec = importlib.util.find_spec('eth_account')
    except Exception:
        return {'spec_found': False, 'origin': None, 'import_ok': False}
    origin = getattr(spec, 'origin', None) if spec is not None else None
    data = {'spec_found': spec is not None,
            'origin': str(origin)[:240] if isinstance(origin, str) else None,
            'import_ok': False}
    if spec is not None:
        try:
            import eth_account  # type: ignore
            data['import_ok'] = hasattr(eth_account, 'Account')
        except Exception as error:
            data['import_error'] = type(error).__name__[:80]
    return data


class Roundtrip:
    def __init__(self, transport, directory, *, preview, sleep=time.sleep, wait_polls=60,
                 strategy_input=None, clock=None, account_directory=None):
        self.t, self.directory, self.preview = transport, Path(directory), preview
        self.account_directory = Path(account_directory) if account_directory is not None else self.directory
        self.sleep, self.wait_polls = sleep, wait_polls
        self.strategy_input = Path(strategy_input) if strategy_input is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.token, self.candidate, self.account = USDT, None, {}
        self.gas_seen = set()
        self.report = {'wallet': transport.wallet_address, 'provider': 'okx', 'run_id': str(uuid.uuid4()),
                       'state': 'checking', 'reason': '', 'buy_tx': None, 'sell_tx': None,
                       'approval_txs': [], 'amount_native': None, 'bought_atomic': '0',
                       'remaining_atomic': '0', 'gas_native': '0', 'pnl_native': None,
                       'notional_usd': '1', 'max_gas_usd': '1', 'slippage_percent': '1',
                       'token': None if strategy_input is not None else USDT,
                       'symbol': 'MEME' if strategy_input is not None else 'USDT',
                       'test_mode': 'strategy_meme' if strategy_input is not None else 'stablecoin',
                       'execution_arm': None, 'strategy_started': False, 'check_stage': None}

    def save(self, state=None, **fields):
        self.report.update(fields)
        if state:
            self.report['state'] = state
        self.report['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_write(self.directory / 'roundtrip-status.json', self.report)

    def idle(self):
        require(not (self.account_directory / 'PAUSE_ENTRIES').exists(), 'entries_paused')
        path = self.account_directory / 'ledger.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT data FROM ledger WHERE id=1').fetchone()
            require(row is not None, 'strategy_account_not_idle')
            state = json.loads(row[0])
            require(state.get('wallet') == self.t.wallet_address and not state.get('positions')
                    and isinstance(state.get('orders'), dict)
                    and all(o.get('status') in {'filled', 'failed', 'rejected'} and not o.get('fill_anomaly')
                            for o in state['orders'].values()),
                    'strategy_account_not_idle')
            self.account = state
        require(self.t.ready_for_entries(), 'unresolved_account_transactions')

    def strategy_candidate(self):
        try:
            payload = json.loads(self.strategy_input.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise ProbeError('strategy_input_unavailable') from None
        return select_candidate(payload, self.clock(), account=self.account,
                                token=self.candidate['contract_address'] if self.candidate else None,
                                pool=self.candidate['pool_address'] if self.candidate else None)

    def admit_candidate(self):
        try:
            self.strategy_candidate()
        except ProbeError:
            raise RoundtripError('strategy_candidate_expired') from None
        require(not (self.account_directory / 'PAUSE_ENTRIES').exists(), 'entries_paused')

    def wait(self, txhash, *, approval=False):
        require(HASH.fullmatch(str(txhash)), 'invalid_transaction_hash')
        for _ in range(self.wait_polls):
            receipt = self.t.order(txhash)
            if receipt.get('finalized') is True and receipt.get('gas_native') is not None:
                self.add_gas(txhash, receipt)
            if receipt.get('status') in {'failed', 'rejected'}:
                raise RoundtripError('transaction_failed')
            if receipt.get('finalized') is True and (
                    receipt.get('approval_confirmed') is True if approval else
                    receipt.get('status') == 'confirmed' and receipt.get('fill_complete') is True):
                require(receipt.get('gas_native') is not None, 'receipt_gas_unavailable')
                return receipt
            self.save()
            self.sleep(3)
        raise RoundtripError('confirmation_timeout')

    def add_gas(self, txhash, receipt):
        if txhash in self.gas_seen:
            return
        self.save(gas_native=format(number(self.report['gas_native']) + number(receipt['gas_native']), 'f'))
        self.gas_seen.add(txhash)

    def fill(self, receipt, side, amount):
        buy = side == 'buy'
        require(receipt.get('wallet_address') == self.t.wallet_address and receipt.get('chain') == 'bsc'
                and receipt.get('tx_hash') == self.report[side + '_tx']
                and receipt.get('native_amount_proven') is True and not receipt.get('fill_anomalies')
                and receipt.get('input_token') == (NATIVE if buy else self.token)
                and receipt.get('output_token') == (self.token if buy else NATIVE)
                and atomic(receipt.get('input_amount')) == amount
                and atomic(receipt.get('output_amount')) > 0, 'fill_identity_or_amount_mismatch')
        return atomic(receipt['output_amount'])

    def submit(self, side, amount):
        require(side in {'buy', 'sell'} and not self.report[side + '_tx'], 'duplicate_submission')
        self.save(side + '_submitting')
        def prepared(txhash):
            require(HASH.fullmatch(str(txhash)), 'invalid_transaction_hash')
            self.save(side + '_pending', **{side + '_tx': txhash})
            if side == 'buy' and self.candidate:
                self.admit_candidate()
        result = self.t.submit(self.token, side, str(amount), '1', on_prepared=prepared)
        require(result.get('tx_hash') == self.report[side + '_tx'], 'prepared_hash_response_mismatch')
        return self.wait(result['tx_hash'])

    def execute(self):
        self.save(check_stage='preflight', eth_account=eth_account_diagnostic())
        require(self.t.preflight().get('enabled') is True, 'explicit_live_environment_required')
        self.save(check_stage='idle')
        self.idle()
        self.save(check_stage='verify_connection')
        require(self.t.verify_connection().get('ready') is True, 'provider_verification_incomplete')
        if self.strategy_input is not None:
            self.save(check_stage='strategy_candidate')
            self.candidate = self.strategy_candidate()
            self.token = self.candidate['contract_address']
            require(self.token not in {NATIVE, USDT, '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'}, 'test_token_mismatch')
            self.save(token=self.token, symbol=self.candidate['symbol'],
                      execution_arm=self.candidate['execution_arm'])
            self.save(check_stage='security')
            require(self.t.security(self.token).get('safe') is True, 'strategy_security_failed')
            if hasattr(self.t, 'set_test_candidate'):
                self.t.set_test_candidate(self.token, self.admit_candidate)
        self.save(check_stage='simulate')
        self.save('simulating')
        plan = self.preview()
        if self.candidate:
            require(plan['buy_pool'] == self.candidate['pool_address']
                    and plan['sell_pool'] == self.candidate['pool_address'], 'strategy_pool_mismatch')
            self.admit_candidate()
        amount = atomic(plan['native_input_atomic'])
        price = number(self.t.market()['native_price_usd'])
        require(price > 0 and 0 < Decimal(amount) * price / 10**18 <= 1, 'test_buy_exceeds_1_usd')
        if hasattr(self.t, 'set_test_budget'):
            self.t.set_test_budget(price, amount)
        balance = atomic(self.t.balance(NATIVE)['amount_atomic'])
        require(Decimal(balance - amount) * price / 10**18 >= 1, 'insufficient_gas_reserve')
        baseline = atomic(self.t.balance(self.token)['amount_atomic'])
        self.save(amount_native=format(Decimal(amount) / 10**18, 'f'), original_token_atomic=str(baseline),
                  **({'original_usdt_atomic': str(baseline)} if self.token == USDT else {}))
        self.t.set_context(self.token, plan['buy_pool'])
        if self.candidate:
            self.admit_candidate()
        bought = self.fill(self.submit('buy', amount), 'buy', amount)
        self.save('approving', bought_atomic=str(bought), remaining_atomic=str(bought))
        if hasattr(self.t, 'set_sell_budget'):
            self.t.set_sell_budget(bought)
        require(atomic(self.t.balance(self.token)['amount_atomic']) >= baseline + bought,
                'wallet_balance_changed')
        # At most a zero-reset and one exact approval; never re-submit an unknown tx.
        ready = False
        for _ in range(3):
            approval = self.t.ensure_allowance(self.token, str(bought), allow_submit=len(self.report['approval_txs']) < 2)
            if approval.get('ready') is True:
                ready = True
                break
            h = approval.get('approval_tx_hash')
            known = h in self.report['approval_txs']
            if HASH.fullmatch(str(h)) and not known:
                self.save(approval_txs=[*self.report['approval_txs'], h])
            require(approval.get('status') == 'pending' and HASH.fullmatch(str(h))
                    and not known and len(self.report['approval_txs']) <= 2,
                    'approval_requires_reconciliation')
            self.wait(h, approval=True)
        require(ready, 'allowance_not_ready')
        require(atomic(self.t.balance(self.token)['amount_atomic']) >= baseline + bought,
                'wallet_balance_changed')
        self.t.set_context(self.token, plan['sell_pool'])
        received = self.fill(self.submit('sell', bought), 'sell', bought)
        self.save('reconciling', remaining_atomic='0')
        require(atomic(self.t.balance(self.token)['amount_atomic']) >= baseline,
                'original_holdings_changed')
        pnl = Decimal(received - amount) / 10**18 - number(self.report['gas_native'])
        self.save('completed', pnl_native=format(pnl, 'f'), native_received=format(Decimal(received) / 10**18, 'f'))

    def run(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.account_directory.mkdir(parents=True, exist_ok=True)
        with account_lock(self.account_directory / 'worker.lock'), localcontext() as ctx:
            ctx.prec = 120
            require(not (self.directory / 'roundtrip-status.json').exists(), 'test_already_attempted')
            try:
                with (self.directory / 'roundtrip-attempt.json').open('x', encoding='utf-8') as claim:
                    json.dump({'wallet': self.t.wallet_address, 'run_id': self.report['run_id'],
                               'test_mode': self.report['test_mode']}, claim)
                    claim.flush()
                    os.fsync(claim.fileno())
            except FileExistsError:
                raise RoundtripError('test_already_attempted') from None
            self.save()
            try:
                self.execute()
            except Exception as error:
                # Error handling must work even if ABI dependencies failed to import.
                known = isinstance(error, (RoundtripError, OkxTransportError, ProbeError)) or (
                    type(error).__module__ == 'alpha_okx_dag_preflight' and type(error).__name__ == 'PreflightError')
                code = str(error) if known else 'dependency_missing' if isinstance(error, ImportError) else 'test_check_failed'
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code):
                    code = 'test_check_failed'
                sent = bool(self.report['buy_tx'] or self.report['approval_txs'])
                self.save('attention' if sent else 'blocked', reason=code)
            return dict(self.report)


class BoundedTransport(OkxLiveTransport):
    """Enforce test limits again at the last boundary before signing."""
    def set_test_candidate(self, token, admission):
        require(not hasattr(self, 'test_token') and not hasattr(self, 'test_amount'), 'test_token_mismatch')
        require(isinstance(token, str) and re.fullmatch(r'0x[0-9a-f]{40}', token)
                and int(token[2:], 16) and token not in {NATIVE, USDT}, 'test_token_mismatch')
        self.test_token, self.test_admission = token, admission

    def set_test_budget(self, price, amount):
        self.test_price, self.test_amount = price, amount
        self.reserved_gas = 0
        self.test_swaps = set()
        self.test_approvals = 0

    def set_sell_budget(self, amount):
        self.test_bought = amount

    def _broadcast(self, db, tx, intent, kind, on_prepared=None):
        require(hasattr(self, 'test_amount'), 'test_budget_not_set')
        require(intent['token'] == getattr(self, 'test_token', USDT), 'test_token_mismatch')
        price = max(self.test_price, number(intent['native_price_usd']))
        self.test_price = price
        gas = tx['gas'] * tx['gasPrice']
        require(Decimal(self.reserved_gas + gas) * price / 10**18 <= 1, 'test_gas_budget_exceeded')
        require(not db.execute("SELECT 1 FROM transactions WHERE nonce=? AND status='cancelled'",
                               (tx['nonce'],)).fetchone(), 'test_cancelled_nonce_requires_review')
        if kind == 'swap':
            side = intent['side']
            require(side in {'buy', 'sell'} and side not in self.test_swaps, 'duplicate_submission')
            if side == 'buy':
                if hasattr(self, 'test_admission'):
                    self.test_admission()
                require(int(intent['amount']) == self.test_amount and
                        Decimal(self.test_amount) * price / 10**18 <= 1, 'test_buy_exceeds_1_usd')
            else:
                require(hasattr(self, 'test_bought') and int(intent['amount']) == self.test_bought,
                        'test_sell_amount_mismatch')
            self.test_swaps.add(side)
        elif kind == 'approval':
            require(hasattr(self, 'test_bought') and int(intent['amount']) in {0, self.test_bought}
                    and self.test_approvals < 2, 'test_approval_limit_exceeded')
            self.test_approvals += 1
        else:
            raise RoundtripError('test_transaction_kind_invalid')
        self.reserved_gas += gas
        return super()._broadcast(db, tx, intent, kind, on_prepared)


def live_preview(transport, token=USDT):
    from alpha_okx_roundtrip_preflight import run
    from alpha_okx_dag_execution import validate_trim
    result = run({'wallet_address': transport.wallet_address,
                  'bsc_rpc_url': transport._env['BSC_RPC_URL']}, notional_usd='1', token=token)
    require(result['status'] in {'simulated_round_trip', 'historical_simulated_round_trip'}, 'roundtrip_simulation_failed')
    for inspection in result['inspections']:
        validate_trim(inspection, transport._env.get('OKX_DAG_TRIM_RECIPIENT'),
                      transport._env.get('OKX_DAG_MAX_TRIM_PER_MILLE', '0'))
    buy, sell = result['inspections']
    return {'native_input_atomic': result['native_input_atomic'],
            'buy_pool': buy['hops'][-1]['pool'], 'sell_pool': sell['hops'][0]['pool']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--signer-check', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--strategy-meme', action='store_true')
    parser.add_argument('--session-id')
    args = parser.parse_args(argv)
    if args.signer_check:
        transport = BoundedTransport()
        try:
            transport.preflight()
        except Exception as error:
            code = getattr(error, 'code', str(error))
            reason = code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code) else 'signer_check_failed'
            print(json.dumps({'configured': False, 'live_started': False, 'wallet': transport.wallet_address,
                              'provider': 'okx', 'signer_checked': False, 'reason': reason}), flush=True)
            return 1
        print(json.dumps({'configured': True, 'live_started': False, 'wallet': transport.wallet_address,
                          'provider': 'okx', 'signer_checked': True}), flush=True)
        return 0
    if not args.live:
        parser.error('--live is required unless --signer-check is used')
    if os.environ.get('OKX_LIVE_ENABLED') != '1' or os.environ.get('OKX_ALLOW_AUTOMATED_TRADES') != '1':
        print('{"state":"blocked","reason":"explicit_live_environment_required"}')
        return 1
    transport = BoundedTransport()
    directory = Path(transport.state_dir)
    account_directory = directory
    if args.session_id:
        if not SESSION_ID.fullmatch(args.session_id):
            print(json.dumps({'state': 'blocked', 'reason': 'invalid_session_id'}), flush=True)
            return 1
        directory = account_directory / 'roundtrip-sessions' / args.session_id
    runner = Roundtrip(transport, directory, preview=lambda: live_preview(transport, runner.token),
                       strategy_input=ROOT / 'outputs/bsc-execution-input.json' if args.strategy_meme else None,
                       account_directory=account_directory)
    try:
        if args.preflight_only:
            runner.directory.mkdir(parents=True, exist_ok=True)
            runner.account_directory.mkdir(parents=True, exist_ok=True)
            with account_lock(runner.account_directory / 'worker.lock'):
                runner.save(check_stage='preflight', eth_account=eth_account_diagnostic())
                transport.preflight()
                runner.save('preflight_ok')
                result = dict(runner.report)
        else:
            result = runner.run()
    except Exception as error:
        # Class name only: never reflect exception messages containing provider data.
        reason = 'test_start_failed'
        if isinstance(error, (RoundtripError, OkxTransportError, ProbeError)):
            code = str(error)
            if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code):
                reason = code
        print(json.dumps({'state': 'blocked', 'reason': reason,
                          'error_type': type(error).__name__, 'last_step': runner.report['state']}), flush=True)
        return 1
    print(json.dumps(result))
    return 0 if result['state'] in {'completed', 'preflight_ok'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
