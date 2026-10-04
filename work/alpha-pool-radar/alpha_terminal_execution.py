"""Explicit operator controls; requests never imply an execution acknowledgement."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
import json
import subprocess
import uuid

from alpha_terminal_control import (TerminalControl, ControlError, POWERSHELL,
                                    _amount, _read_json, _option, _basename)

STEMS = {'bsc': 'okx-dex-sdk', 'robinhood': 'okx-dex-sdk-robinhood'}
ACKNOWLEDGEMENT_LOCK_SECONDS = 125
CHAIN_V2 = {
    'bsc': {'amount_usd': '1', 'signal_stage': 'aggregate_early_bird',
            'entry_route': 'bsc_aggregate_early_bird'},
    'robinhood': {'amount_usd': '2', 'signal_stage': 'aggregate_early_bird',
                  'entry_route': 'robinhood_aggregate_early_bird'},
}


def age(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return float('inf')


def launch_worker(repo, chain, wallet, amount, *, popen=subprocess.Popen):
    """Called only by an authenticated, explicit start request, never on import."""
    script = repo / 'work/alpha-pool-radar/start_okx_dex_sdk_live.ps1'
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(('OKX_', 'BSC_', 'EVM_', 'GMGN_'))
           and key.upper() not in {'NODE_OPTIONS', 'NODE_PATH'}}
    log_path = repo / 'outputs' / f'okx-dex-sdk-{chain}-launcher.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('ab', buffering=0) as log:
        process = popen([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                         '-File', str(script), '-Live', '-Daemon', '-Chain', chain,
                         '-ExpectedWallet', wallet, '-AmountUsd', amount],
                        cwd=str(repo), env=env, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT,
                        creationflags=0x08000000 if os.name == 'nt' else 0)
    return process.pid


class ExecutionControl(TerminalControl):
    def __init__(self, *args, launcher=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.launcher = launcher or launch_worker

    def _path(self, chain, kind):
        return self.repo / 'outputs' / f'{STEMS[chain]}-{kind}.json'

    def _read_optional(self, path):
        try:
            return _read_json(path)
        except (OSError, ValueError):
            return {}

    def _read_chain_file(self, chain, kind):
        raw = self._read_optional(self._path(chain, kind))
        claims = [raw.get('chain')] if isinstance(raw.get('chain'), str) else []
        if kind == 'live-status' and isinstance(raw.get('terminal_control'), dict):
            receipt_chain = raw['terminal_control'].get('chain')
            if isinstance(receipt_chain, str):
                claims.append(receipt_chain)
        return raw if claims and all(value == chain for value in claims) else {}

    @contextmanager
    def _chain_locked(self, chain):
        path = self.root / f'.terminal-control-{chain}.lock'
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ControlError('control_busy') from None
        except OSError:
            raise ControlError('storage_failed') from None
        os.close(descriptor)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _effective_strategy(chain, receipt):
        expected = CHAIN_V2[chain]
        if (receipt.get('strategy_version') != 'chain_v2'
                or receipt.get('signal_stage') != expected['signal_stage']
                or receipt.get('entry_route') != expected['entry_route']
                or _amount(receipt.get('amount_usd')) != expected['amount_usd']):
            return None
        return {
            'strategy_version': 'chain_v2',
            'signal_stage': expected['signal_stage'],
            'entry_route': expected['entry_route'],
            'acknowledged_at': receipt.get('updated_at'),
        }

    def _receipt(self, chain, pid, wallet, require_fresh=True):
        raw = self._read_chain_file(chain, 'live-status').get('terminal_control', {})
        if (not isinstance(raw, dict) or raw.get('supported') is not True or
                raw.get('pid') != pid or raw.get('chain') != chain or
                str(raw.get('wallet', '')).lower() != wallet or
                (require_fresh and not 0 <= age(raw.get('updated_at')) < 40)):
            return None
        return {key: raw.get(key) for key in ('supported', 'pid', 'chain', 'request_id',
                'action', 'state', 'reason', 'updated_at', 'exit_only',
                'strategy_version', 'signal_stage', 'entry_route')} | {
                    'amount_usd': _amount(raw.get('amount_usd')),
                    'amount_native_atomic': _amount(raw.get('amount_native_atomic'), atomic=True)}

    def _remote_worker(self, chain, wallet):
        sync = self._read_optional(self.repo / 'outputs' / f'remote-{chain}-sync-status.json')
        if sync.get('ok') is not True or sync.get('chain') != chain or age(sync.get('updated_at')) >= 15:
            return None
        receipt = self._read_chain_file(chain, 'live-status').get('terminal_control', {})
        pid = receipt.get('pid')
        if (not isinstance(receipt, dict) or receipt.get('supported') is not True or
                receipt.get('chain') != chain or str(receipt.get('wallet', '')).lower() != wallet or
                type(pid) is not int or pid <= 0 or age(receipt.get('updated_at')) >= 15):
            return None
        effective_strategy = self._effective_strategy(chain, receipt)
        return {'chain': chain, 'pid': pid, 'parent_pid': None, 'running': True,
                'amount_usd': _amount(receipt.get('amount_usd')),
                'configured_amount_usd': CHAIN_V2[chain]['amount_usd'],
                'effective_amount_usd': _amount(receipt.get('amount_usd')) if effective_strategy else None,
                'executor_acknowledged_at': receipt.get('updated_at') if effective_strategy else None,
                'effective_strategy': effective_strategy,
                'amount_native_atomic': _amount(receipt.get('amount_native_atomic'), atomic=True),
                'exit_only': bool(receipt.get('exit_only')), 'verified_at': receipt.get('updated_at'),
                'config_source': 'remote_worker_ack', 'location': 'japan', 'remote': True}

    def runtime(self):
        result = super().runtime()
        for row in result['strategies']:
            row['configured_amount_usd'] = row.get('amount_usd')
            row['effective_amount_usd'] = None
            row['executor_acknowledged_at'] = None
            row['effective_strategy'] = None
        try:
            wallet = self._config()['wallet_address'].lower()
        except ControlError:
            wallet = ''
        for chain in STEMS:
            remote = self._remote_worker(chain, wallet)
            if remote:
                result['strategies'].append(remote)
        controls = []
        for chain in STEMS:
            rows = [r for r in result['strategies'] if r.get('chain') == chain]
            if len(rows) > 1:
                controls.append({'chain': chain, 'supported': False, 'state': 'conflict',
                                 'reason': 'duplicate_workers', 'request_id': None})
                continue
            current = rows[0] if rows else None
            receipt = self._receipt(chain, current['pid'], wallet, False) if current else None
            fresh = bool(receipt and 0 <= age(receipt.get('updated_at')) < 40)
            state = 'legacy_worker' if current and not receipt else 'ready' if fresh else 'stopped'
            reason = None
            if current and current.get('remote'):
                controls.append({'chain': chain, 'supported': False, 'state': 'remote_ready',
                                 'reason': 'remote_controls_pending', 'request_id': None})
                continue
            if receipt:
                current['config_source'] = 'worker_ack' if fresh else 'unknown'
                current.update({key: receipt[key] if fresh else None
                                for key in ('amount_usd', 'amount_native_atomic', 'exit_only')})
                effective_strategy = self._effective_strategy(chain, receipt) if fresh else None
                current['effective_amount_usd'] = receipt['amount_usd'] if effective_strategy else None
                current['executor_acknowledged_at'] = receipt['updated_at'] if effective_strategy else None
                current['effective_strategy'] = effective_strategy
                if not fresh:
                    state, reason = 'heartbeat_stale', 'worker_heartbeat_stale'
            command = self._read_chain_file(chain, 'terminal-control')
            start = self._read_chain_file(chain, 'terminal-start')
            is_start = start.get('wallet') == wallet and age(start.get('created_at')) < age(command.get('created_at'))
            request = start if is_start else command
            request_id = request.get('request_id')
            if request_id and request.get('wallet') == wallet:
                if is_start:
                    if current and fresh and current.get('parent_pid') == start.get('launcher_pid'):
                        state, reason = 'applied', 'started'
                    elif not current:
                        state = ('start_failed' if start.get('state') == 'failed' or age(start.get('created_at')) >= 30
                                 else 'starting')
                elif current and command.get('expected_pid') == current['pid']:
                    if fresh and receipt.get('request_id') == request_id:
                        state, reason = receipt.get('state'), receipt.get('reason')
                    elif fresh:
                        state = ('pending' if 0 <= age(command.get('created_at')) < ACKNOWLEDGEMENT_LOCK_SECONDS
                                 else 'timed_out')
                elif not current:
                    terminal = self._receipt(chain, command.get('expected_pid'), wallet, False)
                    if (terminal and terminal.get('request_id') == request_id and
                            terminal.get('action') == 'stop' and terminal.get('state') == 'applied' and
                            self._read_chain_file(chain, 'live-status').get('status') == 'stopped'):
                        state, reason = 'applied', 'stopped'
                    else:
                        state, reason = 'outcome_unknown', 'worker_exited_without_ack'
            controls.append({'chain': chain, 'supported': fresh, 'state': state,
                             'reason': reason, 'request_id': request_id if isinstance(request_id, str) else None})
        result['controls'] = controls
        return result

    def _command(self, payload):
        allowed = {'action', 'chain', 'wallet_address', 'expected_pid', 'amount_usd', 'request_id'}
        if set(payload) - allowed:
            return 400, {'error': 'invalid_payload'}
        action, chain = payload.get('action'), payload.get('chain')
        if action not in {'start', 'stop', 'apply', 'pause', 'resume'} or chain not in STEMS:
            return 400, {'error': 'invalid_action'}
        try:
            request_id = str(uuid.UUID(payload['request_id']))
        except (ValueError, TypeError, KeyError, AttributeError):
            return 400, {'error': 'invalid_request_id'}
        amount = _amount(payload.get('amount_usd'))
        if action in {'start', 'apply'} and amount is None:
            return 400, {'error': 'invalid_amount'}
        if (action in {'start', 'apply'}
                and Decimal(amount) != Decimal(CHAIN_V2[chain]['amount_usd'])):
            return 409, {'error': 'chain_v2_order_size_conflict'}
        if action in {'start', 'apply'}:
            amount = CHAIN_V2[chain]['amount_usd']
        if action not in {'start', 'apply'} and 'amount_usd' in payload:
            return 400, {'error': 'invalid_payload'}
        with self._chain_locked(chain):
            config = self._config()
            wallet = config['wallet_address'].lower()
            if str(payload.get('wallet_address', '')).lower() != wallet:
                return 409, {'error': 'wallet_address_mismatch'}
            remote = self._remote_worker(chain, wallet)
            if remote:
                return 409, {'error': 'already_running' if action == 'start' else 'remote_controls_pending'}
            history_root = self.root / 'terminal-requests'
            history_root.mkdir(exist_ok=True)
            self.secure_path(history_root)
            history_path = history_root / f'{request_id}.json'
            fingerprint = {'action': action, 'chain': chain, 'wallet': wallet,
                           'amount_usd': amount, 'expected_pid': payload.get('expected_pid')}
            if history_path.exists():
                stored = _read_json(history_path)
                if stored.get('fingerprint') != fingerprint:
                    return 409, {'error': 'request_id_conflict'}
                return stored['code'], stored['result']
            def remember(code, body, stage='submitted'):
                self._atomic(history_path, json.dumps({'fingerprint': fingerprint, 'code': code, 'result': body, 'stage': stage}))
                return code, body
            processes = self._process_snapshot()
            running = super().runtime()
            if not running['ok'] or any(r.get('chain') not in STEMS for r in running['strategies']):
                return 409, {'error': 'process_state_unavailable'}
            matches = [r for r in running['strategies'] if r['chain'] == chain]
            if len(matches) > 1:
                return 409, {'error': 'duplicate_workers'}
            previous = self._read_chain_file(
                chain, 'terminal-start' if action == 'start' else 'terminal-control')
            if previous.get('request_id') == request_id:
                if previous.get('action') != action or previous.get('wallet') != wallet or previous.get('amount_usd') != amount:
                    return 409, {'error': 'request_id_conflict'}
                if previous.get('state') == 'failed':
                    return 503, {'error': 'start_failed'}
                return 202, {'ok': True, 'state': 'submitted', 'request_id': request_id}
            if action == 'start':
                if matches:
                    return 409, {'error': 'already_running'}
                for row in processes.values():
                    if any(_basename(t) == 'start_okx_dex_sdk_live.ps1' for t in row['tokens']):
                        if (_option(row['tokens'], '-Chain') or 'bsc').lower() == chain:
                            return 409, {'error': 'already_starting'}
                if 0 <= age(previous.get('created_at')) < 30:
                    return 409, {'error': 'already_starting'}
                request = {'schema_version': 1, 'request_id': request_id, 'action': action,
                           'chain': chain, 'wallet': wallet, 'amount_usd': amount, 'created_at': self.clock()}
                remember(409, {'error': 'start_outcome_unknown', 'request_id': request_id}, 'reserved')
                self._atomic(self._path(chain, 'terminal-start'), json.dumps(request))
                try:
                    request['launcher_pid'] = self.launcher(self.repo, chain, wallet, amount)
                except Exception:
                    request['state'] = 'failed'
                    self._atomic(self._path(chain, 'terminal-start'), json.dumps(request))
                    return remember(503, {'error': 'start_failed', 'request_id': request_id}, 'failed')
                self._atomic(self._path(chain, 'terminal-start'), json.dumps(request))
                return remember(202, {'ok': True, 'state': 'starting', 'request_id': request_id})
            if not matches:
                return 409, {'error': 'worker_not_running'}
            worker = matches[0]
            if type(payload.get('expected_pid')) is not int or payload['expected_pid'] != worker['pid']:
                return 409, {'error': 'worker_changed'}
            receipt = self._receipt(chain, worker['pid'], wallet)
            if not receipt:
                if self._receipt(chain, worker['pid'], wallet, False):
                    return 409, {'error': 'worker_heartbeat_stale'}
                return 409, {'error': 'worker_upgrade_required'}
            if (previous.get('request_id') and previous.get('expected_pid') == worker['pid'] and
                    previous.get('request_id') != receipt.get('request_id') and
                    0 <= age(previous.get('created_at')) < ACKNOWLEDGEMENT_LOCK_SECONDS):
                return 409, {'error': 'command_pending'}
            request = {'schema_version': 1, 'request_id': request_id, 'action': action,
                       'chain': chain, 'wallet': wallet, 'expected_pid': worker['pid'],
                       'created_at': self.clock()}
            if action == 'apply':
                request['amount_usd'] = amount
            remember(409, {'error': 'command_delivery_unknown', 'request_id': request_id}, 'reserved')
            self._atomic(self._path(chain, 'terminal-control'), json.dumps(request))
            return remember(202, {'ok': True, 'state': 'submitted', 'request_id': request_id})

    def dispatch(self, action, payload):
        if action != 'strategy/command':
            return super().dispatch(action, payload)
        try:
            if not isinstance(payload, dict):
                return 400, {'error': 'invalid_payload'}
            return self._command(payload)
        except ControlError as error:
            return 409, {'error': error.code}
        except Exception:
            return 500, {'error': 'operation_failed'}
