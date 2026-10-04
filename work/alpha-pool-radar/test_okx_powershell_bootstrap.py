"""Operator-script bootstrap tests using temporary files, never real credentials."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_dex_sdk_launcher_forwards_date_bound_loss_override():
    source = Path(__file__).with_name("start_okx_dex_sdk_live.ps1").read_text(encoding="utf-8")
    assert "[string]$DailyLossOverrideDate = ''" in source
    assert "OKX_DAILY_LOSS_OVERRIDE_CHAIN" in source
    assert "OKX_DAILY_LOSS_OVERRIDE_DATE" in source
    assert "yyyy-MM-dd" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI and ACL integration")
@pytest.mark.parametrize("script", ["setup_okx_live.ps1", "start_okx_live.ps1"])
def test_native_security_module_with_inherited_powershell7_paths(script, tmp_path):
    source = Path(__file__).with_name(script)
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OKX_", "BSC_", "GMGN_"))}
    env["BOOTSTRAP_SOURCE"] = str(source)
    env["BOOTSTRAP_FIXTURE"] = str(tmp_path)
    # Keep the inherited PS7 module paths: this is how Start-Process launched PS5.
    code = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:BOOTSTRAP_SOURCE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_error' }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [Management.Automation.Language.FunctionDefinitionAst] -or
        ($statement -is [Management.Automation.Language.PipelineAst] -and
         $statement.PipelineElements[0] -is [Management.Automation.Language.CommandAst] -and
         $statement.PipelineElements[0].GetCommandName() -eq 'Import-Module')) {
        . ([ScriptBlock]::Create($statement.Extent.Text))
    }
}
$directory = $env:BOOTSTRAP_FIXTURE
$file = Join-Path $directory 'synthetic.dpapi'
if (Get-Command Set-OkxPrivateAcl -ErrorAction SilentlyContinue) {
    Set-OkxPrivateAcl $directory
}
$secret = ConvertTo-SecureString 'offline-test-only' -AsPlainText -Force
try { [IO.File]::WriteAllText($file, (ConvertFrom-SecureString $secret)) }
finally { $secret.Dispose() }
if (Get-Command Set-OkxPrivateAcl -ErrorAction SilentlyContinue) {
    Set-OkxPrivateAcl $file
    $acl = Get-Acl -LiteralPath $file
    if (-not $acl.AreAccessRulesProtected -or $acl.Access.Count -ne 2) { throw 'acl_not_private' }
}
if (Get-Command Read-OkxProtectedSecret -ErrorAction SilentlyContinue) {
    if ((Read-OkxProtectedSecret $file) -ne 'offline-test-only') { throw 'dpapi_roundtrip_failed' }
}
$expected = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
if ((Get-Command Get-Acl).Module.Path -ine $expected) { throw 'wrong_security_module' }
Write-Output 'bootstrap_ok'
'''
    encoded = base64.b64encode(code.encode("utf-16-le")).decode()
    result = subprocess.run([str(executable), "-NoProfile", "-EncodedCommand", encoded],
                            env=env, capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-1600:]
    assert b"bootstrap_ok" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI and ACL integration")
def test_full_setup_saves_only_encrypted_fixture_credentials(tmp_path):
    source = Path(__file__).with_name("setup_okx_live.ps1").read_text(encoding="utf-8")
    home_expression = "[Environment]::GetFolderPath('UserProfile')"
    assert source.count(home_expression) == 1
    checkout = tmp_path / "checkout/work/radar"
    checkout.mkdir(parents=True)
    script = checkout / "setup_okx_live.ps1"
    script.write_text(source.replace(home_expression, "$env:BOOTSTRAP_HOME"), encoding="utf-8")
    home = tmp_path / "synthetic-home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKX_", "BSC_", "GMGN_"))}
    env.update(BOOTSTRAP_HOME=str(home), BOOTSTRAP_SOURCE=str(script), BOOTSTRAP_PYTHON=sys.executable)
    code = r'''
$ErrorActionPreference = 'Stop'
function Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    $value = 'offline-fixture-credential'
    if ($Prompt -like 'BSC_PRIVATE_KEY*') { $value = '0x' + ('01' * 32) }
    ConvertTo-SecureString $value -AsPlainText -Force
}
& $env:BOOTSTRAP_SOURCE -PythonPath $env:BOOTSTRAP_PYTHON
'''
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-EncodedCommand",
                             base64.b64encode(code.encode("utf-16-le")).decode()],
                            env=env, capture_output=True, timeout=40,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-1800:]
    config_path = home / ".config/alpha-radar/okx-live-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    assert config["wallet_address"] == "0x1a642f0e3c3af545e7acbd38b07251b3990914f1"
    for field in ("api_key_dpapi_path", "secret_key_dpapi_path", "passphrase_dpapi_path", "wallet_private_key_dpapi_path"):
        path = Path(config[field])
        assert path.is_relative_to(home)
        content = path.read_text(encoding="utf-8-sig")
        assert content and "offline-fixture" not in content and "01" * 32 not in content
    assert not list((home / ".config/alpha-radar/gmgn-live").glob("*/ledger.sqlite3"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI and ACL integration")
def test_setup_can_update_existing_operator_config_without_null_backup_path(tmp_path):
    source = Path(__file__).with_name("setup_okx_live.ps1").read_text(encoding="utf-8")
    checkout = tmp_path / "checkout/work/radar"
    checkout.mkdir(parents=True)
    script = checkout / "setup_okx_live.ps1"
    script.write_text(
        source.replace("[Environment]::GetFolderPath('UserProfile')", "$env:BOOTSTRAP_HOME"),
        encoding="utf-8",
    )
    home = tmp_path / "synthetic-home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKX_", "BSC_", "GMGN_"))}
    env.update(BOOTSTRAP_HOME=str(home), BOOTSTRAP_SOURCE=str(script), BOOTSTRAP_PYTHON=sys.executable)
    code = r'''
$ErrorActionPreference = 'Stop'
function Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    $value = 'offline-fixture-credential'
    if ($Prompt -like 'BSC_PRIVATE_KEY*') { $value = '0x' + ('01' * 32) }
    ConvertTo-SecureString $value -AsPlainText -Force
}
& $env:BOOTSTRAP_SOURCE -PythonPath $env:BOOTSTRAP_PYTHON
& $env:BOOTSTRAP_SOURCE -PythonPath $env:BOOTSTRAP_PYTHON
'''
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-EncodedCommand",
                             base64.b64encode(code.encode("utf-16-le")).decode()],
                            env=env, capture_output=True, timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, stderr[-1800:]
    assert "路径的形式不合法" not in stderr


def test_setup_can_derive_wallet_with_node_ethers_when_python_eth_account_is_missing():
    source = Path(__file__).with_name("setup_okx_live.ps1").read_text(encoding="utf-8")
    assert "function Get-OkxDerivedAddressWithNode" in source
    assert "okx-dex-executor" in source
    assert "node_modules\\ethers" in source
    assert "import('ethers').then(({ Wallet }) =>" in source
    assert "$derivedAddress = Get-OkxDerivedAddress -PythonExe $pythonExe" in source
    assert "Get-OkxDerivedAddressWithNode -PrivateKey $secrets['BSC_PRIVATE_KEY']" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows child process environment")
def test_dag_options_do_not_enable_live_or_inherit_fee_policy():
    env = dict(os.environ)
    env["BOOTSTRAP_SOURCE"] = str(Path(__file__).with_name("start_okx_live.ps1"))
    env["OKX_DAG_ENABLED"] = "1"
    env["OKX_DAG_TRIM_RECIPIENT"] = "untrusted-inherited-value"
    code = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:BOOTSTRAP_SOURCE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_error' }
foreach ($s in $ast.EndBlock.Statements) {
    if ($s -is [Management.Automation.Language.FunctionDefinitionAst]) {
        . ([ScriptBlock]::Create($s.Extent.Text))
    }
}
$args = @{
    PythonExe='fixture-python'; Worker='fixture-worker'; Root='C:\'; WorkerArguments=@('--check');
    Wallet=('0x' + ('12' * 20)); EnableLive=$false;
    Credentials=@{OKX_API_KEY='fixture'; OKX_SECRET_KEY='fixture'; OKX_PASSPHRASE='fixture'; BSC_PRIVATE_KEY='fixture'}
}
$p = New-OkxChildProcessInfo @args
if ($p.EnvironmentVariables['OKX_DAG_ENABLED'] -ne '0') { throw 'dag_enabled_by_inheritance' }
if ($p.EnvironmentVariables.ContainsKey('OKX_DAG_TRIM_RECIPIENT')) { throw 'fee_policy_inherited' }
$args.EnableDagRoutes=$true
$p = New-OkxChildProcessInfo @args
if ($p.EnvironmentVariables['OKX_DAG_ENABLED'] -ne '1' -or $p.EnvironmentVariables['OKX_LIVE_ENABLED'] -ne '0') {
    throw 'dag_optin_armed_live'
}
$args.DagTrimRecipient='invalid'
$args.DagMaxTrimPerMille=100
$rejected=$false
try { $null=New-OkxChildProcessInfo @args } catch { $rejected=$true }
if (-not $rejected) { throw 'invalid_fee_recipient_accepted' }
Write-Output 'dag_environment_ok'
'''
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-EncodedCommand",
                             base64.b64encode(code.encode("utf-16-le")).decode()], env=env,
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-1200:]
    assert b"dag_environment_ok" in result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell AST test')
def test_strategy_meme_requires_roundtrip_and_only_forwards_runner_flag():
    """Evaluate only parsed guards and argv assignment, never the launcher body."""
    source = Path(__file__).with_name('start_okx_live.ps1')
    env = {'SystemRoot': os.environ['SystemRoot'], 'BOOTSTRAP_SOURCE': str(source)}
    code = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:BOOTSTRAP_SOURCE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_error' }
$parameter = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'StrategyMeme' })
if ($parameter.Count -ne 1 -or $parameter[0].StaticType -ne [Management.Automation.SwitchParameter]) { throw 'missing_strategy_switch' }
$sessionParameter = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'NewRoundTripSession' })
if ($sessionParameter.Count -ne 1 -or $sessionParameter[0].StaticType -ne [Management.Automation.SwitchParameter]) { throw 'missing_session_switch' }
$signerCheckParameter = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'SignerCheck' })
if ($signerCheckParameter.Count -ne 1 -or $signerCheckParameter[0].StaticType -ne [Management.Automation.SwitchParameter]) { throw 'missing_signer_check_switch' }
$preflightOnlyParameter = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'PreflightOnly' })
if ($preflightOnlyParameter.Count -ne 1 -or $preflightOnlyParameter[0].StaticType -ne [Management.Automation.SwitchParameter]) { throw 'missing_preflight_only_switch' }
$statements = @($ast.EndBlock.Statements)
$guard = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -match '\$StrategyMeme' -and $_.Extent.Text -match '\bthrow\b'
})
$sessionGuard = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -match '\$NewRoundTripSession' -and $_.Extent.Text -match '\bthrow\b'
})
$signerCheckGuard = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -match '^\$SignerCheck\b' -and $_.Extent.Text -match '\bthrow\b'
})
$forward = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$StrategyMeme' -and $_.Extent.Text -match '\$workerArguments'
})
$sessionForward = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$NewRoundTripSession' -and $_.Extent.Text -match '--session-id'
})
$signerCheckForward = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$SignerCheck' -and $_.Extent.Text -match '--signer-check'
})
$preflightOnlyForward = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$PreflightOnly' -and $_.Extent.Text -match '--preflight-only'
})
if ($guard.Count -ne 1 -or $sessionGuard.Count -ne 1 -or $signerCheckGuard.Count -ne 1 -or $forward.Count -ne 1 -or
    $sessionForward.Count -ne 1 -or $signerCheckForward.Count -ne 1 -or $preflightOnlyForward.Count -ne 1) {
    throw 'missing_mode_guard_or_forward'
}
$configRead = @($ast.FindAll({param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-Content'}, $true))[0]
if ($guard[0].Extent.StartOffset -ge $configRead.Extent.StartOffset) { throw 'late_mode_guard' }
if ($sessionGuard[0].Extent.StartOffset -ge $configRead.Extent.StartOffset) { throw 'late_session_guard' }
foreach ($meme in @($false, $true)) {
    foreach ($liveFlag in @($false, $true)) {
        foreach ($roundFlag in @($false, $true)) {
            $StrategyMeme = $meme; $Live = $liveFlag; $RoundTrip = $roundFlag
            $rejected = $false
            try { . ([ScriptBlock]::Create($guard[0].Extent.Text)) } catch { $rejected = $true }
            if ($rejected -ne ($meme -and (-not $liveFlag -or -not $roundFlag))) { throw 'guard_mismatch' }
        }
    }
    $StrategyMeme = $meme
    $workerArguments = @('--live')
    . ([ScriptBlock]::Create($forward[0].Extent.Text))
    $expected = if ($meme) { '--live,--strategy-meme' } else { '--live' }
    if (($workerArguments -join ',') -ne $expected) { throw 'wrong_runner_arguments' }
}
$roundTripGuard = @($statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -match '^\$RoundTrip\b' -and $_.Extent.Text -match 'requires -Live'
})[0]
$RoundTrip = $true; $Live = $true; $Check = $false; $Verify = $false; $Once = $false
$SignerCheck = $true; $StrategyMeme = $false; $NewRoundTripSession = $false
$rejected = $false
try { . ([ScriptBlock]::Create($signerCheckGuard[0].Extent.Text)) } catch { $rejected = $true }
if ($rejected) { throw 'live_signer_check_rejected_by_signer_guard' }
try { . ([ScriptBlock]::Create($roundTripGuard.Extent.Text)) } catch { $rejected = $true }
if ($rejected) { throw 'signer_check_rejected_by_roundtrip_guard' }
$RoundTrip = $true; $Live = $false; $Check = $false; $Verify = $false; $Once = $false
$SignerCheck = $true; $StrategyMeme = $false; $NewRoundTripSession = $false
$rejected = $false
try { . ([ScriptBlock]::Create($roundTripGuard.Extent.Text)) } catch { $rejected = $true }
if ($rejected) { throw 'signer_check_rejected_by_roundtrip_guard' }
$Live = $true; $RoundTrip = $true; $NewRoundTripSession = $true
$workerArguments = @('--live')
. ([ScriptBlock]::Create($sessionForward[0].Extent.Text))
if ($workerArguments.Count -ne 3 -or $workerArguments[1] -ne '--session-id' -or
    $workerArguments[2] -notmatch '^rt-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$') { throw 'wrong_session_arguments' }
$SignerCheck = $true
$workerArguments = @('--check')
. ([ScriptBlock]::Create($signerCheckForward[0].Extent.Text))
if (($workerArguments -join ',') -ne '--signer-check') { throw 'wrong_signer_check_arguments' }
$PreflightOnly = $true
$workerArguments = @('--live')
. ([ScriptBlock]::Create($preflightOnlyForward[0].Extent.Text))
if (($workerArguments -join ',') -ne '--live,--preflight-only') { throw 'wrong_preflight_only_arguments' }
Write-Output 'strategy_mode_ok'
'''
    executable = Path(env['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(code.encode('utf-16-le')).decode()], env=env,
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')[-1200:]
    assert b'strategy_mode_ok' in result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell AST test')
def test_okx_dex_sdk_launcher_collects_project_id_and_starts_node_executor():
    source = Path(__file__).with_name('start_okx_dex_sdk_live.ps1')
    text = source.read_text(encoding='utf-8')
    assert '[string]$ProjectId' in text
    assert '[switch]$RoundTrip' in text
    assert '[switch]$RemotePreflight' in text
    assert '[switch]$Daemon' in text
    assert '[switch]$Once' in text
    assert '[string]$Token' in text
    assert '[string]$Pool' in text
    assert '[string]$AmountUsd' in text
    assert '[string]$SlippagePercent' in text
    assert "OKX_PROJECT_ID" in text
    assert "OKX_API_PASSPHRASE" in text
    assert "okx_dex_executor.mjs" in text
    assert "okx_dex_live_daemon.mjs" in text
    assert "Select-Object -First 1" in text
    assert "$info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '0'" in text
    assert "$info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '0'" in text
    assert "$info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '1'" in text
    assert "$info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '1'" in text
    assert "if ($chainName -eq 'robinhood') { '5' } else { '1' }" in text
    assert "--round-trip" in text
    assert "--remote-preflight" in text
    assert "--interval-seconds" in text
    assert "[int]$IntervalSeconds = 1" in text
    assert "$IntervalSeconds -lt 1" in text
    assert "--once" in text
    assert "--token" in text
    assert "--amount-usd" in text


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell AST test')
def test_okx_dex_sdk_launcher_reads_credentials_from_dotenv(tmp_path):
    source = Path(__file__).with_name('start_okx_dex_sdk_live.ps1')
    dotenv = tmp_path / '.env'
    dotenv.write_text(
        'OKX_API_KEY=env-api\n'
        'OKX_SECRET_KEY=env-secret\n'
        'OKX_PASSPHRASE=env-pass\n'
        'OKX_PROJECT_ID=env-project\n',
        encoding='utf-8',
    )
    env = {
        'SystemRoot': os.environ['SystemRoot'],
        'BOOTSTRAP_SOURCE': str(source),
        'BOOTSTRAP_DOTENV': str(dotenv),
    }
    code = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:BOOTSTRAP_SOURCE, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_error' }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [Management.Automation.Language.FunctionDefinitionAst]) {
        . ([ScriptBlock]::Create($statement.Extent.Text))
    }
}
if (-not (Get-Command Read-OkxDotEnv -ErrorAction SilentlyContinue)) { throw 'missing_dotenv_reader' }
$values = Read-OkxDotEnv -Path $env:BOOTSTRAP_DOTENV
if ($values['OKX_API_KEY'] -ne 'env-api') { throw 'api_key_not_loaded' }
if ($values['OKX_SECRET_KEY'] -ne 'env-secret') { throw 'secret_key_not_loaded' }
if ($values['OKX_PASSPHRASE'] -ne 'env-pass') { throw 'passphrase_not_loaded' }
if ($values['OKX_PROJECT_ID'] -ne 'env-project') { throw 'project_id_not_loaded' }
Write-Output 'dotenv_ok'
'''
    executable = Path(env['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(code.encode('utf-16-le')).decode()], env=env,
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')[-1200:]
    assert b'dotenv_ok' in result.stdout
