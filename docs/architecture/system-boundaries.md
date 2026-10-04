# System Boundaries

## Data Flow

```text
provider feeds
    -> monitor adapters
    -> normalized event ledger
    -> resonance and stage projection
    -> compact monitor snapshot
       -> monitor frontend
       -> stage-specific alerts
       -> versioned execution signal

promoted candidates
    -> bounded intelligence queue
    -> intelligence overlay
    -> monitor detail view

versioned execution signal
    -> chain strategy gate
    -> selected-candidate quote
    -> route and impact validation
    -> order and position service
```

## Ownership

### Monitor

Owns source adapters, event identity, timestamps, provider-family
deduplication, signal lanes, flow snapshots, stages, and alert transitions.
The fast publication path never reads the historical full report.
Explicit market-behavior failures are downgraded to `trend_watch`; missing
behavior data remains `unknown` and is never fabricated as a pass.

ARC Mainnet (`5042` / `0x13b2`) belongs only to this monitor boundary. Its
read-only adapter, normalized evidence, stage projection, alerts, outcome
tracking, compact snapshot, and frontend presentation may use the same
discovery, early-bird, and confirmation model as the other monitor chains.
ARC RPC and Arcscan evidence share the single `onchain` provider family and
cannot manufacture cross-provider resonance.

Code capability is not runtime activation. ARC remains outside the default
`bsc,robinhood` monitor scope because the latest one-shot validated the chain
ID but failed closed on ERC-20 metadata `execution reverted` responses and an
HTTP 429 from `eth_getCode`; no inbox or checkpoint was produced. Enabling ARC
requires successful live metadata validation and explicit approval before any
monitor-side restart. Removing `arc` through
`ALPHA_MEME_CHAINS=bsc,robinhood` is the monitor-only rollback; it does not
change BSC or Robinhood execution state.

### Monitor Frontend

Owns display, search, stage tabs, token detail, sound, speech, and desktop
notification preferences. It reads the compact monitor snapshot first and
applies intelligence as an optional overlay.

### Intelligence

Owns AI narrative, official-CA and social evidence, wash analysis, wallet
quality, audit reconciliation, and outcome jobs. Missing or slow intelligence
cannot stop monitor publication, alerts, or execution.

Only fresh `building`, `resonating`, `smart_cluster`, and `revival` candidates
enter the bounded external-research queue. `trend_watch` and `blocked_risk`
tokens remain visible without consuming AI calls.

### Execution

Owns accepted stages, strategy thresholds, quote freshness, route validation,
price impact, order notional, submission, reconciliation, positions, and
exits. It does not mutate monitor state.
The monitor hands off only `aggregate_early_bird` and
`aggregate_confirmation`; exact quotes are requested after this handoff.

Execution supports only its explicitly configured BSC and Robinhood paths.
ARC is outside every execution boundary: it has no strategy policy, execution
input, quote or route adapter, risk-engine policy, wallet or approval path,
order submission, reconciliation, position, or exit service. ARC rows are
removed before strategy annotation and before `bsc-execution-input.json` and
`robinhood-execution-input.json` are built; `arc-execution-input.json` must not
exist. `StrategyPolicy.for_chain("arc")` remains unsupported.

### Outcome Ledger

The fast monitor writes `alpha-monitor-outcomes-latest.json`. It separates
selected, market-behavior-downgraded, and hard-blocked cohorts and records
5m, 15m, 30m, 1h, 2h, 6h, and 24h market-cap returns. This ledger is the basis
for false-positive and missed-opportunity tuning.

## Target Monorepo Ownership

```text
apps/monitor-web
apps/trading-terminal
services/monitor
services/intelligence
services/execution
packages/contracts
packages/fixtures
ops/local
ops/cloud
tests/integration
runtime
```

Migration is additive. A path moves only after its public contract and replay
behavior are covered by tests. Compatibility launchers keep the current local
paths working until the final cutover.

## Latency Budget

- connected provider event to compact snapshot: target under 2 seconds;
- frontend fallback polling: 2 seconds;
- local alert dispatch: same monitor transition cycle;
- AI and deep audit: asynchronous and outside the fast-path budget;
- exact quote and tradeability: requested only for strategy-selected tokens.

Every service reports its own process state and data age. A running frontend or
HTTP server is not evidence that discovery, intelligence, quotes, or execution
is running.
