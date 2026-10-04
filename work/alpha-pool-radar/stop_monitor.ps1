$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$OutDir = Join-Path $Root 'outputs'
$PidFile = Join-Path $OutDir 'alpha-pool-radar-monitor.pid'

$Pids = @()
if (Test-Path $PidFile) {
  $Pids += Get-Content $PidFile | Where-Object { $_ -match '^\d+$' }
}

$Running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -like "*alpha_pool_radar.py*--monitor*" }
$Pids += $Running.ProcessId
$Pids = $Pids | Sort-Object -Unique

if (-not $Pids) {
  Write-Host "No monitor process found."
  exit 0
}

foreach ($MonitorPid in $Pids) {
  Stop-Process -Id $MonitorPid -Force -ErrorAction SilentlyContinue
  Write-Host "Stopped monitor PID: $MonitorPid"
}

Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
