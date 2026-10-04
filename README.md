# Alpha MEME Radar

Open-source, local-first system for multi-source MEME discovery, evidence
fusion, asynchronous intelligence, and separately controlled execution.

The repository is safe-by-default: monitoring does not authorize an order,
live execution requires explicit local configuration, and credentials, wallet
material, runtime state, reports, positions, and ledgers are not part of the
source tree.

## Product Boundaries

The system has four independent paths:

1. **Monitor:** collects GMGN, OKX, 985, DeBot, Wind, Noxa, Proficy,
   DexScreener, and direct-chain events; deduplicates them by provider family;
   and promotes tokens through observable monitor stages.
2. **Monitor frontend:** displays selected candidates, details, evidence, and
   stage-specific alerts from the compact monitor snapshot.
3. **Intelligence:** enriches promoted candidates with AI, social, audit, and
   outcome evidence without blocking discovery or alerts.
4. **Execution:** applies independent chain strategy, quote, route, risk,
   sizing, order, position, and exit rules.

Monitor stages are evidence. They are not automatic orders by themselves.
Only `aggregate_early_bird` and `aggregate_confirmation` may cross the
monitor-to-execution handoff. `aggregate_discovery` remains monitor-only on
every chain.

## ARC Mainnet Monitor Boundary

ARC Mainnet is implemented as a read-only monitoring chain with chain ID
`5042` (`0x13b2`). The public-chain adapter accepts data only after an RPC
endpoint returns `eth_chainId == 0x13b2`. Its bounded, read-only RPC order is:

1. `https://rpc.blockdaemon.mainnet.arc.io`;
2. `https://rpc.drpc.mainnet.arc.io`;
3. `https://rpc.quicknode.mainnet.arc.io`;
4. `https://rpc.mainnet.arc.io`.

The adapter falls through validated endpoints for log and metadata reads. An
explorer indexer is disabled by default and is used only when
`ARC_SCAN_API_URL` is explicitly configured.

Run one isolated adapter cycle with:

```powershell
python work\alpha-pool-radar\alpha_arc_public_chain_poll.py --out-dir outputs
```

A successful cycle writes `outputs/meme-source-inbox/arc-onchain.json`,
`outputs/arc-public-chain-poll-status.json`, and
`outputs/arc-public-chain-poll-state.json`. The adapter preserves explicit
error/degraded status instead of publishing an empty successful result or
advancing a checkpoint after unresolved metadata.

Current ARC-capable evidence mapping includes the `onchain` family (ARC RPC
and Arcscan remain one family), `985`, `wind`, and `proficy`. Existing `noxa`,
`gmgn`, and `okx` family semantics are preserved for valid ARC rows, but their
fast provider requests remain inside each adapter's supported-chain filter and
no live ARC rows have been validated. DexScreener is a separate bounded market
or enrichment family. Paid visibility remains ranking evidence; only verified
address-level wallet events count as wallet evidence.

ARC is enabled by default only in the fast monitor, whose effective scope is
`bsc,robinhood,arc`. The base provider default remains
`DEFAULT_MEME_CHAINS=bsc,robinhood`, so adapters without validated ARC support
are not widened implicitly. ARC polling fails closed: unresolved log or token
metadata evidence does not publish a successful empty result or advance its
checkpoint. The monitor-only rollback override is:

```powershell
$env:ALPHA_MEME_CHAINS = 'bsc,robinhood'
```

No ARC strategy, execution input, executor, wallet, order, position, or
risk-engine route exists. BSC and Robinhood execution remain independent.

## Current Compatibility Layout

The repository is being migrated without breaking the current local launch
paths:

```text
web/                              monitor frontend
work/alpha-pool-radar/            current Python services and tests
work/alpha-pool-radar/terminal-ui trading terminal frontend
ops/local/                        read-only operations and health commands
packages/contracts/               versioned service boundary contracts
tests/integration/                repository and operations contract tests
docs/                             architecture, designs, and plans
```

The target ownership layout is documented in
`docs/architecture/system-boundaries.md`. Existing paths remain authoritative
until each service migration passes replay and contract tests.

## Quick Start

The monitor frontend can be tested and built independently:

```powershell
npm --prefix web ci
npm --prefix web run test:unit
npm --prefix web run build
```

The Python services are exercised directly from the repository:

```powershell
python -m pytest work\alpha-pool-radar -q
```

These commands do not enable live execution. Live adapters require explicit
environment-specific configuration; see
`work/alpha-pool-radar/OKX_DEX_SDK_LIVE.md`.

## Local Status

Check every component independently:

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\local\system-status.ps1
```

This command is read-only. It does not start or stop monitoring or trading.

## Tests

```powershell
python -m pytest work\alpha-pool-radar\test_alpha_meme_fast_discovery.py work\alpha-pool-radar\test_alpha_live_server.py -q
npm --prefix web run test:unit
npm --prefix web run build
powershell -ExecutionPolicy Bypass -File tests\integration\test_repository_hygiene.ps1
powershell -ExecutionPolicy Bypass -File tests\integration\test_arc_monitor_boundary.ps1
```

## Local Data

Credentials, wallet material, reports, ledgers, positions, logs, caches,
generated builds, browser profiles, and imported repositories are local-only
and excluded from Git.

## Security

Do not commit credentials, private keys, seed phrases, wallet exports, live
hostnames, or runtime output. See [SECURITY.md](SECURITY.md) for private
reporting guidance and the supported-version policy.

## License

Licensed under the [Apache License 2.0](LICENSE).
