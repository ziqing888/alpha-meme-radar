import { ExternalLink } from "lucide-react";

import type { MonitorEvent, MonitorStateHistoryEntry, SignalLane } from "./types";

const LANE_LABELS: ReadonlyArray<{ lane: SignalLane; label: string }> = [
  { lane: "new_launch", label: "新币首发" },
  { lane: "trending", label: "趋势榜" },
  { lane: "hot_search", label: "热搜" },
  { lane: "smart_money", label: "聪明钱" },
  { lane: "kol_social", label: "KOL / 社交" },
  { lane: "market_flow", label: "资金流" },
];

const STATE_LABELS: Record<string, string> = {
  new: "新发现",
  building: "聚合早鸟",
  resonating: "聚合确认",
  smart_cluster: "聪明钱",
  cooling: "冷却",
  revival: "老币复活",
  blocked_risk: "风险淘汰",
  stale: "陈旧",
};

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "不可用";
  return date.toLocaleString("zh-CN", { hour12: false });
}

interface TimelineItem {
  id: string;
  at: string;
  kind: "event" | "state";
  title: string;
  detail: string;
}

function timelineItems(
  events: MonitorEvent[],
  stateHistory: MonitorStateHistoryEntry[],
): TimelineItem[] {
  return [
    ...events.map((event) => ({
      id: `event-${event.event_id}`,
      at: event.observed_at,
      kind: "event" as const,
      title: event.event_type.replace(/_/g, " "),
      detail: `${event.provider_family.toUpperCase()} · ${event.provider_feed}`,
    })),
    ...stateHistory.map((state) => ({
      id: `state-${state.state_version}-${state.observed_at}`,
      at: state.observed_at,
      kind: "state" as const,
      title: STATE_LABELS[state.state] ?? state.state,
      detail: `状态版本 ${state.state_version}`,
    })),
  ].sort((left, right) => Date.parse(right.at) - Date.parse(left.at));
}

export function EvidenceTimeline({
  events,
  stateHistory,
}: {
  events: MonitorEvent[];
  stateHistory: MonitorStateHistoryEntry[];
}) {
  const timeline = timelineItems(events, stateHistory);

  return (
    <section className="monitor-v3-detail-section" aria-labelledby="monitor-v3-timeline-title">
      <h3 id="monitor-v3-timeline-title">事件与状态时间线</h3>
      <ol className="monitor-v3-timeline">
        {timeline.map((item) => (
          <li key={item.id} className={`is-${item.kind}`}>
            <time dateTime={item.at}>{formatTime(item.at)}</time>
            <strong>{item.title}</strong>
            <span>{item.detail}</span>
          </li>
        ))}
      </ol>

      <h3>六路证据</h3>
      <div className="monitor-v3-evidence-lanes">
        {LANE_LABELS.map(({ lane, label }) => {
          const laneEvents = events.filter((event) => event.signal_lane === lane);
          return (
            <section key={lane} aria-label={`${label}证据`}>
              <header>
                <strong>{label}</strong>
                <span>{laneEvents.length}</span>
              </header>
              {laneEvents.length === 0 ? (
                <p className="monitor-v3-unavailable">不可用</p>
              ) : (
                <ul>
                  {laneEvents.map((event) => (
                    <li key={event.event_id}>
                      <div>
                        <strong>{event.provider_family.toUpperCase()}</strong>
                        <span>{event.provider_feed}</span>
                      </div>
                      <time dateTime={event.observed_at}>{formatTime(event.observed_at)}</time>
                      <small>事件 {formatTime(event.event_at)} · {event.evidence_role}</small>
                      {event.source_url && (
                        <a
                          href={event.source_url}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`打开 ${label} 证据`}
                          title={`打开 ${label} 证据`}
                        >
                          <ExternalLink size={12} aria-hidden="true" />
                        </a>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </section>
  );
}
