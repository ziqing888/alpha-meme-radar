[CmdletBinding()]
param([switch]$StopLegacy)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$daemon = Join-Path $PSScriptRoot 'okx-dex-executor\src\okx_dex_live_daemon.mjs'
$launcher = Join-Path $PSScriptRoot 'start_okx_dex_sdk_live.ps1'

function Test-CutoverStartTime([datetime]$Actual, [datetime]$Expected) {
    # CIM retains microseconds; Process.StartTime also includes 100ns ticks.
    return $Actual.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.ffffff') -ceq `
        $Expected.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.ffffff')
}

function Assert-CutoverChain($Row) {
    if ($null -eq $Row -or $null -eq $Row.open_positions -or $null -eq $Row.pending_orders -or
        $Row.open_positions -ne 0 -or $Row.pending_orders -ne 0) {
        throw 'Positions or unresolved local orders exist, or their state is unavailable.'
    }
    $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($Row.updated_at)).TotalSeconds
    if ($age -lt 0 -or $age -gt 20 -or $Row.activity_status -ne 'waiting_for_strategy_candidate') {
        throw 'Worker is not recently observed waiting for candidates. Leave it running and inspect the terminal.'
    }
}

function Get-CutoverNonces($Config, [string]$Chain) {
    $expected = if ($Chain -eq 'bsc') { 56 } else { 4663 }
    $requests = @(
        @{jsonrpc='2.0'; id=1; method='eth_chainId'; params=@()},
        @{jsonrpc='2.0'; id=2; method='eth_getTransactionCount'; params=@($Config.wallet_address, 'latest')},
        @{jsonrpc='2.0'; id=3; method='eth_getTransactionCount'; params=@($Config.wallet_address, 'pending')}
    )
    try {
        $url = $Config.($Chain + '_rpc_url')
        $response = Invoke-RestMethod -Uri $url -Method Post -ContentType 'application/json' `
            -Body ($requests | ConvertTo-Json -Depth 4 -Compress) -TimeoutSec 8
        $values = @{}
        foreach ($row in $response) {
            if ($row.PSObject.Properties['error'] -or $row.result -notmatch '^0x[0-9a-fA-F]+$' -or
                $values.ContainsKey([int]$row.id)) { throw 'invalid_rpc' }
            $values[[int]$row.id] = [Convert]::ToInt64($row.result.Substring(2), 16)
        }
        if ($values.Count -ne 3 -or $values[1] -ne $expected -or $values[2] -ne $values[3]) {
            throw 'pending_or_chain_mismatch'
        }
        return $values[2]
    } catch { throw "Cannot confirm zero pending transactions for $Chain. No process was stopped by this check." }
}

function Get-CutoverTargets {
    $configPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.config\alpha-radar\okx-live-config.json'
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $runtime = Invoke-RestMethod 'http://127.0.0.1:8771/api/terminal/runtime' -TimeoutSec 8
    $snapshot = Invoke-RestMethod 'http://127.0.0.1:8771/api/terminal/snapshot' -TimeoutSec 8
    if (-not $runtime.verified -or $runtime.unassigned_workers -ne 0 -or $snapshot.wallet -ine $config.wallet_address) {
        throw 'Process or wallet identity is unverified.'
    }
    $targets = @()
    foreach ($chain in @('bsc','robinhood')) {
        $rows = @($runtime.runtime | Where-Object { $_.chain -eq $chain })
        if ($rows.Count -eq 0) { continue }
        $control = @($runtime.controls | Where-Object { $_.chain -eq $chain })
        if ($rows.Count -ne 1 -or $control.Count -ne 1 -or $control[0].state -ne 'legacy_worker') {
            throw "Not exactly one legacy worker for $chain. Use the terminal for upgraded workers."
        }
        $chainRows = @($snapshot.chains | Where-Object { $_.chain -eq $chain })
        if ($chainRows.Count -ne 1) { throw 'Chain state unavailable.' }
        Assert-CutoverChain $chainRows[0]
        $worker = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $rows[0].pid)
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $rows[0].parent_pid)
        if ($worker.Name -ne 'node.exe' -or $worker.ParentProcessId -ne $parent.ProcessId -or
            $worker.CommandLine.IndexOf($daemon, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
            $parent.CommandLine.IndexOf($launcher, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw 'Worker ownership does not match this checkout.'
        }
        $nonce = Get-CutoverNonces $config $chain
        $targets += [pscustomobject]@{Chain=$chain; ProcessId=$worker.ProcessId; Created=$worker.CreationDate;
            AmountUsd=$rows[0].amount_usd; ConfirmedNonce=$nonce; ObservedPositions=0; ObservedPending=0}
    }
    return $targets
}

if ($MyInvocation.InvocationName -eq '.') { return }
$targets = @(Get-CutoverTargets)
$targets | Format-Table Chain,ProcessId,AmountUsd,ObservedPositions,ObservedPending,ConfirmedNonce
if (-not $StopLegacy) {
    Write-Host 'CHECK ONLY. No process or trading setting changed.'
    Write-Host 'This is a point-in-time observation, not a reservation of worker idle state.'
    exit 0
}
if ($targets.Count -eq 0) { Write-Host 'No running legacy worker found. Nothing stopped.'; exit 0 }
Write-Host 'Stopping a legacy worker is not a graceful drain. A just-submitted transaction may still settle.'
Write-Host 'This command does NOT start trading. Recheck positions and pending transactions after stopping.'
$answer = Read-Host 'To stop ONLY the listed legacy workers, type STOP LEGACY'
if ($answer -cne 'STOP LEGACY') { Write-Host 'Cancelled. No process stopped.'; exit 0 }
$current = @(Get-CutoverTargets)
if ($current.Count -ne $targets.Count) { throw 'Worker set changed. Run the check again.' }
foreach ($target in $targets) {
    $match = @($current | Where-Object { $_.Chain -eq $target.Chain -and $_.ProcessId -eq $target.ProcessId -and $_.Created -eq $target.Created })
    if ($match.Count -ne 1) { throw 'Worker identity changed. Nothing stopped.' }
}
foreach ($target in $targets) {
    # Recheck process creation time immediately before targeting a numeric PID.
    $process = Get-Process -Id $target.ProcessId -ErrorAction Stop
    if (-not (Test-CutoverStartTime $process.StartTime $target.Created)) {
        throw 'Process ID was reused. Stop aborted.'
    }
    Stop-Process -InputObject $process -ErrorAction Stop
    $process.WaitForExit()
    Write-Host ($target.Chain + ': legacy worker stopped.')
}
Write-Host 'No replacement worker started. Inspect the terminal, then use Strategy > Start for each chain.'
