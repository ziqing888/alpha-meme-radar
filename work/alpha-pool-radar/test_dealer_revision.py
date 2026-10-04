import alpha_market as market
from alpha_dealer import dealer_dimensions
from alpha_recommendations import classify_row, score_of


def test_quantity_oi_does_not_count_price_appreciation(monkeypatch):
    monkeypatch.setattr(market, "http_json", lambda _: [
        {"sumOpenInterest": "100", "sumOpenInterestValue": "1000", "timestamp": 100},
        {"sumOpenInterest": "100", "sumOpenInterestValue": "1300", "timestamp": 200},
    ])
    data = market.fetch_oi_metrics("TESTUSDT")
    assert data["oi_change_1h_pct"] == 0
    assert data["oi_value_change_1h_pct"] == 30


def test_missing_quantity_is_not_zero(monkeypatch):
    monkeypatch.setattr(market, "http_json", lambda _: [
        {"sumOpenInterestValue": "1000"}, {"sumOpenInterestValue": "1300"},
    ])
    assert market.fetch_oi_metrics("TESTUSDT")["oi_change_1h_pct"] is None


def test_funding_interval_and_normalization(monkeypatch):
    monkeypatch.setattr(market, "http_json", lambda _: [
        {"fundingRate": "-0.0001", "fundingTime": 3600000},
        {"fundingRate": "-0.0002", "fundingTime": 7200000},
    ])
    data = market.fetch_funding_metrics("TESTUSDT")
    assert data["funding_interval_hours"] == 1
    assert data["funding_rate_8h_pct"] == -0.16
    assert data["funding_settled_at_ms"] == 7200000


def test_stage_long_cannot_bypass_risk():
    row = {"action": "做多", "stage": "启动", "top10_holder_pct": 80, "max_holder_pct": 5}
    assert classify_row(row)["recommendation_bucket"] == "danger"


def test_unknown_holders_cannot_bypass_risk():
    assert classify_row({"action": "做多", "score": 100})["recommendation_action"] == "仅观察"


def test_risk_bonus_is_not_an_opportunity():
    row = {"market_cap": 10000000, "alpha_volume24h": 5000000,
           "top10_holder_pct": 30, "max_holder_pct": 5}
    baseline = dealer_dimensions(row)
    assert dealer_dimensions({**row, "dex_liquidity": 1, "funding_rate_pct": -1})["dealer_opportunity_score"] == baseline["dealer_opportunity_score"]
    assert dealer_dimensions({**row, "top10_holder_pct": 80})["dealer_opportunity_score"] == 0
    assert dealer_dimensions({})["dealer_opportunity_score"] is None
    assert score_of({"score": 80, "dealer_score": 100, "dealer_opportunity_score": 0}) == 0
