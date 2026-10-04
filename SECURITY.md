# Security Policy

## Supported Version

Security fixes are applied to the default branch. Older commits and forks are
not maintained.

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting feature for this repository. Do
not include credentials, private keys, seed phrases, wallet exports, or live
production data in a public issue.

Include the affected revision, file and line, reachable input, observed impact,
and a minimal reproduction. Reports are evaluated against the repository's
documented trust boundaries.

## Security Boundaries

- Monitoring, intelligence, and execution are independent paths.
- Monitor stages never authorize an order by themselves.
- Only `aggregate_early_bird` and `aggregate_confirmation` may enter an
  execution handoff.
- ARC Mainnet is monitor-only and has no wallet, order, position, or exit path.
- Live execution is disabled until explicitly configured on the operator's
  machine.
- Public payloads must not contain wallet attribution, private evidence IDs,
  secrets, or machine-local paths.

Generated reports, logs, caches, positions, ledgers, credentials, and wallet
material must remain outside Git.

The latest dependency-audit snapshot is recorded in
[`docs/security/dependency-audit.md`](docs/security/dependency-audit.md).
