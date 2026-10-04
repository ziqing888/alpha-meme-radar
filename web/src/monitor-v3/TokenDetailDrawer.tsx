import { Copy, ExternalLink, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { AuditEvidencePanel } from "./AuditEvidencePanel";
import {
  monitorChainLabel,
  monitorContractExplorerUrl,
  monitorDexScreenerUrl,
} from "./CandidateTable";
import { EvidenceTimeline } from "./EvidenceTimeline";
import { FlowMiniChart } from "./FlowMiniChart";
import { formatUsd, UNAVAILABLE_LABEL } from "./formatters";
import { TokenIntelligenceDetail } from "./TokenIntelligenceDetail";
import { normalizeMonitorChain } from "./selectors";
import type { MonitorDisplayQuote, MonitorTokenIntelligence, MonitorTokenView } from "./types";

function copyAddress(address: string) {
  void navigator.clipboard?.writeText(address);
}

function isExpiredTrendWatch(token: MonitorTokenView): boolean {
  return (
    token.freshness.status === "fresh" &&
    !token.active_states.includes("blocked_risk") &&
    !token.active_states.includes("revival") &&
    (token.resonance.confirmation_gate?.failures ?? []).includes("discovery_window_expired") &&
    (
      token.resonance.subtype !== null ||
      token.wallet_evidence.verified_buyers > 0 ||
      token.active_states.includes("building")
    )
  );
}

interface TokenDetailDrawerProps {
  token: MonitorTokenView;
  imageUrl?: string;
  displayQuote?: MonitorDisplayQuote;
  onClose: () => void;
  onOpenMarket: (token: MonitorTokenView) => void;
  returnFocusTo: HTMLElement | null;
  intelligence?: MonitorTokenIntelligence | null;
}

export function TokenDetailDrawer({
  token,
  imageUrl,
  displayQuote,
  onClose,
  onOpenMarket,
  returnFocusTo,
  intelligence,
}: TokenDetailDrawerProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const symbol = token.identity.symbol ?? token.identity.name ?? "未知代币";
  const chainLabel = monitorChainLabel(token.identity.chain);
  const isArc = normalizeMonitorChain(token.identity.chain) === "arc";
  const explorerUrl = isArc ? monitorContractExplorerUrl(token) : null;
  const dexScreenerUrl = isArc ? monitorDexScreenerUrl(token) : null;
  const expiredTrendWatch = isExpiredTrendWatch(token);
  const candidateWallets = token.wallet_evidence.candidate_wallet_addresses ?? [];

  useEffect(() => {
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      returnFocusTo?.focus();
    };
  }, [onClose, returnFocusTo]);

  return (
    <div className="monitor-v3-drawer-layer">
      <button
        type="button"
        className="monitor-v3-drawer-backdrop"
        aria-label="关闭详情遮罩"
        onClick={onClose}
      />
      <aside
        className="monitor-v3-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`${symbol} 详情`}
      >
        <header className="monitor-v3-drawer-header">
          <div className="monitor-v3-drawer-identity">
            <span className="monitor-v3-drawer-avatar">
              <b>{symbol.slice(0, 2).toUpperCase()}</b>
              {imageUrl && (
                <img
                  src={imageUrl}
                  alt=""
                  referrerPolicy="no-referrer"
                  onError={(event) => {
                    event.currentTarget.style.display = "none";
                  }}
                />
              )}
            </span>
            <div>
              <span>{chainLabel} · {expiredTrendWatch ? "趋势观察" : token.primary_state}</span>
              <h2 title={symbol}>{symbol}</h2>
              {token.identity.name && token.identity.name !== token.identity.symbol && <p title={token.identity.name}>{token.identity.name}</p>}
            </div>
          </div>
          <div className="monitor-v3-drawer-actions">
            {explorerUrl && (
              <a
                href={explorerUrl}
                target="_blank"
                rel="noopener noreferrer"
                title="在 ARC 浏览器查看合约"
                aria-label="在 ARC 浏览器查看合约"
              >
                <ExternalLink size={16} aria-hidden="true" />
              </a>
            )}
            <button type="button" title="复制完整合约地址" aria-label="复制完整合约地址" onClick={() => copyAddress(token.identity.contract_address)}>
              <Copy size={16} aria-hidden="true" />
            </button>
            {dexScreenerUrl ? (
              <a
                href={dexScreenerUrl}
                target="_blank"
                rel="noopener noreferrer"
                title="在 DexScreener 打开"
                aria-label="在 DexScreener 打开"
              >
                <ExternalLink size={16} aria-hidden="true" />
              </a>
            ) : (
              <button type="button" title="在 GMGN 打开" aria-label="在 GMGN 打开" onClick={() => onOpenMarket(token)}>
                <ExternalLink size={16} aria-hidden="true" />
              </button>
            )}
            <button ref={closeButtonRef} type="button" title="关闭详情" aria-label="关闭详情" onClick={onClose}>
              <X size={17} aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="monitor-v3-drawer-body">
          <section className="monitor-v3-detail-summary" aria-label="代币身份与市场摘要">
            {explorerUrl ? (
              <a
                className="monitor-v3-contract-link"
                href={explorerUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                <code>{token.identity.contract_address}</code>
              </a>
            ) : (
              <code>{token.identity.contract_address}</code>
            )}
            <dl>
              <div><dt>当前市值</dt><dd>{formatUsd((displayQuote?.marketCap && displayQuote.marketCap > 0 ? displayQuote.marketCap : displayQuote?.fdv) || token.market.market_cap_usd)}</dd></div>
              <div><dt>流动性</dt><dd>{formatUsd((displayQuote?.liquidityUsd && displayQuote.liquidityUsd > 0 ? displayQuote.liquidityUsd : null) ?? token.market.liquidity_usd)}</dd></div>
              <div><dt>来源数</dt><dd>{token.resonance.provider_families.length || UNAVAILABLE_LABEL}</dd></div>
              <div><dt>赛道数</dt><dd>{token.resonance.signal_lanes.length || UNAVAILABLE_LABEL}</dd></div>
            </dl>
            {expiredTrendWatch && (
              <p className="monitor-v3-context-note">
                趋势观察：该币已经超过首发确认窗口，这里只表示热榜、资金或钱包再次共振；不属于聚合早鸟或聚合确认。
              </p>
            )}
          </section>

          <TokenIntelligenceDetail token={token} intelligence={intelligence} />

          <FlowMiniChart snapshots={token.market.snapshots} />
          <EvidenceTimeline events={token.events} stateHistory={token.state_history} />

          <section className="monitor-v3-detail-section" aria-labelledby="monitor-v3-wallet-title">
            <h3 id="monitor-v3-wallet-title">钱包级证据</h3>
            <div className="monitor-v3-wallet-groups">
              <div>
                <strong>核验买家 · {token.wallet_evidence.verified_buyers}</strong>
                {token.wallet_evidence.wallet_addresses.length > 0 ? token.wallet_evidence.wallet_addresses.map((address) => <code key={address}>{address}</code>) : <span className="monitor-v3-unavailable">不可用</span>}
              </div>
              <div className="is-platform">
                <strong>平台钱包 · {token.wallet_evidence.candidate_buyers ?? candidateWallets.length}</strong>
                {candidateWallets.length > 0 ? candidateWallets.map((address) => <code key={address}>{address}</code>) : <span className="monitor-v3-unavailable">未采集</span>}
              </div>
              <div className="is-conflicting">
                <strong><span>冲突钱包</span><span>{token.wallet_evidence.conflicting_wallets.length}</span></strong>
                {token.wallet_evidence.conflicting_wallets.length > 0 ? token.wallet_evidence.conflicting_wallets.map((address) => <code key={address}>{address}</code>) : <span>无已知冲突</span>}
              </div>
            </div>
          </section>

          <AuditEvidencePanel token={token} />

          <section className="monitor-v3-detail-section" aria-labelledby="monitor-v3-missing-title">
            <h3 id="monitor-v3-missing-title">缺失证据</h3>
            {token.missing_evidence.length > 0 ? (
              <ul className="monitor-v3-missing-list">{token.missing_evidence.map((field) => <li key={field}>{field}</li>)}</ul>
            ) : (
              <p>当前定义字段已采集</p>
            )}
          </section>
        </div>
      </aside>
    </div>
  );
}
