export type Chain = "bsc" | "robinhood";
export type StrategyView = {
  strategy_version?: string;
  signal_stage?: string;
  entry_route?: string;
  execution_mode?: string;
  rank_score?: number | null;
  rank_components?: Record<string, number>;
  first_seen_at?: string;
  first_price_usd?: number | null;
  first_mcap_usd?: number | null;
  current_price_usd?: number | null;
  current_mcap_usd?: number | null;
  entry_delay_seconds?: number | null;
  markup_from_first?: number | null;
  policy_checks?: Record<string, boolean>;
  eligible?: boolean | null;
  reject_reason?: string | null;
  order_notional_usd?: number | null;
  first_mcap_min_usd?: number | null;
  first_mcap_max_usd?: number | null;
  max_entry_delay_seconds?: number | null;
  max_markup_from_first?: number | null;
  min_liquidity_usd?: number | null;
  max_round_trip_loss_pct?: number | null;
  max_buy_price_impact_pct?: number | null;
  max_sell_price_impact_pct?: number | null;
  min_sellable_cycles?: number | null;
  requires_observed_sell?: boolean | null;
  max_open_positions?: number | null;
  max_open_notional_usd?: number | null;
  daily_realized_loss_stop_usd?: number | null;
  min_rank_score?: number | null;
  max_pair_age_hours?: number | null;
  min_independent_sources?: number | null;
  allowed_recommendation_buckets?: string[] | null;
};
export type TradeabilityView = {
  status?: "pending" | "unavailable" | "passed" | "failed" | string | null;
  reject_reason?: string | null;
  liquidity_usd?: number | null;
  buy_impact_pct?: number | null;
  sell_impact_pct?: number | null;
  round_trip_loss_pct?: number | null;
  quote_at?: string | null;
};
export type ModelView = {
  status?: string | null;
  model_version?: string | null;
  p_2x?: number | null;
  p_5x?: number | null;
  expected_net_return_pct?: number | null;
  sample_count?: number | null;
};
export type Position = {
  chain: Chain;
  token: string;
  symbol: string;
  entry_at?: string;
  last_quote_at?: string;
  entry_price_usd?: number | null;
  current_price_usd?: number | null;
  return_pct?: number | null;
  entry_notional_usd?: number | null;
  entry_native_price_usd?: number | null;
  entry_native_atomic?: string;
  entry_native_spent_atomic?: string;
  remaining_atomic?: string;
  original_atomic?: string;
  tp1_hit?: boolean;
  tp2_hit?: boolean;
  tp3_hit?: boolean;
  exit_status?: string;
  strategy?: StrategyView | null;
};
export type AccountingEvidence = {
  status: string;
  reason?: string;
  receipt_status?: string;
  trace_status?: string;
  fifo_status?: string;
  gas_native_atomic?: string | null;
  native_spent_atomic?: string | null;
  net_native_received_atomic?: string | null;
  cost_basis_native_atomic?: string | null;
  pnl_native_atomic?: string | null;
  verified_at?: string;
};
export type Order = {
  accounting?: AccountingEvidence;
  chain: Chain;
  token: string;
  symbol: string;
  side: string;
  time?: string;
  tx_hash?: string;
  explorer_url?: string;
  exit_reason?: string;
  pnl_native?: number | null;
  return_pct?: number | null;
  native_spent_atomic?: string;
  net_native_received_atomic?: string;
  gas_native_atomic?: string;
  status?: string;
};
export type Candidate = {
  chain: Chain;
  token?: string;
  symbol: string;
  candidate_kind?: "active" | "shadow" | "rejected" | string;
  mcap?: number | null;
  first_mcap_usd?: number | null;
  liquidity?: number | null;
  score?: number | null;
  source_count?: number | null;
  reason?: string;
  signal_at?: string;
  status?: string;
  strategy?: StrategyView | null;
  tradeability?: TradeabilityView | null;
  model?: ModelView | null;
};
export type ChainStatus = {
  chain: Chain;
  state: string;
  ledger_identity_status?: string;
  activity_status?: string;
  activity_message?: string;
  activity_reason?: string;
  state_label?: string;
  updated_at?: string;
  open_positions?: number | null;
  realized_pnl_native?: number | null;
  realized_pnl_status?: "verified" | "pending_verification" | string;
  realized_pnl_pending_count?: number | null;
  unverified_sell_fills?: number | null;
  unreconciled_external_sales?: number | null;
  realized_pnl_method?: string | null;
  manual_reconciled_sales?: number | null;
  native_symbol?: string;
  configured_strategy?: StrategyView | null;
  effective_strategy?: StrategyView | null;
  manual_reconciliation?: {
    status?: "clear" | "unreconciled" | "unknown" | string;
    count?: number | null;
    reconciled_count?: number | null;
  } | null;
};
export type Balance = {
  chain: Chain;
  native_symbol?: string;
  native_balance?: number | null;
  native_price_usd?: number | null;
  native_balance_usd?: number | null;
  usdt_balance?: number | null;
  updated_at?: string;
  status?: string;
};
export type ControlState = {
  chain: Chain;
  supported: boolean;
  state: string;
  reason?: string | null;
  request_id?: string | null;
};
export type Runtime = {
  chain: Chain;
  pid?: number;
  running: boolean;
  amount_usd?: number | string | null;
  configured_amount_usd?: number | string | null;
  effective_amount_usd?: number | string | null;
  executor_acknowledged_at?: string | null;
  amount_native_atomic?: string | null;
  exit_only?: boolean;
  verified_at?: string;
};
export type Snapshot = {
  system_health?: {
    ok: boolean;
    updated_at: string;
    blocking_entry_issue_count: number;
    blocking_exit_issue_count: number;
    issues: {
      component: string;
      code: string;
      message: string;
      affects_live_entries: boolean;
      affects_live_exits: boolean;
    }[];
  };
  session?: {
    strategy_version: string;
    started_at: Record<Chain, string>;
  };
  accounting?: {
    total: number;
    attempted: number;
    receipt_covered: number;
    gas_covered: number;
    payment_covered: number;
    fifo_covered: number;
    pnl_covered: number;
    persistence_status: string;
  };
  updated_at: string;
  wallet: string | null;
  chains: ChainStatus[];
  positions: Position[];
  orders: Order[];
  candidates: Candidate[];
  events: {
    chain?: Chain;
    time?: string;
    symbol?: string;
    message?: string;
    type?: string;
  }[];
  balances: Balance[];
};

export function resolveApiRoot(search: string, buildRoot = "") {
  const queryRoot = new URLSearchParams(search).get("api") || "";
  const candidate = queryRoot || buildRoot;
  if (!candidate) return { root: "/api/terminal", publicReadOnly: false };
  try {
    const url = new URL(candidate);
    const valid =
      url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      url.pathname.replace(/\/$/, "") === "/api/terminal";
    if (!valid) return { root: "/api/terminal", publicReadOnly: false };
    return { root: url.href.replace(/\/$/, ""), publicReadOnly: true };
  } catch {
    return { root: "/api/terminal", publicReadOnly: false };
  }
}

const apiConfig = resolveApiRoot(
  typeof location === "undefined" ? "" : location.search,
  import.meta.env.VITE_TERMINAL_API_BASE || "",
);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiConfig.root}/${path}`, {
    cache: "no-store",
    credentials: apiConfig.publicReadOnly ? "omit" : "same-origin",
    ...init,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.message || body.error || "请求失败");
  return body;
}

export const terminalApi = {
  publicReadOnly: apiConfig.publicReadOnly,
  snapshot: () => request<Snapshot>("snapshot"),
  runtime: () =>
    request<{
      runtime: Runtime[];
      controls?: ControlState[];
      verified: boolean;
      unassigned_workers?: number;
      drafts: {
        chain: Chain;
        amount_usd: string | null;
        requested_at?: string;
      }[];
    }>("runtime"),
  post: async (path: string, payload: object) => {
    if (apiConfig.publicReadOnly) throw new Error("公网只读模式仅支持查看");
    const session = await request<{ csrf_token: string }>("session");
    return request<{
      message?: string;
      status?: string;
      wallet_address?: string;
    }>(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": session.csrf_token,
      },
      body: JSON.stringify(payload),
    });
  },
};

export const chainName = (chain: string) =>
  chain === "bsc" ? "BSC" : chain === "robinhood" ? "Robinhood" : chain;
export const nativeSymbol = (chain: string) =>
  chain === "bsc" ? "BNB" : "ETH";
export const usd = (n?: number | null) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      }).format(n);
export const pct = (n?: number | null) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
export const atomic = (n?: string) =>
  n && /^-?\d+$/.test(n) ? Number(n) / 1e18 : null;
export const native = (n?: number | null) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 }).format(n);
export const time = (s?: string) =>
  !s || !Number.isFinite(Date.parse(s))
    ? "—"
    : new Date(s).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
export const short = (s?: string | null) =>
  s ? `${s.slice(0, 6)}…${s.slice(-4)}` : "未配置";
export const gmgn = (chain: string, token: string) =>
  `https://gmgn.ai/${encodeURIComponent(chain)}/token/${encodeURIComponent(token)}`;
export const executionReasons: Record<string, string> = {
  execution_reconciliation_required: "成交待核验，暂不重复提交",
  ledger_wallet_unverified: "历史账本钱包待核验",
  ledger_wallet_mismatch: "历史账本钱包与当前钱包不一致",
  ledger_chain_mismatch: "历史账本网络与当前网络不一致",
  sell_settlement_unverified: "卖出数量待核验，保留持仓",
  executable_exit_quote_unavailable: "卖出报价暂不可用，持续重试",
  buy_round_trip_loss_exceeded: "预计往返损耗超过上限",
  bsc_liquidity_gate: "BSC 流动性低于 20K",
  bsc_score_gate: "BSC 评分不在 60-69",
  bsc_momentum_gate: "BSC 5 分钟涨幅不在 0%-30%",
  bsc_no_observed_sells: "BSC 尚未观察到真实卖单",
  market_cap_unavailable: "流通市值缺失，仅 FDV 不能作为入场依据",
  ledger_identity_unverified: "账本钱包与网络归属待核验",
  ledger_identity_mismatch: "账本钱包或网络与当前配置不一致",
  missing_token_identity: "缺少链与合约身份",
  unsupported_chain: "当前网络不受策略支持",
  missing_first_snapshot: "缺少不可变首次发现快照",
  first_snapshot_identity_mismatch: "首次发现快照与当前链或合约不一致",
  current_metrics_unavailable: "当前价格或市值不可用",
  robinhood_rank_unavailable: "Robinhood 发现排名不可用",
  bsc_rank_unavailable: "BSC 发现排名不可用",
  bsc_shadow_only: "BSC 聚合发现仅作影子观察",
  bsc_mcap_shadow_only: "BSC 该市值区间仅作影子观察",
  robinhood_signal_stage: "Robinhood 信号阶段不符合入场路线",
  bsc_signal_stage: "BSC 信号阶段不符合入场路线",
  robinhood_stage_required: "Robinhood 信号阶段不符合入场路线",
  bsc_stage_required: "BSC 信号阶段不符合入场路线",
  chain_v2_contract_incomplete: "V2 候选合同字段不完整",
  chain_v2_input_required: "等待有效的 V2 候选输入",
  chain_v2_order_size_conflict: "下单金额与该链固定策略冲突",
  exact_entry_tradeability_timeout: "精确交易性检查超时，跳过本轮候选",
  buy_price_impact_exceeded: "买入价格冲击超过策略上限",
  sell_price_impact_exceeded: "卖出价格冲击超过策略上限",
  entry_signal_expired: "候选已超过入场时间窗口",
  markup_from_first_exceeded: "当前价格相对首次发现已追高",
  daily_loss_limit: "该链今日亏损已达到熔断上限",
  insufficient_round_trip_gas: "原生币不足以覆盖买入和退出 Gas",
  robinhood_first_mcap_range: "Robinhood 首次市值不在 10K–300K",
  bsc_first_mcap_range: "BSC 首次市值不在 10K–100K",
  robinhood_entry_window: "Robinhood 已超过 10 分钟入场窗口",
  bsc_entry_window: "BSC 已超过 10 分钟入场窗口",
  robinhood_markup_limit: "Robinhood 相对首次价格涨幅超过 1.25x",
  bsc_markup_limit: "BSC 相对首次价格涨幅超过 1.5x",
  robinhood_liquidity_minimum: "Robinhood 流动性低于 8K",
  bsc_liquidity_minimum: "BSC 流动性低于 8K",
  robinhood_buy_route: "Robinhood 买入路线不可用",
  robinhood_sell_route: "Robinhood 卖出路线不可用",
  bsc_buy_route: "BSC 买入路线不可用",
  bsc_sell_route: "BSC 卖出路线不可用",
  robinhood_round_trip_loss_limit: "Robinhood 预计往返损耗超过 25%",
  bsc_round_trip_loss_limit: "BSC 预计往返损耗超过 25%",
  robinhood_buy_impact_limit: "Robinhood 买入冲击超过 12%",
  bsc_buy_impact_limit: "BSC 买入冲击超过策略上限",
  robinhood_sell_impact_limit: "Robinhood 卖出冲击超过策略上限",
  bsc_sell_impact_limit: "BSC 卖出冲击超过 12%",
  robinhood_hard_risk: "Robinhood 合约硬风险未通过",
  bsc_hard_risk: "BSC 合约硬风险未通过",
  robinhood_sellability_confirmation: "Robinhood 可卖确认未完成",
  bsc_sellability_confirmation: "BSC 尚未完成两轮可卖确认",
  robinhood_observed_sell_required: "Robinhood 尚未观察到真实卖单",
  bsc_observed_sell_required: "BSC 尚未观察到真实卖单",
};
export const reason = (s?: string | null) =>
  ({
    ...executionReasons,
    stop_loss: "止损",
    take_profit_1: "首次止盈",
    trailing_stop: "移动止盈",
    time_stop: "持仓超时",
    hold: "继续持有",
    first_discovery: "首次发现",
    narrative_breakout: "叙事突破",
    exit_conditions_not_met: "尚未达到卖出条件",
    strategy_candidate_unavailable: "等待合格候选",
    new_entries_disabled: "暂停新开仓",
  })[s || ""] ||
  s ||
  "—";
