# Operator-started round-trip acceptance

The local account page at `http://127.0.0.1:8771/` has two one-shot modes:
BNB -> strategy-selected MEME -> BNB, and BNB -> USDT -> BNB.
Both are separate from the continuous MEME strategy.
Nothing starts on page load, refresh, a GET request, or server startup.

## Strategy MEME Mode

The runner reads `outputs/bsc-execution-input.json` and reuses `entry_decision`
from the existing BSC live policy. It keeps the producer's candidate order,
first-discovery/narrative-breakout conditions, source requirements, momentum,
risk flags, previous token history and daily loss gate. Ratings are not a new
buy gate. The account must have no open strategy positions or pending orders.

The input must be at most 30 seconds old, with one unique token/pool quote.
The selected token is pinned for the attempt. Security, sequential buy/sell
simulation and both route/pool identities must pass. The same candidate must
still pass the original policy before buying and again before broadcasting;
it cannot be replaced silently by a different coin.

The test changes only sizing (at most USD 1) and exit timing (sell the purchased
quantity after confirmed receipt). It does not use the normal strategy TP/SL,
does not prove strategy profitability, and does not start the continuous worker.
No matching candidate means stop, not a random purchase or a waiting daemon.
The route parser still supports only the already-validated route families.
Unsupported pools, adapters or fees remain blocked; not every MEME is routable.

## Limits

- Buy notional: at most USD 1 using the native BNB amount quoted at preparation.
  The amount is never increased if that size is unsupported by the provider.
- Slippage: 1 percent per swap, not inherited from the continuous worker.
- Buy gas cap: USD 0.50 estimate. Approval plus sell gas cap: USD 0.50 estimate.
  A separate total USD 1 gas-reservation cap is checked before signing.
  USD values are estimates at checked BNB prices, not guaranteed conversion rates.
- Sell only the token quantity proven received by the finalized buy receipt.
  Existing holdings of the selected token are not included in the sell amount.
- At most one buy and one sell. Exact approval may require one zero-reset.
- Stop on failed checks, unknown results, or confirmation timeout. No blind retry.
- Extra positive-slippage fees are rejected unless the operator explicitly enables
  an exact recipient and maximum rate in the page. Fee bytes are never removed.

## User Operation

Review the account and limits on the local page, explicitly confirm the test,
then click the one-shot test button. That one click launches the private local
PowerShell launcher. Signing and execution then happen in the background.
The browser never receives a private key or API credential. Closing the page
does not cancel an already submitted transaction.

The optional fee profile is separate consent, disabled by default. Rate 100
per mille means the trim can take the whole positive output improvement up to
10 percent of actual output; it is not a fixed 10 percent charge on every swap.
The final route must match the exact accepted recipient and stay within the cap.

The runner performs fresh read-only buy/approve/sell simulation before the buy,
then obtains and validates actual transaction routes again during execution.
A simulation success is not a proof of actual transaction acceptance or MEME
sellability. A failure before buying is reported as blocked, not as success.

## Persistence and Reconciliation

The existing private per-wallet directory under
`~/.config/alpha-radar/gmgn-live/<wallet>/` remains authoritative.
`okx-transactions.sqlite3` is shared with the normal transport; it retains
transaction hashes and nonce reservations. The existing `worker.lock` and
launcher lock prevent concurrent account workers.

`roundtrip-attempt.json` is an exclusive, durable attempt claim.
`roundtrip-status.json` contains progress, transaction hashes, remaining test
tokens, confirmed native gas, and realized BNB P/L after the sell.
Neither is automatically deleted or reset. An interrupted attempt cannot buy
again on restart. After an incomplete attempt, inspect the chain receipts and
remaining tokens before preparing any subsequent test; there is no reset button.
Both test modes share these records. Switching modes does not bypass an earlier
attempt. New UI/launcher code does not reload the running local server by itself.

Do not publish these private account files to Vercel. The public radar site does
not have this local execution endpoint. The local web server uses same-origin,
loopback, Host, CSRF and strict request-schema checks.

## Verification Boundary

Automated tests use injected RPC/provider fixtures and synthetic signing keys.
They do not load host credentials or send transactions to a real network.
Only an operator-started real test can establish actual buy-and-sell acceptance.
The current changes were verified with offline fixtures, including both modes
through the actual transport and synthetic signing/RPC. No real MEME round trip
was started or verified by this code change.
