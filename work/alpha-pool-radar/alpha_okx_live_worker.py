"""Explicitly enabled OKX executor using the existing BSC policy and account ledger."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time

from alpha_gmgn_live_worker import ExecutionError, GmgnLiveWorker, ROOT, STATE_ROOT, address


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "outputs" / "bsc-execution-input.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args(argv)
    if not 2 <= args.interval <= 60:
        parser.error("interval must be 2..60 seconds")
    enabled = False
    try:
        if args.live and not (os.environ.get("OKX_LIVE_ENABLED") == "1" and
                              os.environ.get("OKX_ALLOW_AUTOMATED_TRADES") == "1"):
            raise ExecutionError("explicit_live_environment_required")
        from alpha_okx_live_transport import OkxLiveTransport
        transport = OkxLiveTransport()
        transport.preflight()
        wallet = address(transport.wallet_address)
        verified = False
        if args.verify or args.live:
            result = transport.verify_connection()
            verified = isinstance(result, dict) and result.get("ready") is True
            if not verified:
                raise ExecutionError("provider_verification_incomplete")
        if not args.live:
            print(json.dumps({"configured": True, "live_started": False, "wallet": wallet,
                              "provider": "okx", "ledger": str(STATE_ROOT / wallet),
                              "provider_permissions_verified": verified}))
            return 0
        worker = GmgnLiveWorker(args.input, transport, enabled=True, provider="okx",
                               slippage_percent=os.environ.get("OKX_SLIPPAGE_PERCENT", "5"))
        enabled = True
        while True:
            result = worker.run_once()
            print(json.dumps({k: v for k, v in result.items() if k not in {"orders", "positions"}}), flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ExecutionError) else getattr(error, "code", "configuration_or_ledger_error")
        reason = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code) else "execution_error"
        print(json.dumps({"status": "stopped", "reason": reason, "live_started": enabled,
                          "check_pending_orders": enabled}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
