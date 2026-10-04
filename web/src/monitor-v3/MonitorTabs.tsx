import type { MonitorTab } from "./types";

const TAB_LABELS: ReadonlyArray<{ id: MonitorTab; label: string }> = [
  { id: "focus", label: "精选总览" },
  { id: "selected", label: "聚合发现" },
  { id: "building", label: "聚合早鸟" },
  { id: "resonating", label: "聚合确认" },
  { id: "smart", label: "聪明钱" },
  { id: "trend", label: "趋势观察" },
  { id: "revival", label: "老币复活" },
  { id: "risk", label: "淘汰复盘" },
];

interface MonitorTabsProps {
  activeTab: MonitorTab;
  counts: Record<MonitorTab, number>;
  onChange: (tab: MonitorTab) => void;
}

export function MonitorTabs({ activeTab, counts, onChange }: MonitorTabsProps) {
  return (
    <div className="monitor-v3-tabs" role="tablist" aria-label="MEME V3 监控视图">
      {TAB_LABELS.map(({ id, label }) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={activeTab === id}
          className={activeTab === id ? "is-active" : undefined}
          onClick={() => onChange(id)}
        >
          <span>{label}</span><strong>{counts[id]}</strong>
        </button>
      ))}
    </div>
  );
}
