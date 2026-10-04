# OKX DEX SDK Live Executor

This is the faster BSC executor path:

`radar strategy -> bsc-execution-input.json -> OKX DEX SDK -> local EVM signer -> OKX broadcast`

It is the current live execution path. The older Python OKX transport is kept as
historical reference only; use this SDK launcher for new BSC MEME live trading.

It reuses the DPAPI-protected credentials created by `setup_okx_live.ps1`.
It additionally requires the OKX Web3 API `Project ID`, read from `.env` as
`OKX_PROJECT_ID` or passed with `-ProjectId`.

## Preflight

```powershell
cd "C:\path\to\alpha-meme-radar"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -Preflight -ProjectId "YOUR_OKX_PROJECT_ID"
```

Expected shape:

```json
{"ready":true,"live_started":false,"provider":"okx-dex-sdk","chain_id":56,"wallet":"0x..."}
```

For the real OKX account/API validation, use the remote read-only check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -Preflight -RemotePreflight -ProjectId "YOUR_OKX_PROJECT_ID"
```

## Dry Run Current Strategy Candidate

This requests OKX swap data but does not sign or broadcast.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -StrategyMeme -ProjectId "YOUR_OKX_PROJECT_ID"
```

If `outputs\bsc-execution-input.json` has no BSC signal, it returns
`strategy_candidate_unavailable`.

## Live Strategy Daemon

This is the normal automation mode. It waits for the strategy to write a BSC
candidate into `outputs\bsc-execution-input.json`, buys it through OKX DEX SDK,
records the live SDK position, then manages exits with the original rules:
`-22%` stop loss, `+100%` TP1 selling `80%`, `35%` trailing drawdown after TP1,
and a `90m` time stop.

One safe non-broadcast check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -Daemon -Once
```

Start real automated trading after explicit wallet confirmation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -Daemon -Live -ExpectedWallet "YOUR_EXPECTED_WALLET" -AmountNativeAtomic "1000000000000000" -IntervalSeconds 3
```

`1000000000000000` wei is `0.001 BNB`, roughly a 1 USDT-class entry depending on
BNB price. Leave this terminal open while the automation is running.

## Small Live Round Trip

This is the minimum real buy/sell chain test. It buys the current strategy MEME
candidate, waits for the buy receipt, sells only the received token delta, and
waits for the sell receipt.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -StrategyMeme -Live -RoundTrip -ExpectedWallet "YOUR_EXPECTED_WALLET" -ProjectId "YOUR_OKX_PROJECT_ID" -AmountNativeAtomic "1000000000000000"
```

`1000000000000000` wei is `0.001 BNB`, roughly a 1 USDT-class test depending on
BNB price.

## Manual Token Round Trip

Use this when the strategy input currently has no candidate but you want to test
the buy/sell execution path on a specific BSC MEME contract.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\work\alpha-pool-radar\start_okx_dex_sdk_live.ps1" -Token "0xTOKEN_CONTRACT" -Symbol "MEME" -Live -RoundTrip -ExpectedWallet "YOUR_EXPECTED_WALLET" -ProjectId "YOUR_OKX_PROJECT_ID" -AmountNativeAtomic "1000000000000000"
```
