# Local OKX DEX BSC Operator

These Windows scripts configure and launch `alpha_okx_live_worker.py` for the
existing BSC strategy. Setup never starts a worker, installs a service, schedules
a task, deploys anything, or starts scanners. The default launch is offline
`--check`; `-Once` without `-Live` also performs an offline check, with no ledger
cycle or trade execution. Only an explicit `-Live` enables
automatic trading. This is local account-scoped execution, with no web-wallet
popup to approve each trade.

Execution scope is BSC (chain 56), native BNB input/output, and supported
single-pool V2/V3 routes only. This does not include Robinhood, USDT-funded
multi-hop routing, or unrestricted execution across all tokens. Candidates
must satisfy the existing strategy and the supported route/token checks.

## Strategy Routing

The live input uses the original strict 45-minute discovery classifier, not the
separate three-arm execution audit. Pullback and old-revival entries are rejected;
the 180-minute shadow simulation remains paper only. The two allowed routes are
first discovery ($10K-$300K current market cap, $8K minimum liquidity) and
narrative breakout (above $300K through $1M, $30K minimum liquidity, OKX plus
GMGN or DexScreener, 1h change no higher than 100%). Both still require the
original discovery classification and current executable-market checks.

Rating/score orders the candidate queue; it is not a second execution gate.
The 5m change range matches the original classifier (-25% through +45%).
Candidates without a paper position can enter the bridge, using the recorded
first-discovery time when no separate signal timestamp exists. Refresh time
never resets the discovery window. Overheated, old, unsupported, or risk-flagged
tokens are not admitted merely because they later became big winners.

The $100 account retains $5 orders, at most 3 open positions, $15 exposure and
an $8 daily realized-loss limit. Exits keep -22% stop loss, +100% take profit
selling 80% of original quantity, a 35% trailing drawdown on the remainder and
the existing 90-minute time rule. These core rules follow the original probe;
BSC-only execution, smaller fixed sizing and provider checks still differ from
the historical multi-chain paper ledger. Historical paper profit is not a live
fill guarantee. Changing this routing does not start the live worker.

## Local Setup

Use the same Windows account and machine that owns the shared account ledger.
Run these commands in an interactive PowerShell window from the workspace root:

```powershell
Set-Location 'C:\path\to\alpha-meme-radar'
powershell.exe -NoProfile -File .\work\alpha-pool-radar\setup_okx_live.ps1
```

Enter four credentials at the masked local prompts:

| Input | Purpose |
| --- | --- |
| `OKX_API_KEY` | Identifies the OKX API account/project used for DEX requests |
| `OKX_SECRET_KEY` | Signs OKX API authentication requests |
| `OKX_PASSPHRASE` | Passphrase belonging to that OKX API credential |
| `BSC_PRIVATE_KEY` | Actual dedicated EVM wallet private key used to sign BSC transactions |

The API secret and passphrase do not replace the blockchain wallet private key.
The wallet key is 64 hexadecimal digits, optionally prefixed with `0x`, not a
GMGN API signing PEM, seed phrase, wallet password, or public address. Supply it
only at the local masked prompt, never in chat, command arguments, logs, or a
public dashboard. A connected browser wallet alone does not authorize this worker.

Setup ignores existing `.env`, `.env.local`, OKX config files from other tools,
and inherited credential variables. It never imports a previous or compromised
wallet key. Existing user-configured OKX secrets in those files are left alone;
enter the intended credentials explicitly at these new prompts.

Setup uses local Python `eth_account.Account.from_key` to derive the public
`BSC_WALLET_ADDRESS`, so normally there is no address prompt. The helper receives
the key through stdin, never its arguments, and makes no network calls. If
`eth_account` is missing, setup validates the private-key scalar locally and asks
for the public address. Key/address matching remains unverified in this fallback;
re-run setup with a Python installation containing `eth_account` to verify it
locally. Missing Python or an invalid key stops setup.

To select a Python installation that already has the worker dependencies, pass
its executable path. Optional parameters contain public values only:

```powershell
powershell.exe -NoProfile -File .\work\alpha-pool-radar\setup_okx_live.ps1 -PythonPath 'C:\path\to\python.exe' -RpcUrl 'https://bsc-dataseed.binance.org/'
```

`-WalletAddress` can supply the public BSC address and must match the derived
address when derivation is available. The RPC default is exactly
`https://bsc-dataseed.binance.org/`, regardless of inherited environment values.
An override must be an HTTP(S) endpoint without user info, query, fragment,
or whitespace. Private/paid RPC URLs with an API key in the URL path are accepted;
query-string API keys are rejected. The URL is stored as plaintext inside the
ACL-protected local config, not as a DPAPI credential. Treat a path-key URL as a
credential and keep it out of chat, screenshots, shared config, and logs. For
such an endpoint, set `bsc_rpc_url` in the protected JSON through a local editor;
do not paste a real path key into the example command or a saved command history.

The configured RPC must support `debug_traceTransaction` with usable evidence
from either `callTracer` OR `prestateTracer` (diff mode); both are not required.
The transport tries the call tracer first, then the prestate tracer fallback.
Ordinary chain ID, balance, or receipt support alone is insufficient. The default
public BSC node may not expose these debug methods; its presence in the config
does not mean it is ready for live execution. Select a trace-capable endpoint
and run `-Verify` before `-Live`. The worker's read-only verification is responsible
for probing trace support as well as API, chain, balance, and finality; missing support
must stop verification. The live worker repeats verification before enabling
execution, so an earlier successful check does not bypass current RPC readiness.

The RPC must also support the `finalized` block tag. `-Verify` must check this
capability before live execution. Before any terminal accounting, the worker
requires finalized-chain evidence; an ordinary receipt or latest-block response
does not replace it. Missing `finalized` support must block verification and
terminal accounting.

Local verification on 2026-09-08: the default public endpoint passed chain and
finalized checks but rejected `debug_traceTransaction`. The operator config was
changed to `https://rpc-bnb.blockmachine.io`, an endpoint listed in the BNB Chain
RPC documentation. With the standard `alpha-radar/1.0` User-Agent and JSON Accept
header, this endpoint passed chain 56, finalized-header validation and callTracer
verification on a public transaction. The configured account's `-Verify` then
exited 0 with `provider_permissions_verified: true` and `live_started: false`.
No real transaction was signed or broadcast by this verification. Public-node
availability can change; keep the readiness checks enabled before every launch.

The fixed config is `~/.config/alpha-radar/okx-live-config.json`. It contains the
public wallet address, whether derivation succeeded, masked API-key marker,
Python path, RPC URL, slippage (default 5%), fixed state root, and paths to four
DPAPI-encrypted files in `~/.config/alpha-radar/okx-live-secrets/`. Config, secret
files, secret directory, and state directory grant access to the current Windows
user and SYSTEM. Nothing is written into the repository during setup.

DPAPI protection is tied to this Windows user and machine. Re-run setup to rotate
credentials for the same wallet. New encrypted versions are written before an
atomic config replacement; old encrypted versions are retained. Changing the
configured wallet is rejected, so account migration requires reconciliation of
the existing orders and positions first.

## Commands

After setup, run `-Check`, then `-Verify`, and review both results before an
explicit `-Live` launch. From the same workspace root:

```powershell
# Default offline preflight; decrypts all four credentials, both trading gates 0.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1

# Equivalent explicit offline preflight.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1 -Check

# Offline check only, without a ledger cycle or trade execution.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1 -Once

# Read-only API, BSC chain/balance, transaction traces, and finalized-tag checks.
# Makes network requests; both trading gates remain 0 and no transaction is sent.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1 -Verify

# Explicit real automation, continuous 3-second cycles.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1 -Live -Interval 3

# Explicit real automation for one cycle only.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1 -Live -Once
```

`-Check` rejects combinations with `-Once` or `-Live`. Interval is 3-60 seconds.
`-Verify` rejects `-Check`, `-Live`, and `-Once`. Verification returns the worker's
exit code; a failed or incomplete verification must be resolved before the
separate explicit live command. Verification does not automatically start live
trading or save an authorization flag. Only `-Live` sets either trading gate to 1.
The worker requires `verify_connection()` to return `ready: true`; an older
transport returning only `ok: true` is incomplete and must be updated before use.
There is no unconfigured public-address-only success: configured checks require
the protected credentials and worker. The launcher waits for Python, forwards
redacted status output, returns its exit code, and terminates its supervised
worker on interruption. There is no automatic restart after exit or reboot.

## Worker Contract And Shared Account State

The launcher calls exactly `alpha_okx_live_worker.py --check`, `--verify`, `--once`, or
`--live --interval 3` (plus `--once` when requested). It fixes the working
directory to the workspace root so the original signal input remains
`outputs/bsc-execution-input.json`. It does not implement a new strategy.

| Child environment | Default / `-Check` / `-Verify` / `-Once` | Explicit `-Live` |
| --- | --- | --- |
| `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` | Decrypted protected API credentials | Same |
| `BSC_PRIVATE_KEY` | Decrypted dedicated wallet key | Same |
| `BSC_WALLET_ADDRESS` | Configured lowercase public address | Same |
| `BSC_RPC_URL` | Configured RPC endpoint, including a path key if required | Same |
| `BSC_EXECUTION_CAPITAL_USD` | `100` | `100` |
| `OKX_SLIPPAGE_PERCENT` | Configured integer, default `5` | Same |
| `OKX_LIVE_ENABLED` | `0` | `1` |
| `OKX_ALLOW_AUTOMATED_TRADES` | `0` | `1` |

All inherited `OKX_*`, `BSC_*`, `GMGN_*`, Python overrides, `HOME`, and legacy
`LIVE_TRADING_ENABLED`/`ARMED` values are removed from the worker environment.
The launcher then supplies the table above and fixes `USERPROFILE` to the actual
Windows profile. The parent environment is unchanged. Credentials travel only
in the worker's environment, not its command line. Worker stdout/stderr are
redacted against the loaded credentials and full configured RPC URL before being forwarded; workers must
also avoid writing credentials to their own files or reports. The worker must
use this environment directly and must not load old credentials from `.env`.

The account ledger root deliberately remains
`~/.config/alpha-radar/gmgn-live/<lowercase-wallet>/`, shared with GMGN across
checkouts and providers. It is not an OKX-specific ledger. Configs pointing to
another root are rejected and there is no state-path switch. The shared
`gmgn-live/operator-launcher.lock` excludes overlapping GMGN/OKX live launchers.
Live execution must use the same account lock and ledger schema, including
direct CLI launches. `-Once` without `-Live` does not open a ledger cycle.
Separate Windows accounts or machines do not share this ledger.

The worker also enforces the ledger's provider binding. A populated legacy GMGN
ledger is treated as GMGN even if it has no explicit `provider` field; OKX stops
with `ledger_provider_mismatch`. An explicit provider binding also prevents
alternating providers against that account. Prior orders still count as ledger
history even after positions close. Changing checkout, config state paths, or
environment variables does not create an independent allowance.

Migration requires reconciling the original provider's orders, fills, positions,
pending transactions, and accumulated losses, followed by a deliberate compatible
ledger migration. There is no automatic migration/rebinding command in these
scripts. Do not delete the ledger, edit its provider field, or choose another
state root to bypass the binding. Offline/API verification does not itself open
and validate the account ledger or authorize a provider migration.

Retain the original `strict_45m` entry/exit policy, including its existing exits:

| Original account cap | Value |
| --- | --- |
| Reference capital | USD 100 |
| Maximum order | USD 5 |
| Maximum positions | 3 |
| Maximum aggregate exposure across providers | USD 15 |
| Daily loss limit across providers | USD 8 |
| First-observation entry window | 45 minutes |

The worker and existing policy enforce these caps against the shared account
ledger; the launcher exposes no cap overrides. Reference capital is not an
on-chain balance. The local signer wallet needs native BNB for the input amount
plus gas. Live preflight/execution must obtain authoritative BSC (chain 56) BNB
balance and gas requirements through RPC; a USDT-only wallet or an OKX exchange
account balance does not fund this local BSC signer automatically.

Keep the continuous worker and machine running for local exit checks. A single
live cycle does not keep monitoring afterwards. Stopping the worker does not
close existing positions. Preserve the account ledger, pending orders, and loss
history when switching providers, rotating API credentials, or restarting.

## Verification Scope

### Private Operator Dashboard

On 2026-09-08 the configured account passed the launcher's read-only `-Verify`
check (`provider_permissions_verified: true`, `live_started: false`). This is
configuration/API/chain readiness evidence, not a completed real buy/sell test.

`py -3 work/alpha-pool-radar/alpha_live_dashboard.py` serves a read-only account
view at `http://127.0.0.1:8771/`. It reads the configured wallet identity and an
explicitly filtered projection of its `status.json`; it never decrypts secrets,
starts a worker, or creates a ledger. Missing status means not started, not zero
profit. A heartbeat older than 90 seconds is unknown/stale, not running.

This endpoint is deliberately separate from public report/cloud uploads. It
binds loopback only, rejects other Host/Origin values and cross-site requests,
and has no CORS permission. It shows receipt-confirmed orders and USD estimates,
not a promise that all fees are reconciled or an actual wallet balance.

Start live execution separately using the existing operator launcher:

```powershell
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_okx_live.ps1 -Live -Interval 3
```

This command authorizes real automated BSC buys and sells. Keep the launcher
running for exit monitoring. Closing it stops monitoring, not existing positions.
The independent `execution-challenger` paper experiment is not a live input.

### Original Script-Only Test Scope

This change is limited to the two PowerShell scripts and this document. Validation
uses PowerShell parsing and offline synthetic fixtures only, including DPAPI
roundtrip, local address derivation, child environment isolation, output
redaction, and mode selection. Live environment construction can be inspected
in memory without starting a live process. No real credential provisioning,
API call, transaction, live activation, deployment, or automatic startup is part
of validation. Existing GMGN scripts are not modified.

Local fixture checks passed on Windows PowerShell 5.1 and PowerShell 7.6:
parsing, key derivation and malformed-key rejection, DPAPI roundtrip/private ACL,
inherited environment removal, path quoting, and output redaction. Windows
PowerShell 5.1 subprocess fixtures also verified default check, explicit check,
offline once, credential loading with both gates zero, exit-code propagation,
conflicting-mode rejection, fixed state/secret paths, and missing-dependency
fallback. Live CLI and environment cases were inspected in memory only.

The `-Verify` launcher extension passed offline subprocess fixtures for
`--verify` forwarding, all four DPAPI credential reads, both gates zero, output
redaction, exit-code propagation, and conflicts with check/live/once. This tests
launcher wiring with fixtures, not a real RPC endpoint's trace capability.

The actual OKX worker also passed configured `--check` and offline `--once`
through the launcher using in-memory synthetic config and credential reads,
with Python socket connections blocked. Both returned exit 0, reported the
fixed shared wallet path and `live_started: false`, and exposed no fixture
credentials. No real config, credential, or account ledger was loaded by these
fixture checks.

Run the parser locally without executing either script:

```powershell
foreach ($file in @('setup_okx_live.ps1', 'start_okx_live.ps1')) {
    $tokens = $null
    $parseErrors = $null
    $path = Join-Path '.\work\alpha-pool-radar' $file
    [void][Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path), [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "$file failed PowerShell parsing" }
    Write-Host "$file parsed successfully"
}
```

If `alpha_okx_live_worker.py` is absent, launch stops with an explicit missing
worker error before reading credentials. The offline worker check, once the
worker is installed, calls `transport.preflight()` without RPC or API requests.
The transport preflight validates local API credential presence, the RPC URL,
and wallet-key/address matching without signing a transaction or making network
requests. DPAPI decryption alone does not establish credential validity. Offline success does not establish
API authorization, RPC availability, or available on-chain funds. Run `-Verify`
for the separate read-only API/chain/balance/trace/finalized checks before `-Live`. Verification
success does not establish an accepted transaction, confirmed first fill, or
successful exit. Real provider verification and trading remain untested by this
script-only, offline fixture change.
