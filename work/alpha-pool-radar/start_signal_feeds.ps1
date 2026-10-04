[CmdletBinding()]
param([switch]$CheckOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Out = Join-Path $Root 'outputs'
$Python = (& py -3 -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Python)) { throw 'python_unavailable' }

$Mutex = [Threading.Mutex]::new($false, 'Local\AlphaRadarSignalFeedLauncher')
$Held = $false
$Saved = @{}
$Overrides = @{
    OKX_LIVE_ENABLED = '0'
    OKX_ALLOW_AUTOMATED_TRADES = '0'
    GMGN_LIVE_ENABLED = '0'
    GMGN_ALLOW_AUTOMATED_TRADES = '0'
    ALPHA_NARRATIVE_TWEET_URLS = ''
    ALPHA_NARRATIVE_BROWSER_COMMANDS = ''
    ALPHA_GOLD_VOICE_ALERT = '1'
    ALPHA_GOLD_VOICE_ALERT_REPEAT = '1'
    PYTHONUNBUFFERED = '1'
    PYTHONUTF8 = '1'
}
foreach ($Item in (Get-ChildItem Env: | Where-Object { $_.Name -match '_COMMANDS$' })) {
    $Overrides[$Item.Name] = ''
}
try {
    try { $Held = $Mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $Held = $true }
    if (-not $Held) { throw 'signal_feed_launcher_busy' }
    $Processes = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'node.exe'")
    if (@($Processes | Where-Object { -not $_.CommandLine }).Count) { throw 'process_state_unavailable' }
    # Reuse the terminal's full worker/launcher inventory, including legacy paths.
    Push-Location $PSScriptRoot
    try {
        $Idle = & $Python -c 'from alpha_terminal_control import TerminalControl; c=TerminalControl(); print(not c._has_worker(c._process_snapshot()))'
        $GuardExit = $LASTEXITCODE
    } finally { Pop-Location }
    if ($GuardExit -ne 0 -or $Idle -ne 'True') {
        throw 'trading_worker_running_check_before_restoring_feeds'
    }
    foreach ($Name in $Overrides.Keys) {
        $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
        [Environment]::SetEnvironmentVariable($Name, $Overrides[$Name], 'Process')
    }
    foreach ($Feed in @(
        @{ Name = 'discovery'; Script = 'alpha_meme_live_refresh.py'; Interval = '60' },
        @{ Name = 'quotes'; Script = 'alpha_fast_track.py'; Interval = '10' }
    )) {
        $Script = Join-Path $PSScriptRoot $Feed.Script
        $Existing = @($Processes | Where-Object {
            $_.CommandLine -match ('(?i)(?:^|[\s"''])' + [regex]::Escape($Script) + '(?:[\s"'']|$)') -or
            $_.CommandLine -match ('(?i)(?:^|[\s"''/\\])' + [regex]::Escape($Feed.Script) + '(?:[\s"'']|$)')
        })
        if ($Existing.Count -gt 1) { throw ('duplicate_feed_' + $Feed.Name) }
        if ($Existing.Count) {
            [pscustomobject]@{ Feed = $Feed.Name; State = 'already_running'; ProcessId = $Existing[0].ProcessId }
        } elseif ($CheckOnly) {
            [pscustomobject]@{ Feed = $Feed.Name; State = 'stopped'; ProcessId = $null }
        } else {
            $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
            $Arguments = @('-u', ('"' + $Script + '"'), '--interval-seconds', $Feed.Interval)
            $Process = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Root `
                -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $Out ("signal-" + $Feed.Name + "-$Stamp.stdout.log")) `
                -RedirectStandardError (Join-Path $Out ("signal-" + $Feed.Name + "-$Stamp.stderr.log"))
            [pscustomobject]@{ Feed = $Feed.Name; State = 'started'; ProcessId = $Process.Id }
        }
    }
    Write-Host 'Data feeds only. No trading worker, wallet transaction signing, cloud upload or X fetching was started.'
} finally {
    foreach ($Name in $Saved.Keys) { [Environment]::SetEnvironmentVariable($Name, $Saved[$Name], 'Process') }
    if ($Held) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
