[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'alpha_terminal_server.py'
$oldScript = Join-Path $PSScriptRoot 'alpha_live_dashboard.py'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw 'Dashboard source is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'terminal-ui\dist\index.html'))) {
    throw 'Terminal build is missing. Run npm run build inside terminal-ui first.'
}
$python = & py -3 -I -B -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or @($python).Count -ne 1 -or -not (Test-Path -LiteralPath $python)) {
    throw 'Python 3 is unavailable.'
}
$listeners = @(Get-NetTCPConnection -LocalPort 8771 -State Listen -ErrorAction SilentlyContinue)
foreach ($owner in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
    $full = ($process.CommandLine -like ('*' + $script + '*')) -or ($process.CommandLine -like ('*' + $oldScript + '*'))
    $legacy = $process.CommandLine -match '-B -u work[\\/]alpha-pool-radar[\\/]alpha_live_dashboard\.py\s*$'
    if ($process.Name -ne 'python.exe' -or -not ($full -or $legacy)) {
        throw 'Port 8771 belongs to another application. Nothing was stopped.'
    }
    try {
        $test = Invoke-RestMethod -Uri 'http://127.0.0.1:8771/roundtrip/status.json' -TimeoutSec 3
        if ($test.roundtrip.running) { throw 'A test process is running; leave this dashboard open.' }
    }
    catch {
        # A 404 is the old read-only dashboard. Other failures do not permit restart.
        if (-not $_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 404) { throw }
    }
    Stop-Process -Id $owner -ErrorAction Stop
}
$child = Start-Process -FilePath $python -ArgumentList @('-B', '-u', ('"' + $script + '"')) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 200
    if ($child.HasExited) { throw 'Dashboard exited during startup.' }
    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8771/api/terminal/session' -TimeoutSec 1
        if ($null -ne $status.csrf_token) {
            Write-Host 'Dashboard ready: http://127.0.0.1:8771/'
            Write-Host 'Terminal only. Existing trading workers and positions were not changed.'
            exit 0
        }
    }
    catch { }
}
throw 'Dashboard did not become ready. No trade process was started.'
