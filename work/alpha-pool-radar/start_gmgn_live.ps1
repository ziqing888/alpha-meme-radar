[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Once,
    [switch]$Live,
    [string]$WalletAddress,
    [string]$RecoverOrder,
    [string]$IntentKey,
    [ValidateRange(3, 60)][int]$Interval = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-GmgnProtectedSecret([string]$Path) {
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

function New-GmgnChildProcessInfo {
    param(
        [string]$PythonExe, [string]$Worker, [string]$Root,
        [string[]]$WorkerArguments, [string]$Wallet,
        [string]$ApiKey, [string]$SigningPem, [bool]$EnableLive,
        [ValidateRange(1, 10)][int]$Slippage = 5,
        [string]$RpcUrl = 'https://bsc-dataseed.binance.org/'
    )
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $PythonExe
    $prefix = @('-B', '-u')
    if ([IO.Path]::GetFileName($PythonExe) -ieq 'py.exe') { $prefix = @('-3') + $prefix }
    $info.Arguments = (($prefix + @('"' + $Worker + '"') + $WorkerArguments) -join ' ')
    $info.WorkingDirectory = $Root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    # All GMGN variables are supplied here, so inherited flags cannot arm a default launch.
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -like 'GMGN_*' -or $name -in @('BSC_PRIVATE_KEY', 'LIVE_TRADING_ENABLED', 'ARMED')) {
            $info.EnvironmentVariables.Remove($name)
        }
    }
    $info.EnvironmentVariables['BSC_EXECUTION_CAPITAL_USD'] = '100'
    $info.EnvironmentVariables['BSC_RPC_URL'] = $RpcUrl
    $info.EnvironmentVariables['GMGN_SLIPPAGE_PERCENT'] = $Slippage.ToString()
    $info.EnvironmentVariables['USERPROFILE'] = [Environment]::GetFolderPath('UserProfile')
    if ($Wallet) { $info.EnvironmentVariables['GMGN_WALLET_ADDRESS'] = $Wallet }
    $info.EnvironmentVariables['GMGN_ALLOW_AUTOMATED_TRADES'] = '0'
    $info.EnvironmentVariables['GMGN_LIVE_ENABLED'] = '0'
    if ($ApiKey -and $SigningPem) {
        $info.EnvironmentVariables['GMGN_API_KEY'] = $ApiKey
        $info.EnvironmentVariables['GMGN_PRIVATE_KEY'] = $SigningPem
    }
    if ($EnableLive) {
        $info.EnvironmentVariables['GMGN_ALLOW_AUTOMATED_TRADES'] = '1'
        $info.EnvironmentVariables['GMGN_LIVE_ENABLED'] = '1'
    }
    return $info
}

$recoveryRequested = $PSBoundParameters.ContainsKey('RecoverOrder') -or $PSBoundParameters.ContainsKey('IntentKey')
if ($recoveryRequested) {
    if (-not $RecoverOrder -or -not $IntentKey) { throw 'Recovery requires both -RecoverOrder and -IntentKey.' }
    if ($Live -or $Check -or $Once -or $WalletAddress) {
        throw 'Recovery cannot be combined with -Live, -Check, -Once, or -WalletAddress.'
    }
    if ($RecoverOrder -cnotmatch '\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\z' -or $IntentKey -cnotmatch '\A[a-f0-9]{64}\z') {
        throw 'Recovery requires a valid provider order ID and the 64-character lowercase intent key from the ledger.'
    }
}
if ($Check -and ($Live -or $Once)) { throw '-Check cannot be combined with -Live or -Once.' }
if ($WalletAddress -and ($Live -or $Once)) { throw '-WalletAddress is only supported for offline checks.' }
if ($WalletAddress -and ($WalletAddress -notmatch '^0x[0-9a-fA-F]{40}$' -or $WalletAddress -match '^0x0{40}$')) {
    throw 'A valid nonzero public address is required for the offline check.'
}
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This operator launcher requires Windows.' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$worker = Join-Path $PSScriptRoot 'alpha_gmgn_live_worker.py'
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw 'alpha_gmgn_live_worker.py is missing. Complete the worker implementation before launching.'
}
$operatorHome = [Environment]::GetFolderPath('UserProfile')
$configRoot = Join-Path $operatorHome '.config\alpha-radar'
$configPath = Join-Path $configRoot 'gmgn-live-config.json'
$stateRoot = Join-Path $configRoot 'gmgn-live'
$secretRoot = Join-Path $configRoot 'gmgn-live-secrets'
$wallet = ''
$config = $null
$pythonExe = $null
$slippage = 5
$rpcUrl = 'https://bsc-dataseed.binance.org/'
if (Test-Path -LiteralPath $configPath) {
    try {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($config.schema_version -ne 1 -or $config.wallet_address -notmatch '^0x[0-9a-fA-F]{40}$' -or $config.wallet_address -match '^0x0{40}$') {
            throw 'invalid_config'
        }
        if ([IO.Path]::GetFullPath($config.state_root).TrimEnd('\') -ine $stateRoot.TrimEnd('\')) {
            throw 'alternate_state_root'
        }
        $wallet = $config.wallet_address
        if ($WalletAddress -and $WalletAddress -ine $wallet) { throw 'configured_wallet_mismatch' }
        if ($config.PSObject.Properties.Name -contains 'bsc_rpc_url') {
            $rpcUri = $null
            if (-not [Uri]::TryCreate([string]$config.bsc_rpc_url, [UriKind]::Absolute, [ref]$rpcUri) -or
                $rpcUri.Scheme -notin @('http', 'https') -or -not $rpcUri.Host -or
                $rpcUri.UserInfo -or $rpcUri.Query -or $rpcUri.Fragment -or $config.bsc_rpc_url -match '\s') {
                throw 'invalid_public_rpc_url'
            }
            $rpcUrl = [string]$config.bsc_rpc_url
        }
        if ($config.PSObject.Properties.Name -contains 'slippage_percent') {
            $slippageText = [string]$config.slippage_percent
            if ($slippageText -notmatch '^(10|[1-9])$') { throw 'invalid_slippage' }
            $slippage = [int]$slippageText
        }
        $pythonExe = (Get-Command $config.python_path -CommandType Application -ErrorAction Stop).Source
        foreach ($path in @($config.api_key_dpapi_path, $config.api_signing_key_dpapi_path)) {
            if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($path)) -ine $secretRoot) {
                throw 'unexpected_secret_path'
            }
        }
    }
    catch { throw 'Invalid local GMGN config. Run setup again; alternate account state paths are not supported.' }
}
elseif ($Live -or $Once -or $recoveryRequested) {
    throw 'GMGN operator config is missing. Run setup_gmgn_live.ps1 first.'
}
if ($WalletAddress) { $wallet = $WalletAddress.ToLowerInvariant() }
if (-not $wallet) { throw 'For an offline check supply -WalletAddress with a public BSC address, or run operator setup first.' }
if ($null -eq $config) {
    [ordered]@{
        configured = $false
        offline = $true
        check_scope = 'public_address_structure_only'
        wallet = $wallet
        api_credentials_loaded = $false
        runtime_checked = $false
        preflight_completed = $false
        live_started = $false
        provider_permissions_verified = $false
    } | ConvertTo-Json -Compress
    exit 0
}
if (-not $pythonExe) { $pythonExe = (Get-Command py.exe -CommandType Application).Source }
if ([IO.Path]::GetFileName($pythonExe) -ieq 'py.exe') {
    # Resolve the interpreter before loading secrets so the supervised child is Python itself.
    $resolvedPython = & $pythonExe -3 -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or @($resolvedPython).Count -ne 1 -or
        -not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
        throw 'Python 3 could not be resolved through py.exe.'
    }
    $pythonExe = $resolvedPython
}

$workerArguments = @('--check')
if ($Live) {
    $workerArguments = @('--live', '--interval', $Interval.ToString())
    if ($Once) { $workerArguments += '--once' }
}
elseif ($Once) { $workerArguments = @('--once') }
elseif ($recoveryRequested) { $workerArguments = @('--attach-order', $RecoverOrder, '--intent-key', $IntentKey) }

$apiKey = $null
$pemText = $null
$childInfo = $null
$child = $null
$childStarted = $false
$lock = $null
try {
    if ($Live -or $recoveryRequested) {
        [void][IO.Directory]::CreateDirectory($stateRoot)
        try {
            $lock = [IO.File]::Open((Join-Path $stateRoot 'operator-launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        }
        catch { throw 'The account launcher is already running, or its state directory is unavailable.' }
    }
    $apiKey = Read-GmgnProtectedSecret $config.api_key_dpapi_path
    $pemText = Read-GmgnProtectedSecret $config.api_signing_key_dpapi_path
    $childInfo = New-GmgnChildProcessInfo -PythonExe $pythonExe -Worker $worker -Root $root `
        -WorkerArguments $workerArguments -Wallet $wallet -ApiKey $apiKey -SigningPem $pemText `
        -EnableLive $Live.IsPresent -Slippage $slippage -RpcUrl $rpcUrl
    $apiKey = $null
    $pemText = $null
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $childInfo
    [void]$child.Start()
    $childStarted = $true
    $childInfo.EnvironmentVariables.Remove('GMGN_API_KEY')
    $childInfo.EnvironmentVariables.Remove('GMGN_PRIVATE_KEY')
    # Drain both pipes concurrently to keep long-running status output responsive.
    $stdoutRead = $child.StandardOutput.ReadLineAsync()
    $stderrRead = $child.StandardError.ReadLineAsync()
    while ($null -ne $stdoutRead -or $null -ne $stderrRead) {
        if ($null -ne $stdoutRead -and $stdoutRead.IsCompleted) {
            $line = $stdoutRead.GetAwaiter().GetResult()
            $stdoutRead = $null
            if ($null -ne $line) {
                Write-Output $line
                $stdoutRead = $child.StandardOutput.ReadLineAsync()
            }
        }
        if ($null -ne $stderrRead -and $stderrRead.IsCompleted) {
            $line = $stderrRead.GetAwaiter().GetResult()
            $stderrRead = $null
            if ($null -ne $line) {
                [Console]::Error.WriteLine($line)
                $stderrRead = $child.StandardError.ReadLineAsync()
            }
        }
        if ($null -ne $stdoutRead -or $null -ne $stderrRead) { Start-Sleep -Milliseconds 50 }
    }
    while (-not $child.WaitForExit(250)) { }
    $exitCode = $child.ExitCode
}
finally {
    $apiKey = $null
    $pemText = $null
    if ($null -ne $childInfo) {
        $childInfo.EnvironmentVariables.Remove('GMGN_API_KEY')
        $childInfo.EnvironmentVariables.Remove('GMGN_PRIVATE_KEY')
    }
    if ($null -ne $child) {
        try {
            if ($childStarted -and -not $child.HasExited) {
                $child.Kill()
                $child.WaitForExit()
            }
        }
        finally { $child.Dispose() }
    }
    if ($null -ne $lock) { $lock.Dispose() }
}
exit $exitCode
