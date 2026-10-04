import type { MonitorAlert, MonitorV3Snapshot } from "./types";
import { isConfirmationEligible } from "./selectors";

export const MONITOR_ALERT_MAX_AGE_MS = 15 * 60_000;
export const MONITOR_ALERT_STARTUP_WINDOW_MS = 90_000;

function alertTime(alert: MonitorAlert): number | null {
  const value = Date.parse(alert.observed_at);
  return Number.isFinite(value) ? value : null;
}

export type MonitorAlertStage =
  | "early"
  | "confirmed"
  | "smart"
  | "revival"
  | "trend_watch"
  | "high_risk";

export function monitorAlertStage(alert: MonitorAlert): MonitorAlertStage {
  if (alert.severity === "critical" || alert.label === "高风险") return "high_risk";
  if (alert.policy.stage === "revival") return "revival";
  if (["late_resonance", "market_behavior"].includes(alert.policy.stage) || alert.label === "趋势观察") {
    return "trend_watch";
  }
  if (["building", "early_focus"].includes(alert.policy.stage) || alert.label === "加速") return "early";
  if (alert.policy.stage === "high_priority" || alert.label === "多源共振" || alert.severity === "high") {
    return "confirmed";
  }
  if (alert.verified_wallet_count > 0 || (alert.candidate_wallet_count ?? 0) > 0) return "smart";
  return "early";
}

export function monitorAlertDisplayKey(alert: MonitorAlert): string {
  const chain = alert.chain.trim().toLowerCase();
  const address = ["sol", "solana"].includes(chain)
    ? alert.contract_address.trim()
    : alert.contract_address.trim().toLowerCase();
  return `${chain}:${address}:${monitorAlertStage(alert)}`;
}

function monitorAlertStageLabel(alert: MonitorAlert): string {
  const stage = monitorAlertStage(alert);
  if (stage === "confirmed") return "聚合确认";
  if (stage === "early") return "聚合早鸟";
  if (stage === "smart") return "聪明钱";
  if (stage === "revival") return "老币复活";
  if (stage === "trend_watch") return "趋势观察";
  return "高风险";
}

function monitorAlertStageDetail(alert: MonitorAlert): string {
  const stage = monitorAlertStage(alert);
  if (stage === "confirmed") {
    return alert.transition.startsWith("early_focus->") ? "早鸟已升级" : "多源持续确认";
  }
  if (stage === "early") return "首次多源共振";
  if (stage === "smart") return "聪明钱异动";
  if (stage === "revival") return "老池重新放量";
  if (stage === "trend_watch") return "趋势变化待观察";
  return "风险门已拦截";
}

export function activeMonitorAlerts(
  snapshot: MonitorV3Snapshot | undefined,
  now = Date.now(),
): MonitorAlert[] {
  if (!snapshot) return [];
  const unique = new Map<string, MonitorAlert>();
  for (const alert of snapshot.alerts) {
    const createdAt = alertTime(alert);
    if (createdAt === null || createdAt > now + 60_000 || now - createdAt > MONITOR_ALERT_MAX_AGE_MS) {
      continue;
    }
    const token = snapshot.tokens.find(
      (item) =>
        item.identity.chain === alert.chain &&
        item.identity.contract_address.toLowerCase() === alert.contract_address.toLowerCase(),
    );
    if (
      token &&
      alert.label !== "高风险" &&
      alert.verified_wallet_count < 1 &&
      token.resonance.subtype !== null &&
      !isConfirmationEligible(token, snapshot.observed_at)
    ) {
      continue;
    }
    const displayKey = monitorAlertDisplayKey(alert);
    const previous = unique.get(displayKey);
    if (!previous || Date.parse(previous.observed_at) < createdAt) unique.set(displayKey, alert);
  }
  return [...unique.values()].sort(
    (left, right) => (alertTime(right) ?? 0) - (alertTime(left) ?? 0),
  );
}

export function startupMonitorAlerts(
  alerts: MonitorAlert[],
  now = Date.now(),
): MonitorAlert[] {
  return alerts.filter((alert) => {
    const createdAt = alertTime(alert);
    return createdAt !== null && createdAt >= now - MONITOR_ALERT_STARTUP_WINDOW_MS;
  });
}

export function monitorAlertSoundTier(alert: MonitorAlert): "early" | "confirmed" | "revival" | "trend_watch" | "high_risk" {
  const stage = monitorAlertStage(alert);
  return stage === "smart" ? "early" : stage;
}

export function monitorAlertTitle(alert: MonitorAlert): string {
  return `${monitorAlertStageLabel(alert)} · ${alert.symbol || "未知代币"}`;
}

export function monitorAlertBody(alert: MonitorAlert): string {
  const parts = [monitorAlertStageDetail(alert), alert.chain.toUpperCase()];
  const candidateWalletCount = Math.max(0, Math.floor(alert.candidate_wallet_count ?? 0));
  if (alert.policy.stage === "late_resonance") {
    parts.push("超过首发窗口");
  } else if (alert.policy.stage === "market_behavior") {
    parts.push("市场行为降级");
  }
  if (alert.market.current_market_cap_usd !== null) {
    parts.push(`MC $${Math.round(alert.market.current_market_cap_usd).toLocaleString("en-US")}`);
  }
  if (alert.verified_wallet_count > 0) parts.push(`${alert.verified_wallet_count}个验证钱包`);
  else if (candidateWalletCount > 0) parts.push(`${candidateWalletCount}个平台钱包`);
  return parts.join(" · ");
}

export function monitorAlertSpeechText(alert: MonitorAlert): string {
  const symbol = alert.symbol?.trim() || "未知代币";
  const candidateWalletCount = Math.max(0, Math.floor(alert.candidate_wallet_count ?? 0));
  const stage = monitorAlertStage(alert);

  if (stage === "high_risk") {
    return `高风险，${symbol}，已拦截。`;
  }
  if (stage === "revival") {
    return `老币复活，${symbol}，等二次放量。`;
  }
  if (stage === "trend_watch") {
    return `趋势观察，${symbol}，不追高。`;
  }
  if (stage === "early") {
    return `聚合早鸟，${symbol}，首次多源共振，进入快速观察。`;
  }
  if (stage === "confirmed") {
    const detail = alert.transition.startsWith("early_focus->") ? "已从早鸟升级" : "多源持续确认";
    return `聚合确认，${symbol}，${detail}，请重点查看。`;
  }
  if (alert.verified_wallet_count > 0) {
    return `聪明钱，${symbol}，${alert.verified_wallet_count}个钱包。`;
  }
  if (candidateWalletCount > 0) {
    return `平台钱包，${symbol}，${candidateWalletCount}个钱包。`;
  }
  return `早鸟，${symbol}，继续观察。`;
}
