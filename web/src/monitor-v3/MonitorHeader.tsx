import { Activity, RefreshCw } from "lucide-react";

import { monitorTabCounts } from "./selectors";
import type { MonitorV3Snapshot } from "./types";

const HEALTHY_SOURCE_STATES = new Set(["ok", "healthy", "success", "fresh"]);

interface SourceSummary {
  active: number;
  degraded: number;
  total: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sourceState(value: unknown): string {
  if (typeof value === "boolean") return value ? "ok" : "error";
  if (isRecord(value)) return String(value.status ?? "unknown").toLowerCase();
  return String(value ?? "unknown").toLowerCase();
}

function summarizeSources(sourceHealth: Record<string, unknown>): SourceSummary {
  const entries = Object.values(sourceHealth);
  const active = entries.filter((entry) => HEALTHY_SOURCE_STATES.has(sourceState(entry))).length;
  return { active, degraded: entries.length - active, total: entries.length };
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "不可用";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}时`;
  return `${Math.floor(seconds / 86_400)}天`;
}

function latestEventLatency(snapshot: MonitorV3Snapshot): number | null {
  const ages = snapshot.tokens
    .map((token) => token.ranking_axes.timing.latest_event_age_seconds)
    .filter((age): age is number => age !== null && Number.isFinite(age));
  return ages.length > 0 ? Math.min(...ages) : null;
}

interface MonitorHeaderProps {
  snapshot: MonitorV3Snapshot;
}

export function MonitorHeader({ snapshot }: MonitorHeaderProps) {
  const counts = monitorTabCounts(snapshot);
  const sources = summarizeSources(snapshot.source_health);
  const hasSourceHealth = sources.total > 0;
  const refreshLabel =
    snapshot.monitor_status === "healthy"
      ? "正常刷新"
      : snapshot.monitor_status === "degraded"
        ? "刷新降级"
        : "刷新未知";

  const metrics: ReadonlyArray<{
    label: string;
    value: string;
    tone?: "warning" | "positive" | "accent" | "danger";
  }> = [
    { label: "聚合候选", value: String(counts.selected), tone: "accent" },
    { label: "来源在线", value: hasSourceHealth ? `${sources.active}/${sources.total}` : "不可用", tone: "positive" },
    { label: "信号延迟", value: formatAge(latestEventLatency(snapshot)) },
    { label: "异常 / 淘汰", value: hasSourceHealth ? `${sources.degraded} / ${counts.risk}` : `— / ${counts.risk}`, tone: sources.degraded > 0 ? "warning" : "danger" },
  ];

  return (
    <header className="monitor-v3-header">
      <div className="monitor-v3-title-block">
        <div className="monitor-v3-heading-row">
          <span className={`monitor-v3-kicker is-${snapshot.monitor_status}`}>
            <Activity size={14} aria-hidden="true" /> 实时监控
          </span>
          <h1>MEME 金狗监控</h1>
          <span className={`monitor-v3-refresh is-${snapshot.monitor_status}`}>
            <RefreshCw size={13} aria-hidden="true" /> {refreshLabel}
          </span>
        </div>
        <p>多源共振筛选，点击币种查看叙事、AI、聪明钱与审计证据</p>
      </div>

      <div className="monitor-v3-metrics" aria-label="监控运行状态">
        {metrics.map(({ label, value, tone }) => (
          <div className={`monitor-v3-metric${tone ? ` is-${tone}` : ""}`} key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
    </header>
  );
}
