import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_http.py")
SPEC = importlib.util.spec_from_file_location("alpha_http", MODULE_PATH)
alpha_http = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = alpha_http
SPEC.loader.exec_module(alpha_http)


def test_source_name_groups_known_public_sources():
    assert alpha_http.source_name("https://api.dexscreener.com/tokens/v1/bsc/0x1") == "dexscreener"
    assert alpha_http.source_name("https://fapi.binance.com/fapi/v1/ticker/24hr") == "binance_futures"
    assert alpha_http.source_name("https://api.rugcheck.xyz/v1/tokens/mint/report") == "rugcheck"
    assert alpha_http.source_name("https://api.mainnet-beta.solana.com") == "solana_rpc"


def test_data_quality_summary_tracks_live_cached_and_failed_events():
    alpha_http.reset_data_quality()

    alpha_http.record_http_event("https://api.dexscreener.com/tokens/v1/bsc/0x1", "live")
    alpha_http.record_http_event("https://api.rugcheck.xyz/v1/tokens/mint/report", "cached", cache_age_seconds=12.34)
    alpha_http.record_http_event("https://api.mainnet-beta.solana.com", "failed", error="429")

    summary = alpha_http.data_quality_summary()

    assert summary["overall"] == "partial"
    assert summary["request_count"] == 3
    assert summary["by_source"]["dexscreener"]["live"] == 1
    assert summary["by_source"]["rugcheck"]["cached"] == 1
    assert summary["by_source"]["solana_rpc"]["failed"] == 1
