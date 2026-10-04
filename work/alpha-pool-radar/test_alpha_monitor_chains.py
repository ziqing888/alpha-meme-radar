from alpha_monitor_chains import (
    ARC_CHAIN_ID,
    ARC_CHAIN_ID_HEX,
    monitor_chain_id,
    monitor_chain_names,
    normalize_monitor_chain,
)


def test_arc_aliases_normalize_to_arc():
    for value in ("arc", "ARC", "5042", 5042, "0x13b2", "arc-mainnet", "arc_mainnet"):
        assert normalize_monitor_chain(value) == "arc"


def test_monitor_registry_contains_arc_without_defining_execution_policy():
    assert ARC_CHAIN_ID == 5042
    assert ARC_CHAIN_ID_HEX == "0x13b2"
    assert monitor_chain_id("arc") == 5042
    assert monitor_chain_names() == frozenset({"bsc", "robinhood", "arc"})
