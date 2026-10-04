import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// Compile the actual UI helpers in memory without a browser or generated files.
const source = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const { outputFiles } = buildSync({
  stdin: { contents: `${source}\nexport { buildLiveAlerts, startupLiveAlerts, alertIdentity, SmartMoneySummary, RadarMonitoringView, ScreenerTape, rowWithLiveQuote, liveQuoteTargetsFromRows, quoteIsFresh, displayQuoteIsFresh, quoteStatusText, LivePriceCell, validatedReportRows, GoldBacktestStrip, RatingV2Panel, RatingV2Comparison, GoldTierPanel, TokenNarrativeDetails, TokenIntelligencePanel, isNewerAlert, ConfirmationEvidence, heatPriorityOf, earlyConvictionOf, sourceText, DetailPanel, goldTierOf, goldCandidateRowsFromReport, goldProgressText, mergeGoldCandidateRows, watchStageLabel, legacyLiveAlertsEnabled, monitorMarketUrl };`, loader: "tsx", resolveDir: fileURLToPath(new URL(".", import.meta.url)) },
  loader: { ".css": "empty" },
  bundle: true, write: false, platform: "node", mainFields: ["module", "main"], format: "cjs", jsx: "automatic", define: { "import.meta.env": "{}" },
  external: ["react", "react-dom", "@tanstack/react-query", "recharts"],
});
const compiled = { exports: {} };
new Function("require", "module", "exports", outputFiles[0].text)(createRequire(import.meta.url), compiled, compiled.exports);
const { buildLiveAlerts, startupLiveAlerts, alertIdentity, SmartMoneySummary, RadarMonitoringView, ScreenerTape, rowWithLiveQuote, liveQuoteTargetsFromRows, quoteIsFresh, displayQuoteIsFresh, quoteStatusText, LivePriceCell, validatedReportRows, GoldBacktestStrip, RatingV2Panel, RatingV2Comparison, GoldTierPanel, TokenNarrativeDetails, TokenIntelligencePanel, isNewerAlert, ConfirmationEvidence, heatPriorityOf, earlyConvictionOf, sourceText, DetailPanel, goldTierOf, goldCandidateRowsFromReport, goldProgressText, mergeGoldCandidateRows, watchStageLabel, legacyLiveAlertsEnabled, monitorMarketUrl } = compiled.exports;
const now = Date.parse("2026-09-07T10:00:00Z");
const event = (changes = {}) => ({ chain: "solana", contract_address: "TestAddressA", symbol: "TEST", event_type: "confirmed", created_at: new Date(now - 1000).toISOString(), ...changes });
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const renderWithQuery = (component, props) => {
  const { QueryClient, QueryClientProvider } = createRequire(import.meta.url)("@tanstack/react-query");
  const client = new QueryClient();
  try {
    return renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(component, props)));
  } finally {
    client.clear();
  }
};

test("challenger has its own paper summary without implying live execution", () => {
  const markup = render(RadarMonitoringView, { report: { meta: { execution_challenger: {
    ok: true, status: "预热中", cash_usd: 1000, realized_pnl_usd: 0, open_count: 0,
  } } } });
  assert.match(markup, /回踩 \/ 老币复活 · 独立试验/);
  assert.match(markup, /预热中/);
  assert.doesNotMatch(markup, /实盘运行中/);
});

test("all backend events remain eligible beyond the old top-12 limit", () => {
  const alerts = buildLiveAlerts({ gold_watch_alerts: Array.from({ length: 30 }, (_, i) => event({ contract_address: `Test${i}` })) }, now);
  assert.equal(alerts.length, 30);
  assert.equal(new Set(alerts.map((alert) => alert.id)).size, 30);
});

test("semantic duplicates collapse, and the latest invalidation replaces confirmation", () => {
  const confirmed = event();
  const invalidated = event({ event_type: "invalidated", created_at: now, reason: "liquidity removed" });
  const result = buildLiveAlerts({ gold_watch_alerts: [invalidated, confirmed, { ...confirmed, event_id: "duplicate" }] }, now);
  assert.equal(result.length, 1);
  assert.equal(result[0].tier, "invalidated");
  assert.equal(result[0].row.watch_status, "invalidated");
  assert.equal(result[0].reason, "liquidity removed");
});

test("an older confirmation cannot resurface when rolling alert history loses an invalidation", () => {
  const previous = { createdAt: now, tier: "invalidated" };
  assert.equal(isNewerAlert({ createdAt: now - 1000, tier: "confirmed" }, previous), false);
  assert.equal(isNewerAlert({ createdAt: now, tier: "confirmed" }, previous), false);
  assert.equal(isNewerAlert({ createdAt: now + 1000, tier: "confirmed" }, previous), true);
});

test("discovery, confirmation and invalidation have distinct notice identities", () => {
  const ids = ["discovered", "confirmed", "invalidated"].map((event_type) => buildLiveAlerts({ gold_watch_alerts: [event({ event_type })] }, now)[0].id);
  assert.equal(new Set(ids).size, 3);
});

test("expired, undated, future and stale-report events do not notify", () => {
  for (const changes of [{ created_at: now - 16 * 60_000 }, { created_at: undefined }, { created_at: "invalid" }, { created_at: now + 120_000 }, { expires_at: now - 1 }, { event_type: "unrecognized" }]) {
    assert.deepEqual(buildLiveAlerts({ gold_watch_alerts: [event(changes)] }, now), []);
  }
  assert.deepEqual(buildLiveAlerts({ meta: { generated_at: new Date(now - 16 * 60_000).toISOString() }, gold_watch_alerts: [event()] }, now), []);
});

test("only recent alerts are eligible for first-load popup handling", () => {
  const recent = buildLiveAlerts({ gold_watch_alerts: [event({ created_at: new Date(now - 30_000).toISOString() })] }, now)[0];
  const old = buildLiveAlerts({ gold_watch_alerts: [event({ contract_address: "OldAddress", created_at: new Date(now - 120_000).toISOString() })] }, now)[0];
  assert.deepEqual(startupLiveAlerts([recent, old], now), [recent]);
});

test("current invalidation and backend eligibility veto old positive events", () => {
  for (const state of [{ watch_status: "invalidated" }, { watch_alert_eligible: false }]) {
    assert.deepEqual(buildLiveAlerts({ gold_watch_alerts: [event()], gold_watch_pick: { ...event(), ...state }, meme_rows: [event()] }, now), []);
  }
});

test("an explicit empty event list does not fabricate candidate alerts", () => {
  const row = event({ watch_status: "strong_candidate", watch_alerted_at: new Date(now).toISOString() });
  assert.equal(buildLiveAlerts({ meme_potential_rows: [row] }, now).length, 1);
  assert.deepEqual(buildLiveAlerts({ meme_potential_rows: [row], gold_watch_alerts: [] }, now), []);
});

test("legacy flat confirmation alerts are supported", () => {
  const legacy = event({ event_type: undefined, first_confirmed_at: new Date(now).toISOString() });
  assert.equal(buildLiveAlerts({ gold_watch_alerts: [legacy] }, now)[0].tier, "confirmed");
});

test("EVM chain aliases deduplicate while Solana address case is preserved", () => {
  assert.equal(alertIdentity({ chain: "56", contract_address: "0xAbCd" }), alertIdentity({ chain: "bsc", contract_address: "0xabcd" }));
  assert.notEqual(alertIdentity(event()), alertIdentity(event({ contract_address: "testaddressa" })));
  assert.deepEqual(buildLiveAlerts({ gold_watch_alerts: [event({ chain: undefined })] }, now), []);
});

test("smart money distinguishes missing evidence from measured zero", () => {
  const unknown = render(SmartMoneySummary, {});
  assert.match(unknown, /尚未采集钱包证据/);
  assert.doesNotMatch(unknown, /\$0/);
  const measured = render(SmartMoneySummary, { evidence: { unique_wallets: 0, independent_wallets: "2", net_buy_usd: -50, buy_usd: "", sell_usd: null } });
  assert.match(measured, /观察到净流出/);
  assert.match(measured, /独立钱包 2/);
  assert.match(measured, /down/);
  assert.match(measured, /买入 未采集/);
});

test("monitor summaries retain unknowns and no prelaunch projects are fabricated", () => {
  const html = render(RadarMonitoringView, {});
  assert.match(html, /快速通道/);
  assert.match(html, /严格执行核验/);
  assert.match(html, /未知/);
  assert.doesNotMatch(html, /未发射项目/);
});

test("prelaunch names do not imply identity verification and source links are explicit", () => {
  const html = render(RadarMonitoringView, { report: { prelaunch_projects: [{ name: "Keyword Only", source_url: "https://example.com/project", identity_evidence: [{ label: "Announcement", url: "https://example.com/evidence" }] }, { name: "Unlinked", source_url: "javascript:alert(1)" }] } });
  assert.match(html, /身份核验：未知/);
  assert.match(html, /https:\/\/example.com\/evidence/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.doesNotMatch(html, /javascript:/);
});

test("the screener renders rows beyond the old 80-row cutoff", () => {
  const rows = Array.from({ length: 81 }, (_, i) => ({ chain: "solana", contract_address: `Token${i}`, symbol: `TOKEN${i}` }));
  assert.match(render(ScreenerTape, { rows, onSelect() {}, variant: "meme" }), /TOKEN80/);
});

test("multi-source momentum gets an explicit heat-priority label", () => {
  const row = {
    chain: "bsc",
    contract_address: "0xBNC4",
    symbol: "BNC4",
    mcap: 5057980,
    volume24h: 29567000,
    change_h1: 147.99,
    source_count: 5,
    heat_score: 170.38,
    heat_priority: "high",
    heat_priority_label: "重点热度",
    heat_priority_reason: "5源共振 / 热度 170 / 1h +148%",
  };
  assert.equal(heatPriorityOf(row).label, "重点热度");
  assert.match(render(ScreenerTape, { rows: [row], onSelect() {}, variant: "meme" }), /重点热度/);
});

test("discovery provenance excludes market and analysis enrichment labels", () => {
  const row = {
    chain: "bsc",
    contract_address: "0xSTEP",
    symbol: "STEP",
    sources: ["proficy_trending"],
    source_groups: ["proficy"],
    source_count: 1,
    source_labels: ["GMGN", "Proficy", "DS", "Alpha_AI"],
    source_hit_counts: { proficy_trending: 1 },
    change_h1: 148,
    market_cap: 72_000,
    dex_volume24h: 300_000,
  };

  assert.equal(sourceText(row), "Proficy");
  assert.equal(heatPriorityOf(row).label, "");

  const html = renderWithQuery(DetailPanel, { row: { ...row, quote_status: "unavailable", quote_observed_at: null, price_usd: 0 } });
  assert.match(html, /发现来源<\/strong><span>1 个独立发现来源<\/span>/);
  assert.match(html, /Proficy ×1/);
  assert.match(html, /处理补充：DexScreener 行情 \/ Alpha AI 分析。不计入独立发现来源。/);
  assert.match(html, /市值<\/span><b>不可用<\/b>/);
  assert.doesNotMatch(html, /GMGN \/ Proficy \/ DS \/ Alpha_AI|\$72K/);
});

test("stale token detail values are explicitly marked as stale", () => {
  const html = renderWithQuery(DetailPanel, { row: {
    chain: "bsc",
    contract_address: "0xOZZY",
    symbol: "OZZY",
    sources: ["proficy_trending"],
    source_groups: ["proficy"],
    source_count: 1,
    source_labels: ["Proficy", "DS", "Alpha_AI"],
    quote_status: "stale",
    quote_observed_at: "2026-09-10T01:35:02Z",
    price_usd: 0.00042,
    market_cap: 146_000,
  } });

  assert.match(html, /市值<\/span><b>已过期 · \$146K<\/b>/);
  assert.match(html, /流动性<\/span><b>不可用<\/b>/);
  assert.doesNotMatch(html, /流动性<\/span><b>已过期<\/b>/);
  assert.match(html, /最近记录价格/);
  assert.match(html, /报价已过期/);
});

test("early narrative and structure get a separate conviction label", () => {
  const row = {
    chain: "bsc",
    contract_address: "0x4STOCK",
    symbol: "4Stock",
    name: "BSC Stock Meme",
    mcap: 133051,
    liquidity: 818468,
    pair_age_hours: 1.7,
    top10_holder_pct: 13.6,
    holders: 3893,
    source_count: 5,
    sources: ["gmgn_trending", "okx_signal", "985_monitor", "985_fomo_wallets", "wind_monitor"],
    early_conviction_level: "high",
    early_conviction_label: "早期重点 · 大叙事",
    early_conviction_reason: "大叙事：币股大叙事 / 5源独立命中 / Top10 13.6%",
  };
  assert.equal(earlyConvictionOf(row).label, "早期重点 · 大叙事");
  assert.match(render(ScreenerTape, { rows: [row], onSelect() {}, variant: "meme" }), /早期重点 · 大叙事/);
});

test("gold tier board keeps decision content visible and technical evidence collapsed", () => {
  const freshAt = new Date().toISOString();
  const discovered = {
    chain: "bsc",
    contract_address: "0xdiscover",
    symbol: "DISCOVER",
    watch_v2_tier: "discovered",
    pair_age_hours: 1,
    watch_last_seen_at: freshAt,
    narrative_tags: ["AI", "社区/CTO"],
    narrative_reasons: ["AI代理叙事", "社区接管后成交加速"],
    source_labels: ["GMGN", "DS"],
    gold_dog_rationale: ["池龄在早期窗口"],
    risk_deduction_reasons: ["叙事仍待外部确认"],
  };
  const early = {
    ...discovered,
    contract_address: "0xearly",
    symbol: "EARLY",
    watch_v2_tier: "early",
    early_conviction_next_step: "观察连续买盘和二次放量",
  };
  const items = [discovered, early].map((row) => ({ row, rec: { bucket: "ambush", action: "继续观察", reason: "多源开始共振", risk: "流动性仍需确认" } }));
  const html = render(GoldTierPanel, { items, onSelect() {} });
  assert.match(html, /聚合确认/);
  assert.match(html, /聚合早鸟/);
  assert.match(html, /聚合发现/);
  assert.match(html, /完整叙事/);
  assert.match(html, /AI代理叙事/);
  assert.match(html, /社区接管后成交加速/);
  assert.match(html, /风险与下一步/);
  assert.match(html, /<details class="gold-tier-evidence-details">/);
  assert.match(html, /核验与模型明细/);
  assert.doesNotMatch(html, /<details class="gold-tier-evidence-details" open/);
  assert.match(html, /role="listbox"/);
  assert.match(html, /打开行情与全部数据/);
  assert.match(html, /EARLY/);
  assert.doesNotMatch(html, /DISCOVER/);
});

test("frontend fallback requires a recent first observation before calling a row discovered", () => {
  const recentFirstSeen = new Date(now - 30 * 60_000).toISOString();
  const base = {
    chain: "bsc",
    quote_status: "fresh",
    market_cap: 120_000,
    score: 95,
  };

  assert.equal(goldTierOf({ ...base, contract_address: "0xyesterday", pair_age_hours: 22, replay: { first_seen_at: recentFirstSeen } }, now), "review");
  assert.equal(goldTierOf({ ...base, contract_address: "0xundated", pair_age_hours: 1 }, now), "review");
  assert.equal(goldTierOf({ ...base, contract_address: "0xrecent", pair_age_hours: 1, replay: { first_seen_at: recentFirstSeen } }, now), "discovered");
  assert.equal(goldTierOf({ ...base, contract_address: "0xunknownage", replay: { first_seen_at: recentFirstSeen } }, now), "review");
});

test("monitor board only promotes backend state-machine candidates", () => {
  const monitorNow = Date.now();
  const recent = new Date(monitorNow - 10 * 60_000).toISOString();
  const recentQuote = new Date(monitorNow - 5_000).toISOString();
  const stale = new Date(monitorNow - 7 * 60 * 60_000).toISOString();
  const base = { chain: "bsc", quote_status: "unavailable", watch_v2_tier: "review" };
  const report = {
    meme_rows: [
      { ...base, contract_address: "0xsingle", symbol: "SINGLE", pair_age_hours: 1, watch_status: "watch", watch_ticket_stage: "seed" },
      { ...base, contract_address: "0xdiscovered", symbol: "DISCOVERED", pair_age_hours: 1, watch_status: "seed_pool", watch_v2_tier: "discovered", quote_status: "fresh", quote_observed_at: recentQuote, watch_last_seen_at: recent },
      { ...base, contract_address: "0xdead", symbol: "DEAD", pair_age_hours: 100, watch_status: "watch" },
    ],
    meme_potential_rows: [
      { ...base, contract_address: "0xearly", symbol: "EARLY", pair_age_hours: 2, watch_status: "early_candidate", watch_v2_tier: "early", quote_status: "fresh", quote_observed_at: recentQuote, watch_last_seen_at: recent },
      { ...base, contract_address: "0xstale", symbol: "STALE", pair_age_hours: 2, watch_status: "early_candidate", watch_v2_tier: "early", quote_status: "fresh", quote_observed_at: stale, watch_last_seen_at: stale },
    ],
    meme_watch_universe: [
      { ...base, contract_address: "0xrevive", symbol: "REVIVE", pair_age_hours: 100, watch_status: "pullback", watch_last_seen_at: recent, old_meme_revival_active: true },
      { ...base, contract_address: "0xinvalid", symbol: "INVALID", pair_age_hours: 1, watch_status: "invalidated" },
    ],
  };

  const rows = goldCandidateRowsFromReport(report);
  const tiers = Object.fromEntries(rows.map((row) => [row.symbol, goldTierOf(row, monitorNow)]));

  assert.equal(tiers.SINGLE, "review");
  assert.equal(tiers.DISCOVERED, "discovered");
  assert.equal(tiers.EARLY, "early");
  assert.equal(tiers.STALE, "review");
  assert.equal(tiers.REVIVE, "revival");
  assert.equal(tiers.DEAD, "review");
  assert.equal(tiers.INVALID, "review");
  assert.equal(rows.find((row) => row.symbol === "SINGLE").quote_status, "unavailable");
});

test("active MEME monitor route consumes only the versioned V3 snapshot", () => {
  assert.match(source, /label: "MEME 监控"/);
  assert.match(source, /parseMonitorV3Snapshot\(liveMonitorData\?\.monitor_v3 \?\? data\?\.monitor_v3\)/);
  assert.match(source, /MONITOR_REFETCH_INTERVAL_MS = 2_000/);
  assert.match(source, /<MemeMonitorView[\s\S]*?snapshot=\{monitorV3\}/);
  assert.doesNotMatch(
    source,
    /view === "memePotential"[\s\S]{0,500}<GoldTierPanel/,
  );
});

test("V3 monitor disables legacy alert side effects and preserves the real GMGN chain", () => {
  assert.equal(legacyLiveAlertsEnabled("memePotential"), false);
  assert.equal(legacyLiveAlertsEnabled("meme"), true);
  assert.equal(
    monitorMarketUrl({ identity: { chain: "base", contract_address: "0xAbC" } }),
    "https://gmgn.ai/base/token/0xAbC",
  );
  assert.equal(
    monitorMarketUrl({ identity: { chain: "robinhood", contract_address: "0xToken" } }),
    "https://gmgn.ai/robinhood/token/0xToken",
  );
  assert.equal(monitorMarketUrl({ identity: { chain: "", contract_address: "0xToken" } }), null);
});

test("gold candidate progress exposes source resonance and confirmation streak", () => {
  const row = { chain: "bsc", contract_address: "0xprogress", symbol: "PROGRESS", watch_v2_tier: "early", watch_source_confirmation_sources: ["GMGN", "DS"], watch_strong_scan_count: 1 };
  assert.equal(goldProgressText(row, 3), "来源 2/2 · 连确 1/3");
  assert.equal(goldProgressText({ watch_source_confirmation_ok: true, watch_strong_scan_count: 3, watch_first_confirmation_active: true }, 3), "来源 2/2 · 已确认 3/3");
  const html = render(GoldTierPanel, { items: [{ row, rec: { bucket: "ambush", action: "观察", reason: "多源共振", risk: "待确认" } }], summary: { min_confirmations: 3 }, onSelect() {} });
  assert.match(html, /来源 2\/2 · 连确 1\/3/);
});

test("gold discovery shows immutable first-observation market cap and pool age", () => {
  const recentFirstSeen = new Date(now - 30 * 60_000).toISOString();
  const row = {
    chain: "bsc", contract_address: "0xfirst", symbol: "FIRST", watch_v2_tier: "discovered",
    watch_last_seen_at: recentFirstSeen, pair_age_hours: 0.75,
    replay: { first_seen_at: recentFirstSeen, first_mcap_usd: 12_000 },
  };
  const html = render(GoldTierPanel, { items: [{ row, rec: { bucket: "ambush", action: "观察", reason: "新池", risk: "待核验" } }], onSelect() {} });
  assert.match(html, /首次发现 \$12K/);
  assert.match(html, /池龄 45分钟/);
  assert.match(html, /首次发现市值<\/small><b>\$12K/);
});

test("backend watch tier expires instead of keeping yesterday's token active", () => {
  const recent = new Date(now - 30 * 60_000).toISOString();
  const recentQuote = new Date(now - 30_000).toISOString();
  const stale = new Date(now - 7 * 60 * 60_000).toISOString();
  assert.equal(goldTierOf({ watch_v2_tier: "early", pair_age_hours: 22, quote_status: "unavailable", watch_last_seen_at: recent }, now), "review");
  assert.equal(goldTierOf({ watch_v2_tier: "early", pair_age_hours: 1, quote_status: "fresh", watch_last_seen_at: stale }, now), "review");
  assert.equal(goldTierOf({ watch_v2_tier: "early", pair_age_hours: 1, quote_status: "fresh", quote_observed_at: recentQuote, watch_last_seen_at: recent }, now), "early");
  assert.equal(goldTierOf({ watch_v2_tier: "review", pair_age_hours: 1, quote_status: "fresh" }, now), "review");
  assert.equal(watchStageLabel({ watch_v2_tier: "early", pair_age_hours: 22, watch_last_seen_at: stale, confirmation_status: "waiting_fresh_market" }), "复盘");
});

test("duplicate candidate rows retain backend watch state found in later sections", () => {
  const identity = { chain: "bsc", contract_address: "0xduplicate", symbol: "DUP" };
  const [merged] = mergeGoldCandidateRows([
    { ...identity, quote_status: "fresh", pair_age_hours: 1, market_cap: 120_000 },
    { ...identity, watch_v2_tier: "review", watch_status: "pullback", watch_reason: "等待回踩" },
  ]);

  assert.equal(merged.watch_v2_tier, "review");
  assert.equal(merged.watch_status, "pullback");
  assert.equal(merged.watch_reason, "等待回踩");
  assert.equal(goldTierOf(merged, now), "review");
});

test("gold tier cards prefer ready token intelligence and render contract-scoped evidence", () => {
  const row = {
    chain: "bsc",
    contract_address: "0xprimary",
    symbol: "INTEL",
    watch_v2_tier: "confirmed",
    narrative_reasons: ["旧叙事不应覆盖新结论"],
    risk_deduction_reasons: ["旧风险"],
    token_intelligence: {
      status: "ready",
      one_line_judgement: "新情报判断：注意力与链上买盘同步",
      project_narrative: "围绕链上研究工具形成传播叙事",
      attention_evidence: ["三处独立社区开始讨论"],
      smart_wallets: [{ address: "0xwallet", reason: "历史盈利钱包本轮净买入", observed_at: "2026-09-10T08:00:00Z" }],
      identity: { official_status: "matched", official_contract: "0xprimary", alternate_contracts: ["0xalternate"] },
      risks: ["流动性仍偏薄"],
      ai_narrative: "AI整理后的完整叙事",
      official_ca_status: "matched",
      ca_conflict: false,
      social_source_count: 3,
      wash_score: 18,
      flow_acceleration: 1.75,
      ai_risk_flags: ["多CA风险待持续核验"],
      evidence_urls: ["https://example.com/token"],
      ai_analyzed_at: "2026-09-10T08:09:00Z",
      missing_evidence: ["官方团队背景"],
      sources: [{ title: "Official site", url: "https://example.com/token", observed_at: "2026-09-10T08:05:00Z" }],
      generated_at: "2026-09-10T08:10:00Z",
    },
  };
  const html = render(GoldTierPanel, { items: [{ row, rec: { bucket: "lead", action: "旧动作", reason: "旧判断", risk: "旧风险" } }], onSelect() {} });
  assert.match(html, /情报就绪/);
  assert.match(html, /新情报判断：注意力与链上买盘同步/);
  assert.match(html, /AI整理后的完整叙事/);
  assert.match(html, /社交来源 3/);
  assert.match(html, /刷量 18分/);
  assert.match(html, /交易加速 1.75x/);
  assert.match(html, /多CA风险待持续核验/);
  assert.match(html, /三处独立社区开始讨论/);
  assert.match(html, /0xwallet/);
  assert.match(html, /观察于/);
  assert.match(html, /官方合约匹配/);
  assert.match(html, /0xalternate/);
  assert.match(html, /流动性仍偏薄/);
  assert.match(html, /官方团队背景/);
  assert.match(html, /href="https:\/\/example.com\/token"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.ok(html.indexOf("新情报判断：注意力与链上买盘同步") < html.indexOf("旧叙事不应覆盖新结论"));
});

test("token intelligence exposes partial and unavailable states while retaining legacy fallbacks", () => {
  const partial = render(TokenIntelligencePanel, { row: {
    narrative_reasons: ["旧叙事回退"],
    risk_deduction_reasons: ["旧风险回退"],
    token_intelligence: { status: "partial", missing_evidence: ["官网合约声明"] },
  } });
  assert.match(partial, /待补关键证据/);
  assert.match(partial, /旧叙事回退/);
  assert.match(partial, /旧风险回退/);
  assert.match(partial, /官网合约声明/);

  const unavailable = render(TokenIntelligencePanel, { row: {
    narrative_reasons: ["本地叙事仍可用"],
    token_intelligence: { status: "unavailable" },
  }, compact: true });
  assert.match(unavailable, /缺少外部证据/);
  assert.match(unavailable, /本地叙事仍可用/);

  const pending = render(TokenIntelligencePanel, { row: { token_intelligence: { status: "pending" } }, compact: true });
  assert.match(pending, /情报生成中/);
});

test("token intelligence tolerates malformed evidence collections", () => {
  const html = render(TokenIntelligencePanel, { row: {
    token_intelligence: {
      status: "partial",
      attention_evidence: null,
      smart_wallets: [null, { wallet: "0xabc", observed_at: "2026-09-10T02:00:00Z" }],
      risks: { reason: "bad shape" },
      sources: [null, { title: "valid source", url: "https://example.com/source" }],
    },
  } });
  assert.match(html, /0xabc/);
  assert.match(html, /观察于/);
  assert.doesNotMatch(html, /bad shape/);
  assert.match(html, /valid source/);
});

test("token intelligence renders grounded statement text", () => {
  const html = render(TokenIntelligencePanel, { row: {
    token_intelligence: {
      status: "ready",
      one_line_judgement: "合约级判断",
      project_narrative: "合约级叙事",
      attention_evidence: [{ text: "多个独立来源提及", evidence_ids: ["source-1"] }],
      smart_wallets: [{ text: "三个已核验钱包买入", evidence_ids: ["wallet-1"] }],
      risks: [{ text: "存在同名多合约", evidence_ids: ["risk-1"] }],
    },
  }});
  assert.match(html, /多个独立来源提及/);
  assert.match(html, /三个已核验钱包买入/);
  assert.match(html, /存在同名多合约/);
  assert.match(html, /证据 source-1/);
});

test("token intelligence distinguishes unknown shadow fields and renders evidence url fallback", () => {
  const html = render(TokenIntelligencePanel, { row: {
    token_intelligence: {
      status: "partial",
      evidence_urls: ["https://example.com/ai-proof"],
    },
  }});
  assert.match(html, /CA 待核验/);
  assert.match(html, /社交来源 未采集/);
  assert.match(html, /交易加速 未计算/);
  assert.match(html, /href="https:\/\/example.com\/ai-proof"/);
});

test("operational clutter is grouped behind one system and model disclosure", () => {
  assert.match(source, /<details className="system-model-panel">/);
  assert.match(source, /<b>系统与模型<\/b>/);
  assert.match(source, /showRating=\{false\}/);
  assert.match(source, /!selected && view !== "memePotential" && \(/);
  assert.doesNotMatch(source, /\{\(view === "memePotential" \|\| view === "meme"\) && <RadarMonitoringView/);
});

test("fresh validated backend quotes are never overwritten or requested through raw API", () => {
  const row = { ...event(), quote_status: "fresh", quote_observed_at: now, price_usd: 1, market_cap: 100 };
  assert.equal(rowWithLiveQuote(row, { priceUsd: 999, marketCap: 999, updatedAt: now }, now), row);
  assert.deepEqual(liveQuoteTargetsFromRows([row], 80, now), []);
});

test("stale backend quotes get a display-only live quote without becoming validated", () => {
  const row = { ...event(), dex_url: "https://dexscreener.com/bsc/0xPair", quote_status: "stale", quote_observed_at: now - 60_000, price_usd: 1, market_cap: 100 };
  const targets = liveQuoteTargetsFromRows([row], 80, now);
  const live = rowWithLiveQuote(row, { priceUsd: 2, marketCap: 200, updatedAt: now }, now);
  assert.equal(targets.length, 1);
  assert.equal(targets[0].kind, "token");
  assert.equal(live.price_usd, 2);
  assert.equal(live.market_cap, 200);
  assert.equal(live.quote_status, "stale");
  assert.equal(live.quote_observed_at, now - 60_000);
  assert.equal(quoteIsFresh(live, now), false);
  assert.equal(displayQuoteIsFresh(live, now), true);
  assert.equal(quoteStatusText(live, now), "Dex 实时");
});

test("quarantined backend quotes stay isolated from display-only quotes", () => {
  const row = { ...event(), quote_status: "quarantined", quote_observed_at: now - 60_000, price_usd: 1, market_cap: 100 };
  assert.deepEqual(liveQuoteTargetsFromRows([row], 80, now), []);
  assert.equal(rowWithLiveQuote(row, { priceUsd: 999, marketCap: 999, updatedAt: now }, now), row);
});

test("stale and undated fast quotes are never labeled as current", () => {
  assert.equal(quoteIsFresh({ quote_status: "fresh", quote_observed_at: now }, now), true);
  assert.equal(quoteIsFresh({ quote_status: "fresh", quote_observed_at: now - 61_000 }, now), false);
  assert.equal(quoteIsFresh({ quote_status: "fresh" }, now), false);
  assert.equal(quoteStatusText({ quote_status: "stale", live_price_source: "Dex real-time" }, now), "报价已过期");
  const html = render(LivePriceCell, { row: { quote_status: "stale", price_usd: 1, live_price_source: "Dex real-time" } });
  assert.match(html, /报价已过期/);
  assert.doesNotMatch(html, /active|Dex real-time/);
});

test("fast-track and prelaunch summaries use the supplied backend contract", () => {
  const html = render(RadarMonitoringView, { report: {
    meta: { fast_track: { ok: true, stale: true, tracked_count: 40, fresh_count: 0, universe_count: 250, target_interval_seconds: 10, elapsed_seconds: 2.5, errors: ["provider timeout"] }, prelaunch_watch: { candidate_count: 1, source_count: 2 } },
    prelaunch_projects: [{ project: "Unverified Project", stage: "prelaunch", post_url: "https://example.com/post", matched_signals: ["launch"], text: "Source text", has_contract: true, official_verified: false }],
  } });
  assert.match(html, /已过期/);
  assert.match(html, /provider timeout/);
  assert.match(html, /<dd>0<\/dd>/);
  assert.match(html, /Unverified Project/);
  assert.match(html, /官方未核验/);
  assert.match(html, /归属未核验/);
  assert.match(html, /线索匹配/);
});

test("source-attributed wallet arrays and net-flow fields are rendered without inferring independence", () => {
  const html = render(SmartMoneySummary, { evidence: { unique_wallets: ["WalletA", "WalletB"], unique_wallet_count: 2, fresh_unique_wallet_count: 1, linked_clusters: ["ClusterA"], net_flow_usd: 100, buy_usd: 150, sell_usd: 50, flow_event_count: 2, freshness: "fresh", wallet_verification: "source_attributed_address_only", watchlist: { matched_wallets: ["WalletA"] } } });
  assert.match(html, /去重钱包 2/);
  assert.match(html, /独立钱包 未采集/);
  assert.match(html, /关注钱包 1/);
  assert.match(html, /独立性未核验/);
  assert.match(html, /普通钱包净流入 \$100/);
  assert.match(html, /平台钱包 · 钱包归因 2 · 暂无新买入/);
  const empty = render(SmartMoneySummary, { evidence: { unique_wallets: [], unique_wallet_count: 0, fresh_unique_wallet_count: 0, net_flow_usd: 0, buy_usd: 0, sell_usd: 0, flow_event_count: 0 } });
  assert.match(empty, /未发现钱包交易/);
  assert.match(empty, /普通钱包净流入 \$0/);
});

test("smart money counts and flows only use profitability gated evidence", () => {
  const html = render(SmartMoneySummary, { evidence: {
    wallet_verification: "profit_history_gated", qualified_wallet_count: 2,
    unique_wallet_count: 50, unique_wallets: ["A", "B"], net_flow_usd: 999999,
    confirmation: { buy_wallet_count: 1, buy_usd: 30, sell_usd: 10, net_flow_usd: 20 },
    freshness: "fresh", watchlist: { matched_wallets: ["A", "B"] }
  } });
  assert.match(html, /已核验盈利钱包 2/);
  assert.match(html, /盈利钱包净流入 \$20/);
  assert.doesNotMatch(html, /999|去重钱包 50/);
});

test("backend early events do not require confirmed eligibility and alert_type is supported", () => {
  for (const field of ["type", "alert_type"]) {
    for (const tier of ["early", "confirmed", "invalidated"]) {
      const notice = event({ event_type: undefined, [field]: tier });
      const row = { ...notice, watch_alert_eligible: tier === "confirmed" };
      assert.equal(buildLiveAlerts({ gold_watch_alerts: [notice], meme_rows: [row] }, now)[0].tier, tier);
    }
  }
});

test("fresh fast-track events survive an older full-report timestamp", () => {
  assert.equal(buildLiveAlerts({ meta: { generated_at: new Date(now - 3_600_000).toISOString(), fast_track: { updated_at: now } }, gold_watch_alerts: [event()] }, now).length, 1);
});

test("selected picks preserve validated metadata while stale prices can refresh for display", () => {
  const pick = event({ price_usd: 1 });
  const overlay = { ...pick, quote_status: "stale", price_usd: 2, smart_money_evidence: { unique_wallet_count: 3 } };
  const tracked = validatedReportRows({ gold_watch_pick: pick, meme_watch_universe: [overlay] });
  const merged = { ...pick, ...tracked.get(alertIdentity(pick)) };
  const live = rowWithLiveQuote(merged, { priceUsd: 999, updatedAt: now }, now);
  assert.equal(live.price_usd, 999);
  assert.equal(live.quote_status, "stale");
  assert.equal(merged.smart_money_evidence.unique_wallet_count, 3);
});

test("strict ledger keeps pending valuations unknown and legacy returns unvalidated", () => {
  const audit = {
    status: "pending_quotes", cash_usd: 1000, equity_usd: null, pending_valuations: 1,
    positions: [{}], pending_orders: [], candidate_gates: [{}], quote_issues: { token: "missing_quote" },
    legacy_audit: {
      validated_pnl_usd: null, largest_pnl_claims: [{ realized_pnl_usd: 999999 }],
      findings: [{ flags: ["missing_quote_fallback_exit", "abnormal_execution_jump"] }],
    },
  };
  const html = render(RadarMonitoringView, { report: { meta: { execution_audit: audit } } });
  assert.match(html, /等待报价/);
  assert.match(html, /<dt>权益<\/dt><dd>未知<\/dd>/);
  assert.match(html, /历史收益：待核验/);
  assert.match(html, /缺报价退出 1/);
  assert.doesNotMatch(html, /999999/);
  const old = render(GoldBacktestStrip, { backtest: { best_strategy: { summary: { win_rate: 0.99, average_return_pct: 12 } } } });
  assert.match(old, /收益待核验/);
  assert.doesNotMatch(old, /99|1200|胜率/);
});

test("250-plus token issues stay in closed scrollable details with compact Chinese counts", () => {
  const quote_issues = Object.fromEntries(Array.from({ length: 260 }, (_, i) => [`bsc:0xToken${i}`, i < 200 ? "source_quote_unavailable" : "missing_quote_timestamp"]));
  const html = render(RadarMonitoringView, { report: { meta: { execution_audit: { quote_issues } } } });
  const outsideDetails = html.match(/<summary class="radar-overview-summary">([\s\S]*?)<\/summary>/)[1];
  assert.match(outsideDetails, /报价不可用 <b>200<\/b>/);
  assert.match(outsideDetails, /缺少报价时间 <b>60<\/b>/);
  assert.doesNotMatch(outsideDetails, /0xToken|source_quote_unavailable|missing_quote_timestamp/);
  assert.match(html, /<details class="radar-issue-details">/);
  assert.match(html, /问题明细 · 260 项/);
  assert.match(html, /bsc:0xToken259/);
  assert.equal((html.match(/<li>/g) || []).length, 260);
  assert.match(html, /tabindex="0" role="region" aria-label="监测问题明细"/);
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  assert.match(css, /\.alpha-terminal \.radar-issue-scroll\s*\{[^}]*max-height: 180px;[^}]*overflow-y: auto;/);
});

test("unknown machine reasons are grouped without expanding the overview", () => {
  const errors = Array.from({ length: 300 }, (_, i) => `unknown_provider_error_${i}`);
  const html = render(RadarMonitoringView, { report: { meta: { fast_track: { errors } } } });
  const overview = html.match(/<summary class="radar-overview-summary">([\s\S]*?)<\/summary>/)[1];
  assert.match(overview, /其他问题 <b>300<\/b>/);
  assert.doesNotMatch(overview, /unknown_provider_error/);
});

test("fast-track counts distinguish this round from all valid cached quotes", () => {
  const html = render(RadarMonitoringView, { report: { meta: { fast_track: { tracked_count: 80, fresh_count: 183 } } } });
  assert.match(html, /<dt>本轮抓取<\/dt><dd>80<\/dd>/);
  assert.match(html, /<dt>有效报价<\/dt><dd>183<\/dd>/);
});

test("fast-track cooldown takes label priority and preserves zero counts", () => {
  const fast_track = { rate_limited: true, retry_after: "2026-09-07T12:00:00Z", rate_limit_failures: 2, stale: true, ok: false, tracked_count: 0, fresh_count: 0 };
  const html = render(RadarMonitoringView, { report: { meta: { fast_track } } });
  assert.match(html, /快速通道 限流退避/);
  assert.match(html, /快速通道 <small>限流退避<\/small>/);
  assert.match(html, /<dt>本轮抓取<\/dt><dd>0<\/dd>/);
  assert.match(html, /<dt>有效报价<\/dt><dd>0<\/dd>/);
  const recovered = render(RadarMonitoringView, { report: { meta: { fast_track: { ...fast_track, rate_limited: false, stale: false, ok: true } } } });
  assert.doesNotMatch(recovered, /限流退避/);
  assert.match(recovered, /快速通道 正常/);
});

test("collapsed navigation buttons retain accessible names and hover titles", () => {
  const { QueryClient, QueryClientProvider } = createRequire(import.meta.url)("@tanstack/react-query");
  const client = new QueryClient();
  const previousWindow = globalThis.window;
  globalThis.window = { location: { search: "" } };
  try {
    const html = renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(compiled.exports.default)));
    const nav = html.match(/<nav>([\s\S]*?)<\/nav>/)[1];
    const buttons = [...nav.matchAll(/<button\b[^>]*aria-label="([^"]+)"[^>]*title="([^"]+)"[^>]*>/g)];
    assert.equal(buttons.length, 7);
    buttons.forEach(([, label, title]) => assert.equal(label, title));
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    client.clear();
  }
});

test("platform labels remain distinct from wallet buy confirmation in rows and alerts", () => {
  for (const [confirmation_status, label] of [
    ["confirmed_platform_fallback", "标签共振"],
    ["confirmed_wallet_evidence", "钱包买入确认"],
    ["pending_wallet_evidence", "钱包证据待补"],
    ["blocked_sell_dominance", "卖出占优 · 已阻断"],
    ["waiting_fresh_market", "等待新鲜行情"],
  ]) {
    const row = event({ confirmation_status, confirmation_reason: "后端中文确认依据" });
    const html = render(ConfirmationEvidence, { row });
    assert.ok(html.includes(label));
    assert.match(html, /后端中文确认依据/);
    if (confirmation_status === "confirmed_platform_fallback") assert.doesNotMatch(html, /钱包买入确认/);
    const [alert] = buildLiveAlerts({ gold_watch_alerts: [row] }, now);
    assert.ok(alert.title.includes(label));
    assert.equal(alert.reason, "后端中文确认依据");
  }
  assert.match(render(ConfirmationEvidence, { row: {} }), /确认依据未知/);
});

test("overview defaults closed and aggregate realized PnL preserves zero and losses", () => {
  for (const [realized_pnl_usd, expected] of [[0, "$0"], [-5, "-$5"]]) {
    const html = render(RadarMonitoringView, { report: { meta: { execution_audit: { realized_pnl_usd, equity_usd: 993.907, pending_valuations: 0 } } } });
    assert.match(html, /<details class="radar-overview-details">/);
    const summary = html.match(/<summary class="radar-overview-summary">([\s\S]*?)<\/summary>/)[1];
    assert.ok(summary.includes(`已实现盈亏 <b>${expected}</b>`));
    assert.match(summary, /待估值 <b>0<\/b>/);
    assert.ok(html.includes(`<dt>已实现盈亏</dt><dd>${expected}</dd>`));
    assert.match(html, /<dt>权益<\/dt><dd>\$993\.91<\/dd>/);
  }
});

test("operational header uses 24px titles and four inline counts at desktop widths", () => {
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  assert.match(css, /\.alpha-terminal\.radar-operational \.hero-console h1\s*\{[^}]*font-size: 24px;/);
  assert.match(css, /\.alpha-terminal\.radar-operational \.hero-stats\s*\{[^}]*grid-template-columns: repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(source, /<details className="radar-history-details">/);
});

test("V3 alert polling stays global and speaks stage-specific monitor text before tone fallback", () => {
  const queryBlock = source.match(/const \{[^}]*data: liveMonitorData[^}]*\} = useQuery\(\{([\s\S]*?)\n  \}\);/)?.[1] || "";
  assert.ok(queryBlock);
  assert.doesNotMatch(queryBlock, /enabled:\s*view\s*===\s*"memePotential"/);
  assert.doesNotMatch(source, /if \(view !== "memePotential" \|\| !monitorV3\) return;/);
  assert.match(source, /const speechText = monitorAlertSpeechText\(lead\);/);
  assert.match(source, /speakLiveAlertText\(speechText\)/);
});

test("V2 panel distinguishes collecting progress from model probabilities", () => {
  const html = render(RatingV2Panel, { rating: {
    readiness: {
      training_rows: 7,
      minimum_labeled: 500,
      targets: { "2x": { labeled: 3, positive: 1, negative: 2, ready: false } },
    },
    model: {
      minimum_labeled: 200,
      minimum_each_class: 20,
      ready_target_count: 0,
      targets: { "2x": { labeled: 3, positive: 1, negative: 2, status: "collecting" } },
    },
  } });
  assert.match(html, /V2 评级/);
  assert.match(html, /影子模式/);
  assert.match(html, /7 \/ 200/);
  assert.match(html, /采集中/);
  assert.doesNotMatch(html, />0%</);
});

test("V2 panel does not call a trained but weak model usable", () => {
  const html = render(RatingV2Panel, { rating: {
    readiness: {
      training_rows: 1440,
      minimum_labeled: 500,
      targets: { "2x": { labeled: 632, positive: 123, negative: 509, ready: true } },
    },
    model: {
      minimum_labeled: 500,
      minimum_each_class: 50,
      trained_target_count: 3,
      ready_target_count: 0,
      targets: {
        "2x": {
          labeled: 632,
          positive: 123,
          negative: 509,
          status: "shadow_model_weak",
          metrics: { roc_auc: 0.503, brier_skill: -0.05 },
        },
      },
    },
  } });
  assert.match(html, /模型已训练 · 待验证/);
  assert.match(html, /模型偏弱/);
  assert.match(html, /AUC 0\.503/);
  assert.doesNotMatch(html, /概率可用/);
  assert.doesNotMatch(html, /模型已就绪/);
});

test("candidate comparison shows real probabilities and score delta", () => {
  const html = render(RatingV2Comparison, { row: {
    score: 82,
    rating_v2: {
      status: "scored",
      legacy_score: 82,
      score: 64,
      delta: -18,
      probabilities: { "2x": 0.71, "3x": 0.43, "5x": 0.22, "10x": 0.08 },
    },
  } });
  assert.match(html, /旧分 82/);
  assert.match(html, /V2 64/);
  assert.match(html, /-18/);
  assert.match(html, /2x 71%/);
  assert.match(html, /10x 8%/);
});
