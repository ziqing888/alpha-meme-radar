# Local GMGN Operator

This launcher connects the existing BSC strategy to `alpha_gmgn_live_worker.py`.
It does not install a service, schedule a task, start scanners, or change paper
trading. Setup never starts the worker. With no switches, the launcher runs the
offline `--check` mode. The first actual fill has not yet been verified.
Backend execution is local; no deployment is necessary.

## Credentials

Prepare these in your GMGN account when you are ready:

- A GMGN API key authorized for the intended hosted wallet.
- The GMGN **API request signing private key in PEM format** (Ed25519 or RSA), in a local file
  outside this repository. This is not a wallet blockchain private key or seed.
- `GMGN_WALLET_ADDRESS`: the public address of that GMGN hosted BSC wallet.

A publicly connected wallet/address alone does not authorize automated execution.
The launcher does not request, import, or derive a blockchain wallet private key.
Credential provisioning and trade authorization must match the hosted wallet.

From the workspace root, run setup interactively only after obtaining credentials:

```powershell
powershell.exe -NoProfile -File .\work\alpha-pool-radar\setup_gmgn_live.ps1
```

The script prompts for a public wallet address, a PEM file path, and the API key
using hidden input. Optionally pass `-WalletAddress`, `-ApiSigningKeyPath`, and
`-PythonPath` (an executable path, not a shell command). Never pass the API key or
PEM contents as command arguments. Setup validates the PEM envelope locally; it
does not authenticate with GMGN or verify account permissions.

Setup saves `~/.config/alpha-radar/gmgn-live-config.json` containing only the public
address, `api_key_masked: "********"`, DPAPI file paths, Python executable path,
schema version, integer slippage (default 5%), the public `bsc_rpc_url` (default
`https://bsc-dataseed.binance.org/`), and the fixed state path. API key and PEM text are encrypted with
Windows DPAPI for the current Windows user and stored in `gmgn-live-secrets`
beside the config. Config, secrets, and state directory ACLs grant access to the
current user and SYSTEM. No credential files are created in the repository,
browser, dashboard, or public site. The source PEM file remains user-managed;
setup leaves it in place and does not store its original path in the JSON.

Repeat setup as the same Windows user to rotate credentials for the same wallet.
It writes new encrypted files before replacing the config, preserving the prior
config if a write fails. Old encrypted versions are retained. Another Windows
user or machine cannot decrypt them through this launcher. Do not change wallets
or delete account state to reset limits; reconcile existing orders and positions
before any account migration.

## Modes

Run from the workspace root. `py.exe` with Python 3 is the default interpreter;
setup can select another Python executable containing the worker dependencies.
The configured offline check and real provider operations require Node.js on PATH
and the built vendor CLI at `work/external/open-source-radar/gmgn-skills/dist/index.js`.
The worker's offline `preflight()` checks local credential/PEM structure, runtime
availability, and RPC URL format without network requests. It does not verify API
authorization or wallet binding. Actual signing-key validation occurs when the
transport invokes Node crypto.

```powershell
# Before setup only: public-address structure check, configured=false.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Check -WalletAddress 0x1111111111111111111111111111111111111111

# After setup: default offline check against the configured public address.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1

# Equivalent configured offline preflight: credentials loaded, trading disabled.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Check

# One worker cycle, live authorization remains off.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Once

# Explicit operator action, when ready: real automated execution every 3 seconds.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Live -Interval 3

# Explicit operator action: one live cycle, including any eligible submissions.
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Live -Once
```

`-Check` rejects combination with `-Live` or `-Once`. `-WalletAddress` is accepted
only for offline checks; with a saved config it must match that account. Live and
recovery execution always use the saved hosted wallet. Without setup, the fixture
address above only tests public-address syntax: the launcher reports
`configured: false`, `api_credentials_loaded: false`, `runtime_checked: false`,
and `preflight_completed: false`, then exits without starting Python. Exit 0 in
this case means only that the address structure is valid. Without either a public
address or setup, the default fails closed. Interval must be 3-60 seconds. There is no
automatic start after setup, login, reboot, or a failure. The launcher waits for
its worker and returns the worker exit code. Ctrl+C stops the supervised Python
worker; interrupted submissions may still need remote order reconciliation.

For a configured account, the launcher decrypts credentials for offline `-Check`,
disabled `-Once`, recovery, and `-Live`. It puts them into the child environment
without printing their values or putting them on the command line. Only `-Live`
sets both trading gates to `1`; possession of credentials does not enable trades.
It does not modify the parent process environment. Inherited `GMGN_*`
variables are cleared before constructing the child environment:

| Child environment | Configured check / disabled once / recovery | Explicit `-Live` |
| --- | --- | --- |
| `GMGN_API_KEY` | Decrypted API key | Decrypted API key |
| `GMGN_PRIVATE_KEY` | Decrypted API signing PEM text, not a path | Decrypted API signing PEM text, not a path |
| `GMGN_WALLET_ADDRESS` | Configured hosted wallet | Configured hosted wallet |
| `GMGN_ALLOW_AUTOMATED_TRADES` | `0` | `1` |
| `GMGN_LIVE_ENABLED` | `0` | `1` |
| `GMGN_SLIPPAGE_PERCENT` | Configured integer, default `5` | Configured integer, default `5` |
| `BSC_RPC_URL` | Configured public URL or default | Configured public URL or default |

The worker reads process environment directly; it does not need to parse the
operator JSON. The launcher's environment contract is intentionally limited to
these GMGN variables. It also sets `BSC_EXECUTION_CAPITAL_USD=100` for the existing
execution policy. Credentials are transient in child process memory.
To set slippage, edit only `slippage_percent` in the local operator JSON to an
integer from 1 through 10. Other values are rejected before worker startup.
`bsc_rpc_url` overrides inherited `BSC_RPC_URL`; older configs without the field
use the same public default. It is a nonsecret HTTP(S) URL, with no embedded
credentials, query, or fragment. The transport uses read-only, chain-56-checked
RPC for authoritative native BNB balances and transaction-sender verification.
Do not put an authenticated provider token into this plaintext config field.

## Missing Order Recovery

Recovery also verifies the execution-block timestamp against the intent's
creation time. A historical same-wallet/same-token trade is not sufficient.
Confirmed partial inputs may be reconciled up to the intended amount. If a
failed provider order contains no request token/amount evidence, the worker
retains the reservation and reports
`failed_recovery_requires_provider_request_evidence`; do not clear the ledger
to bypass this state. Such a provider-side ambiguity needs manual order review.

When an ambiguous submission lacks a provider order ID, use the existing intent's
64-character lowercase `key` from the local ledger/status and the matching GMGN
provider order ID. For example, with those two actual values in local variables:

```powershell
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -RecoverOrder $providerOrderId -IntentKey $intentKey
```

Both parameters are required. Recovery rejects `-Live`, `-Check`, `-Once`, and
`-WalletAddress`. It passes `--attach-order ID --intent-key KEY` with credentials
loaded and both trading flags `0`. It queries the provider, verifies the wallet
using receipt evidence or read-only transaction-sender RPC, checks the token pair
and input amount, and updates the existing local ledger only after validation.
It never submits or resubmits a trade. Recovery is networked and writes local
reconciliation state; it is not the offline check and does not create a new
independent ledger. A pending or unverifiable receipt must not be treated as a fill.

The shared launcher lock excludes simultaneous live and recovery launches. Stop
the live launcher for recovery, reconcile, then explicitly restart continuous
live monitoring. Locally managed exits are not monitored during that interruption.

## State And Strategy

The account state root is fixed at `~/.config/alpha-radar/gmgn-live`, outside the
checkout. Every checkout launched by this Windows user uses this same root. There
is no launcher state-directory override, and an edited config pointing elsewhere
is rejected. A file lock in that root prevents overlapping live/recovery launchers. The
worker stores the ledger in a lowercased wallet-address subdirectory and locks
that account during each cycle, including direct CLI invocation. The launcher
lock alone does not protect against a separately started worker.
Use one operator Windows account and machine for this hosted wallet. Separate
Windows users or machines do not share this local ledger.

Account files are `~/.config/alpha-radar/gmgn-live/<wallet>/ledger.sqlite3` and
`status.json`. The database retains orders, fills, positions, and risk state.
The status JSON provides the worker's latest operational state. Keep both local.

To pause new buys while keeping exits running, create an empty `PAUSE_ENTRIES`
file in that wallet directory. Delete only this marker to resume entry checks.
For example, after setup, from PowerShell:

```powershell
$operatorConfig = Get-Content "$env:USERPROFILE/.config/alpha-radar/gmgn-live-config.json" -Raw | ConvertFrom-Json
$pauseFile = Join-Path (Join-Path $operatorConfig.state_root $operatorConfig.wallet_address) 'PAUSE_ENTRIES'
New-Item -ItemType File -Path $pauseFile -Force | Out-Null
# Resume only when intended:
# Remove-Item -LiteralPath $pauseFile
```

Keep the continuous live worker running while paused. Pausing buys does not
cancel already submitted orders.

The worker's signal input defaults to `outputs/bsc-execution-input.json` relative
to the workspace. The launcher fixes the working directory to the workspace root
even when called from another directory. Existing scanners supply that input;
this launcher does not start or alter them.

The worker retains the original `strict_45m` policy and caps:

| Setting | Value |
| --- | --- |
| Reference strategy capital | USD 100 |
| Maximum order notional | USD 5 |
| Maximum open positions | 3 |
| Maximum aggregate exposure | USD 15 |
| Daily loss limit | USD 8 |
| First-observation entry window | 45 minutes |

Caps and entry/exit decisions belong to the worker and existing execution policy;
they are not configurable through this launcher. A reference capital value is
not proof of a funded account. The offline check is not proof of authentication,
an accepted order, a confirmed fill, or a working exit.

Buys spend native BNB, with the BNB amount recalculated from the USD 5 limit and
GMGN's gas-price native-USD valuation. The hosted wallet needs BNB trading funds
plus BNB for gas. A wallet funded only with USDT is insufficient for this route.
The worker does not create GMGN conditional orders. All stop-loss, take-profit,
trailing, and time-based exits are managed by this local worker.

## Operation And Verification

Keep the local machine powered on, awake, connected, and the continuous live
worker running for locally managed exits. Closing the worker, sleeping, rebooting,
or losing connectivity stops those checks. `-Live -Once` does not keep monitoring
positions after its one cycle. Stopping the launcher does not itself close open
positions or cancel remote orders. Inspect and reconcile existing state and
GMGN orders before restarting after interruption; do not clear the ledger.

Public-only validation command before setup (no credentials or worker preflight):

```powershell
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Check -WalletAddress 0x1111111111111111111111111111111111111111
```

Validated locally with Windows PowerShell 5.1:

- Both scripts parse without errors.
- The public-only invocation above exits 0 with `configured: false`,
  `live_started: false`, and `preflight_completed: false`.
- In-memory tests cover default-off flags, explicit live child environment
  construction (no process started), removal of inherited secrets and state
  overrides, parent environment isolation, multiline PEM preservation, path
  quoting, slippage settings, DPAPI roundtrip, and sanitized credential errors.
- Configured checks, disabled once, and recovery child environments contain
  fixture credentials with both trading gates `0`; explicit live construction
  alone sets them to `1`. No recovery or live process is started by these tests.
- Public-only checks ignore fake inherited credentials and print none of them.

After setup, the full offline invocation is:

```powershell
powershell.exe -NoProfile -File .\work\alpha-pool-radar\start_gmgn_live.ps1 -Check
```

This configured invocation now requires DPAPI credentials and the local runtime.
An additional configured-launcher smoke test passed against the real worker's
offline preflight using in-memory fake config and mocked DPAPI reads. Both API
key and multiline PEM reached the child, both trading gates remained `0`, the
worker exited 0, and no fixture credential appeared in output. This test did not
read real credentials, contact the provider, or start live execution.
It has not been run against real credentials. A former public-address-only
`configured: true` result predates the required transport preflight and does not
establish current configured readiness.

The check constructs local worker configuration and may create the wallet state
directory, but does not submit orders. If the worker file is not yet present, the
launcher exits with a missing-worker error before any process starts.
No real startup, credential provisioning, or actual fill is part of this setup
and documentation change. **First actual fill: not yet verified.**
**API authorization: not tested until credentials are provided.**

Remaining operator setup: obtain the provider API key, verify that its account
permissions bind it to the intended GMGN hosted BSC wallet, and supply the matching
GMGN API signing private key in PEM format (not the blockchain wallet key).
Run setup and the configured offline check after those are available. Successful
offline preflight still leaves actual provider authorization, wallet binding,
signing acceptance, and the first confirmed fill unverified.
