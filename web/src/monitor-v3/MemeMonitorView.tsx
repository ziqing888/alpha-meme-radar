import { Bell, BellOff, ExternalLink, Eye, EyeOff, Radio, Search, Volume2, VolumeX, X } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";

import { CandidateTable } from "./CandidateTable";
import type { MonitorStageTiming } from "./CandidateTable";
import { MonitorHeader } from "./MonitorHeader";
import { MonitorTabs } from "./MonitorTabs";
import { RiskAuditView } from "./RiskAuditView";
import {
  MONITOR_CHAINS,
  monitorTabCounts,
  monitorV3Availability,
  selectMonitorRows,
  type ChainFilter,
  type MonitorChain,
} from "./selectors";
import { TokenDetailDrawer } from "./TokenDetailDrawer";
import { monitorAlertBody, monitorAlertDisplayKey, monitorAlertStage, monitorAlertTitle } from "./alerts";
import { tokenIntelligenceKey } from "./intelligence";
import type { MonitorAlert, MonitorDiscoveryBaseline, MonitorDisplayQuote, MonitorTab, MonitorTokenIntelligence, MonitorTokenView, MonitorV3Snapshot } from "./types";
import "./monitor-v3.css";

interface MemeMonitorViewProps {
  snapshot: MonitorV3Snapshot | undefined;
  onOpenMarket: (row: MonitorTokenView) => void;
  imageByToken?: Record<string, string>;
  quoteByToken?: Record<string, MonitorDisplayQuote>;
  baselineByToken?: Record<string, MonitorDiscoveryBaseline>;
  intelligenceByToken?: Record<string, MonitorTokenIntelligence>;
  activeAlerts?: MonitorAlert[];
  onDismissAlert?: (key: string) => void;
  onOpenAlertMarket?: (alert: MonitorAlert) => void;
  alertControls?: {
    popupEnabled: boolean;
    soundEnabled: boolean;
    browserNoticeEnabled: boolean;
    onTogglePopup: () => void;
    onToggleSound: () => void;
    onToggleBrowserNotice: () => void;
  };
}

const CHAIN_FILTERS: ReadonlyArray<{ value: ChainFilter; label: string }> = [
  { value: "all", label: "全部" },
  ...Object.entries(MONITOR_CHAINS).map(([value, metadata]) => ({
    value: value as MonitorChain,
    label: metadata.label,
  })),
];

const HEALTHY_SOURCE_STATES = new Set(["ok", "healthy", "success", "fresh"]);

function sourceHealthStatus(value: unknown): string {
  if (typeof value === "boolean") return value ? "ok" : "error";
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return String((value as Record<string, unknown>).status ?? "unknown").toLowerCase();
  }
  return String(value ?? "unknown").toLowerCase();
}

function chainSourceHealth(
  snapshot: MonitorV3Snapshot,
  chain: MonitorChain,
): "healthy" | "degraded" | "unknown" | null {
  const entries = Object.entries(snapshot.source_health).filter(([source]) => {
    const normalized = source.trim().toLowerCase();
    return normalized === chain || normalized.startsWith(`${chain}_`) || normalized.startsWith(`${chain}-`);
  });
  if (entries.length === 0) return null;
  const states = entries.map(([, value]) => sourceHealthStatus(value));
  if (states.every((state) => HEALTHY_SOURCE_STATES.has(state))) return "healthy";
  if (states.some((state) => state !== "unknown")) return "degraded";
  return "unknown";
}

function elapsedStageSeconds(firstSeenAt: string, observedAtValues: string[]): number | null {
  const firstSeen = Date.parse(firstSeenAt);
  if (!Number.isFinite(firstSeen)) return null;
  const observed = observedAtValues
    .map((value) => Date.parse(value))
    .filter((value) => Number.isFinite(value) && value >= firstSeen);
  return observed.length === 0 ? null : Math.max(0, Math.round((Math.min(...observed) - firstSeen) / 1000));
}

function stageTimings(snapshot: MonitorV3Snapshot): Record<string, MonitorStageTiming> {
  const result: Record<string, MonitorStageTiming> = {};
  for (const token of snapshot.tokens) {
    const matchingAlerts = snapshot.alerts.filter(
      (alert) => alert.chain === token.identity.chain
        && alert.contract_address.toLowerCase() === token.identity.contract_address.toLowerCase(),
    );
    const earlyTimes = token.state_history
      .filter((entry) => entry.state === "building")
      .map((entry) => entry.observed_at);
    const confirmedTimes = token.state_history
      .filter((entry) => entry.state === "resonating")
      .map((entry) => entry.observed_at);
    for (const alert of matchingAlerts) {
      const stage = monitorAlertStage(alert);
      if (stage === "early") earlyTimes.push(alert.observed_at);
      if (stage === "confirmed") confirmedTimes.push(alert.observed_at);
    }
    result[token.id] = {
      earlySeconds: elapsedStageSeconds(token.identity.first_seen_at, earlyTimes),
      confirmedSeconds: elapsedStageSeconds(token.identity.first_seen_at, confirmedTimes),
    };
  }
  return result;
}

export function MemeMonitorView({
  snapshot,
  onOpenMarket,
  imageByToken = {},
  quoteByToken = {},
  baselineByToken = {},
  intelligenceByToken = {},
  activeAlerts = [],
  onDismissAlert,
  onOpenAlertMarket,
  alertControls,
}: MemeMonitorViewProps) {
  const [activeTab, setActiveTab] = useState<MonitorTab>("focus");
  const [chainFilter, setChainFilter] = useState<ChainFilter>("all");
  const [query, setQuery] = useState("");
  const [selectedToken, setSelectedToken] = useState<MonitorTokenView | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const counts = useMemo(() => monitorTabCounts(snapshot, chainFilter), [chainFilter, snapshot]);
  const rows = useMemo(
    () => selectMonitorRows(snapshot, activeTab, query, chainFilter),
    [activeTab, chainFilter, query, snapshot],
  );
  const confirmedRows = useMemo(
    () => selectMonitorRows(snapshot, "resonating", query, chainFilter),
    [chainFilter, query, snapshot],
  );
  const earlyRows = useMemo(
    () => selectMonitorRows(snapshot, "building", query, chainFilter),
    [chainFilter, query, snapshot],
  );
  const stageTimingByToken = useMemo(
    () => snapshot ? stageTimings(snapshot) : {},
    [snapshot],
  );
  const openDetails = useCallback((row: MonitorTokenView) => {
    const rowTrigger = Array.from(document.querySelectorAll<HTMLElement>("[data-monitor-token-id]"))
      .find((element) => element.dataset.monitorTokenId === row.id);
    returnFocusRef.current = rowTrigger ?? (
      document.activeElement instanceof HTMLElement ? document.activeElement : null
    );
    setSelectedToken(row);
  }, []);
  const closeDetails = useCallback(() => setSelectedToken(null), []);

  if (monitorV3Availability(snapshot) === "unavailable" || snapshot === undefined) {
    return (
      <section className="meme-monitor-v3 monitor-v3-unpublished" aria-label="MEME V3 实时监控">
        <strong>V3 数据尚未发布</strong>
        <span>等待事件投影生成首个版本化快照</span>
      </section>
    );
  }

  return (
    <section className="meme-monitor-v3" aria-label="MEME V3 实时监控">
      <MonitorHeader snapshot={snapshot} />
      <div className="monitor-v3-controls">
        <MonitorTabs activeTab={activeTab} counts={counts} onChange={setActiveTab} />
        <div className="monitor-v3-chain-filters" role="group" aria-label="链筛选">
          {CHAIN_FILTERS.map((filter) => (
            <button
              type="button"
              className={chainFilter === filter.value ? "is-active" : undefined}
              aria-pressed={chainFilter === filter.value}
              key={filter.value}
              onClick={() => setChainFilter(filter.value)}
            >
              {filter.label}
            </button>
          ))}
        </div>
        {alertControls && (
          <div className="monitor-v3-alert-controls" aria-label="V3 提醒设置">
            <button
              type="button"
              aria-label={alertControls.popupEnabled ? "关闭页面提醒" : "开启页面提醒"}
              title={alertControls.popupEnabled ? "关闭页面提醒" : "开启页面提醒"}
              aria-pressed={alertControls.popupEnabled}
              onClick={alertControls.onTogglePopup}
            >
              {alertControls.popupEnabled ? <Eye size={15} /> : <EyeOff size={15} />}
            </button>
            <button
              type="button"
              aria-label={alertControls.soundEnabled ? "关闭声音提醒" : "开启声音提醒"}
              title={alertControls.soundEnabled ? "关闭声音提醒" : "开启声音提醒"}
              aria-pressed={alertControls.soundEnabled}
              onClick={alertControls.onToggleSound}
            >
              {alertControls.soundEnabled ? <Volume2 size={15} /> : <VolumeX size={15} />}
            </button>
            <button
              type="button"
              aria-label={alertControls.browserNoticeEnabled ? "关闭桌面通知" : "开启桌面通知"}
              title={alertControls.browserNoticeEnabled ? "关闭桌面通知" : "开启桌面通知"}
              aria-pressed={alertControls.browserNoticeEnabled}
              onClick={alertControls.onToggleBrowserNotice}
            >
              {alertControls.browserNoticeEnabled ? <Bell size={15} /> : <BellOff size={15} />}
            </button>
          </div>
        )}
        <label className="monitor-v3-search">
          <Search size={15} aria-hidden="true" />
          <span className="monitor-v3-sr-only">搜索实时候选</span>
          <input
            type="search"
            aria-label="搜索实时候选"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索代币、链或完整 CA"
          />
        </label>
      </div>
      <div className="monitor-v3-chain-health" aria-label="链数据源状态">
        {(Object.keys(MONITOR_CHAINS) as MonitorChain[]).map((chain) => {
          const health = chainSourceHealth(snapshot, chain);
          if (health === null || (chainFilter !== "all" && chainFilter !== chain)) return null;
          const healthLabel = health === "healthy" ? "正常" : health === "degraded" ? "降级" : "未知";
          return (
            <span className={`is-${health}`} key={chain}>
              {MONITOR_CHAINS[chain].label} 数据源{healthLabel}
            </span>
          );
        })}
      </div>
      {activeTab === "focus" ? (
        confirmedRows.length === 0 && earlyRows.length === 0 ? (
          <div className="monitor-v3-empty" role="status">
            <Radio size={18} aria-hidden="true" />
            <strong>当前没有精选候选</strong>
            <span>等待新的聚合早鸟或聚合确认信号</span>
          </div>
        ) : (
          <div className="monitor-v3-focus-board">
            {confirmedRows.length > 0 && (
              <section className="monitor-v3-focus-section is-confirmed" aria-label="聚合确认">
                <header>
                  <div><strong>聚合确认</strong><span>{confirmedRows.length}</span></div>
                  <p>持续多源确认，优先查看</p>
                </header>
                <CandidateTable
                  rows={confirmedRows}
                  onSelect={openDetails}
                  onOpenMarket={onOpenMarket}
                  imageByToken={imageByToken}
                  quoteByToken={quoteByToken}
                  baselineByToken={baselineByToken}
                  intelligenceByToken={intelligenceByToken}
                  stageTimingByToken={stageTimingByToken}
                  showEvidence
                  ariaLabel="聚合确认候选"
                />
              </section>
            )}
            {earlyRows.length > 0 && (
              <section className="monitor-v3-focus-section is-early" aria-label="聚合早鸟">
                <header>
                  <div><strong>聚合早鸟</strong><span>{earlyRows.length}</span></div>
                  <p>首次共振加速，等待升级</p>
                </header>
                <CandidateTable
                  rows={earlyRows}
                  onSelect={openDetails}
                  onOpenMarket={onOpenMarket}
                  imageByToken={imageByToken}
                  quoteByToken={quoteByToken}
                  baselineByToken={baselineByToken}
                  intelligenceByToken={intelligenceByToken}
                  stageTimingByToken={stageTimingByToken}
                  showEvidence
                  ariaLabel="聚合早鸟候选"
                />
              </section>
            )}
          </div>
        )
      ) : activeTab === "risk" ? (
        <RiskAuditView
          snapshot={snapshot}
          blockedRows={rows}
          chainFilter={chainFilter}
          query={query}
          onSelect={openDetails}
        />
      ) : (
        <CandidateTable
          rows={rows}
          onSelect={openDetails}
          onOpenMarket={onOpenMarket}
          imageByToken={imageByToken}
          quoteByToken={quoteByToken}
          baselineByToken={baselineByToken}
          intelligenceByToken={intelligenceByToken}
          stageTimingByToken={stageTimingByToken}
          showEvidence={["building", "resonating", "smart"].includes(activeTab)}
          emptyMessage={activeTab === "selected" ? "当前没有通过去重与时效检查、且尚未晋级的新发现" : undefined}
        />
      )}
      {selectedToken && (
        <TokenDetailDrawer
          token={selectedToken}
          imageUrl={imageByToken[selectedToken.id]}
          displayQuote={quoteByToken[selectedToken.id]}
          onClose={closeDetails}
          onOpenMarket={onOpenMarket}
          returnFocusTo={returnFocusRef.current}
          intelligence={intelligenceByToken[tokenIntelligenceKey(
            selectedToken.identity.chain,
            selectedToken.identity.contract_address,
          )]}
        />
      )}
      {activeAlerts.length > 0 && (
        <aside className="monitor-v3-alert-dock" aria-label="V3 实时提醒">
          {activeAlerts.slice(0, 4).map((alert) => (
            <article className={`is-${alert.severity}`} key={monitorAlertDisplayKey(alert)}>
              <div>
                <strong>{monitorAlertTitle(alert)}</strong>
                <span>{monitorAlertBody(alert)}</span>
              </div>
              {onOpenAlertMarket && alert.label !== "高风险" && (
                <button type="button" title="打开 GMGN" aria-label={`打开 ${alert.symbol || "代币"} GMGN`} onClick={() => onOpenAlertMarket(alert)}>
                  <ExternalLink size={15} />
                </button>
              )}
              {onDismissAlert && (
                <button type="button" title="关闭提醒" aria-label={`关闭 ${alert.symbol || "代币"} 提醒`} onClick={() => onDismissAlert(monitorAlertDisplayKey(alert))}>
                  <X size={15} />
                </button>
              )}
            </article>
          ))}
        </aside>
      )}
    </section>
  );
}
