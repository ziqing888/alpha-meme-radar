import { AlertTriangle, ShieldX } from "lucide-react";

import { normalizeMonitorChain, type ChainFilter } from "./selectors";
import type { MonitorRejection, MonitorTokenView, MonitorV3Snapshot } from "./types";

const REASON_LABELS: Record<string, string> = {
  invalid_address: "地址无效",
  wrong_chain: "链不匹配",
  no_usable_pool: "无可用池",
  sell_simulation_failed: "卖出模拟失败",
  liquidity_removed: "流动性移除",
  official_ca_conflict: "官方 CA 冲突",
  wash_pattern: "疑似刷量",
  stale_evidence: "证据陈旧",
  confirmed_honeypot: "确认蜜罐",
};

const KILL_REASONS = [
  "invalid_address",
  "wrong_chain",
  "no_usable_pool",
  "sell_simulation_failed",
  "liquidity_removed",
  "official_ca_conflict",
  "wash_pattern",
  "stale_evidence",
] as const;

function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason.replace(/_/g, " ");
}

function matchesQuery(rejection: MonitorRejection, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [rejection.reason, rejection.source, rejection.chain, rejection.contract_address].some(
    (value) => value.toLowerCase().includes(normalized),
  );
}

function matchesChain(chain: string, chainFilter: ChainFilter): boolean {
  return chainFilter === "all" || normalizeMonitorChain(chain) === chainFilter;
}

interface RiskAuditViewProps {
  snapshot: MonitorV3Snapshot;
  blockedRows: MonitorTokenView[];
  chainFilter: ChainFilter;
  query: string;
  onSelect: (token: MonitorTokenView) => void;
}

interface PublishedKillCounts {
  by_reason?: Record<string, number>;
}

export function RiskAuditView({ snapshot, blockedRows, chainFilter, query, onSelect }: RiskAuditViewProps) {
  const chainRejections = snapshot.rejections.filter((rejection) =>
    matchesChain(rejection.chain, chainFilter),
  );
  const rejections = chainRejections.filter((rejection) => matchesQuery(rejection, query));
  const chainTokens = snapshot.tokens.filter((token) =>
    matchesChain(token.identity.chain, chainFilter),
  );
  const publishedKillCounts = (snapshot as MonitorV3Snapshot & { kill_counts?: PublishedKillCounts })
    .kill_counts?.by_reason;
  const counts = Object.fromEntries(
    KILL_REASONS.map((reason) => [
      reason,
      chainFilter === "all" && typeof publishedKillCounts?.[reason] === "number"
        ? publishedKillCounts[reason]
        : chainRejections.filter((rejection) => rejection.reason === reason).length +
          chainTokens.filter((token) =>
            [...token.risk.hard_failures, ...token.risk.soft_flags].includes(reason),
          ).length,
    ]),
  ) as Record<(typeof KILL_REASONS)[number], number>;

  return (
    <section className="monitor-v3-risk-view" aria-label="淘汰复盘审计">
      <header className="monitor-v3-risk-heading">
        <div>
          <ShieldX size={18} aria-hidden="true" />
          <div><h2>淘汰统计</h2><p>结构风险保留原始原因、来源与时间</p></div>
        </div>
        <strong>{blockedRows.length + chainRejections.length} 条</strong>
      </header>
      <dl className="monitor-v3-kill-counts">
        {KILL_REASONS.map((reason) => (
          <div key={reason}>
            <dt>{reasonLabel(reason)}</dt>
            <dd>{counts[reason]}</dd>
          </div>
        ))}
      </dl>

      <div className="monitor-v3-risk-table-scroll" data-overflow-axis="horizontal">
        <table className="monitor-v3-risk-table" aria-label="淘汰复盘明细">
          <thead><tr><th>类型</th><th>代币 / CA</th><th>链</th><th>原因</th><th>来源</th><th>时间</th></tr></thead>
          <tbody>
            {blockedRows.map((token) => {
              const symbol = token.identity.symbol ?? token.identity.name ?? "未知代币";
              return (
                <tr
                  key={`blocked-${token.id}`}
                  tabIndex={0}
                  data-monitor-token-id={token.id}
                  aria-label={`查看 ${symbol} 风险详情`}
                  onClick={() => onSelect(token)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(token);
                    }
                  }}
                >
                  <td><span className="monitor-v3-badge is-danger"><AlertTriangle size={11} aria-hidden="true" />已阻断</span></td>
                  <td><strong>{symbol}</strong><code>{token.identity.contract_address}</code></td>
                  <td>{token.identity.chain.toUpperCase()}</td>
                  <td>{token.risk.hard_failures.map(reasonLabel).join(" · ") || "结构风险"}</td>
                  <td>{token.audit_facts.map((fact) => fact.provider_family.toUpperCase()).filter((value, index, all) => all.indexOf(value) === index).join(" · ") || "不可用"}</td>
                  <td><time dateTime={token.state_updated_at}>{new Date(token.state_updated_at).toLocaleString("zh-CN", { hour12: false })}</time></td>
                </tr>
              );
            })}
            {rejections.map((rejection) => (
              <tr key={`rejection-${rejection.raw_fingerprint}`}>
                <td><span className="monitor-v3-badge is-warning">原始淘汰</span></td>
                <td><code>{rejection.contract_address}</code></td>
                <td>{rejection.chain.toUpperCase()}</td>
                <td>{reasonLabel(rejection.reason)}</td>
                <td>{rejection.source}</td>
                <td><time dateTime={rejection.observed_at}>{new Date(rejection.observed_at).toLocaleString("zh-CN", { hour12: false })}</time></td>
              </tr>
            ))}
            {blockedRows.length === 0 && rejections.length === 0 && (
              <tr><td colSpan={6} className="monitor-v3-risk-empty">当前没有匹配的淘汰记录</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
