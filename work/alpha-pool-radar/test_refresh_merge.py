from alpha_meme_live_refresh import merge_refresh_changes


def test_meme_refresh_preserves_new_alpha_holders():
    previous = {"alpha_rows": [{"top10_holder_pct": None}], "meme_rows": [1], "meta": {"holder_coverage": 0, "meme_count": 1}}
    updated = {**previous, "meme_rows": [2, 3], "meta": {**previous["meta"], "meme_count": 2}}
    latest = {**previous, "alpha_rows": [{"top10_holder_pct": 30}], "meta": {**previous["meta"], "holder_coverage": 100}}
    result = merge_refresh_changes(previous, updated, latest)
    assert result["alpha_rows"] == latest["alpha_rows"]
    assert result["meta"]["holder_coverage"] == 100
    assert result["meme_rows"] == [2, 3]
    assert result["meta"]["meme_count"] == 2
