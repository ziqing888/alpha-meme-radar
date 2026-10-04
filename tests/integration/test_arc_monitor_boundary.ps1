Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Radar = Join-Path $Root 'work\alpha-pool-radar'
$Web = Join-Path $Root 'web'
$Esbuild = Join-Path $Web 'node_modules\.bin\esbuild.cmd'
$Scratch = Join-Path ([IO.Path]::GetTempPath()) ('arc-monitor-boundary-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Scratch | Out-Null
$PreviousPythonPath = $env:PYTHONPATH

try {
    if (-not (Test-Path -LiteralPath $Esbuild)) {
        throw 'web dependencies are missing; run npm install in web before this integration test'
    }

    $PythonHarnessPath = Join-Path $Scratch 'arc_boundary.py'
    $PythonHarness = @'
import json
import sys
from pathlib import Path

from alpha_chain_strategy import StrategyPolicy
from alpha_meme_fast_discovery import publish_execution_inputs_from_fast_snapshot
from alpha_monitor_v3 import compact_monitor_snapshot, normalize_provider_event, update_monitor_state


NOW = "2026-09-16T03:00:00+00:00"
DISCOVERY = "0x" + ("a" * 40)
EARLY = "0x" + ("b" * 40)
CONFIRMATION = "0x" + ("c" * 40)


def accepted_event(address, source, event_id, **raw_overrides):
    raw = {
        "chainId": "0x13b2",
        "event_at": NOW,
        "provider_event_id": event_id,
        **raw_overrides,
    }
    event, rejection = normalize_provider_event(
        raw,
        source=source,
        chain="5042",
        contract_address=address,
        observed_at=NOW,
    )
    assert rejection is None, rejection
    assert event is not None
    assert event["chain"] == "arc"
    assert event["contract_address"] == address
    return event


def market(address, *, market_cap=40_000, liquidity=12_000, holders=40):
    return {
        "chain": "arc-mainnet",
        "contract_address": address,
        "observed_at": NOW,
        "market_cap_usd": market_cap,
        "liquidity_usd": liquidity,
        "holders": holders,
    }


def main(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    events = [
        accepted_event(DISCOVERY, "arc_rpc", "arc-discovery"),
        accepted_event(EARLY, "arc_rpc", "arc-early-rpc"),
        accepted_event(
            EARLY,
            "985_monitor",
            "arc-early-985",
            monitor985_kind="dex_paid",
        ),
        accepted_event(CONFIRMATION, "arc_rpc", "arc-confirm-rpc"),
        accepted_event(CONFIRMATION, "wind_monitor", "arc-confirm-wind"),
        accepted_event(CONFIRMATION, "proficy_trending", "arc-confirm-proficy"),
    ]
    snapshot = update_monitor_state(
        None,
        events=events,
        candidates=[
            market(EARLY),
            market(CONFIRMATION, market_cap=500_000, liquidity=8_000, holders=20),
        ],
        rejections=[],
        source_health={
            "arc_onchain": {"status": "ok", "observed_at": NOW},
            "985": {"status": "ok", "observed_at": NOW},
            "wind": {"status": "ok", "observed_at": NOW},
            "proficy": {"status": "ok", "observed_at": NOW},
        },
        observed_at=NOW,
    )
    by_address = {
        token["identity"]["contract_address"]: token
        for token in snapshot["tokens"]
    }
    assert set(by_address) == {DISCOVERY, EARLY, CONFIRMATION}
    assert by_address[DISCOVERY]["primary_state"] == "new"
    assert by_address[EARLY]["primary_state"] == "building"
    assert by_address[CONFIRMATION]["primary_state"] == "resonating"
    assert by_address[EARLY]["resonance"]["provider_families"] == ["985", "onchain"]
    assert by_address[CONFIRMATION]["resonance"]["family_count"] == 3
    assert by_address[CONFIRMATION]["resonance"]["confirmation_gate"]["eligible"] is True

    snapshot_path = output_dir / "monitor-v3-compact.json"
    snapshot_path.write_text(
        json.dumps(compact_monitor_snapshot(snapshot), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    execution_dir = output_dir / "execution"
    execution_dir.mkdir()
    controls = [
        {"chain": "bsc", "contract_address": "0x" + ("d" * 40), "symbol": "BSC"},
        {"chain": "robinhood", "contract_address": "0x" + ("e" * 40), "symbol": "RH"},
    ]
    result = publish_execution_inputs_from_fast_snapshot(
        out_dir=execution_dir,
        potential=[
            {"chain": "arc", "contract_address": DISCOVERY, "symbol": "ARC-D"},
            {"chain": "5042", "contract_address": EARLY, "symbol": "ARC-E"},
            {"chain": "0x13b2", "contract_address": CONFIRMATION, "symbol": "ARC-C"},
            *controls,
        ],
        replay={},
        generated_at=NOW,
    )
    assert result["published"] is True
    assert result["candidate_count"] == 2
    serialized_inputs = {}
    for filename in ("bsc-execution-input.json", "robinhood-execution-input.json"):
        path = execution_dir / filename
        assert path.exists(), filename
        serialized_inputs[filename] = path.read_text(encoding="utf-8")
    assert controls[0]["contract_address"] in serialized_inputs["bsc-execution-input.json"]
    assert controls[1]["contract_address"] in serialized_inputs["robinhood-execution-input.json"]
    for filename, serialized in serialized_inputs.items():
        for address in (DISCOVERY, EARLY, CONFIRMATION):
            assert address not in serialized, (filename, address)
    assert not (execution_dir / "arc-execution-input.json").exists()

    try:
        StrategyPolicy.for_chain("arc")
    except ValueError as exc:
        assert str(exc) == "unsupported_chain:arc"
    else:
        raise AssertionError("StrategyPolicy unexpectedly accepted ARC")

    print(json.dumps({
        "snapshot": str(snapshot_path),
        "discovery": DISCOVERY,
        "early_bird": EARLY,
        "confirmation": CONFIRMATION,
    }))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
'@
    [IO.File]::WriteAllText(
        $PythonHarnessPath,
        $PythonHarness,
        [Text.UTF8Encoding]::new($false)
    )

    $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($PreviousPythonPath)) {
        $Radar
    } else {
        $Radar + [IO.Path]::PathSeparator + $PreviousPythonPath
    }
    $PythonOutput = & python $PythonHarnessPath $Scratch
    if ($LASTEXITCODE -ne 0) { throw 'ARC backend boundary harness failed' }
    $Backend = ($PythonOutput | Select-Object -Last 1) | ConvertFrom-Json

    $SelectorPath = (Join-Path $Web 'src\monitor-v3\selectors.ts').Replace('\', '/')
    $FrontendHarnessPath = Join-Path $Scratch 'arc_boundary_frontend.ts'
    $FrontendBundlePath = Join-Path $Scratch 'arc_boundary_frontend.cjs'
    $FrontendHarness = @"
import fs from 'node:fs';
import { selectMonitorRows } from '$SelectorPath';

const [snapshotPath, discovery, earlyBird, confirmation] = process.argv.slice(2);
const snapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf8'));
const selected = selectMonitorRows(snapshot, 'focus', '', 'arc');
const selectedIds = selected.map((token) => token.id);
const expectedIds = new Set(['arc:' + earlyBird, 'arc:' + confirmation]);

if (selected.length !== 2 || selectedIds.some((id) => !expectedIds.has(id))) {
  throw new Error('unexpected ARC selected overview: ' + JSON.stringify(selectedIds));
}
if (selectedIds.includes('arc:' + discovery)) {
  throw new Error('ARC aggregate discovery leaked into the selected overview');
}
if (!selected.some((token) => token.primary_state === 'building')) {
  throw new Error('ARC aggregate early-bird is missing from the selected overview');
}
if (!selected.some((token) => token.primary_state === 'resonating')) {
  throw new Error('ARC aggregate confirmation is missing from the selected overview');
}
console.log('ARC frontend selected-overview boundary passed');
"@
    [IO.File]::WriteAllText(
        $FrontendHarnessPath,
        $FrontendHarness,
        [Text.UTF8Encoding]::new($false)
    )

    & $Esbuild $FrontendHarnessPath --bundle --platform=node --format=cjs --outfile=$FrontendBundlePath --log-level=warning
    if ($LASTEXITCODE -ne 0) { throw 'ARC frontend boundary harness failed to build' }
    & node $FrontendBundlePath $Backend.snapshot $Backend.discovery $Backend.early_bird $Backend.confirmation
    if ($LASTEXITCODE -ne 0) { throw 'ARC frontend boundary harness failed' }

    Write-Output 'ARC monitor boundary passed'
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
    Remove-Item -LiteralPath $Scratch -Recurse -Force
}
