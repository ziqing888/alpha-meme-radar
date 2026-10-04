[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteHost,
    [string]$RemoteUser = 'alpha-radar',
    [Parameter(Mandatory = $true)]
    [string]$IdentityFile,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedWallet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$securityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
Import-Module $securityModule -Global -ErrorAction Stop

function Read-ProtectedSecret([string]$Path) {
    $secure = $null
    $pointer = [IntPtr]::Zero
    try {
        $secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($Path))
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plain)) { throw "Empty protected secret: $Path" }
        return $plain
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($null -ne $secure) { $secure.Dispose() }
    }
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { continue }
        $name = $matches[1]
        $value = $matches[2]
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value.Trim()
    }
    return $values
}

function Send-RemoteFile([string]$Content, [string]$RemotePath, [string]$Mode = '0640') {
    if ($RemotePath -notmatch '^/etc/alpha-radar/(?:secrets/[a-z_]+|runtime\.env)$') {
        throw "Unexpected remote path: $RemotePath"
    }
    # Native PowerShell pipelines append CRLF. Normalize only the transport
    # framing so Linux dotenv and one-line secret readers receive LF content.
    $remoteCommand = "umask 027; tr -d '\r' > '$RemotePath'; chown root:alpha-radar '$RemotePath'; chmod $Mode '$RemotePath'"
    $result = $Content | & ssh -i $IdentityFile -o BatchMode=yes -o ConnectTimeout=20 -o ConnectionAttempts=3 `
        "$RemoteUser@$RemoteHost" $remoteCommand 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Remote write failed for $RemotePath`: $result" }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$envPath = Join-Path $repoRoot '.env'
$configPath = Join-Path $HOME '.config\alpha-radar\okx-live-config.json'
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw 'SSH identity file is missing.' }
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { throw '.env is missing.' }
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw 'OKX live config is missing.' }

$dotEnv = Read-DotEnv $envPath
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$wallet = ([string]$config.wallet_address).ToLowerInvariant()
if ($wallet -ne $ExpectedWallet.ToLowerInvariant()) { throw 'Configured wallet does not match ExpectedWallet.' }

$requiredEnv = @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'OKX_PROJECT_ID')
foreach ($name in $requiredEnv) {
    if (-not $dotEnv.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$dotEnv[$name])) {
        throw "Missing $name in .env."
    }
}

$privateKey = Read-ProtectedSecret ([string]$config.wallet_private_key_dpapi_path)
$secrets = [ordered]@{
    okx_api_key = [string]$dotEnv.OKX_API_KEY
    okx_secret_key = [string]$dotEnv.OKX_SECRET_KEY
    okx_passphrase = [string]$dotEnv.OKX_PASSPHRASE
    evm_private_key = $privateKey
}

try {
    foreach ($entry in $secrets.GetEnumerator()) {
        Send-RemoteFile $entry.Value "/etc/alpha-radar/secrets/$($entry.Key)"
    }

    $bscRpc = [string]$config.bsc_rpc_url
    $robinhoodRpc = [string]$config.robinhood_rpc_url
    if ([string]::IsNullOrWhiteSpace($robinhoodRpc)) { $robinhoodRpc = 'https://rpc.mainnet.chain.robinhood.com' }
    $runtime = @(
        "OKX_PROJECT_ID=$([string]$dotEnv.OKX_PROJECT_ID)"
        "BSC_WALLET_ADDRESS=$wallet"
        "BSC_RPC_URL=$bscRpc"
        "ROBINHOOD_RPC_URL=$robinhoodRpc"
        'OKX_SLIPPAGE_PERCENT=1'
        'OKX_ECONOMIC_GUARD_ENABLED=1'
        'OKX_EXECUTABLE_EXIT_ENABLED=1'
        'OKX_POSITION_BALANCE_RECONCILE=1'
        'OKX_MAX_ROUND_TRIP_LOSS_PERCENT=10'
        'OKX_MAX_PRICE_IMPACT_PERCENT=8'
        'ALPHA_ORDER_USD=5'
        'ALPHA_INTERVAL_SECONDS=1'
    ) -join "`n"
    Send-RemoteFile ($runtime + "`n") '/etc/alpha-radar/runtime.env'
    Write-Output 'Remote runtime provisioned. Live services remain disabled, unarmed, and stopped.'
}
finally {
    $privateKey = $null
    foreach ($name in @($secrets.Keys)) { $secrets[$name] = $null }
    $secrets.Clear()
    $dotEnv.Clear()
}
