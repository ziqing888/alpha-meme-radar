# Dependency Audit

Snapshot: 2026-10-04

| Package | Result |
| --- | --- |
| `web` | 0 vulnerabilities |
| `work/alpha-pool-radar/terminal-ui` | 0 vulnerabilities |
| `work/alpha-pool-radar/okx-dex-executor` | 0 vulnerabilities |

The executor pins the unused legacy `@okxweb3/coin-ethereum` dependency from
`@okx-dex/okx-dex-sdk@1.0.19` to the current major and pins vulnerable
transitive packages to patched versions through package overrides. A lockfile
regression test rejects the removed legacy signing stack, while an offline
SDK smoke test verifies that the production loader can still derive an EVM
wallet and sign a message. The full executor test suite passes with these
overrides.

Re-run the audit before release:

```powershell
npm --prefix web audit
npm --prefix work\alpha-pool-radar\terminal-ui audit
npm --prefix work\alpha-pool-radar\okx-dex-executor audit --omit=dev
```
