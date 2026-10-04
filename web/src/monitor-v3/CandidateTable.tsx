import { Fragment } from "react";
import { AlertTriangle, Copy, ExternalLink, Radio } from "lucide-react";

import { formatNullableNumber, formatUsd } from "./formatters";
import { tokenIntelligenceKey } from "./intelligence";
import { MONITOR_CHAINS, normalizeMonitorChain, type MonitorChain } from "./selectors";
import type { MonitorDiscoveryBaseline, MonitorDisplayQuote, MonitorState, MonitorTokenIntelligence, MonitorTokenView, ResonanceSubtype } from "./types";

const EMPTY_VALUE = "—";
const UNKNOWN_VALUE = "未知";

const CHAIN_LINK_POLICIES: Record<MonitorChain, { explorerBaseUrl: string; dexscreenerSlug: string }> = {
  bsc: { explorerBaseUrl: "https://bscscan.com", dexscreenerSlug: "bsc" },
  robinhood: {
    explorerBaseUrl: "https://explorer.testnet.chain.robinhood.com",
    dexscreenerSlug: "robinhood",
  },
  arc: { explorerBaseUrl: "https://explorer.arc.io", dexscreenerSlug: "arc" },
};

function monitorChain(value: unknown): MonitorChain | null {
  const chain = normalizeMonitorChain(value);
  return chain in MONITOR_CHAINS ? chain as MonitorChain : null;
}

function validEvmAddress(value: string): boolean {
  return /^0x[0-9a-fA-F]{40}$/.test(value);
}

export function monitorContractExplorerUrl(row: MonitorTokenView): string | null {
  const chain = monitorChain(row.identity.chain);
  const address = row.identity.contract_address.trim();
  if (chain === null || !validEvmAddress(address)) return null;
  return `${CHAIN_LINK_POLICIES[chain].explorerBaseUrl}/address/${encodeURIComponent(address)}`;
}

function safePairSpecificDexUrl(row: MonitorTokenView, chain: MonitorChain): string | null {
  const slug = CHAIN_LINK_POLICIES[chain].dexscreenerSlug;
  for (const event of row.events) {
    if (!event.source_url) continue;
    try {
      const url = new URL(event.source_url);
      const path = url.pathname.split("/").filter(Boolean);
      if (
        url.protocol === "https:" &&
        (url.hostname === "dexscreener.com" || url.hostname === "www.dexscreener.com") &&
        path.length >= 2 &&
        path[0].toLowerCase() === slug &&
        /^[a-zA-Z0-9_-]+$/.test(path[1])
      ) {
        return url.href;
      }
    } catch {
      // Ignore malformed upstream evidence URLs and use the contract fallback.
    }
  }
  return null;
}

export function monitorDexScreenerUrl(row: MonitorTokenView): string | null {
  const chain = monitorChain(row.identity.chain);
  const address = row.identity.contract_address.trim();
  if (chain === null || !validEvmAddress(address)) return null;
  return safePairSpecificDexUrl(row, chain)
    ?? `https://dexscreener.com/${CHAIN_LINK_POLICIES[chain].dexscreenerSlug}/${encodeURIComponent(address)}`;
}

export function monitorChainLabel(value: unknown): string {
  const chain = monitorChain(value);
  return chain === null ? String(value ?? "").trim().toUpperCase() : MONITOR_CHAINS[chain].label;
}

type DisplayState = MonitorState | "trend_watch";

const STATE_LABELS: Record<DisplayState, string> = {
  new: "新发现",
  building: "聚合早鸟",
  resonating: "聚合确认",
  smart_cluster: "聪明钱",
  trend_watch: "趋势观察",
  cooling: "冷却",
  revival: "老币复活",
  blocked_risk: "淘汰",
  stale: "行情陈旧",
};

const RESONANCE_LABELS: Record<ResonanceSubtype, string> = {
  platform_stack: "平台叠加",
  cross_provider: "跨源共振",
  persistent: "持续确认",
  full: "完整共振",
};

const FLOW_LABELS: Record<MonitorTokenView["ranking_axes"]["flow"]["direction"], string> = {
  accelerating: "资金加速",
  decaying: "资金衰减",
  flat: "资金平稳",
  unknown: "资金待补",
};

const LANE_LABELS: Record<string, string> = {
  new_launch: "首发",
  trending: "热榜",
  hot_search: "热搜",
  smart_money: "聪明钱",
  kol_social: "KOL / 社交",
  market_flow: "资金流",
  security_audit: "风险审计",
};

const RISK_LABELS: Record<string, string> = {
  confirmed_honeypot: "确认蜜罐",
  official_ca_conflict: "官方 CA 冲突",
  liquidity_removed: "流动性移除",
  sell_simulation_failed: "卖出模拟失败",
  unlocked_lp: "LP 未锁定",
  wash_pattern: "疑似刷量",
  wrong_chain: "链不匹配",
  no_usable_pool: "无可用池",
  stale_evidence: "证据陈旧",
};

function formatAge(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return EMPTY_VALUE;
  const safeSeconds = Math.max(0, Math.round(seconds));
  if (safeSeconds < 60) return `${safeSeconds}秒`;
  if (safeSeconds < 3600) return `${Math.floor(safeSeconds / 60)}分`;
  if (safeSeconds < 86_400) {
    const hours = Math.floor(safeSeconds / 3600);
    const minutes = Math.floor((safeSeconds % 3600) / 60);
    return minutes > 0 ? `${hours}时${minutes}分` : `${hours}时`;
  }
  const days = Math.floor(safeSeconds / 86_400);
  const hours = Math.floor((safeSeconds % 86_400) / 3600);
  return hours > 0 ? `${days}天${hours}时` : `${days}天`;
}

function formatPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return EMPTY_VALUE;
  return `${formatNullableNumber(value, { maximumFractionDigits: 2 })}%`;
}

function formatPrice(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return EMPTY_VALUE;
  if (value > 0 && value < 0.000000000001) return "<$0.000000000001";
  return formatNullableNumber(value, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: value < 0.000001 ? 12 : value < 1 ? 8 : 2,
  });
}

function positiveValue(value: number | null | undefined): number | null {
  return value !== null && value !== undefined && Number.isFinite(value) && value > 0 ? value : null;
}

function firstPositiveValue(
  row: MonitorTokenView,
  field: "price_usd" | "market_cap_usd",
): number | null {
  for (const snapshot of row.market.snapshots) {
    const value = positiveValue(snapshot[field]);
    if (value !== null) return value;
  }
  return null;
}

function peakPositiveValue(
  row: MonitorTokenView,
  field: "price_usd" | "market_cap_usd",
): number | null {
  const values = row.market.snapshots
    .map((snapshot) => positiveValue(snapshot[field]))
    .filter((value): value is number => value !== null);
  return values.length > 0 ? Math.max(...values) : null;
}

function changeSinceDiscovery(first: number | null, current: number | null): number | null {
  if (first === null || current === null || first <= 0) return null;
  return ((current / first) - 1) * 100;
}

function formatChange(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return EMPTY_VALUE;
  return `${value > 0 ? "+" : ""}${formatNullableNumber(value, { maximumFractionDigits: 2 })}%`;
}

function quoteSourceLabel(quote: MonitorDisplayQuote | undefined): string {
  if (!quote) return "监控快照";
  if (!Number.isFinite(quote.updatedAt)) return "DS 实时";
  const ageSeconds = Math.max(0, (Date.now() - Number(quote.updatedAt)) / 1000);
  return `DS ${formatAge(ageSeconds)}前`;
}

function compactAddress(address: string): string {
  return address.length > 16 ? `${address.slice(0, 7)}…${address.slice(-5)}` : address;
}

function riskLabel(value: string): string {
  return RISK_LABELS[value] ?? value.replace(/_/g, " ");
}

function sourceLabel(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (normalized === "gmgn") return "GMGN";
  if (normalized === "okx") return "OKX";
  if (normalized === "onchain") return "链上";
  if (normalized === "985") return "985";
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function initials(symbol: string): string {
  return Array.from(symbol.trim()).slice(0, 2).join("").toUpperCase() || "?";
}

function avatarHue(seed: string): number {
  return Array.from(seed).reduce((total, character) => total + character.charCodeAt(0), 0) % 360;
}

function isExpiredTrendWatch(row: MonitorTokenView): boolean {
  return (
    row.freshness.status === "fresh" &&
    !row.active_states.includes("blocked_risk") &&
    !row.active_states.includes("revival") &&
    (row.resonance.confirmation_gate?.failures ?? []).includes("discovery_window_expired") &&
    (
      row.resonance.subtype !== null ||
      row.wallet_evidence.verified_buyers > 0 ||
      row.active_states.includes("building")
    )
  );
}

function walletSignalLabel(row: MonitorTokenView): string | null {
  const verified = Math.max(0, Math.floor(row.wallet_evidence.verified_buyers ?? 0));
  const candidates = Math.max(
    0,
    Math.floor(
      row.wallet_evidence.candidate_buyers
        ?? row.wallet_evidence.candidate_wallet_addresses?.length
        ?? 0,
    ),
  );
  if (candidates > verified) return `平台钱包 ${candidates} · 已核验 ${verified}`;
  if (verified > 0) return `已核验钱包 ${verified}`;
  return null;
}

function mainState(row: MonitorTokenView): DisplayState {
  if (isExpiredTrendWatch(row)) return "trend_watch";
  return row.primary_state;
}

function intelligenceLabel(row: MonitorTokenView, intelligence?: MonitorTokenIntelligence): string {
  if (intelligence?.ai_analyzed_at) return "AI 已完成";
  if (intelligence?.status === "pending") return "AI 分析中";
  if (intelligence?.status === "unavailable") return "AI 未启动";
  if (intelligence) return "情报已整理";
  if (row.ai.status === "available") return "AI 已完成";
  if (row.ai.status === "pending") return "AI 分析中";
  return "AI 未启动";
}

function intelligenceItemText(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  for (const field of ["text", "summary", "reason", "label", "evidence"]) {
    if (typeof item[field] === "string" && item[field].trim()) return item[field].trim();
  }
  return null;
}

function holderFreshnessLabel(row: MonitorTokenView, nowMs: number): string {
  const source = row.market.field_sources?.holders;
  const observedAt = row.market.field_observed_at?.holders;
  const stamp = observedAt ? Date.parse(observedAt) : Number.NaN;
  const sourceText = source?.toLowerCase() === "gmgn" ? "GMGN" : source ? sourceLabel(source) : "持仓快照";
  if (!Number.isFinite(stamp)) return `${sourceText} 时间未知`;
  return `${sourceText} ${formatAge(Math.max(0, (nowMs - stamp) / 1000))}前`;
}

interface CandidateTableProps {
  rows: MonitorTokenView[];
  onSelect: (row: MonitorTokenView) => void;
  onOpenMarket?: (row: MonitorTokenView) => void;
  imageByToken?: Record<string, string>;
  quoteByToken?: Record<string, MonitorDisplayQuote>;
  baselineByToken?: Record<string, MonitorDiscoveryBaseline>;
  intelligenceByToken?: Record<string, MonitorTokenIntelligence>;
  stageTimingByToken?: Record<string, MonitorStageTiming>;
  emptyMessage?: string;
  nowMs?: number;
  showEvidence?: boolean;
  ariaLabel?: string;
}

export interface MonitorStageTiming {
  earlySeconds: number | null;
  confirmedSeconds: number | null;
}

function stageTimingLabel(timing: MonitorStageTiming | undefined): string {
  if (!timing) return "早鸟未记录 · 确认未记录";
  const early = timing.earlySeconds === null ? "早鸟未记录" : `早鸟 ${timing.earlySeconds}秒`;
  const confirmed = timing.confirmedSeconds === null ? "确认未记录" : `确认 ${timing.confirmedSeconds}秒`;
  return `${early} · ${confirmed}`;
}

export function CandidateTable({ rows, onSelect, onOpenMarket, imageByToken = {}, quoteByToken = {}, baselineByToken = {}, intelligenceByToken = {}, stageTimingByToken = {}, emptyMessage, nowMs = Date.now(), showEvidence = false, ariaLabel = "MEME V3 实时候选" }: CandidateTableProps) {
  if (rows.length === 0) {
    return (
      <div className="monitor-v3-empty" role="status">
        <Radio size={18} aria-hidden="true" />
        <strong>当前视图暂无候选</strong>
        <span>{emptyMessage ?? "该状态当前没有匹配事件"}</span>
      </div>
    );
  }

  return (
    <div className="monitor-v3-table-scroll" data-overflow-axis="responsive">
      <table className="monitor-v3-table" aria-label={ariaLabel}>
        <colgroup>
          <col className="col-token" />
          <col className="col-current" />
          <col className="col-discovery" />
          <col className="col-pool" />
          <col className="col-volume" />
          <col className="col-holders" />
          <col className="col-decision" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">排名 / 币种</th>
            <th scope="col">当前市值 / 发现后</th>
            <th scope="col">首发 / 最高</th>
            <th scope="col">池子</th>
            <th scope="col">24h 成交</th>
            <th scope="col">持有人 / Top10</th>
            <th scope="col">判断 / 风险</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const symbol = row.identity.symbol ?? row.identity.name ?? "未知代币";
            const chain = monitorChain(row.identity.chain);
            const chainLabel = monitorChainLabel(row.identity.chain);
            const isArc = chain === "arc";
            const explorerUrl = isArc ? monitorContractExplorerUrl(row) : null;
            const dexScreenerUrl = isArc ? monitorDexScreenerUrl(row) : null;
            const quote = quoteByToken[row.id];
            const baseline = baselineByToken[row.id];
            const currentPrice = positiveValue(quote?.priceUsd) ?? positiveValue(row.market.price_usd);
            const currentCap = positiveValue(quote?.marketCap) ?? positiveValue(quote?.fdv) ?? positiveValue(row.market.market_cap_usd);
            const currentLiquidity = positiveValue(quote?.liquidityUsd) ?? positiveValue(row.market.liquidity_usd);
            const currentVolume = positiveValue(quote?.volume24h) ?? positiveValue(row.market.volume_24h_usd);
            const holders = positiveValue(row.market.holders);
            const firstPrice = positiveValue(baseline?.first_price_usd) ?? firstPositiveValue(row, "price_usd");
            const firstCap = positiveValue(baseline?.first_market_cap_usd) ?? firstPositiveValue(row, "market_cap_usd");
            const historicalPeakCap = peakPositiveValue(row, "market_cap_usd");
            const baselinePeakCap = positiveValue(baseline?.peak_market_cap_usd);
            const peakCandidates = [currentCap, historicalPeakCap, baselinePeakCap]
              .filter((value): value is number => value !== null);
            const peakCap = peakCandidates.length > 0 ? Math.max(...peakCandidates) : null;
            const poolAge = row.ranking_axes.timing.real_age_seconds
              ?? row.ranking_axes.timing.pair_age_seconds;
            const visibleAge = poolAge ?? row.ranking_axes.timing.first_seen_age_seconds;
            const ageLabel = poolAge === null ? "发现" : "池龄";
            const change = firstCap !== null && currentCap !== null
              ? changeSinceDiscovery(firstCap, currentCap)
              : changeSinceDiscovery(firstPrice, currentPrice);
            const state = mainState(row);
            const riskFlags = [...row.risk.hard_failures, ...row.risk.soft_flags];
            const risk = riskFlags[0];
            const imageUrl = imageByToken[row.id];
            const intelligence = intelligenceByToken[tokenIntelligenceKey(
              row.identity.chain,
              row.identity.contract_address,
            )];
            const aiJudgement = intelligence?.ai_analyzed_at
              ? intelligence.one_line_judgement?.trim() || null
              : null;
            const aiRisk = (intelligence?.ai_risk_flags ?? intelligence?.risks ?? [])
              .map(intelligenceItemText)
              .find((value): value is string => Boolean(value));
            const primaryRisk = aiRisk ?? (risk ? riskLabel(risk) : null);
            const hue = avatarHue(row.id);
            const sourceLabels = row.resonance.provider_families.map(sourceLabel);
            const sourceText = sourceLabels.slice(0, 3).join(" + ");
            const evidenceSourceText = sourceLabels.join(" + ");
            const laneText = row.resonance.signal_lanes
              .map((lane) => LANE_LABELS[lane] ?? lane)
              .join(" / ");
            const walletText = walletSignalLabel(row);
            return (
              <Fragment key={row.id}>
                <tr
                  tabIndex={0}
                  data-monitor-token-id={row.id}
                  aria-label={`查看 ${symbol} 详情`}
                  className={row.risk.hard_blocked ? "is-blocked" : undefined}
                  onClick={() => onSelect(row)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(row);
                    }
                  }}
                >
                <td data-label="币种">
                  <div className="monitor-v3-token-row">
                    <span className="monitor-v3-rank">#{index + 1}</span>
                    <span className="monitor-v3-avatar" style={{ background: `hsl(${hue} 55% 28%)` }}>
                      <span>{initials(symbol)}</span>
                      {imageUrl && (
                        <img
                          src={imageUrl}
                          alt=""
                          loading="lazy"
                          referrerPolicy="no-referrer"
                          onError={(event) => {
                            event.currentTarget.style.display = "none";
                          }}
                        />
                      )}
                    </span>
                    <span className="monitor-v3-token-copy">
                      <span><strong title={symbol}>{symbol}</strong><em className={`is-chain-${chain ?? "unknown"}`}>{chainLabel}</em></span>
                      <small title={row.identity.name && row.identity.name !== symbol ? row.identity.name : row.identity.contract_address}>{row.identity.name && row.identity.name !== symbol ? row.identity.name : compactAddress(row.identity.contract_address)}</small>
                    </span>
                    <span className="monitor-v3-token-actions">
                      {explorerUrl && (
                        <a
                          href={explorerUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          title="在 ARC 浏览器查看合约"
                          aria-label={`在 ARC 浏览器查看 ${symbol} 合约`}
                          onClick={(event) => event.stopPropagation()}
                        >
                          <ExternalLink size={14} aria-hidden="true" />
                        </a>
                      )}
                      <button
                        className="monitor-v3-copy-button"
                        type="button"
                        title="复制完整合约地址"
                        aria-label={`复制完整合约地址 ${row.identity.contract_address}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          void navigator.clipboard?.writeText(row.identity.contract_address);
                        }}
                      >
                        <Copy size={14} aria-hidden="true" />
                      </button>
                    </span>
                  </div>
                </td>
                <td data-label="当前市值">
                  <div className="monitor-v3-primary-value">
                    <strong>{currentCap === null ? "未采集" : formatUsd(currentCap)}</strong>
                    <small className={change === null ? "is-muted" : change >= 0 ? "is-up" : "is-down"}>
                      <span>发现后</span> {change === null ? "待基准" : formatChange(change)}
                    </small>
                    <small className="monitor-v3-secondary-price">价格 {formatPrice(currentPrice)} · {quoteSourceLabel(quote)}</small>
                  </div>
                </td>
                <td data-label="首发 / 最高">
                  <div className="monitor-v3-primary-value">
                    <strong>{firstCap === null ? "待基准" : formatUsd(firstCap)}</strong>
                    <small>最高 {peakCap === null ? "未采集" : formatUsd(peakCap)}</small>
                  </div>
                </td>
                <td data-label="池子">
                  <div className="monitor-v3-primary-value">
                    <strong>{currentLiquidity === null ? "未采集" : formatUsd(currentLiquidity)}</strong>
                    <small>{ageLabel} {formatAge(visibleAge)}</small>
                  </div>
                </td>
                <td data-label="24h 成交">
                  <div className="monitor-v3-primary-value">
                    <strong>{currentVolume === null ? "未采集" : formatUsd(currentVolume)}</strong>
                    <small className={`is-flow-${row.ranking_axes.flow.direction}`}>{FLOW_LABELS[row.ranking_axes.flow.direction]}</small>
                  </div>
                </td>
                <td data-label="持有人 / Top10">
                  <div className="monitor-v3-primary-value">
                    <strong>{holders === null ? UNKNOWN_VALUE : formatNullableNumber(holders)}</strong>
                    <small>Top10 {positiveValue(row.market.top10_holder_pct) === null ? UNKNOWN_VALUE : formatPercent(row.market.top10_holder_pct)}</small>
                    {holders !== null && <small>{holderFreshnessLabel(row, nowMs)}</small>}
                  </div>
                </td>
                <td data-label="判断 / 风险">
                  <div className="monitor-v3-decision-cell">
                    <div className="monitor-v3-decision-heading">
                      <span className={`monitor-v3-stage is-state-${state}`}>{STATE_LABELS[state]}</span>
                      <strong>{row.resonance.subtype ? RESONANCE_LABELS[row.resonance.subtype] : `${row.resonance.provider_families.length}源 · ${row.resonance.signal_lanes.length}路`}</strong>
                    </div>
                    <small>
                      {isExpiredTrendWatch(row)
                        ? "超过首发窗口，不进早鸟/确认"
                        : sourceText || "来源核验中"}
                    </small>
                    {walletText && <small className="monitor-v3-wallet-line">{walletText}</small>}
                    {!showEvidence && aiJudgement && <small className="monitor-v3-ai-judgement">{aiJudgement}</small>}
                    {!showEvidence && (primaryRisk || row.risk.status !== "unknown") && <span className={`monitor-v3-risk-line ${row.risk.hard_blocked ? "is-danger" : primaryRisk ? "is-warning" : "is-clear"}`}>
                      {row.risk.hard_blocked && <AlertTriangle size={13} aria-hidden="true" />}
                      {primaryRisk || "未见硬风险"}
                    </span>}
                    <div className="monitor-v3-decision-footer">
                      <small>{intelligenceLabel(row, intelligence)}</small>
                      {dexScreenerUrl ? (
                        <a
                          href={dexScreenerUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          title="在 DexScreener 打开"
                          aria-label={`在 DexScreener 打开 ${symbol}`}
                          onClick={(event) => event.stopPropagation()}
                        >
                          <ExternalLink size={14} aria-hidden="true" />
                        </a>
                      ) : onOpenMarket ? (
                        <button
                          type="button"
                          title="在 GMGN 打开"
                          aria-label={`在 GMGN 打开 ${symbol}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenMarket(row);
                          }}
                        >
                          <ExternalLink size={14} aria-hidden="true" />
                        </button>
                      ) : null}
                    </div>
                  </div>
                </td>
                </tr>
                {showEvidence && (
                  <tr className="monitor-v3-evidence-row" onClick={() => onSelect(row)}>
                    <td colSpan={7}>
                      <div className="monitor-v3-evidence-strip">
                        <div className="is-ai">
                          <span>AI 判断</span>
                          <strong>{aiJudgement ?? (intelligenceLabel(row, intelligence) === "AI 分析中" ? "分析进行中，不阻塞阶段展示" : "等待精选阶段分析")}</strong>
                        </div>
                        <div>
                          <span>共振证据</span>
                          <strong>{evidenceSourceText || "来源核验中"}</strong>
                          <small>{laneText || walletText || "信号路径整理中"}</small>
                        </div>
                        <div className={row.risk.hard_blocked ? "is-danger" : primaryRisk ? "is-warning" : "is-clear"}>
                          <span>主要风险</span>
                          <strong>{primaryRisk ?? (row.risk.status === "known" ? "未见硬风险" : "风险数据更新中")}</strong>
                        </div>
                        <div className="is-timing">
                          <span>发现→阶段</span>
                          <strong>{stageTimingLabel(stageTimingByToken[row.id])}</strong>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
