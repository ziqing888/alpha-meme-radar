param(
  [string]$TaskName = "AlphaRadarLiveRefresh",
  [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Script = Join-Path $Root "work\alpha-pool-radar\alpha_live_refresh.py"
$LogDir = Join-Path $Root "outputs"
$LogFile = Join-Path $LogDir "alpha-live-refresh-task.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$ActionArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location '$Root'; python '$Script' --once --cloud-upload *> '$LogFile'`""
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArgs
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Read-only Alpha Radar refresh and cloud report upload." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Output "registered:$TaskName"
