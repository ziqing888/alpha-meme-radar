[CmdletBinding()]
param(
    [string]$WalletAddress,
    [string]$PythonPath,
    [string]$RpcUrl = 'https://bsc-dataseed.bnbchain.org'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# A PS5 child can inherit PS7's module search path from its launcher.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Global -ErrorAction Stop

function Set-OkxPrivateAcl([string]$Path) {
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
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $identity, 'FullControl', $inheritance, 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-OkxDerivedAddress([string]$PythonExe, [Security.SecureString]$PrivateKey) {
    # Only stdin carries the key. The isolated helper returns a public address or a fixed code.
    $code = @'
import re, sys
try:
    key = sys.stdin.buffer.read().decode("utf-8-sig").strip()
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key):
        sys.exit(2)
    if not 0 < int(key, 16) < 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141:
        sys.exit(2)
    try:
        from eth_account import Account
    except ImportError:
        sys.exit(3)
    print(Account.from_key(key).address)
except Exception:
    sys.exit(2)
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $PythonExe
    $info.Arguments = "-E -B -c `"import base64;exec(base64.b64decode('$encoded'))`""
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($PythonExe)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -like 'OKX_*' -or $name -like 'BSC_*' -or $name -like 'GMGN_*' -or
            $name -in @('LIVE_TRADING_ENABLED', 'ARMED')) { $info.EnvironmentVariables.Remove($name) }
    }
    $info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '0'
    $info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '0'
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $info
    $pointer = [IntPtr]::Zero
    $started = $false
    try {
        [void]$child.Start()
        $started = $true
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($PrivateKey)
        $child.StandardInput.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer))
        $child.StandardInput.Close()
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $pointer = [IntPtr]::Zero
        if (-not $child.WaitForExit(20000)) { throw 'helper_timeout' }
        $result = $stdout.GetAwaiter().GetResult().Trim()
        [void]$stderr.GetAwaiter().GetResult()
        if ($child.ExitCode -eq 3) { return $null }
        if ($child.ExitCode -ne 0 -or $result -notmatch '\A0x[0-9a-fA-F]{40}\z') { throw 'invalid_key' }
        return $result
    }
    catch { throw 'Local wallet-key validation failed. Enter a valid dedicated EVM wallet private key.' }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        try {
            if ($started -and -not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
        }
        finally { $child.Dispose() }
    }
}

function Get-OkxDerivedAddressWithNode([Security.SecureString]$PrivateKey) {
    try { $nodeExe = (Get-Command 'node.exe' -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
    catch { return $null }
    $executorRoot = Join-Path $PSScriptRoot 'okx-dex-executor'
    $ethersRoot = Join-Path $executorRoot 'node_modules\ethers'
    if (-not (Test-Path -LiteralPath $ethersRoot -PathType Container)) { return $null }
    $code = @'
import('ethers').then(({ Wallet }) => {
  let key = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => { key += chunk; });
  process.stdin.on('end', () => {
    try {
      key = key.replace(/^\uFEFF/, '').trim();
      if (!/^(?:0x)?[0-9a-fA-F]{64}$/.test(key)) {
        process.exit(2);
      }
      console.log(new Wallet(key).address);
    } catch {
      process.exit(2);
    }
  });
}).catch(() => process.exit(3));
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $nodeExe
    $info.Arguments = "-e `"eval(Buffer.from('$encoded','base64').toString('utf8'))`""
    $info.WorkingDirectory = $executorRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -like 'OKX_*' -or $name -like 'BSC_*' -or $name -like 'GMGN_*' -or
            $name -in @('LIVE_TRADING_ENABLED', 'ARMED')) { $info.EnvironmentVariables.Remove($name) }
    }
    $info.EnvironmentVariables['OKX_LIVE_ENABLED'] = '0'
    $info.EnvironmentVariables['OKX_ALLOW_AUTOMATED_TRADES'] = '0'
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $info
    $pointer = [IntPtr]::Zero
    $started = $false
    try {
        [void]$child.Start()
        $started = $true
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($PrivateKey)
        $child.StandardInput.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer))
        $child.StandardInput.Close()
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $pointer = [IntPtr]::Zero
        if (-not $child.WaitForExit(20000)) { throw 'helper_timeout' }
        $result = $stdout.GetAwaiter().GetResult().Trim()
        [void]$stderr.GetAwaiter().GetResult()
        if ($child.ExitCode -eq 3) { return $null }
        if ($child.ExitCode -ne 0 -or $result -notmatch '\A0x[0-9a-fA-F]{40}\z') { throw 'invalid_key' }
        return $result
    }
    catch { throw 'Local wallet-key validation failed. Enter a valid dedicated EVM wallet private key.' }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        try {
            if ($started -and -not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
        }
        finally { $child.Dispose() }
    }
}

if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windows DPAPI is required.' }
$operatorHome = [Environment]::GetFolderPath('UserProfile')
$configRoot = Join-Path $operatorHome '.config\alpha-radar'
$configPath = Join-Path $configRoot 'okx-live-config.json'
$secretRoot = Join-Path $configRoot 'okx-live-secrets'
$stateRoot = Join-Path $configRoot 'gmgn-live'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') + '\'
if ([IO.Path]::GetFullPath($configRoot).StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Operator configuration must be outside the repository.'
}
$rpcUri = $null
if (-not [Uri]::TryCreate($RpcUrl, [UriKind]::Absolute, [ref]$rpcUri) -or
    $rpcUri.Scheme -notin @('http', 'https') -or -not $rpcUri.Host -or
    $rpcUri.UserInfo -or $rpcUri.Query -or $rpcUri.Fragment -or $RpcUrl -match '\s') {
    throw 'RpcUrl must be an HTTP(S) URL without user info, query, fragment, or whitespace.'
}
if (-not $PythonPath) { $PythonPath = 'py.exe' }
try { $pythonExe = (Get-Command $PythonPath -CommandType Application -ErrorAction Stop).Source }
catch { throw 'Python executable was not found. Supply -PythonPath with its full path.' }
if ([IO.Path]::GetFileName($pythonExe) -ieq 'py.exe') {
    $resolvedPython = & $pythonExe -3 -I -B -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or @($resolvedPython).Count -ne 1 -or
        -not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw 'Python 3 could not be resolved.' }
    $pythonExe = $resolvedPython
}

# Check filesystem permissions before asking the operator for any credentials.
[void][IO.Directory]::CreateDirectory($secretRoot)
Set-OkxPrivateAcl $secretRoot
[void][IO.Directory]::CreateDirectory($stateRoot)
Set-OkxPrivateAcl $stateRoot

$secrets = @{}
try {
    foreach ($name in @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'BSC_PRIVATE_KEY')) {
        $prompt = "$name (hidden)"
        if ($name -eq 'BSC_PRIVATE_KEY') { $prompt = 'BSC_PRIVATE_KEY: actual dedicated EVM wallet private key, NOT GMGN PEM (hidden)' }
        $secrets[$name] = Read-Host $prompt -AsSecureString
        if ($secrets[$name].Length -eq 0) { throw 'All four credentials are required.' }
    }
    $derivedAddress = Get-OkxDerivedAddress -PythonExe $pythonExe -PrivateKey $secrets['BSC_PRIVATE_KEY']
    if (-not $derivedAddress) {
        $derivedAddress = Get-OkxDerivedAddressWithNode -PrivateKey $secrets['BSC_PRIVATE_KEY']
    }
    if ($derivedAddress) {
        if ($WalletAddress -and $WalletAddress.Trim() -ine $derivedAddress) {
            throw 'WalletAddress does not match the locally derived wallet address.'
        }
        $WalletAddress = $derivedAddress
        Write-Host "Derived public BSC wallet: $WalletAddress"
    }
    else {
        Write-Host 'Local address derivation is unavailable. Public address entry is required; key/address matching is unverified. Re-run setup after npm install in okx-dex-executor or with eth_account installed to verify it locally.'
        if (-not $WalletAddress) { $WalletAddress = Read-Host 'BSC_WALLET_ADDRESS (public)' }
    }
    $WalletAddress = $WalletAddress.Trim()
    if ($WalletAddress -notmatch '\A0x[0-9a-fA-F]{40}\z' -or $WalletAddress -match '\A0x0{40}\z') {
        throw 'A valid nonzero BSC public address is required.'
    }
    if (Test-Path -LiteralPath $configPath) {
        try { $previous = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
        catch { throw 'Existing operator config is invalid. Review it before setup.' }
        if ($previous.wallet_address -ine $WalletAddress) {
            throw 'Configured wallet differs. Reconcile the existing account state before changing wallets.'
        }
    }
    $version = [Guid]::NewGuid().ToString('N')
    $paths = @{}
    foreach ($name in @('OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE', 'BSC_PRIVATE_KEY')) {
        $path = Join-Path $secretRoot ($version + '-' + $name.ToLowerInvariant() + '.dpapi')
        # With no explicit encryption key, Windows uses current-user DPAPI.
        [IO.File]::WriteAllText($path, (ConvertFrom-SecureString -SecureString $secrets[$name]))
        Set-OkxPrivateAcl $path
        $paths[$name] = $path
    }
    $config = [ordered]@{
        schema_version = 1
        wallet_address = $WalletAddress.ToLowerInvariant()
        wallet_address_derived = [bool]$derivedAddress
        api_key_masked = '********'
        api_key_dpapi_path = $paths['OKX_API_KEY']
        secret_key_dpapi_path = $paths['OKX_SECRET_KEY']
        passphrase_dpapi_path = $paths['OKX_PASSPHRASE']
        wallet_private_key_dpapi_path = $paths['BSC_PRIVATE_KEY']
        python_path = $pythonExe
        state_root = $stateRoot
        slippage_percent = 5
        bsc_rpc_url = $RpcUrl
    }
    # Versioned secrets and atomic config replacement preserve the previous setup on write failure.
    $staging = Join-Path $secretRoot ($version + '-config.json')
    [IO.File]::WriteAllText($staging, ($config | ConvertTo-Json))
    Set-OkxPrivateAcl $staging
    if (Test-Path -LiteralPath $configPath) {
        $backup = Join-Path $secretRoot ($version + '-config.backup.json')
        [IO.File]::Replace($staging, $configPath, $backup)
        if (Test-Path -LiteralPath $backup -PathType Leaf) { Remove-Item -LiteralPath $backup -Force }
    }
    else { [IO.File]::Move($staging, $configPath) }
    Set-OkxPrivateAcl $configPath
    Write-Host 'OKX API credentials and wallet private key saved with Windows DPAPI protection.'
    Write-Host 'No worker started. Run start_okx_live.ps1 for an offline check; only -Live authorizes automation.'
}
finally {
    foreach ($secret in $secrets.Values) { if ($null -ne $secret) { $secret.Dispose() } }
    $secrets.Clear()
}
