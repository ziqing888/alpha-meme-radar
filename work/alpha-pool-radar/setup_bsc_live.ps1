param(
  [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'

$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$LiveLauncher = Join-Path $Root 'work\alpha-pool-radar\start_bsc_execution_live.ps1'
$Preflight = Join-Path $Root 'work\alpha-pool-radar\alpha_bsc_live_preflight.py'
$RpcUrl = if ($env:BSC_RPC_URL) { $env:BSC_RPC_URL } else { 'https://bsc-dataseed.binance.org/' }

$WalletAddress = Read-Host '在本机输入 BSC 钱包地址'
if (-not $WalletAddress -or $WalletAddress.Trim() -notmatch '^0x[0-9a-fA-F]{40}$') {
  throw '钱包地址格式无效，未启动实盘。'
}

$PrivateKey = $null
$SecureKey = Read-Host '在本机输入 BSC 私钥（不会显示）' -AsSecureString
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
try {
  $PrivateKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
}
finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
}

try {
  if (-not $PrivateKey -or -not $PrivateKey.Trim()) {
    throw '未输入私钥，未启动实盘。'
  }

  # Keep the key in this process environment only; never write it to disk or output it.
  $env:BSC_PRIVATE_KEY = $PrivateKey.Trim()
  $env:BSC_WALLET_ADDRESS = $WalletAddress.Trim()
  $env:BSC_RPC_URL = $RpcUrl
  $env:BSC_ALLOWED_ROUTER_ADDRESSES = if ($env:BSC_ALLOWED_ROUTER_ADDRESSES) {
    $env:BSC_ALLOWED_ROUTER_ADDRESSES
  } else {
    '0x3156020dfF8D99af1dDC523ebDfb1ad2018554a0'
  }
  $env:BSC_OKX_SWAP_SELECTOR = if ($env:BSC_OKX_SWAP_SELECTOR) {
    $env:BSC_OKX_SWAP_SELECTOR
  } else {
    '0xe99bfa95'
  }
  $env:BSC_LIVE_BUY_AMOUNT_ATOMIC = if ($env:BSC_LIVE_BUY_AMOUNT_ATOMIC) {
    $env:BSC_LIVE_BUY_AMOUNT_ATOMIC
  } else {
    '5000000000000000'
  }
  $env:BSC_LIVE_SLIPPAGE_PERCENT = if ($env:BSC_LIVE_SLIPPAGE_PERCENT) {
    $env:BSC_LIVE_SLIPPAGE_PERCENT
  } else {
    '3'
  }
  $env:LIVE_TRADING_ENABLED = 'true'
  $env:ARMED = 'true'

  $DerivedAddress = & py -3 -c 'import os; from eth_account import Account; print(Account.from_key(os.environ["BSC_PRIVATE_KEY"]).address)'
  if ($LASTEXITCODE -ne 0 -or -not $DerivedAddress) {
    throw '私钥格式无效，未启动实盘。'
  }
  $DerivedAddress = ($DerivedAddress | Select-Object -Last 1).Trim()
  if ($DerivedAddress.ToLowerInvariant() -ne $env:BSC_WALLET_ADDRESS.ToLowerInvariant()) {
    throw '钱包地址与私钥推导地址不一致，未启动实盘。'
  }
  Write-Host "钱包地址: $env:BSC_WALLET_ADDRESS"
  if ($PrepareOnly) {
    Write-Host '仅执行实盘 preflight；不会启动 live worker。'
    & py -3 $Preflight --json
    if ($LASTEXITCODE -ne 0) {
      throw '实盘 preflight 未通过，live worker 未启动。'
    }
    return
  }
  Write-Host '正在执行实盘 preflight；未通过则不会启动。'
  & powershell -NoProfile -ExecutionPolicy Bypass -File $LiveLauncher
  if ($LASTEXITCODE -ne 0) {
    throw '实盘 preflight 未通过，worker 未启动。'
  }
}
finally {
  Remove-Item Env:BSC_PRIVATE_KEY -ErrorAction SilentlyContinue
}
