"""User-confirmed, persistent one-shot launcher. Never imports a trading client."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

POWERSHELL = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
CREATE_NO_WINDOW = 0x08000000
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
TX = re.compile(r'0x[0-9a-fA-F]{64}')
TEST_MODES = ('stablecoin', 'strategy_meme')
STATES = frozenset({'starting', 'checking', 'simulating', 'buy_submitting', 'sell_submitting',
                    'preflight', 'buying', 'buy_pending', 'bought',
                    'approving', 'approval_pending', 'selling', 'sell_pending',
                    'reconciling', 'completed', 'failed', 'blocked', 'attention', 'stopped'})
REASONS = frozenset({'completed', 'buy_failed', 'sell_failed', 'approval_failed',
                     'insufficient_native_balance', 'gas_exceeds_0_5_usd',
                     'dag_trim_consent_required', 'dag_trim_recipient_mismatch',
                     'dag_trim_cap_exceeded', 'submission_deadline_expired',
                     'deadline_invalid', 'quote_expired', 'security_not_passed',
                     'allowance_not_ready', 'approval_pending', 'wallet_nonce_pending',
                     'journal_unresolved_do_not_retry', 'broadcast_unknown_do_not_retry',
                     'roundtrip_already_exists', 'roundtrip_probe_failed', 'rpc_request_failed',
                     'entries_paused', 'strategy_account_not_idle', 'unresolved_account_transactions',
                     'transaction_failed', 'confirmation_timeout', 'fill_identity_or_amount_mismatch',
                     'explicit_live_environment_required', 'provider_verification_incomplete',
                     'test_buy_exceeds_1_usd', 'insufficient_gas_reserve', 'wallet_balance_changed',
                     'approval_requires_reconciliation', 'original_holdings_changed', 'test_already_attempted',
                     'test_check_failed', 'test_gas_budget_exceeded', 'test_cancelled_nonce_requires_review',
                     'roundtrip_simulation_failed', 'dag_calldata_expired', 'dag_deadline_too_short',
                     'no_strategy_candidate', 'strategy_input_unavailable', 'strategy_input_stale',
                     'strategy_candidate_expired', 'strategy_security_failed', 'test_token_mismatch',
                     'strategy_pool_mismatch'})


def read_object(path):
    try:
        if path.stat().st_size > 262144:
            return {}
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def wallet_config(root):
    config = read_object(root / 'okx-live-config.json')
    wallet = config.get('wallet_address')
    if (type(config.get('schema_version')) is int and config['schema_version'] == 1
            and isinstance(wallet, str) and ADDRESS.fullmatch(wallet)
            and int(wallet[2:], 16) != 0):
        return wallet.lower()
    return None


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def amount_text(value, *, atomic=False, signed=False):
    text = str(value)
    pattern = (r'-?' if signed else '') + r'[0-9]{1,78}' + ('' if atomic else r'(?:\.[0-9]{1,78})?(?:[eE][+-]?[0-9]{1,3})?')
    if not re.fullmatch(pattern, text):
        return None
    if atomic and int(text) >= 1 << 256:
        return None
    number = Decimal(text)
    return format(number, 'f') if -78 <= number.as_tuple().exponent <= 78 and number.adjusted() <= 78 else None


def historical_fee(root, wallet):
    raw = read_object(root / 'okx-roundtrip-preflight.json')
    if (raw.get('status') not in ('historical_simulated_round_trip', 'simulated_round_trip')
            or raw.get('wallet', wallet) != wallet or not stamp(raw.get('created_at'))):
        return None
    inspections = raw.get('inspections')
    if not isinstance(inspections, list) or len(inspections) != 2:
        return None
    profiles = []
    for inspection in inspections:
        trim = inspection.get('trim') if isinstance(inspection, dict) else None
        if not isinstance(trim, dict):
            return None
        recipient, rate = trim.get('recipient'), trim.get('rate_per_1000')
        if (not isinstance(recipient, str) or not ADDRESS.fullmatch(recipient) or int(recipient[2:], 16) == 0
                or type(rate) is not int or not 0 <= rate <= 100):
            return None
        profiles.append((recipient.lower(), rate))
    if profiles[0][0] != profiles[1][0]:
        return None
    return {'created_at': stamp(raw['created_at']), 'recipient': profiles[0][0],
            'max_trim_per_mille': max(rate for _, rate in profiles)}


def valid_post(host, origin, client, port, fetch_site):
    return (client == '127.0.0.1' and host in {f'127.0.0.1:{port}', f'localhost:{port}'}
            and origin == 'http://' + host and fetch_site in {None, 'same-origin'})


class RoundtripControl:
    def __init__(self, root: Path, runner: Path | None = None):
        self.root = root
        self.runner = runner or Path(__file__).resolve().parent
        self.csrf_token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._child = None
        self._failure = None
        self._wallet = wallet_config(root)

    def _available(self):
        try:
            launcher = (self.runner / 'start_okx_live.ps1').read_text(encoding='utf-8-sig')
            return (POWERSHELL.is_file() and (self.runner / 'alpha_okx_roundtrip_live.py').is_file()
                    and re.search(r'\[switch\]\s*\$RoundTrip\b', launcher, re.IGNORECASE) is not None)
        except (OSError, ValueError):
            return False

    def status(self):
        with self._lock:
            return self._status()

    def _status(self):
        wallet = wallet_config(self.root)
        running = self._child is not None and self._child.poll() is None
        result = {'configured': wallet is not None, 'wallet': wallet, 'running': running,
                  'process_state': 'unobserved' if self._child is None else 'running' if running else 'exited',
                  'report_fresh': None, 'historical_fee': historical_fee(self.root, wallet) if wallet else None,
                  'can_start': False, 'existing_report': False, 'attempted': False,
                  'state': 'not_configured', 'reason': 'not_configured',
                  'test_mode': 'stablecoin', 'token': None, 'symbol': None, 'execution_arm': None,
                  'run_id': None, 'updated_at': None, 'buy_tx': None, 'sell_tx': None,
                  'approval_txs': [], 'amount_native': None, 'bought_atomic': None,
                  'remaining_atomic': None, 'gas_native': None, 'pnl_native': None}
        if wallet is None:
            return result
        if wallet != self._wallet:
            return {**result, 'state': 'blocked', 'reason': 'configuration_changed'}
        account = self.root / 'gmgn-live' / wallet
        report_path = account / 'roundtrip-status.json'
        marker = account / 'roundtrip-ui-start.json'
        # lexists also blocks a broken symlink; neither GET nor status repairs files.
        existing = os.path.lexists(report_path)
        attempted = os.path.lexists(marker) or os.path.lexists(account / 'roundtrip-attempt.json')
        result.update(existing_report=existing, attempted=attempted)
        if attempted:
            claim = read_object(marker if os.path.lexists(marker) else account / 'roundtrip-attempt.json')
            mode = claim.get('test_mode', 'stablecoin')
            result['test_mode'] = mode if mode in TEST_MODES else None
        if existing:
            raw = read_object(report_path)
            if str(raw.get('wallet', '')).lower() != wallet:
                return {**result, 'state': 'attention', 'reason': 'report_unreadable_or_mismatched'}
            mode = raw.get('test_mode', 'stablecoin')
            if mode not in TEST_MODES:
                return {**result, 'test_mode': None, 'state': 'attention', 'reason': 'report_unreadable_or_mismatched'}
            result['test_mode'] = mode
            token, symbol, arm = (raw.get(key) for key in ('token', 'symbol', 'execution_arm'))
            result['token'] = token.lower() if isinstance(token, str) and ADDRESS.fullmatch(token) and int(token[2:], 16) else None
            result['symbol'] = symbol if isinstance(symbol, str) and re.fullmatch(r'[A-Za-z0-9\u3400-\u9fff_$][A-Za-z0-9\u3400-\u9fff ._$-]{0,39}', symbol) else None
            result['execution_arm'] = arm if arm in ('first_discovery', 'narrative_breakout') else None
            state = raw.get('state', raw.get('step'))
            result['state'] = state if isinstance(state, str) and state in STATES else 'attention'
            reason = raw.get('reason')
            result['reason'] = reason if isinstance(reason, str) and reason in REASONS else (
                'report_requires_review' if reason else None)
            result['updated_at'] = stamp(raw.get('updated_at'))
            result['report_fresh'] = bool(result['updated_at']) and 0 <= (
                datetime.now(timezone.utc) - datetime.fromisoformat(result['updated_at'])).total_seconds() <= 90
            run_id = raw.get('run_id')
            if isinstance(run_id, str) and re.fullmatch(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', run_id):
                result['run_id'] = run_id
            for key in ('buy_tx', 'sell_tx'):
                value = raw.get(key)
                result[key] = value if isinstance(value, str) and TX.fullmatch(value) else None
            approvals = raw.get('approval_txs')
            if isinstance(approvals, list):
                result['approval_txs'] = [v for v in approvals[:20] if isinstance(v, str) and TX.fullmatch(v)]
            for key in ('amount_native', 'bought_atomic', 'remaining_atomic', 'gas_native', 'pnl_native'):
                result[key] = amount_text(raw.get(key), atomic=key.endswith('_atomic'), signed=key == 'pnl_native')
            return result
        if running:
            return {**result, 'state': 'starting', 'reason': 'waiting_for_report'}
        if self._failure or self._child is not None:
            return {**result, 'state': 'failed', 'reason': self._failure or 'child_exit_before_report'}
        if attempted:
            return {**result, 'state': 'attention', 'reason': 'attempt_recorded_no_report'}
        worker = read_object(account / 'status.json')
        updated = stamp(worker.get('updated_at'))
        if (worker.get('enabled') is True and updated and
                0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds() <= 90):
            return {**result, 'state': 'blocked', 'reason': 'worker_running'}
        if not self._available():
            return {**result, 'state': 'blocked', 'reason': 'runner_unavailable'}
        return {**result, 'state': 'not_started', 'reason': 'awaiting_confirmation', 'can_start': True}

    def start(self, *, payload, host, origin, client, port, csrf, fetch_site=None):
        if (not valid_post(host, origin, client, port, fetch_site)
                or not isinstance(csrf, str) or not csrf.isascii()
                or not hmac.compare_digest(csrf, self.csrf_token)):
            return 403, {'error': 'request_forbidden'}
        if not isinstance(payload, dict) or payload.get('confirmed') is not True or type(payload.get('fee_enabled')) is not bool:
            return 400, {'error': 'confirmation_required'}
        fields = {'confirmed', 'fee_enabled'}
        mode = payload.get('test_mode', 'stablecoin')
        if mode not in TEST_MODES:
            return 400, {'error': 'test_mode_invalid'}
        if 'test_mode' in payload:
            fields.add('test_mode')
        if payload['fee_enabled']:
            fields |= {'trim_recipient', 'max_trim_per_mille'}
            recipient, cap = payload.get('trim_recipient'), payload.get('max_trim_per_mille')
            if (not isinstance(recipient, str) or not ADDRESS.fullmatch(recipient)
                    or int(recipient[2:], 16) == 0 or type(cap) is not int or not 0 <= cap <= 100):
                return 400, {'error': 'fee_policy_invalid'}
        else:
            recipient, cap = None, 0
        if set(payload) != fields:
            return 400, {'error': 'unexpected_fields'}
        with self._lock:
            status = self._status()
            if not status['can_start']:
                return 409, {'error': 'start_blocked', 'roundtrip': status}
            account = self.root / 'gmgn-live' / status['wallet']
            try:
                account.mkdir(parents=True, exist_ok=True)
                # Exclusive creation is the persistent cross-process one-shot claim.
                with (account / 'roundtrip-ui-start.json').open('x', encoding='utf-8') as handle:
                    json.dump({'wallet': status['wallet'], 'run_id': str(uuid.uuid4()), 'test_mode': mode,
                               'updated_at': datetime.now(timezone.utc).isoformat()}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                return 409, {'error': 'start_blocked', 'roundtrip': self._status()}
            except OSError:
                self._failure = 'launch_failed'
                return 503, {'error': 'launch_failed', 'roundtrip': self._status()}
            # A concurrent CLI run may have written its report after our first read.
            if (os.path.lexists(account / 'roundtrip-status.json') or os.path.lexists(account / 'roundtrip-attempt.json')
                    or wallet_config(self.root) != status['wallet']):
                return 409, {'error': 'start_blocked', 'roundtrip': self._status()}
            command = [str(POWERSHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                       '-File', str(self.runner / 'start_okx_live.ps1'), '-Live', '-RoundTrip', '-EnableDagRoutes',
                       '-ExpectedWallet', status['wallet']]
            if mode == 'strategy_meme':
                command.append('-StrategyMeme')
            if cap > 0:
                command.extend(['-DagTrimRecipient', recipient.lower(), '-DagMaxTrimPerMille', str(cap)])
            try:
                # The launcher redacts credentials before output; retain it privately.
                with (account / 'roundtrip-launch.log').open('ab') as log:
                    self._child = subprocess.Popen(command, shell=False, cwd=str(self.runner),
                                                   creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                                   stdout=log, stderr=subprocess.STDOUT)
            except (OSError, ValueError):
                self._failure = 'launch_failed'
                return 503, {'error': 'launch_failed', 'roundtrip': self._status()}
            return 202, {'roundtrip': self._status()}
