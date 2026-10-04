[CmdletBinding()]
param(
    [ValidateSet('bsc', 'robinhood')]
    [string]$Chain = 'robinhood',
    [Parameter(Mandatory = $true)]
    [string]$RemoteHost,
    [string]$RemoteUser = 'alpha-radar',
    [Parameter(Mandatory = $true)]
    [string]$IdentityFile,
    [ValidateRange(2, 300)]
    [int]$IntervalSeconds = 3,
    [ValidateRange(1, 100)]
    [int]$PullEveryCycles = 5,
    [switch]$Once
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$outputRoot = Join-Path $repoRoot 'outputs'
$remoteRoot = '/var/lib/alpha-radar/outputs'
$stem = if ($Chain -eq 'bsc') { 'okx-dex-sdk-live' } else { 'okx-dex-sdk-robinhood-live' }
$inputName = "$Chain-execution-input.json"
$stateNames = @("$stem-state.json", "$stem-status.json")
$statusPath = Join-Path $outputRoot "remote-$Chain-sync-status.json"
$mutexName = 'Local\AlphaRadarRemoteSync-' + $Chain

function Invoke-Ssh([string]$Command) {
    $result = & ssh -i $IdentityFile -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=2 `
        -o ServerAliveInterval=3 -o ServerAliveCountMax=2 `
        "$RemoteUser@$RemoteHost" $Command 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ssh_failed: $result" }
    return $result
}

function Invoke-SshInput([string]$Command, [string]$InputText) {
    $sshPath = (Get-Command ssh -ErrorAction Stop).Source
    $escapedCommand = $Command.Replace('"', '\"')
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $sshPath
    $start.Arguments = "-i `"$IdentityFile`" -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=2 -o ServerAliveInterval=3 -o ServerAliveCountMax=2 $RemoteUser@$RemoteHost `"$escapedCommand`""
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'ssh_input_start_failed' }
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($InputText + "`n")
        $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(25000)) {
            $process.Kill()
            throw 'ssh_input_timeout'
        }
        $output = $process.StandardOutput.ReadToEnd()
        $errorText = $process.StandardError.ReadToEnd()
        if ($process.ExitCode -ne 0) { throw "ssh_input_failed: $errorText" }
        return $output
    }
    finally {
        $process.Dispose()
    }
}

function Assert-JsonFile([string]$Path, [int]$MaxBytes) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.Length -le 0 -or $item.Length -gt $MaxBytes) { throw "invalid_file_size: $Path" }
    Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
}

function Push-Input {
    $source = Join-Path $outputRoot $inputName
    Assert-JsonFile $source 2097152
    $temporary = "$remoteRoot/.$inputName.upload.$PID"
    $encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($source))
    Invoke-SshInput "set -e; base64 -d > '$temporary'; chown alpha-radar:alpha-radar '$temporary'; chmod 0640 '$temporary'; mv -f '$temporary' '$remoteRoot/$inputName'" $encoded | Out-Null
}

function Publish-LocalFile([string]$Temporary, [string]$Destination) {
    Move-Item -LiteralPath $Temporary -Destination $Destination -Force
}

function Pull-State {
    $names = $stateNames -join ' '
    $command = "for name in $names; do " + 'path=' + $remoteRoot + '/$name; if test -f $path; then printf ''%s:'' $name; base64 -w0 $path; printf ''\n''; fi; done'
    $rows = Invoke-Ssh $command
    foreach ($row in @($rows)) {
        if ($row -notmatch '^([^:]+):([A-Za-z0-9+/=]+)$') { continue }
        $name = $matches[1]
        if ($name -notin $stateNames) { throw 'unexpected_remote_state' }
        $temporary = Join-Path $outputRoot ".$name.remote.$PID.tmp"
        try {
            [IO.File]::WriteAllBytes($temporary, [Convert]::FromBase64String($matches[2]))
            Assert-JsonFile $temporary 8388608
            Publish-LocalFile $temporary (Join-Path $outputRoot $name)
        }
        finally {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-Status([bool]$Ok, [string]$ErrorText = '') {
    $payload = [ordered]@{
        ok = $Ok
        chain = $Chain
        remote_host = $RemoteHost
        updated_at = [DateTimeOffset]::UtcNow.ToString('o')
        interval_seconds = $IntervalSeconds
        error = $ErrorText
    }
    $temporary = "$statusPath.$PID.tmp"
    try {
        [IO.File]::WriteAllText($temporary, (($payload | ConvertTo-Json) + "`n"), [Text.UTF8Encoding]::new($false))
        Publish-LocalFile $temporary $statusPath
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw 'ssh_identity_missing' }
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$mutex = [Threading.Mutex]::new($false, $mutexName)
$held = $false
try {
    try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { throw 'remote_sync_already_running' }
    $cycle = 0
    while ($true) {
        $started = Get-Date
        try {
            Push-Input
            if (($cycle % $PullEveryCycles) -eq 0) { Pull-State }
            Write-Status $true
        }
        catch {
            Write-Status $false $_.Exception.Message
            if ($Once) { throw }
        }
        $cycle++
        if ($Once) { break }
        $elapsed = ((Get-Date) - $started).TotalSeconds
        Start-Sleep -Milliseconds ([Math]::Max(250, [int](1000 * ($IntervalSeconds - $elapsed))))
    }
}
finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
