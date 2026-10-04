[CmdletBinding()]
param(
    [switch]$Preflight,
    [switch]$RemotePreflight,
    [switch]$StrategyMeme,
    [switch]$Daemon,
    [switch]$Once,
    [switch]$ExitOnly,
    [switch]$Live,
    [switch]$RoundTrip,
    [ValidateSet('bsc', 'robinhood')]
    [string]$Chain = 'bsc',
    [string]$ExpectedWallet = '',
    [string]$ProjectId = '',
    [string]$Token = '',
    [string]$Pool = '',
    [string]$Symbol = 'MEME',
    [string]$AmountNativeAtomic = '',
    [string]$AmountUsd = '',
    [string]$DailyLossOverrideDate = '',
    [string]$SlippagePercent = '',
    [int]$IntervalSeconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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
    catch { throw 'Credential unavailable. Run setup_okx_live.ps1 under the same Windows user.' }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($null -ne $secure) { $secure.Dispose() }
    }
}

function Protect-OkxOutput([string]$Line, [hashtable]$Credentials) {
    foreach ($value in $Credentials.Values) {
        if ($value) { $Line = $Line.Replace([string]$value, '[REDACTED]') }
    }
    $key = ([string]$Credentials['BSC_PRIVATE_KEY']).Trim() -replace '^0x', ''
    if ($key) { $Line = [regex]::Replace($Line, '(?i)(?:0x)?' + [regex]::Escape($key), '[REDACTED]') }
    return $Line
}

function Read-OkxDotEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { continue }
        $name = $matches[1]
        $value = $matches[2]
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($name -in @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'OKX_PROJECT_ID') -and
            -not [string]::IsNullOrWhiteSpace($value)) {
            $values[$name] = $value.Trim()
        }
    }
    return $values
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$dotEnvValues = Read-OkxDotEnv -Path (Join-Path $repoRoot '.env')
if ([string]::IsNullOrWhiteSpace($ProjectId) -and $dotEnvValues.ContainsKey('OKX_PROJECT_ID')) {
    $ProjectId = $dotEnvValues['OKX_PROJECT_ID']
}

if (-not $Preflight -and -not $StrategyMeme -and -not $Daemon -and [string]::IsNullOrWhiteSpace($Token)) {
    throw 'Choose -Preflight, -StrategyMeme, -Daemon, or -Token.'
}
if ($Preflight -and $StrategyMeme) { throw '-Preflight cannot combine with -StrategyMeme.' }
if ($Preflight -and $Daemon) { throw '-Preflight cannot combine with -Daemon.' }
if ($Preflight -and $Token) { throw '-Preflight cannot combine with -Token.' }
if ($StrategyMeme -and $Token) { throw '-StrategyMeme cannot combine with -Token.' }
if ($Daemon -and $Token) { throw '-Daemon cannot combine with -Token.' }
if ($Daemon -and $RoundTrip) { throw '-Daemon cannot combine with -RoundTrip.' }
if ($ExitOnly -and -not $Daemon) { throw '-ExitOnly requires -Daemon.' }
if ($Live -and -not $StrategyMeme -and -not $Daemon -and [string]::IsNullOrWhiteSpace($Token)) { throw '-Live requires -StrategyMeme, -Daemon, or -Token.' }
if ($RoundTrip -and (-not $Live -or (-not $StrategyMeme -and [string]::IsNullOrWhiteSpace($Token)))) {
    throw '-RoundTrip requires -Live plus -StrategyMeme or -Token.'
}
if ($Token -and $Token -notmatch '^0x[0-9a-fA-F]{40}$') { throw 'Invalid -Token.' }
if ($Pool -and $Pool -notmatch '^0x[0-9a-fA-F]{40}$') { throw 'Invalid -Pool.' }
if ($Live -and ($ExpectedWallet -notmatch '^0x[0-9a-fA-F]{40}$' -or $ExpectedWallet -eq ('0x' + ('0' * 40)))) {
    throw '-Live requires the explicitly confirmed -ExpectedWallet.'
}
if ([string]::IsNullOrWhiteSpace($ProjectId)) { throw 'Missing -ProjectId from the OKX Web3 API project.' }
if ($AmountNativeAtomic -and $AmountNativeAtomic -notmatch '^[0-9]{1,78}$') { throw 'Invalid -AmountNativeAtomic.' }
if ($AmountNativeAtomic -and $AmountUsd) { throw 'Choose -AmountNativeAtomic or -AmountUsd, not both.' }
if ($AmountUsd) {
    [decimal]$parsedAmountUsd = 0
    if (-not [decimal]::TryParse(
            $AmountUsd.Trim(),
            [Globalization.NumberStyles]::AllowDecimalPoint,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsedAmountUsd
        ) -or $parsedAmountUsd -lt [decimal]0.1 -or $parsedAmountUsd -gt [decimal]100) {
        throw 'Invalid -AmountUsd. Use a value from 0.1 to 100.'
    }
}
if ($DailyLossOverrideDate) {
    [datetime]$parsedOverrideDate = [datetime]::MinValue
    if (-not [datetime]::TryParseExact(
            $DailyLossOverrideDate.Trim(),
            'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None,
            [ref]$parsedOverrideDate
        )) {
        throw 'Invalid -DailyLossOverrideDate. Use yyyy-MM-dd in UTC.'
    }
}
if ($IntervalSeconds -lt 1 -or $IntervalSeconds -gt 60) { throw 'Invalid -IntervalSeconds.' }

$operatorHome = [Environment]::GetFolderPath('UserProfile')
$configRoot = Join-Path $operatorHome '.config\alpha-radar'
$configPath = Join-Path $configRoot 'okx-live-config.json'
$secretRoot = Join-Path $configRoot 'okx-live-secrets'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'OKX operator config is missing. Run setup_okx_live.ps1 first.'
}

try {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.schema_version -ne 1 -or $config.wallet_address -notmatch '\A0x[0-9a-fA-F]{40}\z' -or
        $config.wallet_address -match '\A0x0{40}\z') { throw 'invalid_config' }
    if ($ExpectedWallet -and $config.wallet_address -ine $ExpectedWallet) { throw 'confirmed_wallet_changed' }
    $chainName = $Chain.ToLowerInvariant()
    $chainId = if ($chainName -eq 'robinhood') { 4663 } else { 56 }
    $effectiveSlippagePercent = if ([string]::IsNullOrWhiteSpace($SlippagePercent)) {
        if ($chainName -eq 'robinhood') { '5' } else { '1' }
    } else {
        $SlippagePercent.Trim()
    }
    [decimal]$parsedSlippagePercent = 0
    if (-not [decimal]::TryParse(
            $effectiveSlippagePercent,
            [Globalization.NumberStyles]::AllowDecimalPoint,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsedSlippagePercent
        ) -or $parsedSlippagePercent -lt [decimal]0.1 -or $parsedSlippagePercent -gt [decimal]50) {
        throw 'Invalid -SlippagePercent. Use a value from 0.1 to 50.'
    }
    $rpcUrl = if ($chainName -eq 'robinhood') {
        $configured = [string]$config.robinhood_rpc_url
        if ($configured) { $configured } else { 'https://rpc.mainnet.chain.robinhood.com' }
    } else {
        [string]$config.bsc_rpc_url
    }
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
catch { throw 'Invalid local OKX config. Run setup_okx_live.ps1 again.' }

$nodeExe = ((Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source)
$executor = Join-Path $PSScriptRoot 'okx-dex-executor\src\okx_dex_executor.mjs'
if ($Daemon) { $executor = Join-Path $PSScriptRoot 'okx-dex-executor\src\okx_dex_live_daemon.mjs' }
if (-not (Test-Path -LiteralPath $executor -PathType Leaf)) { throw 'OKX DEX SDK executor is missing.' }

$args = @('"' + $executor + '"')
if ($Preflight) { $args += '--preflight' }
if ($RemotePreflight) { $args += '--remote-preflight' }
if ($StrategyMeme) { $args += '--strategy-meme' }
if ($Daemon) {
    $args += @('--interval-seconds', [string]$IntervalSeconds)
    if ($Once) { $args += '--once' }
    if ($ExitOnly) { $args += '--exit-only' }
}
if ($Token) {
    $args += @('--token', $Token.ToLowerInvariant())
    if ($Pool) { $args += @('--pool', $Pool.ToLowerInvariant()) }
    if ($Symbol) { $args += @('--symbol', $Symbol) }
}
if ($Live) { $args += '--live' }
if ($RoundTrip) { $args += '--round-trip' }
if ($AmountNativeAtomic) { $args += @('--amount-native-atomic', $AmountNativeAtomic) }
if ($AmountUsd) { $args += @('--amount-usd', $AmountUsd.Trim()) }

$credentials = @{}
$childInfo = $null
$child = $null
try {
    foreach ($name in $secretPaths.Keys) { $credentials[$name] = Read-OkxProtectedSecret $secretPaths[$name] }
    foreach ($name in @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE')) {
        if ($dotEnvValues.ContainsKey($name)) { $credentials[$name] = $dotEnvValues[$name] }
    }
    $credentials['BSC_PRIVATE_KEY'] = $credentials['BSC_PRIVATE_KEY'].Trim()
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $nodeExe
    $info.Arguments = ($args -join ' ')
    $info.WorkingDirectory = (Join-Path $PSScriptRoot 'okx-dex-executor')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -like 'OKX_*' -or $name -like 'BSC_*' -or $name -like 'EVM_*') {
            $info.EnvironmentVariables.Remove($name)
        }
    }
    $info.EnvironmentVariables['OKX_API_KEY'] = $credentials['OKX_API_KEY']
    $info.EnvironmentVariables['OKX_SECRET_KEY'] = $credentials['OKX_SECRET_KEY']
    $info.EnvironmentVariables['OKX_PASSPHRASE'] = $credentials['OKX_PASSPHRASE']
    $info.EnvironmentVariables['OKX_API_PASSPHRASE'] = $credentials['OKX_PASSPHRASE']
    $info.EnvironmentVariables['OKX_PROJECT_ID'] = $ProjectId.Trim()
    $info.EnvironmentVariables['BSC_PRIVATE_KEY'] = $credentials['BSC_PRIVATE_KEY']
    $info.EnvironmentVariables['EVM_PRIVATE_KEY'] = $credentials['BSC_PRIVATE_KEY']
    $info.EnvironmentVariables['BSC_RPC_URL'] = $rpcUrl
    $info.EnvironmentVariables['EVM_RPC_URL'] = $rpcUrl
    $info.EnvironmentVariables['BSC_WALLET_ADDRESS'] = $config.wallet_address.ToLowerInvariant()
    $info.EnvironmentVariables['EVM_WALLET_ADDRESS'] = $config.wallet_address.ToLowerInvariant()
    $info.EnvironmentVariables['EXECUTION_CHAIN_NAME'] = $chainName
    if ($DailyLossOverrideDate) {
        $info.EnvironmentVariables['OKX_DAILY_LOSS_OVERRIDE_CHAIN'] = $chainName
        $info.EnvironmentVariables['OKX_DAILY_LOSS_OVERRIDE_DATE'] = $DailyLossOverrideDate.Trim()
    }
    $info.EnvironmentVariables['EXECUTION_CHAIN_ID'] = [string]$chainId
    $info.EnvironmentVariables['OKX_SLIPPAGE_PERCENT'] = $effectiveSlippagePercent
    $info.EnvironmentVariables['OKX_ECONOMIC_GUARD_ENABLED'] = '1'
    $info.EnvironmentVariables['OKX_EXECUTABLE_EXIT_ENABLED'] = '1'
    $info.EnvironmentVariables['OKX_POSITION_BALANCE_RECONCILE'] = '1'
    $info.EnvironmentVariables['OKX_MAX_ROUND_TRIP_LOSS_PERCENT'] = '25'
    $info.EnvironmentVariables['OKX_MAX_PRICE_IMPACT_PERCENT'] = '12'
    $info.EnvironmentVariables['OKX_ENTRY_TRADEABILITY_TIMEOUT_MS'] = '12000'
    $info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '0'
    $info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '0'
    if ($Live) {
        $info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '1'
        $info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '1'
    }
    $childInfo = $info
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $info
    [void]$child.Start()
    $stdoutTask = $child.StandardOutput.ReadToEndAsync()
    $stderrTask = $child.StandardError.ReadToEndAsync()
    foreach ($name in $secretPaths.Keys) { $info.EnvironmentVariables.Remove($name) }
    $info.EnvironmentVariables.Remove('EVM_PRIVATE_KEY')
    $child.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    foreach ($line in ($stdout -split "`r?`n")) {
        if ($line) { Write-Output (Protect-OkxOutput $line $credentials) }
    }
    foreach ($line in ($stderr -split "`r?`n")) {
        if ($line) { [Console]::Error.WriteLine((Protect-OkxOutput $line $credentials)) }
    }
    exit $child.ExitCode
}
finally {
    if ($null -ne $childInfo) {
        foreach ($name in @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'OKX_API_PASSPHRASE', 'BSC_PRIVATE_KEY', 'EVM_PRIVATE_KEY')) {
            $childInfo.EnvironmentVariables.Remove($name)
        }
    }
    $credentials.Clear()
    if ($null -ne $child) { $child.Dispose() }
}
