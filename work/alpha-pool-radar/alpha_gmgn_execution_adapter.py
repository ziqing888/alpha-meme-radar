"""GMGN execution intent adapter.

This module maps the local meme strategy into GMGN CLI arguments without
submitting a transaction. It is deliberately preview-only until a separate,
operator-controlled execution boundary is implemented and verified.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
GMGN_ROOT = ROOT / "work" / "external" / "open-source-radar" / "gmgn-skills"
GMGN_DIST = GMGN_ROOT / "dist" / "index.js"

SUPPORTED_CHAINS = frozenset({"sol", "bsc", "base", "eth", "robinhood", "stable"})
NATIVE_TOKENS = {
    "sol": "So11111111111111111111111111111111111111112",
    "bsc": "0x0000000000000000000000000000000000000000",
    "base": "0x0000000000000000000000000000000000000000",
    "eth": "0x0000000000000000000000000000000000000000",
}
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class GmgnAdapterError(ValueError):
    """Stable, secret-free error for invalid execution intents."""


@dataclass(frozen=True)
class GmgnExecutionAdapter:
    """Build GMGN execution requests while remaining preview-only."""

    wallet_address: str
    runner: tuple[str, ...] | None = None
    anti_mev: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GmgnExecutionAdapter":
        values = os.environ if env is None else env
        wallet = str(values.get("GMGN_WALLET_ADDRESS") or values.get("BSC_WALLET_ADDRESS") or "").strip()
        if not wallet:
            raise GmgnAdapterError("missing_wallet_address")
        configured = str(values.get("GMGN_CLI_PATH") or "").strip()
        if configured:
            path = Path(configured)
            if path.exists() and path.suffix.lower() == ".js":
                runner = ("node", str(path))
            elif path.exists():
                runner = (str(path),)
            else:
                found = shutil.which(configured)
                runner = (found,) if found else None
        else:
            found = shutil.which("gmgn-cli")
            runner = (found,) if found else (("node", str(GMGN_DIST)) if GMGN_DIST.exists() else None)
        return cls(wallet_address=wallet, runner=runner)

    @staticmethod
    def _chain(value: Any) -> str:
        chain = str(value or "").strip().lower()
        if chain == "solana":
            chain = "sol"
        if chain not in SUPPORTED_CHAINS:
            raise GmgnAdapterError("unsupported_chain")
        return chain

    @staticmethod
    def _address(value: Any, *, chain: str, code: str) -> str:
        address = str(value or "").strip()
        if not address:
            raise GmgnAdapterError(code)
        if chain in {"bsc", "base", "eth"} and not EVM_ADDRESS_RE.fullmatch(address):
            raise GmgnAdapterError(code)
        return address

    @staticmethod
    def _positive_int(value: Any) -> str:
        text = str(value or "").strip()
        if not text.isdigit() or int(text) <= 0:
            raise GmgnAdapterError("invalid_amount")
        return text

    @staticmethod
    def _slippage(value: Any) -> str:
        text = str(value or "").strip()
        if text.lower() in {"auto", "automatic"}:
            return "auto"
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            raise GmgnAdapterError("invalid_slippage") from None
        if parsed < 0 or parsed > 100:
            raise GmgnAdapterError("invalid_slippage")
        return str(int(parsed)) if parsed.is_integer() else text

    @staticmethod
    def strategy_condition_orders() -> list[dict[str, str]]:
        """Map the current paper exit policy to GMGN condition orders."""
        return [
            {"order_type": "profit_stop", "side": "sell", "price_scale": "100", "sell_ratio": "80"},
            {"order_type": "loss_stop", "side": "sell", "price_scale": "22", "sell_ratio": "100"},
        ]

    def build_swap_command(
        self,
        *,
        side: str,
        token_address: str,
        amount_atomic: Any,
        slippage_percent: Any,
        chain: Any = "bsc",
        input_token: str | None = None,
        output_token: str | None = None,
        condition_orders: list[dict[str, str]] | None = None,
    ) -> list[str]:
        selected_chain = self._chain(chain)
        selected_side = str(side or "").strip().lower()
        if selected_side not in {"buy", "sell"}:
            raise GmgnAdapterError("invalid_side")
        token = self._address(token_address, chain=selected_chain, code="invalid_token_address")
        wallet = self._address(self.wallet_address, chain=selected_chain, code="invalid_wallet_address")
        amount = self._positive_int(amount_atomic)
        slippage = self._slippage(slippage_percent)
        if selected_side == "buy":
            in_token = input_token or NATIVE_TOKENS.get(selected_chain)
            out_token = output_token or token
        else:
            in_token = input_token or token
            out_token = output_token or NATIVE_TOKENS.get(selected_chain)
        if not in_token or not out_token:
            raise GmgnAdapterError("missing_counter_token")
        in_token = self._address(in_token, chain=selected_chain, code="invalid_input_token")
        out_token = self._address(out_token, chain=selected_chain, code="invalid_output_token")
        runner = list(self.runner or ("gmgn-cli",))
        command = [
            *runner,
            "swap",
            "--chain", selected_chain,
            "--from", wallet,
            "--input-token", in_token,
            "--output-token", out_token,
            "--amount", amount,
        ]
        if slippage == "auto":
            command.append("--auto-slippage")
        else:
            command.extend(["--slippage", slippage])
        if self.anti_mev and selected_chain in {"sol", "bsc", "eth"}:
            command.append("--anti-mev")
        if condition_orders:
            command.extend(["--condition-orders", json.dumps(condition_orders, separators=(",", ":"), ensure_ascii=True)])
            command.extend(["--sell-ratio-type", "buy_amount"])
        command.extend(["--raw"])
        return command

    def preview(self, **kwargs: Any) -> dict[str, Any]:
        """Return an auditable intent; never invoke the command."""
        command = self.build_swap_command(**kwargs)
        side = str(kwargs.get("side") or "").strip().lower()
        return {
            "ok": True,
            "status": "preview",
            "provider": "gmgn",
            "side": side,
            "chain": self._chain(kwargs.get("chain", "bsc")),
            "token_address": str(kwargs.get("token_address") or "").strip(),
            "amount_atomic": self._positive_int(kwargs.get("amount_atomic")),
            "command": command,
            "submitted": False,
        }

    def buy(self, token_address: str, _pool_address: str, amount_atomic: Any, slippage_percent: Any, *, chain_id: Any = 56, **kwargs: Any) -> dict[str, Any]:
        chain = kwargs.pop("chain", None) or ("bsc" if str(chain_id) == "56" else "")
        input_token = kwargs.pop("from_token_address", None)
        return self.preview(
            side="buy", token_address=token_address, amount_atomic=amount_atomic,
            slippage_percent=slippage_percent, chain=chain, input_token=input_token, **kwargs,
        )

    def sell(self, token_address: str, _pool_address: str, amount_atomic: Any, slippage_percent: Any, *, chain_id: Any = 56, **kwargs: Any) -> dict[str, Any]:
        chain = kwargs.pop("chain", None) or ("bsc" if str(chain_id) == "56" else "")
        output_token = kwargs.pop("to_token_address", None)
        return self.preview(
            side="sell", token_address=token_address, amount_atomic=amount_atomic,
            slippage_percent=slippage_percent, chain=chain, output_token=output_token, **kwargs,
        )


__all__ = ["GmgnAdapterError", "GmgnExecutionAdapter", "NATIVE_TOKENS", "SUPPORTED_CHAINS"]
