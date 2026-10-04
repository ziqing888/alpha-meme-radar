# Alpha Pool Radar

Read-only scanner for a Binance Alpha meme/low-cap watchlist.

It pulls public data from:

- Binance Alpha token list
- DexScreener token pairs
- Binance USDS-M futures exchange info, 24h ticker, funding rate, and open interest history

The first version deliberately does not trade, does not need private keys, and leaves `top10_holder_pct` empty until an explorer adapter is added.

## Run

```powershell
python .\work\alpha-pool-radar\alpha_pool_radar.py --out-dir .\outputs
```

## Chain V2 Live Strategy And Cutover Truth

The current source implements the versioned `chain_v2` pipeline. Robinhood
uses aggregate discovery at exactly 5 USD per order. BSC uses aggregate early
bird at exactly 1 USD per order; BSC aggregate discovery is shadow-only. Fast
Track proposes candidates, but it never authorizes an order. The Node executor
must obtain fresh executable buy and reverse-sell quotes and pass its own
tradeability checks before signing.

Configuration is not proof that a running worker has adopted V2. The terminal
shows the approved setting as `configured_strategy`. It shows
`effective_strategy`, `effective_amount_usd`, and `executor_acknowledged_at`
only after a fresh daemon receipt matches the chain, wallet, PID, exact stage,
route, and `strategy_version=chain_v2`. A missing effective value means
`pending confirmation`; it must never fall back to the configured amount.

Existing workers remain legacy until deliberately restarted. Building the UI,
refreshing the monitor, or running the checks below does not restart a worker,
start a stopped chain, or change an order amount. BSC and Robinhood have
separate command files and locks, so a control action for one chain does not
act on the other.

Run the static/offline cutover gate from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\work\alpha-pool-radar\test_terminal_cutover.ps1
```

This gate reads source and the existing production build only. It validates
the V2 envelope, chain-specific stage/route/amount, separate live/shadow/reject
streams, daemon acknowledgment boundary, chain-local controls, and referenced
UI build assets. It does not inspect live profitability and does not certify a
currently running daemon as upgraded.

## Legacy BSC Execution: Paper, Shadow, Live

The section below documents the earlier BSC execution path and remains for
historical/operator reference. It is not the `chain_v2` strategy contract.

The BSC execution worker is a separate local process. `paper` records simulated
fills and exits in its own ledger; `shadow` records intents and quote evidence
without changing cash or positions. The `live` path is now wired to the local
OKX swap adapter, signer, and BSC JSON-RPC broadcaster, but remains fail-closed
until every preflight check passes. The paper launcher never starts live mode.

Before connecting a wallet, verify the live path offline:

```powershell
python .\work\alpha-pool-radar\alpha_bsc_live_preflight.py --json
```

The fast-track bridge takes only current open `first_discovery_probe` paper
positions on BSC and joins them to a fresh, same-pool Dex quote. This keeps the
live candidate stream aligned with the paper strategy; old, closed, non-BSC,
missing-pool, or stale candidates are excluded. Live signals must include
explicit smallest-unit `amount_atomic` and `slippage_percent` values. To
provide those fields after wallet-side setup, set them outside source control:

```powershell
$env:BSC_LIVE_BUY_AMOUNT_ATOMIC = "<explicit BNB amount in wei>"
$env:BSC_LIVE_SLIPPAGE_PERCENT = "3"
```

The live launcher runs the same preflight and starts the live worker only when
all checks pass:

```powershell
powershell -ExecutionPolicy Bypass -File .\work\alpha-pool-radar\start_bsc_execution_live.ps1
```

The launcher supplies the official OKX BSC router and a supported dynamic
swap selector when those two non-secret values are not already set. Wallet,
RPC, live-enable, arming, buy amount, and slippage values are never guessed.

For a local one-time setup, enter the wallet address and then the private key
into a masked prompt. The key is held in process memory only, checked against
the supplied address, and never written to the repository:

```powershell
powershell -ExecutionPolicy Bypass -File .\work\alpha-pool-radar\setup_bsc_live.ps1
```

To validate the local credentials and all non-secret defaults without starting
a live worker, add `-PrepareOnly`:

```powershell
powershell -ExecutionPolicy Bypass -File .\work\alpha-pool-radar\setup_bsc_live.ps1 -PrepareOnly
```

The setup defaults to the public BSC RPC, approximately 0.005 BNB per buy,
and 3% slippage. Set `BSC_RPC_URL`, `BSC_LIVE_BUY_AMOUNT_ATOMIC`, or
`BSC_LIVE_SLIPPAGE_PERCENT` in the local shell first when different values are
required.

The worker refuses to infer either value. Unknown OKX selectors, mismatched
tokens/pools/recipients, stale quotes, invalid receipts, and incomplete
configuration are rejected before signing or broadcasting.

## GMGN Execution Intents

`alpha_gmgn_execution_adapter.py` maps a policy-approved signal into the
official GMGN CLI swap shape, including native-token routing, slippage, anti-MEV
and the current +100% TP1 / 80% sell condition. It is preview-only: it returns
an auditable command and never invokes `swap`, never passes `--yes`, and never
forwards a private key. Use it to validate the strategy-to-provider contract
before any separately controlled execution integration is considered.

Check the local preview handoff without making a network request:

```powershell
python .\work\alpha-pool-radar\alpha_gmgn_preflight.py --json
```

The preflight reports only whether a wallet address, runner, amount, and
slippage are configured. `ready_for_submission` is permanently false in this
path, and `private_key_loaded` is permanently false.

The fast-track refresh also writes `outputs/gmgn-execution-intents.json` from
the current `bsc-execution-input.json`. Its `submitted` field is always false;
missing wallet or amount configuration produces a waiting/error status instead
of an inferred order. Each intent also has a deterministic `idempotency_key`,
signal/quote timestamps, strategy arm, score, and candidate reason so a later
operator-controlled executor can deduplicate and audit it.

The paper test capital is 100U. Each order is 5U, with at most 3 concurrent
positions and 15U total open exposure. The daily loss circuit breaker is 8U.
The paper exit rules are -22% stop loss, +100% TP1 selling 80%, followed by a
trailing exit for the remaining 20%. These are local simulation rules, not a
live trading configuration.

Worker artifacts are written under `outputs/bsc-execution/`:

- `state.json`: authoritative paper/shadow state
- `events.jsonl`: append-only event journal
- `report.json`: latest structured cycle report
- `report.md`: human-readable report when produced by the worker/report flow
- `health.json`: latest mode, status, ledger, and position summary
- `worker.log`: launcher stdout/stderr

Start exactly one paper worker:

```powershell
powershell -ExecutionPolicy Bypass -File .\work\alpha-pool-radar\start_bsc_execution_paper.ps1
```

The launcher clears `ALPHA_NARRATIVE_BROWSER_COMMANDS`,
`ALPHA_NARRATIVE_TWEET_URLS`, `ALPHA_NARRATIVE_TWEET_FILES`, and
`ALPHA_NARRATIVE_TWEETS_FILE` before starting the worker.

Run one paper cycle without starting a persistent process:

```powershell
python .\work\alpha-pool-radar\alpha_execution_worker.py --mode paper --once
```

Verify the worker, mode, health, and artifacts:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match '(?i)(^|[\\/])alpha_execution_worker\.py([\s\"'']|$)' } |
  Select-Object ProcessId, CommandLine
Get-Content .\outputs\bsc-execution\health.json
Get-Content .\outputs\bsc-execution\report.json
```

Stop the paper worker by its exact discovered PID:

```powershell
$worker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match '(?i)(^|[\\/])alpha_execution_worker\.py([\s\"'']|$)' }
if ($worker) { Stop-Process -Id $worker.ProcessId }
```

No real credentials or private keys belong in this README or the local paper
launcher. Keep the live configuration outside source control and do not use the
paper launcher to start a live process.

## Real-Time Monitor

Start the read-only monitor:

```powershell
.\work\alpha-pool-radar\start_monitor.ps1
```

Stop it:

```powershell
.\work\alpha-pool-radar\stop_monitor.ps1
```

Monitor outputs:

- `outputs/alpha-pool-radar-monitor-heartbeat.json`
- `outputs/alpha-pool-radar-alerts-latest.md`
- `outputs/alpha-pool-radar-alerts.jsonl`
- `outputs/alpha-pool-radar-monitor.log`

Default alerts fire when:

- A new candidate reaches score >= 58.
- A tracked token score jumps by >= 8.
- 1h OI change crosses >= 5%.
- Absolute funding crosses >= 0.05%.
- A new pool is younger than 24h.

Useful options:

```powershell
python .\work\alpha-pool-radar\alpha_pool_radar.py --alpha-limit 180 --top 40 --max-market-cap 200000000 --min-volume 1000000 --out-dir .\outputs
python .\work\alpha-pool-radar\alpha_pool_radar.py --chains bsc,base,solana --out-dir .\outputs
python .\work\alpha-pool-radar\alpha_pool_radar.py --skip-futures-details --out-dir .\outputs
```

Outputs:

- `outputs/alpha-pool-radar-latest.md`
- `outputs/alpha-pool-radar-latest.csv`
- `outputs/alpha-pool-radar-latest.json`

## Screenshot-Style Report

Generate Alpha + Meme + dealer-proxy sections:

```powershell
python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs
```

Outputs:

- `outputs/alpha-radar-report-latest.md`
- `outputs/alpha-radar-report-latest.json`
- `outputs/alpha-radar-dashboard.html`

The report command also writes a static local dashboard. Open the HTML file in a
browser to inspect the three sections without starting a server.

## Gold-Dog Backtest

The backtest reads the local replay history and can merge external historical
samples without changing the live monitor. Use this before changing the public
dashboard rules.

```powershell
python .\work\alpha-pool-radar\alpha_backtest_report.py --out-dir .\outputs --min-count 20 --target-days 365
```

Generic CSV/JSON/pickle imports are supported:

```powershell
python .\work\alpha-pool-radar\alpha_backtest_report.py --out-dir .\outputs --history-import .\data\history.csv
```

MELT repository data can be merged directly when `data/label/label.csv` and
`data/memecoin/*.jsonl` are present:

```powershell
python .\work\alpha-pool-radar\alpha_backtest_report.py --out-dir .\outputs --melt-data-dir .\work\external\MELT\data --min-count 20 --target-days 365
```

Outputs:

- `outputs/alpha-radar-backtest-latest.json`
- `outputs/alpha-radar-backtest-latest.md`

The backtest uses peak return when available. A gold-dog outcome requires both
10x from discovery (>=900% peak return) and at least $1M peak market cap. The
report also lists missed gold dogs and the reason each one did not enter the
single-pick slot.

## Telegram Shell

The Telegram shell reads the latest report JSON. It does not rescan markets and
does not store tokens in files.

Local dry-run commands:

```powershell
python .\work\alpha-pool-radar\alpha_radar_tg_bot.py --dry-run --command /top
python .\work\alpha-pool-radar\alpha_radar_tg_bot.py --dry-run --command "/watch QUID"
python .\work\alpha-pool-radar\alpha_radar_tg_bot.py --dry-run --once
```

Telegram env vars:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
```

Send new deduped alerts once:

```powershell
python .\work\alpha-pool-radar\alpha_radar_tg_bot.py --once
```

Run command polling:

```powershell
python .\work\alpha-pool-radar\alpha_radar_tg_bot.py --poll
```

Commands:

- `/top`
- `/dealer`
- `/alpha`
- `/meme`
- `/find SYMBOL`
- `/watch SYMBOL`
- `/unwatch SYMBOL`
- `/watchlist`

State file:

- `outputs/alpha-radar-tg-state.json`

## Stability Layer

The scanner keeps a local public-data cache in:

- `outputs/alpha-radar-cache/`

When DexScreener or Binance public endpoints fail transiently, the scanner
retries first. If a valid cached response exists, it uses the cached copy and
marks the run as `cached` or `partial` in report metadata. This prevents an API
hiccup from turning the Meme榜 or Dealer代理榜 into a misleading empty screen.

If the Meme profile/boost sources fail and return no current rows, the report
keeps the previous valid Meme rows and marks the Meme section as `cached`.
The dashboard shows a yellow notice when this fallback is active.

Manual stable refresh:

```powershell
python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs
```

Or use the helper:

```powershell
.\work\alpha-pool-radar\refresh_dashboard.ps1
```

Then open:

```powershell
.\outputs\alpha-radar-dashboard.html
```

Alert freshness:

- `outputs/alpha-pool-radar-alerts-latest.md` is rewritten every monitor cycle.
- If there is no new alert, it contains `No new alerts this cycle.`
- Historical alerts remain in `outputs/alpha-pool-radar-alerts.jsonl`.

## Current Score Inputs

- Small market cap under the configured cap.
- Alpha and DexScreener 24h volume strength.
- Volume-to-market-cap ratio.
- Fresh pool age from DexScreener.
- Futures listing, futures 24h quote volume, 1h OI change, and funding rate.
- Alpha hot tag and basic social-link presence.

## Next Adapter

Add holder concentration as a separate adapter:

- BSC/EVM: BscScan/Etherscan `tokenholderlist` or compatible explorer endpoint.
- Solana: Solscan/Birdeye holder endpoint.
- Output field: `top10_holder_pct`.

Keep that adapter optional, because those services usually need API keys or have stricter rate limits.

## Optional EVM Holder Adapter

The EVM holder adapter is wired but disabled by default.

Providers:

- `routescan` default: keyless free tier, Etherscan-compatible `module=token&action=tokenholderlist`.
- `blockscout`: compatible `module=token&action=getTokenHolders`; set `BLOCKSCOUT_API_BASE` for non-Ethereum instances.
- `etherscan`: Etherscan V2 `module=token&action=topholders`; requires `ETHERSCAN_API_KEY` or `EVM_SCAN_API_KEY`.

Optional env keys:

- `ROUTESCAN_API_KEY` raises Routescan rate limits, but is not required.
- `BLOCKSCOUT_API_KEY` is optional if the selected instance supports it.
- `ETHERSCAN_API_KEY` or `EVM_SCAN_API_KEY` is required for `--holder-provider etherscan`.

Example:

```powershell
python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs --holders-enable --holder-provider routescan --holder-top-tokens 12 --holder-offset 20
```

Holder fields added to outputs:

- `top10_holder_pct`
- `top20_holder_pct`
- `max_holder_pct`
- `holder_contract_count`
- `holder_source`

If a provider fails, the report still runs normally and records the holder error in output metadata.

## Optional Risk Adapter

The risk adapter is disabled by default and remains read-only.

Sources:

- `GoPlus`: EVM token security via `https://api.gopluslabs.io/api/v1/token_security/{chain_id}`.
- `RugCheck`: Solana token summary via `https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary`.

Example:

```powershell
python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs --risk-enable --risk-top-tokens 12
```

Use it with holder concentration:

```powershell
python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs --holders-enable --holder-provider auto --holder-top-tokens 12 --risk-enable --risk-top-tokens 12
```

Optional env key:

- `GOPLUS_API_KEY` for authenticated GoPlus requests. The adapter also tries public requests without it.

Risk fields added to outputs:

- `risk_score`
- `risk_level`
- `risk_flags`
- `risk_source`

## Heat Adapter

The Meme report now scores heat from official DexScreener public sources:

- `token-boosts/top`
- `token-boosts/latest`
- `token-profiles/latest`
- `ads/latest`
- `community-takeovers/latest`

Heat fields:

- `heat_score`
- `heat_flags`
- `sources`

Default and optional Meme discovery sources:

- `DexScreener`: enabled by default via latest profiles, latest/top boosts,
  ads, and community takeovers.
- `GMGN trending`: enabled by default via public rank URLs for Solana, Base,
  BSC, and Ethereum.
- `Mobula`: enabled by default with the demo trending endpoint. Set
  `MOBULA_API_KEY` for authenticated requests, or override with
  `MOBULA_TRENDING_URLS`.
- `Birdeye`: set `BIRDEYE_API_KEY` to enable the default trending and meme-list
  endpoints. Override with `BIRDEYE_TRENDING_URLS` if the endpoint shape changes.
- `GMGN Trenches`: enabled by default through the official `gmgn-cli market
  trenches` read-only command. Tune it with `GMGN_TRENCHES_CLI_CHAINS`,
  `GMGN_TRENCHES_CLI_TYPES`, `GMGN_TRENCHES_CLI_LIMIT`, or disable it with
  `GMGN_TRENCHES_CLI_DISABLE=1`. You can also set `GMGN_TRENCHES_URLS` to one
  or more GMGN-compatible JSON endpoints, use `GMGN_TRENCHES_COMMANDS` for a
  browser/scraper command that prints JSON, or `GMGN_TRENCHES_FILES` for local
  JSON exports.
- `OKX Trenches`: set `OKX_TRENCHES_URLS` to one or more OKX/OnchainOS-compatible
  JSON endpoints. You can also use `OKX_TRENCHES_COMMANDS` or
  `OKX_TRENCHES_FILES`.
  The built-in read-only official connector automatically runs when all three
  process environment variables `OKX_API_KEY`, `OKX_SECRET_KEY`, and
  `OKX_PASSPHRASE` are present (Onchain OS market developer credentials, not a
  wallet private key). It queries NEW/MIGRATING/MIGRATED and smart-money/KOL/whale
  signals using process environment credentials or the same three string fields
  in root `.okx-market.local.json` (git-ignored and reloaded each refresh).
  Environment values take precedence. Never include this local file in archives.
  The default discovery scope is `bsc,robinhood` (Robinhood mainnet 4663,
  never testnet 46630). Solana remains available through `ALPHA_MEME_CHAINS`.
  GMGN CLI queries are limited to its existing supported chains; Robinhood
  discovery uses OKX and DexScreener. A source listing alone is not a gold pick.
  Results join the normal refresh as `okx_trenches` / `okx_signal`; both count as
  one independent OKX provider. `okx_trenches` is a discovery/early-entry source,
  while `okx_signal` is the OKX signal-panel confirmation source for Smart Money,
  KOL/Influencer, and Whale buy-direction events. Signal rows preserve wallet type,
  trigger wallet count, signal amount, sold ratio, trigger price, holders, and
  top-10 holder percent so the gold-dog layer can confirm without treating every
  OKX hit as a fresh buy.
  Duplicate records within a response are removed.
  `meta.meme_source_status.okx_trenches.official_api` reports actual query errors
  and row counts. HTTP 402 is reported as `payment_required`; the connector never
  signs payments or accesses account/trading endpoints. No credentials means no
  API requests. Existing URL/file inputs remain usable independently.
- `Binance Wallet`: wallet/Web3 market rank, Meme Rush, chain radar, and
  smart-money signal exports belong to the Meme gold-dog source layer, not the
  Binance Alpha dealer radar. Feed hot/radar rows through `BINANCE_WALLET_HOT_URLS`,
  `BINANCE_WALLET_HOT_COMMANDS`, `BINANCE_WALLET_HOT_FILES`, or
  `outputs/meme-source-inbox/binance-wallet-hot.json`. Feed smart-money/signal
  rows through `BINANCE_WALLET_SIGNAL_URLS`, `BINANCE_WALLET_SIGNAL_COMMANDS`,
  `BINANCE_WALLET_SIGNAL_FILES`, or
  `outputs/meme-source-inbox/binance-wallet-signals.json`. The two labels join
  candidates as `binance_wallet_hot` / `binance_wallet_signal` and count as one
  independent `binance_wallet` provider, so repeated Binance Wallet hits add heat
  but do not inflate multi-source confirmation.
- `DeBot`: set `DEBOT_TRENCHES_URLS` / `DEBOT_SIGNAL_URLS` for direct JSON
  sources, `DEBOT_TRENCHES_COMMANDS` / `DEBOT_SIGNAL_COMMANDS` for browser
  commands, or `DEBOT_TRENCHES_FILES` / `DEBOT_SIGNAL_FILES` for local exports.
- `Birdeye custom`: in addition to the API-key default, `BIRDEYE_TRENDING_URLS`,
  `BIRDEYE_TRENDING_COMMANDS`, and `BIRDEYE_TRENDING_FILES` can feed the same
  external-source normalization path.

The report writes `meta.meme_source_status` so the website shows each source as
enabled, waiting for configuration, direct URL-backed, browser-backed,
file-backed, or key-backed. These source labels are also stored per candidate,
which lets replay/backtest later compare win rate by source combination.

GMGN 默认会作为 Meme 推荐的外部热榜源一起抓取，当前默认尝试：

- `sol` 1h swaps rank
- `base` 1h swaps rank
- `bsc` 1h swaps rank
- `eth` 1h swaps rank

如果默认接口不可用，系统会记录错误但不会影响 DexScreener 来源。也可以手动指定一个兼容 GMGN/机器人格式的 JSON 源：

```powershell
$env:GMGN_TRENDING_URL="https://your-gmgn-compatible-endpoint.example/trending"
python .\work\alpha-pool-radar\alpha_radar_report.py --out-dir .\outputs
```

支持的字段包括 `chain`/`chainId`、`token_address`/`tokenAddress`/`address`/`mint`，以及
`smart_money`、`kol`、`holder_count`、`potential`。页面会把它们展示成“来源、聪明钱、KOL、持有人、潜力”等字段。
