export type MonitorTab =
  | "focus"
  | "all"
  | "new"
  | "selected"
  | "building"
  | "resonating"
  | "smart"
  | "trend"
  | "revival"
  | "risk";

export type MonitorState =
  | "new"
  | "building"
  | "resonating"
  | "smart_cluster"
  | "cooling"
  | "trend_watch"
  | "revival"
  | "blocked_risk"
  | "stale";

export type EvidenceRole =
  | "discovery"
  | "ranking"
  | "wallet"
  | "market"
  | "audit"
  | "enrichment";

export type SignalLane =
  | "new_launch"
  | "trending"
  | "hot_search"
  | "smart_money"
  | "kol_social"
  | "market_flow"
  | "security_audit";

export type AuditStatus = "known" | "unknown" | "stale" | "conflicting";
export type AuditConfidence = "provider_reported" | "cross_checked" | "locally_verified";
export type MonitorAvailability = "available" | "unavailable";
export type KnowledgeStatus = "known" | "unknown";

export interface AuditFact {
  audit_field: string;
  value: unknown;
  status: AuditStatus;
  provider_family: string;
  upstream_provider: string;
  observed_at: string;
  confidence: AuditConfidence;
  evidence_id: string;
}

export interface MonitorEvent {
  schema_version: 1;
  event_id: string;
  chain: string;
  contract_address: string;
  event_type: string;
  event_at: string;
  observed_at: string;
  provider_family: string;
  provider_feed: string;
  evidence_role: EvidenceRole;
  signal_lane: SignalLane;
  provider_event_id: string;
  source_url: string | null;
  raw_fingerprint: string;
  upstream_provider: string;
  event_time_basis: string;
  counts_for_resonance: boolean;
  wallet_address?: string;
  direction?: string;
  wallet_verified?: boolean;
  amount_usd?: unknown;
}

export interface MonitorTokenIdentity {
  chain: string;
  contract_address: string;
  symbol: string | null;
  name: string | null;
  creator: string | null;
  launchpad: string | null;
  real_created_at: string | null;
  real_created_source: string | null;
  first_seen_at: string;
  first_seen_source: string | null;
  pair_created_at: string | null;
  migration_at: string | null;
}

export interface MonitorMarketValues {
  price_usd: number | null;
  market_cap_usd: number | null;
  liquidity_usd: number | null;
  volume_1m_usd: number | null;
  volume_5m_usd: number | null;
  volume_24h_usd: number | null;
  buys_1m: number | null;
  sells_1m: number | null;
  buys_5m: number | null;
  sells_5m: number | null;
  unique_buyers_1m: number | null;
  unique_buyers_5m: number | null;
  net_buy_flow_1m_usd: number | null;
  net_buy_flow_5m_usd: number | null;
  trades_1m: number | null;
  trades_5m: number | null;
  holders: number | null;
  top10_holder_pct: number | null;
  dev_holder_pct: number | null;
  bundler_pct: number | null;
  sniper_pct: number | null;
}

export type MonitorMarketField = keyof MonitorMarketValues;
export type MonitorMarketFieldStatus = Record<MonitorMarketField, KnowledgeStatus>;

export interface MonitorMarketPoint extends MonitorMarketValues {
  observed_at: string;
  field_source: string | null;
  field_sources?: Partial<Record<MonitorMarketField, string | null>>;
  field_observed_at?: Partial<Record<MonitorMarketField, string | null>>;
  field_status: MonitorMarketFieldStatus;
}

export interface MonitorMarketSnapshot extends MonitorMarketValues {
  status: KnowledgeStatus;
  observed_at: string | null;
  field_status: MonitorMarketFieldStatus;
  field_sources?: Partial<Record<MonitorMarketField, string | null>>;
  field_observed_at?: Partial<Record<MonitorMarketField, string | null>>;
  snapshots: MonitorMarketPoint[];
}

export interface FlowMetricValues {
  unique_buyers: number | null;
  net_buy_flow: number | null;
  trade_frequency: number | null;
  volume: number | null;
}

export interface MonitorFlowAxis {
  status: KnowledgeStatus;
  direction: "accelerating" | "decaying" | "flat" | "unknown";
  sample_count: number;
  previous_positive: boolean;
  current: FlowMetricValues;
  deltas: FlowMetricValues;
  baseline_deltas: FlowMetricValues;
}

export interface MonitorQualityAxis {
  status: KnowledgeStatus;
  liquidity_usd: number | null;
  holders: number | null;
  top10_holder_pct: number | null;
  dev_holder_pct: number | null;
  bundler_pct: number | null;
  sniper_pct: number | null;
  audit_evidence_count: number | null;
}

export interface MonitorRisk {
  hard_blocked: boolean;
  hard_failures: string[];
  soft_flags: string[];
  status: "blocked" | KnowledgeStatus;
}

export interface MonitorNarrativeAxis {
  status: KnowledgeStatus;
  evidence_ids: string[];
}

export interface MonitorTimingAxis {
  status: KnowledgeStatus;
  real_age_seconds: number | null;
  first_seen_age_seconds: number | null;
  latest_event_age_seconds: number | null;
  current_observation_age_seconds: number | null;
  last_observation_age_seconds: number | null;
  market_snapshot_age_seconds: number | null;
  previous_meaningful_event_age_seconds: number | null;
  pair_created_at: string | null;
  pair_age_seconds: number | null;
  migration_at: string | null;
  migration_age_seconds: number | null;
  last_observation_at: string | null;
  market_observed_at: string | null;
  previous_meaningful_event_at: string | null;
  report_refreshed_at: string;
}

export interface MonitorRankingAxes {
  flow: MonitorFlowAxis;
  quality: MonitorQualityAxis;
  market_behavior?: {
    status: "known" | "unknown";
    disposition: "pass" | "observe" | "unknown";
    flags: string[];
    evidence: Record<string, unknown>;
  };
  risk: MonitorRisk;
  narrative: MonitorNarrativeAxis;
  timing: MonitorTimingAxis;
}

export type ResonanceSubtype = "platform_stack" | "cross_provider" | "persistent" | "full";

export interface MonitorResonance {
  subtype: ResonanceSubtype | null;
  signal_resonance: boolean;
  provider_resonance: boolean;
  time_confirmation: boolean;
  provider_families: string[];
  signal_lanes: SignalLane[];
  lanes_by_family: Record<string, SignalLane[]>;
  evidence_ids: string[];
  freshness_window_seconds: number;
  historical_provider_families: string[];
  historical_evidence_ids: string[];
  confirmation_gate?: {
    eligible: boolean;
    failures: string[];
    minimum_market_cap_usd: number;
    minimum_liquidity_usd: number;
    minimum_holders: number | null;
  };
}

export interface WalletEvidence {
  verified_buyers: number;
  wallet_addresses: string[];
  evidence_ids: string[];
  candidate_buyers?: number;
  candidate_wallet_addresses?: string[];
  candidate_evidence_ids?: string[];
  status: "verified" | "candidate" | "conflicting" | "unknown";
  freshness_window_seconds: number;
  conflicting_wallets: string[];
  conflicting_evidence_ids: string[];
  historical_candidate_buyers?: number;
  historical_candidate_wallet_addresses?: string[];
  historical_verified_buyers: number;
  historical_wallet_addresses: string[];
}

export interface MonitorStateHistoryEntry {
  state: MonitorState;
  state_version: number;
  observed_at: string;
}

export interface MonitorAiEvidence {
  status: "pending" | "available" | "unavailable";
  analyzed_at: string | null;
  evidence_ids: string[];
}

export type TokenIntelligenceItem = string | {
  text?: string;
  evidence_ids?: string[];
  address?: string;
  wallet?: string;
  title?: string;
  label?: string;
  name?: string;
  reason?: string;
  summary?: string;
  evidence?: string;
  source?: string;
  observed_at?: string | number | null;
  [key: string]: unknown;
};

export interface MonitorTokenIntelligence {
  status?: "ready" | "partial" | "unavailable" | "pending" | string;
  one_line_judgement?: string;
  project_narrative?: string;
  attention_evidence?: TokenIntelligenceItem[];
  smart_wallets?: TokenIntelligenceItem[];
  identity?: {
    official_status?: string;
    official_contract?: string;
    alternate_contracts?: TokenIntelligenceItem[];
  };
  risks?: TokenIntelligenceItem[];
  missing_evidence?: TokenIntelligenceItem[];
  sources?: Array<{
    source_id?: string;
    title?: string;
    url?: string;
    observed_at?: string | number | null;
  }>;
  ai_narrative?: string | null;
  official_ca_status?: string;
  ca_conflict?: boolean | null;
  social_source_count?: number | null;
  wash_score?: number | null;
  flow_acceleration?: number | null;
  ai_risk_flags?: TokenIntelligenceItem[];
  evidence_urls?: string[];
  ai_analyzed_at?: string | number | null;
  generated_at?: string | number | null;
}

export interface MonitorFreshness {
  status: "fresh" | "stale";
  last_observed_at: string;
  report_refreshed_at: string;
}

export interface MonitorDisplayQuote {
  priceUsd?: number;
  marketCap?: number;
  fdv?: number;
  liquidityUsd?: number;
  volume24h?: number;
  changeM5?: number;
  changeH1?: number;
  changeH24?: number;
  updatedAt?: number;
}

export interface MonitorDiscoveryBaseline {
  first_seen_at: string | null;
  first_market_cap_usd: number | null;
  first_price_usd: number | null;
  peak_market_cap_usd: number | null;
}

export interface MonitorTokenView {
  id: string;
  identity: MonitorTokenIdentity;
  primary_state: MonitorState;
  active_states: MonitorState[];
  state_version: number;
  state_updated_at: string;
  state_history: MonitorStateHistoryEntry[];
  market: MonitorMarketSnapshot;
  ranking_axes: MonitorRankingAxes;
  resonance: MonitorResonance;
  wallet_evidence: WalletEvidence;
  audit_facts: AuditFact[];
  risk: MonitorRisk;
  events: MonitorEvent[];
  missing_evidence: MonitorMarketField[];
  ai: MonitorAiEvidence;
  freshness: MonitorFreshness;
}

export interface MonitorRejection {
  schema_version: 1;
  reason: string;
  source: string;
  chain: string;
  contract_address: string;
  observed_at: string;
  raw_fingerprint: string;
}

export interface MonitorAlert {
  key: string;
  label: "重点" | "加速" | "多源共振" | "趋势观察" | "高风险";
  severity: "medium" | "high" | "critical";
  policy: {
    chain: string;
    stage: string;
  };
  chain: string;
  contract_address: string;
  symbol: string | null;
  transition: string;
  state_version: number;
  event_at: string | null;
  observed_at: string;
  real_age_seconds: number | null;
  event_age_seconds: number | null;
  market: {
    current_market_cap_usd: number | null;
    liquidity_usd: number | null;
    unique_buyers_5m: number | null;
    net_buy_flow_5m_usd: number | null;
  };
  verified_wallet_count: number;
  candidate_wallet_count?: number;
  candidate_wallet_net_flow_usd?: number | null;
  candidate_wallets?: Array<{
    address: string;
    evidence_id: string;
    provider_family?: string | null;
    provider_feed?: string | null;
    event_at?: string | null;
    observed_at?: string | null;
    net_flow_usd?: number | null;
  }>;
  risks: {
    hard_blocked: boolean;
    hard_failures: string[];
    soft_flags: string[];
  };
  missing_evidence: string[];
  evidence_ids: string[];
}

export interface MonitorV3Snapshot {
  schema_version: 3;
  observed_at: string;
  monitor_status: "healthy" | "degraded" | "unknown";
  source_health: Record<string, unknown>;
  tokens: MonitorTokenView[];
  rejections: MonitorRejection[];
  alerts: MonitorAlert[];
}

const MARKET_FIELDS: MonitorMarketField[] = [
  "price_usd",
  "market_cap_usd",
  "liquidity_usd",
  "volume_1m_usd",
  "volume_5m_usd",
  "volume_24h_usd",
  "buys_1m",
  "sells_1m",
  "buys_5m",
  "sells_5m",
  "unique_buyers_1m",
  "unique_buyers_5m",
  "net_buy_flow_1m_usd",
  "net_buy_flow_5m_usd",
  "trades_1m",
  "trades_5m",
  "holders",
  "top10_holder_pct",
  "dev_holder_pct",
  "bundler_pct",
  "sniper_pct",
];

const FLOW_FIELDS: (keyof FlowMetricValues)[] = [
  "unique_buyers",
  "net_buy_flow",
  "trade_frequency",
  "volume",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString);
}

function isMarketField(value: string): value is MonitorMarketField {
  return MARKET_FIELDS.some((field) => field === value);
}

function isMarketValues(value: Record<string, unknown>): boolean {
  return MARKET_FIELDS.every((field) => isNullableNumber(value[field]));
}

function isFieldStatus(value: unknown): value is MonitorMarketFieldStatus {
  return (
    isRecord(value) &&
    MARKET_FIELDS.every((field) => value[field] === "known" || value[field] === "unknown")
  );
}

function isMarketPoint(value: unknown): value is MonitorMarketPoint {
  return (
    isRecord(value) &&
    isString(value.observed_at) &&
    isNullableString(value.field_source) &&
    isMarketValues(value) &&
    isFieldStatus(value.field_status)
  );
}

function isMarket(value: unknown): value is MonitorMarketSnapshot {
  return (
    isRecord(value) &&
    (value.status === "known" || value.status === "unknown") &&
    isNullableString(value.observed_at) &&
    isMarketValues(value) &&
    isFieldStatus(value.field_status) &&
    Array.isArray(value.snapshots) &&
    value.snapshots.every(isMarketPoint)
  );
}

function isFlowValues(value: unknown): value is FlowMetricValues {
  return isRecord(value) && FLOW_FIELDS.every((field) => isNullableNumber(value[field]));
}

function isRisk(value: unknown): value is MonitorRisk {
  return (
    isRecord(value) &&
    typeof value.hard_blocked === "boolean" &&
    isStringArray(value.hard_failures) &&
    isStringArray(value.soft_flags) &&
    ["blocked", "known", "unknown"].includes(String(value.status))
  );
}

function isRankingAxes(value: unknown): value is MonitorRankingAxes {
  if (!isRecord(value) || !isRecord(value.flow) || !isRecord(value.quality)) return false;
  const flow = value.flow;
  const quality = value.quality;
  const marketBehavior = value.market_behavior;
  const narrative = value.narrative;
  const timing = value.timing;
  return (
    (flow.status === "known" || flow.status === "unknown") &&
    ["accelerating", "decaying", "flat", "unknown"].includes(String(flow.direction)) &&
    typeof flow.sample_count === "number" &&
    typeof flow.previous_positive === "boolean" &&
    isFlowValues(flow.current) &&
    isFlowValues(flow.deltas) &&
    isFlowValues(flow.baseline_deltas) &&
    (quality.status === "known" || quality.status === "unknown") &&
    [
      "liquidity_usd",
      "holders",
      "top10_holder_pct",
      "dev_holder_pct",
      "bundler_pct",
      "sniper_pct",
      "audit_evidence_count",
    ].every((field) => isNullableNumber(quality[field])) &&
    (marketBehavior === undefined || (
      isRecord(marketBehavior) &&
      ["known", "unknown"].includes(String(marketBehavior.status)) &&
      ["pass", "observe", "unknown"].includes(String(marketBehavior.disposition)) &&
      isStringArray(marketBehavior.flags) &&
      isRecord(marketBehavior.evidence)
    )) &&
    isRisk(value.risk) &&
    isRecord(narrative) &&
    (narrative.status === "known" || narrative.status === "unknown") &&
    isStringArray(narrative.evidence_ids) &&
    isRecord(timing) &&
    (timing.status === "known" || timing.status === "unknown") &&
    isNullableNumber(timing.real_age_seconds) &&
    isNullableNumber(timing.first_seen_age_seconds) &&
    isNullableNumber(timing.latest_event_age_seconds) &&
    isNullableNumber(timing.current_observation_age_seconds) &&
    isNullableNumber(timing.last_observation_age_seconds) &&
    isNullableNumber(timing.market_snapshot_age_seconds) &&
    isNullableNumber(timing.previous_meaningful_event_age_seconds) &&
    isNullableString(timing.pair_created_at) &&
    isNullableNumber(timing.pair_age_seconds) &&
    isNullableString(timing.migration_at) &&
    isNullableNumber(timing.migration_age_seconds) &&
    isNullableString(timing.last_observation_at) &&
    isNullableString(timing.market_observed_at) &&
    isNullableString(timing.previous_meaningful_event_at) &&
    isString(timing.report_refreshed_at)
  );
}

function isIdentity(value: unknown): value is MonitorTokenIdentity {
  return (
    isRecord(value) &&
    isString(value.chain) &&
    isString(value.contract_address) &&
    isNullableString(value.symbol) &&
    isNullableString(value.name) &&
    isNullableString(value.creator) &&
    isNullableString(value.launchpad) &&
    isNullableString(value.real_created_at) &&
    isNullableString(value.real_created_source) &&
    isString(value.first_seen_at) &&
    isNullableString(value.first_seen_source) &&
    isNullableString(value.pair_created_at) &&
    isNullableString(value.migration_at)
  );
}

function isMonitorState(value: unknown): value is MonitorState {
  return [
    "new",
    "building",
    "resonating",
    "smart_cluster",
    "cooling",
    "trend_watch",
    "revival",
    "blocked_risk",
    "stale",
  ].includes(String(value));
}

function isEvent(value: unknown): value is MonitorEvent {
  return (
    isRecord(value) &&
    value.schema_version === 1 &&
    [
      "event_id",
      "chain",
      "contract_address",
      "event_type",
      "event_at",
      "observed_at",
      "provider_family",
      "provider_feed",
      "provider_event_id",
      "raw_fingerprint",
      "upstream_provider",
      "event_time_basis",
    ].every((field) => isString(value[field])) &&
    isNullableString(value.source_url) &&
    typeof value.counts_for_resonance === "boolean" &&
    ["discovery", "ranking", "wallet", "market", "audit", "enrichment"].includes(
      String(value.evidence_role),
    ) &&
    [
      "new_launch",
      "trending",
      "hot_search",
      "smart_money",
      "kol_social",
      "market_flow",
      "security_audit",
    ].includes(String(value.signal_lane))
  );
}

function isAuditFact(value: unknown): value is AuditFact {
  return (
    isRecord(value) &&
    Object.prototype.hasOwnProperty.call(value, "value") &&
    ["audit_field", "provider_family", "upstream_provider", "observed_at", "evidence_id"].every(
      (field) => isString(value[field]),
    ) &&
    ["known", "unknown", "stale", "conflicting"].includes(String(value.status)) &&
    ["provider_reported", "cross_checked", "locally_verified"].includes(
      String(value.confidence),
    )
  );
}

function isResonance(value: unknown): value is MonitorResonance {
  if (!isRecord(value) || !isRecord(value.lanes_by_family)) return false;
  const gate = value.confirmation_gate;
  const validGate = gate === undefined || (
    isRecord(gate) &&
    typeof gate.eligible === "boolean" &&
    isStringArray(gate.failures) &&
    isNumber(gate.minimum_market_cap_usd) &&
    isNumber(gate.minimum_liquidity_usd) &&
    isNullableNumber(gate.minimum_holders)
  );
  return (
    (value.subtype === null ||
      ["platform_stack", "cross_provider", "persistent", "full"].includes(
        String(value.subtype),
      )) &&
    typeof value.signal_resonance === "boolean" &&
    typeof value.provider_resonance === "boolean" &&
    typeof value.time_confirmation === "boolean" &&
    isStringArray(value.provider_families) &&
    isStringArray(value.signal_lanes) &&
    Object.values(value.lanes_by_family).every(isStringArray) &&
    isStringArray(value.evidence_ids) &&
    isNumber(value.freshness_window_seconds) &&
    isStringArray(value.historical_provider_families) &&
    isStringArray(value.historical_evidence_ids) &&
    validGate
  );
}

function isWalletEvidence(value: unknown): value is WalletEvidence {
  const candidateBuyers = (value as Record<string, unknown>).candidate_buyers;
  const candidateAddresses = (value as Record<string, unknown>).candidate_wallet_addresses;
  const candidateEvidenceIds = (value as Record<string, unknown>).candidate_evidence_ids;
  const historicalCandidateBuyers = (value as Record<string, unknown>).historical_candidate_buyers;
  const historicalCandidateAddresses = (value as Record<string, unknown>).historical_candidate_wallet_addresses;
  return (
    isRecord(value) &&
    typeof value.verified_buyers === "number" &&
    isStringArray(value.wallet_addresses) &&
    isStringArray(value.evidence_ids) &&
    (candidateBuyers === undefined || isNumber(candidateBuyers)) &&
    (candidateAddresses === undefined || isStringArray(candidateAddresses)) &&
    (candidateEvidenceIds === undefined || isStringArray(candidateEvidenceIds)) &&
    ["verified", "candidate", "conflicting", "unknown"].includes(String(value.status)) &&
    isNumber(value.freshness_window_seconds) &&
    isStringArray(value.conflicting_wallets) &&
    isStringArray(value.conflicting_evidence_ids) &&
    (historicalCandidateBuyers === undefined || isNumber(historicalCandidateBuyers)) &&
    (historicalCandidateAddresses === undefined || isStringArray(historicalCandidateAddresses)) &&
    isNumber(value.historical_verified_buyers) &&
    isStringArray(value.historical_wallet_addresses)
  );
}

function isStateHistoryEntry(value: unknown): value is MonitorStateHistoryEntry {
  return (
    isRecord(value) &&
    isMonitorState(value.state) &&
    isNumber(value.state_version) &&
    isString(value.observed_at)
  );
}

function isAiEvidence(value: unknown): value is MonitorAiEvidence {
  return (
    isRecord(value) &&
    ["pending", "available", "unavailable"].includes(String(value.status)) &&
    isNullableString(value.analyzed_at) &&
    isStringArray(value.evidence_ids)
  );
}

function isFreshness(value: unknown): value is MonitorFreshness {
  return (
    isRecord(value) &&
    (value.status === "fresh" || value.status === "stale") &&
    isString(value.last_observed_at) &&
    isString(value.report_refreshed_at)
  );
}

function isToken(value: unknown): value is MonitorTokenView {
  return (
    isRecord(value) &&
    isString(value.id) &&
    isIdentity(value.identity) &&
    isMonitorState(value.primary_state) &&
    Array.isArray(value.active_states) &&
    value.active_states.every(isMonitorState) &&
    isNumber(value.state_version) &&
    isString(value.state_updated_at) &&
    Array.isArray(value.state_history) &&
    value.state_history.every(isStateHistoryEntry) &&
    isMarket(value.market) &&
    isRankingAxes(value.ranking_axes) &&
    isResonance(value.resonance) &&
    isWalletEvidence(value.wallet_evidence) &&
    Array.isArray(value.audit_facts) &&
    value.audit_facts.every(isAuditFact) &&
    isRisk(value.risk) &&
    Array.isArray(value.events) &&
    value.events.every(isEvent) &&
    isStringArray(value.missing_evidence) &&
    value.missing_evidence.every(isMarketField) &&
    isAiEvidence(value.ai) &&
    isFreshness(value.freshness)
  );
}

function isRejection(value: unknown): value is MonitorRejection {
  return (
    isRecord(value) &&
    value.schema_version === 1 &&
    ["reason", "source", "chain", "contract_address", "observed_at", "raw_fingerprint"].every(
      (field) => isString(value[field]),
    )
  );
}

function isAlertWallet(value: unknown): value is NonNullable<MonitorAlert["candidate_wallets"]>[number] {
  return (
    isRecord(value) &&
    isString(value.address) &&
    isString(value.evidence_id) &&
    (value.provider_family === undefined || isNullableString(value.provider_family)) &&
    (value.provider_feed === undefined || isNullableString(value.provider_feed)) &&
    (value.event_at === undefined || isNullableString(value.event_at)) &&
    (value.observed_at === undefined || isNullableString(value.observed_at)) &&
    (value.net_flow_usd === undefined || isNullableNumber(value.net_flow_usd))
  );
}

function isAlert(value: unknown): value is MonitorAlert {
  if (!isRecord(value) || !isRecord(value.policy) || !isRecord(value.market) || !isRecord(value.risks)) {
    return false;
  }
  const candidateWalletCount = value.candidate_wallet_count;
  const candidateWalletNetFlow = value.candidate_wallet_net_flow_usd;
  const candidateWallets = value.candidate_wallets;
  return (
    isString(value.key) &&
    ["重点", "加速", "多源共振", "趋势观察", "高风险"].includes(String(value.label)) &&
    ["medium", "high", "critical"].includes(String(value.severity)) &&
    isString(value.policy.chain) &&
    isString(value.policy.stage) &&
    isString(value.chain) &&
    isString(value.contract_address) &&
    isNullableString(value.symbol) &&
    isString(value.transition) &&
    isNumber(value.state_version) &&
    isNullableString(value.event_at) &&
    isString(value.observed_at) &&
    isNullableNumber(value.real_age_seconds) &&
    isNullableNumber(value.event_age_seconds) &&
    isNullableNumber(value.market.current_market_cap_usd) &&
    isNullableNumber(value.market.liquidity_usd) &&
    isNullableNumber(value.market.unique_buyers_5m) &&
    isNullableNumber(value.market.net_buy_flow_5m_usd) &&
    isNumber(value.verified_wallet_count) &&
    (candidateWalletCount === undefined || isNumber(candidateWalletCount)) &&
    (candidateWalletNetFlow === undefined || isNullableNumber(candidateWalletNetFlow)) &&
    (candidateWallets === undefined || (Array.isArray(candidateWallets) && candidateWallets.every(isAlertWallet))) &&
    typeof value.risks.hard_blocked === "boolean" &&
    isStringArray(value.risks.hard_failures) &&
    isStringArray(value.risks.soft_flags) &&
    isStringArray(value.missing_evidence) &&
    isStringArray(value.evidence_ids)
  );
}

export function isMonitorV3Snapshot(value: unknown): value is MonitorV3Snapshot {
  return (
    isRecord(value) &&
    value.schema_version === 3 &&
    isString(value.observed_at) &&
    ["healthy", "degraded", "unknown"].includes(String(value.monitor_status)) &&
    isRecord(value.source_health) &&
    Array.isArray(value.tokens) &&
    value.tokens.every(isToken) &&
    Array.isArray(value.rejections) &&
    value.rejections.every(isRejection) &&
    Array.isArray(value.alerts) &&
    value.alerts.every(isAlert)
  );
}

export function parseMonitorV3Snapshot(value: unknown): MonitorV3Snapshot | undefined {
  if (!isRecord(value)) return undefined;
  const normalized = Object.prototype.hasOwnProperty.call(value, "alerts")
    ? value
    : { ...value, alerts: [] };
  return isMonitorV3Snapshot(normalized) ? normalized : undefined;
}
