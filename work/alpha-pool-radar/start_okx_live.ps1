[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Verify,
    [switch]$Once,
    [switch]$Live,
    [switch]$RoundTrip,
    [switch]$StrategyMeme,
    [switch]$NewRoundTripSession,
    [switch]$SignerCheck,
    [switch]$PreflightOnly,
    [string]$ExpectedWallet = '',
    [switch]$EnableDagRoutes,
    [string]$DagTrimRecipient = '',
    [ValidateRange(0, 100)][int]$DagMaxTrimPerMille = 0,
    [ValidateRange(3, 60)][int]$Interval = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Resolve the security module from this engine, not an inherited PS7 path.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Global -ErrorAction Stop

function Read-OkxProtectedSecret([string]$Path) {
    $secure = $null
    $pointer = [IntPtr]::Zero
    try {
        $secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($Path))
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plain)) { throw 'empty_secret' }
        return $plain
    }
    catch { throw 'Credential unavailable. Run setup under the same Windows user on this machine.' }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($null -ne $secure) { $secure.Dispose() }
    }
}

function New-OkxChildProcessInfo {
    param(
        [string]$PythonExe, [string]$Worker, [string]$Root,
        [string[]]$WorkerArguments, [string]$Wallet, [hashtable]$Credentials,
        [bool]$EnableLive, [ValidateRange(1, 10)][int]$Slippage = 5,
        [bool]$EnableDagRoutes = $false, [string]$DagTrimRecipient = '',
        [ValidateRange(0, 100)][int]$DagMaxTrimPerMille = 0,
        [string]$RpcUrl = 'https://bsc-dataseed.binance.org/'
    )
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $PythonExe
    $info.Arguments = ((@('-B', '-u', ('"' + $Worker + '"')) + $WorkerArguments) -join ' ')
    $info.WorkingDirectory = $Root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -like 'OKX_*' -or $name -like 'BSC_*' -or $name -like 'GMGN_*' -or
            $name -like 'PYTHON*' -or $name -in @('LIVE_TRADING_ENABLED', 'ARMED', 'HOME')) {
            $info.EnvironmentVariables.Remove($name)
        }
    }
    $info.EnvironmentVariables['USERPROFILE'] = [Environment]::GetFolderPath('UserProfile')
    $info.EnvironmentVariables['BSC_EXECUTION_CAPITAL_USD'] = '100'
    $info.EnvironmentVariables['BSC_WALLET_ADDRESS'] = $Wallet
    $info.EnvironmentVariables['BSC_RPC_URL'] = $RpcUrl
    $info.EnvironmentVariables['OKX_SLIPPAGE_PERCENT'] = $Slippage.ToString()
    $info.EnvironmentVariables['OKX_DAG_ENABLED'] = $(if ($EnableDagRoutes) { '1' } else { '0' })
    if ($DagMaxTrimPerMille -gt 0 -or $DagTrimRecipient) {
        if (-not $EnableDagRoutes -or $DagMaxTrimPerMille -le 0 -or
            $DagTrimRecipient -notmatch '^0x[0-9a-fA-F]{40}$' -or
            $DagTrimRecipient -eq ('0x' + ('0' * 40))) { throw 'Invalid explicit DAG trim policy.' }
        $info.EnvironmentVariables['OKX_DAG_TRIM_RECIPIENT'] = $DagTrimRecipient.ToLowerInvariant()
        $info.EnvironmentVariables['OKX_DAG_MAX_TRIM_PER_MILLE'] = $DagMaxTrimPerMille.ToString()
    }
    $info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '0'
    $info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '0'
    foreach ($name in @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'BSC_PRIVATE_KEY')) {
        if ([string]::IsNullOrWhiteSpace($Credentials[$name])) { throw 'All four protected credentials are required.' }
        $info.EnvironmentVariables[$name] = $Credentials[$name]
    }
    if ($EnableLive) {
        $info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '1'
        $info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '1'
    }
    return $info
}

function Protect-OkxOutput([string]$Line, [hashtable]$Credentials) {
    foreach ($value in $Credentials.Values) {
        if ($value) { $Line = $Line.Replace([string]$value, '[REDACTED]') }
    }
    # Also cover normalized/case-changed private keys, with or without the 0x prefix.
    $key = ([string]$Credentials['BSC_PRIVATE_KEY']).Trim() -replace '^0x', ''
    if ($key) { $Line = [regex]::Replace($Line, '(?i)(?:0x)?' + [regex]::Escape($key), '[REDACTED]') }
    return $Line
}

if ($SignerCheck -and ($Check -or $Verify -or $Once -or -not $RoundTrip)) {
    throw '-SignerCheck requires -RoundTrip and cannot combine with -Check, -Verify, or -Once.'
}
if ($PreflightOnly -and (-not $Live -or -not $RoundTrip -or $Check -or $Verify -or $Once -or $SignerCheck)) {
    throw '-PreflightOnly requires -Live -RoundTrip and cannot combine with -Check, -Verify, -Once, or -SignerCheck.'
}
if ($Check -and ($Live -or $Once)) { throw '-Check cannot be combined with -Live or -Once.' }
if ($StrategyMeme -and (-not $Live -or -not $RoundTrip)) {
    throw '-StrategyMeme requires -Live -RoundTrip.'
}
if ($NewRoundTripSession -and (-not $Live -or -not $RoundTrip)) {
    throw '-NewRoundTripSession requires -Live -RoundTrip.'
}
if ($RoundTrip -and -not $SignerCheck -and (-not $Live -or $Check -or $Verify -or $Once)) {
    throw '-RoundTrip requires -Live and cannot combine with -Check, -Verify or -Once.'
}
if ($RoundTrip -and -not $SignerCheck -and ($ExpectedWallet -notmatch '^0x[0-9a-fA-F]{40}$' -or
    $ExpectedWallet -eq ('0x' + ('0' * 40)))) { throw '-RoundTrip requires the explicitly confirmed -ExpectedWallet.' }
if ($Verify -and ($Live -or $Check -or $Once)) { throw '-Verify cannot be combined with -Live, -Check, or -Once.' }
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This operator launcher requires Windows.' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$worker = Join-Path $PSScriptRoot 'alpha_okx_live_worker.py'
if ($RoundTrip) { $worker = Join-Path $PSScriptRoot 'alpha_okx_roundtrip_live.py' }
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw 'alpha_okx_live_worker.py is missing. Complete the worker implementation before launching.'
}
$operatorHome = [Environment]::GetFolderPath('UserProfile')
$configRoot = Join-Path $operatorHome '.config\alpha-radar'
$configPath = Join-Path $configRoot 'okx-live-config.json'
$secretRoot = Join-Path $configRoot 'okx-live-secrets'
$stateRoot = Join-Path $configRoot 'gmgn-live'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'OKX operator config is missing. Run setup_okx_live.ps1 first.'
}
try {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.schema_version -ne 1 -or $config.wallet_address -notmatch '\A0x[0-9a-fA-F]{40}\z' -or
        $config.wallet_address -match '\A0x0{40}\z') { throw 'invalid_config' }
    if ($ExpectedWallet -and $config.wallet_address -ine $ExpectedWallet) { throw 'confirmed_wallet_changed' }
    if ([IO.Path]::GetFullPath($config.state_root).TrimEnd('\') -ine $stateRoot.TrimEnd('\')) {
        throw 'alternate_state_root'
    }
    $rpcUrl = [string]$config.bsc_rpc_url
    $rpcUri = $null
    if (-not [Uri]::TryCreate($rpcUrl, [UriKind]::Absolute, [ref]$rpcUri) -or
        $rpcUri.Scheme -notin @('http', 'https') -or -not $rpcUri.Host -or
        $rpcUri.UserInfo -or $rpcUri.Query -or $rpcUri.Fragment -or $rpcUrl -match '\s') { throw 'invalid_rpc_url' }
    if ([string]$config.slippage_percent -notmatch '\A(10|[1-9])\z') { throw 'invalid_slippage' }
    $pythonExe = (Get-Command $config.python_path -CommandType Application -ErrorAction Stop).Source
    $secretPaths = @{
        OKX_API_KEY = $config.api_key_dpapi_path
        OKX_SECRET_KEY = $config.secret_key_dpapi_path
        OKX_PASSPHRASE = $config.passphrase_dpapi_path
        BSC_PRIVATE_KEY = $config.wallet_private_key_dpapi_path
    }
    foreach ($path in $secretPaths.Values) {
        if (-not [IO.Path]::IsPathRooted($path) -or
            [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($path)) -ine $secretRoot) { throw 'unexpected_secret_path' }
    }
}
catch { throw 'Invalid local OKX config. Run setup again; alternate account state paths are not supported.' }
if ([IO.Path]::GetFileName($pythonExe) -ieq 'py.exe') {
    $resolvedPython = & $pythonExe -3 -I -B -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or @($resolvedPython).Count -ne 1 -or
        -not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw 'Python 3 could not be resolved.' }
    $pythonExe = $resolvedPython
}
$workerArguments = @('--check')
if ($Live) {
    $workerArguments = @('--live', '--interval', $Interval.ToString())
    if ($Once) { $workerArguments += '--once' }
}
elseif ($Once) { $workerArguments = @('--once') }
elseif ($Verify) { $workerArguments = @('--verify') }
if ($RoundTrip) { $workerArguments = @('--live') }
if ($StrategyMeme) { $workerArguments += '--strategy-meme' }
if ($NewRoundTripSession) {
    $sessionId = 'rt-' + ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 8))
    $workerArguments += @('--session-id', $sessionId)
}
if ($SignerCheck) { $workerArguments = @('--signer-check') }
if ($PreflightOnly) { $workerArguments += '--preflight-only' }

$credentials = @{}
$childInfo = $null
$child = $null
$childStarted = $false
$lock = $null
try {
    if ($Live) {
        [void][IO.Directory]::CreateDirectory($stateRoot)
        try {
            $lock = [IO.File]::Open((Join-Path $stateRoot 'operator-launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        }
        catch { throw 'An OKX or GMGN account launcher is already running, or its state directory is unavailable.' }
    }
    # Check, read-only verify, and disabled once load credentials with both gates still zero.
    foreach ($name in $secretPaths.Keys) { $credentials[$name] = Read-OkxProtectedSecret $secretPaths[$name] }
    $credentials['BSC_PRIVATE_KEY'] = $credentials['BSC_PRIVATE_KEY'].Trim()
    $credentials['BSC_RPC_URL'] = $rpcUrl
    $effectiveSlippage = [int]$config.slippage_percent
    if ($RoundTrip) { $effectiveSlippage = 1 }
    $childInfo = New-OkxChildProcessInfo -PythonExe $pythonExe -Worker $worker -Root $root `
        -WorkerArguments $workerArguments -Wallet $config.wallet_address.ToLowerInvariant() `
        -Credentials $credentials -EnableLive $Live.IsPresent -Slippage $effectiveSlippage -RpcUrl $rpcUrl `
        -EnableDagRoutes $EnableDagRoutes.IsPresent -DagTrimRecipient $DagTrimRecipient -DagMaxTrimPerMille $DagMaxTrimPerMille
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $childInfo
    [void]$child.Start()
    $childStarted = $true
    foreach ($name in $secretPaths.Keys) { $childInfo.EnvironmentVariables.Remove($name) }
    $stdoutRead = $child.StandardOutput.ReadLineAsync()
    $stderrRead = $child.StandardError.ReadLineAsync()
    while ($null -ne $stdoutRead -or $null -ne $stderrRead) {
        if ($null -ne $stdoutRead -and $stdoutRead.IsCompleted) {
            $line = $stdoutRead.GetAwaiter().GetResult()
            $stdoutRead = $null
            if ($null -ne $line) {
                Write-Output (Protect-OkxOutput $line $credentials)
                $stdoutRead = $child.StandardOutput.ReadLineAsync()
            }
        }
        if ($null -ne $stderrRead -and $stderrRead.IsCompleted) {
            $line = $stderrRead.GetAwaiter().GetResult()
            $stderrRead = $null
            if ($null -ne $line) {
                [Console]::Error.WriteLine((Protect-OkxOutput $line $credentials))
                $stderrRead = $child.StandardError.ReadLineAsync()
            }
        }
        if ($null -ne $stdoutRead -or $null -ne $stderrRead) { Start-Sleep -Milliseconds 50 }
    }
    while (-not $child.WaitForExit(250)) { }
    $exitCode = $child.ExitCode
}
finally {
    if ($null -ne $childInfo) {
        foreach ($name in $secretPaths.Keys) { $childInfo.EnvironmentVariables.Remove($name) }
    }
    $credentials.Clear()
    if ($null -ne $child) {
        try {
            if ($childStarted -and -not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
        }
        finally { $child.Dispose() }
    }
    if ($null -ne $lock) { $lock.Dispose() }
}
exit $exitCode
