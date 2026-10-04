import type {
  MonitorAvailability,
  MonitorState,
  MonitorTab,
  MonitorTokenView,
  MonitorV3Snapshot,
  ResonanceSubtype,
} from "./types";

export const MONITOR_CHAINS = {
  bsc: { label: "BSC", freshnessSeconds: 120 },
  robinhood: { label: "Robinhood", freshnessSeconds: 300 },
  arc: { label: "ARC", freshnessSeconds: 120 },
} as const;

export type MonitorChain = keyof typeof MONITOR_CHAINS;
export type ChainFilter = "all" | MonitorChain;

const MONITOR_CHAIN_ALIASES: Record<string, MonitorChain> = {
  "56": "bsc",
  "0x38": "bsc",
  bnb: "bsc",
  bsc: "bsc",
  "binance-smart-chain": "bsc",
  "4663": "robinhood",
  "0x1237": "robinhood",
  robinhood: "robinhood",
  "robinhood-chain": "robinhood",
  "5042": "arc",
  "0x13b2": "arc",
  arc: "arc",
  "arc-mainnet": "arc",
  arc_mainnet: "arc",
};

export function normalizeMonitorChain(value: unknown): string {
  const chain = String(value ?? "").trim().toLowerCase();
  return MONITOR_CHAIN_ALIASES[chain] ?? chain;
}

const TAB_STATE: Partial<Record<MonitorTab, MonitorState>> = {
  building: "building",
  resonating: "resonating",
  smart: "smart_cluster",
  revival: "revival",
  risk: "blocked_risk",
};

const PRIMARY_STATE_PRIORITY: Record<MonitorState, number> = {
  blocked_risk: 8,
  revival: 7,
  smart_cluster: 6,
  resonating: 5,
  building: 4,
  trend_watch: 3,
  cooling: 2,
  new: 1,
  stale: 0,
};

const RESONANCE_PRIORITY: Record<ResonanceSubtype, number> = {
  full: 4,
  cross_provider: 3,
  persistent: 2,
  platform_stack: 1,
};

const CONFIRMATION_MIN_MARKET_CAP_USD = 10_000;
const CONFIRMATION_MIN_LIQUIDITY_USD = 8_000;
const CONFIRMATION_MIN_HOLDERS = 20;

function recentMarketValue(
  token: MonitorTokenView,
  field: "market_cap_usd" | "liquidity_usd" | "holders",
  snapshotObservedAt: string,
): number | null {
  const current = token.market[field];
  if (current !== null) return current;
  const chain = normalizeMonitorChain(token.identity.chain);
  const windowSeconds = chain in MONITOR_CHAINS
    ? MONITOR_CHAINS[chain as MonitorChain].freshnessSeconds
    : 300;
  for (const point of [...token.market.snapshots].reverse()) {
    const age = ageSeconds(snapshotObservedAt, point.observed_at);
    if (age === null || age > windowSeconds) continue;
    const value = point[field];
    if (value !== null) return value;
  }
  return null;
}

export function isConfirmationEligible(
  token: MonitorTokenView,
  snapshotObservedAt = token.freshness.report_refreshed_at,
): boolean {
  if (token.resonance.confirmation_gate) {
    return token.resonance.confirmation_gate.eligible;
  }
  const marketCap = recentMarketValue(token, "market_cap_usd", snapshotObservedAt);
  const liquidity = recentMarketValue(token, "liquidity_usd", snapshotObservedAt);
  const holders = recentMarketValue(token, "holders", snapshotObservedAt);
  if (marketCap === null || marketCap < CONFIRMATION_MIN_MARKET_CAP_USD) return false;
  if (liquidity === null || liquidity < CONFIRMATION_MIN_LIQUIDITY_USD) return false;
  switch (normalizeMonitorChain(token.identity.chain)) {
    case "bsc":
    case "arc":
      return holders !== null && holders >= CONFIRMATION_MIN_HOLDERS;
    case "robinhood":
      return true;
    default:
      return true;
  }
}

function confirmationGateFailures(token: MonitorTokenView): Set<string> {
  return new Set(token.resonance.confirmation_gate?.failures ?? []);
}

function isExpiredTrendWatch(
  token: MonitorTokenView,
  snapshotObservedAt: string,
): boolean {
  if (token.freshness.status !== "fresh") return false;
  if (token.active_states.includes("blocked_risk") || token.primary_state === "blocked_risk") return false;
  if (token.active_states.includes("revival") || token.primary_state === "revival") return false;
  if (
    token.primary_state === "trend_watch" ||
    token.active_states.includes("trend_watch") ||
    token.ranking_axes.market_behavior?.disposition === "observe"
  ) return true;
  if (!confirmationGateFailures(token).has("discovery_window_expired")) return false;
  return (
    token.resonance.subtype !== null ||
    token.wallet_evidence.verified_buyers > 0 ||
    token.active_states.includes("building") ||
    currentSelectedSignalAgeSeconds(token, snapshotObservedAt) !== null
  );
}

function ageSeconds(observedAt: string, eventAt: string): number | null {
  const observed = Date.parse(observedAt);
  const event = Date.parse(eventAt);
  if (!Number.isFinite(observed) || !Number.isFinite(event) || event > observed) return null;
  return (observed - event) / 1000;
}

function currentSignalAgeSeconds(
  token: MonitorTokenView,
  snapshotObservedAt: string,
  evidenceRoles: ReadonlySet<string>,
): number | null {
  const windowSeconds = token.resonance.freshness_window_seconds;
  if (!Number.isFinite(windowSeconds) || windowSeconds <= 0) return null;

  const currentAges = token.events.flatMap((event) => {
    if (!evidenceRoles.has(event.evidence_role) || event.counts_for_resonance !== true) return [];
    const eventAge = ageSeconds(snapshotObservedAt, event.event_at);
    const observationAge = ageSeconds(snapshotObservedAt, event.observed_at);
    if (
      eventAge === null ||
      observationAge === null ||
      eventAge > windowSeconds ||
      observationAge > windowSeconds
    ) {
      return [];
    }
    return [eventAge];
  });
  return currentAges.length > 0 ? Math.min(...currentAges) : null;
}

function currentDiscoveryAgeSeconds(
  token: MonitorTokenView,
  snapshotObservedAt: string,
): number | null {
  return currentSignalAgeSeconds(token, snapshotObservedAt, new Set(["discovery"]));
}

function currentSelectedSignalAgeSeconds(
  token: MonitorTokenView,
  snapshotObservedAt: string,
): number | null {
  return currentSignalAgeSeconds(token, snapshotObservedAt, new Set(["discovery", "ranking"]));
}

export function isCurrentNewDiscovery(
  token: MonitorTokenView,
  snapshotObservedAt: string,
): boolean {
  return currentDiscoveryAgeSeconds(token, snapshotObservedAt) !== null;
}

function matchesTab(
  token: MonitorTokenView,
  tab: MonitorTab,
  snapshotObservedAt: string,
): boolean {
  if (tab === "focus") {
    if (
      token.freshness.status !== "fresh" ||
      token.active_states.includes("blocked_risk") ||
      isExpiredTrendWatch(token, snapshotObservedAt)
    ) return false;
    if (token.primary_state === "building") return true;
    return token.primary_state === "resonating" && isConfirmationEligible(token, snapshotObservedAt);
  }
  if (tab === "all") return true;
  if (tab === "new") return isCurrentNewDiscovery(token, snapshotObservedAt);
  if (tab === "selected") {
    return (
      token.freshness.status === "fresh" &&
      !token.active_states.includes("blocked_risk") &&
      token.primary_state === "new" &&
      currentSelectedSignalAgeSeconds(token, snapshotObservedAt) !== null &&
      isConfirmationEligible(token, snapshotObservedAt)
    );
  }
  if (tab === "trend") return isExpiredTrendWatch(token, snapshotObservedAt);
  const state = TAB_STATE[tab];
  if (state === "blocked_risk") return token.primary_state === state;
  if (
    state !== undefined &&
    ["building", "resonating", "smart_cluster"].includes(state) &&
    isExpiredTrendWatch(token, snapshotObservedAt)
  ) {
    return false;
  }
  return (
    state !== undefined &&
    token.freshness.status === "fresh" &&
    !token.active_states.includes("blocked_risk") &&
    token.primary_state === state &&
    (state !== "resonating" || isConfirmationEligible(token, snapshotObservedAt))
  );
}

function matchesQuery(token: MonitorTokenView, query: string): boolean {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) return true;

  const searchable = [
    token.identity.symbol,
    token.identity.name,
    token.identity.chain,
    token.identity.contract_address,
  ];
  return searchable.some((value) => value?.toLowerCase().includes(normalizedQuery));
}

function matchesChain(token: MonitorTokenView, chainFilter: ChainFilter): boolean {
  return chainFilter === "all" || normalizeMonitorChain(token.identity.chain) === chainFilter;
}

function hasSupportedSchema(snapshot: MonitorV3Snapshot | undefined): snapshot is MonitorV3Snapshot {
  return snapshot?.schema_version === 3;
}

function timestampValue(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}

function compareDescending(left: number | null, right: number | null): number {
  return (right ?? Number.NEGATIVE_INFINITY) - (left ?? Number.NEGATIVE_INFINITY);
}

function compareAscending(left: number | null, right: number | null): number {
  return (left ?? Number.POSITIVE_INFINITY) - (right ?? Number.POSITIVE_INFINITY);
}

function firstDifference(comparisons: number[]): number {
  return comparisons.find((comparison) => comparison !== 0) ?? 0;
}

function compareFlowMetrics(
  left: MonitorTokenView,
  right: MonitorTokenView,
  axis: "deltas" | "baseline_deltas",
): number {
  const leftFlow = left.ranking_axes.flow[axis];
  const rightFlow = right.ranking_axes.flow[axis];
  return firstDifference([
    compareDescending(leftFlow.unique_buyers, rightFlow.unique_buyers),
    compareDescending(leftFlow.net_buy_flow, rightFlow.net_buy_flow),
    compareDescending(leftFlow.trade_frequency, rightFlow.trade_frequency),
    compareDescending(leftFlow.volume, rightFlow.volume),
  ]);
}

function compareForTab(
  left: MonitorTokenView,
  right: MonitorTokenView,
  tab: MonitorTab,
  snapshotObservedAt: string,
): number {
  if (tab === "focus") {
    const leftConfirmed = left.primary_state === "resonating" ? 1 : 0;
    const rightConfirmed = right.primary_state === "resonating" ? 1 : 0;
    return firstDifference([
      rightConfirmed - leftConfirmed,
      right.resonance.provider_families.length - left.resonance.provider_families.length,
      timestampValue(right.state_updated_at) - timestampValue(left.state_updated_at),
    ]);
  }
  if (tab === "all") {
    return firstDifference([
      PRIMARY_STATE_PRIORITY[right.primary_state] - PRIMARY_STATE_PRIORITY[left.primary_state],
      timestampValue(right.state_updated_at) - timestampValue(left.state_updated_at),
    ]);
  }
  if (tab === "new") {
    return compareAscending(
      currentDiscoveryAgeSeconds(left, snapshotObservedAt),
      currentDiscoveryAgeSeconds(right, snapshotObservedAt),
    );
  }
  if (tab === "selected") {
    return compareAscending(
      currentSelectedSignalAgeSeconds(left, snapshotObservedAt),
      currentSelectedSignalAgeSeconds(right, snapshotObservedAt),
    );
  }
  if (tab === "building") return compareFlowMetrics(left, right, "deltas");
  if (tab === "resonating") {
    const leftSubtype = left.resonance.subtype
      ? RESONANCE_PRIORITY[left.resonance.subtype]
      : 0;
    const rightSubtype = right.resonance.subtype
      ? RESONANCE_PRIORITY[right.resonance.subtype]
      : 0;
    return firstDifference([
      rightSubtype - leftSubtype,
      right.resonance.provider_families.length - left.resonance.provider_families.length,
      right.resonance.signal_lanes.length - left.resonance.signal_lanes.length,
      Number(right.resonance.time_confirmation) - Number(left.resonance.time_confirmation),
      right.resonance.evidence_ids.length - left.resonance.evidence_ids.length,
    ]);
  }
  if (tab === "smart") {
    return firstDifference([
      right.wallet_evidence.verified_buyers - left.wallet_evidence.verified_buyers,
      right.wallet_evidence.wallet_addresses.length - left.wallet_evidence.wallet_addresses.length,
      right.wallet_evidence.evidence_ids.length - left.wallet_evidence.evidence_ids.length,
    ]);
  }
  if (tab === "trend") {
    return firstDifference([
      right.wallet_evidence.verified_buyers - left.wallet_evidence.verified_buyers,
      right.resonance.provider_families.length - left.resonance.provider_families.length,
      compareFlowMetrics(left, right, "deltas"),
      timestampValue(right.state_updated_at) - timestampValue(left.state_updated_at),
    ]);
  }
  if (tab === "revival") return compareFlowMetrics(left, right, "baseline_deltas");
  return firstDifference([
    right.risk.hard_failures.length - left.risk.hard_failures.length,
    right.risk.soft_flags.length - left.risk.soft_flags.length,
  ]);
}

function sortForTab(
  tokens: MonitorTokenView[],
  tab: MonitorTab,
  snapshotObservedAt: string,
): MonitorTokenView[] {
  return [...tokens].sort(
    (left, right) =>
      compareForTab(left, right, tab, snapshotObservedAt) || left.id.localeCompare(right.id),
  );
}

export function selectMonitorRows(
  snapshot: MonitorV3Snapshot | undefined,
  tab: MonitorTab,
  query: string,
  chainFilter: ChainFilter = "all",
): MonitorTokenView[] {
  if (!hasSupportedSchema(snapshot)) return [];
  const tokens = snapshot.tokens;
  return sortForTab(
    tokens
      .filter((token) => matchesTab(token, tab, snapshot.observed_at))
      .filter((token) => matchesChain(token, chainFilter))
      .filter((token) => matchesQuery(token, query)),
    tab,
    snapshot.observed_at,
  );
}

export function monitorTabCounts(
  snapshot: MonitorV3Snapshot | undefined,
  chainFilter: ChainFilter = "all",
): Record<MonitorTab, number> {
  return {
    focus: selectMonitorRows(snapshot, "focus", "", chainFilter).length,
    all: selectMonitorRows(snapshot, "all", "", chainFilter).length,
    new: selectMonitorRows(snapshot, "new", "", chainFilter).length,
    selected: selectMonitorRows(snapshot, "selected", "", chainFilter).length,
    building: selectMonitorRows(snapshot, "building", "", chainFilter).length,
    resonating: selectMonitorRows(snapshot, "resonating", "", chainFilter).length,
    smart: selectMonitorRows(snapshot, "smart", "", chainFilter).length,
    trend: selectMonitorRows(snapshot, "trend", "", chainFilter).length,
    revival: selectMonitorRows(snapshot, "revival", "", chainFilter).length,
    risk: selectMonitorRows(snapshot, "risk", "", chainFilter).length,
  };
}

export function monitorV3Availability(
  snapshot: MonitorV3Snapshot | undefined,
): MonitorAvailability {
  return hasSupportedSchema(snapshot) ? "available" : "unavailable";
}
