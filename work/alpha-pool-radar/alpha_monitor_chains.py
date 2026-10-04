from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ARC_CHAIN_ID = 5042
ARC_CHAIN_ID_HEX = "0x13b2"


@dataclass(frozen=True)
class MonitorChain:
    name: str
    chain_id: int
    explorer_url: str
    dexscreener_slug: str


MONITOR_CHAINS = {
    "bsc": MonitorChain("bsc", 56, "https://bscscan.com", "bsc"),
    "robinhood": MonitorChain("robinhood", 4663, "https://explorer.testnet.chain.robinhood.com", "robinhood"),
    "arc": MonitorChain("arc", ARC_CHAIN_ID, "https://explorer.arc.io", "arc"),
}

MONITOR_CHAIN_ALIASES = {
    "56": "bsc",
    "0x38": "bsc",
    "bsc": "bsc",
    "bnb": "bsc",
    "binance-smart-chain": "bsc",
    "4663": "robinhood",
    "0x1237": "robinhood",
    "robinhood": "robinhood",
    "robinhood-chain": "robinhood",
    "5042": "arc",
    "0x13b2": "arc",
    "arc": "arc",
    "arc-mainnet": "arc",
    "arc_mainnet": "arc",
}


def normalize_monitor_chain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return MONITOR_CHAIN_ALIASES.get(raw, raw)


def monitor_chain_id(value: Any) -> int | None:
    config = MONITOR_CHAINS.get(normalize_monitor_chain(value))
    return config.chain_id if config else None


def monitor_chain_names() -> frozenset[str]:
    return frozenset(MONITOR_CHAINS)
