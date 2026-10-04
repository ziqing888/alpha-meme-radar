import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sources = load_module("alpha_radar_sources", BASE / "alpha_radar_sources.py")
alpha = load_module("alpha_pool_radar", BASE / "alpha_pool_radar.py")


def test_source_registry_keeps_public_data_endpoints_together():
    assert sources.BINANCE_ALPHA_LIST_URL.startswith("https://www.binance.com/bapi/defi/")
    assert sources.DEX_TOKEN_PAIRS_URL == "https://api.dexscreener.com/tokens/v1/{chain}/{address}"
    assert sources.FAPI_OI_HIST_URL.startswith("https://fapi.binance.com/")
    assert sources.RUGCHECK_REPORT_URL.endswith("/tokens/{mint}/report")
    assert sources.CHAIN_ID_TO_DEX["56"] == "bsc"
    assert sources.CHAIN_ID_TO_DEX["solana"] == "solana"
    assert sources.BLOCKSCOUT_API_BASES["8453"] == "https://base.blockscout.com/api"
    assert sources.REQUEST_HEADERS["User-Agent"].startswith("alpha-pool-radar/")


def test_alpha_pool_radar_reexports_source_registry_for_existing_callers():
    assert alpha.BINANCE_ALPHA_LIST_URL == sources.BINANCE_ALPHA_LIST_URL
    assert alpha.DEX_TOKEN_PAIRS_URL == sources.DEX_TOKEN_PAIRS_URL
    assert alpha.CHAIN_ID_TO_DEX is sources.CHAIN_ID_TO_DEX
    assert alpha.REQUEST_HEADERS is sources.REQUEST_HEADERS
