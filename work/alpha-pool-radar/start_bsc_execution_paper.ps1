$ErrorActionPreference = 'Stop'

$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$Script = Join-Path $Root 'work\alpha-pool-radar\alpha_execution_worker.py'
$OutDir = Join-Path $Root 'outputs\bsc-execution'
$Log = Join-Path $OutDir 'worker.log'

$Digest = [Security.Cryptography.SHA256]::Create()
try {
  $NameHash = (($Digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.Path)) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 24)
}
finally {
  $Digest.Dispose()
}
$MutexName = "Local\AlphaBscExecutionLauncher-$NameHash"
$LauncherMutex = $null
$LockHeld = $false

try {
  $CreatedNew = $false
  $LauncherMutex = [System.Threading.Mutex]::new($false, $MutexName, [ref]$CreatedNew)
  try {
    $LockHeld = $LauncherMutex.WaitOne(0)
  }
  catch [System.Threading.AbandonedMutexException] {
    $LockHeld = $true
  }
  if (-not $LockHeld) {
    Write-Error "BSC paper launcher is already starting. Refusing concurrent start. Mutex: $MutexName"
    exit 1
  }

  foreach ($Name in @(
    'ALPHA_NARRATIVE_BROWSER_COMMANDS',
    'ALPHA_NARRATIVE_TWEET_URLS',
    'ALPHA_NARRATIVE_TWEET_FILES',
    'ALPHA_NARRATIVE_TWEETS_FILE'
  )) {
    Set-Item -Path "Env:$Name" -Value ''
  }
  $env:BSC_EXECUTION_MODE = 'paper'

  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

  $Existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)(^|[\\/])alpha_execution_worker\.py([\s\"'']|$)' }

  if ($Existing) {
    $Pids = $Existing.ProcessId -join ', '
    Write-Error "BSC execution worker already running. Refusing duplicate start. PID: $Pids"
    exit 1
  }

  $Command = "py -3 -u `"$Script`" --mode paper --interval-seconds 5 >> `"$Log`" 2>&1"
  $Process = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d', '/s', '/c', $Command) -WindowStyle Hidden -PassThru

  Start-Sleep -Milliseconds 250
  $Worker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)(^|[\\/])alpha_execution_worker\.py([\s\"'']|$)' }
  if (-not $Worker) {
    throw "Paper worker failed to start. See log: $Log"
  }

  Write-Host "BSC paper worker started. PID: $($Worker.ProcessId -join ', ')"
  Write-Host "Mode: $env:BSC_EXECUTION_MODE"
  Write-Host "Log: $Log"
}
finally {
  if ($LockHeld -and $LauncherMutex) {
    $LauncherMutex.ReleaseMutex()
  }
  if ($LauncherMutex) {
    $LauncherMutex.Dispose()
  }
}
