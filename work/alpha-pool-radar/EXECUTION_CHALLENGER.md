# Independent Forward Paper Trial

- State: `outputs/execution-challenger/state.json`.
- Evidence and accounting: `outputs/execution-challenger/report.json`.
- Feed: the existing fast-track loop, without extra market requests.
- Capital: USD 1,000; order: USD 35, plus the unchanged execution cost model.
- Baseline ledgers remain unchanged. No challenger signal is routed to live execution.

The fixed trial adds a 30-minute startup warmup, independently timestamped
observations, 20-second-spaced rising-price confirmation, and buy-count support.
Old-token revival also needs at least six minute-spaced prior volume observations
spanning 15 minutes, excluding the latest five minutes, and twice their median
five-minute volume. A pullback needs a higher low and a reclaimed bounce level.
Neither token age nor a positive five-minute change alone is sufficient.

A pending buy is canceled if its new executable observation has risen more than
5% above the signal price. Same-token same-arm reentry has a six-hour cooldown.
Baseline next-observation fills, slippage/tax/gas assumptions and exits are retained.
Bad or repeated quotes remain unavailable; they do not fabricate fills or equity.

Evaluate after forward closures: net PnL after all modeled costs, per-chain and
per-arm results, win rate, concentration in the largest winner, delayed exits,
and pending valuations. A profitable historical filter or a few winners does not
establish forward profitability. Do not retune the fixed trial in place or reset
its losses; use a new explicitly versioned experiment for any changed rules.
