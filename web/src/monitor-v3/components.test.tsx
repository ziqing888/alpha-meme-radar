import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import reportFixture from "../../tests/fixtures/monitor-v3-report.json";
import { CandidateTable } from "./CandidateTable";
import { buildTokenIntelligenceIndex, tokenIntelligenceKey } from "./intelligence";
import { MemeMonitorView } from "./MemeMonitorView";
import {
  parseMonitorV3Snapshot,
  type MonitorEvent,
  type MonitorTokenView,
  type MonitorV3Snapshot,
} from "./types";

afterEach(cleanup);

function fixtureSnapshot(): MonitorV3Snapshot {
  const snapshot = parseMonitorV3Snapshot(structuredClone(reportFixture));
  if (snapshot === undefined) throw new Error("Invalid monitor V3 fixture");
  return snapshot;
}

function token(snapshot: MonitorV3Snapshot, symbol: string): MonitorTokenView {
  const row = snapshot.tokens.find((item) => item.identity.symbol === symbol);
  if (row === undefined) throw new Error(`Missing fixture token: ${symbol}`);
  return row;
}

function richSnapshot(): MonitorV3Snapshot {
  const snapshot = fixtureSnapshot();
  const stack = token(snapshot, "STACK");
  stack.market.market_cap_usd = 24_000;
  stack.market.liquidity_usd = 18_000;
  stack.market.buys_1m = 14;
  stack.market.sells_1m = 5;
  stack.market.buys_5m = 48;
  stack.market.sells_5m = 21;
  stack.market.unique_buyers_5m = 31;
  stack.market.net_buy_flow_5m_usd = 920;
  stack.market.holders = 420;
  stack.market.top10_holder_pct = 17.15;
  stack.ranking_axes.narrative = { status: "known", evidence_ids: ["narrative-1"] };
  stack.ai = {
    status: "available",
    analyzed_at: "2026-09-10T12:01:30+00:00",
    evidence_ids: ["narrative-1"],
  };
  stack.risk.soft_flags = ["unlocked_lp", "wash_pattern"];
  stack.audit_facts = [
    {
      audit_field: "honeypot",
      value: true,
      status: "known",
      provider_family: "gmgn",
      upstream_provider: "gmgn_token_security",
      observed_at: "2026-09-10T12:01:00+00:00",
      confidence: "cross_checked",
      evidence_id: "audit-1",
    },
    {
      audit_field: "official_ca_status",
      value: "conflict",
      status: "conflicting",
      provider_family: "okx",
      upstream_provider: "okx_scan",
      observed_at: "2026-09-10T12:01:10+00:00",
      confidence: "provider_reported",
      evidence_id: "audit-2",
    },
    {
      audit_field: "mint",
      value: true,
      status: "known",
      provider_family: "okx",
      upstream_provider: "okx_permissions",
      observed_at: "2026-09-10T12:01:20+00:00",
      confidence: "provider_reported",
      evidence_id: "audit-3",
    },
  ];
  snapshot.source_health = {
    gmgn: { status: "healthy", last_success_at: snapshot.observed_at },
    okx: { status: "error", last_success_at: "2026-09-10T11:58:00+00:00" },
  };
  snapshot.monitor_status = "degraded";
  return snapshot;
}

function arcToken(snapshot: MonitorV3Snapshot, symbol = "ARCTEST"): MonitorTokenView {
  const row = structuredClone(token(snapshot, "STACK"));
  row.id = "arc:0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  row.identity.chain = "arc";
  row.identity.contract_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  row.identity.symbol = symbol;
  row.identity.name = "ARC Evidence Token";
  row.events = row.events.map((event) => ({
    ...event,
    chain: "arc",
    source_url: null,
  }));
  row.market.holders = null;
  row.market.top10_holder_pct = null;
  return row;
}

function taskThreeSnapshot(): MonitorV3Snapshot {
  const snapshot = richSnapshot();
  const stack = token(snapshot, "STACK");
  const eventTemplate = stack.events[0];
  const laneEvents = [
    ["trending", "gmgn", "gmgn_trending"],
    ["hot_search", "okx", "okx_hot_search"],
    ["kol_social", "wind", "wind_social"],
  ] as const;

  stack.events.push(
    ...laneEvents.map(([signalLane, providerFamily, providerFeed], index) => ({
      ...eventTemplate,
      event_id: `task3-${signalLane}`,
      event_type: signalLane,
      event_at: `2026-09-10T12:00:${10 + index}+00:00`,
      observed_at: `2026-09-10T12:00:${15 + index}+00:00`,
      provider_family: providerFamily,
      provider_feed: providerFeed,
      provider_event_id: `task3-${signalLane}`,
      raw_fingerprint: `task3-${signalLane}-fingerprint`,
      upstream_provider: providerFamily,
      evidence_role: (signalLane === "kol_social" ? "enrichment" : "ranking") as MonitorEvent["evidence_role"],
      signal_lane: signalLane,
      source_url: `https://example.com/${signalLane}`,
    })),
  );
  stack.wallet_evidence.status = "conflicting";
  stack.wallet_evidence.conflicting_wallets = [
    "0xcccccccccccccccccccccccccccccccccccccccc",
  ];
  stack.wallet_evidence.conflicting_evidence_ids = ["wallet-conflict-1"];
  stack.ai = { status: "pending", analyzed_at: null, evidence_ids: [] };
  stack.missing_evidence = ["dev_holder_pct", "bundler_pct", "sniper_pct"];

  snapshot.rejections = [
    ...snapshot.rejections,
    ...[
      "wrong_chain",
      "no_usable_pool",
      "sell_simulation_failed",
      "liquidity_removed",
      "official_ca_conflict",
      "wash_pattern",
      "stale_evidence",
    ].map((reason, index) => ({
      schema_version: 1 as const,
      reason,
      source: index % 2 === 0 ? "gmgn" : "okx",
      chain: "bsc",
      contract_address: `rejected-ca-${index}`,
      observed_at: `2026-09-10T12:01:${10 + index}+00:00`,
      raw_fingerprint: `rejection-task3-${index}`,
    })),
  ];
  Object.assign(snapshot, {
    kill_counts: {
      total: 15,
      rejections: 8,
      hard_risks: 7,
      by_reason: {
        invalid_address: 7,
        wrong_chain: 1,
        no_usable_pool: 1,
        sell_simulation_failed: 1,
        liquidity_removed: 1,
        official_ca_conflict: 2,
        wash_pattern: 1,
        stale_evidence: 1,
      },
    },
  });
  return snapshot;
}

describe("MemeMonitorView", () => {
  it("filters the stage-selected universe across All, BSC, Robinhood, and ARC", () => {
    const snapshot = richSnapshot();
    const bsc = token(snapshot, "STACK");
    const robinhood = structuredClone(bsc);
    robinhood.id = "robinhood:0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    robinhood.identity.chain = "robinhood";
    robinhood.identity.contract_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    robinhood.identity.symbol = "ROBIN";
    robinhood.events.forEach((event) => { event.chain = "robinhood"; });
    const arc = arcToken(snapshot);
    snapshot.tokens = [bsc, robinhood, arc];

    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /聪明钱/ }));

    expect(screen.getByText("STACK")).toBeInTheDocument();
    expect(screen.getByText("ROBIN")).toBeInTheDocument();
    expect(screen.getByText("ARCTEST")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "BSC" }));
    expect(screen.getByText("STACK")).toBeInTheDocument();
    expect(screen.queryByText("ROBIN")).not.toBeInTheDocument();
    expect(screen.queryByText("ARCTEST")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Robinhood" }));
    expect(screen.getByText("ROBIN")).toBeInTheDocument();
    expect(screen.queryByText("STACK")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "ARC" }));
    expect(screen.getByText("ARCTEST")).toBeInTheDocument();
    expect(screen.queryByText("ROBIN")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(screen.getByText("STACK")).toBeInTheDocument();
    expect(screen.getByText("ROBIN")).toBeInTheDocument();
    expect(screen.getByText("ARCTEST")).toBeInTheDocument();
  });

  it("shows ARC degraded health and keeps discovery out of the selected overview", () => {
    const snapshot = richSnapshot();
    const discovery = arcToken(snapshot, "ARCDISC");
    discovery.primary_state = "new";
    discovery.active_states = ["new"];
    const early = structuredClone(discovery);
    early.id = "arc:0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    early.identity.contract_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    early.identity.symbol = "ARCEARLY";
    early.primary_state = "building";
    early.active_states = ["new", "building"];
    const confirmed = structuredClone(discovery);
    confirmed.id = "arc:0xcccccccccccccccccccccccccccccccccccccccc";
    confirmed.identity.contract_address = "0xcccccccccccccccccccccccccccccccccccccccc";
    confirmed.identity.symbol = "ARCCONF";
    confirmed.primary_state = "resonating";
    confirmed.active_states = ["new", "building", "resonating"];
    confirmed.resonance.confirmation_gate = {
      eligible: true,
      failures: [],
      minimum_market_cap_usd: 10_000,
      minimum_liquidity_usd: 8_000,
      minimum_holders: 0,
    };
    snapshot.tokens = [discovery, early, confirmed];
    snapshot.source_health = {
      arc_onchain: { status: "error", error: "rpc_unavailable" },
    };

    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);

    expect(screen.getByText("ARC 数据源降级")).toBeVisible();
    expect(screen.queryByText("ARCDISC")).not.toBeInTheDocument();
    expect(screen.getByText("ARCEARLY")).toBeInTheDocument();
    expect(screen.getByText("ARCCONF")).toBeInTheDocument();
  });

  it("opens with a stage-separated focus view and keeps selected evidence visible", () => {
    const snapshot = richSnapshot();
    const confirmed = token(snapshot, "STACK");
    confirmed.primary_state = "resonating";
    confirmed.active_states = ["new", "building", "resonating"];
    confirmed.identity.first_seen_at = "2026-09-10T12:00:00+00:00";
    confirmed.state_history = [
      { state: "new", state_version: 1, observed_at: "2026-09-10T12:00:00+00:00" },
      { state: "building", state_version: 2, observed_at: "2026-09-10T12:00:07+00:00" },
      { state: "resonating", state_version: 3, observed_at: "2026-09-10T12:00:12+00:00" },
    ];
    confirmed.resonance.confirmation_gate = {
      eligible: true,
      failures: [],
      minimum_market_cap_usd: 10_000,
      minimum_liquidity_usd: 8_000,
      minimum_holders: 20,
    };
    const early = structuredClone(confirmed);
    early.id = "bsc:0x4444444444444444444444444444444444444444";
    early.identity.symbol = "BIRD";
    early.identity.name = "Early Bird";
    early.identity.contract_address = "0x4444444444444444444444444444444444444444";
    early.primary_state = "building";
    early.active_states = ["new", "building"];
    early.state_history = early.state_history.slice(0, 2);
    snapshot.tokens.push(early);

    const confirmedKey = tokenIntelligenceKey(
      confirmed.identity.chain,
      confirmed.identity.contract_address,
    );
    const earlyKey = tokenIntelligenceKey(early.identity.chain, early.identity.contract_address);
    render(
      <MemeMonitorView
        snapshot={snapshot}
        onOpenMarket={vi.fn()}
        intelligenceByToken={{
          [confirmedKey]: {
            status: "ready",
            one_line_judgement: "确认资金持续增强，适合重点跟踪。",
            ai_risk_flags: ["Top10 集中度抬升"],
            ai_analyzed_at: "2026-09-10T12:01:30+00:00",
          },
          [earlyKey]: {
            status: "ready",
            one_line_judgement: "早期热榜与资金流首次同步。",
            ai_risk_flags: ["持有人仍少"],
            ai_analyzed_at: "2026-09-10T12:01:30+00:00",
          },
        }}
      />,
    );

    expect(screen.getByRole("tab", { name: /精选总览/ })).toHaveAttribute("aria-selected", "true");
    const sections = screen.getAllByRole("region", { name: /聚合确认|聚合早鸟/ });
    expect(sections).toHaveLength(2);
    expect(sections[0]).toHaveAccessibleName("聚合确认");
    expect(sections[1]).toHaveAccessibleName("聚合早鸟");
    expect(within(sections[0]).getByText("STACK")).toBeInTheDocument();
    expect(within(sections[0]).queryByText("BIRD")).not.toBeInTheDocument();
    expect(within(sections[1]).getByText("BIRD")).toBeInTheDocument();
    expect(screen.getByText("确认资金持续增强，适合重点跟踪。")).toBeVisible();
    expect(screen.getByText("早期热榜与资金流首次同步。")).toBeVisible();
    expect(screen.getByText("Top10 集中度抬升")).toBeVisible();
    expect(screen.getByText("持有人仍少")).toBeVisible();
    expect(screen.getAllByText("共振证据")).toHaveLength(2);
    expect(screen.getByText("早鸟 7秒 · 确认 12秒")).toBeVisible();
    expect(screen.getByText("早鸟 7秒 · 确认未记录")).toBeVisible();
  });

  it("keeps aggregate discovery out of the focus view", () => {
    const snapshot = richSnapshot();
    const discovery = token(snapshot, "STACK");
    discovery.primary_state = "new";
    discovery.active_states = ["new"];
    discovery.resonance.confirmation_gate = {
      eligible: true,
      failures: [],
      minimum_market_cap_usd: 10_000,
      minimum_liquidity_usd: 8_000,
      minimum_holders: 20,
    };
    snapshot.tokens = [discovery];

    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);

    expect(screen.getByRole("tab", { name: /精选总览 0/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("当前没有精选候选")).toBeVisible();
    expect(screen.queryByRole("region", { name: "聚合发现" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /聚合发现 1/ }));
    expect(screen.getByText("STACK")).toBeVisible();
  });

  it("renders mutually exclusive monitor stages", () => {
    render(<MemeMonitorView snapshot={richSnapshot()} onOpenMarket={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "MEME 金狗监控" })).toBeInTheDocument();
    expect(screen.getByText("信号延迟")).toBeInTheDocument();
    expect(screen.getByText("来源在线")).toBeInTheDocument();
    expect(screen.getByText("异常 / 淘汰")).toBeInTheDocument();
    expect(screen.getAllByText("聚合发现").length).toBeGreaterThan(0);
    expect(screen.getAllByText("聚合早鸟").length).toBeGreaterThan(0);
    expect(screen.getAllByText("聚合确认").length).toBeGreaterThan(0);
    expect(screen.getAllByText("聪明钱").length).toBeGreaterThan(0);
    expect(screen.getByRole("tab", { name: /淘汰复盘/ })).toBeInTheDocument();
    expect(screen.getByText("刷新降级")).toBeInTheDocument();
    expect(screen.queryByText("原始发现")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /全部记录/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^新发现/ })).not.toBeInTheDocument();

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "精选总览0",
      "聚合发现0",
      "聚合早鸟0",
      "聚合确认0",
      "聪明钱1",
      "趋势观察0",
      "老币复活1",
      "淘汰复盘1",
    ]);

    const pageText = document.body.textContent ?? "";
    expect(pageText).not.toMatch(/钱包余额|账户权益|实盘持仓|模型训练/);
  });

  it("filters the shared token universe when a tab or search query changes", () => {
    const onOpenMarket = vi.fn();
    render(<MemeMonitorView snapshot={richSnapshot()} onOpenMarket={onOpenMarket} />);

    fireEvent.click(screen.getByRole("tab", { name: /聪明钱/ }));
    expect(screen.getByText("STACK")).toBeInTheDocument();
    expect(screen.queryByText("SOLO")).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "MEME V3 实时候选" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "在 GMGN 打开 STACK" }));
    expect(onOpenMarket).toHaveBeenCalledWith(expect.objectContaining({
      id: "bsc:0x2222222222222222222222222222222222222222",
    }));

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索实时候选" }), {
      target: { value: "not-present" },
    });
    expect(screen.getByText("当前视图暂无候选")).toBeInTheDocument();
  });

  it("shows platform wallet candidates separately from verified smart money", () => {
    const snapshot = richSnapshot();
    const stack = token(snapshot, "STACK");
    stack.wallet_evidence.verified_buyers = 0;
    stack.wallet_evidence.wallet_addresses = [];
    stack.wallet_evidence.candidate_buyers = 6;
    stack.wallet_evidence.candidate_wallet_addresses = [
      "0x1111111111111111111111111111111111111111",
      "0x2222222222222222222222222222222222222222",
      "0x3333333333333333333333333333333333333333",
      "0x4444444444444444444444444444444444444444",
      "0x5555555555555555555555555555555555555555",
      "0x6666666666666666666666666666666666666666",
    ];

    render(<CandidateTable rows={[stack]} onSelect={vi.fn()} />);

    expect(screen.getByText("平台钱包 6 · 已核验 0")).toBeInTheDocument();
  });

  it("explains when no current unpromoted discovery is available", () => {
    const snapshot = richSnapshot();
    snapshot.tokens.forEach((row) => {
      row.primary_state = "new";
      row.active_states = ["new"];
      row.freshness.status = "fresh";
      row.events.forEach((event) => {
        event.event_at = "2026-09-09T00:00:00+00:00";
        event.observed_at = "2026-09-09T00:00:00+00:00";
      });
    });

    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: /聚合发现/ }));

    expect(screen.getByText(/尚未晋级的新发现/)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /全部记录|新发现/ })).not.toBeInTheDocument();
  });

  it("renders unavailable source health when no source status has been published", () => {
    const snapshot = fixtureSnapshot();
    snapshot.source_health = {};
    snapshot.monitor_status = "unknown";
    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);

    const activeSourceMetric = screen.getByText("来源在线").closest(".monitor-v3-metric");
    const degradedSourceMetric = screen.getByText("异常 / 淘汰").closest(".monitor-v3-metric");
    expect(activeSourceMetric).not.toBeNull();
    expect(degradedSourceMetric).not.toBeNull();
    expect(within(activeSourceMetric as HTMLElement).getByText("不可用")).toBeInTheDocument();
    expect(within(degradedSourceMetric as HTMLElement).getByText("— / 1")).toBeInTheDocument();
    expect(screen.getByText("刷新未知")).toBeInTheDocument();
  });

  it("opens an accessible evidence drawer and restores focus after Escape", () => {
    const onOpenMarket = vi.fn();
    render(<MemeMonitorView snapshot={taskThreeSnapshot()} onOpenMarket={onOpenMarket} />);

    fireEvent.click(screen.getByRole("tab", { name: /聪明钱/ }));
    const row = screen.getByRole("row", { name: /查看 STACK 详情/ });
    fireEvent.click(row);

    const drawer = screen.getByRole("dialog", { name: "STACK 详情" });
    expect(drawer).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭详情" })).toHaveFocus();
    expect(within(drawer).getByText("事件与状态时间线")).toBeInTheDocument();
    expect(within(drawer).getByText("新币首发")).toBeInTheDocument();
    expect(within(drawer).getByText("趋势榜")).toBeInTheDocument();
    expect(within(drawer).getByText("热搜")).toBeInTheDocument();
    expect(within(drawer).getAllByText("聪明钱").length).toBeGreaterThan(0);
    expect(within(drawer).getByText("KOL / 社交")).toBeInTheDocument();
    expect(within(drawer).getByText("资金流")).toBeInTheDocument();
    expect(within(drawer).getByText("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).toBeInTheDocument();
    expect(within(drawer).getByText("0xcccccccccccccccccccccccccccccccccccccccc")).toBeInTheDocument();
    expect(within(drawer).getByText("冲突钱包")).toBeInTheDocument();
    expect(within(drawer).getByText("official_ca_status: conflict")).toBeInTheDocument();
    expect(within(drawer).getByText("缺失证据")).toBeInTheDocument();
    expect(within(drawer).getByText("dev_holder_pct")).toBeInTheDocument();
    expect(within(drawer).getByText("AI 分析中")).toBeInTheDocument();
    expect(within(drawer).getByRole("img", { name: "资金流与流动性走势" })).toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("button", { name: "在 GMGN 打开" }));
    expect(onOpenMarket).toHaveBeenCalledWith(expect.objectContaining({ id: "bsc:0x2222222222222222222222222222222222222222" }));

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "STACK 详情" })).not.toBeInTheDocument();
    expect(row).toHaveFocus();
  });

  it("keeps ARC explorer and DexScreener evidence available in the detail drawer", () => {
    const snapshot = richSnapshot();
    const arc = arcToken(snapshot);
    snapshot.tokens = [arc];
    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: /聪明钱/ }));
    fireEvent.click(screen.getByRole("row", { name: /查看 ARCTEST 详情/ }));

    const drawer = screen.getByRole("dialog", { name: "ARCTEST 详情" });
    expect(within(drawer).getByRole("link", { name: "在 ARC 浏览器查看合约" })).toHaveAttribute(
      "href",
      "https://explorer.arc.io/address/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
    expect(within(drawer).getByRole("link", { name: "在 DexScreener 打开" })).toHaveAttribute(
      "href",
      "https://dexscreener.com/arc/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
  });

  it("shows AI unavailable explicitly and closes details with the icon button", () => {
    const snapshot = taskThreeSnapshot();
    token(snapshot, "STACK").ai = { status: "unavailable", analyzed_at: null, evidence_ids: [] };
    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: /聪明钱/ }));
    fireEvent.click(screen.getByRole("row", { name: /查看 STACK 详情/ }));
    expect(screen.getByText("AI 尚未启动")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("restores full legacy intelligence inside the V3 token drawer", () => {
    const snapshot = richSnapshot();
    const stack = token(snapshot, "STACK");
    const key = tokenIntelligenceKey(stack.identity.chain, stack.identity.contract_address);
    render(
      <MemeMonitorView
        snapshot={snapshot}
        onOpenMarket={vi.fn()}
        intelligenceByToken={{
          [key]: {
            status: "ready",
            one_line_judgement: "多源资金共振，但同名 CA 冲突仍需核验。",
            ai_narrative: "项目围绕链上积分与社区分发，热度来自三个独立来源。",
            attention_evidence: ["12 条社交讨论，独立作者 8 个"],
            smart_wallets: [{ wallet: "SW-e7f2", text: "首次买入 420 美元" }],
            official_ca_status: "match",
            ca_conflict: true,
            social_source_count: 12,
            wash_score: 23,
            flow_acceleration: 1.84,
            identity: {
              official_status: "match",
              official_contract: stack.identity.contract_address,
              alternate_contracts: ["0x3333333333333333333333333333333333333333"],
            },
            ai_risk_flags: ["同名合约仍在传播"],
            missing_evidence: ["开发者历史未完成"],
            sources: [{
              source_id: "gmgn-social",
              title: "GMGN 社交证据",
              url: "https://gmgn.ai/example",
              observed_at: "2026-09-10T12:01:00+00:00",
            }],
            ai_analyzed_at: "2026-09-10T12:02:00+00:00",
          },
        }}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /聪明钱/ }));
    fireEvent.click(screen.getByRole("row", { name: /查看 STACK 详情/ }));
    const drawer = screen.getByRole("dialog", { name: "STACK 详情" });
    expect(within(drawer).getByText("详情分析与 AI")).toBeInTheDocument();
    expect(within(drawer).getByText("多源资金共振，但同名 CA 冲突仍需核验。")).toBeInTheDocument();
    expect(within(drawer).getByText(/项目围绕链上积分与社区分发/)).toBeInTheDocument();
    expect(within(drawer).getByText(/SW-e7f2/)).toBeInTheDocument();
    expect(within(drawer).getByText("官方合约匹配")).toBeInTheDocument();
    expect(within(drawer).getByText("存在冲突")).toBeInTheDocument();
    expect(within(drawer).getByText("23 分")).toBeInTheDocument();
    expect(within(drawer).getByText("1.84x")).toBeInTheDocument();
    expect(within(drawer).getByText("同名合约仍在传播")).toBeInTheDocument();
    expect(within(drawer).getByText("开发者历史未完成")).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: /GMGN 社交证据/ })).toHaveAttribute("href", "https://gmgn.ai/example");
  });

  it("renders kill counts and inspectable rejection rows in the risk tab", () => {
    render(<MemeMonitorView snapshot={taskThreeSnapshot()} onOpenMarket={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: /淘汰复盘/ }));
    const audit = screen.getByRole("region", { name: "淘汰复盘审计" });
    expect(within(audit).getByText("淘汰统计")).toBeInTheDocument();
    const expectedKillCounts = new Map([
      ["地址无效", "7"],
      ["链不匹配", "1"],
      ["无可用池", "1"],
      ["卖出模拟失败", "1"],
      ["流动性移除", "1"],
      ["官方 CA 冲突", "2"],
      ["疑似刷量", "1"],
      ["证据陈旧", "1"],
    ]);
    expectedKillCounts.forEach((count, label) => {
      const labelNode = within(audit).getAllByText(label)[0];
      expect(within(labelNode.parentElement as HTMLElement).getByText(count)).toBeInTheDocument();
    });
    expect(within(audit).getByText("bad-ca")).toBeInTheDocument();
    expect(within(audit).getByText("gmgn_trenches")).toBeInTheDocument();
    expect(within(audit).getByText("rejected-ca-6")).toBeInTheDocument();
    expect(within(audit).getByRole("table", { name: "淘汰复盘明细" }).parentElement).toHaveAttribute(
      "data-overflow-axis",
      "horizontal",
    );

    fireEvent.click(within(audit).getByRole("row", { name: /查看 BLOCK 风险详情/ }));
    expect(screen.getByRole("dialog", { name: "BLOCK 详情" })).toBeInTheDocument();
  });

  it("scopes rejection rows and derived reason totals to the active chain filter", () => {
    const snapshot = fixtureSnapshot();
    snapshot.tokens = [];
    snapshot.rejections = [
      {
        schema_version: 1,
        reason: "invalid_address",
        source: "bsc-source",
        chain: "bsc",
        contract_address: "bsc-rejected-ca",
        observed_at: snapshot.observed_at,
        raw_fingerprint: "bsc-rejection",
      },
      {
        schema_version: 1,
        reason: "wrong_chain",
        source: "robinhood-source",
        chain: "4663",
        contract_address: "robinhood-rejected-ca",
        observed_at: snapshot.observed_at,
        raw_fingerprint: "robinhood-rejection",
      },
      ...["arc-rejected-ca-1", "arc-rejected-ca-2"].map((contractAddress, index) => ({
        schema_version: 1 as const,
        reason: "official_ca_conflict",
        source: "arc-source",
        chain: index === 0 ? "arc" : "5042",
        contract_address: contractAddress,
        observed_at: snapshot.observed_at,
        raw_fingerprint: `arc-rejection-${index}`,
      })),
    ];

    render(<MemeMonitorView snapshot={snapshot} onOpenMarket={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /淘汰复盘/ }));

    const audit = screen.getByRole("region", { name: "淘汰复盘审计" });
    const table = within(audit).getByRole("table", { name: "淘汰复盘明细" });
    const reasonCount = (label: string) => {
      const labelNode = within(audit).getAllByText(label)[0];
      return within(labelNode.parentElement as HTMLElement);
    };

    expect(within(audit).getByText("4 条")).toBeInTheDocument();
    expect(within(table).getByText("bsc-rejected-ca")).toBeInTheDocument();
    expect(within(table).getByText("robinhood-rejected-ca")).toBeInTheDocument();
    expect(within(table).getByText("arc-rejected-ca-1")).toBeInTheDocument();
    expect(reasonCount("地址无效").getByText("1")).toBeInTheDocument();
    expect(reasonCount("链不匹配").getByText("1")).toBeInTheDocument();
    expect(reasonCount("官方 CA 冲突").getByText("2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "ARC" }));
    expect(within(audit).getByText("2 条")).toBeInTheDocument();
    expect(within(table).getByText("arc-rejected-ca-1")).toBeInTheDocument();
    expect(within(table).getByText("arc-rejected-ca-2")).toBeInTheDocument();
    expect(within(table).queryByText("bsc-rejected-ca")).not.toBeInTheDocument();
    expect(within(table).queryByText("robinhood-rejected-ca")).not.toBeInTheDocument();
    expect(reasonCount("官方 CA 冲突").getByText("2")).toBeInTheDocument();
    expect(reasonCount("地址无效").getByText("0")).toBeInTheDocument();
    expect(reasonCount("链不匹配").getByText("0")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "BSC" }));
    expect(within(audit).getByText("1 条")).toBeInTheDocument();
    expect(within(table).getByText("bsc-rejected-ca")).toBeInTheDocument();
    expect(within(table).queryByText("arc-rejected-ca-1")).not.toBeInTheDocument();
    expect(reasonCount("地址无效").getByText("1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Robinhood" }));
    expect(within(audit).getByText("1 条")).toBeInTheDocument();
    expect(within(table).getByText("robinhood-rejected-ca")).toBeInTheDocument();
    expect(within(table).queryByText("bsc-rejected-ca")).not.toBeInTheDocument();
    expect(reasonCount("链不匹配").getByText("1")).toBeInTheDocument();
  });
});

describe("token intelligence index", () => {
  it("matches numeric and named chains and keeps the strongest newest record", () => {
    const address = "0xABCDEFabcdefABCDEFabcdefABCDEFabcdef1234";
    const index = buildTokenIntelligenceIndex([
      {
        chain_id: "56",
        contract_address: address,
        token_intelligence: { status: "partial", one_line_judgement: "较完整记录", generated_at: 20 },
      },
      {
        chain: "bsc",
        token_address: address.toLowerCase(),
        token_intelligence: { status: "unavailable", one_line_judgement: "更新但较弱", generated_at: 30 },
      },
    ]);

    expect(index[tokenIntelligenceKey("BNB", address)]?.one_line_judgement).toBe("较完整记录");
  });

  it("normalizes ARC chain id 5042 to the canonical intelligence key", () => {
    const address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    expect(tokenIntelligenceKey("5042", address)).toBe(`arc:${address}`);
  });
});

describe("CandidateTable", () => {
  it("renders ARC evidence links, unknown holders, and a readable long identity", () => {
    const snapshot = richSnapshot();
    const arc = arcToken(
      snapshot,
      "ARC-SYMBOL-WITH-A-VERY-LONG-IDENTIFIER-THAT-MUST-STAY-USABLE",
    );
    arc.identity.name = "ARC token with an exceptionally long descriptive name for narrow viewports";

    const { container } = render(
      <CandidateTable rows={[arc]} onSelect={vi.fn()} onOpenMarket={vi.fn()} />,
    );

    const table = screen.getByRole("table", { name: "MEME V3 实时候选" });
    expect(within(table).getByText("ARC")).toHaveClass("is-chain-arc");
    expect(within(table).getByRole("link", { name: /在 ARC 浏览器查看.*合约/ })).toHaveAttribute(
      "href",
      "https://explorer.arc.io/address/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
    expect(within(table).getByRole("link", { name: /在 DexScreener 打开/ })).toHaveAttribute(
      "href",
      "https://dexscreener.com/arc/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
    const holdersCell = within(table).getByText("未知").closest("td");
    expect(holdersCell).not.toBeNull();
    expect(holdersCell).not.toHaveTextContent(/^0$/);
    expect(within(table).getByTitle(arc.identity.symbol as string)).toBeInTheDocument();
    expect(within(table).getByTitle(arc.identity.name as string)).toBeInTheDocument();
    expect(table.parentElement).toHaveAttribute("data-overflow-axis", "responsive");
    expect(container.querySelector(".monitor-v3-token-copy")).toBeInTheDocument();
  });

  it("renders a readable heatboard with avatar, contract access, and market access", () => {
    const snapshot = richSnapshot();
    const stack = token(snapshot, "STACK");
    const onSelect = vi.fn();
    const onOpenMarket = vi.fn();
    const { container } = render(
      <CandidateTable
        rows={[stack]}
        onSelect={onSelect}
        onOpenMarket={onOpenMarket}
        imageByToken={{ [stack.id]: "https://assets.example/stack.png" }}
      />,
    );

    const table = screen.getByRole("table", { name: "MEME V3 实时候选" });
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual([
      "排名 / 币种",
      "当前市值 / 发现后",
      "首发 / 最高",
      "池子",
      "24h 成交",
      "持有人 / Top10",
      "判断 / 风险",
    ]);
    expect(table.parentElement).toHaveAttribute("data-overflow-axis", "responsive");
    expect(container.querySelector(".monitor-v3-avatar img")).toHaveAttribute("src", "https://assets.example/stack.png");
    expect(within(table).getByRole("button", { name: /复制完整合约地址/ })).toBeInTheDocument();
    expect(within(table).getByText("$24,000")).toBeInTheDocument();
    expect(within(table).getByText("$18,000")).toBeInTheDocument();
    expect(within(table).getByText("完整共振")).toBeInTheDocument();
    expect(within(table).getByText("420")).toBeInTheDocument();
    expect(within(table).getByText(/Top10 17.15%/)).toBeInTheDocument();
    expect(within(table).getByText("AI 已完成")).toBeInTheDocument();
    expect(within(table).getByText("LP 未锁定")).toBeInTheDocument();
    expect(within(table).queryByText("2 个核验买家")).not.toBeInTheDocument();
    expect(within(table).queryByText("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).not.toBeInTheDocument();
    expect(within(table).queryByText("honeypot: true")).not.toBeInTheDocument();

    fireEvent.click(within(table).getByRole("button", { name: "在 GMGN 打开 STACK" }));
    expect(onOpenMarket).toHaveBeenCalledWith(stack);

    const dataRow = within(table).getByRole("row", { name: /查看 STACK 详情/ });
    fireEvent.keyDown(dataRow, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(stack);
  });

  it("shows live holder freshness and the AI judgement without missing-evidence clutter", () => {
    const snapshot = richSnapshot();
    const stack = token(snapshot, "STACK");
    stack.market.field_sources = { holders: "gmgn", top10_holder_pct: "gmgn" };
    stack.market.field_observed_at = {
      holders: "2026-09-10T12:01:25+00:00",
      top10_holder_pct: "2026-09-10T12:01:25+00:00",
    };
    render(
      <CandidateTable
        rows={[stack]}
        onSelect={vi.fn()}
        nowMs={Date.parse("2026-09-10T12:01:30+00:00")}
        intelligenceByToken={{
          [stack.id]: {
            status: "ready",
            one_line_judgement: "多源资金同步增强，但筹码集中度仍需留意。",
            ai_risk_flags: ["Top10 集中度正在上升"],
            ai_analyzed_at: "2026-09-10T12:01:28+00:00",
          },
        }}
      />,
    );

    expect(screen.getByText("GMGN 5秒前")).toBeInTheDocument();
    expect(screen.getByText("多源资金同步增强，但筹码集中度仍需留意。")).toBeInTheDocument();
    expect(screen.getByText("Top10 集中度正在上升")).toBeInTheDocument();
    expect(screen.queryByText("风险数据未覆盖")).not.toBeInTheDocument();
  });

  it("keeps wallet-level evidence out of the list and shows the primary risk", () => {
    const snapshot = fixtureSnapshot();
    render(<CandidateTable rows={[token(snapshot, "BLOCK")]} onSelect={vi.fn()} />);

    expect(screen.getByText("确认蜜罐")).toBeInTheDocument();
    expect(screen.queryByText("冲突 1")).not.toBeInTheDocument();
    expect(screen.queryByText("官方 CA 冲突")).not.toBeInTheDocument();
  });

  it("renders null market and timing values as unavailable rather than zero", () => {
    const snapshot = fixtureSnapshot();
    render(<CandidateTable rows={[token(snapshot, "SOLO")]} onSelect={vi.fn()} />);

    expect(screen.getAllByText("未采集").length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByText("未知").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("待基准").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });

  it("uses the live display quote and calculates discovery return from the first market cap", () => {
    const snapshot = richSnapshot();
    const stack = token(snapshot, "STACK");
    render(
      <CandidateTable
        rows={[stack]}
        onSelect={vi.fn()}
        quoteByToken={{
          [stack.id]: {
            priceUsd: 0.0005,
            marketCap: 40_000,
            liquidityUsd: 30_000,
            volume24h: 90_000,
          },
        }}
      />,
    );

    expect(screen.getByText(/\$0\.0005/)).toBeInTheDocument();
    expect(screen.getByText("$40,000")).toBeInTheDocument();
    expect(screen.getByText("$30,000")).toBeInTheDocument();
    expect(screen.getByText("$90,000")).toBeInTheDocument();
    expect(screen.getByText("+233.33%")).toBeInTheDocument();
    expect(screen.getByText(/DS 实时/)).toBeInTheDocument();
  });

  it("uses the canonical discovery baseline instead of a late V3 market snapshot", () => {
    const snapshot = richSnapshot();
    const stack = token(snapshot, "STACK");
    stack.market.snapshots[0].market_cap_usd = 364_925;
    render(
      <CandidateTable
        rows={[stack]}
        onSelect={vi.fn()}
        baselineByToken={{
          [stack.id]: {
            first_seen_at: "2026-09-11T03:14:33+08:00",
            first_market_cap_usd: 84_390,
            first_price_usd: 0.00008439,
            peak_market_cap_usd: 980_468,
          },
        }}
        quoteByToken={{
          [stack.id]: {
            priceUsd: 0.000643736,
            marketCap: 643_736,
            liquidityUsd: 75_195,
            volume24h: 951_974,
          },
        }}
      />,
    );

    expect(screen.getByText("$84,390")).toBeInTheDocument();
    expect(screen.getByText(/最高 \$980,468/)).toBeInTheDocument();
    expect(screen.getByText("+662.81%")).toBeInTheDocument();
    expect(screen.queryByText("$364,925")).not.toBeInTheDocument();
  });
});
