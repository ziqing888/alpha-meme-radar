import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("build_vercel_site.py")
SPEC = importlib.util.spec_from_file_location("build_vercel_site", MODULE_PATH)
builder = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def test_build_static_site_writes_vercel_ready_files_with_chinese_ui(tmp_path):
    report = {
        "meta": {
            "report_generated_at": "2026-08-14T10:00:00+08:00",
            "holder_coverage": {"unique_tokens": 1, "with_top10": 1, "coverage_pct": 100.0},
        },
        "dealer_rows": [{"symbol": "QUID", "dealer_score": 76, "chain": "base", "top10_holder_pct": 51.2}],
        "meme_rows": [{"symbol": "nana", "score": 100, "heat_score": 38}],
        "alpha_rows": [{"symbol": "DOS", "score": 71}],
    }
    report_path = tmp_path / "report.json"
    out_dir = tmp_path / "site"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    builder.build_site(report_path, out_dir)

    assert (out_dir / "index.html").exists()
    assert (out_dir / "report.json").exists()
    assert (out_dir / "vercel.json").exists()
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert 'id="root"' in html
    assert "assets/" in html
    report_copy = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report_copy["dealer_rows"][0]["symbol"] == "QUID"


def test_build_site_prefers_react_terminal_frontend_when_available(tmp_path, monkeypatch):
    report = {
        "meta": {"report_generated_at": "2026-08-14T20:00:00+08:00"},
        "dealer_rows": [{"symbol": "AKE", "score": 82, "market_cap": 112_000_000}],
        "meme_rows": [{"symbol": "sricalc", "score": 93, "change_h1": 1010.8}],
        "alpha_rows": [{"symbol": "HOME", "score": 74, "funding_rate_pct": -0.12}],
    }
    report_path = tmp_path / "report.json"
    out_dir = tmp_path / "site"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(builder._site_builder, "WEB_DIR", web_dir)

    build_calls = []

    def fake_run(command, cwd, check=True):
        build_calls.append((command, cwd, check))
        dist = Path(cwd) / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text(
            '<div id="root"></div><script type="module" src="/assets/index-test.js"></script>',
            encoding="utf-8",
        )
        (dist / "assets" / "index-test.js").write_text("console.log('Alpha Radar Terminal')", encoding="utf-8")

    monkeypatch.setattr(builder, "run_frontend_build", fake_run)

    builder.build_site(report_path, out_dir)

    html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert build_calls
    assert (out_dir / "assets" / "index-test.js").exists()
    assert (out_dir / "api" / "report.js").exists()
    assert (out_dir / "package.json").exists()
    assert (out_dir / "report.json").exists()
    assert (out_dir / "vercel.json").exists()
    assert 'id="root"' in html
    assert "assets/index-test.js" in html


def test_build_site_embeds_latest_fast_track_overlay(tmp_path, monkeypatch):
    report = {"meta": {}, "meme_rows": [{"symbol": "DOG", "chain": "bsc"}]}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "alpha-fast-track.json").write_text(json.dumps({
        "updated_at": "2026-09-08T07:00:00+00:00",
        "status": {"ok": True, "fresh_count": 1},
        "gmgn_execution": {"status": "awaiting_local_wallet_address", "count": 0, "submitted": False},
    }), encoding="utf-8")

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(builder._site_builder, "WEB_DIR", web_dir)
    monkeypatch.setattr(builder, "run_frontend_build", lambda command, cwd, check=True: (
        (Path(cwd) / "dist" / "assets").mkdir(parents=True, exist_ok=True),
        (Path(cwd) / "dist" / "index.html").write_text('<div id="root"></div>', encoding="utf-8"),
    )[-1])

    builder.build_site(report_path, tmp_path / "site")
    deployed = json.loads((tmp_path / "site" / "report.json").read_text(encoding="utf-8"))
    assert deployed["meta"]["fast_track"]["gmgn_execution"]["submitted"] is False
    assert deployed["meta"]["fast_track"]["fresh_count"] == 1


def test_react_terminal_frontend_has_chinese_recommendation_contract():
    web_dir = Path(__file__).parents[2] / "web"
    package_json = json.loads((web_dir / "package.json").read_text(encoding="utf-8"))
    app_source = (web_dir / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (web_dir / "src" / "styles.css").read_text(encoding="utf-8")

    assert package_json["scripts"]["build"] == "tsc -b && vite build"
    assert package_json["dependencies"]["lightweight-charts"]
    assert package_json["dependencies"]["@tanstack/react-query"]
    assert "Alpha 雷达工作台" in app_source
    assert "金狗候选" in app_source
    assert "主推候选" in app_source
    assert "埋伏候选" in app_source
    assert "等回踩候选" in app_source
    assert "高危别碰" in app_source
    assert "MEME 实时监控" in app_source
    assert "entry_score" in app_source
    assert "上车分" in app_source
    assert "gold_dog_score" in app_source
    assert "金狗分" in app_source
    assert "freshness_score" in app_source
    assert "新鲜度" in app_source
    assert "narrative_score" in app_source
    assert "narrative_tags" in app_source
    assert "叙事分" in app_source
    assert "叙事" in app_source
    assert "pro_signal_score" in app_source
    assert "pro_signal_tags" in app_source
    assert "专业分" in app_source
    assert "专业信号" in app_source
    assert "gmgn_skill_score" in app_source
    assert "gmgn_skill_tags" in app_source
    assert "gmgn_skill_categories" in app_source
    assert "GMGN技能分" in app_source
    assert "GMGN技能" in app_source
    assert "技能类别" in app_source
    assert "risk_filter_score" in app_source
    assert "风险过滤" in app_source
    assert "absorption_score" in app_source
    assert "承接质量" in app_source
    assert "stage" in app_source
    assert "direction" in app_source
    assert "action" in app_source
    assert "关键位" in app_source
    assert "失效位" in app_source
    assert "持仓处理" in app_source
    assert "保护规则" in app_source
    assert "entry_stage" in app_source
    assert "entry_level" in app_source
    assert "gold_dog_rationale" in app_source
    assert "holder_quality_score" in app_source
    assert "security_filter_score" in app_source
    assert "liquidity_age_score" in app_source
    assert "dev_trust_score" in app_source
    assert "smart_kol_score" in app_source
    assert "bot_manipulation_score" in app_source
    assert "阶段" in app_source
    assert "池龄" in app_source
    assert "聪明钱" in app_source
    assert "上车等级" in app_source
    assert "推荐理由" in app_source
    assert '<span>??</span>' not in app_source
    assert "????" not in app_source
    assert "Binance Alpha 庄家雷达" in app_source
    assert "MEME 实时监控" in app_source
    assert "Meme 热度榜" in app_source
    assert "meme_potential_rows" in app_source
    assert "gold_watch_pick" in app_source
    assert "gold_watch" in app_source
    assert "watch_status" in app_source
    assert "watch_entry_reason" in app_source
    assert "watch_alert_gate_reason" in app_source
    assert "risk_deduction_summary" in app_source
    assert "风险扣分" in app_source
    assert "meme_seed_score" in app_source
    assert "meme_seed_terms" in app_source
    assert "链下热梗" in app_source
    assert "source_labels" in app_source
    assert "source_count" in app_source
    assert "smart_money" in app_source
    assert "potential_label" in app_source
    assert "gmgn_risk_flags" in app_source
    assert "meme_source_status" in app_source
    assert "command_count" in app_source
    assert "file_count" in app_source
    assert "数据源状态" in app_source
    assert "source-status-panel" in app_source
    assert "aria-expanded={expanded}" in app_source
    assert "多源入口" not in app_source
    assert "浏览器" in app_source
    assert "需配置" in app_source
    assert "mode?: \"default\" | \"meme\"" in app_source
    assert "5m" in app_source
    assert "1h" in app_source
    assert "只读模式" in app_source
    assert "recommendation_rows" in app_source
    assert "recommendation_action" in app_source
    assert "recommendation_reason" in app_source
    assert "recommendation_risk" in app_source
    assert "复盘" in app_source
    assert "复盘榜" in app_source
    assert "replay_boards" in app_source
    assert "by_action" in app_source
    assert "动作胜率" in app_source
    assert "avg_return_1h_pct" in app_source
    assert "best_hits" in app_source
    assert "worst_misses" in app_source
    assert "risk_avoided" in app_source
    assert "return_since_first_pct" in app_source
    assert "hit_status" in app_source
    assert "replay_score_adjustment" in app_source
    assert "回测校准" in app_source
    assert "replay_calibration_reason" in app_source
    assert "<table" not in app_source
    assert "table-shell" not in app_source
    assert "screener-tape" in app_source
    assert "screener-head" in app_source
    assert "screener-row" in app_source
    assert "signal-card-list" not in app_source
    assert "coin-signal-card" not in app_source
    assert "recommend-bucket" not in app_source
    assert "--ot-color-accent-primary" in style_source
    assert ".terminal-layout" in style_source
    assert ".source-status-panel" in style_source
    assert ".source-status-summary" in style_source
    assert ".source-status-strip span.pending" in style_source
    assert ".screener-tape" in style_source
    assert ".screener-head" in style_source
    assert ".screener-row" in style_source
    assert ".gold-tier-ops" in style_source
    assert ".signal-card-list" not in style_source
    assert ".coin-signal-card" not in style_source
    assert ".recommend-bucket" not in style_source


def test_react_terminal_frontend_supports_live_report_polling():
    web_dir = Path(__file__).parents[2] / "web"
    app_source = (web_dir / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "VITE_REPORT_URL" in app_source
    assert "reportUrl" in app_source
    assert "URLSearchParams" in app_source
    assert "http://127.0.0.1:8765/report.json" in app_source
    assert "reportUrlCandidates" in app_source
    assert "fetchJsonWithTimeout" in app_source
    assert "/api/report" in app_source
    assert "/report.json" in app_source
    assert "const REPORT_REFETCH_INTERVAL_MS = 10_000" in app_source
    assert 'refetchInterval: view === "memePotential" ? false : REPORT_REFETCH_INTERVAL_MS' in app_source
    assert "withCacheBuster" in app_source
    assert "refetchIntervalInBackground: true" in app_source


def test_react_terminal_frontend_does_not_render_missing_pool_age_as_one_minute():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert 'if (hours == null || hours === "") return "未记录";' in app_source


def test_react_terminal_frontend_has_polished_product_branding():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "Alpha 雷达" in app_source
    assert "Meme · Alpha · 庄家监控" in app_source
    assert "Radar" in app_source
    assert "brand-sigil" in app_source
    assert ">AR</span>" not in app_source
    assert ".brand-orbit" in style_source
    assert ".brand-sigil" in style_source


def test_react_terminal_frontend_has_token_price_panel():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "function TokenPricePanel" in app_source
    assert "tokenPriceData" in app_source
    assert "fmtPrice" in app_source
    assert "function liveQuoteTargetForRow" in app_source
    assert "function tokenQuoteTarget" in app_source
    assert 'kind: "pair" | "token"' in app_source
    assert "fetchLiveDexQuote" in app_source
    assert "fetchLiveDexQuotes" in app_source
    assert "/api/dex-price" in app_source
    assert "/api/dex-prices" not in app_source
    assert "return fetchDirectDexQuotes(targets)" in app_source
    assert "fetchDirectDexQuotes" in app_source
    assert "api.dexscreener.com/latest/dex/pairs" in app_source
    assert "live-dex-quote" in app_source
    assert "live-dex-quotes" in app_source
    assert "refetchInterval: 15_000" in app_source
    assert "refetchInterval: 12_000" in app_source
    assert "Dex 实时" in app_source
    assert "报告快照" in app_source
    assert "liveCurrentRows" in app_source
    assert "rowWithLiveQuoteMap" in app_source
    assert "function LivePriceCell" in app_source
    assert "fmtPrice(priceOf(row))" in app_source
    assert "fmtMoney(row.price_usd)" not in app_source
    assert "price_usd" in app_source
    assert "mcap:" in app_source
    assert "change_m5" in app_source
    assert "change_h1" in app_source
    assert "change_h24" in app_source
    assert "token-price-panel" in app_source
    assert "token-chart" in app_source
    assert ".token-price-panel" in style_source
    assert ".token-chart" in style_source
    assert ".live-price-source" in style_source


def test_react_terminal_frontend_does_not_duplicate_one_hour_change_column():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "<span>涨跌</span>" in app_source
    assert "<span>涨幅</span>" not in app_source
    assert "function changeMeta" in app_source
    assert "合约24h" in app_source
    assert "change-cell" in app_source
    assert "repeat(9, minmax(88px, 0.72fr))" in style_source
    assert "min-width: 1220px" in style_source
    assert ".change-cell" in style_source


def test_react_terminal_frontend_switches_screener_columns_by_board():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "variant: ViewKey" in app_source
    assert 'const memeColumns = variant === "meme"' in app_source
    assert '{memeColumns ? "聪明钱" : "数量OI / 1h"}' in app_source
    assert '{memeColumns ? "KOL" : "已结算费率"}' in app_source
    assert 'memeColumns ? <SmartMoneySummary evidence={row.smart_money_evidence} compact /> : fmtPct(row.oi_change_1h_pct)' in app_source
    assert 'memeColumns ? (row.kol ?? "--") : fmtPct(row.funding_rate_pct)' in app_source
    assert "variant={view}" in app_source


def test_react_terminal_frontend_uses_clear_chip_and_contrast_labels():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "<span>Top10筹码</span>" in app_source
    assert '["Top10筹码", plainPct(row.top10_holder_pct)]' in app_source
    assert "--ot-color-text-primary: #f5f5f5" in style_source
    assert "--ot-color-text-secondary: #cccccc" in style_source
    assert "--ot-color-text-muted: #808080" in style_source
    assert "color: #9fc2e8" in style_source
    assert "font-weight: 650" in style_source
    assert ".token-copy small" in style_source


def test_react_terminal_frontend_colors_action_badges_by_bucket():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "function actionClass" in app_source
    assert "action-badge" in app_source
    assert "signal hot" not in app_source
    assert ".action-badge.lead" in style_source
    assert ".action-badge.ambush" in style_source
    assert ".action-badge.pullback" in style_source
    assert ".action-badge.danger" in style_source
    assert ".action-badge.reject" in style_source


def test_react_terminal_frontend_combines_token_identity_with_avatar():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "function TokenIdentity" in app_source
    assert "tokenIconUrl" in app_source
    assert "tokenInitials" in app_source
    assert "token-avatar" in app_source
    assert "image_url" in app_source
    assert "<span>名称</span>" not in app_source
    assert "<span>代币</span>" in app_source
    assert ".token-identity" in style_source
    assert ".token-avatar" in style_source
    assert ".token-copy" in style_source


def test_react_terminal_frontend_shows_gold_watch_tiers():
    web_source = Path(__file__).parents[2] / "web" / "src"
    app_source = (web_source / "App.tsx").read_text(encoding="utf-8")
    monitor_source = (web_source / "monitor-v3" / "MemeMonitorView.tsx").read_text(encoding="utf-8")
    monitor_style = (web_source / "monitor-v3" / "monitor-v3.css").read_text(encoding="utf-8")

    assert "<MemeMonitorView" in app_source
    assert 'aria-label="MEME V3 实时监控"' in monitor_source
    assert "MonitorTabs" in monitor_source
    assert "CandidateTable" in monitor_source
    assert "TokenDetailDrawer" in monitor_source
    assert "RiskAuditView" in monitor_source
    assert "monitor-v3-alert-controls" in monitor_source
    assert ".meme-monitor-v3" in monitor_style
    assert ".monitor-v3-table" in monitor_style
    assert ".monitor-v3-token-avatar" in monitor_style
    assert ".monitor-v3-drawer" in monitor_style


def test_react_terminal_frontend_shows_gold_backtest_summary():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (Path(__file__).parents[2] / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "gold_backtest" in app_source
    assert "function GoldBacktestStrip" in app_source
    assert "金狗回测" in app_source
    assert ".gold-backtest-strip" in style_source


def test_react_terminal_frontend_exposes_okx_status_on_gold_cards():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "function okxSourceText" in app_source
    assert "<small>OKX</small>" in app_source
    assert "OKX已命中" in app_source
    assert "OKX未命中" in app_source


def test_react_terminal_frontend_exposes_priority_battlefield():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "priority_battlefield_label" in app_source
    assert "function battlefieldLabel" in app_source
    assert '"上所想象"' in app_source
    assert '"入场加分"' in app_source


def test_react_terminal_frontend_makes_sound_unlock_obvious():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "开启声音" in app_source
    assert "测试/解锁声音" in app_source
    assert "点击后开启" in app_source
    assert "setStoredBoolean(LIVE_ALERT_SOUND_STORAGE_KEY, true)" in app_source


def test_react_terminal_frontend_alerts_link_to_gmgn():
    root = Path(__file__).parents[2]
    app_source = (root / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "function gmgnTokenUrl" in app_source
    assert "function openGmgnTokenPage" in app_source
    assert "LIVE_ALERT_GMGN_OPEN_STORAGE_KEY" in app_source
    assert "自动开GMGN" in app_source
    assert "手动开GMGN" in app_source
    assert "打开GMGN盘口" in app_source
    assert ".live-alert-actions" in style_source


def test_react_terminal_frontend_alerts_review_only_runners():
    app_source = (Path(__file__).parents[2] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert 'type LiveAlertTier = "confirmed" | "early" | "discovered" | "runner"' in app_source
    assert 'row.watch_status === "achieved_gold" && row.watch_gold_capture_type === "review_only"' in app_source
    assert 'return "runner"' in app_source
    assert 'if (tier === "runner") return "飞行中"' in app_source
    assert "已快速拉成金狗，打开GMGN复盘走势和回踩" in app_source


def test_react_terminal_frontend_exposes_old_meme_revival_lane():
    root = Path(__file__).parents[2]
    app_source = (root / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    style_source = (root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert 'type GoldTierKey = "discovered" | "early" | "confirmed" | "revival" | "review"' in app_source
    assert "老币复活" in app_source
    assert "老币复活分" in app_source
    assert "复活依据" in app_source
    assert ".gold-tier-card.revival" in style_source
    assert "function liveAlertAudioUrl" in app_source
    assert "function playEmbeddedLiveAlertAudio" in app_source
    assert "new Blob([buffer], { type: \"audio/wav\" })" in app_source
    assert "function armLiveAlertSilentAudio" in app_source
    assert "function speakLiveAlert" in app_source
    assert "speechSynthesis" in app_source
    assert "金狗来了，注意观察" in app_source


def test_vercel_site_includes_cloud_report_api_contract():
    api_source = (Path(__file__).with_name("vercel_api") / "report.js").read_text(encoding="utf-8")

    assert "ALPHA_REPORT_WRITE_TOKEN" in api_source
    assert "ALPHA_REPORT_BLOB_PATH" in api_source
    assert "put(" in api_source
    assert "list(" in api_source
    assert "allowOverwrite: true" in api_source


def test_vercel_site_includes_live_dex_price_api_contract():
    api_source = (Path(__file__).with_name("vercel_api") / "dex-price.js").read_text(encoding="utf-8")
    batch_api_source = (Path(__file__).with_name("vercel_api") / "dex-prices.js").read_text(encoding="utf-8")
    builder_source = (Path(__file__).with_name("site_builder.py")).read_text(encoding="utf-8")

    assert "api.dexscreener.com/latest/dex/pairs" in api_source
    assert "api.dexscreener.com/latest/dex/tokens" in api_source
    assert "priceUsd" in api_source
    assert "liquidityUsd" in api_source
    assert "changeH1" in api_source
    assert "cleanPathPart" in api_source
    assert "bestPairForToken" in api_source
    assert '"Cache-Control", "no-store"' in api_source
    assert "batch_quote_endpoint_retired" in batch_api_source
    assert "sendJson(res, 410" in batch_api_source
    assert "api.dexscreener.com" not in batch_api_source
    assert "Promise.allSettled" not in batch_api_source
    assert '"Cache-Control", "public, max-age=3600"' in batch_api_source
    assert 'api_source.glob("*.js")' in builder_source
