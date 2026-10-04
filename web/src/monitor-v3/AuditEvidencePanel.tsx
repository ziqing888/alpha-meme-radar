import { AlertTriangle, CircleHelp, FileSearch } from "lucide-react";

import { formatNullableNumber, UNAVAILABLE_LABEL } from "./formatters";
import type { AuditFact, MonitorTokenView } from "./types";

const STATUS_LABELS: Record<AuditFact["status"], string> = {
  known: "已采集",
  unknown: UNAVAILABLE_LABEL,
  stale: "证据陈旧",
  conflicting: "证据冲突",
};

const CONFIDENCE_LABELS: Record<AuditFact["confidence"], string> = {
  provider_reported: "来源报告",
  cross_checked: "交叉核验",
  locally_verified: "本地核验",
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return UNAVAILABLE_LABEL;
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function FactIcon({ fact }: { fact: AuditFact }) {
  if (fact.status === "conflicting" || fact.status === "stale") {
    return <AlertTriangle size={14} aria-hidden="true" />;
  }
  if (fact.status === "known") return <FileSearch size={14} aria-hidden="true" />;
  return <CircleHelp size={14} aria-hidden="true" />;
}

export function AuditEvidencePanel({ token }: { token: MonitorTokenView }) {
  return (
    <section className="monitor-v3-detail-section" aria-labelledby="monitor-v3-audit-title">
      <h3 id="monitor-v3-audit-title">合约、持仓与审计证据</h3>
      <dl className="monitor-v3-holder-checks">
        <div><dt>持有人</dt><dd>{formatNullableNumber(token.market.holders)}</dd></div>
        <div><dt>Top10 集中度</dt><dd>{token.market.top10_holder_pct === null ? UNAVAILABLE_LABEL : `${formatNullableNumber(token.market.top10_holder_pct)}%`}</dd></div>
        <div><dt>DEV 持仓</dt><dd>{token.market.dev_holder_pct === null ? UNAVAILABLE_LABEL : `${formatNullableNumber(token.market.dev_holder_pct)}%`}</dd></div>
        <div><dt>捆绑比例</dt><dd>{token.market.bundler_pct === null ? UNAVAILABLE_LABEL : `${formatNullableNumber(token.market.bundler_pct)}%`}</dd></div>
        <div><dt>狙击比例</dt><dd>{token.market.sniper_pct === null ? UNAVAILABLE_LABEL : `${formatNullableNumber(token.market.sniper_pct)}%`}</dd></div>
      </dl>
      {token.audit_facts.length === 0 ? (
        <p className="monitor-v3-unavailable">审计证据不可用</p>
      ) : (
        <ul className="monitor-v3-audit-list">
          {token.audit_facts.map((fact) => (
            <li key={fact.evidence_id} className={`is-${fact.status}`}>
              <FactIcon fact={fact} />
              <div>
                <strong>{fact.audit_field}: {formatValue(fact.value)}</strong>
                <span>{fact.provider_family.toUpperCase()} · {fact.upstream_provider}</span>
                <time dateTime={fact.observed_at}>{new Date(fact.observed_at).toLocaleString("zh-CN", { hour12: false })}</time>
              </div>
              <div className="monitor-v3-audit-status">
                <span>{STATUS_LABELS[fact.status]}</span>
                <small>{CONFIDENCE_LABELS[fact.confidence]}</small>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
