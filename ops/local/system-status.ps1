[CmdletBinding()]
param([switch]$Json)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Outputs = Join-Path $Root 'outputs'
$Now = Get-Date
$Processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.CommandLine })

function Read-StatusFile {
    param([string]$Name)
    $Path = Join-Path $Outputs $Name
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Get-UpdateAgeSeconds {
    param($Payload)
    if ($null -eq $Payload) { return $null }
    foreach ($Field in @('updated_at', 'finished_at', 'generated_at', 'observed_at')) {
        $Property = $Payload.PSObject.Properties[$Field]
        if ($null -eq $Property -or [string]::IsNullOrWhiteSpace([string]$Property.Value)) { continue }
        try {
            $Stamp = [DateTimeOffset]::Parse([string]$Property.Value)
            return [math]::Max(0, [math]::Round(([DateTimeOffset]$Now - $Stamp).TotalSeconds, 1))
        } catch { continue }
    }
    return $null
}

function Get-WorkerSection {
    param(
        [string]$Pattern,
        [string]$StatusFile = '',
        [int]$MaxAgeSeconds = 0
    )
    $Matches = @($Processes | Where-Object { $_.CommandLine -match $Pattern })
    $Payload = if ($StatusFile) { Read-StatusFile $StatusFile } else { $null }
    $Age = Get-UpdateAgeSeconds $Payload
    $State = if (-not $Matches.Count) {
        'stopped'
    } elseif ($MaxAgeSeconds -gt 0 -and ($null -eq $Age -or $Age -gt $MaxAgeSeconds)) {
        'stale'
    } else {
        'running'
    }
    return [ordered]@{
        state = $State
        process_count = $Matches.Count
        process_ids = @($Matches | Select-Object -ExpandProperty ProcessId)
        update_age_seconds = $Age
        data_ok = if ($null -ne $Payload -and $null -ne $Payload.PSObject.Properties['ok']) { [bool]$Payload.ok } else { $null }
    }
}

function Get-PortSection {
    param([int]$Port)
    $Listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    return [ordered]@{
        state = if ($Listeners.Count) { 'running' } else { 'stopped' }
        process_count = @($Listeners | Select-Object -ExpandProperty OwningProcess -Unique).Count
        process_ids = @($Listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        update_age_seconds = $null
        data_ok = if ($Listeners.Count) { $true } else { $null }
        url = "http://127.0.0.1:$Port/"
    }
}

$Payload = [ordered]@{
    generated_at = [DateTimeOffset]$Now
    monitor = Get-WorkerSection '(^|[\\/])alpha_meme_fast_discovery\.py([\s"'']|$)' 'alpha-meme-fast-discovery-status.json' 20
    frontend = Get-PortSection 5173
    intelligence = Get-WorkerSection '(^|[\\/])alpha_meme_live_refresh\.py([\s"'']|$)' 'alpha-meme-live-refresh-status.json' 180
    quotes = Get-WorkerSection '(^|[\\/])alpha_fast_track\.py([\s"'']|$)' 'alpha-fast-track-status.json' 15
    bsc_execution = Get-WorkerSection 'okx_dex_live_daemon\.mjs.*--chain\s+bsc([\s"'']|$)'
    robinhood_execution = Get-WorkerSection 'okx_dex_live_daemon\.mjs.*--chain\s+robinhood([\s"'']|$)'
}

if ($Json) {
    $Payload | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

$Payload.GetEnumerator() |
    Where-Object { $_.Key -ne 'generated_at' } |
    ForEach-Object {
        [pscustomobject]@{
            Component = $_.Key
            State = $_.Value.state
            Processes = $_.Value.process_count
            UpdateAgeSeconds = $_.Value.update_age_seconds
        }
    } |
    Format-Table -AutoSize
