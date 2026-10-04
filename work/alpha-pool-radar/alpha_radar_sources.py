#!/usr/bin/env python3
from __future__ import annotations

import os


BINANCE_ALPHA_LIST_URL = (
    "https://www.binance.com/bapi/defi/v1/public/"
    "wallet-direct/buw/wallet/cex/alpha/all/token/list"
)
DEX_TOKEN_PAIRS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{address}"

FAPI_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FAPI_24H_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FAPI_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FAPI_OI_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"

ETHERSCAN_V2_API_URL = "https://api.etherscan.io/v2/api"
ROUTESCAN_ETHERSCAN_API_URL = "https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api"
BSC_SCAN_TOKEN_HOLDERS_URL = "https://bscscan.com/token/generic-tokenholders2"
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

GOPLUS_TOKEN_SECURITY_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"

BLOCKSCOUT_API_BASES = {
    "1": "https://eth.blockscout.com/api",
    "10": "https://optimism.blockscout.com/api",
    "8453": "https://base.blockscout.com/api",
    "42161": "https://arbitrum.blockscout.com/api",
}

CHAIN_ID_TO_DEX = {
    "1": "ethereum",
    "10": "optimism",
    "56": "bsc",
    "130": "unichain",
    "137": "polygon",
    "146": "sonic",
    "324": "zksync",
    "5000": "mantle",
    "8453": "base",
    "42161": "arbitrum",
    "43114": "avalanche",
    "534352": "scroll",
    "59144": "linea",
    "81457": "blast",
    "solana": "solana",
}

REQUEST_HEADERS = {
    "User-Agent": "alpha-pool-radar/0.1 read-only",
    "Accept": "application/json,text/plain,*/*",
}
