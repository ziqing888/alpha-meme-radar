# Local Trading Terminal

Forked UI: see THIRD_PARTY_NOTICES.md and LICENSE. The upstream backend is not used.

## Build

From this directory:

```powershell
npm ci --ignore-scripts
npm run build
```

## Run

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\work\alpha-pool-radar\start_okx_dashboard.ps1
```

Open http://127.0.0.1:8771/. The launcher only replaces the local dashboard process, not trading workers.
For a separate preview, run `py -3 work/alpha-pool-radar/alpha_terminal_server.py --port 8772`.

## Data and Control

- `alpha_terminal_data.py`: whitelisted ledger/status projections, never raw provider objects.
- `alpha_terminal_balances.py`: nonblocking background public RPC balances and ticker cache. BSC/RH native and BSC USDT are queried; RH USDT remains null. Failure/staleness never becomes a fabricated zero. Current BNB/ETH USDT prices are valuation estimates, not historical USD rates.
- `alpha_terminal_control.py`: process verification, DPAPI wallet replacement and amount-draft storage.
- `alpha_terminal_execution.py`: `ExecutionControl`, explicit web execution commands and worker-acknowledgement projection.
- `alpha_terminal_accounting.py`: nonblocking historical receipt/trace verification and bounded FIFO in a separate sidecar.
- `alpha_terminal_server.py`: loopback static assets and same-origin/CSRF JSON API.

Wallet replacement requires idle workers and no open positions. Keys are sent only to the local handler and never persisted by the browser.

## Execution Commands

The strategy page now exposes `start`, `pause`, `resume`, `apply`, and `stop` through `POST /api/terminal/strategy/command`. `GET /api/terminal/runtime` supplies verified process state and command outcomes. Saving via `POST /api/terminal/strategy/config` still saves a draft only; explicit start/apply is separate and drafts are not automatically applied.

| Action | Behavior |
| --- | --- |
| `start` | Explicitly launch the selected chain with the current wallet and requested USD amount. Reject an existing worker or conflicting launcher. |
| `pause` | Disable new buys while continuing management/exits of existing positions. |
| `resume` | Re-enable new buys in the same worker. |
| `apply` | Change subsequent buy size in the current process, not existing positions, exit rules or launcher defaults. |
| `stop` | Exit only with no local positions/pending transactions and a successful chain pending-nonce check. Never force-kill or liquidate. |

Commands carry `action`, `chain`, `wallet_address`, and a UUID `request_id`. Running-worker actions require the matching expected PID as `expected_pid`; `start` and `apply` also require `amount_usd`. Reusing a UUID with the same request is deduplicated; changing its payload is rejected.

HTTP 202 (`submitted` or `starting`) is not an execution acknowledgement. The worker consumes a per-chain command mailbox and publishes `terminal_control` in SDK live status. Match the request UUID, chain, wallet, PID and fresh heartbeat before treating a running-worker command as applied. Start completion additionally verifies the worker/launcher parent relationship. Expired commands, stale heartbeats, rejections and exits without acknowledgement remain unconfirmed; do not display them as successful actions.

Existing old running workers remain unchanged. They show `legacy_worker`; running-worker commands return `worker_upgrade_required` until an operator arranges a manual, controlled restart after checking positions and pending transactions. Reloading the web service does not upgrade a trading process. No automatic worker restart, forced termination or stale-lock deletion is performed to adopt the protocol. The new daemon lock requires operator inspection if left behind after a crash.

## Historical Accounting

The server owns one `alpha_terminal_accounting.Collector` and calls `enrich_snapshot(snapshot)` without waiting for RPC. The collector defaults to a 20-second cycle, at most 3 orders and 30 RPC calls per batch, a 3-second per-request timeout, a 15-second request budget and failure backoff capped at 300 seconds. It atomically writes only `outputs/terminal-accounting.json`; it never changes the SDK ledger/state, signs or submits a transaction.

- Receipt verification binds chain, wallet, transaction hash, successful receipt and block identity. Gas is `gasUsed * effectiveGasPrice` plus `l1Fee` when present. Gas and outer transaction `value` can be recovered even when complete settlement is unavailable.
- `native_spent_atomic` is gross native wallet debits plus this transaction's fees. `buy_cost_native_atomic` subtracts trace-proven refunds; gross payment is not automatically exact net acquisition cost.
- Sell native proceeds require valid `debug_traceTransaction` / `callTracer` internal-transfer evidence. Reverted subtrees and inherited DELEGATECALL value are not payments. Missing/invalid traces leave unsupported settlement/cost/PNL fields null; native block-balance differences are never used to invent proceeds.
- FIFO uses actual wallet ERC20 Transfer quantities in block/transaction/log order, reconciled with historical token `balanceOf` and transfer logs. Its per-token window is capped at 10,000 blocks, not a full-chain indexer. Unknown opening/external-acquisition costs remain unknown lots; archive/log gaps, window limits or ambiguous multi-asset consideration prevent unsupported exact PNL.
- Partial exits allocate verified acquisition cost including buy fees, subtract sell receipt fees once, and retain integer rounding remainders for the final lot consumption. `fee_scope` is `transaction_only`: separate approval-transaction fees are excluded. Historical USD exchange rates, funding flows and a complete equity curve are not backfilled.

Order details show `order.accounting` separately from ledger PNL. Non-null SDK accounting values are preserved; the sidecar fills only missing outer fields. Price return, ledger-accounted native PNL and independently verified transaction PNL are distinct. The ledger chart is not a complete historical net-profit/equity statement, and native currencies from different chains cannot simply be summed.

Top-level coverage is `accounting.{total,attempted,receipt_covered,gas_covered,payment_covered,fifo_covered,pnl_covered,persistence_status}`. Gas/payment/FIFO/PNL coverage requires `receipt_status == "verified"`. Persisted values reload as `stale`, do not count toward current coverage and cannot seed trusted FIFO until reverified. Missing evidence stays null and sidecar write failures are reported separately. Do not claim all historical PNL is complete.

## Validation Scope

Python fixtures and Node protocol tests exercise process/launcher mocks, command identity and acknowledgement, receipts/traces, partial FIFO, stale-cache coverage and sidecar persistence. No real live start/pause/resume/apply/stop actions, real trades, forced legacy-worker restarts or real wallet replacement were tested in this implementation. Limited historical read-only RPC checks recovered receipt gas/value; records without usable trace evidence still lack complete net cost/PNL. Passing tests does not mean existing workers have adopted the protocol.
