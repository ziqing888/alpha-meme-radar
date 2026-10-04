$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$Script = Join-Path $Root 'work\alpha-pool-radar\alpha_pool_radar.py'
$OutDir = Join-Path $Root 'outputs'
$Log = Join-Path $OutDir 'alpha-pool-radar-monitor.log'
$ErrLog = Join-Path $OutDir 'alpha-pool-radar-monitor.err.log'
$PidFile = Join-Path $OutDir 'alpha-pool-radar-monitor.pid'

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$Existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -like "*alpha_pool_radar.py*--monitor*" }

if ($Existing) {
  $Existing.ProcessId | Set-Content -Encoding ASCII -Path $PidFile
  Write-Host "Monitor already running. PID: $($Existing.ProcessId -join ', ')"
  exit 0
}

$Args = @(
  '-u', $Script,
  '--monitor',
  '--interval-seconds', '120',
  '--alpha-limit', '120',
  '--top', '30',
  '--out-dir', $OutDir,
  '--alert-score', '58',
  '--alert-score-jump', '8',
  '--alert-oi', '5',
  '--alert-funding-abs', '0.05',
  '--alert-cooldown-seconds', '1800'
)

$Process = Start-Process -FilePath 'python' -ArgumentList $Args -RedirectStandardOutput $Log -RedirectStandardError $ErrLog -WindowStyle Hidden -PassThru
$Process.Id | Set-Content -Encoding ASCII -Path $PidFile
Write-Host "Monitor started. PID: $($Process.Id)"
Write-Host "Log: $Log"
Write-Host "Error log: $ErrLog"
