[CmdletBinding()]
param(
    [ValidateSet('bsc', 'robinhood')]
    [string]$Chain = 'robinhood'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$python = (& py -3 -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'python_unavailable' }
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'pythonw_unavailable' }
$script = Join-Path $PSScriptRoot 'alpha_remote_sync.py'
$taskName = "AlphaRadarRemoteSync-$Chain"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" --chain $Chain --interval-seconds 3"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest -Force | Out-Null
Write-Output "$taskName registered. Existing sync process was not interrupted."
