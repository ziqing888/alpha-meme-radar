# OKX DAG Read-Only Preflight

The diagnostic harness does not authorize execution. Its `execution_ready`
always remains false. A separate opt-in execution adapter is described below.

## Checks

- Canonical `dagSwapTo` ABI, sender/router/chain, input value and amount,
  receiver, minimum output, deadline, linear DAG indices and weights.
- Universal V3 two-address inner payload and token continuity. Native BNB is
  wrapped to WBNB for the first pool and unwrapped for native output.
- Explicit two-word positive-slippage trim decoding. Unknown trailers,
  branches, transfer modes, reserved flags and other adapter schemas reject.
- RPC method allowlist excludes all signing and broadcast methods.
- Pool token pair and fee are checked against the official BSC Pancake V3
  factory's `getPool`; a pool's self-reported factory is not sufficient.
- The universal adapter's current bytecode is compared with a pinned Sourcify
  exact-match runtime hash and its WETH getter is checked against BSC WBNB.
  Verified source identity does not prove official adapter authorization.
- Independent `eth_call` probes for approximately $5 of BNB into USDT and
  5 USDT into BNB. Approval simulation does not persist an allowance.

The diagnostic option for a provider's one-hour deadline records
`deadline_exceeds_live_limit`; it does NOT modify calldata or relax the live
transport's 20-minute deadline restriction. Quotes older than ten seconds
before or after simulation are rejected. Block headers must be recent and
well-formed; calls are pinned to canonical block hashes using EIP-1898.

## Running

Offline tests require the project's existing `eth_abi`, `eth_utils`, and pytest:

```powershell
py -3 -m pytest work/alpha-pool-radar/test_alpha_okx_dag_preflight.py -q
```

The read-only CLI uses the existing private `okx-live-config.json` wallet/RPC
fields and only `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` from its process
environment. It never reads the wallet's private-key DPAPI file. It must not be
launched through the live worker. Do not pass secrets as command arguments.

```powershell
py -3 -B work/alpha-pool-radar/alpha_okx_dag_preflight.py
```

The report is kept outside the repository and public frontend at
`~/.config/alpha-radar/okx-dag-preflight.json`. It contains no raw calldata,
private keys or API credentials. CLI exit code zero means the independent swap
calls simulated successfully, NOT that production execution is ready.

## Remaining Gates

Unpinned observed contract code hashes do not authenticate deployment provenance.
Router runtime identity, official adapter authorization, and trim payout
configuration still need verification before any production integration.
The two calls use existing wallet balances/allowances independently; this is
not a sequential buy-approve-sell test. No gas was actually paid, no receipt or
real fill was produced, and no MEME sellability was demonstrated.

## Stateful Round Trip

`alpha_okx_roundtrip_preflight.py` adds a separate read-only USDT control test
using `eth_simulateV1`: balance, buy, balance, reset allowance, exact approval,
sell, balance. No state overrides are used. Token balance deltas prove the
sell amount is covered by the new simulated purchase, not existing holdings.
Native-transfer simulation logs independently check BNB delivered to the wallet.
Parent hash, child number/timestamp, calldata deadlines and quote TTL are checked.

This sells the buy's guaranteed minimum amount; surplus stays as a recorded
simulated token remainder. It does not claim a full liquidation. Admission
validation is disabled (eth_call semantics); signing, fees and nonce admission
are NOT tested. The report is `~/.config/alpha-radar/okx-roundtrip-preflight.json`.
If attestation finishes after the quote TTL, success is explicitly labeled
`historical_simulated_round_trip`, not a fresh execution opportunity.

It uses the same three API environment credentials as the standalone probe:

```powershell
py -3 -B work/alpha-pool-radar/alpha_okx_roundtrip_preflight.py
```

## Opt-In Execution Preparation

`alpha_okx_dag_execution.py` is integrated with the existing transport before
gas estimation, signing and journaled broadcast. No live process is started by
this module. Existing execution, exposure and reconciliation gates remain intact.
`OKX_DAG_ENABLED=1` is required; it does not enable the separate live gates.

Preparation authenticates the configured candidate pool at the token endpoint,
every V3 pool's factory membership, the adapter source-runtime pin and WBNB
configuration, and the router's official-address/runtime pin. It shortens only
the calldata deadline to at most 180 seconds, keeps all fee/route bytes intact,
revalidates, and performs a pinned-block eth_call before returning unsigned data.
After simulation and immediately before the signing path, at least 30 seconds
must remain on the encoded deadline; the original deadline is never extended.
The router pin is an observed runtime at the official deployment, not a
Sourcify recompilation proof. Unknown protocols and pool mismatches reject.

The default fee policy accepts no trailer or a recognized single-trim trailer
whose rate is zero. Any nonzero rate rejects with `dag_trim_consent_required`.
An operator may separately configure `OKX_DAG_TRIM_RECIPIENT` plus
`OKX_DAG_MAX_TRIM_PER_MILLE` (1..100). The launcher exposes explicit
`-EnableDagRoutes`, `-DagTrimRecipient`, and `-DagMaxTrimPerMille` options;
none is enabled or populated by default, and inherited fee settings are removed.
No host configuration was changed during implementation.

Important: per the router source, rate 100 caps the trim at 10% of actual
output, **not** 10% of the gain. It may consume the entire positive price gain
up to that cap; the final minimum-output check still applies. Pool fees, token
taxes and gas are separate. Do not infer fee acceptance from a successful quote
or a simulation with zero realized trim. Fee-recipient organizational ownership
remains unverified. The tested API returned error 51000 for an explicit zero-rate
request, so zero-fee routing availability is not established.

## Primary References

- https://web3.okx.com/onchainos/dev-docs/trade/dex-swap
- https://web3.okx.com/onchainos/dev-docs/trade/dex-smart-contract
- https://developer.pancakeswap.finance/contracts/v3/addresses
- https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-eth
- https://sourcify.dev/server/v2/contract/56/0x7a7ad9aa93cd0a2d0255326e5fb145cec14997ff?fields=all
- https://github.com/okxlabs/Web3-DEX-Router-EVM-V1/blob/1fc1508df3caeb18a8179dfc0ea4e956099ab481/contracts/8/interfaces/IDexRouter.sol
- https://github.com/okxlabs/Web3-DEX-Router-EVM-V1/blob/1fc1508df3caeb18a8179dfc0ea4e956099ab481/contracts/8/DagRouter.sol
- https://github.com/okxlabs/Web3-DEX-Router-EVM-V1/blob/1fc1508df3caeb18a8179dfc0ea4e956099ab481/contracts/8/adapter/TemplateAdaptor/BaseUniversalUniswapV3Adaptor.sol
- https://github.com/okxlabs/Web3-DEX-Router-EVM-V1/blob/1fc1508df3caeb18a8179dfc0ea4e956099ab481/contracts/8/libraries/CommissionLib.sol
