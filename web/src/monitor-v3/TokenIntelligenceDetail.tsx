import { ExternalLink } from "lucide-react";

import type {
  MonitorTokenIntelligence,
  MonitorTokenView,
  TokenIntelligenceItem,
} from "./types";

const STATUS_LABELS: Record<string, string> = {
  ready: "情报就绪",
  partial: "待补关键证据",
  pending: "AI 分析中",
  unavailable: "AI 尚未启动",
  available: "AI 证据已就绪",
};

function timeText(value: string | number | null | undefined): string {
  if (value == null || value === "") return "时间未记录";
  const numeric = typeof value === "number" ? value : /^\d+(\.\d+)?$/.test(value) ? Number(value) : NaN;
  const time = Number.isFinite(numeric)
    ? (numeric < 1e12 ? numeric * 1000 : numeric)
    : Date.parse(String(value));
  return Number.isFinite(time)
    ? new Date(time).toLocaleString("zh-CN", { hour12: false })
    : "时间未记录";
}

function itemText(item: TokenIntelligenceItem): string {
  if (typeof item === "string") return item.trim();
  const subject = item.address || item.wallet || item.title || item.label || item.name;
  const detail = item.text || item.reason || item.summary || item.evidence;
  const citations = item.evidence_ids?.length ? `证据 ${item.evidence_ids.join(" / ")}` : "";
  const observed = item.observed_at ? `观察于 ${timeText(item.observed_at)}` : "";
  return [subject, detail, item.source, citations, observed]
    .filter((value, index, values) => typeof value === "string" && value.trim() && values.indexOf(value) === index)
    .join(" · ");
}

function itemLines(items: TokenIntelligenceItem[] | undefined): string[] {
  return (items ?? []).map(itemText).filter(Boolean);
}

function identityStatus(status: string | undefined): string {
  return ({
    matched: "官方合约匹配",
    match: "官方合约匹配",
    verified: "官方合约匹配",
    mismatch: "官方合约不一致",
    alternate: "存在同名多合约",
    unknown: "尚无官方 CA 证据",
    unavailable: "官方 CA 来源未接入",
  } as Record<string, string>)[status ?? ""] ?? status ?? "尚无官方 CA 证据";
}

function safeUrl(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

interface TokenIntelligenceDetailProps {
  token: MonitorTokenView;
  intelligence?: MonitorTokenIntelligence | null;
}

export function TokenIntelligenceDetail({ token, intelligence }: TokenIntelligenceDetailProps) {
  const status = intelligence?.status ?? token.ai.status;
  const judgement = intelligence?.one_line_judgement;
  const narrative = intelligence?.ai_narrative || intelligence?.project_narrative;
  const attention = itemLines(intelligence?.attention_evidence);
  const wallets = itemLines(intelligence?.smart_wallets);
  const risks = itemLines(intelligence?.ai_risk_flags).length
    ? itemLines(intelligence?.ai_risk_flags)
    : itemLines(intelligence?.risks);
  const missing = itemLines(intelligence?.missing_evidence);
  const alternateContracts = itemLines(intelligence?.identity?.alternate_contracts);
  const sourceUrls = new Set<string>();
  const sources = [
    ...(intelligence?.sources ?? []).map((source) => ({
      title: source.title || source.source_id || "外部证据",
      detail: [source.source_id, timeText(source.observed_at)].filter(Boolean).join(" · "),
      url: safeUrl(source.url),
    })),
    ...(intelligence?.evidence_urls ?? []).map((url) => ({
      title: "AI 引用证据",
      detail: timeText(intelligence?.ai_analyzed_at),
      url: safeUrl(url),
    })),
  ].filter((source) => {
    if (!source.url || sourceUrls.has(source.url)) return false;
    sourceUrls.add(source.url);
    return true;
  });
  const officialStatus = intelligence?.official_ca_status || intelligence?.identity?.official_status;
  const analyzedAt = intelligence?.ai_analyzed_at || intelligence?.generated_at || token.ai.analyzed_at;

  return (
    <section className={`monitor-v3-intelligence is-${status}`} aria-labelledby="monitor-v3-intelligence-title">
      <header>
        <div>
          <h3 id="monitor-v3-intelligence-title">详情分析与 AI</h3>
          <span>{STATUS_LABELS[status] ?? status}</span>
        </div>
        <time>{intelligence?.ai_analyzed_at ? "AI 分析" : "证据更新"} · {timeText(analyzedAt)}</time>
      </header>

      <div className="monitor-v3-intelligence-judgement">
        <span>一句话判断</span>
        <strong>{judgement || "完整判断尚未生成"}</strong>
      </div>

      <div className="monitor-v3-intelligence-grid">
        <section>
          <h4>项目叙事</h4>
          <p>{narrative || "AI 正文尚未生成；当前只保留已采集的结构化证据。"}</p>
          {attention.length > 0 ? <ul>{attention.map((line) => <li key={line}>{line}</li>)}</ul> : <small>注意力与社交证据未采集</small>}
        </section>

        <section>
          <h4>聪明钱证据</h4>
          {wallets.length > 0 ? <ul>{wallets.map((line) => <li key={line}>{line}</li>)}</ul> : (
            <p>{token.wallet_evidence.verified_buyers > 0
              ? `V3 已核验 ${token.wallet_evidence.verified_buyers} 个独立买家，详细钱包叙事待补。`
              : (token.wallet_evidence.candidate_buyers ?? 0) > 0
                ? `平台钱包候选 ${token.wallet_evidence.candidate_buyers} 个，尚未通过收益历史核验。`
                : "未采集到可核验的聪明钱证据。"}</p>
          )}
        </section>

        <section>
          <h4>官方身份与多 CA</h4>
          <strong>{identityStatus(officialStatus)}</strong>
          <dl className="monitor-v3-intelligence-signals">
            <div className={intelligence?.ca_conflict ? "is-danger" : ""}><dt>CA</dt><dd>{intelligence?.ca_conflict == null ? "待核验" : intelligence.ca_conflict ? "存在冲突" : "未见冲突"}</dd></div>
            <div><dt>社交来源</dt><dd>{intelligence?.social_source_count == null ? "未采集" : intelligence.social_source_count}</dd></div>
            <div><dt>刷量评分</dt><dd>{intelligence?.wash_score == null ? "未计算" : `${intelligence.wash_score.toFixed(0)} 分`}</dd></div>
            <div><dt>资金流加速度</dt><dd>{intelligence?.flow_acceleration == null ? "未计算" : `${intelligence.flow_acceleration.toFixed(2)}x`}</dd></div>
          </dl>
          {intelligence?.identity?.official_contract ? <code>{intelligence.identity.official_contract}</code> : <small>官方合约待确认</small>}
          {alternateContracts.length > 0 && <div className="monitor-v3-intelligence-alternates"><small>其他合约</small>{alternateContracts.map((address) => <code key={address}>{address}</code>)}</div>}
        </section>

        <section className={risks.length > 0 ? "is-risk" : ""}>
          <h4>风险与缺失证据</h4>
          {risks.length > 0 ? <ul>{risks.map((line) => <li key={line}>{line}</li>)}</ul> : <small>暂无已记录风险结论</small>}
          {missing.length > 0 && <div className="monitor-v3-intelligence-missing"><small>仍缺</small>{missing.map((line) => <span key={line}>{line}</span>)}</div>}
        </section>
      </div>

      <footer>
        <strong>证据来源</strong>
        {sources.length > 0 ? sources.map((source) => (
          <a key={source.url} href={source.url} target="_blank" rel="noreferrer">
            <span>{source.title}<small>{source.detail}</small></span>
            <ExternalLink size={13} aria-hidden="true" />
          </a>
        )) : <small>暂无可验证外部链接</small>}
      </footer>
    </section>
  );
}
