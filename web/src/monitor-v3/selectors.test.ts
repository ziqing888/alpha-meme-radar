import { describe, expect, it, vi } from "vitest";

import reportFixture from "../../tests/fixtures/monitor-v3-report.json";
import { formatNullableNumber, formatUsd } from "./formatters";
import {
  isConfirmationEligible,
  monitorTabCounts,
  monitorV3Availability,
  selectMonitorRows,
} from "./selectors";
import {
  parseMonitorV3Snapshot,
  type MonitorState,
  type MonitorTokenView,
  type MonitorV3Snapshot,
} from "./types";

function requireMonitorV3Snapshot(value: unknown): MonitorV3Snapshot {
  const parsed = parseMonitorV3Snapshot(value);
  if (parsed === undefined) {
    throw new Error("monitor-v3-report.json does not match the backend V3 projection contract");
  }
  return parsed;
}

const snapshot = requireMonitorV3Snapshot(reportFixture);

function tokenNamed(symbol: string): MonitorTokenView {
  const token = snapshot.tokens.find((item) => item.identity.symbol === symbol);
  if (token === undefined) throw new Error(`Missing fixture token: ${symbol}`);
  return structuredClone(token);
}

function variant(
  symbol: string,
  addressDigit: string,
  primaryState: MonitorState,
  activeStates: MonitorState[],
): MonitorTokenView {
  const token = tokenNamed(symbol);
  const address = `0x${addressDigit.repeat(40)}`;
  token.id = `bsc:${address}`;
  token.identity.contract_address = address;
  token.primary_state = primaryState;
  token.active_states = activeStates;
  return token;
}

function withTokens(...tokens: MonitorTokenView[]): MonitorV3Snapshot {
  return { ...snapshot, tokens };
}

describe("backend V3 contract", () => {
  it("accepts the current schema-3 projection without a masking assertion", () => {
    const solo = tokenNamed("SOLO");
    const stack = tokenNamed("STACK");
    const block = tokenNamed("BLOCK");

    expect(snapshot.schema_version).toBe(3);
    expect(snapshot.monitor_status).toBe("healthy");
    expect(solo.market.status).toBe("unknown");
    expect(solo.market.field_status.liquidity_usd).toBe("unknown");
    expect(solo.market.snapshots).toHaveLength(1);
    expect(solo.ranking_axes.quality.status).toBe("unknown");
    expect(stack.wallet_evidence).toEqual({
      verified_buyers: 2,
      wallet_addresses: [
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      ],
      evidence_ids: ["stack-wallet-1", "stack-wallet-2"],
      status: "verified",
      freshness_window_seconds: 900,
      historical_verified_buyers: 2,
      historical_wallet_addresses: [
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      ],
      conflicting_wallets: [],
      conflicting_evidence_ids: [],
    });
    expect(block.wallet_evidence.status).toBe("conflicting");
    expect(block.wallet_evidence.verified_buyers).toBe(0);
    expect(block.wallet_evidence.conflicting_wallets).toEqual([
      "0xcccccccccccccccccccccccccccccccccccccccc",
    ]);
    expect(block.wallet_evidence.conflicting_evidence_ids).toHaveLength(2);
    expect(stack.resonance.provider_families).toEqual(["985", "gmgn", "okx"]);
    expect(stack.resonance.freshness_window_seconds).toBe(900);
    expect(stack.resonance.historical_provider_families).toEqual(["985", "gmgn", "okx"]);
    expect(stack.resonance.historical_evidence_ids).toEqual(stack.resonance.evidence_ids);
    expect(stack.ranking_axes.timing).toEqual({
      status: "known",
      real_age_seconds: null,
      first_seen_age_seconds: 120,
      latest_event_age_seconds: 120,
      current_observation_age_seconds: 0,
      last_observation_age_seconds: 0,
      market_snapshot_age_seconds: 0,
      previous_meaningful_event_age_seconds: 120,
      pair_created_at: null,
      pair_age_seconds: null,
      migration_at: null,
      migration_age_seconds: null,
      last_observation_at: "2026-09-10T12:02:00+00:00",
      market_observed_at: "2026-09-10T12:02:00+00:00",
      previous_meaningful_event_at: "2026-09-10T12:00:00+00:00",
      report_refreshed_at: "2026-09-10T12:02:00+00:00",
    });
    expect(stack.market.snapshots[0].top10_holder_pct).toBe(17.15);
    expect(stack.state_history).toEqual([
      {
        state: "new",
        state_version: 1,
        observed_at: "2026-09-10T12:00:00+00:00",
      },
      {
        state: "smart_cluster",
        state_version: 2,
        observed_at: "2026-09-10T12:00:00+00:00",
      },
      {
        state: "smart_cluster",
        state_version: 3,
        observed_at: "2026-09-10T12:01:00+00:00",
      },
    ]);
    expect(stack.risk.hard_blocked).toBe(false);
    expect(solo.freshness.status).toBe("stale");
    expect(solo.ai.analyzed_at).toBeNull();
    expect(solo.missing_evidence).toContain("liquidity_usd");
  });

  it("rejects projections that omit current backend contract fields", () => {
    const [first, ...rest] = reportFixture.tokens;
    const { historical_verified_buyers: _walletHistory, ...walletEvidence } =
      first.wallet_evidence;
    const { conflicting_wallets: _walletConflicts, ...walletEvidenceWithoutConflicts } =
      first.wallet_evidence;
    const { historical_evidence_ids: _resonanceHistory, ...resonance } = first.resonance;
    const { last_observation_at: _lastObservation, ...timing } = first.ranking_axes.timing;
    const { state_history: _stateHistory, ...tokenWithoutStateHistory } = first;
    const withoutWalletHistory = {
      ...reportFixture,
      tokens: [{ ...first, wallet_evidence: walletEvidence }, ...rest],
    };
    const withoutWalletConflicts = {
      ...reportFixture,
      tokens: [{ ...first, wallet_evidence: walletEvidenceWithoutConflicts }, ...rest],
    };
    const withoutResonanceHistory = {
      ...reportFixture,
      tokens: [{ ...first, resonance }, ...rest],
    };
    const withoutObservationTiming = {
      ...reportFixture,
      tokens: [{ ...first, ranking_axes: { ...first.ranking_axes, timing } }, ...rest],
    };
    const withoutStateHistory = {
      ...reportFixture,
      tokens: [tokenWithoutStateHistory, ...rest],
    };

    expect(parseMonitorV3Snapshot(withoutWalletHistory)).toBeUndefined();
    expect(parseMonitorV3Snapshot(withoutWalletConflicts)).toBeUndefined();
    expect(parseMonitorV3Snapshot(withoutResonanceHistory)).toBeUndefined();
    expect(parseMonitorV3Snapshot(withoutObservationTiming)).toBeUndefined();
    expect(parseMonitorV3Snapshot(withoutStateHistory)).toBeUndefined();
  });

  it("requires an audit value property while accepting an explicit null value", () => {
    const auditFact = {
      audit_field: "honeypot",
      value: null,
      status: "unknown",
      provider_family: "gmgn",
      upstream_provider: "gmgn_token_security",
      observed_at: "2026-09-10T12:01:00+00:00",
      confidence: "provider_reported",
      evidence_id: "audit-null-value",
    };
    const withExplicitNull = {
      ...reportFixture,
      tokens: [
        { ...reportFixture.tokens[0], audit_facts: [auditFact] },
        ...reportFixture.tokens.slice(1),
      ],
    };
    const { value: _value, ...auditFactWithoutValue } = auditFact;
    const withoutValueProperty = {
      ...withExplicitNull,
      tokens: [
        { ...withExplicitNull.tokens[0], audit_facts: [auditFactWithoutValue] },
        ...withExplicitNull.tokens.slice(1),
      ],
    };

    expect(parseMonitorV3Snapshot(withExplicitNull)).toBeDefined();
    expect(parseMonitorV3Snapshot(withoutValueProperty)).toBeUndefined();
  });

  it("keeps event and rejection record schemas at version 1", () => {
    expect(tokenNamed("SOLO").events[0].schema_version).toBe(1);
    expect(snapshot.rejections[0].schema_version).toBe(1);
  });
});

describe("selectMonitorRows", () => {
  it("normalizes the ARC chain id and applies every chain filter after stage selection", () => {
    const bsc = variant("STACK", "6", "building", ["new", "building"]);
    const robinhood = variant("STACK", "7", "building", ["new", "building"]);
    robinhood.id = "robinhood:0x7777777777777777777777777777777777777777";
    robinhood.identity.chain = "robinhood";
    robinhood.events.forEach((event) => { event.chain = "robinhood"; });
    const arc = variant("STACK", "8", "building", ["new", "building"]);
    arc.id = "arc:0x8888888888888888888888888888888888888888";
    arc.identity.chain = "5042";
    arc.events.forEach((event) => { event.chain = "5042"; });
    const current = withTokens(bsc, robinhood, arc);

    expect(selectMonitorRows(current, "building", "", "all")).toEqual([arc, bsc, robinhood]);
    expect(selectMonitorRows(current, "building", "", "bsc")).toEqual([bsc]);
    expect(selectMonitorRows(current, "building", "", "robinhood")).toEqual([robinhood]);
    expect(selectMonitorRows(current, "building", "", "arc")).toEqual([arc]);
  });

  it("keeps ARC discovery out of focus while admitting ARC early-bird and confirmation", () => {
    const discovery = variant("STACK", "6", "new", ["new"]);
    const early = variant("STACK", "7", "building", ["new", "building"]);
    const confirmed = variant("STACK", "8", "resonating", ["new", "building", "resonating"]);
    for (const row of [discovery, early, confirmed]) {
      row.id = row.id.replace("bsc:", "arc:");
      row.identity.chain = "arc";
      row.identity.name = `ARC ${row.primary_state}`;
      row.market.market_cap_usd = 25_000;
      row.market.liquidity_usd = 12_000;
      row.market.holders = null;
      row.freshness.status = "fresh";
      row.events.forEach((event) => { event.chain = "arc"; });
    }

    const rows = selectMonitorRows(withTokens(discovery, early, confirmed), "focus", "", "arc");

    expect(rows).toEqual([confirmed, early]);
    expect(rows).not.toContain(discovery);
  });

  it("places backend market-behavior downgrades in trend watch", () => {
    const watch = structuredClone(tokenNamed("STACK")) as MonitorTokenView & {
      ranking_axes: MonitorTokenView["ranking_axes"] & {
        market_behavior: { status: string; disposition: string; flags: string[]; evidence: Record<string, unknown> };
      };
    };
    watch.primary_state = "trend_watch" as MonitorState;
    watch.active_states = ["new", "trend_watch"] as MonitorState[];
    watch.ranking_axes.market_behavior = {
      status: "known",
      disposition: "observe",
      flags: ["thin_holder_base"],
      evidence: { holders: 3 },
    };
    const current = withTokens(watch);

    expect(parseMonitorV3Snapshot(current)).toBeDefined();
    expect(selectMonitorRows(current, "trend", "")).toEqual([watch]);
    expect(selectMonitorRows(current, "resonating", "")).toEqual([]);
  });

  it("keeps a stale one-source new discovery visible with unknown market values", () => {
    const rows = selectMonitorRows(snapshot, "new", "solo");

    expect(rows.map((row) => row.id)).toContain(
      "bsc:0x1111111111111111111111111111111111111111",
    );
    expect(rows.find((row) => row.identity.symbol === "SOLO")?.resonance.provider_families).toEqual([
      "gmgn",
    ]);
    expect(rows.find((row) => row.identity.symbol === "SOLO")?.freshness.status).toBe("stale");
    expect(rows.find((row) => row.identity.symbol === "SOLO")?.market.market_cap_usd).toBeNull();
  });

  it("excludes old foundational new states without a current discovery event", () => {
    expect(selectMonitorRows(snapshot, "new", "").map((row) => row.identity.symbol)).toEqual([
      "STACK",
      "SOLO",
      "BLOCK",
    ]);
    expect(selectMonitorRows(snapshot, "new", "OLDIE")).toEqual([]);
    expect(selectMonitorRows(snapshot, "new", "REVIVE")).toEqual([]);
  });

  it("uses both event and observation time at the published chain-window boundary", () => {
    const current = tokenNamed("SOLO");
    const reportTime = Date.parse(snapshot.observed_at);
    const secondsAgo = (seconds: number) => new Date(reportTime - seconds * 1000).toISOString();

    current.events[0].event_at = secondsAgo(900);
    current.events[0].observed_at = secondsAgo(900);
    current.resonance.freshness_window_seconds = 900;
    expect(selectMonitorRows(withTokens(current), "new", "")).toHaveLength(1);

    current.events[0].observed_at = secondsAgo(901);
    expect(selectMonitorRows(withTokens(current), "new", "")).toEqual([]);

    current.identity.chain = "robinhood";
    current.events[0].chain = "robinhood";
    current.events[0].event_at = secondsAgo(1800);
    current.events[0].observed_at = secondsAgo(1800);
    current.resonance.freshness_window_seconds = 1800;
    expect(selectMonitorRows(withTokens(current), "new", "")).toHaveLength(1);
  });

  it("derives current discoveries from snapshot time instead of the wall clock", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime("1999-01-01T00:00:00Z");
      const before = selectMonitorRows(snapshot, "new", "").map((row) => row.id);
      vi.setSystemTime("2049-01-01T00:00:00Z");
      const after = selectMonitorRows(snapshot, "new", "").map((row) => row.id);

      expect(after).toEqual(before);
      expect(after).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows one token only in its highest current stage", () => {
    const id = "bsc:0x2222222222222222222222222222222222222222";

    expect(selectMonitorRows(snapshot, "selected", "").map((row) => row.id)).not.toContain(id);
    expect(selectMonitorRows(snapshot, "building", "").map((row) => row.id)).not.toContain(id);
    expect(selectMonitorRows(snapshot, "resonating", "").map((row) => row.id)).not.toContain(id);
    expect(selectMonitorRows(snapshot, "smart", "").map((row) => row.id)).toContain(id);
  });

  it("keeps fresh unpromoted discoveries in discovery only", () => {
    const discovery = variant("STACK", "8", "new", ["new"]);

    expect(selectMonitorRows(withTokens(discovery), "selected", "")).toEqual([discovery]);
    expect(selectMonitorRows(withTokens(discovery), "building", "")).toEqual([]);
    expect(selectMonitorRows(withTokens(discovery), "resonating", "")).toEqual([]);
  });

  it("shows a fresh ranked candidate before a second provider confirms it", () => {
    const ranked = variant("STACK", "7", "new", ["new"]);
    ranked.events = ranked.events.slice(0, 1).map((event) => ({
      ...event,
      evidence_role: "ranking",
      signal_lane: "hot_search",
      provider_family: "gmgn",
      provider_feed: "gmgn_hot_search",
      event_at: snapshot.observed_at,
      observed_at: snapshot.observed_at,
      counts_for_resonance: true,
    }));
    ranked.resonance.provider_families = ["gmgn"];
    ranked.resonance.signal_lanes = ["hot_search"];
    ranked.market.market_cap_usd = 25_000;
    ranked.market.liquidity_usd = 12_000;
    ranked.market.holders = 30;

    expect(selectMonitorRows(withTokens(ranked), "selected", "")).toEqual([ranked]);
    expect(selectMonitorRows(withTokens(ranked), "resonating", "")).toEqual([]);
  });

  it("keeps thin raw launches out of the visible discovery stage", () => {
    const thin = variant("STACK", "8", "new", ["new"]);
    thin.market.market_cap_usd = 4_000;
    thin.market.liquidity_usd = 500;
    thin.market.holders = 2;

    expect(selectMonitorRows(withTokens(thin), "selected", "")).toEqual([]);
  });

  it("keeps thin BSC launches out of aggregate confirmation", () => {
    const thin = variant("STACK", "9", "resonating", ["new", "resonating"]);
    thin.market.market_cap_usd = 4_400;
    thin.market.liquidity_usd = 0;
    thin.market.holders = 2;

    expect(isConfirmationEligible(thin)).toBe(false);
    expect(selectMonitorRows(withTokens(thin), "resonating", "")).toEqual([]);
    expect(selectMonitorRows(withTokens(thin), "selected", "")).toEqual([]);
  });

  it.each([
    [null, false],
    [19, false],
    [20, true],
  ])("requires known ARC holders at the 20-holder fallback boundary (%s)", (holders, eligible) => {
    const arc = variant("STACK", "6", "resonating", ["new", "resonating"]);
    arc.identity.chain = "arc";
    arc.id = `arc:${arc.identity.contract_address}`;
    arc.resonance.confirmation_gate = undefined;
    arc.market.market_cap_usd = 40_000;
    arc.market.liquidity_usd = 12_000;
    arc.market.holders = holders;
    arc.market.snapshots = [];

    expect(isConfirmationEligible(arc)).toBe(eligible);
  });

  it("moves expired cross-source attention into trend watch instead of early or confirmation", () => {
    const late = variant("STACK", "a", "building", ["new", "building", "smart_cluster"]);
    late.resonance.subtype = "cross_provider";
    late.resonance.provider_families = ["gmgn", "proficy"];
    late.resonance.signal_lanes = ["trending", "hot_search"];
    late.resonance.confirmation_gate = {
      eligible: false,
      failures: ["discovery_window_expired"],
      minimum_market_cap_usd: 10_000,
      minimum_liquidity_usd: 8_000,
      minimum_holders: 20,
    };
    late.market.market_cap_usd = 210_000;
    late.market.liquidity_usd = 37_000;
    late.market.holders = 147;
    late.freshness.status = "fresh";

    expect(selectMonitorRows(withTokens(late), "building", "")).toEqual([]);
    expect(selectMonitorRows(withTokens(late), "resonating", "")).toEqual([]);
    expect(selectMonitorRows(withTokens(late), "smart", "")).toEqual([]);
    expect(selectMonitorRows(withTokens(late), "trend", "")).toEqual([late]);
  });

  it("keeps a blocked valid token in both all-live and risk", () => {
    const id = "bsc:0x3333333333333333333333333333333333333333";

    expect(selectMonitorRows(snapshot, "all", "").map((row) => row.id)).toContain(id);
    expect(selectMonitorRows(snapshot, "risk", "").map((row) => row.id)).toContain(id);
  });

  it("does not infer revival from token age", () => {
    expect(selectMonitorRows(snapshot, "revival", "OLDIE")).toEqual([]);
    expect(selectMonitorRows(snapshot, "revival", "REVIVE").map((row) => row.id)).toEqual([
      "bsc:0x5555555555555555555555555555555555555555",
    ]);
  });

  it("matches symbol, name, chain, and full contract address case-insensitively", () => {
    expect(selectMonitorRows(snapshot, "all", "stacked signal")).toHaveLength(1);
    expect(selectMonitorRows(snapshot, "all", "BSC")).toHaveLength(5);
    expect(
      selectMonitorRows(snapshot, "all", "0X1111111111111111111111111111111111111111"),
    ).toHaveLength(1);
  });
});

describe("tab-specific sorting", () => {
  it("sorts all-live by primary-state priority, then state transition time", () => {
    expect(selectMonitorRows(snapshot, "all", "").map((row) => row.identity.symbol)).toEqual([
      "BLOCK",
      "REVIVE",
      "STACK",
      "SOLO",
      "OLDIE",
    ]);
  });

  it("sorts new discoveries by current discovery-event age rather than another event or transition", () => {
    const fresher = variant("SOLO", "6", "new", ["new"]);
    const older = variant("OLDIE", "7", "new", ["new"]);
    const reportTime = Date.parse(snapshot.observed_at);
    const secondsAgo = (seconds: number) => new Date(reportTime - seconds * 1000).toISOString();
    fresher.events[0].event_at = secondsAgo(10);
    fresher.events[0].observed_at = secondsAgo(10);
    fresher.ranking_axes.timing.latest_event_age_seconds = 999;
    fresher.state_updated_at = "2026-09-10T10:00:00+00:00";
    older.events[0].event_at = secondsAgo(20);
    older.events[0].observed_at = secondsAgo(20);
    older.ranking_axes.timing.latest_event_age_seconds = 0;
    older.state_updated_at = "2026-09-10T13:00:00+00:00";

    expect(selectMonitorRows(withTokens(older, fresher), "new", "").map((row) => row.id)).toEqual([
      fresher.id,
      older.id,
    ]);
  });

  it("sorts accelerating rows by flow deltas, not an unrelated later transition", () => {
    const stronger = variant("STACK", "6", "building", ["new", "building"]);
    const weaker = variant("REVIVE", "7", "building", ["new", "building"]);
    stronger.ranking_axes.flow.deltas = {
      unique_buyers: 12,
      net_buy_flow: 400,
      trade_frequency: 20,
      volume: 900,
    };
    stronger.state_updated_at = "2026-09-10T10:00:00+00:00";
    weaker.ranking_axes.flow.deltas = {
      unique_buyers: 2,
      net_buy_flow: 40,
      trade_frequency: 4,
      volume: 100,
    };
    weaker.state_updated_at = "2026-09-10T13:00:00+00:00";

    expect(
      selectMonitorRows(withTokens(weaker, stronger), "building", "").map((row) => row.id),
    ).toEqual([stronger.id, weaker.id]);
  });

  it("sorts resonance by subtype and independent evidence breadth", () => {
    const stronger = variant("STACK", "6", "resonating", ["new", "resonating"]);
    const weaker = variant("STACK", "7", "resonating", ["new", "resonating"]);
    weaker.resonance.subtype = "platform_stack";
    weaker.resonance.provider_families = ["gmgn"];
    weaker.resonance.signal_lanes = ["new_launch", "trending"];
    weaker.resonance.evidence_ids = ["weak-1", "weak-2"];
    stronger.state_updated_at = "2026-09-10T10:00:00+00:00";
    weaker.state_updated_at = "2026-09-10T13:00:00+00:00";

    expect(
      selectMonitorRows(withTokens(weaker, stronger), "resonating", "").map((row) => row.id),
    ).toEqual([stronger.id, weaker.id]);
  });

  it("sorts smart-money rows by verified address-level buyers", () => {
    const stronger = variant("STACK", "6", "smart_cluster", ["new", "smart_cluster"]);
    const weaker = variant("STACK", "7", "smart_cluster", ["new", "smart_cluster"]);
    stronger.wallet_evidence.verified_buyers = 4;
    weaker.wallet_evidence.verified_buyers = 2;
    stronger.state_updated_at = "2026-09-10T10:00:00+00:00";
    weaker.state_updated_at = "2026-09-10T13:00:00+00:00";

    expect(selectMonitorRows(withTokens(weaker, stronger), "smart", "").map((row) => row.id)).toEqual([
      stronger.id,
      weaker.id,
    ]);
  });

  it("sorts revival rows by baseline-relative acceleration", () => {
    const stronger = variant("REVIVE", "6", "revival", ["new", "building", "revival"]);
    const weaker = variant("REVIVE", "7", "revival", ["new", "building", "revival"]);
    stronger.ranking_axes.flow.baseline_deltas = {
      unique_buyers: 15,
      net_buy_flow: 500,
      trade_frequency: 30,
      volume: 1200,
    };
    weaker.ranking_axes.flow.baseline_deltas = {
      unique_buyers: 3,
      net_buy_flow: 80,
      trade_frequency: 5,
      volume: 200,
    };
    stronger.state_updated_at = "2026-09-10T10:00:00+00:00";
    weaker.state_updated_at = "2026-09-10T13:00:00+00:00";

    expect(
      selectMonitorRows(withTokens(weaker, stronger), "revival", "").map((row) => row.id),
    ).toEqual([stronger.id, weaker.id]);
  });

  it("sorts risk rows by hard failures, then soft flags", () => {
    const stronger = variant("BLOCK", "6", "blocked_risk", ["new", "blocked_risk"]);
    const weaker = variant("BLOCK", "7", "blocked_risk", ["new", "blocked_risk"]);
    stronger.risk.hard_failures = ["confirmed_honeypot", "liquidity_removed"];
    stronger.risk.soft_flags = [];
    weaker.risk.hard_failures = ["confirmed_honeypot"];
    weaker.risk.soft_flags = ["wash_pattern"];
    stronger.state_updated_at = "2026-09-10T10:00:00+00:00";
    weaker.state_updated_at = "2026-09-10T13:00:00+00:00";

    expect(selectMonitorRows(withTokens(weaker, stronger), "risk", "").map((row) => row.id)).toEqual([
      stronger.id,
      weaker.id,
    ]);
  });
});

describe("monitorTabCounts", () => {
  it("counts mutually exclusive visible stages independently of search", () => {
    expect(selectMonitorRows(snapshot, "all", "solo")).toHaveLength(1);
    expect(monitorTabCounts(snapshot)).toEqual({
      focus: 0,
      all: 5,
      new: 3,
      selected: 0,
      building: 0,
      resonating: 0,
      smart: 1,
      trend: 0,
      revival: 1,
      risk: 1,
    });
  });
});

describe("missing and unknown V3 data", () => {
  it("returns a dedicated unavailable status without consulting legacy data", () => {
    expect(monitorV3Availability(undefined)).toBe("unavailable");
    expect(selectMonitorRows(undefined, "all", "")).toEqual([]);
    expect(monitorTabCounts(undefined)).toEqual({
      focus: 0,
      all: 0,
      new: 0,
      selected: 0,
      building: 0,
      resonating: 0,
      smart: 0,
      trend: 0,
      revival: 0,
      risk: 0,
    });
  });

  it("rejects an incompatible monitor envelope while preserving schema-1 child records", () => {
    const incompatible = parseMonitorV3Snapshot({ ...reportFixture, schema_version: 1 });

    expect(incompatible).toBeUndefined();
    expect(monitorV3Availability(incompatible)).toBe("unavailable");
    expect(selectMonitorRows(incompatible, "all", "")).toEqual([]);
  });

  it("formats unknown values as unavailable instead of zero", () => {
    expect(formatNullableNumber(null)).toBe("不可用");
    expect(formatNullableNumber(undefined)).toBe("不可用");
    expect(formatUsd(null)).toBe("不可用");
    expect(formatNullableNumber(0)).toBe("0");
    expect(formatUsd(0)).toBe("$0");
  });
});
