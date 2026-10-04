#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 77
fi

install -d -m 0755 /opt/alpha-radar/bin /opt/alpha-radar/okx-dex-executor
if ! id alpha-radar >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/alpha-radar --create-home --shell /usr/sbin/nologin alpha-radar
fi
install -d -o alpha-radar -g alpha-radar -m 0750 /var/lib/alpha-radar /var/lib/alpha-radar/outputs
install -d -o root -g alpha-radar -m 0750 /etc/alpha-radar /etc/alpha-radar/secrets
install -d -o root -g root -m 0755 /etc/alpha-radar/armed

install -o root -g root -m 0755 ./alpha-chain-runner.sh /opt/alpha-radar/bin/alpha-chain-runner
install -o root -g root -m 0755 ./alpha-preflight.sh /opt/alpha-radar/bin/alpha-preflight
install -o root -g root -m 0755 ./alpha_sync_bridge.py /opt/alpha-radar/bin/alpha-sync-bridge
install -o root -g root -m 0644 ./alpha-chain@.service /etc/systemd/system/alpha-chain@.service

systemctl daemon-reload
systemctl disable alpha-chain@bsc.service alpha-chain@robinhood.service >/dev/null 2>&1 || true
systemctl stop alpha-chain@bsc.service alpha-chain@robinhood.service >/dev/null 2>&1 || true

echo "alpha trading host prepared; both live services are disabled and stopped"
