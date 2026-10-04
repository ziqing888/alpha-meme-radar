"""Offline terminal controls; all mutable fixtures live in temporary directories."""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import alpha_terminal_control as control


KEY = '0x' + '0' * 63 + '1'
ADDRESS = '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf'
OLD_ADDRESS = '0x' + '2' * 40
STAMP = '2026-09-09T00:00:00+00:00'
CIPHER = '01000000' + 'ab' * 40


def process(pid, command, parent=0, name='node.exe'):
    return {'ProcessId': pid, 'ParentProcessId': parent, 'Name': name,
            'CommandLine': command, 'CreationDate': STAMP}


class TerminalControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.outputs = self.repo / 'outputs'
        self.outputs.mkdir(parents=True)
        self.root = Path(self.temp.name) / 'config'
        self.secrets = self.root / 'okx-live-secrets'
        self.secrets.mkdir(parents=True)
        self.state = self.root / 'gmgn-live'
        self.state.mkdir()
        self.config_path = self.root / 'okx-live-config.json'
        self.old_key = self.secrets / 'old.dpapi'
        self.old_key.write_text('old encrypted fixture', encoding='utf-8')
        self.config = {
            'schema_version': 1, 'wallet_address': OLD_ADDRESS,
            'wallet_address_derived': True,
            'wallet_private_key_dpapi_path': str(self.old_key),
            'api_key_dpapi_path': str(self.secrets / 'api.dpapi'),
            'secret_key_dpapi_path': str(self.secrets / 'secret.dpapi'),
            'passphrase_dpapi_path': str(self.secrets / 'phrase.dpapi'),
            'state_root': str(self.state), 'bsc_rpc_url': 'https://example.test',
            'future_option': {'keep': True},
        }
        self.write_json(self.config_path, self.config)
        self.processes = []
        self.subject = self.make_control()

    def make_control(self, **overrides):
        dependencies = dict(config_root=self.root, repo_root=self.repo,
                            process_reader=lambda: self.processes,
                            derive_address=lambda key: ADDRESS,
                            protect_key=lambda key: CIPHER,
                            secure_path=lambda path: None,
                            clock=lambda: STAMP)
        dependencies.update(overrides)
        return control.TerminalControl(**dependencies)

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def import_key(self, subject=None, **extra):
        return (subject or self.subject).dispatch('wallet/import', {'private_key': KEY, **extra})

    def test_import_preserves_credentials_and_only_repoints_versioned_wallet_key(self):
        code, body = self.import_key()
        self.assertEqual(code, 200)
        self.assertEqual(body, {'ok': True, 'wallet_address': ADDRESS.lower(),
                                'wallet_address_derived': True, 'live_started': False})
        current = json.loads(self.config_path.read_text())
        for field, value in self.config.items():
            if field not in {'wallet_address', 'wallet_private_key_dpapi_path'}:
                self.assertEqual(current[field], value)
        self.assertEqual(current['wallet_address'], ADDRESS.lower())
        self.assertEqual(Path(current['wallet_private_key_dpapi_path']).read_text(), CIPHER)
        self.assertEqual(self.old_key.read_text(), 'old encrypted fixture')
        self.assertNotIn(KEY, json.dumps(body))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text())

    def test_rejects_any_daemon_even_if_chain_or_live_flag_unknown(self):
        self.processes = [process(9, 'node okx_dex_live_daemon.mjs --amount-usd 1')]
        self.assertEqual(self.import_key(), (409, {'error': 'live_worker_running'}))
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_rejects_launcher_before_child_exists_and_legacy_worker(self):
        for command, name in [
            ('powershell -File start_okx_dex_sdk_live.ps1 -Daemon -Live', 'powershell.exe'),
            ('python alpha_okx_live_worker.py --live', 'python.exe'),
            ('node okx_dex_executor.mjs --live', 'node.exe'),
        ]:
            with self.subTest(command=command):
                self.processes = [process(1, command, name=name)]
                self.assertEqual(self.import_key()[1]['error'], 'live_worker_running')

    def test_rejects_open_positions_on_any_chain(self):
        for stem in ('okx-dex-sdk-live-state.json', 'okx-dex-sdk-robinhood-live-state.json',
                     'okx-dex-sdk-future-chain-live-state.json'):
            with self.subTest(stem=stem):
                path = self.outputs / stem
                self.write_json(path, {'version': 1, 'positions': {'token': {'amount': '1'}}})
                self.assertEqual(self.import_key(), (409, {'error': 'open_positions'}))
                path.unlink()

    def test_legacy_sqlite_positions_are_read_only_and_block_import(self):
        directory = self.state / OLD_ADDRESS
        directory.mkdir()
        ledger = directory / 'ledger.sqlite3'
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, data TEXT)')
            db.execute('INSERT INTO ledger VALUES(1, ?)', (json.dumps({
                'wallet': OLD_ADDRESS, 'positions': {'token': {}}, 'orders': {}}),))
        before = ledger.read_bytes()
        self.assertEqual(self.import_key()[1]['error'], 'open_positions')
        self.assertEqual(ledger.read_bytes(), before)

    def test_unresolved_legacy_order_blocks_wallet_change(self):
        path = self.state / OLD_ADDRESS / 'ledger.sqlite3'
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, data TEXT)')
            db.execute('INSERT INTO ledger VALUES(1, ?)', (json.dumps({
                'positions': {}, 'orders': {'order': {'status': 'pending'}}}),))
        self.assertEqual(self.import_key(), (409, {'error': 'unresolved_account_state'}))

    def test_roundtrip_remaining_holdings_block_import(self):
        self.write_json(self.state / OLD_ADDRESS / 'roundtrip-status.json',
                        {'state': 'failed', 'remaining_atomic': '12'})
        self.assertEqual(self.import_key(), (409, {'error': 'open_positions'}))

    def test_injected_positions_reader_requires_an_exact_nonnegative_integer(self):
        for result in (None, False, -1, {}, []):
            subject = self.make_control(positions_reader=lambda config: result)
            self.assertEqual(self.import_key(subject)[1]['error'], 'position_state_unavailable')

    def test_missing_or_alternate_config_does_not_create_wallet_setup(self):
        self.config_path.unlink()
        self.assertEqual(self.import_key()[1]['error'], 'config_unavailable')
        self.write_json(self.config_path, {**self.config, 'state_root': str(self.repo)})
        self.assertEqual(self.import_key()[1]['error'], 'config_unavailable')

    def test_malformed_state_and_unavailable_process_inventory_fail_closed(self):
        path = self.outputs / 'okx-dex-sdk-live-state.json'
        for raw in ('{', '{}', '{"positions": null}'):
            path.write_text(raw)
            self.assertEqual(self.import_key()[1]['error'], 'position_state_unavailable')
        path.unlink()
        def unavailable():
            raise RuntimeError(KEY)
        subject = self.make_control(process_reader=unavailable)
        self.assertEqual(self.import_key(subject), (503, {'error': 'process_state_unavailable'}))
        self.assertEqual(subject.runtime()['error'], 'process_state_unavailable')

    def test_inaccessible_node_command_line_blocks_import(self):
        self.processes = [process(4, None)]
        self.assertEqual(self.import_key()[1]['error'], 'process_state_unavailable')

    def test_runtime_uses_parent_chain_and_child_amounts_only(self):
        self.processes = [
            process(4, 'powershell -File "C:\\folder space\\start_okx_dex_sdk_live.ps1" -Chain robinhood -Daemon -Live', name='powershell.exe'),
            process(5, 'node "C:\\folder space\\okx_dex_live_daemon.mjs" --amount-usd 2.50 --exit-only --live', 4),
            process(6, 'node okx_dex_live_daemon.mjs --amount-native-atomic 1000000000000000001', 999),
            process(7, 'node unrelated.mjs --amount-usd 99'),
        ]
        before = self.config_path.read_bytes()
        body = self.subject.runtime()
        self.assertTrue(body['ok'])
        self.assertEqual(body['strategies'], [
            {'chain': 'robinhood', 'pid': 5, 'parent_pid': 4, 'running': True, 'amount_usd': '2.50',
             'amount_native_atomic': None, 'exit_only': True, 'verified_at': STAMP},
            {'chain': None, 'pid': 6, 'parent_pid': 999, 'running': True, 'amount_usd': None,
             'amount_native_atomic': '1000000000000000001', 'exit_only': False, 'verified_at': STAMP},
        ])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertNotIn('CommandLine', json.dumps(body))

    def test_chain_never_defaults_from_missing_or_unrecognized_parent_argument(self):
        for suffix in ('', '-Chain mystery', '-Chain bsc -Chain robinhood'):
            self.processes = [process(1, 'pwsh -File start_okx_dex_sdk_live.ps1 ' + suffix, name='pwsh.exe'),
                              process(2, 'node okx_dex_live_daemon.mjs', 1)]
            self.assertIsNone(self.subject.runtime()['strategies'][0]['chain'])

    def test_runtime_does_not_echo_malformed_amounts_or_arbitrary_arguments(self):
        self.processes = [process(2, 'node okx_dex_live_daemon.mjs --amount-usd ' + KEY)]
        body = self.subject.runtime()
        self.assertIsNone(body['strategies'][0]['amount_usd'])
        self.assertNotIn(KEY, json.dumps(body))

    def test_unrelated_node_eval_is_not_a_daemon_and_reused_parent_pid_is_unknown(self):
        self.processes = [process(3, 'node -e okx_dex_live_daemon.mjs')]
        self.assertEqual(self.subject.runtime()['strategies'], [])
        self.processes = [
            process(1, 'pwsh -File start_okx_dex_sdk_live.ps1 -Chain bsc', name='pwsh.exe'),
            {**process(2, 'node okx_dex_live_daemon.mjs', 1), 'CreationDate': '2026-09-08T00:00:00+00:00'}]
        self.assertIsNone(self.subject.runtime()['strategies'][0]['chain'])

    def test_invalid_keys_and_unexpected_payload_fields_rejected_before_derivation(self):
        def forbidden(key):
            self.fail('invalid input reached helper')
        subject = self.make_control(derive_address=forbidden)
        for key in ('', '0x' + '0' * 64, '0x' + 'f' * 64, ['key'], KEY + '\nother'):
            self.assertEqual(subject.dispatch('wallet/import', {'private_key': key})[0], 400)
        self.assertEqual(self.import_key(subject, start=True)[0], 400)

    def test_expected_wallet_mismatch_does_not_write(self):
        self.assertEqual(self.import_key(wallet_address=OLD_ADDRESS),
                         (400, {'error': 'wallet_address_mismatch'}))
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_secret_bearing_helper_failure_is_sanitized(self):
        def fail(key):
            raise RuntimeError('provider diagnostic: ' + key)
        for dependency in ('derive_address', 'protect_key'):
            subject = self.make_control(**{dependency: fail})
            code, body = self.import_key(subject)
            self.assertGreaterEqual(code, 400)
            self.assertNotIn(KEY, json.dumps(body))
            self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_config_replace_failure_keeps_previous_key_and_config_usable(self):
        def replace(source, destination):
            if Path(destination) == self.config_path:
                raise OSError(KEY)
            os.replace(source, destination)
        code, body = self.import_key(self.make_control(replace=replace))
        self.assertEqual((code, body), (500, {'error': 'storage_failed'}))
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)
        self.assertTrue(self.old_key.is_file())
        self.assertEqual(set(self.secrets.iterdir()), {self.old_key})

    def test_worker_starting_during_validation_is_rechecked_before_commit(self):
        def derive(key):
            self.processes.append(process(10, 'node okx_dex_live_daemon.mjs --live'))
            return ADDRESS
        self.assertEqual(self.import_key(self.make_control(derive_address=derive))[1]['error'],
                         'live_worker_running')
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_worker_starting_during_config_staging_prevents_final_replace(self):
        prepared = []
        def secure(path):
            if Path(path).suffix == '.tmp':
                prepared.append(path)
                if len(prepared) == 2:
                    self.processes.append(process(10, 'node okx_dex_live_daemon.mjs --live'))
        code, body = self.import_key(self.make_control(secure_path=secure))
        self.assertEqual((code, body), (409, {'error': 'live_worker_running'}))
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)
        self.assertEqual(set(self.secrets.iterdir()), {self.old_key})

    def test_persistent_lock_blocks_competing_import(self):
        (self.root / '.terminal-control.lock').write_text('')
        self.assertEqual(self.import_key(), (409, {'error': 'control_busy'}))

    def test_config_change_during_validation_is_not_overwritten(self):
        def derive(key):
            self.write_json(self.config_path, {**self.config, 'new_option': 42})
            return ADDRESS
        self.assertEqual(self.import_key(self.make_control(derive_address=derive))[1]['error'],
                         'configuration_changed')
        self.assertEqual(json.loads(self.config_path.read_text())['new_option'], 42)

    def test_strategy_config_is_staged_separately_and_does_not_run_processes(self):
        def forbidden():
            self.fail('staging must not launch or even need process inventory')
        subject = self.make_control(process_reader=forbidden)
        payload = {'chain': 'robinhood', 'amount_usd': '5', 'exit_only': True}
        code, body = subject.dispatch('strategy/config', payload)
        self.assertEqual(code, 202)
        self.assertEqual(body, {'ok': True, 'staged': True, 'live_started': False,
                                'requested_config': {**payload, 'amount_native_atomic': None,
                                                     'requested_at': STAMP}})
        stored = json.loads((self.root / 'terminal-strategy-requests.json').read_text())
        self.assertEqual(stored['strategies']['robinhood'], body['requested_config'])
        subject.dispatch('strategy/config', {'chain': 'bsc', 'amount_usd': '1'})
        stored = json.loads((self.root / 'terminal-strategy-requests.json').read_text())
        self.assertEqual(set(stored['strategies']), {'bsc', 'robinhood'})
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)
        self.assertEqual(subject.dispatch('strategy/start', {'chain': 'bsc'}),
                         (405, {'error': 'start_not_implemented'}))

    def test_strategy_payload_validation(self):
        for payload in ({'chain': 'bsc'}, {'chain': 'unknown', 'amount_usd': '1'},
                        {'chain': 'bsc', 'amount_usd': 'nan'},
                        {'chain': 'bsc', 'amount_usd': '101'},
                        {'chain': 'bsc', 'amount_usd': True},
                        {'chain': 'bsc', 'amount_native_atomic': '0'},
                        {'chain': 'bsc', 'amount_native_atomic': str(2 ** 256)},
                        {'chain': 'bsc', 'amount_usd': '5'},
                        {'chain': 'robinhood', 'amount_usd': '1'},
                        {'chain': 'bsc', 'amount_usd': '1', 'exit_only': 'false'},
                        {'chain': 'bsc', 'amount_usd': '1', 'amount_native_atomic': '1'},
                        {'chain': 'bsc', 'amount_usd': '1', 'private_key': KEY}):
            with self.subTest(payload=payload):
                self.assertEqual(self.subject.dispatch('strategy/config', payload)[0], 400)
        self.assertFalse((self.root / 'terminal-strategy-requests.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
    def test_real_dpapi_and_private_acl_import_roundtrip_in_temporary_directory(self):
        subject = self.make_control(protect_key=control.protect_private_key,
                                    secure_path=control.set_private_acl)
        code, body = self.import_key(subject)
        self.assertEqual((code, body.get('error')), (200, None))
        current = json.loads(self.config_path.read_text())
        encrypted = Path(current['wallet_private_key_dpapi_path']).read_text()
        self.assertNotIn(KEY, encrypted)
        # Decrypt only our synthetic fixture; the child emits a boolean, never a key.
        script = control._PS_HEADER + """
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
$secure = ConvertTo-SecureString -String $data.encrypted
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $matches = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) -ceq $data.expected
  $acl = Get-Acl -LiteralPath $data.path
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $ids = @($acl.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
  $private = $acl.AreAccessRulesProtected -and ($ids.Count -eq 2) -and
      ($ids -contains $sid) -and ($ids -contains 'S-1-5-18')
  [Console]::Out.Write(($matches -and $private).ToString())
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  $secure.Dispose()
}
"""
        result = control._helper([control.POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script],
                                 input_text=json.dumps({'encrypted': encrypted, 'expected': KEY,
                                                        'path': str(self.config_path)}),
                                 error='encryption_failed')
        self.assertEqual(result, 'True')


class HelperTests(unittest.TestCase):
    def test_key_only_travels_over_stdin_with_hidden_timeout_and_disabled_live_env(self):
        def runner(args, **options):
            self.assertNotIn(KEY, repr(args))
            self.assertEqual(options['input'], KEY.encode('utf-8'))
            self.assertNotIn('text', options)
            self.assertFalse(options.get('shell', False))
            self.assertGreater(options['timeout'], 0)
            self.assertLessEqual(options['timeout'], 20)
            self.assertEqual(options['env']['OKX_LIVE_ENABLED'], '0')
            self.assertEqual(options['env']['OKX_ALLOW_AUTOMATED_TRADES'], '0')
            if os.name == 'nt':
                self.assertEqual(options['creationflags'], 0x08000000)
            return subprocess.CompletedProcess(args, 0, ADDRESS.encode('utf-8'), b'')
        result = control.derive_wallet_address(KEY, Path(__file__).parent / 'okx-dex-executor', run=runner)
        self.assertEqual(result, ADDRESS.lower())

    def test_subprocess_timeout_and_stderr_are_never_in_public_errors(self):
        for failure in (subprocess.TimeoutExpired(['node'], 20, output=KEY, stderr=KEY),
                        OSError(KEY)):
            def runner(*args, **kwargs):
                raise failure
            with self.assertRaises(control.ControlError) as raised:
                control.derive_wallet_address(KEY, Path('.'), run=runner)
            self.assertNotIn(KEY, str(raised.exception))

    def test_invalid_helper_output_encoding_is_caught_without_reader_thread_traceback(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, b'\xff', b'')
        with self.assertRaises(control.ControlError) as raised:
            control.derive_wallet_address(KEY, Path('.'), run=runner)
        self.assertEqual(str(raised.exception), 'wallet_validation_failed')

    @unittest.skipUnless(os.name == 'nt', 'Windows CIM inventory')
    def test_real_process_inventory_reads_host_without_serializing_commands_to_test_output(self):
        rows = control.read_processes()
        self.assertIsInstance(rows, list)
        self.assertTrue(any(row.get('ProcessId') == os.getpid() for row in rows))

    @unittest.skipUnless(shutil.which('node') and
                         (Path(__file__).parent / 'okx-dex-executor/node_modules/ethers').is_dir(),
                         'local Node ethers not installed')
    def test_real_ethers_derives_public_fixture_without_network_or_wallet_files(self):
        self.assertEqual(control.derive_wallet_address(KEY, Path(__file__).parent / 'okx-dex-executor'),
                         ADDRESS.lower())


if __name__ == '__main__':
    unittest.main()
