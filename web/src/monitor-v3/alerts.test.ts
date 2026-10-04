import { describe, expect, it } from "vitest";

import reportFixture from "../../tests/fixtures/monitor-v3-report.json";
import {
  activeMonitorAlerts,
  monitorAlertDisplayKey,
  monitorAlertBody,
  monitorAlertStage,
  monitorAlertSpeechText,
  monitorAlertSoundTier,
  monitorAlertTitle,
  startupMonitorAlerts,
} from "./alerts";
import { parseMonitorV3Snapshot, type MonitorAlert, type MonitorV3Snapshot } from "./types";

const now = Date.parse("2026-09-11T00:10:00Z");

function alert(overrides: Partial<MonitorAlert> = {}): MonitorAlert {
  return {
    key: "bsc:0xabc:new->cross_provider:2",
    label: "多源共振",
    severity: "high",
    policy: { chain: "bsc", stage: "high_priority" },
    chain: "bsc",
    contract_address: "0xabc",
    symbol: "DOG",
    transition: "new->cross_provider",
    state_version: 2,
    event_at: "2026-09-11T00:09:30Z",
    observed_at: "2026-09-11T00:09:35Z",
    real_age_seconds: 95,
    event_age_seconds: 5,
    market: {
      current_market_cap_usd: 20_000,
      liquidity_usd: 8_000,
      unique_buyers_5m: 15,
      net_buy_flow_5m_usd: 2_100,
    },
    verified_wallet_count: 2,
    candidate_wallet_count: 2,
    risks: { hard_blocked: false, hard_failures: [], soft_flags: [] },
    missing_evidence: [],
    evidence_ids: ["gmgn", "okx"],
    ...overrides,
  };
}

function snapshot(alerts: MonitorAlert[]): MonitorV3Snapshot {
  return {
    schema_version: 3,
    observed_at: "2026-09-11T00:10:00Z",
    monitor_status: "healthy",
    source_health: {},
    tokens: [],
    rejections: [],
    alerts,
  };
}

describe("monitor V3 transition alerts", () => {
  it("keeps one recent alert per token stage instead of repeating backend state versions", () => {
    const current = alert();
    const repeated = alert({
      key: "bsc:0xabc:cross_provider->cross_provider:3",
      transition: "cross_provider->cross_provider",
      state_version: 3,
      observed_at: "2026-09-11T00:09:45Z",
    });
    const stale = alert({ key: "old", observed_at: "2026-09-10T23:30:00Z" });
    expect(activeMonitorAlerts(snapshot([current, repeated, stale]), now)).toEqual([repeated]);
    expect(monitorAlertDisplayKey(current)).toBe(monitorAlertDisplayKey(repeated));
  });

  it("keeps early and confirmed identities separate for the same token", () => {
    const early = alert({
      key: "bsc:0xabc:new->early_focus:1",
      label: "重点",
      severity: "medium",
      policy: { chain: "bsc", stage: "early_focus" },
      transition: "new->early_focus",
      state_version: 1,
    });
    const confirmed = alert({
      key: "bsc:0xabc:early_focus->cross_provider:2",
      transition: "early_focus->cross_provider",
      state_version: 2,
    });

    expect(monitorAlertStage(early)).toBe("early");
    expect(monitorAlertStage(confirmed)).toBe("confirmed");
    expect(monitorAlertDisplayKey(early)).not.toBe(monitorAlertDisplayKey(confirmed));
    expect(activeMonitorAlerts(snapshot([early, confirmed]), now)).toHaveLength(2);
  });

  it("normalizes EVM identities without lowercasing Solana addresses", () => {
    expect(monitorAlertDisplayKey(alert({ chain: "BSC", contract_address: "0xAbC" }))).toContain("bsc:0xabc:");
    expect(monitorAlertDisplayKey(alert({ chain: "solana", contract_address: "AbCdEf" }))).toContain("solana:AbCdEf:");
  });

  it("only replays a transition on first load when it happened in the startup window", () => {
    const recent = alert();
    const older = alert({ key: "older", observed_at: "2026-09-11T00:07:00Z" });
    expect(startupMonitorAlerts(activeMonitorAlerts(snapshot([recent, older]), now), now)).toEqual([
      recent,
    ]);
  });

  it("maps transition severity to sound and preserves useful desktop evidence", () => {
    const current = alert();
    expect(monitorAlertSoundTier(current)).toBe("confirmed");
    expect(monitorAlertSoundTier(alert({ label: "高风险", severity: "critical" }))).toBe(
      "high_risk",
    );
    expect(monitorAlertBody(current)).toContain("多源持续确认 · BSC · MC $20,000");
    expect(monitorAlertBody(current)).toContain("2个验证钱包");
  });

  it("uses a distinct voice tier for genuine old-token revival", () => {
    expect(
      monitorAlertSoundTier(alert({ policy: { chain: "bsc", stage: "revival" } })),
    ).toBe("revival");
  });

  it("does not speak expired-window trend attention as confirmation", () => {
    const late = alert({
      label: "趋势观察",
      severity: "medium",
      policy: { chain: "bsc", stage: "late_resonance" },
      real_age_seconds: 14_400,
    });

    expect(monitorAlertSoundTier(late)).toBe("trend_watch");
    expect(monitorAlertBody(late)).toContain("超过首发窗口");
  });

  it("speaks market-behavior downgrades as trend watch", () => {
    const downgraded = alert({
      label: "趋势观察",
      severity: "medium",
      policy: { chain: "bsc", stage: "market_behavior" },
      symbol: "THIN",
    });

    expect(monitorAlertSoundTier(downgraded)).toBe("trend_watch");
    expect(monitorAlertSpeechText(downgraded)).toBe("趋势观察，THIN，不追高。");
  });

  it("speaks different monitor stages with clear intent", () => {
    expect(monitorAlertTitle(alert({ symbol: "OVC" }))).toBe("聚合确认 · OVC");
    expect(monitorAlertSpeechText(alert({ symbol: "OVC" }))).toBe(
      "聚合确认，OVC，多源持续确认，请重点查看。",
    );
    expect(
      monitorAlertSpeechText(
        alert({ label: "加速", severity: "medium", policy: { chain: "bsc", stage: "building" } }),
      ),
    ).toBe("聚合早鸟，DOG，首次多源共振，进入快速观察。");
    expect(
      monitorAlertSpeechText(
        alert({ label: "趋势观察", severity: "medium", policy: { chain: "bsc", stage: "late_resonance" } }),
      ),
    ).toBe("趋势观察，DOG，不追高。");
    expect(
      monitorAlertSpeechText(
        alert({
          label: "重点",
          severity: "medium",
          policy: { chain: "bsc", stage: "smart_money" },
          verified_wallet_count: 4,
        }),
      ),
    ).toBe("聪明钱，DOG，4个钱包。");
    expect(
      monitorAlertSpeechText(
        alert({
          label: "重点",
          severity: "medium",
          policy: { chain: "bsc", stage: "smart_money" },
          verified_wallet_count: 0,
          candidate_wallet_count: 5,
        }),
      ),
    ).toBe("平台钱包，DOG，5个钱包。");
    expect(
      monitorAlertSpeechText(alert({ policy: { chain: "bsc", stage: "revival" } })),
    ).toBe("老币复活，DOG，等二次放量。");
    expect(
      monitorAlertSpeechText(alert({ label: "高风险", severity: "critical" })),
    ).toBe("高风险，DOG，已拦截。");
  });

  it("keeps early-bird stage voice even when backend labels it as resonance", () => {
    const earlyResonance = alert({
      label: "多源共振",
      severity: "high",
      policy: { chain: "bsc", stage: "early_focus" },
      symbol: "BOB",
    });

    expect(monitorAlertSoundTier(earlyResonance)).toBe("early");
    expect(monitorAlertTitle(earlyResonance)).toBe("聚合早鸟 · BOB");
    expect(monitorAlertSpeechText(earlyResonance)).toBe(
      "聚合早鸟，BOB，首次多源共振，进入快速观察。",
    );
  });

  it("announces an early-bird promotion as an upgrade", () => {
    const promoted = alert({
      transition: "early_focus->cross_provider",
      symbol: "MICROHOOD",
    });

    expect(monitorAlertSpeechText(promoted)).toBe(
      "聚合确认，MICROHOOD，已从早鸟升级，请重点查看。",
    );
  });

  it("suppresses old-runtime resonance alerts for thin BSC launches", () => {
    const fixture = parseMonitorV3Snapshot(reportFixture);
    if (!fixture) throw new Error("invalid monitor fixture");
    const thin = structuredClone(fixture.tokens.find((token) => token.identity.symbol === "STACK"));
    if (!thin) throw new Error("missing STACK fixture");
    thin.market.market_cap_usd = 4_400;
    thin.market.liquidity_usd = 0;
    thin.market.holders = 2;
    const current = alert({
      contract_address: thin.identity.contract_address,
      verified_wallet_count: 0,
    });
    const currentSnapshot: MonitorV3Snapshot = {
      ...fixture,
      observed_at: "2026-09-11T00:10:00Z",
      tokens: [thin],
      alerts: [current],
    };

    expect(activeMonitorAlerts(currentSnapshot, now)).toEqual([]);
  });
});
