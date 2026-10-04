# Alpha Radar server runtime

The server runtime keeps each live chain in a separate systemd service:

- `alpha-chain@bsc.service`
- `alpha-chain@robinhood.service`

Installation leaves both services disabled and stopped. A chain cannot start
until its `/etc/alpha-radar/armed/<chain>` marker exists. This prevents a
staging deployment or reboot from creating a second live writer.

Secrets live outside the application tree under `/etc/alpha-radar/secrets`.
Runtime state and execution inputs live under `/var/lib/alpha-radar/outputs`.

Run a non-trading credential and RPC check with:

```bash
/opt/alpha-radar/bin/alpha-preflight bsc
/opt/alpha-radar/bin/alpha-preflight robinhood
```

During the staged migration, `sync_remote_execution.ps1` atomically sends the
small per-chain execution input to the trading host and mirrors its state and
status back to the local terminal. It does not start or arm a live service.

For continuous operation use `alpha_remote_sync.py`; it keeps one authenticated
SSH session open, avoiding a new SSH handshake on every fast signal refresh.
