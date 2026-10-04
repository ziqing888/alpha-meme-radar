$ErrorActionPreference = 'Stop'

$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$Preflight = Join-Path $Root 'work\alpha-pool-radar\alpha_bsc_live_preflight.py'
$Script = Join-Path $Root 'work\alpha-pool-radar\alpha_execution_worker.py'
$OutDir = Join-Path $Root 'outputs\bsc-execution'
$Log = Join-Path $OutDir 'worker-live.log'

foreach ($Name in @(
  'ALPHA_NARRATIVE_BROWSER_COMMANDS',
  'ALPHA_NARRATIVE_TWEET_URLS',
  'ALPHA_NARRATIVE_TWEET_FILES',
  'ALPHA_NARRATIVE_TWEETS_FILE'
)) {
  Set-Item -Path "Env:$Name" -Value ''
}

# Official OKX BSC router and the supported smartSwapByInvest selector.
# User-provided values remain authoritative when explicitly set.
if (-not $env:BSC_ALLOWED_ROUTER_ADDRESSES) {
  $env:BSC_ALLOWED_ROUTER_ADDRESSES = '0x3156020dfF8D99af1dDC523ebDfb1ad2018554a0'
}
if (-not $env:BSC_OKX_SWAP_SELECTOR) {
  $env:BSC_OKX_SWAP_SELECTOR = '0xe99bfa95'
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$PreflightJson = & py -3 $Preflight --json
if ($LASTEXITCODE -ne 0) {
  throw "Live preflight failed. No live worker was started."
}
$PreflightResult = $PreflightJson | ConvertFrom-Json
if ($PreflightResult.ready -ne $true) {
  throw "Live preflight is not ready. No live worker was started."
}

$Existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match '(?i)(^|[\\/])alpha_execution_worker\.py([\s"'']|$)' }
if ($Existing) {
  $Pids = $Existing.ProcessId -join ', '
  throw "BSC execution worker already running. Refusing duplicate start. PID: $Pids"
}

$Command = "py -3 -u `"$Script`" --mode live --interval-seconds 5 >> `"$Log`" 2>&1"
$Process = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d', '/s', '/c', $Command) -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 500
$Worker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match '(?i)(^|[\\/])alpha_execution_worker\.py([\s"'']|$)' }
if (-not $Worker) {
  throw "Live worker failed to start. See log: $Log"
}

Write-Host "BSC live worker started after preflight. PID: $($Worker.ProcessId -join ', ')"
Write-Host "Log: $Log"
