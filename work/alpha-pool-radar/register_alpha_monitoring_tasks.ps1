[CmdletBinding()]
param(
    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$PythonW = [Environment]::GetEnvironmentVariable('ALPHA_PYTHONW')
if ([string]::IsNullOrWhiteSpace($PythonW) -or -not (Test-Path -LiteralPath $PythonW)) {
    $PythonW = (& py -3 -c 'import pathlib,sys; print(pathlib.Path(sys.executable).with_name("pythonw.exe"))').Trim()
}
if (-not (Test-Path -LiteralPath $PythonW)) { throw 'pythonw_unavailable' }

$MemeScript = Join-Path $PSScriptRoot 'alpha_meme_live_refresh.py'
$ServerScript = Join-Path $PSScriptRoot 'alpha_live_server.py'
$Report = Join-Path $Root 'outputs\alpha-radar-report-latest.json'

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Tasks = @(
    @{
        Name = 'AlphaMemeVoiceMonitor'
        Description = 'Full Meme intelligence refresh; realtime voice is handled by the V3 frontend.'
        Arguments = ('-B -u "{0}" --interval-seconds 60' -f $MemeScript)
    },
    @{
        Name = 'AlphaLiveReportServer'
        Description = 'Local live report bridge used by the production Alpha Radar page.'
        Arguments = ('-B -u "{0}" --report "{1}" --no-fast-track' -f $ServerScript, $Report)
    }
)

foreach ($Task in $Tasks) {
    $Action = New-ScheduledTaskAction -Execute $PythonW -Argument $Task.Arguments -WorkingDirectory $Root
    Register-ScheduledTask -TaskName $Task.Name -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description $Task.Description -Force | Out-Null
    if ($StartNow) { Start-ScheduledTask -TaskName $Task.Name }
    [pscustomobject]@{ TaskName = $Task.Name; State = (Get-ScheduledTask -TaskName $Task.Name).State }
}
