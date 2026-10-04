Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$StatusScript = Join-Path $Root 'ops\local\system-status.ps1'
if (-not (Test-Path -LiteralPath $StatusScript)) {
    throw 'system-status.ps1 is missing'
}

$Raw = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StatusScript -Json
if ($LASTEXITCODE -ne 0) { throw 'system-status.ps1 failed' }
$Payload = $Raw | ConvertFrom-Json

foreach ($Name in @('monitor', 'frontend', 'intelligence', 'quotes', 'bsc_execution', 'robinhood_execution')) {
    if ($null -eq $Payload.$Name) { throw "missing status section: $Name" }
    if ($Payload.$Name.state -notin @('running', 'stale', 'stopped')) {
        throw "invalid state for $Name"
    }
}

$Serialized = $Payload | ConvertTo-Json -Depth 8 -Compress
foreach ($Forbidden in @('private_key', 'secret_key', 'passphrase', 'mnemonic', 'api_key')) {
    if ($Serialized -match $Forbidden) { throw "status leaked credential field: $Forbidden" }
}

Write-Output 'system-status contract passed'
