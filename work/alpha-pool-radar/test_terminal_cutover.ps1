$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'prepare_terminal_cutover.ps1')

$script:CheckCount = 0

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:CheckCount++
}

function Assert-Throws([scriptblock]$Code) {
    $thrown = $false
    try { & $Code } catch { $thrown = $true }
    Assert-True $thrown 'Expected rejection.'
}

function Read-RequiredText([string]$RelativePath) {
    $path = Join-Path $PSScriptRoot $RelativePath
    Assert-True (Test-Path -LiteralPath $path -PathType Leaf) "Missing cutover dependency: $RelativePath"
    $text = Get-Content -LiteralPath $path -Raw
    Assert-True (-not [string]::IsNullOrWhiteSpace($text)) "Empty cutover dependency: $RelativePath"
    return $text
}

function Assert-Matches([string]$Text, [string]$Pattern, [string]$Message) {
    Assert-True ([regex]::IsMatch($Text, $Pattern, [Text.RegularExpressions.RegexOptions]::Singleline)) $Message
}

function Assert-NotMatches([string]$Text, [string]$Pattern, [string]$Message) {
    Assert-True (-not [regex]::IsMatch($Text, $Pattern, [Text.RegularExpressions.RegexOptions]::Singleline)) $Message
}

# Point-in-time legacy drain helpers remain fail-closed. RPC is stubbed below.
$idle = [pscustomobject]@{open_positions=0;pending_orders=0;updated_at=[DateTimeOffset]::UtcNow.ToString('o');activity_status='waiting_for_strategy_candidate'}
Assert-CutoverChain $idle
Assert-True $true 'Idle chain should be accepted.'
Assert-Throws { Assert-CutoverChain $null }
$idle.open_positions = 1
Assert-Throws { Assert-CutoverChain $idle }
$idle.open_positions = 0
$idle.pending_orders = 1
Assert-Throws { Assert-CutoverChain $idle }
$idle.pending_orders = 0
$idle.updated_at = [DateTimeOffset]::UtcNow.AddMinutes(-1).ToString('o')
Assert-Throws { Assert-CutoverChain $idle }
$idle.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
$idle.activity_status = 'buying'
Assert-Throws { Assert-CutoverChain $idle }
$date = [datetime]::Parse('2026-09-09T02:46:42.1227960Z').ToUniversalTime()
Assert-True (Test-CutoverStartTime $date.AddTicks(6) $date) 'CIM precision mismatch.'
Assert-True (-not (Test-CutoverStartTime $date.AddSeconds(1) $date)) 'PID reuse accepted.'

function Invoke-RestMethod { param($Uri,$Method,$ContentType,$Body,$TimeoutSec); return $script:rpcResponse }
$script:rpcResponse = @([pscustomobject]@{id=1;result='0x38'},[pscustomobject]@{id=2;result='0xa'},[pscustomobject]@{id=3;result='0xa'})
$cfg = [pscustomobject]@{wallet_address=('0x'+('1'*40));bsc_rpc_url='https://fixture.invalid'}
Assert-True ((Get-CutoverNonces $cfg 'bsc') -eq 10) 'Unexpected nonce.'
$script:rpcResponse[2].result = '0xb'
Assert-Throws { Get-CutoverNonces $cfg 'bsc' }
$script:rpcResponse[2].result = '0xa'
$script:rpcResponse[0].result = '0x1'
Assert-Throws { Get-CutoverNonces $cfg 'bsc' }
$script:rpcResponse = @([pscustomobject]@{id=1;result='0x38'},[pscustomobject]@{id=2;result='0xa'},[pscustomobject]@{id=2;result='0xa'})
Assert-Throws { Get-CutoverNonces $cfg 'bsc' }

# Static chain_v2 integration. These checks read source/build artifacts only.
$strategy = Read-RequiredText 'alpha_chain_strategy.py'
$fastTrack = Read-RequiredText 'alpha_fast_track.py'
$execution = Read-RequiredText 'alpha_terminal_execution.py'
$terminalData = Read-RequiredText 'alpha_terminal_data.py'
$daemon = Read-RequiredText 'okx-dex-executor\src\okx_dex_live_daemon.mjs'

Assert-Matches $strategy 'STRATEGY_VERSION\s*=\s*["'']chain_v2["'']' 'Python strategy version is not chain_v2.'
Assert-Matches $strategy 'chain="robinhood".*?live_signal_stage="aggregate_early_bird".*?entry_route="robinhood_aggregate_early_bird".*?order_notional_usd=2\.0' 'Robinhood Python V2 policy drifted.'
Assert-Matches $strategy 'chain="bsc".*?live_signal_stage="aggregate_early_bird".*?entry_route="bsc_aggregate_early_bird".*?order_notional_usd=1\.0' 'BSC Python V2 policy drifted.'

Assert-Matches $fastTrack '["'']strategy_version["'']:\s*STRATEGY_VERSION' 'Fast Track does not publish the V2 envelope.'
Assert-Matches $fastTrack '["'']order_authorized["'']:\s*False' 'Fast Track must not authorize signing.'
Assert-Matches $fastTrack '["'']requires_executor_tradeability_check["'']:\s*True' 'Fast Track must require the executor tradeability check.'
Assert-Matches $fastTrack '["'']signals["'']:\s*signals' 'Fast Track live signals are missing.'
Assert-Matches $fastTrack '["'']shadow_signals["'']:\s*shadow_signals' 'Fast Track shadow signals are not separate.'
Assert-Matches $fastTrack '["'']rejections["'']:\s*rejections' 'Fast Track rejection stream is missing.'

Assert-Matches $execution '["'']bsc["'']:\s*\{["'']amount_usd["'']:\s*["'']1["''].*?["'']signal_stage["'']:\s*["'']aggregate_early_bird["''].*?["'']entry_route["'']:\s*["'']bsc_aggregate_early_bird["'']' 'Terminal BSC fixed amount/stage/route drifted.'
Assert-Matches $execution '["'']robinhood["'']:\s*\{["'']amount_usd["'']:\s*["'']2["''].*?["'']signal_stage["'']:\s*["'']aggregate_early_bird["''].*?["'']entry_route["'']:\s*["'']robinhood_aggregate_early_bird["'']' 'Terminal Robinhood fixed amount/stage/route drifted.'
Assert-Matches $execution 'f''\.terminal-control-\{chain\}\.lock''' 'Execution controls do not use a chain-local lock.'
Assert-NotMatches $execution 'self\.root\s*/\s*[''"]\.terminal-control\.lock[''"]' 'Execution controls still use an ambiguous shared lock.'
Assert-Matches $execution 'f''\{STEMS\[chain\]\}-\{kind\}\.json''' 'Execution command/status paths are not chain-specific.'
Assert-Matches $execution 'configured_amount_usd[''"]?\s*[:=]' 'Configured amount is not projected separately.'
Assert-Matches $execution 'effective_amount_usd[''"]?\s*[:=]' 'Effective amount is not projected separately.'
Assert-Matches $execution 'executor_acknowledged_at[''"]?\s*[:=]' 'Executor acknowledgment time is missing.'
Assert-Matches $execution 'receipt\.get\([''"]strategy_version[''"]\).*?receipt\.get\([''"]signal_stage[''"]\).*?receipt\.get\([''"]entry_route[''"]\)' 'Effective strategy does not require the full daemon acknowledgment.'
Assert-Matches $execution 'raw\.get\([''"]pid[''"]\)\s*!=\s*pid.*?raw\.get\([''"]chain[''"]\)\s*!=\s*chain.*?wallet' 'Effective receipt is not bound to PID, chain, and wallet.'
Assert-Matches $terminalData 'projected\[[''"]configured_strategy[''"]\]\s*=\s*_chain_strategy\(chain\).*?projected\[[''"]effective_strategy[''"]\]\s*=\s*None' 'Configured policy is being presented as an effective policy.'

Assert-Matches $daemon 'chain:\s*''robinhood''.*?signalStage:\s*''aggregate_early_bird''.*?entryRoute:\s*''robinhood_aggregate_early_bird''.*?amountUsd:\s*2' 'Node Robinhood V2 policy drifted.'
Assert-Matches $daemon 'chain:\s*''bsc''.*?signalStage:\s*''aggregate_early_bird''.*?entryRoute:\s*''bsc_aggregate_early_bird''.*?amountUsd:\s*1' 'Node BSC V2 policy drifted.'
Assert-Matches $daemon 'function fixedAmountMatches\(terminal, chain\).*?receipt\.amount_native_atomic\s*===\s*null.*?amount_usd.*?amountUsd' 'Daemon does not bind V2 effectiveness to the fixed chain amount.'
Assert-Matches $daemon 'function attachEffectiveStrategy\(result, terminal, chain.*?result\.terminal_control\s*=\s*\{\s*\.\.\.terminal\.receipt\(\),\s*\.\.\.fields\s*\}' 'Daemon receipt does not acknowledge the effective V2 policy.'
Assert-Matches $daemon 'strictV2\s*&&\s*input\?\.strategy_version\s*!==\s*STRATEGY_VERSION' 'Daemon does not reject non-V2 input by default.'

$dist = Join-Path $PSScriptRoot 'terminal-ui\dist'
$indexPath = Join-Path $dist 'index.html'
Assert-True (Test-Path -LiteralPath $indexPath -PathType Leaf) 'Terminal production build index is missing.'
$index = Get-Content -LiteralPath $indexPath -Raw
$assetRefs = [regex]::Matches($index, '(?:src|href)="/assets/([^"?]+)"')
Assert-True ($assetRefs.Count -ge 2) 'Terminal production build does not reference JS and CSS assets.'
foreach ($match in $assetRefs) {
    $asset = Join-Path (Join-Path $dist 'assets') $match.Groups[1].Value
    Assert-True ((Test-Path -LiteralPath $asset -PathType Leaf) -and (Get-Item -LiteralPath $asset).Length -gt 0) "Missing or empty terminal build asset: $asset"
}

Write-Host ($script:CheckCount.ToString() + ' cutover checks passed. Source and build were read only; no process control, live RPC, or trade executed.')
