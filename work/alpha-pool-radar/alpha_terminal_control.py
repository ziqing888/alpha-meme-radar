"""Local single-wallet administration, with no execution or worker-start API.

HTTP adapters must enforce loopback, same-origin/CSRF and request-size limits, and
must not log wallet/import payloads. dispatch returns (HTTP status, public JSON).
runtime is read-only. Amounts are decimal strings; unverified chains are None.

Only the standard setup_okx_live configuration and local ledger locations are
supported. Import serializes terminal writers and checks workers/positions again
before committing. External launchers do not share this lock: keep them stopped
throughout wallet replacement. No on-chain balance or external ledger is queried.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = Path.home() / '.config' / 'alpha-radar'
POWERSHELL = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
CHAINS = frozenset({'bsc', 'robinhood'})
CHAIN_V2_AMOUNTS = {'bsc': '1', 'robinhood': '5'}
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
KEY = re.compile(r'(?:0x)?[0-9a-fA-F]{64}')
CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
ERRORS = {
    'invalid_payload': 400, 'invalid_private_key': 400, 'invalid_strategy_config': 400,
    'wallet_address_mismatch': 400, 'unknown_action': 404, 'start_not_implemented': 405,
    'live_worker_running': 409, 'open_positions': 409, 'unresolved_account_state': 409,
    'configuration_changed': 409, 'control_busy': 409,
    'config_unavailable': 503, 'position_state_unavailable': 503,
    'process_state_unavailable': 503, 'wallet_validation_failed': 503,
    'encryption_failed': 500, 'storage_failed': 500, 'operation_failed': 500,
}
NODE_DAEMON = 'okx_dex_live_daemon.mjs'
WORKER_SCRIPTS = frozenset({NODE_DAEMON, 'okx_dex_executor.mjs',
    'alpha_okx_live_worker.py', 'alpha_gmgn_live_worker.py',
    'alpha_execution_worker.py', 'alpha_okx_roundtrip_live.py',
    'start_okx_dex_sdk_live.ps1', 'start_okx_live.ps1', 'start_gmgn_live.ps1',
    'start_bsc_execution_live.ps1'})


class ControlError(Exception):
    """Only fixed public codes, never child diagnostics or submitted values."""
    def __init__(self, code):
        self.code = code if code in ERRORS else 'operation_failed'
        super().__init__(self.code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _helper(args, *, input_text='', cwd=None, run=subprocess.run, error):
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(('OKX_', 'BSC_', 'EVM_', 'GMGN_'))
           and name.upper() not in {'ARMED', 'LIVE_TRADING_ENABLED', 'NODE_OPTIONS', 'NODE_PATH'}}
    env.update(OKX_LIVE_ENABLED='0', OKX_ALLOW_AUTOMATED_TRADES='0')
    try:
        result = run([str(arg) for arg in args], input=input_text.encode('utf-8'), cwd=cwd,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     timeout=20,
                     shell=False, creationflags=0x08000000 if os.name == 'nt' else 0,
                     env=env, check=False)
        if result.returncode != 0:
            raise ValueError()
        return result.stdout.decode('utf-8-sig').strip()
    except Exception:
        raise ControlError(error) from None


def derive_wallet_address(private_key, executor_root, *, run=subprocess.run):
    """Derive locally with existing ethers; key is sent only on stdin."""
    code = """
import('ethers').then(({Wallet}) => {
  let key = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', value => { key += value; });
  process.stdin.on('end', () => {
    try { process.stdout.write(new Wallet(key.trim()).address); }
    catch { process.exitCode = 2; }
  });
}).catch(() => { process.exitCode = 3; });
"""
    value = _helper([shutil.which('node') or 'node', '-e', code], input_text=private_key,
                    cwd=executor_root, run=run, error='wallet_validation_failed')
    if not ADDRESS.fullmatch(value) or int(value[2:], 16) == 0:
        raise ControlError('wallet_validation_failed')
    return value.lower()


_PS_ENCODING = """
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
"""
_PS_HEADER = _PS_ENCODING + """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -Global -ErrorAction Stop
"""


def protect_private_key(private_key, *, run=subprocess.run):
    """PowerShell's no-key SecureString format is current-user Windows DPAPI."""
    if os.name != 'nt':
        raise ControlError('encryption_failed')
    code = _PS_HEADER + """
$secure = $null
try {
    $secure = ConvertTo-SecureString -String ([Console]::In.ReadToEnd()) -AsPlainText -Force
    [Console]::Out.Write((ConvertFrom-SecureString -SecureString $secure))
} finally { if ($null -ne $secure) { $secure.Dispose() } }
"""
    value = _helper([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', code],
                    input_text=private_key, run=run, error='encryption_failed')
    if not re.fullmatch(r'[0-9a-fA-F]{32,16384}', value):
        raise ControlError('encryption_failed')
    return value


def set_private_acl(path, *, run=subprocess.run):
    """Restrict new files to this Windows identity and SYSTEM, matching setup."""
    if os.name != 'nt':
        raise ControlError('storage_failed')
    code = _PS_HEADER + """
$path = [Console]::In.ReadToEnd() | ConvertFrom-Json
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = Get-Acl -LiteralPath $path
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
$acl.SetOwner($sid)
$inheritance = [Security.AccessControl.InheritanceFlags]::None
if (Test-Path -LiteralPath $path -PathType Container) {
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
}
foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $identity, 'FullControl', $inheritance, 'None', 'Allow'))
}
Set-Acl -LiteralPath $path -AclObject $acl
"""
    _helper([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', code],
            input_text=json.dumps(str(path)), run=run, error='storage_failed')


def read_processes(*, run=subprocess.run):
    """Read CIM metadata only; never return raw metadata through the HTTP API."""
    if os.name != 'nt':
        raise ControlError('process_state_unavailable')
    code = _PS_ENCODING + """
$ErrorActionPreference = 'Stop'
$items = @(Get-CimInstance Win32_Process -ErrorAction Stop |
    Select-Object ProcessId, ParentProcessId, Name, CommandLine, CreationDate)
ConvertTo-Json -InputObject $items -Compress -Depth 3
"""
    text = _helper([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', code],
                   run=run, error='process_state_unavailable')
    try:
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError()
        return value
    except Exception:
        raise ControlError('process_state_unavailable') from None


def _tokens(command):
    if not isinstance(command, str):
        return []
    # CommandLineToArgvW preserves Windows backslashes and quoted executable paths.
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel.LocalFree.restype = wintypes.HLOCAL
        count = ctypes.c_int()
        values = shell.CommandLineToArgvW(command, ctypes.byref(count))
        if not values:
            raise ControlError('process_state_unavailable')
        try:
            return [values[i] for i in range(count.value)]
        finally:
            kernel.LocalFree(values)
    return [part.strip('"\'') for part in shlex.split(command, posix=False)]


def _basename(value):
    return value.replace('\\', '/').rsplit('/', 1)[-1].lower()


def _option(tokens, flag):
    indices = [i for i, value in enumerate(tokens) if value.lower() == flag.lower()]
    return tokens[indices[0] + 1] if len(indices) == 1 and indices[0] + 1 < len(tokens) else None


def _node_script(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == '--':
            return _basename(tokens[index + 1]) if index + 1 < len(tokens) else None
        if token in {'-e', '--eval', '-p', '--print'} or token.startswith(('--eval=', '--print=')):
            return None
        if token in {'-r', '--require', '--import', '--loader', '--experimental-loader', '-C', '--conditions'}:
            index += 2
        elif token.startswith('-'):
            index += 1
        else:
            return _basename(token)
    return None


def _amount(value, *, atomic=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    pattern = r'[0-9]{1,78}' if atomic else r'[0-9]{1,3}(?:\.[0-9]{1,18})?'
    if not re.fullmatch(pattern, text):
        return None
    number = Decimal(text)
    if (atomic and not 0 < number < 2 ** 256) or (not atomic and not Decimal('0.1') <= number <= 100):
        return None
    return text


def _read_json(path):
    with Path(path).open('r', encoding='utf-8-sig') as stream:
        text = stream.read(8 * 1024 * 1024 + 1)
    if len(text) > 8 * 1024 * 1024:
        raise ValueError()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError()
    return value


class TerminalControl:
    """Inject process/crypto/ACL/filesystem dependencies for offline tests.

    positions_reader(config) may be provided by a trusted local adapter; it must
    return an exact nonnegative int across ALL chains, or raise if unverified.
    No dependency, filesystem path, executable or command comes from POST data.
    """
    def __init__(self, config_root=None, repo_root=None, *, process_reader=None,
                 positions_reader=None, derive_address=None, protect_key=None,
                 secure_path=None, replace=None, clock=None):
        self.root = Path(config_root) if config_root is not None else CONFIG_ROOT
        self.repo = Path(repo_root) if repo_root is not None else ROOT
        self.config_path = self.root / 'okx-live-config.json'
        self.secret_root = self.root / 'okx-live-secrets'
        self.requests_path = self.root / 'terminal-strategy-requests.json'
        self.process_reader = process_reader or read_processes
        self.positions_reader = positions_reader or self._positions
        self.derive_address = derive_address or (lambda key: derive_wallet_address(
            key, self.repo / 'work' / 'alpha-pool-radar' / 'okx-dex-executor'))
        self.protect_key = protect_key or protect_private_key
        self.secure_path = secure_path or set_private_acl
        self.replace = replace or os.replace
        self.clock = clock or _now

    def _process_snapshot(self):
        try:
            rows = self.process_reader()
            if not isinstance(rows, list):
                raise ValueError()
            parsed = {}
            for row in rows:
                if not isinstance(row, dict) or type(row.get('ProcessId')) is not int:
                    raise ValueError()
                name = str(row.get('Name', '')).lower()
                relevant = name in {'node', 'node.exe', 'powershell.exe', 'pwsh.exe', 'python.exe',
                                    'pythonw.exe', 'python', 'python3', 'py.exe'}
                if relevant and not row.get('CommandLine'):
                    raise ValueError()
                parsed[row['ProcessId']] = {**row, 'tokens': _tokens(row.get('CommandLine'))}
            return parsed
        except Exception:
            raise ControlError('process_state_unavailable') from None

    @staticmethod
    def _has_worker(snapshot):
        return any(any(_basename(token) in WORKER_SCRIPTS for token in row['tokens'])
                   for row in snapshot.values())

    def runtime(self):
        """Return {ok, strategies, verified_at[, error]}; no writes or secret reads."""
        verified_at = self.clock()
        try:
            snapshot = self._process_snapshot()
            strategies = []
            for pid, row in sorted(snapshot.items()):
                tokens = row['tokens']
                if (str(row.get('Name', '')).lower() not in {'node', 'node.exe'} or
                        _node_script(tokens) != NODE_DAEMON):
                    continue
                chain = None
                parent = snapshot.get(row.get('ParentProcessId'))
                if parent and any(_basename(token) == 'start_okx_dex_sdk_live.ps1'
                                  for token in parent['tokens']):
                    candidate = _option(parent['tokens'], '-Chain')
                    if candidate and candidate.lower() in CHAINS:
                        # PID reuse can attach an unrelated newer launcher to an orphan.
                        child_time, parent_time = row.get('CreationDate'), parent.get('CreationDate')
                        if not child_time or not parent_time or parent_time <= child_time:
                            chain = candidate.lower()
                strategies.append({'chain': chain, 'pid': pid, 'parent_pid': row.get('ParentProcessId'), 'running': True,
                    'amount_usd': _amount(_option(tokens, '--amount-usd')),
                    'amount_native_atomic': _amount(_option(tokens, '--amount-native-atomic'), atomic=True),
                    'exit_only': '--exit-only' in tokens, 'verified_at': verified_at})
            return {'ok': True, 'strategies': strategies, 'verified_at': verified_at}
        except Exception:
            return {'ok': False, 'strategies': [], 'verified_at': verified_at,
                    'error': 'process_state_unavailable'}

    def _config(self):
        try:
            config = _read_json(self.config_path)
            wallet = config.get('wallet_address')
            if (type(config.get('schema_version')) is not int or config['schema_version'] != 1 or
                    not isinstance(wallet, str) or not ADDRESS.fullmatch(wallet) or int(wallet[2:], 16) == 0):
                raise ValueError()
            for field in ('wallet_private_key_dpapi_path', 'api_key_dpapi_path',
                          'secret_key_dpapi_path', 'passphrase_dpapi_path'):
                path = Path(config[field])
                if not path.is_absolute() or path.resolve().parent != self.secret_root.resolve():
                    raise ValueError()
            if Path(config['state_root']).resolve() != (self.root / 'gmgn-live').resolve():
                raise ValueError()
            return config
        except Exception:
            raise ControlError('config_unavailable') from None

    def _positions(self, config):
        count = 0
        # Enumerate every chain's daemon file, not just the chain selected in the UI.
        with os.scandir(self.repo / 'outputs') as entries:
            files = [Path(entry.path) for entry in entries if re.fullmatch(
                r'okx-dex-sdk(?:-[a-z0-9-]+)?-live-state\.json', entry.name)]
        for path in files:
            state = _read_json(path)
            if not isinstance(state.get('positions'), dict):
                raise ValueError()
            count += len(state['positions'])
        state_root = Path(config['state_root'])
        if not state_root.is_dir():
            raise ValueError()
        def failed(error):
            raise error
        for directory, dirs, names in os.walk(state_root, onerror=failed):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise ValueError()
            if 'ledger.sqlite3' in names:
                path = Path(directory) / 'ledger.sqlite3'
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                    row = db.execute('SELECT data FROM ledger WHERE id=1').fetchone()
                state = json.loads(row[0]) if row else None
                if (not isinstance(state, dict) or not isinstance(state.get('positions'), dict) or
                        not isinstance(state.get('orders'), dict)):
                    raise ValueError()
                count += len(state['positions'])
                for order in state['orders'].values():
                    if (not isinstance(order, dict) or order.get('status') not in {'filled', 'failed', 'rejected'}
                            or order.get('fill_anomaly')):
                        raise ControlError('unresolved_account_state')
            if 'roundtrip-status.json' in names:
                state = _read_json(Path(directory) / 'roundtrip-status.json')
                remaining = str(state.get('remaining_atomic', ''))
                if not re.fullmatch(r'[0-9]{1,78}', remaining):
                    raise ValueError()
                count += int(int(remaining) > 0)
                if not count and state.get('state') not in {'completed', 'failed', 'blocked', 'stopped'}:
                    raise ControlError('unresolved_account_state')
        return count

    def _require_idle(self, config):
        if self._has_worker(self._process_snapshot()):
            raise ControlError('live_worker_running')
        try:
            positions = self.positions_reader(config)
            if type(positions) is not int or positions < 0:
                raise ValueError()
        except ControlError:
            raise
        except Exception:
            raise ControlError('position_state_unavailable') from None
        if positions:
            raise ControlError('open_positions')

    @contextmanager
    def _locked(self):
        # Exclusive creation also serializes separate Python server processes.
        path = self.root / '.terminal-control.lock'
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

    def _atomic(self, destination, text, *, staging_directory=None, before_replace=None):
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix='.terminal-', suffix='.tmp',
                                        dir=staging_directory or destination.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                self.secure_path(temporary)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            if before_replace is not None:
                before_replace()
            self.replace(temporary, destination)
        except ControlError:
            raise
        except Exception:
            raise ControlError('storage_failed') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _import(self, payload):
        if set(payload) - {'private_key', 'wallet_address'}:
            raise ControlError('invalid_payload')
        key = payload.get('private_key')
        if not isinstance(key, str) or not KEY.fullmatch(key.strip()):
            raise ControlError('invalid_private_key')
        key = key.strip()
        if not 0 < int(key, 16) < CURVE_ORDER:
            raise ControlError('invalid_private_key')
        key = '0x' + key.removeprefix('0x')
        expected = payload.get('wallet_address')
        if expected is not None and (not isinstance(expected, str) or not ADDRESS.fullmatch(expected)):
            raise ControlError('invalid_payload')
        with self._locked():
            config = self._config()
            self._require_idle(config)
            try:
                address = self.derive_address(key)
                if not isinstance(address, str) or not ADDRESS.fullmatch(address) or int(address[2:], 16) == 0:
                    raise ValueError()
            except Exception:
                raise ControlError('wallet_validation_failed') from None
            if expected is not None and address.lower() != expected.lower():
                raise ControlError('wallet_address_mismatch')
            try:
                encrypted = self.protect_key(key)
                if not isinstance(encrypted, str) or not re.fullmatch(r'[0-9a-fA-F]{32,16384}', encrypted):
                    raise ValueError()
            except Exception:
                raise ControlError('encryption_failed') from None
            finally:
                key = None
            self._require_idle(config)
            if self._config() != config:
                raise ControlError('configuration_changed')
            new_key = self.secret_root / (uuid.uuid4().hex + '-bsc_private_key.dpapi')
            updated = {**config, 'wallet_address': address.lower(), 'wallet_address_derived': True,
                       'wallet_private_key_dpapi_path': str(new_key)}
            def verify_commit():
                self._require_idle(config)
                if self._config() != config:
                    raise ControlError('configuration_changed')
            committed = False
            try:
                self.secure_path(self.secret_root)
                self._atomic(new_key, encrypted)
                # Publish a new immutable key first; never overwrite the previous key.
                self._atomic(self.config_path, json.dumps(updated, indent=2) + '\n',
                             staging_directory=self.secret_root, before_replace=verify_commit)
                committed = True
            finally:
                if not committed:
                    new_key.unlink(missing_ok=True)
            return 200, {'ok': True, 'wallet_address': address.lower(),
                         'wallet_address_derived': True, 'live_started': False}

    def _stage_strategy(self, payload):
        allowed = {'chain', 'amount_usd', 'exit_only'}
        chain = payload.get('chain')
        if set(payload) - allowed or not isinstance(chain, str) or chain not in CHAINS:
            raise ControlError('invalid_strategy_config')
        usd = _amount(payload.get('amount_usd'))
        if (usd is None or Decimal(usd) != Decimal(CHAIN_V2_AMOUNTS[chain]) or
                type(payload.get('exit_only', False)) is not bool):
            raise ControlError('invalid_strategy_config')
        requested = {'chain': chain, 'amount_usd': CHAIN_V2_AMOUNTS[chain], 'amount_native_atomic': None,
                     'exit_only': payload.get('exit_only', False), 'requested_at': self.clock()}
        with self._locked():
            if self.requests_path.exists():
                try:
                    stored = _read_json(self.requests_path)
                    if stored.get('schema_version') != 1 or not isinstance(stored.get('strategies'), dict):
                        raise ValueError()
                except Exception:
                    raise ControlError('storage_failed') from None
            else:
                stored = {'schema_version': 1, 'strategies': {}}
            stored['strategies'][chain] = requested
            self._atomic(self.requests_path, json.dumps(stored, indent=2) + '\n')
        return 202, {'ok': True, 'staged': True, 'live_started': False, 'requested_config': requested}

    def dispatch(self, action, payload):
        """wallet/import, strategy/config, strategy/start (always 405)."""
        try:
            if not isinstance(payload, dict):
                raise ControlError('invalid_payload')
            if action == 'wallet/import':
                return self._import(payload)
            if action == 'strategy/config':
                return self._stage_strategy(payload)
            raise ControlError('start_not_implemented' if action == 'strategy/start' else 'unknown_action')
        except ControlError as error:
            return ERRORS[error.code], {'error': error.code}
        except Exception:
            return 500, {'error': 'operation_failed'}


def dispatch(action, payload):
    """Default local instance; returns (HTTP status, public JSON object)."""
    return TerminalControl().dispatch(action, payload)


def runtime():
    """Read-only default instance; returns public runtime JSON."""
    return TerminalControl().runtime()
