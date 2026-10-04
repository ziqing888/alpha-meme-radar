[CmdletBinding()]
param(
    [string]$WalletAddress,
    [string]$ApiSigningKeyPath,
    [string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Set-GmgnPrivateAcl([string]$Path) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    $acl.SetOwner($sid)
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    }
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $identity, 'FullControl', $inheritance, 'None', 'Allow'
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windows DPAPI is required.' }
$operatorHome = [Environment]::GetFolderPath('UserProfile')
$configRoot = Join-Path $operatorHome '.config\alpha-radar'
$configPath = Join-Path $configRoot 'gmgn-live-config.json'
$secretRoot = Join-Path $configRoot 'gmgn-live-secrets'
$stateRoot = Join-Path $configRoot 'gmgn-live'

if (-not $WalletAddress) { $WalletAddress = Read-Host 'GMGN hosted BSC wallet public address' }
$WalletAddress = $WalletAddress.Trim()
if ($WalletAddress -notmatch '^0x[0-9a-fA-F]{40}$' -or $WalletAddress -match '^0x0{40}$') {
    throw 'A valid nonzero BSC public address is required.'
}
if (Test-Path -LiteralPath $configPath) {
    try { $previous = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
    catch { throw 'Existing operator config is invalid. Review it before setup.' }
    if ($previous.wallet_address -ine $WalletAddress) {
        throw 'Configured wallet differs. Reconcile the existing account state before changing wallets.'
    }
}
if (-not $ApiSigningKeyPath) {
    $ApiSigningKeyPath = Read-Host 'Path to GMGN API signing PEM (not a blockchain wallet private key)'
}
try {
    $pemFile = Get-Item -LiteralPath $ApiSigningKeyPath
    if ($pemFile.PSIsContainer) { throw 'directory' }
    $repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') + '\'
    if ($pemFile.FullName.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'repository_path'
    }
    $pemText = [IO.File]::ReadAllText($pemFile.FullName).Trim()
    if ($pemText -notmatch '(?s)^-----BEGIN (PRIVATE KEY|RSA PRIVATE KEY)-----\s+[A-Za-z0-9+/=\s]+\s+-----END \1-----$') {
        throw 'invalid_pem'
    }
}
catch { throw 'Use an unencrypted GMGN Ed25519/RSA API signing PEM stored outside the repository.' }

if (-not $PythonPath) { $PythonPath = (Get-Command py.exe -CommandType Application).Source }
try { $pythonExe = (Get-Command $PythonPath -CommandType Application -ErrorAction Stop).Source }
catch { throw 'Python executable was not found. Supply -PythonPath with its full path.' }

$apiKey = $null
$pemSecure = $null
try {
    $apiKey = Read-Host 'GMGN API key (hidden)' -AsSecureString
    if ($apiKey.Length -eq 0) { throw 'GMGN API key is required.' }
    $pemSecure = ConvertTo-SecureString -String $pemText -AsPlainText -Force
    $pemText = $null
    # No explicit encryption key: ConvertFrom-SecureString uses current-user Windows DPAPI.
    $apiCipher = ConvertFrom-SecureString -SecureString $apiKey
    $pemCipher = ConvertFrom-SecureString -SecureString $pemSecure
    [void][IO.Directory]::CreateDirectory($secretRoot)
    Set-GmgnPrivateAcl $secretRoot
    [void][IO.Directory]::CreateDirectory($stateRoot)
    Set-GmgnPrivateAcl $stateRoot

    # Versioned secret files keep the previous config usable until the JSON replacement succeeds.
    $version = [Guid]::NewGuid().ToString('N')
    $apiPath = Join-Path $secretRoot ($version + '-api-key.dpapi')
    $pemPath = Join-Path $secretRoot ($version + '-api-signing-pem.dpapi')
    [IO.File]::WriteAllText($apiPath, $apiCipher)
    [IO.File]::WriteAllText($pemPath, $pemCipher)
    Set-GmgnPrivateAcl $apiPath
    Set-GmgnPrivateAcl $pemPath
    $config = [ordered]@{
        schema_version = 1
        wallet_address = $WalletAddress.ToLowerInvariant()
        api_key_masked = '********'
        api_key_dpapi_path = $apiPath
        api_signing_key_dpapi_path = $pemPath
        python_path = $pythonExe
        state_root = $stateRoot
        slippage_percent = 5
        bsc_rpc_url = 'https://bsc-dataseed.binance.org/'
    }
    $staging = Join-Path $secretRoot ($version + '-config.json')
    [IO.File]::WriteAllText($staging, ($config | ConvertTo-Json))
    Set-GmgnPrivateAcl $staging
    if (Test-Path -LiteralPath $configPath) {
        [IO.File]::Replace($staging, $configPath, $null)
    }
    else { [IO.File]::Move($staging, $configPath) }
    Set-GmgnPrivateAcl $configPath
    Write-Host 'Operator configuration saved. API key: ********; API signing PEM: DPAPI protected.'
    Write-Host 'No worker started. Automated trading remains off until the launcher is called with -Live.'
}
finally {
    $pemText = $null
    $apiCipher = $null
    $pemCipher = $null
    if ($null -ne $apiKey) { $apiKey.Dispose() }
    if ($null -ne $pemSecure) { $pemSecure.Dispose() }
}
