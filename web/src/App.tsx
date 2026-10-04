import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useMonitorV3Stream } from "./useMonitorV3Stream";
import { paperValuationLabel, type PaperValuation } from "./paper-valuation";
import {
  Activity,
  ArrowLeft,
  ExternalLink,
  BarChart3,
  Bell,
  BellRing,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Flame,
  Gauge,
  Radar,
  Search,
  ShieldCheck,
  Sparkles,
  Star,
  Volume2,
  VolumeX,
  Wallet,
  X,
} from "lucide-react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { MemeMonitorView } from "./monitor-v3/MemeMonitorView";
import {
  activeMonitorAlerts,
  monitorAlertDisplayKey,
  monitorAlertBody,
  monitorAlertSpeechText,
  monitorAlertSoundTier,
  monitorAlertTitle,
  startupMonitorAlerts,
} from "./monitor-v3/alerts";
import { parseMonitorV3Snapshot } from "./monitor-v3/types";
import { buildTokenIntelligenceIndex, tokenIntelligenceKey } from "./monitor-v3/intelligence";
import type { MonitorAlert, MonitorDiscoveryBaseline, MonitorTokenIntelligence, MonitorTokenView, TokenIntelligenceItem } from "./monitor-v3/types";
import "./App.css";

declare global {
  interface Window {
    ethereum?: {
      request: (args: { method: string; params?: unknown[] }) => Promise<unknown>;
    };
  }
}

const BUCKET_LABELS = {
  lead: "主推候选",
  ambush: "埋伏候选",
  pullback: "等回踩候选",
  danger: "高危别碰",
  reject: "放弃观察",
};

type RecommendationBucket = keyof typeof BUCKET_LABELS;

type EvidenceNumber = number | string | null;
type PaperArmSummary = {
  ok?: boolean;
  summary?: PaperValuation & {
    cash_usd?: number;
    open_exposure_usd?: number;
    realized_pnl_usd?: number;
    unrealized_pnl_usd?: number | null;
  };
  open_count?: number;
  closed_count?: number;
  entry_policy?: string | null;
  profile?: string;
};
type PaperTradingSummary = {
  ok?: boolean;
  updated_at?: string;
  mode?: string;
  first_discovery?: PaperArmSummary;
  first_discovery_shadow?: PaperArmSummary;
  wallet_style?: PaperArmSummary;
};
type SmartMoneyEvidence = {
  qualified_wallet_count?: EvidenceNumber;
  qualified_freshness?: string;
  qualified_latest_evidence_at?: string | null;
  confirmation?: { buy_wallet_count?: EvidenceNumber; buy_usd?: EvidenceNumber; sell_usd?: EvidenceNumber; net_flow_usd?: EvidenceNumber };
  unique_wallets?: EvidenceNumber | string[];
  qualified_wallets?: string[];
  candidate_buy_wallets?: string[];
  candidate_sell_wallets?: string[];
  unique_wallet_count?: EvidenceNumber;
  fresh_unique_wallet_count?: EvidenceNumber;
  linked_clusters?: string[];
  events?: unknown[];
  net_flow_usd?: EvidenceNumber;
  flow_event_count?: EvidenceNumber;
  freshness?: string;
  latest_evidence_at?: string | null;
  wallet_verification?: string;
  watchlist_matches?: string[];
  watchlist?: { matched_wallets?: string[]; status?: string; updated_at?: string | null };
  independent_wallets?: EvidenceNumber;
  buy_usd?: EvidenceNumber;
  sell_usd?: EvidenceNumber;
  net_buy_usd?: EvidenceNumber;
  candidate_buy_wallet_count?: EvidenceNumber;
  candidate_buy_transaction_count?: EvidenceNumber;
  candidate_buy_usd?: EvidenceNumber;
  candidate_sell_usd?: EvidenceNumber;
  candidate_net_flow_usd?: EvidenceNumber;
  candidate_verified_buy_wallet_count?: EvidenceNumber;
  candidate_layer?: string;
  candidate_sources?: string[];
  watchlist_wallets?: EvidenceNumber;
  quality?: string | null;
  status?: string | null;
  reason?: string | null;
  updated_at?: string | number | null;
  [key: string]: unknown;
};

type MonitorSummary = {
  rate_limited?: boolean;
  retry_after?: string;
  rate_limit_failures?: number;
  ok?: boolean | null;
  stale?: boolean | null;
  errors?: unknown;
  status?: string | null;
  enabled?: boolean | null;
  reason?: string | null;
  summary?: string | null;
  updated_at?: string | number | null;
  [key: string]: unknown;
};

type ProjectEvidence = { url?: string; source_url?: string; title?: string; label?: string; reason?: string; source?: string };
type PrelaunchProject = {
  project?: string;
  stage?: string;
  score?: EvidenceNumber;
  post_url?: string;
  observed_at?: string | number | null;
  matched_signals?: string[];
  text?: string;
  has_contract?: boolean;
  official_verified?: boolean;
  id?: string;
  name?: string;
  symbol?: string;
  chain?: string;
  contract_address?: string;
  status?: string;
  identity_status?: string;
  identity_evidence?: string | ProjectEvidence | Array<string | ProjectEvidence> | null;
  source_url?: string;
  url?: string;
  source?: string;
  reason?: string;
  updated_at?: string | number | null;
  [key: string]: unknown;
};

type RatingV2TargetStatus = {
  labeled?: number;
  positive?: number;
  negative?: number;
  positive_rate?: number | null;
  ready?: boolean;
  status?: string;
  reason?: string;
  metrics?: {
    brier_score?: number;
    brier_skill?: number;
    roc_auc?: number;
    mean_predicted_probability?: number;
  };
};

type RatingV2Summary = {
  mode?: string;
  readiness?: {
    training_rows?: number;
    minimum_labeled?: number;
    minimum_each_class?: number;
    any_target_ready?: boolean;
    targets?: Record<string, RatingV2TargetStatus>;
  };
  model?: {
    mode?: string;
    generated_at?: string;
    checked_at?: string;
    minimum_labeled?: number;
    minimum_each_class?: number;
    dataset_training_rows?: number;
    trained_target_count?: number;
    ready_target_count?: number;
    reused_recent_model?: boolean;
    targets?: Record<string, RatingV2TargetStatus>;
  };
};

type RatingV2Row = {
  status?: "collecting" | "scored" | string;
  score?: number | null;
  legacy_score?: number | null;
  delta?: number | null;
  probabilities?: Record<string, number | null | undefined>;
  ready_targets?: string[];
  sample_progress?: { current?: number; required?: number };
  feature_snapshot?: string;
  execution_effect?: string;
};

type TokenIntelligence = MonitorTokenIntelligence;

type RadarRow = {
  token_address?: string;
  contract_address?: string;
  symbol?: string;
  name?: string;
  chain?: string;
  chain_id?: string;
  score?: number;
  entry_score?: number;
  gold_dog_score?: number;
  cliff_risk_score?: number;
  cliff_hype_score?: number;
  cliff_gem_score?: number;
  cliff_risk_label?: string;
  cliff_hype_label?: string;
  cliff_gem_label?: string;
  freshness_score?: number;
  narrative_score?: number;
  narrative_tags?: string[];
  narrative_quality?: string;
  narrative_primary_tag?: string;
  narrative_penalty_tags?: string[];
  narrative_reasons?: string[];
  meme_seed_score?: number;
  meme_seed_matches?: Array<{
    keyword?: string;
    search_terms?: string[];
    trend?: string;
    evidence_count?: number;
    community_count?: number;
    crypto_discovered?: boolean | null;
    score?: number;
    reason?: string;
  }>;
  meme_seed_terms?: string[];
  meme_seed_candidate?: boolean;
  meme_seed_gate?: string;
  pro_signal_score?: number;
  pro_signal_tags?: string[];
  gmgn_skill_score?: number;
  gmgn_skill_tags?: string[];
  gmgn_skill_categories?: string[];
  priority_battlefields?: Array<{
    key?: string;
    label?: string;
    score?: number;
    reason?: string;
  }>;
  priority_battlefield?: string;
  priority_battlefield_label?: string;
  priority_battlefield_score?: number;
  priority_battlefield_reasons?: string[];
  old_meme_revival_score?: number;
  old_meme_revival_active?: boolean;
  old_meme_revival_reasons?: string[];
  risk_filter_score?: number;
  absorption_score?: number;
  holder_quality_score?: number;
  security_filter_score?: number;
  liquidity_age_score?: number;
  dev_trust_score?: number;
  smart_kol_score?: number;
  bot_manipulation_score?: number;
  entry_stage?: string;
  entry_level?: string;
  gold_dog_rationale?: string[];
  watch_status?: string;
  monitor_display_tier?: "discovered" | "early" | "confirmed" | "revival" | "review";
  watch_ticket_stage?: "micro" | "seed" | "early" | "confirmed" | "late" | string;
  watch_alert_eligible?: boolean;
  watch_alerted_at?: string;
  watch_alert_mcap?: number;
  watch_first_seen_at?: string;
  watch_last_seen_at?: string;
  watch_first_confirmed_at?: string;
  watch_first_confirmed_mcap?: number;
  watch_first_confirmation_active?: boolean;
  watch_v2_tier?: "discovered" | "early" | "confirmed" | "review" | string;
  watch_pipeline_role?: "first_layer" | "second_screen" | string;
  watch_discovery_layer?: "onchain_first_layer" | "aggregator_second_screen" | string;
  watch_screening_sources?: string[];
  watch_source_confirmation_sources?: string[];
  watch_source_confirmation_ok?: boolean;
  watch_source_confirmation_reason?: string;
  confirmation_status?: string;
  confirmation_reason?: string;
  confirmation_basis?: unknown;
  smart_money_confirmation?: Record<string, unknown> | null;
  watch_alert_conviction_score?: number;
  watch_scan_count?: number;
  watch_strong_scan_count?: number;
  watch_first_seen_mcap?: number;
  watch_last_seen_mcap?: number;
  watch_max_seen_mcap?: number;
  watch_mcap_multiple_from_first?: number;
  watch_gold_capture_type?: "confirmed_runner" | "review_only" | string;
  watch_achieved_gold_reason?: string;
  watch_invalid_reason?: string;
  watch_pullback_reason?: string;
  watch_recovery_reason?: string;
  watch_reason?: string;
  watch_entry_reason?: string;
  watch_alert_gate_reason?: string;
  risk_deduction_reasons?: string[];
  risk_deduction_summary?: string;
  dealer_score?: number;
  dealer_opportunity_score?: number | null;
  dealer_activity_score?: number;
  dealer_risk_status?: string;
  dealer_structure_status?: string;
  oi_value_change_1h_pct?: number;
  oi_basis?: string;
  funding_rate_8h_pct?: number;
  funding_interval_hours?: number;
  funding_settled_at_ms?: number;
  dealer_type?: string;
  dealer_label?: string;
  dealer_explain?: string;
  dealer_volume_usd?: number;
  dealer_volume_to_mcap_pct?: number;
  dealer_contract_volume_to_mcap_pct?: number;
  alpha_buyflow_score?: number;
  alpha_buyflow_label?: string;
  alpha_buyflow_explain?: string;
  alpha_buyflow_volume_usd?: number;
  alpha_buyflow_volume_to_mcap_pct?: number;
  alpha_buyflow_buy_sell_ratio?: number;
  alpha_buyflow_txns24h?: number;
  alpha_buyflow_confirmed?: boolean;
  alpha_buyflow_source?: string;
  stage?: string;
  direction?: string;
  action?: string;
  reason?: string;
  key_level?: number;
  invalid_level?: number;
  holding_text?: string;
  protection_text?: string;
  stage_change_pct?: number;
  stage_volume_to_mcap_pct?: number;
  alpha_stage?: string;
  alpha_stage_label?: string;
  alpha_type?: string;
  alpha_label?: string;
  alpha_explain?: string;
  price_usd?: number;
  live_price_source?: string;
  live_price_updated_at?: number;
  quote_status?: "fresh" | "stale" | "quarantined" | "unavailable" | string;
  quote_observed_at?: string | number | null;
  market_cap?: number;
  mcap?: number;
  fdv?: number;
  liquidity?: number;
  liquidity_usd?: number;
  dex_liquidity?: number;
  volume24h?: number;
  dex_volume24h?: number;
  heat_score?: number;
  alpha_volume24h?: number;
  change_m5?: number;
  change_h1?: number;
  change_h24?: number;
  alpha_change24h_pct?: number;
  futures_price_change24h_pct?: number;
  funding_rate_pct?: number;
  oi_change_1h_pct?: number;
  oi_value?: number;
  pair_age_hours?: number;
  top10_holder_pct?: number;
  top20_holder_pct?: number;
  max_holder_pct?: number;
  holder_source?: string;
  holder_observed_at?: string;
  holders?: number;
  smart_money?: number;
  smart_money_evidence?: SmartMoneyEvidence | null;
  kol?: number;
  potential_label?: string;
  gmgn_risk_flags?: string[];
  heat_priority?: "high" | "medium" | string;
  heat_priority_label?: string;
  heat_priority_score?: number;
  heat_priority_reason?: string;
  heat_priority_action?: string;
  heat_priority_next_step?: string;
  heat_priority_risk?: string;
  heat_priority_mcap_multiple?: number;
  early_conviction_level?: "high" | "medium" | string;
  early_conviction_label?: string;
  early_conviction_score?: number;
  early_conviction_reason?: string;
  early_conviction_action?: string;
  early_conviction_next_step?: string;
  early_conviction_risk?: string;
  early_conviction_data_status?: string;
  early_conviction_theme_tags?: string[];
  okx_kind?: string;
  okx_source_panel?: string;
  okx_signal_panel?: boolean;
  okx_signal_timestamp?: string;
  okx_signal_type?: string;
  okx_signal_type_label?: string;
  okx_wallet_type?: string;
  okx_trigger_wallet_count?: number | string;
  okx_signal_amount_usd?: number | string;
  okx_signal_sold_ratio_pct?: number | string;
  okx_signal_price_usd?: number | string;
  okx_signal_wallet_addresses?: string[];
  okx_volume_1h?: number | string;
  okx_tags?: Record<string, unknown>;
  replay_score_adjustment?: number;
  replay_calibration_level?: string;
  replay_calibration_reason?: string;
  rating_v2?: RatingV2Row;
  source_labels?: string[];
  sources?: string[];
  source?: string;
  source_family?: string;
  source_count?: number;
  source_hit_counts?: Record<string, number>;
  source_repeat_flags?: string[];
  source_groups?: string[];
  execution_arm?: string;
  route_label?: string;
  execution_candidate_score?: number;
  early_rating?: string;
  rating?: string;
  grade?: string;
  dex_url?: string;
  url?: string;
  icon_url?: string;
  logo_url?: string;
  image_url?: string;
  imageUrl?: string;
  token_icon?: string;
  logo?: string;
  icon?: string;
  futures_symbol?: string;
  recommendation_bucket?: RecommendationBucket;
  recommendation_label?: string;
  recommendation_action?: string;
  recommendation_reason?: string;
  recommendation_risk?: string;
  recommendation_next_step?: string;
  recommendation_rank?: number;
  hit_status?: string;
  return_since_first_pct?: number;
  return_1h_pct?: number;
  return_6h_pct?: number;
  return_24h_pct?: number;
  replay?: {
    first_seen_at?: string;
    latest_seen_at?: string;
    first_price_usd?: number;
    first_mcap_usd?: number;
    latest_price_usd?: number;
    return_since_first_pct?: number;
    return_1h_pct?: number;
    return_6h_pct?: number;
    return_24h_pct?: number;
    hit_status?: string;
  };
  token_intelligence?: TokenIntelligence | null;
};

type SourceStatus = {
  enabled?: boolean;
  url_count?: number;
  command_count?: number;
  file_count?: number;
  key_present?: boolean;
  mode?: string;
  needs?: string;
  freshness_level?: "green" | "yellow" | "red" | "pending" | string;
  freshness_label?: string;
  age_seconds?: number;
  last_updated_at?: string;
  last_checked_at?: string;
};

type VoiceAlertStatus = {
  enabled?: boolean;
  triggered?: boolean;
  reason?: string;
  message?: string;
  repeat_count?: number;
  last_triggered_at?: string;
  last_trigger_symbols?: string[];
  last_tested_at?: string;
  voice_file?: string;
};

type RadarReport = {
  meta?: {
    fast_track?: MonitorSummary | null;
    gmgn_skills_status?: MonitorSummary | null;
    execution_audit?: MonitorSummary | null;
    execution_challenger?: MonitorSummary | null;
    prelaunch_watch?: MonitorSummary | null;
    report_generated_at?: string;
    generated_at?: string;
    alpha_count?: number;
    meme_count?: number;
    meme_potential_count?: number;
    meme_heat_count?: number;
    meme_conviction_count?: number;
    dealer_count?: number;
    data_quality?: { overall?: string; failed_count?: number };
    holder_coverage?: { coverage_pct?: number; with_top10?: number; unique_tokens?: number };
    meme_source_status?: Record<string, SourceStatus>;
    voice_alert?: VoiceAlertStatus;
    replay?: {
      tracked_count?: number;
      actionable_count?: number;
      hit_count?: number;
      miss_count?: number;
      hit_rate_pct?: number;
      risk_avoided_count?: number;
      by_action?: Record<
        string,
        {
          count?: number;
          hit_count?: number;
          miss_count?: number;
          avoided_count?: number;
          hit_rate_pct?: number;
          avg_return_1h_pct?: number;
          avg_return_6h_pct?: number;
          avg_return_24h_pct?: number;
        }
      >;
    };
    replay_boards?: { best_hits?: RadarRow[]; worst_misses?: RadarRow[]; risk_avoided?: RadarRow[] };
    gold_watch?: {
      tracked_count?: number;
      historical_first_confirmed_count?: number;
      confirmed_runner_count?: number;
      review_only_achieved_gold_count?: number;
      current_confirmed_runner_count?: number;
      current_review_only_achieved_gold_count?: number;
      first_confirmation_success_rate_pct?: number;
      latest_first_confirmed_symbol?: string | null;
      latest_first_confirmed_at?: string | null;
      latest_confirmed_runner_symbol?: string | null;
      latest_confirmed_runner_at?: string | null;
      first_confirmed_count?: number;
      achieved_gold_count?: number;
      candidate_count?: number;
      strong_candidate_count?: number;
      pick_symbol?: string;
      confirmed_pick_symbol?: string | null;
      early_pick_symbol?: string | null;
      top_pending_reason?: string;
      min_confirmations?: number;
    };
    gold_backtest?: {
      best_strategy?: {
        name?: string;
        label?: string;
        score?: number;
        summary?: {
          count?: number;
          win_rate?: number;
          tp1_rate?: number;
          two_x_rate?: number;
          average_return_pct?: number;
          profit_factor?: number;
          average_paid_up_multiple?: number;
        };
      } | null;
      strategies?: Array<{
        name?: string;
        label?: string;
        score?: number;
        summary?: {
          count?: number;
          win_rate?: number;
          tp1_rate?: number;
          two_x_rate?: number;
          average_return_pct?: number;
          profit_factor?: number;
          average_paid_up_multiple?: number;
        };
      }>;
    };
    paper_trading?: PaperTradingSummary;
    rating_v2?: RatingV2Summary;
  };
  gold_watch_pick?: RadarRow | null;
  gold_watch_confirmed_pick?: RadarRow | null;
  gold_watch_early_pick?: RadarRow | null;
  gold_watch_alerts?: GoldWatchEvent[] | null;
  prelaunch_projects?: PrelaunchProject[] | null;
  alpha_rows?: RadarRow[];
  meme_rows?: RadarRow[];
  meme_heat_rows?: RadarRow[];
  meme_conviction_rows?: RadarRow[];
  meme_potential_rows?: RadarRow[];
  meme_shadow_rows?: RadarRow[];
  meme_watch_universe?: RadarRow[];
  monitor_intelligence_rows?: RadarRow[];
  monitor_baselines?: Record<string, MonitorDiscoveryBaseline>;
  dealer_rows?: RadarRow[];
  recommendation_rows?: RadarRow[];
  funding_rows?: RadarRow[];
  hot_rows?: RadarRow[];
  monitor_v3?: unknown;
};

type GoldWatchSummary = NonNullable<RadarReport["meta"]>["gold_watch"];
type GoldBacktestSummary = NonNullable<RadarReport["meta"]>["gold_backtest"];

type LiveAlertTier = "confirmed" | "early" | "discovered" | "runner" | "pullback" | "recovered" | "revival" | "trend_watch" | "invalidated" | "high_risk";

type GoldWatchEvent = RadarRow & {
  id?: string;
  event_id?: string;
  event_type?: string;
  alert_type?: string;
  type?: string;
  tier?: string;
  status?: string;
  confirmation_reason?: string;
  created_at?: string | number;
  occurred_at?: string | number;
  expires_at?: string | number;
  first_confirmed_at?: string | null;
  ticket_stage?: string;
  row?: RadarRow;
};

type LiveAlert = {
  id: string;
  tier: LiveAlertTier;
  row: RadarRow;
  title: string;
  reason: string;
  source: string;
  createdAt: number;
  expiresAt: number;
};

type LiveAlertSoundStatus = "未测试" | "已播" | "浏览器未放行" | "不支持";

type LiveDexQuote = {
  priceUsd?: number;
  marketCap?: number;
  fdv?: number;
  liquidityUsd?: number;
  volume24h?: number;
  changeM5?: number;
  changeH1?: number;
  changeH24?: number;
  updatedAt?: number;
};

type LiveDexQuoteTarget = {
  kind: "pair" | "token";
  chain: string;
  address: string;
};

type LiveDexQuoteMap = Record<string, LiveDexQuote>;

type ViewKey = "recommend" | "memePotential" | "meme" | "alpha" | "dealer" | "alerts" | "replay";

function legacyLiveAlertsEnabled(view: ViewKey): boolean {
  return view !== "memePotential";
}

function monitorMarketUrl(row: MonitorTokenView): string | null {
  const chain = row.identity.chain.trim().toLowerCase();
  const contractAddress = row.identity.contract_address.trim();
  if (!chain || !contractAddress || !/^[a-z0-9_-]+$/.test(chain)) return null;
  return `https://gmgn.ai/${encodeURIComponent(chain)}/token/${encodeURIComponent(contractAddress)}`;
}

function monitorAlertMarketUrl(alert: MonitorAlert): string | null {
  const chain = alert.chain.trim().toLowerCase();
  const contractAddress = alert.contract_address.trim();
  if (!chain || !contractAddress || !/^[a-z0-9_-]+$/.test(chain)) return null;
  return `https://gmgn.ai/${encodeURIComponent(chain)}/token/${encodeURIComponent(contractAddress)}`;
}

type GoldTierKey = "discovered" | "early" | "confirmed" | "revival" | "review";

const VIEW_META: Record<ViewKey, { label: string; title: string; desc: string }> = {
  recommend: {
    label: "AI结论",
    title: "AI 操作结论",
    desc: "把分数、热度、筹码、合约异动翻译成可看、等回踩、先别碰三类动作。",
  },
  memePotential: {
    label: "MEME 监控",
    title: "MEME 实时监控",
    desc: "按事件、资金流和多源共振追踪有效候选，监控与实盘策略独立。",
  },
  meme: {
    label: "Meme 热榜",
    title: "Meme 热度榜",
    desc: "市场全局 Meme 热度候选，不等于上车推荐。",
  },
  alpha: {
    label: "Alpha 妖币",
    title: "Binance Alpha 妖币榜",
    desc: "只看 Binance Alpha 名单里的项目，按涨幅、合约、费率、OI 和控盘特征识别阶段。",
  },
  dealer: {
    label: "庄家雷达",
    title: "Binance Alpha 庄家雷达",
    desc: "只在 Alpha 项目内看筹码集中、浅池、量市比和合约异动。",
  },
  alerts: {
    label: "异动提醒",
    title: "异动提醒",
    desc: "把高分跃迁、OI 抬头、费率异常集中出来。",
  },
  replay: {
    label: "复盘榜",
    title: "系统复盘榜",
    desc: "记录推荐后的表现，分出命中、失误和避险有效。",
  },
};

const SOURCE_STATUS_LABELS: Record<string, string> = {
  dexscreener_discovery: "DexScreener",
  debot: "DeBot",
  bsc_onchain: "BSC 链上新池",
  fourmeme_launchpad: "Four.meme 发射台",
  flap_launchpad: "Flap 发射台",
  gmgn_trending: "GMGN 热榜",
  gmgn_live_trending: "GMGN 实时",
  pumpfun_live: "Pump.fun 早鸟",
  pumpfun_onchain: "Pump.fun 链上",
  gmgn_trenches: "GMGN Trenches",
  okx_trenches: "OKX Trenches",
  okx_signal: "OKX 信号",
  binance_wallet_hot: "币安钱包热榜",
  binance_wallet_signal: "币安钱包信号",
  noxa_launchpad: "Noxa",
  proficy_trending: "Proficy",
  "985_monitor": "985 监控",
  "985_fomo_wallets": "985 FOMO",
  "985_smartmoney": "985 SM",
  wind_monitor: "听风",
  mobula: "Mobula",
  birdeye: "Birdeye",
};

const REPORT_REFETCH_INTERVAL_MS = 10_000;
const MONITOR_REFETCH_INTERVAL_MS = 2_000;
const LOCAL_REPORT_TIMEOUT_MS = 5_000;
const LIVE_ALERT_STARTUP_WINDOW_MS = 90_000;
const LIVE_ALERT_MAX_AGE_MS = 15 * 60_000;
const LIVE_ALERT_POPUP_STORAGE_KEY = "alpha-radar-live-popup-enabled";
const LIVE_ALERT_SOUND_STORAGE_KEY = "alpha-radar-live-sound-enabled";
const LIVE_ALERT_BROWSER_STORAGE_KEY = "alpha-radar-live-browser-enabled";
const LIVE_ALERT_GMGN_OPEN_STORAGE_KEY = "alpha-radar-live-gmgn-open-enabled";
const WALLET_ADDRESS_STORAGE_KEY = "alpha-radar-wallet-address";
const WALLET_CHAIN_STORAGE_KEY = "alpha-radar-wallet-chain";
let sharedLiveAlertAudioContext: AudioContext | null = null;
let sharedLiveAlertAudioUrl = "";
let sharedLiveAlertSilentAudioUrl = "";
let sharedLiveAlertSilentAudio: HTMLAudioElement | null = null;
let sharedLiveAlertActiveAudio: HTMLAudioElement | null = null;

function configuredReportUrl(): string {
  const params = new URLSearchParams(window.location.search);
  const queryUrl = params.get("reportUrl");
  return import.meta.env.VITE_REPORT_URL || queryUrl || "auto";
}

function reportUrlCandidates(reportUrl: string): string[] {
  if (reportUrl === "auto") {
    const localPage = window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost";
    return localPage
      ? ["http://127.0.0.1:8765/report.json", "/api/report", "/report.json"]
      : ["/api/report", "/report.json"];
  }
  if (reportUrl === "/api/report") return [reportUrl, "/report.json"];
  return [reportUrl];
}

function monitorUrlCandidates(reportUrl: string): string[] {
  return reportUrlCandidates(reportUrl).map((url) => {
    if (url === "/api/report") return "/api/monitor-v3";
    if (url.endsWith("/report.json")) return `${url.slice(0, -"/report.json".length)}/monitor-v3.json`;
    return `${url.replace(/\/$/, "")}/monitor-v3.json`;
  });
}

function withCacheBuster(url: string): string {
  const stamp = Date.now().toString();
  if (url.startsWith("http://") || url.startsWith("https://")) {
    const next = new URL(url);
    next.searchParams.set("_t", stamp);
    return next.toString();
  }
  const [path, hash] = url.split("#");
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}_t=${stamp}${hash ? `#${hash}` : ""}`;
}

async function fetchJsonWithTimeout(url: string): Promise<RadarReport> {
  const controller = new AbortController();
  const timeoutMs = url.startsWith("http://127.0.0.1") ? LOCAL_REPORT_TIMEOUT_MS : 5_000;
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(withCacheBuster(url), { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`${url} ${response.status}`);
    return response.json();
  } finally {
    window.clearTimeout(timer);
  }
}

async function fetchReport(reportUrl: string): Promise<RadarReport> {
  const urls = reportUrlCandidates(reportUrl);
  let lastError = "";
  for (const url of urls) {
    try {
      return await fetchJsonWithTimeout(url);
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
  }
  throw new Error(lastError || "report load failed");
}

async function fetchMonitorReport(reportUrl: string): Promise<RadarReport> {
  let lastError = "";
  for (const url of monitorUrlCandidates(reportUrl)) {
    try {
      return await fetchJsonWithTimeout(url);
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
  }
  throw new Error(lastError || "monitor snapshot load failed");
}

function storedBoolean(key: string, fallback: boolean): boolean {
  try {
    const value = window.localStorage.getItem(key);
    if (value === "1") return true;
    if (value === "0") return false;
  } catch {
    return fallback;
  }
  return fallback;
}

function setStoredBoolean(key: string, value: boolean) {
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch {
    // localStorage can be unavailable in private or embedded browser contexts.
  }
}

function storedText(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function setStoredText(key: string, value: string | null) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    // localStorage can be unavailable in private or embedded browser contexts.
  }
}

function formatLocalPullTime(timestamp?: number): string {
  if (!timestamp) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(timestamp);
}

function formatAge(seconds?: number): string {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 60) return "刚刚";
  if (value < 3600) return `${Math.floor(value / 60)}分钟前`;
  return `${Math.floor(value / 3600)}小时前`;
}

function voiceReasonLabel(reason?: string): string {
  if (reason === "waiting_for_first_confirmation") return "等待首次确认";
  if (reason === "voice_test") return "自测已播";
  if (reason === "disabled") return "未开启";
  if (!reason) return "已触发";
  return reason;
}

function scoreOf(row: RadarRow): number {
  return Number(row.dealer_score ?? row.score ?? 0);
}

function marketCapOf(row: RadarRow): number {
  return Number(row.market_cap ?? row.mcap ?? row.fdv ?? 0);
}

function volumeOf(row: RadarRow): number {
  const values = [row.dealer_volume_usd, row.volume24h, row.dex_volume24h, row.alpha_volume24h]
    .map(Number)
    .filter(Number.isFinite);
  return values.length ? Math.max(...values) : 0;
}

function liquidityOf(row: RadarRow): number {
  return Number(row.liquidity ?? row.liquidity_usd ?? row.dex_liquidity ?? 0);
}

function changeOf(row: RadarRow): number {
  return Number(row.change_h1 ?? row.alpha_change24h_pct ?? row.futures_price_change24h_pct ?? row.change_h24 ?? 0);
}

function changeMeta(row: RadarRow): { value?: number; label: string } {
  if (row.change_h1 !== undefined && row.change_h1 !== null) return { value: Number(row.change_h1), label: "1h" };
  if (row.alpha_change24h_pct !== undefined && row.alpha_change24h_pct !== null) {
    return { value: Number(row.alpha_change24h_pct), label: "Alpha 24h" };
  }
  if (row.futures_price_change24h_pct !== undefined && row.futures_price_change24h_pct !== null) {
    return { value: Number(row.futures_price_change24h_pct), label: "合约24h" };
  }
  if (row.change_h24 !== undefined && row.change_h24 !== null) return { value: Number(row.change_h24), label: "Dex 24h" };
  return { value: undefined, label: "--" };
}

function actionClass(bucket: RecommendationBucket): string {
  return `action-badge ${bucket}`;
}

function stageTone(row: RadarRow): string | undefined {
  const action = row.action || "";
  const stage = row.stage || row.alpha_stage_label || "";
  if (action.startsWith("做空") || stage.includes("砸盘")) return "danger";
  if (action.startsWith("多单保护") || stage.includes("跑路") || stage.includes("过热")) return "warn";
  if (action.startsWith("做多") || stage.includes("试多") || stage.includes("主升") || stage.includes("洗盘")) return "good";
  if (stage.includes("高控盘风险") || stage.includes("合约数据不足")) return "muted";
  if (stage.includes("蓄势")) return "hot";
  return undefined;
}

function stageAction(row: RadarRow): string {
  return row.action || row.stage || row.alpha_label || row.dealer_label || row.potential_label || "--";
}

function fmtMoney(value?: number): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "--";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

function fmtSignedUsd(value?: number): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return `${n >= 0 ? "+" : "-"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

function fmtPct(value?: number): string {
  if (value == null) return "--";
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return `${n >= 0 ? "+" : ""}${n.toFixed(Math.abs(n) >= 100 ? 0 : 2)}%`;
}

function plainPct(value?: number): string {
  if (value == null) return "--";
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return `${n.toFixed(1)}%`;
}

function fmtFixed(value: unknown, digits = 0): string {
  if (value == null || value === "") return "--";
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return n.toFixed(digits);
}

function fmtTime(value?: string | number): string {
  if (value == null || value === "") return "--";
  const n = Number(value);
  const date = Number.isFinite(n) ? new Date(n) : new Date(String(value));
  return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString();
}

function priceOf(row: RadarRow): number {
  return Number(row.price_usd ?? row.replay?.latest_price_usd ?? row.replay?.first_price_usd ?? 0);
}

function fmtPrice(value?: number): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "--";
  const maximumFractionDigits = n < 0.000001 ? 12 : n < 0.0001 ? 10 : n < 0.01 ? 8 : n < 1 ? 6 : n < 100 ? 4 : 2;
  const minimumFractionDigits = n < 1 ? Math.min(4, maximumFractionDigits) : 0;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits, maximumFractionDigits })}`;
}

function priceBefore(current: number, changePct?: number): number {
  const change = Number(changePct);
  if (!Number.isFinite(current) || current <= 0 || !Number.isFinite(change) || change <= -99) return current;
  return current / (1 + change / 100);
}

function tokenPriceData(row: RadarRow) {
  const current = priceOf(row);
  const change24h = Number(row.change_h24 ?? row.alpha_change24h_pct ?? row.futures_price_change24h_pct);
  return [
    { label: "24h前", price: priceBefore(current, change24h) },
    { label: "1h前", price: priceBefore(current, row.change_h1) },
    { label: "5m前", price: priceBefore(current, row.change_m5) },
    { label: "现在", price: current },
  ].filter((item) => Number.isFinite(item.price) && item.price > 0);
}

function normalizedDexChain(chain: unknown): string {
  const raw = String(chain || "").trim().toLowerCase();
  return (
    {
      bsc: "bsc",
      bnb: "bsc",
      "binance-smart-chain": "bsc",
      sol: "solana",
      solana: "solana",
      eth: "ethereum",
      ethereum: "ethereum",
      base: "base",
      robinhood: "robinhood",
      "4663": "robinhood",
    } as Record<string, string>
  )[raw] || raw;
}

function dexPairTarget(row: RadarRow): LiveDexQuoteTarget | null {
  const raw = String(row.dex_url || row.url || "").trim();
  const match = raw.match(/dexscreener\.com\/([^/?#]+)\/([^/?#]+)/i);
  if (!match) return null;
  return { kind: "pair", chain: normalizedDexChain(match[1]), address: match[2] };
}

function tokenQuoteTarget(row: RadarRow): LiveDexQuoteTarget | null {
  const chain = normalizedDexChain(row.chain || row.chain_id);
  const address = String(row.token_address || row.contract_address || "").trim();
  if (!chain || !address) return null;
  return { kind: "token", chain, address };
}

function liveQuoteTargetForRow(row: RadarRow): LiveDexQuoteTarget | null {
  return tokenQuoteTarget(row) || dexPairTarget(row);
}

function liveQuoteKey(target: LiveDexQuoteTarget): string {
  return `${target.kind}:${target.chain.toLowerCase()}:${target.address.toLowerCase()}`;
}

function liveQuoteKeyForRow(row: RadarRow): string | undefined {
  const target = liveQuoteTargetForRow(row);
  return target ? liveQuoteKey(target) : undefined;
}

function canUseDisplayLiveQuote(row: RadarRow, now = Date.now()): boolean {
  if (!isFastTracked(row)) return true;
  if (quoteIsFresh(row, now)) return false;
  return ["fresh", "stale", "unavailable"].includes(String(row.quote_status || "").toLowerCase());
}

function liveQuoteTargetsFromRows(rows: RadarRow[], limit = 80, now = Date.now()): LiveDexQuoteTarget[] {
  const seen = new Set<string>();
  const targets: LiveDexQuoteTarget[] = [];
  for (const row of rows) {
    if (!canUseDisplayLiveQuote(row, now)) continue;
    const target = liveQuoteTargetForRow(row);
    if (!target) continue;
    const key = liveQuoteKey(target);
    if (seen.has(key)) continue;
    seen.add(key);
    targets.push(target);
    if (targets.length >= limit) break;
  }
  return targets;
}

function gmgnTokenUrl(row: RadarRow): string | undefined {
  const chain = String(row.chain || row.chain_id || "").toLowerCase();
  const address = row.token_address || row.contract_address;
  const gmgnChain = ({ bsc: "bsc", solana: "sol", sol: "sol", ethereum: "eth", eth: "eth", base: "base", robinhood: "robinhood", "4663": "robinhood" } as Record<string, string>)[chain];
  return address && gmgnChain ? `https://gmgn.ai/${gmgnChain}/token/${encodeURIComponent(address)}` : undefined;
}

function openGmgnTokenPage(row: RadarRow): boolean {
  const url = gmgnTokenUrl(row);
  if (!url) return false;
  return Boolean(window.open(url, "_blank", "noopener,noreferrer"));
}

async function fetchLiveDexQuote(target: LiveDexQuoteTarget): Promise<LiveDexQuote> {
  const params = new URLSearchParams({ chain: target.chain });
  params.set(target.kind, target.address);
  const response = await fetch(`/api/dex-price?${params.toString()}`, {
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`dex price ${response.status}`);
  const payload = await response.json();
  if (!payload?.ok) throw new Error(payload?.error || "dex price empty");
  return {
    priceUsd: Number(payload.priceUsd),
    marketCap: Number(payload.marketCap),
    fdv: Number(payload.fdv),
    liquidityUsd: Number(payload.liquidityUsd),
    volume24h: Number(payload.volume24h),
    changeM5: Number(payload.changeM5),
    changeH1: Number(payload.changeH1),
    changeH24: Number(payload.changeH24),
    updatedAt: Number(payload.updatedAt || Date.now()),
  };
}

async function fetchLiveDexQuotes(targets: LiveDexQuoteTarget[]): Promise<LiveDexQuoteMap> {
  if (!targets.length) return {};
  return fetchDirectDexQuotes(targets);
}

function dexChainAliases(chain: string): Set<string> {
  const normalized = chain.toLowerCase();
  const aliases: Record<string, string[]> = {
    bnb: ["bsc"],
    bsc: ["bsc"],
    sol: ["solana"],
    solana: ["solana"],
    eth: ["ethereum", "eth"],
    ethereum: ["ethereum", "eth"],
    robinhood: ["robinhood", "4663"],
    "4663": ["robinhood", "4663"],
  };
  return new Set(aliases[normalized] || [normalized]);
}

function pairLiquidity(pair: Record<string, any>): number {
  const value = Number(pair?.liquidity?.usd);
  return Number.isFinite(value) ? value : 0;
}

function directQuoteFromPair(pair: Record<string, any>): LiveDexQuote {
  const marketCap = Number(pair.marketCap || pair.fdv);
  const fdv = Number(pair.fdv || pair.marketCap);
  return {
    priceUsd: Number(pair.priceUsd),
    marketCap,
    fdv,
    liquidityUsd: pairLiquidity(pair),
    volume24h: Number(pair?.volume?.h24),
    changeM5: Number(pair?.priceChange?.m5),
    changeH1: Number(pair?.priceChange?.h1),
    changeH24: Number(pair?.priceChange?.h24),
    updatedAt: Date.now(),
  };
}

async function fetchDirectDexQuotes(targets: LiveDexQuoteTarget[]): Promise<LiveDexQuoteMap> {
  const settled = await Promise.allSettled(targets.map(async (target) => {
    const endpoint = target.kind === "token"
      ? `https://api.dexscreener.com/latest/dex/tokens/${encodeURIComponent(target.address)}`
      : `https://api.dexscreener.com/latest/dex/pairs/${encodeURIComponent(target.chain)}/${encodeURIComponent(target.address)}`;
    const response = await fetch(endpoint, { cache: "no-store", headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(`direct dex ${response.status}`);
    const payload = await response.json();
    const aliases = dexChainAliases(target.chain);
    const pairs = (Array.isArray(payload?.pairs) ? payload.pairs : payload?.pair ? [payload.pair] : [])
      .filter((pair: Record<string, any>) => aliases.has(String(pair?.chainId || "").toLowerCase()))
      .sort((left: Record<string, any>, right: Record<string, any>) => pairLiquidity(right) - pairLiquidity(left));
    if (!pairs[0]) throw new Error("direct dex empty");
    return [liveQuoteKey(target), directQuoteFromPair(pairs[0])] as const;
  }));
  return Object.fromEntries(
    settled.flatMap((item) => item.status === "fulfilled" ? [item.value] : []),
  );
}

function monitorLiveQuoteTargets(snapshot: ReturnType<typeof parseMonitorV3Snapshot>, limit = 40): LiveDexQuoteTarget[] {
  if (!snapshot) return [];
  const selectedStates = new Set(["building", "resonating", "smart_cluster", "revival"]);
  return snapshot.tokens
    .filter((token) => token.freshness.status === "fresh" && token.active_states.some((state) => selectedStates.has(state)))
    .slice(0, limit)
    .map((token) => ({ kind: "token" as const, chain: token.identity.chain, address: token.identity.contract_address }));
}

function rowWithLiveQuote(row: RadarRow, quote?: LiveDexQuote, now = Date.now()): RadarRow {
  if (!quote || !canUseDisplayLiveQuote(row, now)) return row;
  const liveMarketCap = Number.isFinite(quote.marketCap) && Number(quote.marketCap) > 0 ? quote.marketCap : undefined;
  const liveFdv = Number.isFinite(quote.fdv) && Number(quote.fdv) > 0 ? quote.fdv : undefined;
  return {
    ...row,
    price_usd: Number.isFinite(quote.priceUsd) && Number(quote.priceUsd) > 0 ? quote.priceUsd : row.price_usd,
    live_price_source: "Dex 实时",
    live_price_updated_at: Number.isFinite(quote.updatedAt) && Number(quote.updatedAt) > 0 ? quote.updatedAt : now,
    market_cap: liveMarketCap || liveFdv || row.market_cap,
    mcap: liveMarketCap || liveFdv || row.mcap,
    fdv: liveFdv || liveMarketCap || row.fdv,
    liquidity: Number.isFinite(quote.liquidityUsd) && Number(quote.liquidityUsd) > 0 ? quote.liquidityUsd : row.liquidity,
    dex_volume24h: Number.isFinite(quote.volume24h) && Number(quote.volume24h) > 0 ? quote.volume24h : row.dex_volume24h,
    change_m5: Number.isFinite(quote.changeM5) ? quote.changeM5 : row.change_m5,
    change_h1: Number.isFinite(quote.changeH1) ? quote.changeH1 : row.change_h1,
    change_h24: Number.isFinite(quote.changeH24) ? quote.changeH24 : row.change_h24,
  };
}

function rowWithLiveQuoteMap(row: RadarRow, quotes?: LiveDexQuoteMap): RadarRow {
  const key = liveQuoteKeyForRow(row);
  return key ? rowWithLiveQuote(row, quotes?.[key]) : row;
}

function isFastTracked(row: RadarRow): boolean {
  return row.quote_status != null || row.quote_observed_at != null;
}

function validatedReportRows(report?: RadarReport): Map<string, RadarRow> {
  const rows = [...(report?.meme_rows || []), ...(report?.meme_potential_rows || []), ...(report?.meme_shadow_rows || []), ...(report?.meme_watch_universe || [])];
  return new Map(rows.filter(isFastTracked).map((row) => [alertIdentity(row), row]));
}

function quoteIsFresh(row: RadarRow, now = Date.now()): boolean {
  if (!isFastTracked(row)) return Boolean(row.live_price_source);
  const observed = evidenceTime(row.quote_observed_at);
  return row.quote_status === "fresh" && observed != null && now - observed <= 30_000 && observed <= now + 30_000;
}

function displayQuoteIsFresh(row: RadarRow, now = Date.now()): boolean {
  if (quoteIsFresh(row, now)) return true;
  const observed = evidenceTime(row.live_price_updated_at);
  return Boolean(row.live_price_source) && observed != null && now - observed <= 30_000 && observed <= now + 30_000;
}

function quoteStatusText(row: RadarRow, now = Date.now()): string {
  if (!isFastTracked(row)) return row.live_price_source || "报告快照";
  if (quoteIsFresh(row, now)) return "快速通道 · 已核验";
  if (displayQuoteIsFresh(row, now)) return row.live_price_source || "Dex 实时";
  if (row.quote_status === "quarantined") return "报价已隔离";
  if (row.quote_status === "unavailable") return "报价不可用";
  return row.quote_status === "stale" || evidenceTime(row.quote_observed_at) != null ? "报价已过期" : "报价时间未知";
}

function quoteAwareMetricText(row: RadarRow, value: string, now = Date.now()): string {
  if (!isFastTracked(row) || displayQuoteIsFresh(row, now)) return value;
  if (row.quote_status === "unavailable") return "不可用";
  if (row.quote_status === "quarantined") return "已隔离";
  if (value === "--") return "不可用";
  const status = quoteStatusText(row, now).replace(/^报价/, "");
  return `${status} · ${value}`;
}

function quoteAwarePriceText(row: RadarRow, now = Date.now()): string {
  const value = fmtPrice(priceOf(row));
  if (!isFastTracked(row) || displayQuoteIsFresh(row, now)) return value;
  if (row.quote_status === "unavailable") return "不可用";
  if (row.quote_status === "quarantined") return "已隔离";
  return value === "--" ? "不可用" : value;
}

function shortAge(hours?: number): string {
  const n = Number(hours);
  if (!Number.isFinite(n) || n <= 0) return "--";
  if (n < 24) return `${n.toFixed(0)}小时`;
  return `${(n / 24).toFixed(0)}天`;
}

function sourceText(row: RadarRow): string {
  const sources = discoverySources(row);
  return sources.length ? sources.map(sourceDisplayName).join(" / ") : "来源不可用";
}

function normalizedSource(value: unknown): string {
  return String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
}

function uniqueSources(values: unknown[]): string[] {
  const seen = new Set<string>();
  return values
    .map((value) => String(value || "").trim())
    .filter((value) => {
      const key = normalizedSource(value);
      return Boolean(key) && !seen.has(key) && Boolean(seen.add(key));
    });
}

function isProcessingLabel(value: unknown): boolean {
  return ["ds", "dexscreener", "dexscreener_market", "alpha_ai"].includes(normalizedSource(value));
}

function discoverySources(row: RadarRow): string[] {
  const direct = uniqueSources(row.sources || []);
  if (direct.length) return direct;
  const hits = uniqueSources(Object.keys(row.source_hit_counts || {}));
  if (hits.length) return hits;
  const groups = uniqueSources(row.source_groups || []);
  if (groups.length) return groups;
  return uniqueSources([
    ...(row.source_labels || []).filter((label) => !isProcessingLabel(label)),
    row.source,
    row.source_family,
  ]);
}

function discoverySourceCount(row: RadarRow): number | undefined {
  const explicit = Number(row.source_count);
  if (Number.isFinite(explicit) && explicit >= 0) return explicit;
  const groups = uniqueSources(row.source_groups || []);
  return groups.length || discoverySources(row).length || undefined;
}

function enrichmentText(row: RadarRow): string {
  const labels = new Set((row.source_labels || []).map(normalizedSource));
  return [
    labels.has("ds") || labels.has("dexscreener") || labels.has("dexscreener_market") ? "DexScreener 行情" : "",
    labels.has("alpha_ai") ? "Alpha AI 分析" : "",
  ].filter(Boolean).join(" / ") || "无已标注处理补充";
}

function screeningText(row: RadarRow): string {
  const discovery = discoverySources(row);
  const sources = [...new Set([
    ...(row.watch_screening_sources?.length ? row.watch_screening_sources : discovery.map(sourceDisplayName)),
    ...(discovery.some((label) => /^okx(?:_|$)/i.test(label)) ? ["OKX"] : []),
  ])];
  return sources.length ? sources.join(" / ") : "--";
}

function discoveryLayerText(row: RadarRow): string {
  if (row.watch_discovery_layer === "onchain_first_layer") return "链上首发";
  return "聚合二筛";
}

function pipelineText(row: RadarRow): string {
  if (row.watch_status === "achieved_gold") {
    if (row.watch_gold_capture_type === "confirmed_runner") return "首确后成狗";
    return "复盘已成狗";
  }
  const screen = screeningText(row);
  if (row.watch_pipeline_role === "first_layer") {
    return screen === "--" ? "链上首发" : `链上首发 + ${screen}`;
  }
  return screen === "--" ? "聚合二筛" : `二筛 ${screen}`;
}

function tokenInitials(row: RadarRow): string {
  const raw = String(row.symbol || row.name || "?").trim();
  const clean = raw.replace(/[^a-zA-Z0-9]/g, "");
  return (clean || "?").slice(0, 2).toUpperCase();
}

function tokenIconUrl(row: RadarRow): string {
  return String(row.icon_url || row.logo_url || row.image_url || row.imageUrl || row.token_icon || row.logo || row.icon || "").trim();
}

function tokenSubline(row: RadarRow): string {
  const symbol = String(row.symbol || "").trim().toLowerCase();
  const name = String(row.name || "").trim();
  if (name && name.toLowerCase() !== symbol) return name;
  return row.chain || row.chain_id || sourceText(row);
}

function tokenHue(row: RadarRow): number {
  const seed = String(row.symbol || row.name || "?");
  return [...seed].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
}

function riskSummary(row: RadarRow): string {
  if (row.gmgn_risk_flags?.length) return row.gmgn_risk_flags.slice(0, 2).join(" / ");
  if (row.risk_filter_score != null) return `过滤 ${fmtFixed(row.risk_filter_score)}`;
  return "--";
}

function hasSource(row: RadarRow, needle: string): boolean {
  const query = needle.toLowerCase();
  const values = [
    ...discoverySources(row),
    ...(row.watch_source_confirmation_sources || []),
  ];
  return values.some((value) => String(value || "").toLowerCase().includes(query));
}

function okxSourceText(row: RadarRow): string {
  return hasSource(row, "okx") ? "OKX已命中" : "OKX未命中";
}

function battlefieldLabel(row: RadarRow): string {
  if (row.priority_battlefield_label) return row.priority_battlefield_label;
  if (row.priority_battlefields?.length) return row.priority_battlefields.map((item) => item.label).filter(Boolean).join(" / ");
  return "--";
}

function battlefieldReasonText(row: RadarRow): string {
  if (row.priority_battlefield_reasons?.length) return row.priority_battlefield_reasons.join(" / ");
  if (row.priority_battlefields?.length) {
    return row.priority_battlefields.map((item) => item.reason).filter(Boolean).join(" / ");
  }
  return "暂无上所想象";
}

function riskDeductionText(row: RadarRow): string {
  if (row.risk_deduction_summary) return row.risk_deduction_summary;
  if (row.risk_deduction_reasons?.length) return row.risk_deduction_reasons.slice(0, 3).join(" / ");
  const items: string[] = [];
  if (row.gmgn_risk_flags?.length) items.push(...row.gmgn_risk_flags.slice(0, 2));
  if (row.narrative_penalty_tags?.length) items.push(...row.narrative_penalty_tags.slice(0, 1));
  if (Number(row.cliff_risk_score || 0) >= 35) {
    items.push(`风险${Number(row.cliff_risk_score).toFixed(0)}${row.cliff_risk_label ? `/${row.cliff_risk_label}` : ""}`);
  }
  if (row.risk_filter_score != null && row.risk_filter_score <= 60) {
    items.push(`过滤${fmtFixed(row.risk_filter_score)}`);
  }
  if (row.security_filter_score != null && row.security_filter_score <= 55) {
    items.push(`安全${fmtFixed(row.security_filter_score)}`);
  }
  if (row.holder_quality_score != null && row.holder_quality_score <= 55) {
    items.push(`筹码${fmtFixed(row.holder_quality_score)}`);
  }
  if (row.bot_manipulation_score != null && row.bot_manipulation_score <= 65) {
    items.push(`刷量${fmtFixed(row.bot_manipulation_score)}`);
  }
  if (Number(row.replay_score_adjustment || 0) < 0) {
    items.push(`回测${row.replay_score_adjustment}`);
  }
  return Array.from(new Set(items.filter(Boolean))).slice(0, 3).join(" / ") || "暂无硬伤";
}

function poolEntryReason(row: RadarRow, fallback: string): string {
  if (row.watch_entry_reason) return row.watch_entry_reason;
  if (row.watch_reason) return row.watch_reason;
  if (row.watch_source_confirmation_reason) return row.watch_source_confirmation_reason;
  if (row.watch_source_confirmation_sources?.length) return `命中 ${row.watch_source_confirmation_sources.join(" + ")}`;
  return goldDogReason(row, fallback);
}

function alertGateReason(row: RadarRow): string {
  if (row.watch_alert_gate_reason) return row.watch_alert_gate_reason;
  if (row.watch_first_confirmation_active || hasFirstConfirmation(row)) return "已进确认层";
  if (row.watch_alert_eligible === false) {
    return row.watch_invalid_reason || row.watch_achieved_gold_reason || "不满足提醒门槛";
  }
  if (row.watch_status === "achieved_gold") return "已成金狗，转复盘";
  if (row.watch_status === "invalidated") return row.watch_invalid_reason || "已失效";
  if (row.watch_status === "expired") return row.watch_invalid_reason || "窗口已过";
  if (Number(row.cliff_risk_score || 0) >= 55) return "风险过高，先不响";
  if (row.watch_source_confirmation_ok === false) return row.watch_source_confirmation_reason || "等二源确认";
  const tier = goldTierOf(row);
  if (tier === "discovered") return "发现层，不强提醒";
  if (tier === "early") return "早鸟层，等确认";
  if (tier === "review") return "偏晚/复盘，不提醒";
  return "等待首次确认";
}

function goldProgressText(row: RadarRow, minConfirmations = 3): string {
  const sourceCount = Math.max(
    new Set((row.watch_source_confirmation_sources || []).filter(Boolean)).size,
    row.watch_source_confirmation_ok ? 2 : 0,
  );
  const confirmations = Math.max(0, Number(row.watch_strong_scan_count || 0));
  const target = Math.max(1, Number(minConfirmations || 3));
  const progress = row.watch_first_confirmation_active || hasFirstConfirmation(row)
    ? `已确认 ${Math.max(confirmations, target)}/${target}`
    : `连确 ${Math.min(confirmations, target)}/${target}`;
  return `来源 ${Math.min(sourceCount, 2)}/2 · ${progress}`;
}

function narrativeText(row: RadarRow): string {
  if (row.narrative_primary_tag) return row.narrative_primary_tag;
  if (row.narrative_tags?.length) return row.narrative_tags.slice(0, 2).join(" / ");
  return "--";
}

function goldDogReason(row: RadarRow, fallback: string): string {
  if (row.gold_dog_rationale?.length) return row.gold_dog_rationale.join(" / ");
  if (row.narrative_reasons?.length) return row.narrative_reasons.join(" / ");
  return fallback;
}

function replayStatusLabel(status?: string): string {
  if (status === "hit") return "已命中";
  if (status === "miss") return "未命中";
  if (status === "avoided") return "避险有效";
  if (status === "ignored") return "已过滤";
  if (status === "watch_risk") return "风险观察";
  return "跟踪中";
}

function rowKey(row: RadarRow): string {
  const address = String(row.token_address || row.contract_address || "").toLowerCase();
  const chain = String(row.chain || row.chain_id || "").toLowerCase();
  if (address || chain) return `${address || row.symbol || ""}-${chain}`;
  return `${row.symbol || ""}-${row.name || ""}`;
}

function alertIdentity(row: RadarRow): string {
  const rawAddress = String(row.token_address || row.contract_address || "").trim();
  const address = /^0x/i.test(rawAddress) ? rawAddress.toLowerCase() : rawAddress;
  const rawChain = String(row.chain || row.chain_id || "").trim().toLowerCase();
  const chain = ({ "1": "ethereum", eth: "ethereum", "56": "bsc", bnb: "bsc", "501": "solana", sol: "solana", "8453": "base" } as Record<string, string>)[rawChain] || rawChain;
  if (address && chain) return `${chain}:${address}`;
  return "";
}

function liveAlertTierOf(row: RadarRow): LiveAlertTier | null {
  if (row.watch_status === "invalidated" || row.watch_status === "expired") {
    return "invalidated";
  }
  if (row.watch_status === "pullback") return "pullback";
  if (row.watch_alert_eligible === false) return null;
  if (row.watch_status === "achieved_gold" && row.watch_gold_capture_type === "review_only") return "runner";
  if (row.watch_status === "achieved_gold") return null;
  const tier = goldTierOf(row);
  const score = Math.max(scoreOf(row), Number(row.gold_dog_score || 0), Number(row.watch_alert_conviction_score || 0));
  if (
    tier === "confirmed" ||
    row.watch_first_confirmation_active ||
    row.watch_status === "strong_candidate"
  ) {
    return "confirmed";
  }
  if (tier === "early") return "early";
  if (tier === "discovered" && row.watch_alert_eligible === true && score >= 92) return "discovered";
  return null;
}

function liveAlertReason(row: RadarRow, tier: LiveAlertTier): string {
  if (tier === "invalidated") return row.watch_invalid_reason || row.watch_reason || "信号已失效";
  if (tier === "pullback") return row.watch_pullback_reason || row.watch_reason || "进入回撤观察，等待收复";
  if (tier === "recovered") return row.watch_recovery_reason || row.watch_reason || "回撤修复，重新观察";
  if (row.confirmation_reason) return row.confirmation_reason;
  if (row.watch_source_confirmation_reason) return row.watch_source_confirmation_reason;
  if (row.watch_entry_reason) return row.watch_entry_reason;
  if (row.watch_reason) return row.watch_reason;
  if (tier === "runner") return row.watch_achieved_gold_reason || "已快速拉成金狗，打开GMGN复盘走势和回踩";
  const source = screeningText(row);
  if (tier === "confirmed") return source === "--" ? "聚合确认，进入重点盯盘" : `${source} 聚合确认`;
  if (tier === "early") return source === "--" ? "早鸟候选，等待连续确认" : `${source} 早鸟候选`;
  return "高分首次发现，进入前端弹窗";
}

function liveAlertRank(alert: LiveAlert): number {
  if (alert.tier === "high_risk") return -1;
  if (alert.tier === "invalidated") return -1;
  if (alert.tier === "pullback") return -1;
  if (alert.tier === "recovered") return 0;
  if (alert.tier === "confirmed") return 0;
  if (alert.tier === "early") return 1;
  if (alert.tier === "trend_watch") return 2;
  if (alert.tier === "discovered") return 2;
  return 3;
}

function isNewerAlert(alert: LiveAlert, previous?: LiveAlert): boolean {
  return !previous || alert.createdAt > previous.createdAt || (alert.createdAt === previous.createdAt && liveAlertRank(alert) < liveAlertRank(previous));
}

function evidenceTime(value: unknown): number | undefined {
  if (value == null || value === "") return undefined;
  const numeric = typeof value === "number" ? value : typeof value === "string" && /^\d+(\.\d+)?$/.test(value) ? Number(value) : undefined;
  const time = numeric == null ? (typeof value === "string" ? Date.parse(value) : NaN) : numeric < 1e12 ? numeric * 1000 : numeric;
  return Number.isFinite(time) ? time : undefined;
}

function evidenceTimeText(value: unknown): string {
  const time = evidenceTime(value);
  return time == null ? "未知" : new Date(time).toLocaleString("zh-CN", { hour12: false });
}

function eventTier(event: GoldWatchEvent): LiveAlertTier | null {
  const type = event.alert_type || event.event_type || event.type || event.tier || event.status;
  if (type === "invalidated" || type === "expired") return "invalidated";
  if (type === "discovered" || type === "confirmed" || type === "early" || type === "runner" || type === "pullback" || type === "recovered" || type === "trend_watch" || type === "high_risk") return type;
  if (type) return null;
  if (event.first_confirmed_at) return "confirmed";
  return liveAlertTierOf(event.row || event);
}

function buildLiveAlerts(report?: RadarReport, now = Date.now()): LiveAlert[] {
  const rows = [
      report?.gold_watch_pick,
      report?.gold_watch_confirmed_pick,
      report?.gold_watch_early_pick,
      ...(report?.meme_potential_rows || []),
      ...(report?.meme_shadow_rows || []),
      ...(report?.meme_rows || []),
      ...(report?.meme_watch_universe || []),
    ].filter(Boolean) as RadarRow[];
  const currentRows = new Map<string, RadarRow>();
  for (const row of rows) {
    const identity = alertIdentity(row);
    currentRows.set(identity, { ...currentRows.get(identity), ...row });
  }
  // An explicit empty event list is authoritative; older reports use row transitions.
  const events: GoldWatchEvent[] = Array.isArray(report?.gold_watch_alerts)
    ? report.gold_watch_alerts
    : rows.map((row) => ({ ...row, created_at: row.watch_alerted_at || row.watch_first_confirmed_at }));
  const latest = new Map<string, LiveAlert>();
  for (const event of events) {
    const eventRow = { ...event, ...event.row };
    const identity = alertIdentity(eventRow);
    const tier = eventTier(event);
    if (!identity || !tier) continue;
    const current = currentRows.get(identity);
    if (tier !== "invalidated" && tier !== "high_risk" && current && (current.watch_status === "invalidated" || current.watch_status === "expired" || (tier === "confirmed" && current.watch_alert_eligible === false))) continue;
    const row = { ...eventRow, ...current, ...(tier === "invalidated" ? { watch_status: "invalidated" } : {}) };
    const createdAt = evidenceTime(event.occurred_at ?? event.created_at ?? event.first_confirmed_at);
    if (createdAt == null || createdAt > now + 60_000) continue;
    const expiresAt = Math.min(createdAt + LIVE_ALERT_MAX_AGE_MS, evidenceTime(event.expires_at) ?? Infinity);
    const alert: LiveAlert = {
      id: `${tier}:${identity}:${createdAt}`,
      tier,
      row,
      title: `${tier === "confirmed" && row.confirmation_status ? confirmationLabel(row) : liveAlertTierLabel(tier)} · ${row.symbol || "--"}`,
      reason: (tier === "confirmed" ? event.confirmation_reason || row.confirmation_reason : undefined) || event.reason || liveAlertReason(row, tier),
      source: screeningText(row),
      createdAt,
      expiresAt,
    };
    const previous = latest.get(identity);
    if (isNewerAlert(alert, previous)) latest.set(identity, alert);
  }
  const reportAt = evidenceTime(report?.meta?.fast_track?.updated_at) ?? evidenceTime(report?.meta?.report_generated_at || report?.meta?.generated_at);
  if (reportAt != null && now - reportAt > LIVE_ALERT_MAX_AGE_MS) return [];
  return [...latest.values()].filter((alert) => alert.expiresAt > now)
    .sort((a, b) => liveAlertRank(a) - liveAlertRank(b) || b.createdAt - a.createdAt);
}

function startupLiveAlerts(alerts: LiveAlert[], now = Date.now()): LiveAlert[] {
  return alerts.filter((alert) => alert.createdAt >= now - LIVE_ALERT_STARTUP_WINDOW_MS);
}

async function liveAudioContext(): Promise<AudioContext | null> {
  const AudioContextCtor = window.AudioContext || (window as Window & typeof globalThis & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextCtor) return null;
  if (!sharedLiveAlertAudioContext || sharedLiveAlertAudioContext.state === "closed") {
    sharedLiveAlertAudioContext = new AudioContextCtor();
  }
  if (sharedLiveAlertAudioContext.state === "suspended") {
    await sharedLiveAlertAudioContext.resume();
  }
  return sharedLiveAlertAudioContext;
}

async function playLiveAlertSound(tier: LiveAlertTier): Promise<boolean> {
  await unlockLiveAlertAudio();
  const context = await liveAudioContext();
  let webAudioStarted = false;
  if (context && context.state === "running") {
    const now = context.currentTime;
    const tones = tier === "confirmed" ? [880, 1100, 1320, 1560] : tier === "early" ? [760, 960, 1120] : [660, 860];
    tones.forEach((frequency, index) => {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const start = now + index * 0.16;
      oscillator.type = tier === "confirmed" ? "square" : "sine";
      oscillator.frequency.setValueAtTime(frequency, start);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(tier === "confirmed" ? 0.42 : 0.34, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.15);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.17);
    });
    webAudioStarted = true;
  }

  const embeddedAudioStarted = await playEmbeddedLiveAlertAudio();
  return webAudioStarted || embeddedAudioStarted;
}

async function unlockLiveAlertAudio(): Promise<boolean> {
  const context = await liveAudioContext();
  const silentStarted = await armLiveAlertSilentAudio();
  return Boolean(context && context.state === "running") || silentStarted || "Audio" in window;
}

function liveAlertAudioUrl(): string {
  if (sharedLiveAlertAudioUrl) return sharedLiveAlertAudioUrl;

  const sampleRate = 44_100;
  const durationSeconds = 1.15;
  const sampleCount = Math.floor(sampleRate * durationSeconds);
  const bytesPerSample = 2;
  const headerSize = 44;
  const buffer = new ArrayBuffer(headerSize + sampleCount * bytesPerSample);
  const view = new DataView(buffer);

  const writeString = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
  };

  writeString(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * bytesPerSample, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 8 * bytesPerSample, true);
  writeString(36, "data");
  view.setUint32(40, sampleCount * bytesPerSample, true);

  for (let i = 0; i < sampleCount; i += 1) {
    const t = i / sampleRate;
    const segment = Math.floor(t / 0.18);
    const frequency = [880, 1175, 1480, 1175, 1760, 1320][segment] || 990;
    const envelope = Math.min(1, t * 18) * Math.min(1, (durationSeconds - t) * 10);
    const pulse = Math.sin(2 * Math.PI * frequency * t) + 0.35 * Math.sin(2 * Math.PI * frequency * 2 * t);
    const value = Math.max(-1, Math.min(1, pulse * envelope * 0.72));
    view.setInt16(headerSize + i * bytesPerSample, value * 32767, true);
  }

  sharedLiveAlertAudioUrl = URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
  return sharedLiveAlertAudioUrl;
}

async function playEmbeddedLiveAlertAudio(): Promise<boolean> {
  try {
    const audio = new Audio(liveAlertAudioUrl());
    sharedLiveAlertActiveAudio = audio;
    audio.volume = 1;
    audio.preload = "auto";
    await audio.play();
    return true;
  } catch {
    return false;
  }
}

function speakLiveAlertText(text: string): boolean {
  if (!("speechSynthesis" in window)) return false;
  try {
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = 0.92;
    utterance.pitch = 1.08;
    utterance.volume = 1;
    window.speechSynthesis.speak(utterance);
    return true;
  } catch {
    return false;
  }
}

function speakLiveAlert(tier: LiveAlertTier, symbol = "") : boolean {
  const label = liveAlertTierLabel(tier);
  const text = symbol ? `${symbol}，${label}` : tier === "discovered" ? "金狗来了，注意观察" : label;
  return speakLiveAlertText(text);
}

function silentLiveAlertAudioUrl(): string {
  if (sharedLiveAlertSilentAudioUrl) return sharedLiveAlertSilentAudioUrl;

  const sampleRate = 8_000;
  const durationSeconds = 0.25;
  const sampleCount = Math.floor(sampleRate * durationSeconds);
  const bytesPerSample = 2;
  const headerSize = 44;
  const buffer = new ArrayBuffer(headerSize + sampleCount * bytesPerSample);
  const view = new DataView(buffer);

  const writeString = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
  };

  writeString(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * bytesPerSample, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 8 * bytesPerSample, true);
  writeString(36, "data");
  view.setUint32(40, sampleCount * bytesPerSample, true);

  for (let i = 0; i < sampleCount; i += 1) {
    view.setInt16(headerSize + i * bytesPerSample, 1, true);
  }

  sharedLiveAlertSilentAudioUrl = URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
  return sharedLiveAlertSilentAudioUrl;
}

async function armLiveAlertSilentAudio(): Promise<boolean> {
  try {
    if (!sharedLiveAlertSilentAudio) {
      const audio = new Audio(silentLiveAlertAudioUrl());
      audio.loop = true;
      audio.volume = 0.001;
      audio.preload = "auto";
      audio.setAttribute("aria-hidden", "true");
      sharedLiveAlertSilentAudio = audio;
    }
    await sharedLiveAlertSilentAudio.play();
    return true;
  } catch {
    return false;
  }
}

function stopLegacyLiveAlertAudio(): void {
  for (const audio of [sharedLiveAlertSilentAudio, sharedLiveAlertActiveAudio]) {
    if (!audio) continue;
    audio.pause();
    try {
      audio.currentTime = 0;
    } catch {
      // Some embedded browsers expose a read-only currentTime before metadata loads.
    }
  }
  sharedLiveAlertActiveAudio = null;
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  if (sharedLiveAlertAudioContext?.state === "running") {
    void sharedLiveAlertAudioContext.suspend().catch(() => undefined);
  }
}

const LIVE_ALERT_VOICE_FILES: Partial<Record<LiveAlertTier, string>> = {
  discovered: "/audio/alpha-gold-discovered.wav",
  early: "/audio/alpha-gold-discovered.wav",
  confirmed: "/audio/alpha-signal-confirmed.wav",
  runner: "/audio/alpha-gold-discovered.wav",
  pullback: "/audio/alpha-pullback.wav",
  recovered: "/audio/alpha-recovered.wav",
  invalidated: "/audio/alpha-signal-invalidated.wav",
  high_risk: "/audio/alpha-signal-invalidated.wav",
};

async function playLiveAlertVoice(tier: LiveAlertTier, voiceFile?: string): Promise<boolean> {
  try {
    const localUrl = voiceFile ? `http://127.0.0.1:8765/voice/${encodeURIComponent(voiceFile)}` : "";
    const audioUrl = localUrl || LIVE_ALERT_VOICE_FILES[tier];
    if (!audioUrl) return false;
    const audio = new Audio(audioUrl);
    sharedLiveAlertActiveAudio = audio;
    audio.volume = 1;
    audio.preload = "auto";
    await audio.play();
    return true;
  } catch {
    return false;
  }
}

function firstConfirmationMcapOf(row: RadarRow): number {
  return Number(row.watch_first_confirmed_mcap || row.watch_alert_mcap || 0);
}

function firstSeenMcapOf(row: RadarRow): number {
  return Number(row.watch_first_seen_mcap || row.replay?.first_mcap_usd || 0);
}

function compactHours(hours: unknown): string {
  if (hours == null || hours === "") return "未记录";
  const value = Number(hours);
  if (!Number.isFinite(value) || value < 0) return "未记录";
  if (value < 1) return `${Math.max(1, Math.round(value * 60))}分钟`;
  if (value < 24) return `${value < 10 ? value.toFixed(1) : value.toFixed(0)}小时`;
  return `${(value / 24).toFixed(1)}天`;
}

function firstSeenTimeOf(row: RadarRow): string {
  const value = row.watch_first_seen_at || row.replay?.first_seen_at;
  return value ? evidenceTimeText(value) : "未记录";
}

function hasFirstConfirmation(row: RadarRow): boolean {
  return Boolean(firstConfirmationMcapOf(row) || row.watch_first_confirmed_at || row.watch_alerted_at);
}

function firstConfirmationText(row: RadarRow, currentMcap = marketCapOf(row)): string {
  const firstConfirmedMcap = firstConfirmationMcapOf(row);
  if (firstConfirmedMcap) return `${fmtMoney(firstConfirmedMcap)}${mcapMultipleText(currentMcap, firstConfirmedMcap)}`;
  if (row.watch_source_confirmation_ok) {
    return row.watch_status === "achieved_gold" ? "来源已确认 / 未留首确价" : "来源已确认";
  }
  return "未确认";
}

function mcapMultipleText(current: number, base: number): string {
  if (!current || !base) return "";
  const multiple = current / base;
  return ` / ${multiple >= 10 ? multiple.toFixed(1) : multiple.toFixed(2)}x`;
}

function backendGoldTier(row: RadarRow): GoldTierKey | null {
  if (
    row.watch_v2_tier === "discovered" ||
    row.watch_v2_tier === "early" ||
    row.watch_v2_tier === "confirmed" ||
    row.watch_v2_tier === "review"
  ) {
    return row.watch_v2_tier;
  }
  return null;
}

function watchStageLabel(row: RadarRow): string {
  if (row.watch_status === "invalidated") return "已失效";
  if (row.watch_status === "expired") return "已过期";
  if (row.monitor_display_tier) return goldTierCopy(row.monitor_display_tier).label;
  const backendTier = backendGoldTier(row);
  const visibleTier = backendTier ? goldTierOf(row) : null;
  if (visibleTier === "confirmed") return "聚合确认";
  if (visibleTier === "early") return "聚合早鸟";
  if (visibleTier === "discovered") return "聚合发现";
  if (visibleTier === "review") return hasFirstConfirmation(row) ? "已确认复盘" : "复盘";
  if (row.confirmation_status) return confirmationLabel(row);
  if (row.watch_status === "achieved_gold") {
    return row.watch_gold_capture_type === "confirmed_runner" ? "首确后成狗" : "复盘已成狗";
  }
  if (row.watch_status === "invalidated") return "已失效";
  if (row.watch_status === "expired") return "已过期";
  if (row.watch_ticket_stage === "late") return "已偏晚";
  if (row.watch_status === "strong_candidate") return row.watch_first_confirmation_active ? "聚合确认" : "强候选复盘";
  if (row.watch_status === "early_candidate") return "聚合早鸟";
  if (row.watch_status === "seed_pool") return "聚合发现";
  const tier = goldTierOf(row);
  if (tier === "confirmed") return "聚合确认";
  if (tier === "early") return "聚合早鸟";
  if (tier === "discovered") return "聚合发现";
  if (row.watch_ticket_stage === "micro") return "微盘后台";
  if (row.watch_ticket_stage === "late") return "已偏晚";
  return row.entry_stage || "--";
}

const FALLBACK_EARLY_MAX_AGE_HOURS = 6;

function fallbackDiscoveryAgeHours(row: RadarRow, nowMs: number): number | null {
  const firstSeenAt = row.watch_first_seen_at || row.replay?.first_seen_at;
  if (!firstSeenAt) return null;
  const firstSeenMs = Date.parse(firstSeenAt);
  if (!Number.isFinite(firstSeenMs) || firstSeenMs > nowMs) return null;
  return (nowMs - firstSeenMs) / 3_600_000;
}

function goldTierOf(row: RadarRow, nowMs = Date.now()): GoldTierKey {
  if (row.monitor_display_tier) return row.monitor_display_tier;
  const backendTier = backendGoldTier(row);
  if (backendTier) {
    if (backendTier === "review") return backendTier;
    if (isFastTracked(row) && !quoteIsFresh(row, nowMs)) return "review";
    const pairAgeHours = row.pair_age_hours == null ? Number.NaN : Number(row.pair_age_hours);
    const lastSeenAt = row.watch_last_seen_at || row.watch_first_seen_at || row.replay?.latest_seen_at || row.replay?.first_seen_at;
    const lastSeenMs = lastSeenAt ? Date.parse(lastSeenAt) : Number.NaN;
    const observationAgeHours = Number.isFinite(lastSeenMs) && lastSeenMs <= nowMs
      ? (nowMs - lastSeenMs) / 3_600_000
      : Number.POSITIVE_INFINITY;
    if (!Number.isFinite(pairAgeHours) || pairAgeHours < 0 || pairAgeHours > FALLBACK_EARLY_MAX_AGE_HOURS) return "review";
    if (observationAgeHours > FALLBACK_EARLY_MAX_AGE_HOURS) return "review";
    return backendTier;
  }
  if (row.watch_status) {
    if (row.watch_status === "achieved_gold") return "review";
    if (row.watch_status === "strong_candidate" && row.watch_alert_eligible !== false) {
      return row.watch_first_confirmation_active ? "confirmed" : "review";
    }
    if (row.watch_status === "early_candidate") return "early";
    if (row.watch_status === "seed_pool") return "discovered";
    if (row.watch_status === "invalidated" || row.watch_status === "expired") return "review";
    if (row.watch_ticket_stage === "seed" || row.watch_ticket_stage === "micro") return "discovered";
    if (row.watch_ticket_stage === "early") return "early";
    return "review";
  }
  if (row.watch_first_confirmation_active) return "confirmed";
  if (row.watch_ticket_stage === "late") return "review";
  if (row.old_meme_revival_active) return "revival";
  const pairAgeHours = Number(row.pair_age_hours);
  const discoveryAgeHours = fallbackDiscoveryAgeHours(row, nowMs);
  if (!Number.isFinite(pairAgeHours) || pairAgeHours < 0) return "review";
  if (pairAgeHours > FALLBACK_EARLY_MAX_AGE_HOURS) return "review";
  if (discoveryAgeHours === null || discoveryAgeHours > FALLBACK_EARLY_MAX_AGE_HOURS) return "review";
  const cap = marketCapOf(row);
  if (cap > 500_000) return "review";
  return "discovered";
}

function goldTierCopy(tier: GoldTierKey) {
  if (tier === "discovered") {
    return {
      label: "聚合发现",
      range: "6小时内新池 · 本系统首次捕捉",
      rule: "这里只表示新池被聚合源首次捕捉；不是买入确认，经过二筛后才进入早鸟或确认。",
    };
  }
  if (tier === "early") {
    return {
      label: "聚合早鸟",
      range: "多链新池早期动量",
      rule: "早期候选，等待连续确认；各链按实际接入来源评估。",
    };
  }
  if (tier === "revival") {
    return {
      label: "老币复活",
      range: "老池重新点火",
      rule: "72小时以上老池放量启动；只盯回踩承接和二次放量，不和首发新池混用。",
    };
  }
  if (tier === "review") {
    return {
      label: "已成/偏晚复盘",
      range: "老票 / 失效 / 已暴涨",
      rule: "只复盘，不当新机会；保留首次确认市值便于看倍数。",
    };
  }
  return {
    label: "聚合确认",
    range: "本轮新确认",
    rule: "聚合源首次确认；需要有流动性、二源确认且风险不过高。",
  };
}

function goldTierEmptyText(tier: GoldTierKey): string {
  if (tier === "confirmed") {
    return "当前监控链暂无同时满足流动性、核心二源、连续确认和风险过滤的项目";
  }
  if (tier === "early") {
    return "当前监控链暂无处在早期区间且已过二源、等待连续确认的项目";
  }
  if (tier === "discovered") {
    return "当前监控链暂无新的聚合发现";
  }
  if (tier === "revival") {
    return "当前没有满足老池放量、多源重新点火和风险过滤的复活票";
  }
  return "暂无复盘票";
}

function heatPriorityOf(row: RadarRow): {
  level: string;
  label: string;
  score: number;
  reason: string;
  action: string;
  nextStep: string;
  risk: string;
  mcapMultiple: number;
} {
  const level = String(row.heat_priority || "").toLowerCase();
  const explicit = {
    level,
    label: row.heat_priority_label || (level === "high" ? "重点热度" : level === "medium" ? "热度抬头" : ""),
    score: Number(row.heat_priority_score || 0),
    reason: row.heat_priority_reason || "",
    action: row.heat_priority_action || "",
    nextStep: row.heat_priority_next_step || "",
    risk: row.heat_priority_risk || "",
    mcapMultiple: Number(row.heat_priority_mcap_multiple || 0),
  };
  if (level) return explicit;

  const sourceCount = discoverySourceCount(row) || 0;
  const h1 = Number(row.change_h1 || 0);
  const m5 = Number(row.change_m5 || 0);
  const mcap = marketCapOf(row);
  const volume = volumeOf(row);
  const volumeToMcap = mcap > 0 ? volume / mcap : 0;
  const score = Number(row.heat_score || 0);
  const reasons = [
    sourceCount >= 3 ? `${sourceCount}源共振` : "",
    score >= 100 ? `热度 ${score.toFixed(0)}` : "",
    h1 >= 30 ? `1h +${h1.toFixed(0)}%` : "",
    volumeToMcap >= 1.5 ? `量市比 ${volumeToMcap.toFixed(1)}x` : "",
  ].filter(Boolean);
  const qualifies = sourceCount >= 3 && (h1 >= 50 || m5 >= 20 || volumeToMcap >= 1.5 || score >= 100);
  if (!qualifies) return { level: "", label: "", score: 0, reason: "", action: "", nextStep: "", risk: "", mcapMultiple: 0 };
  const priority = sourceCount >= 4 && (h1 >= 60 || score >= 140 || volumeToMcap >= 3) ? "high" : "medium";
  return {
    level: priority,
    label: priority === "high" ? "重点热度" : "热度抬头",
    score,
    reason: reasons.join(" / "),
    action: priority === "high" ? "热度升级 · 等回踩" : "热度观察",
    nextStep: priority === "high" ? "已拉升，不追第一根；打开GMGN看买卖流，等回踩不破或二次放量。" : "继续看多源是否延续，等买盘和池子同步确认。",
    risk: priority === "high" ? "这是热度升级观察，不是首次发现买入票。" : "单轮热度可能退潮，暂不升级为确认票。",
    mcapMultiple: 0,
  };
}

function earlyConvictionOf(row: RadarRow): {
  level: string;
  label: string;
  score: number;
  reason: string;
  action: string;
  nextStep: string;
  risk: string;
  dataStatus: string;
} {
  const level = String(row.early_conviction_level || "").toLowerCase();
  if (level) {
    return {
      level,
      label: row.early_conviction_label || (level === "high" ? "早期重点" : "早期关注"),
      score: Number(row.early_conviction_score || 0),
      reason: row.early_conviction_reason || "",
      action: row.early_conviction_action || "",
      nextStep: row.early_conviction_next_step || "",
      risk: row.early_conviction_risk || "",
      dataStatus: row.early_conviction_data_status || "",
    };
  }
  return { level: "", label: "", score: 0, reason: "", action: "", nextStep: "", risk: "", dataStatus: "" };
}

function signal(row: RadarRow): { label: string; tone: string } {
  if (row.dealer_risk_status === "blocked") return { label: "风险拦截", tone: "danger" };
  const conviction = earlyConvictionOf(row);
  if (conviction.level === "high") return { label: conviction.label, tone: "hot" };
  if (conviction.level === "medium") return { label: conviction.label, tone: "warn" };
  const heat = heatPriorityOf(row);
  if (heat.level === "high") return { label: heat.label, tone: "hot" };
  if (heat.level === "medium") return { label: heat.label, tone: "warn" };
  if (row.dealer_risk_status === "unknown") return { label: "筹码待补 · 仅观察", tone: "muted" };
  if (row.action || row.stage) {
    return { label: stageAction(row), tone: stageTone(row) || "muted" };
  }
  if (row.dealer_label) {
    const tone =
      row.dealer_type === "distribution" || row.dealer_type === "holder_risk"
        ? "danger"
        : row.dealer_type === "pump"
          ? "hot"
          : "warn";
    return { label: row.dealer_label, tone };
  }
  if (row.alpha_label) {
    const tone =
      row.alpha_type === "distribution" || row.alpha_type === "selloff"
        ? "danger"
        : row.alpha_stage === "overheated"
          ? "warn"
          : row.alpha_type === "high_control_pump" || row.alpha_type === "hot_pump"
            ? "hot"
            : "muted";
    return { label: row.alpha_label, tone };
  }
  const top10 = Number(row.top10_holder_pct || 0);
  const funding = Math.abs(Number(row.funding_rate_pct || 0));
  const oi = Number(row.oi_change_1h_pct || 0);
  const change = changeOf(row);
  const score = scoreOf(row);
  if (top10 >= 55 || Number(row.max_holder_pct || 0) >= 20) return { label: "高控盘风险", tone: "danger" };
  if (change >= 8 && volumeOf(row) >= 30_000) return { label: "拉盘型", tone: "hot" };
  if (change <= -8 && volumeOf(row) >= 30_000) return { label: "出货型", tone: "danger" };
  if (oi >= 5 || funding >= 0.05) return { label: "合约异动", tone: "warn" };
  if (score >= 72) return { label: "重点观察", tone: "good" };
  return { label: "观察", tone: "muted" };
}

function recommendation(row: RadarRow): { bucket: RecommendationBucket; action: string; reason: string; risk: string } {
  if (row.recommendation_bucket) return {
    bucket: row.recommendation_bucket,
    action: row.recommendation_action || "观察",
    reason: row.recommendation_reason || "",
    risk: row.recommendation_risk || "",
  };
  if (row.action && row.stage) {
    const tone = stageTone(row);
    return {
      bucket: tone === "danger" ? "danger" : tone === "warn" ? "pullback" : row.action.startsWith("做多") ? "lead" : "ambush",
      action: row.action,
      reason: row.reason || row.alpha_explain || "阶段模型已给出动作。",
      risk: row.protection_text || row.holding_text || "只读阶段雷达，不自动下单。",
    };
  }
  if (row.watch_status === "strong_candidate") {
    return {
      bucket: "lead",
      action: "金狗确认票",
      reason: row.watch_reason || goldDogReason(row, "连续强信号确认"),
      risk: row.recommendation_risk || "只读雷达确认票，继续按风险过滤和复盘结果观察。",
    };
  }
  if (row.watch_status === "achieved_gold") {
    return {
      bucket: "pullback",
      action: "已成金狗",
      reason: row.watch_achieved_gold_reason || row.watch_reason || "已经完成金狗级别放大。",
      risk: "不再当早期上车票，放入复盘命中和趋势观察。",
    };
  }
  if (row.watch_status === "invalidated" || row.watch_status === "expired") {
    return {
      bucket: "danger",
      action: row.watch_status === "invalidated" ? "确认失效" : "过期复盘",
      reason: row.watch_invalid_reason || row.watch_reason || goldDogReason(row, "候选已经过确认窗口"),
      risk: "不再当作金狗确认票，只留后台复盘。",
    };
  }
  if (row.watch_status === "early_candidate") {
    return {
      bucket: "ambush",
      action: "早期候选",
      reason: row.watch_reason || goldDogReason(row, "30K-100K 区间已连续 2 次确认"),
      risk: row.recommendation_risk || "早期候选只展示不强提醒，继续看买盘、池子和叙事能否延续。",
    };
  }
  return {
    bucket: "pullback",
    action: "未评估",
    reason: "后端尚未提供版本化判断。",
    risk: "缺少完整依据，不生成前端交易结论。",
  };
}

function uniqueRows(rows: RadarRow[]): RadarRow[] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    const key = rowKey(row);
    if (!key.trim() || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function mergeGoldCandidateRows(rows: RadarRow[]): RadarRow[] {
  const mergedByIdentity = new Map<string, RadarRow>();
  const order: string[] = [];
  for (const row of rows) {
    const key = rowKey(row);
    if (!key.trim()) continue;
    const existing = mergedByIdentity.get(key);
    if (!existing) {
      mergedByIdentity.set(key, row);
      order.push(key);
      continue;
    }

    const merged = { ...row, ...existing } as RadarRow;
    const mergedRecord = merged as unknown as Record<string, unknown>;
    for (const [field, value] of Object.entries(row)) {
      if (!field.startsWith("watch_") && !field.startsWith("confirmation_")) continue;
      if (value === undefined || value === null || value === "") continue;
      const current = mergedRecord[field];
      if (current === undefined || current === null || current === "") mergedRecord[field] = value;
    }
    if (row.old_meme_revival_active === true) merged.old_meme_revival_active = true;
    mergedByIdentity.set(key, merged);
  }
  return order.map((key) => mergedByIdentity.get(key) as RadarRow);
}

function monitorTierForSource(row: RadarRow, nowMs = Date.now()): GoldTierKey {
  if (row.watch_status === "invalidated" || row.watch_status === "expired" || row.watch_status === "achieved_gold") {
    return "review";
  }
  const pairAgeHours = Number(row.pair_age_hours);
  const lastSeenAt = row.watch_last_seen_at || row.watch_first_seen_at || row.replay?.latest_seen_at || row.replay?.first_seen_at;
  const lastSeenMs = lastSeenAt ? Date.parse(lastSeenAt) : Number.NaN;
  const observationIsCurrent = Number.isFinite(lastSeenMs)
    && lastSeenMs <= nowMs
    && nowMs - lastSeenMs <= FALLBACK_EARLY_MAX_AGE_HOURS * 3_600_000;
  if (row.old_meme_revival_active === true && Number.isFinite(pairAgeHours) && pairAgeHours >= 72 && observationIsCurrent) {
    return "revival";
  }
  const backendTier = backendGoldTier(row);
  if (!backendTier || backendTier === "review") return "review";
  const { monitor_display_tier: _displayTier, ...backendRow } = row;
  return goldTierOf(backendRow as RadarRow, nowMs);
}

function goldCandidateRowsFromReport(report?: RadarReport): RadarRow[] {
  const trackedRows = validatedReportRows(report);
  const validated = (row: RadarRow) => ({ ...row, ...trackedRows.get(alertIdentity(row)) });
  const currentRows = [
    ...(report?.meme_potential_rows || []).map((row) => ({ row, source: "potential" as const })),
    ...(report?.meme_rows || []).map((row) => ({ row, source: "discovery" as const })),
    ...(report?.meme_watch_universe || []).map((row) => ({ row, source: "watch" as const })),
  ].map(({ row }) => {
    const current = validated(row);
    return { ...current, monitor_display_tier: monitorTierForSource(current) };
  });
  const currentKeys = new Set(currentRows.map(rowKey));
  const reviewRows = [
    ...(report?.meme_shadow_rows || []),
    ...(report?.meme_conviction_rows || []),
    ...(report?.meme_heat_rows || []),
  ]
    .map(validated)
    .filter((row) => !currentKeys.has(rowKey(row)))
    .map((row) => ({ ...row, monitor_display_tier: "review" as GoldTierKey }));
  return mergeGoldCandidateRows([...currentRows, ...reviewRows]);
}

function usePreparedRows(report?: RadarReport) {
  return useMemo(() => {
    const trackedRows = validatedReportRows(report);
    const withValidatedRow = (row: RadarRow) => ({ ...row, ...trackedRows.get(alertIdentity(row)) });
    const meme = uniqueRows((report?.meme_rows || []).map(withValidatedRow)).sort((a, b) => scoreOf(b) - scoreOf(a));
    const watchPicks = uniqueRows(
      [
        report?.gold_watch_pick,
        report?.gold_watch_confirmed_pick,
        report?.gold_watch_early_pick,
      ].filter(Boolean).map((row) => withValidatedRow(row as RadarRow)),
    );
    const primaryGoldKey = watchPicks.length ? rowKey(watchPicks[0]) : "";
    const memePotentialRows = mergeGoldCandidateRows([...watchPicks, ...goldCandidateRowsFromReport(report)]);
    const tierRank: Record<GoldTierKey, number> = { confirmed: 0, early: 1, discovered: 2, revival: 3, review: 4 };
    const memePotential = memePotentialRows
      .map((row) => ({ row, rec: recommendation(row) }))
      .sort((a, b) => {
        const aTier = goldTierOf(a.row);
        const bTier = goldTierOf(b.row);
        const tierDelta = tierRank[aTier] - tierRank[bTier];
        if (tierDelta) return tierDelta;
        if (aTier === "confirmed" && primaryGoldKey) {
          const primaryDelta = Number(rowKey(b.row) === primaryGoldKey) - Number(rowKey(a.row) === primaryGoldKey);
          if (primaryDelta) return primaryDelta;
        }
        return scoreOf(b.row) - scoreOf(a.row);
      });
    const alpha = uniqueRows((report?.alpha_rows || []).map(withValidatedRow)).sort((a, b) => scoreOf(b) - scoreOf(a));
    const dealer = uniqueRows((report?.dealer_rows || []).map(withValidatedRow)).sort((a, b) => scoreOf(b) - scoreOf(a));
    const backendRecommendations = uniqueRows((report?.recommendation_rows || []).map(withValidatedRow));
    const alerts = uniqueRows([...(report?.funding_rows || []), ...(report?.hot_rows || []), ...dealer].map(withValidatedRow))
      .filter((row) => signal(row).tone !== "muted")
      .sort((a, b) => scoreOf(b) - scoreOf(a));
    const recommendSource = backendRecommendations.length ? backendRecommendations : uniqueRows([...alpha, ...dealer]);
    const recommend = recommendSource
      .map((row) => ({ row, rec: recommendation(row) }))
      .sort(
        (a, b) =>
          (a.row.recommendation_rank || 999) - (b.row.recommendation_rank || 999) ||
          scoreOf(b.row) - scoreOf(a.row),
      );
    return { meme, memePotential, alpha, dealer, alerts, recommend };
  }, [report]);
}

function MiniTape({ rows }: { rows: RadarRow[] }) {
  const data = rows.slice(0, 12).map((row, index) => ({
    name: row.symbol || String(index + 1),
    score: scoreOf(row),
  }));
  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={data} margin={{ top: 8, right: 0, left: -28, bottom: 0 }}>
        <defs>
          <linearGradient id="scoreFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#f8b951" stopOpacity={0.28} />
            <stop offset="100%" stopColor="#f8b951" stopOpacity={0.01} />
          </linearGradient>
        </defs>
        <XAxis dataKey="name" tick={{ fill: "#7f8b9d", fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fill: "#7f8b9d", fontSize: 10 }} axisLine={false} tickLine={false} />
        <Tooltip contentStyle={{ background: "#111820", border: "1px solid #2d3745", color: "#d7dde7" }} />
        <Area type="monotone" dataKey="score" stroke="#f8b951" fill="url(#scoreFill)" strokeWidth={2} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function TokenPricePanel({ row }: { row: RadarRow }) {
  const liveTarget = liveQuoteTargetForRow(row);
  const liveQuote = useQuery({
    queryKey: ["live-dex-quote", liveTarget?.kind, liveTarget?.chain, liveTarget?.address],
    queryFn: () => fetchLiveDexQuote(liveTarget as LiveDexQuoteTarget),
    enabled: Boolean(liveTarget) && !quoteIsFresh(row),
    retry: 0,
    staleTime: 10_000,
    refetchInterval: 15_000,
    refetchIntervalInBackground: true,
  });
  const liveRow = rowWithLiveQuote(row, liveQuote.data);
  const data = isFastTracked(liveRow) && !displayQuoteIsFresh(liveRow) ? [] : tokenPriceData(liveRow);
  const change24h = Number(liveRow.change_h24 ?? liveRow.alpha_change24h_pct ?? liveRow.futures_price_change24h_pct);
  const priceSource = isFastTracked(liveRow) ? quoteStatusText(liveRow) : liveQuote.data
    ? "Dex 实时"
    : liveQuote.isFetching
      ? "更新中"
      : liveTarget
        ? "报告价格"
        : "无实时源";
  return (
    <section className="token-price-panel">
      <div className="token-price-head">
        <div>
          <span>{isFastTracked(liveRow) && !displayQuoteIsFresh(liveRow) ? "最近记录价格" : "当前价格"}</span>
          <strong>{quoteAwarePriceText(liveRow)}</strong>
          <small className={displayQuoteIsFresh(liveRow) ? "live-price-source active" : "live-price-source"}>{priceSource}</small>
          {isFastTracked(liveRow) && <small>采集 {evidenceTimeText(liveRow.quote_observed_at)}</small>}
        </div>
        <div className="token-price-meta">
          <b className={Number(liveRow.change_m5 || 0) >= 0 ? "up" : "down"}>5m {quoteAwareMetricText(liveRow, fmtPct(liveRow.change_m5))}</b>
          <b className={Number(liveRow.change_h1 || 0) >= 0 ? "up" : "down"}>1h {quoteAwareMetricText(liveRow, fmtPct(liveRow.change_h1))}</b>
          <b className={change24h >= 0 ? "up" : "down"}>{liveRow.change_h24 != null ? "Dex" : liveRow.alpha_change24h_pct != null ? "Alpha" : "合约"} 24h {quoteAwareMetricText(liveRow, fmtPct(change24h))}</b>
        </div>
      </div>
      <div className="token-chart">
        {data.length ? (
          <ResponsiveContainer width="100%" height={150}>
            <AreaChart data={data} margin={{ top: 10, right: 4, left: -28, bottom: 0 }}>
              <defs>
                <linearGradient id="tokenPriceFill" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor="#647eff" stopOpacity={0.24} />
                  <stop offset="100%" stopColor="#647eff" stopOpacity={0.01} />
                </linearGradient>
              </defs>
              <XAxis dataKey="label" tick={{ fill: "#7f8b9d", fontSize: 10 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: "#7f8b9d", fontSize: 10 }} axisLine={false} tickLine={false} tickFormatter={fmtPrice} width={68} />
              <Tooltip
                formatter={(value) => [fmtPrice(Number(value)), "价格"]}
                contentStyle={{ background: "#111820", border: "1px solid #2d3745", color: "#d7dde7" }}
              />
              <Area type="monotone" dataKey="price" stroke="#647eff" fill="url(#tokenPriceFill)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="token-chart-empty">暂无价格数据</div>
        )}
      </div>
      <div className="token-price-foot">
        <span>市值 {quoteAwareMetricText(liveRow, fmtMoney(marketCapOf(liveRow)))}</span>
        <span>成交 {quoteAwareMetricText(liveRow, fmtMoney(volumeOf(liveRow)))}</span>
        <span>池子 {quoteAwareMetricText(liveRow, fmtMoney(liquidityOf(liveRow)))}</span>
      </div>
    </section>
  );
}

function StatCard({ label, value, detail, tone = "" }: { label: string; value: string; detail: string; tone?: string }) {
  return (
    <section className="stat-card">
      <span>{label}</span>
      <strong className={tone}>{value}</strong>
      <small>{detail}</small>
    </section>
  );
}

function evidenceNumber(value: unknown): number | undefined {
  if (value == null || typeof value === "boolean" || (typeof value !== "number" && typeof value !== "string") || String(value).trim() === "") return undefined;
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function evidenceValue(value: unknown, money = false): string {
  const number = evidenceNumber(value);
  return number == null ? "未知" : money ? new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", notation: "compact", minimumFractionDigits: 0, maximumFractionDigits: 2,
  }).format(number) : number.toLocaleString("zh-CN");
}

function evidenceText(value: unknown): string {
  return typeof value === "string" && value.trim() ? value : "未知";
}

function SmartMoneySummary({ evidence, compact = false }: { evidence?: SmartMoneyEvidence | null; compact?: boolean }) {
  if (!evidence || !Object.keys(evidence).length) {
    return <span className={`radar-smart-money ${compact ? "compact" : ""}`}><strong>尚未采集钱包证据</strong><span>等待 GMGN / OKX 钱包交易数据</span></span>;
  }
  const gated = evidence?.wallet_verification === "profit_history_gated";
  const confirmation = gated ? evidence?.confirmation : undefined;
  const attributed = Array.isArray(evidence?.unique_wallets);
  const walletCount = evidenceNumber(evidence?.unique_wallet_count)
    ?? (Array.isArray(evidence?.unique_wallets) ? evidence.unique_wallets.length : evidenceNumber(evidence?.unique_wallets));
  const hasFlow = evidence?.flow_event_count == null || Number(evidence.flow_event_count) > 0;
  const matches = evidence?.watchlist_matches || evidence?.watchlist?.matched_wallets;
  const watchCount = matches ? matches.length : evidenceNumber(evidence?.watchlist_wallets);
  const freshness = ({ fresh: "新鲜", stale: "已过期", unknown: "未知" } as Record<string, string>)[(gated ? evidence?.qualified_freshness : evidence?.freshness) || ""];
  const quality = gated ? "历史盈利及多币流水核验 · 独立性未核验" : evidence?.wallet_verification === "source_attributed_address_only" ? "来源归因地址 · 独立性未核验" : evidenceText(evidence?.quality);
  const candidateBuyCount = evidenceNumber(evidence?.candidate_buy_wallet_count) ?? 0;
  const candidateSellCount = evidenceNumber(evidence?.candidate_sell_wallet_count) ?? 0;
  const qualifiedWalletCount = evidenceNumber(evidence?.qualified_wallet_count) ?? 0;
  const verifiedBuyCount = evidenceNumber(evidence?.candidate_verified_buy_wallet_count ?? confirmation?.buy_wallet_count) ?? 0;
  const measuredCandidateFlow = evidenceNumber(evidence?.candidate_net_flow_usd ?? evidence?.net_flow_usd ?? evidence?.net_buy_usd);
  const candidateFlow = hasFlow || measuredCandidateFlow === 0 ? measuredCandidateFlow : undefined;
  const displayFlow = gated ? evidenceNumber(confirmation?.net_flow_usd) : candidateFlow;
  const flowLabel = gated ? "盈利钱包净流入" : "普通钱包净流入";
  const metric = (value: unknown, money = false) => evidenceNumber(value) == null ? "未采集" : evidenceValue(value, money);
  const state = gated
    ? verifiedBuyCount > 0 ? `已核验买入 ${verifiedBuyCount}`
      : candidateBuyCount > 0 ? `候选买入 ${candidateBuyCount} · 盈利待核验`
        : candidateSellCount > 0 ? `仅见候选卖出 ${candidateSellCount}` : "未发现已核验买入"
    : candidateBuyCount > 0 ? `候选买入 ${candidateBuyCount} · 盈利待核验`
      : candidateSellCount > 0 ? `仅见候选卖出 ${candidateSellCount}`
        : (walletCount || 0) > 0 ? `钱包归因 ${walletCount} · 暂无新买入`
          : displayFlow != null && displayFlow < 0 ? "观察到净流出" : "未发现钱包交易";
  return (
    <span className={`radar-smart-money ${compact ? "compact" : ""}`}>
      <strong>{gated ? `聪明钱 · ${state}` : `平台钱包 · ${state}`}</strong>
      {gated ? <span>已核验盈利钱包 {qualifiedWalletCount} · 本次核验买入 {verifiedBuyCount}</span>
        : <span>独立钱包 {metric(evidence?.independent_wallets)} · 去重钱包 {walletCount ?? 0}</span>}
      <span className={displayFlow == null ? "" : displayFlow > 0 ? "up" : displayFlow < 0 ? "down" : ""}>{flowLabel} {metric(displayFlow, true)}</span>
      {!compact && <>
        <span>候选买入 {metric(evidence?.candidate_buy_usd, true)} · 候选卖出 {metric(evidence?.candidate_sell_usd, true)} · 关注钱包 {watchCount ?? 0}</span>
        {attributed && !gated && <span>新鲜钱包 {metric(evidence?.fresh_unique_wallet_count)} · 关联簇 {evidence?.linked_clusters?.length ?? 0}</span>}
        <span>质量 {quality === "未知" ? "尚未完成盈利核验" : quality}{evidence?.reason ? ` · ${evidence.reason}` : ""}</span>
        <small>证据时间 {evidenceTimeText(gated ? evidence?.qualified_latest_evidence_at : evidence?.latest_evidence_at || evidence?.updated_at)}{freshness ? ` · ${freshness}` : ""}</small>
      </>}
    </span>
  );
}

type WalletEvidenceRow = {
  address: string;
  labels: string[];
  sources: string[];
  buys: number;
  sells: number;
  buyUsd: number;
  sellUsd: number;
};

function walletAddressList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item).trim()).filter(Boolean) : [];
}

function sourceDisplayName(value: string): string {
  const labels: Record<string, string> = {
    gmgn_skills_smartmoney: "GMGN Skills",
    gmgn_profitable_wallet_trades: "GMGN盈利钱包",
    okx_signal: "OKX信号",
    okx_trenches: "OKX Trenches",
    "985_monitor": "985监控",
    "985_fomo_wallets": "985 FOMO",
    wind_monitor: "听风监控",
    proficy_trending: "Proficy",
    proficy: "Proficy",
    gmgn_trending: "GMGN热榜",
  };
  return labels[value] || SOURCE_STATUS_LABELS[value] || value.split("_").join(" ");
}

function walletEvidenceRows(row: RadarRow): WalletEvidenceRow[] {
  const evidence = row.smart_money_evidence;
  if (!evidence) return [];
  const map = new Map<string, WalletEvidenceRow>();
  const ensure = (address: string) => {
    const key = address.toLowerCase();
    const current = map.get(key) || { address, labels: [], sources: [], buys: 0, sells: 0, buyUsd: 0, sellUsd: 0 };
    map.set(key, current);
    return current;
  };
  const addLabel = (address: string, label: string) => {
    const current = ensure(address);
    if (!current.labels.includes(label)) current.labels.push(label);
  };
  walletAddressList(evidence.qualified_wallets).forEach((address) => addLabel(address, "已核验聪明钱"));
  walletAddressList(evidence.candidate_buy_wallets).forEach((address) => addLabel(address, "候选买入"));
  walletAddressList(evidence.candidate_sell_wallets).forEach((address) => addLabel(address, "候选卖出"));
  walletAddressList(row.okx_signal_wallet_addresses).forEach((address) => addLabel(address, "OKX触发"));
  walletAddressList(evidence.unique_wallets).forEach((address) => addLabel(address, "来源归因"));
  (Array.isArray(evidence.events) ? evidence.events : []).forEach((raw) => {
    if (!raw || typeof raw !== "object") return;
    const event = raw as Record<string, unknown>;
    const addresses = walletAddressList(event.wallets);
    const sources = Array.isArray(event.provenance)
      ? event.provenance.map((item) => item && typeof item === "object" ? String((item as Record<string, unknown>).source || "") : "").filter(Boolean)
      : [];
    addresses.forEach((address) => {
      const current = ensure(address);
      sources.forEach((source) => {
        const label = sourceDisplayName(source);
        if (!current.sources.includes(label)) current.sources.push(label);
      });
      const side = String(event.direction || "").toLowerCase();
      const amount = Number(event.amount_usd || 0);
      if (side === "buy") { current.buys += 1; current.buyUsd += Number.isFinite(amount) ? amount : 0; }
      if (side === "sell") { current.sells += 1; current.sellUsd += Number.isFinite(amount) ? amount : 0; }
    });
  });
  return [...map.values()].sort((a, b) =>
    Number(b.labels.includes("已核验聪明钱")) - Number(a.labels.includes("已核验聪明钱"))
    || b.buys - a.buys
    || b.buyUsd - a.buyUsd
    || a.address.localeCompare(b.address),
  );
}

function shortWallet(address: string): string {
  return address.length > 14 ? `${address.slice(0, 6)}...${address.slice(-6)}` : address;
}

function EvidenceOverview({ row }: { row: RadarRow }) {
  const evidence = row.smart_money_evidence;
  const wallets = walletEvidenceRows(row);
  const qualified = wallets.filter((wallet) => wallet.labels.includes("已核验聪明钱"));
  const attributed = wallets.filter((wallet) => !wallet.labels.includes("已核验聪明钱"));
  const candidateBuys = wallets.filter((wallet) => wallet.labels.includes("候选买入"));
  const candidateSells = wallets.filter((wallet) => wallet.labels.includes("候选卖出"));
  const walletBuys = wallets.reduce((total, wallet) => total + wallet.buys, 0);
  const walletSells = wallets.reduce((total, wallet) => total + wallet.sells, 0);
  const walletBuyUsd = wallets.reduce((total, wallet) => total + wallet.buyUsd, 0);
  const walletSellUsd = wallets.reduce((total, wallet) => total + wallet.sellUsd, 0);
  const walletSources = [...new Set(wallets.flatMap((wallet) => wallet.sources))];
  const discovered = discoverySources(row);
  const discoveredKeys = new Set(discovered.map(normalizedSource));
  const sourceEntries = Object.entries(row.source_hit_counts || {})
    .filter(([source]) => !discoveredKeys.size || discoveredKeys.has(normalizedSource(source)))
    .sort((a, b) => b[1] - a[1]);
  const sourceCount = discoverySourceCount(row);
  const narrative = [...new Set([...(row.narrative_tags || []), ...(row.early_conviction_theme_tags || [])])].filter(Boolean);
  const narrativeReasons = row.narrative_reasons || [];
  const missing = [
    !narrative.length ? "叙事证据" : "",
    !qualified.length ? "已核验聪明钱" : "",
    !wallets.length ? "钱包证据" : "",
    row.kol != null && Number(row.kol) > 0 ? "" : "KOL数量",
    row.top10_holder_pct == null ? "Top10筹码" : "",
    row.max_holder_pct == null ? "最大钱包" : "",
  ].filter(Boolean);
  return (
    <section className="evidence-overview" aria-label="信号证据摘要">
      <div className="evidence-block route-block">
        <div className="evidence-title"><strong>当前通道</strong><span>{row.execution_candidate_score == null ? "评级只排序" : `执行分 ${fmtFixed(row.execution_candidate_score)}`}</span></div>
        <div className="evidence-chips"><span className="evidence-chip hot">{row.route_label || (row.execution_arm === "narrative_breakout" ? "大叙事突破" : row.execution_arm === "first_discovery" ? "首次发现小仓" : "发现层观察")}</span><span className="evidence-chip">评级 {row.early_rating || row.rating || row.grade || "未标注"}</span></div>
        <p>评级用于排序，通道负责决定是小仓试探、重点提醒还是继续观察。</p>
      </div>
      <div className="evidence-block narrative-block">
        <div className="evidence-title"><strong>叙事与催化</strong><span>{row.narrative_score == null ? "未评分" : `叙事分 ${fmtFixed(row.narrative_score)}`}</span></div>
        <div className="evidence-chips">{narrative.length ? narrative.map((tag) => <span key={tag} className="evidence-chip hot">{tag}</span>) : <span className="evidence-empty">未识别到明确叙事</span>}</div>
        <p>{narrativeReasons.length ? narrativeReasons.join(" / ") : battlefieldReasonText(row) || row.early_conviction_reason || "当前没有可展开的叙事依据。"}</p>
      </div>
      <div className="evidence-block">
        <div className="evidence-title"><strong>发现来源</strong><span>{sourceCount == null ? "来源数不可用" : `${sourceCount} 个独立发现来源`}</span></div>
        <div className="evidence-chips">{sourceEntries.length ? sourceEntries.slice(0, 8).map(([source, count]) => <span key={source} className="evidence-chip">{sourceDisplayName(source)} ×{count}</span>) : discovered.length ? discovered.map((source) => <span key={source} className="evidence-chip">{sourceDisplayName(source)}</span>) : <span className="evidence-empty">发现来源不可用</span>}</div>
        <p>{row.source_repeat_flags?.length ? `重复命中：${row.source_repeat_flags.join(" / ")}` : `处理补充：${enrichmentText(row)}。不计入独立发现来源。`}</p>
      </div>
      <div className="evidence-block wallet-block">
        <div className="evidence-title"><strong>聪明钱证据</strong><span>{qualified.length} 个已核验 · {candidateBuys.length} 个候选买入</span></div>
        <div className="evidence-chips">{qualified.length ? <span className="evidence-chip good">已核验聪明钱 ×{qualified.length}</span> : <span className="evidence-empty">暂未通过历史盈利核验</span>}<span className="evidence-chip">候选买入 ×{candidateBuys.length}</span>{candidateSells.length > 0 && <span className="evidence-chip">候选卖出 ×{candidateSells.length}</span>}<span className="evidence-chip">买入 {walletBuys} 笔 {evidenceValue(walletBuyUsd, true)}</span>{walletSells > 0 && <span className="evidence-chip">卖出 {walletSells} 笔 {evidenceValue(walletSellUsd, true)}</span>}</div>
        <p>{walletSources.length ? `钱包来源：${walletSources.join(" / ")}。` : "来源暂未提供可验证的钱包归因。"} 原始地址仅用于后台去重、盈利核验和回测。</p>
      </div>
      <div className="evidence-block wallet-block">
        <div className="evidence-title"><strong>KOL / 外部来源</strong><span>KOL标注 {row.kol ?? "未知"}</span></div>
        <div className="evidence-chips">{row.kol != null && Number(row.kol) > 0 && <span className="evidence-chip hot">平台标注 KOL ×{row.kol}</span>}<span className="evidence-chip">外部归因钱包 ×{attributed.length}</span>{walletSources.length > 0 && <span className="evidence-chip">来源 ×{walletSources.length}</span>}{walletBuys > 0 && <span className="evidence-chip">带来买入 {walletBuys} 笔</span>}</div>
        <p>{attributed.length ? "已记录来源归因和交易行为，前端隐藏原始地址，只保留聚合结果。" : "未提供可核验的KOL或外部钱包证据。"}</p>
      </div>
      <div className={`evidence-block missing-block ${missing.length ? "has-missing" : "complete"}`}>
        <div className="evidence-title"><strong>{missing.length ? "缺失 / 待核验" : "数据完整度"}</strong><span>{missing.length ? `${missing.length} 项待补` : "核心字段齐全"}</span></div>
        <div className="evidence-chips">{missing.length ? missing.map((item) => <span key={item} className="evidence-chip missing">{item}</span>) : <span className="evidence-chip good">核心证据已齐</span>}</div>
        <p>{evidence?.reason || "钱包、来源、叙事和风险会在后续刷新中继续核验。"}</p>
      </div>
    </section>
  );
}

function monitorIssueLabel(reason: string): string {
  const labels: Record<string, string> = {
    missing_quote: "缺少报价",
    source_quote_unavailable: "报价不可用",
    missing_quote_timestamp: "缺少报价时间",
    stale_quote: "报价已过期",
    source_quote_stale: "报价已过期",
    future_quote: "报价时间异常",
    source_quote_quarantined: "报价已隔离",
    missing_token_identity: "代币身份缺失",
    missing_pool_identity: "池子身份缺失",
    pool_changed_pending_verification: "池子变更待核验",
    invalid_price: "价格无效",
    missing_or_low_liquidity: "流动性不足",
  };
  if (labels[reason]) return labels[reason];
  if (/timeout|timed out/i.test(reason)) return "来源超时";
  return "其他问题";
}

function MonitorIssues({ errors, compact = false }: { errors: unknown; compact?: boolean }) {
  const entries = Array.isArray(errors) ? errors.map((reason) => ["", reason] as const)
    : errors && typeof errors === "object" ? Object.entries(errors)
      : typeof errors === "string" && errors.trim() ? [["", errors] as const] : [];
  const issues = entries.map(([identity, value]) => {
    const record = value && typeof value === "object" ? value as Record<string, unknown> : undefined;
    const reason = evidenceText(record ? record.reason || record.error || record.message : value);
    return { identity: identity || (record ? evidenceText(record.key || record.source) : ""), reason, label: monitorIssueLabel(reason) };
  });
  if (!issues.length) return null;
  const counts = new Map<string, number>();
  issues.forEach(({ label }) => counts.set(label, (counts.get(label) || 0) + 1));
  const groups = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const visible = groups.slice(0, 3);
  const remaining = groups.slice(3).reduce((sum, [, count]) => sum + count, 0);
  const countsView = <span className="radar-issue-counts">{visible.map(([label, count]) => <span key={label}>{label} <b>{count}</b></span>)}{remaining > 0 && <span>其余原因 <b>{remaining}</b></span>}</span>;
  if (compact) return countsView;
  return <div className="radar-monitor-issues">
    {countsView}
    <details className="radar-issue-details">
      <summary>问题明细 · {issues.length} 项</summary>
      <div className="radar-issue-scroll" tabIndex={0} role="region" aria-label="监测问题明细">
        <ul>{issues.map((issue, index) => <li key={`${issue.identity}-${index}`}>
          <span>{issue.identity || `来源 ${index + 1}`}</span>
          <span>{issue.label}<code>{issue.reason}</code></span>
        </li>)}</ul>
      </div>
    </details>
  </div>;
}

function confirmationLabel(row: RadarRow): string {
  return ({
    confirmed_wallet_evidence: "钱包买入确认",
    confirmed_platform_fallback: "标签共振",
    pending_wallet_evidence: "钱包证据待补",
    blocked_sell_dominance: "卖出占优 · 已阻断",
    waiting_fresh_market: "等待新鲜行情",
  } as Record<string, string>)[row.confirmation_status || ""] || "确认依据未知";
}

function ConfirmationEvidence({ row }: { row: RadarRow }) {
  return <span className="radar-confirmation">
    <strong>{confirmationLabel(row)}</strong>
    <span>{row.confirmation_reason || row.watch_source_confirmation_reason || "确认依据未知"}</span>
  </span>;
}

function MonitorSummaryView({ title, summary, fields }: { title: string; summary?: MonitorSummary | null; fields: Array<[string, string]> }) {
  const statusLabels: Record<string, string> = { ok: "已更新", pending_quotes: "等待报价", ignored_non_advancing_observation: "等待新观测" };
  const status = summary?.rate_limited === true ? "限流退避" : summary?.stale === true ? "已过期" : summary?.ok === false ? "异常" : summary?.ok === true ? "正常" : statusLabels[summary?.status || ""] || evidenceText(summary?.status);
  return <div className="radar-monitor-summary">
    <h3>{title} <small>{status}</small></h3>
    <dl>{fields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{key === "status" && typeof summary?.[key] === "string" ? (statusLabels[summary[key] as string] || summary[key] as string) : typeof summary?.[key] === "boolean" ? (summary[key] ? "是" : "否") : evidenceValue(summary?.[key], key.endsWith("_usd"))}</dd></div>)}</dl>
    {(summary?.summary || summary?.reason) && <p>{summary?.summary || summary?.reason}</p>}
    <MonitorIssues errors={summary?.errors} />
    <small>更新 {evidenceTimeText(summary?.updated_at)}</small>
  </div>;
}

function safeEvidenceUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}

function ProjectEvidenceView({ evidence }: { evidence: string | ProjectEvidence }) {
  if (typeof evidence === "string") return <span>{evidence}</span>;
  const url = safeEvidenceUrl(evidence.url || evidence.source_url);
  const label = evidence.title || evidence.label || evidence.reason || evidence.source || "证据来源";
  return url ? <a href={url} target="_blank" rel="noopener noreferrer">{label}<ExternalLink size={12} /></a> : <span>{label}</span>;
}

function RadarMonitoringView({ report }: { report?: RadarReport }) {
  const projects = report?.prelaunch_projects;
  const fastTrack = report?.meta?.fast_track;
  const fastAt = evidenceTime(fastTrack?.updated_at);
  const fastStatus: MonitorSummary | undefined = fastTrack ? { ...fastTrack, stale: fastTrack.stale === true || (fastAt != null && Date.now() - fastAt > 30_000) } : undefined;
  const gmgnExecution: MonitorSummary | undefined = fastTrack?.gmgn_execution && typeof fastTrack.gmgn_execution === "object"
    ? (() => {
      const summary = fastTrack.gmgn_execution as MonitorSummary;
      const config = summary.configuration && typeof summary.configuration === "object" ? summary.configuration as Record<string, unknown> : {};
      return { ...summary, ...config };
    })()
    : undefined;
  const audit = report?.meta?.execution_audit;
  const gmgnSkills = report?.meta?.gmgn_skills_status;
  const execution: MonitorSummary | undefined = audit ? {
    ...audit,
    updated_at: (audit.at ?? audit.updated_at) as string | undefined,
    errors: audit.quote_issues ?? audit.errors,
    open_count: Array.isArray(audit.positions) ? audit.positions.length : undefined,
    pending_count: Array.isArray(audit.pending_orders) ? audit.pending_orders.length : undefined,
    gate_count: Array.isArray(audit.candidate_gates) ? audit.candidate_gates.length : undefined,
  } : undefined;
  const legacy = audit?.legacy_audit && typeof audit.legacy_audit === "object" ? audit.legacy_audit as Record<string, unknown> : undefined;
  const findings = Array.isArray(legacy?.findings) ? legacy.findings as Array<{ flags?: string[] }> : undefined;
  return <section className="radar-monitoring" aria-label="监控总览">
    <details className="radar-overview-details">
    <summary className="radar-overview-summary">
      <strong>监控总览</strong>
      <span>快速通道 {fastStatus?.rate_limited === true ? "限流退避" : fastStatus?.stale ? "已过期" : fastStatus?.ok === true ? "正常" : fastStatus?.ok === false ? "异常" : "未知"}</span>
      <span>本轮抓取 <b>{evidenceValue(fastStatus?.tracked_count)}</b></span>
      <span>有效报价 <b>{evidenceValue(fastStatus?.fresh_count)}</b></span>
      <span>待估值 <b>{evidenceValue(execution?.pending_valuations)}</b></span>
      <span>已实现盈亏 <b>{evidenceValue(execution?.realized_pnl_usd, true)}</b></span>
      <span>历史收益：待核验</span>
      <ChevronDown size={14} aria-hidden="true" />
      <MonitorIssues errors={execution?.errors} compact />
      <MonitorIssues errors={fastStatus?.errors} compact />
    </summary>
    <div className="radar-overview-body">
    <div className="radar-monitoring-grid">
      <MonitorSummaryView title="快速通道" summary={fastStatus} fields={[["tracked_count", "本轮抓取"], ["fresh_count", "有效报价"], ["universe_count", "候选总数"], ["target_interval_seconds", "目标间隔 / 秒"], ["elapsed_seconds", "本轮耗时 / 秒"]]} />
      <MonitorSummaryView title="GMGN 执行桥 · 预览" summary={gmgnExecution} fields={[["status", "状态"], ["count", "执行意向"], ["submitted", "已提交"], ["wallet_address_configured", "地址已配置"], ["buy_amount_configured", "金额已配置"], ["slippage_configured", "滑点已配置"]]} />
      <MonitorSummaryView title="GMGN 候选聪明钱" summary={gmgnSkills} fields={[["smartmoney_rows", "实时交易"], ["smartmoney_unique_wallet_count", "去重钱包"], ["smartmoney_unique_token_count", "覆盖代币"], ["smartmoney_buy_wallet_count", "买入钱包"], ["smartmoney_sell_wallet_count", "卖出钱包"]]} />
      <MonitorSummaryView title="严格执行核验 · 模拟账本" summary={execution} fields={[["cash_usd", "现金"], ["equity_usd", "权益"], ["realized_pnl_usd", "已实现盈亏"], ["pending_valuations", "待估值"], ["open_count", "模拟持仓"], ["pending_count", "待执行意向"], ["accepted_quotes", "有效报价"], ["gate_count", "条件未通过"]]} />
      <MonitorSummaryView title="回踩 / 老币复活 · 独立试验" summary={report?.meta?.execution_challenger} fields={[["status", "状态"], ["cash_usd", "现金"], ["equity_usd", "权益"], ["realized_pnl_usd", "已实现盈亏"], ["open_count", "模拟持仓"], ["pending_count", "待执行意向"], ["observation_count", "前向观察数"], ["pending_valuations", "待估值"]]} />
    </div>
    <p className="radar-validation-pending">历史收益：待核验{findings ? ` · 缺报价退出 ${findings.filter((finding) => finding.flags?.includes("missing_quote_fallback_exit")).length} · 异常执行跳价 ${findings.filter((finding) => finding.flags?.includes("abnormal_execution_jump")).length}` : " · 可用回放证据未知"}</p>
    {Array.isArray(projects) && <section className="radar-prelaunch" aria-label="未发射项目">
      <h3>未发射项目 <small>{projects.length}</small></h3>
      {report?.meta?.prelaunch_watch && <MonitorSummaryView title="来源监测" summary={{ ...report.meta.prelaunch_watch, updated_at: report.meta.prelaunch_watch.checked_at as string | undefined }} fields={[["candidate_count", "候选"], ["source_count", "来源"]]} />}
      {projects.length ? projects.map((project, index) => {
        const url = safeEvidenceUrl(project.post_url || project.source_url || project.url);
        const evidence = project.identity_evidence;
        const items = evidence == null ? [] : Array.isArray(evidence) ? evidence : [evidence];
        return <article key={project.id || `${url || project.name}-${index}`}>
          <div><strong>{project.project || project.name || project.symbol || "名称未知"}</strong><small>{project.stage || project.status || "阶段未知"} · 分数 {evidenceValue(project.score)}</small></div>
          <div><span>身份核验：{project.official_verified === false ? "官方未核验" : project.identity_status || "未知"}</span><small>{project.contract_address || (project.has_contract === true ? "来源含合约 · 归属未核验" : project.has_contract === false ? "未提供合约" : "合约未知")}</small></div>
          <div className="radar-project-evidence">{items.length ? items.map((item, i) => <ProjectEvidenceView key={i} evidence={item} />) : <span>身份依据未知</span>}{project.matched_signals?.length ? <small>线索匹配：{project.matched_signals.join(" / ")}</small> : null}{(project.text || project.reason) && <small>{project.text || project.reason}</small>}</div>
          <div>{url ? <a href={url} target="_blank" rel="noopener noreferrer">{project.source || "查看来源"}<ExternalLink size={13} /></a> : <span>来源未知</span>}<small>{evidenceTimeText(project.observed_at || project.updated_at)}</small></div>
        </article>;
      }) : <p>暂无未发射项目记录</p>}
    </section>}
    </div>
    </details>
  </section>;
}

function GoldWatchSummaryStrip({ summary }: { summary?: GoldWatchSummary }) {
  if (!summary) return null;
  return (
    <div className="gold-watch-summary-strip">
      <StatCard
        label="本轮首确"
        value={String(summary.first_confirmed_count || 0)}
        detail={summary.confirmed_pick_symbol ? `当前 ${summary.confirmed_pick_symbol}` : "暂无当前首确"}
        tone={summary.first_confirmed_count ? "hot" : ""}
      />
      <StatCard
        label="历史首确"
        value={String(summary.historical_first_confirmed_count || 0)}
        detail={summary.latest_first_confirmed_symbol ? `最近 ${summary.latest_first_confirmed_symbol}` : "尚无记录"}
      />
      <StatCard
        label="首确后成狗"
        value={String(summary.confirmed_runner_count || 0)}
        detail={summary.latest_confirmed_runner_symbol ? `最近 ${summary.latest_confirmed_runner_symbol}` : "等待样本"}
        tone={summary.confirmed_runner_count ? "good" : ""}
      />
      <StatCard
        label="复盘已成狗"
        value={String(summary.review_only_achieved_gold_count || 0)}
        detail="发现时已偏晚"
      />
      <StatCard
        label="首确成功率"
        value={plainPct(summary.first_confirmation_success_rate_pct)}
        detail="首确后跑成金狗"
        tone={Number(summary.first_confirmation_success_rate_pct || 0) > 0 ? "good" : ""}
      />
    </div>
  );
}

function GoldBacktestStrip({ backtest, paperTrading }: { backtest?: GoldBacktestSummary; paperTrading?: PaperTradingSummary }) {
  if (!backtest && !paperTrading) return null;
  const arms = [
    { key: "first_discovery", label: "首次发现·严格", note: "45分钟内入场" },
    { key: "first_discovery_shadow", label: "首次发现·影子", note: "宽松影子账" },
    { key: "wallet_style", label: "钱包风格·旧", note: "旧退出口径" },
  ] as const;
  return (
    <section className="gold-backtest-strip">
      <div className="gold-backtest-head">
        <b>金狗回测</b>
        <span>首次发现优先</span>
        <small>本地只读纸面账，不是实盘收益证明</small>
        <small>前瞻样本仍在积累，钱包风格仅作对照</small>
      </div>
      <div className="gold-backtest-metrics">
        <span><small>当前比较</small><b>3 套策略</b></span>
        <span><small>最佳已实现</small><b className="positive">{fmtSignedUsd(paperTrading?.first_discovery?.summary?.realized_pnl_usd)}</b></span>
        <span><small>前瞻状态</small><b>收益待核验</b></span>
      </div>
      <div className="gold-backtest-table">
        {arms.map(({ key, label, note }) => {
          const arm = paperTrading?.[key];
          const pnl = Number(arm?.summary?.realized_pnl_usd);
          const pnlClass = Number.isFinite(pnl) ? (pnl >= 0 ? "positive" : "negative") : "";
          return (
            <span key={key}>
              <b>{label}</b>
              <small>{note}</small>
              <strong className={pnlClass}>{fmtSignedUsd(arm?.summary?.realized_pnl_usd)}</strong>
              <small>{arm?.closed_count ?? 0} 已平 · {arm?.open_count ?? 0} 持仓 · 权益 {arm?.summary?.equity_usd === 0 ? "$0.00" : fmtMoney(arm?.summary?.equity_usd ?? undefined)} · {paperValuationLabel(arm?.summary)}</small>
            </span>
          );
        })}
      </div>
    </section>
  );
}

function SourceStatusStrip({ status }: { status?: Record<string, SourceStatus> }) {
  const [expanded, setExpanded] = useState(false);
  const entries = Object.entries(status || {});
  if (!entries.length) return null;
  const enabledCount = entries.filter(([, item]) => item.enabled).length;
  const warningCount = entries.filter(
    ([, item]) => item.enabled && ["yellow", "red"].includes(String(item.freshness_level || "")),
  ).length;
  const pendingCount = entries.length - enabledCount;
  return (
    <section className={expanded ? "source-status-panel open" : "source-status-panel"} aria-label="数据源状态">
      <button type="button" className="source-status-summary" onClick={() => setExpanded((current) => !current)} aria-expanded={expanded}>
        <b>数据源状态</b>
        <span>{enabledCount} 个已接</span>
        <span className={warningCount ? "warn" : "ok"}>{warningCount ? `${warningCount} 个需看` : "运行正常"}</span>
        {pendingCount ? <span>{pendingCount} 个未接</span> : null}
        <ChevronDown size={14} />
      </button>
      {expanded ? (
        <div className="source-status-strip">
          {entries.map(([key, item]) => {
            const active = Boolean(item.enabled);
            const urlCount = Number(item.url_count || 0);
            const commandCount = Number(item.command_count || 0);
            const fileCount = Number(item.file_count || 0);
            const activeRoutes = [
              urlCount ? `${urlCount}直连` : "",
              commandCount ? `${commandCount}浏览器` : "",
              fileCount ? `${fileCount}文件` : "",
              item.key_present ? "Key" : "",
            ].filter(Boolean);
            const detail = active
              ? activeRoutes.join(" · ") || "内置"
              : item.needs
                ? "需配置"
                : "待接";
            const age = formatAge(item.age_seconds);
            const freshness = [item.freshness_label, age].filter(Boolean).join(" · ");
            return (
              <span
                key={key}
                className={`${active ? "active" : "pending"} freshness-${item.freshness_level || "pending"}`}
                title={item.needs || item.last_updated_at || item.last_checked_at || item.mode || undefined}
              >
                {SOURCE_STATUS_LABELS[key] || key}
                <small>
                  {active ? "已接" : "待接"} / {detail}
                  {freshness ? ` / ${freshness}` : ""}
                </small>
              </span>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function VoiceAlertStrip({ status }: { status?: VoiceAlertStatus }) {
  if (!status) return null;
  const active = Boolean(status.enabled);
  const symbols = status.last_trigger_symbols?.length ? status.last_trigger_symbols.join(" / ") : "";
  const detail = active
    ? `${voiceReasonLabel(status.reason)} · ${status.repeat_count || 1}遍`
    : voiceReasonLabel(status.reason);
  return (
    <section className="voice-status-strip" aria-label="语音提醒">
      <b>语音提醒</b>
      <span className={active ? "active" : "pending"}>
        {active ? "已开启" : "未开启"}
        <small>{detail}</small>
      </span>
      <span className={status.triggered ? "active" : "pending"}>
        上次触发
        <small>{status.last_triggered_at ? `${status.last_triggered_at}${symbols ? ` · ${symbols}` : ""}` : "--"}</small>
      </span>
      <span className={status.last_tested_at ? "active" : "pending"}>
        自测
        <small>{status.last_tested_at || "--"}</small>
      </span>
    </section>
  );
}

function liveAlertTierLabel(tier: LiveAlertTier): string {
  if (tier === "high_risk") return "高风险重点";
  if (tier === "revival") return "老币复活";
  if (tier === "trend_watch") return "趋势观察";
  if (tier === "invalidated") return "信号失效";
  if (tier === "pullback") return "回撤观察";
  if (tier === "recovered") return "回撤修复";
  if (tier === "confirmed") return "信号确认";
  if (tier === "early") return "早鸟提醒";
  if (tier === "runner") return "飞行中";
  return "首次发现";
}

function LiveAlertStrip({
  popupEnabled,
  soundEnabled,
  soundStatus,
  browserEnabled,
  gmgnAutoOpenEnabled,
  notificationPermission,
  latestCount,
  onTogglePopup,
  onToggleSound,
  onTestSound,
  onRequestBrowserNotice,
  onToggleGmgnAutoOpen,
}: {
  popupEnabled: boolean;
  soundEnabled: boolean;
  soundStatus: LiveAlertSoundStatus;
  browserEnabled: boolean;
  gmgnAutoOpenEnabled: boolean;
  notificationPermission: NotificationPermission | "unsupported";
  latestCount: number;
  onTogglePopup: () => void;
  onToggleSound: () => void;
  onTestSound: () => void;
  onRequestBrowserNotice: () => void;
  onToggleGmgnAutoOpen: () => void;
}) {
  const browserActive = browserEnabled && notificationPermission === "granted";
  return (
    <section className="live-alert-strip" aria-label="前端弹窗提醒">
      <b>前端弹窗</b>
      <button type="button" className={popupEnabled ? "active" : "pending"} onClick={onTogglePopup} title="开启或关闭页面内悬浮提醒">
        <BellRing size={14} />
        {popupEnabled ? "已开启" : "未开启"}
      </button>
      <button
        type="button"
        className={soundEnabled ? "active" : "pending attention"}
        onClick={onToggleSound}
        title="开启或关闭新候选声音"
      >
        {soundEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
        {soundEnabled ? "声音已开" : "开启声音"}
      </button>
      <button type="button" className={soundStatus === "已播" ? "active" : "pending attention"} onClick={onTestSound} title="点击解锁浏览器声音并播放测试提醒">
        <Volume2 size={14} />
        测试/解锁声音
        <small>{soundEnabled ? soundStatus : "点击后开启"}</small>
      </button>
      <button type="button" className={browserActive ? "active" : "pending"} onClick={onRequestBrowserNotice} title="授权浏览器桌面通知">
        <Bell size={14} />
        {browserActive ? "桌面通知开" : notificationPermission === "denied" ? "通知被拒" : "桌面通知"}
      </button>
      <button type="button" className={gmgnAutoOpenEnabled ? "active" : "pending"} onClick={onToggleGmgnAutoOpen} title="新提醒出现时尝试自动打开最强一条的GMGN页面">
        <ExternalLink size={14} />
        {gmgnAutoOpenEnabled ? "自动开GMGN" : "手动开GMGN"}
      </button>
      <span className={latestCount ? "active" : "pending"}>
        本轮新提醒
        <small>{latestCount ? `${latestCount} 个` : "--"}</small>
      </span>
    </section>
  );
}

function LiveAlertDock({
  alerts,
  onSelect,
  onDismiss,
}: {
  alerts: LiveAlert[];
  onSelect: (row: RadarRow) => void;
  onDismiss: (id: string) => void;
}) {
  if (!alerts.length) return null;
  return (
    <div className="live-alert-dock" aria-live="polite">
      {alerts.map((alert) => (
        <article key={alert.id} className={`live-alert-toast ${alert.tier}`}>
          <button type="button" className="live-alert-main" onClick={() => onSelect(alert.row)}>
            <span>{alert.tier === "confirmed" && alert.row.confirmation_status ? confirmationLabel(alert.row) : liveAlertTierLabel(alert.tier)}</span>
            <b>{alert.row.symbol || "--"}</b>
            <small>{alert.reason}</small>
            <em>
              {alert.row.chain || "未知链"} · {alert.source === "--" ? sourceText(alert.row) : alert.source} · 分数 {fmtFixed(scoreOf(alert.row))} · MC {fmtMoney(marketCapOf(alert.row))} · 1h {fmtPct(alert.row.change_h1)}{alert.row.gmgn_risk_flags?.length ? ` · 风险 ${alert.row.gmgn_risk_flags.slice(0, 2).join(" / ")}` : ""}
            </em>
          </button>
          <div className="live-alert-actions">
            {gmgnTokenUrl(alert.row) && (
              <a href={gmgnTokenUrl(alert.row)} target="_blank" rel="noopener noreferrer" title="打开GMGN盘口">
                <ExternalLink size={14} />
                GMGN
              </a>
            )}
            <button type="button" className="live-alert-close" onClick={() => onDismiss(alert.id)} title="关闭提醒">
              <X size={14} />
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}

function WalletConnectStrip({
  address,
  chain,
  status,
  onConnect,
  onDisconnect,
}: {
  address: string | null;
  chain: string | null;
  status: string;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const shortAddress = address ? `${address.slice(0, 6)}...${address.slice(-4)}` : "未连接";
  return (
    <section className="wallet-connect-strip" aria-label="钱包连接">
      <b><Wallet size={14} />钱包</b>
      {address ? <span className="active"><small>{shortAddress}</small><small>{chain || "链未知"}</small></span> : <span className="pending"><small>{status}</small></span>}
      {address ? (
        <button type="button" className="pending" onClick={onDisconnect} title="清除本浏览器保存的钱包地址">
          <X size={14} />断开
        </button>
      ) : (
        <button type="button" className="active" onClick={onConnect} title="连接浏览器钱包并读取地址，不读取私钥">
          <Wallet size={14} />连接钱包
        </button>
      )}
      <small className="wallet-connect-note">只读地址 · 仅用于预览</small>
    </section>
  );
}

function LivePriceCell({ row }: { row: RadarRow }) {
  return (
    <span className={displayQuoteIsFresh(row) ? "live-value active" : "live-value"} title={isFastTracked(row) ? `后端采集 ${evidenceTimeText(row.quote_observed_at)}${row.live_price_updated_at ? ` · 页面刷新 ${evidenceTimeText(row.live_price_updated_at)}` : ""}` : undefined}>
      <b>{fmtPrice(priceOf(row))}</b>
      <small>{quoteStatusText(row)}</small>
    </span>
  );
}

function TokenIdentity({ row, compact = false }: { row: RadarRow; compact?: boolean }) {
  const icon = tokenIconUrl(row);
  const hue = tokenHue(row);
  return (
    <span className={`token-identity ${compact ? "compact" : ""}`}>
      <span
        className="token-avatar"
        style={{
          background: `linear-gradient(145deg, hsl(${hue} 92% 62%), hsl(${(hue + 42) % 360} 82% 42%))`,
        }}
      >
        <span>{tokenInitials(row)}</span>
        {icon ? <img src={icon} alt="" loading="lazy" referrerPolicy="no-referrer" /> : null}
      </span>
      <span className="token-copy">
        <strong>{row.symbol || "--"}</strong>
        <small>{compact ? (row.chain || row.chain_id || "链未知") : tokenSubline(row)}</small>
      </span>
    </span>
  );
}

function ScreenerTape({
  rows,
  onSelect,
  selected,
  variant,
}: {
  rows: RadarRow[];
  onSelect: (row: RadarRow) => void;
  selected?: RadarRow;
  variant: ViewKey;
}) {
  const memeColumns = variant === "meme";
  return (
    <div className="screener-tape">
      <div className="screener-head">
        <span>排名</span>
        <span>代币</span>
        <span>价格</span>
        <span>涨跌</span>
        <span>市值</span>
        <span>成交</span>
        <span>池子</span>
        <span>持有人</span>
        <span>{memeColumns ? "聪明钱" : "数量OI / 1h"}</span>
        <span>{memeColumns ? "KOL" : "已结算费率"}</span>
        <span>信号</span>
        <span>链接</span>
      </div>
      {rows.map((row, index) => {
        const s = signal(row);
        const active = rowKey(row) === rowKey(selected || {});
        const link = row.dex_url || row.url;
        const potential = recommendation(row);
        const change = changeMeta(row);
        return (
          <button key={`${rowKey(row)}-${index}`} type="button" className={`screener-row ${active ? "active" : ""}`} onClick={() => onSelect(row)}>
            <span className="rank">#{index + 1}</span>
            <TokenIdentity row={row} />
            <LivePriceCell row={row} />
            <span className={`change-cell ${Number(change.value || 0) >= 0 ? "up" : "down"}`}>
              <b>{fmtPct(change.value)}</b>
              <small>{change.label}</small>
            </span>
            <span>{fmtMoney(marketCapOf(row))}</span>
            <span>{fmtMoney(volumeOf(row))}</span>
            <span>{fmtMoney(liquidityOf(row))}</span>
            <span>{row.holders || "--"}</span>
            <span>{memeColumns ? <SmartMoneySummary evidence={row.smart_money_evidence} compact /> : fmtPct(row.oi_change_1h_pct)}</span>
            <span>{memeColumns ? (row.kol ?? "--") : fmtPct(row.funding_rate_pct)}</span>
            <span className={`signal ${s.tone}`}>{s.label || potential.action}</span>
            <span>{link ? sourceText(row) : "--"}</span>
          </button>
        );
      })}
    </div>
  );
}

function RecommendationTape({
  items,
  onSelect,
  mode = "default",
}: {
  items: Array<{ row: RadarRow; rec: ReturnType<typeof recommendation> }>;
  onSelect: (row: RadarRow) => void;
  mode?: "default" | "meme";
}) {
  const isMemeMode = mode === "meme";
  const ordered = (Object.keys(BUCKET_LABELS) as RecommendationBucket[]).flatMap((bucket) =>
    items
      .filter((item) => item.rec.bucket === bucket)
      .map((item) => ({ ...item, bucket })),
  );
  return (
    <div className="recommend-tape screener-tape">
      <div className={`screener-head recommend-head ${isMemeMode ? "meme-recommend-head" : ""}`}>
        {isMemeMode ? (
          <>
            <span>分组</span>
            <span>币</span>
            <span>层级</span>
            <span>池龄</span>
            <span>聪明钱</span>
            <span>KOL</span>
            <span>Top10筹码</span>
            <span>风险</span>
            <span>叙事</span>
            <span>上车等级</span>
            <span>推荐理由</span>
            <span>链接</span>
          </>
        ) : (
          <>
            <span>分组</span>
            <span>币</span>
            <span>动作</span>
            <span>判断</span>
            <span>分数</span>
            <span>市值</span>
            <span>合约</span>
            <span>Top10筹码</span>
            <span>来源</span>
          </>
        )}
      </div>
      {ordered.length ? (
        ordered.map(({ row, rec, bucket }, index) => {
          const link = row.dex_url || row.url;
          if (isMemeMode) {
            return (
              <button
                key={`${bucket}-${rowKey(row)}-${index}`}
                type="button"
                className={`screener-row recommend-row meme-recommend-row ${bucket}`}
                onClick={() => onSelect(row)}
              >
            <span className="bucket-label">{BUCKET_LABELS[bucket]}</span>
            <TokenIdentity row={row} compact />
            <span>{watchStageLabel(row)}</span>
            <span>{shortAge(row.pair_age_hours)}</span>
                <SmartMoneySummary evidence={row.smart_money_evidence} compact />
                <span>{row.kol ?? "--"}</span>
                <span>{plainPct(row.top10_holder_pct)}</span>
                <span className="wide-text muted-text">{riskSummary(row)}</span>
                <span>{narrativeText(row)}</span>
                <span className={actionClass(rec.bucket)}>{row.entry_level || rec.action}</span>
                <span className="wide-text">{goldDogReason(row, rec.reason)}</span>
                <span>{link ? sourceText(row) : "--"}</span>
              </button>
            );
          }
          return (
            <button
              key={`${bucket}-${rowKey(row)}-${index}`}
              type="button"
              className={`screener-row recommend-row ${bucket}`}
              onClick={() => onSelect(row)}
            >
              <span className="bucket-label">{BUCKET_LABELS[bucket]}</span>
              <TokenIdentity row={row} compact />
              <span className={actionClass(rec.bucket)}>{rec.action}</span>
              <span className="decision-cell" title={`${rec.reason} ${rec.risk}`}>
                <b>{rec.reason}</b>
                <small>{rec.risk}</small>
              </span>
              <span>{scoreOf(row).toFixed(0)}</span>
              <span>{fmtMoney(marketCapOf(row))}</span>
              <span className="contract-cell">
                <b className={Number(row.oi_change_1h_pct || 0) >= 0 ? "up" : "down"}>OI {fmtPct(row.oi_change_1h_pct)}</b>
                <small>费率 {fmtPct(row.funding_rate_pct)}</small>
              </span>
              <span>{plainPct(row.top10_holder_pct)}</span>
              <span>{link ? sourceText(row) : "--"}</span>
            </button>
          );
        })
      ) : (
        <div className="empty-row">暂无符合</div>
      )}
    </div>
  );
}

function TokenMarketLinks({ row }: { row: RadarRow }) {
  const chain = normalizedDexChain(row.chain || row.chain_id);
  const address = row.token_address || row.contract_address;
  const dexTarget = dexPairTarget(row);
  const dex = dexTarget ? `https://dexscreener.com/${encodeURIComponent(dexTarget.chain)}/${encodeURIComponent(dexTarget.address)}` : address && chain ? `https://dexscreener.com/${encodeURIComponent(chain)}/${encodeURIComponent(address)}` : undefined;
  const gmgn = gmgnTokenUrl(row);
  return <div className="token-market-links">
    {dex && <a href={dex} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} />DexScreener</a>}
    {gmgn && <a href={gmgn} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} />GMGN</a>}
  </div>;
}

function modelProbability(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "--";
}

function RatingV2Comparison({ row, compact = false }: { row: RadarRow; compact?: boolean }) {
  const rating = row.rating_v2;
  const oldScore = Number(rating?.legacy_score ?? scoreOf(row));
  const newScore = rating?.score;
  const delta = rating?.delta;
  const probabilities = rating?.probabilities || {};
  const scored = rating?.status === "scored" && typeof newScore === "number";
  return (
    <div className={`rating-v2-comparison ${compact ? "compact" : ""} ${scored ? "scored" : "collecting"}`}>
      <div className="rating-v2-score-line">
        <span>旧分 {Number.isFinite(oldScore) ? oldScore.toFixed(0) : "--"}</span>
        <span className="rating-v2-arrow">→</span>
        <b>V2 {scored ? newScore.toFixed(0) : "采集中"}</b>
        {scored && typeof delta === "number" ? (
          <em className={delta >= 0 ? "up" : "down"}>{delta >= 0 ? "+" : ""}{delta.toFixed(0)}</em>
        ) : null}
      </div>
      <div className="rating-v2-probabilities" aria-label="V2 模型概率">
        {["2x", "3x", "5x", "10x"].map((target) => (
          <span key={target}>{target} {modelProbability(probabilities[target])}</span>
        ))}
      </div>
    </div>
  );
}

function RatingV2Panel({ rating }: { rating?: RatingV2Summary }) {
  const readiness = rating?.readiness || {};
  const model = rating?.model || {};
  const current = Number(model.dataset_training_rows ?? readiness.training_rows ?? 0);
  const trialRequired = Math.max(1, Number(model.minimum_labeled || 200));
  const formalRequired = Math.max(trialRequired, Number(readiness.minimum_labeled || 500));
  const trialProgress = Math.min(100, Math.round((current / trialRequired) * 100));
  const formalProgress = Math.min(100, Math.round((current / formalRequired) * 100));
  const readyCount = Number(model.ready_target_count || 0);
  const modelTargets = model.targets || {};
  const trainedCount = Number(model.trained_target_count ?? Object.values(modelTargets).filter(
    (target) => ["shadow_model_ready", "shadow_model_weak"].includes(String(target.status || "")),
  ).length);
  const readinessTargets = readiness.targets || {};
  return (
    <section className="rating-v2-panel" aria-label="V2 评级进度">
      <div className="rating-v2-heading">
        <div>
          <span className="rating-v2-kicker">评级校准</span>
          <h2>V2 评级</h2>
        </div>
        <span className={`rating-v2-mode ${readyCount ? "ready" : "collecting"}`}>
          {readyCount ? `验证通过 ${readyCount}/4` : trainedCount ? "模型已训练 · 待验证" : "影子模式 · 采集中"}
        </span>
      </div>
      <div className="rating-v2-progress-grid">
        <div className="rating-v2-progress-item">
          <span><b>试运行样本</b><strong>{current} / {trialRequired}</strong></span>
          <div className="rating-v2-track" role="progressbar" aria-valuemin={0} aria-valuemax={trialRequired} aria-valuenow={Math.min(current, trialRequired)}>
            <i style={{ width: `${trialProgress}%` }} />
          </div>
        </div>
        <div className="rating-v2-progress-item formal">
          <span><b>正式样本</b><strong>{current} / {formalRequired}</strong></span>
          <div className="rating-v2-track" role="progressbar" aria-valuemin={0} aria-valuemax={formalRequired} aria-valuenow={Math.min(current, formalRequired)}>
            <i style={{ width: `${formalProgress}%` }} />
          </div>
        </div>
      </div>
      <div className="rating-v2-targets">
        {["2x", "3x", "5x", "10x"].map((target) => {
          const sample = readinessTargets[target] || modelTargets[target] || {};
          const modelTarget = modelTargets[target] || {};
          const isReady = modelTarget.status === "shadow_model_ready";
          const isWeak = modelTarget.status === "shadow_model_weak";
          const auc = modelTarget.metrics?.roc_auc;
          return (
            <div key={target} className={`rating-v2-target ${isReady ? "ready" : isWeak ? "weak" : "collecting"}`}>
              <span>{target}</span>
              <b>{isReady ? "验证通过" : isWeak ? "模型偏弱" : "采集中"}</b>
              <small>样本 {sample.labeled || 0} · 正 {sample.positive || 0} / 负 {sample.negative || 0}{typeof auc === "number" ? ` · AUC ${auc.toFixed(3)}` : ""}</small>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function SystemModelPanel({
  report,
  showMonitoring,
  showRating,
  popupEnabled,
  soundEnabled,
  soundStatus,
  browserEnabled,
  gmgnAutoOpenEnabled,
  notificationPermission,
  latestCount,
  walletAddress,
  walletChain,
  walletStatus,
  onTogglePopup,
  onToggleSound,
  onTestSound,
  onRequestBrowserNotice,
  onToggleGmgnAutoOpen,
  onConnectWallet,
  onDisconnectWallet,
}: {
  report?: RadarReport;
  showMonitoring: boolean;
  showRating: boolean;
  popupEnabled: boolean;
  soundEnabled: boolean;
  soundStatus: LiveAlertSoundStatus;
  browserEnabled: boolean;
  gmgnAutoOpenEnabled: boolean;
  notificationPermission: NotificationPermission | "unsupported";
  latestCount: number;
  walletAddress: string | null;
  walletChain: string | null;
  walletStatus: string;
  onTogglePopup: () => void;
  onToggleSound: () => void;
  onTestSound: () => void;
  onRequestBrowserNotice: () => void;
  onToggleGmgnAutoOpen: () => void;
  onConnectWallet: () => void;
  onDisconnectWallet: () => void;
}) {
  const meta = report?.meta || {};
  const sources = Object.values(meta.meme_source_status || {});
  const connectedSources = sources.filter((item) => item.enabled).length;
  const warningSources = sources.filter(
    (item) => item.enabled && ["yellow", "red"].includes(String(item.freshness_level || "")),
  ).length;
  const model = meta.rating_v2?.model || {};
  const readiness = meta.rating_v2?.readiness || {};
  const sampleCount = Number(model.dataset_training_rows ?? readiness.training_rows ?? 0);
  const sampleTarget = Math.max(1, Number(readiness.minimum_labeled || 500));
  const alertActive = popupEnabled || soundEnabled || (browserEnabled && notificationPermission === "granted");
  return (
    <details className="system-model-panel">
      <summary>
        <span className="system-model-label"><Gauge size={15} aria-hidden="true" /><b>系统与模型</b></span>
        <span className="system-model-statuses">
          <span className={warningSources ? "warn" : "ok"}>{connectedSources} 源{warningSources ? ` · ${warningSources} 异常` : "在线"}</span>
          <span>V2 {sampleCount}/{sampleTarget}</span>
          <span className={alertActive ? "ok" : "muted"}>提醒{alertActive ? "已开" : "未开"}</span>
        </span>
        <ChevronDown size={15} aria-hidden="true" />
      </summary>
      <div className="system-model-body">
        <SourceStatusStrip status={meta.meme_source_status} />
        <VoiceAlertStrip status={meta.voice_alert} />
        <LiveAlertStrip
          popupEnabled={popupEnabled}
          soundEnabled={soundEnabled}
          soundStatus={soundStatus}
          browserEnabled={browserEnabled}
          gmgnAutoOpenEnabled={gmgnAutoOpenEnabled}
          notificationPermission={notificationPermission}
          latestCount={latestCount}
          onTogglePopup={onTogglePopup}
          onToggleSound={onToggleSound}
          onTestSound={onTestSound}
          onRequestBrowserNotice={onRequestBrowserNotice}
          onToggleGmgnAutoOpen={onToggleGmgnAutoOpen}
        />
        <WalletConnectStrip
          address={walletAddress}
          chain={walletChain}
          status={walletStatus}
          onConnect={onConnectWallet}
          onDisconnect={onDisconnectWallet}
        />
        {showMonitoring ? <RadarMonitoringView report={report} /> : null}
        {showRating ? <RatingV2Panel rating={meta.rating_v2} /> : null}
      </div>
    </details>
  );
}

function uniqueNarrativeLines(...values: Array<string | string[] | null | undefined>): string[] {
  const seen = new Set<string>();
  return values
    .flatMap((value) => Array.isArray(value) ? value : value ? [value] : [])
    .map((value) => String(value).trim())
    .filter((value) => value && value !== "--" && !seen.has(value) && Boolean(seen.add(value)));
}

const TOKEN_INTELLIGENCE_STATUS: Record<string, { label: string; tone: string }> = {
  ready: { label: "情报就绪", tone: "ready" },
  partial: { label: "待补关键证据", tone: "partial" },
  unavailable: { label: "缺少外部证据", tone: "unavailable" },
  pending: { label: "情报生成中", tone: "pending" },
};

function intelligenceItemText(item: unknown): string {
  if (typeof item === "string") return item.trim();
  if (!item || typeof item !== "object" || Array.isArray(item)) return "";
  const record = item as Exclude<TokenIntelligenceItem, string>;
  const subject = record.address || record.wallet || record.title || record.label || record.name;
  const detail = record.text || record.reason || record.summary || record.evidence;
  const citations = Array.isArray(record.evidence_ids) && record.evidence_ids.length
    ? `证据 ${record.evidence_ids.join(" / ")}`
    : "";
  const observed = record.observed_at ? `观察于 ${evidenceTimeText(record.observed_at)}` : "";
  return [subject, detail, record.source, citations, observed].filter((value, index, values) => typeof value === "string" && value.trim() && values.indexOf(value) === index).join(" · ");
}

function intelligenceItems(items?: unknown): string[] {
  return Array.isArray(items) ? items.map(intelligenceItemText).filter(Boolean) : [];
}

function identityStatusText(status?: string): string {
  return ({
    matched: "官方合约匹配",
    match: "官方合约匹配",
    verified: "官方合约匹配",
    mismatch: "官方合约不一致",
    alternate: "存在同名多合约",
    unknown: "尚无官方 CA 证据",
    unavailable: "官方 CA 来源未接入",
  } as Record<string, string>)[status || ""] || status || "尚无官方 CA 证据";
}

function TokenIntelligencePanel({ row, compact = false }: { row: RadarRow; compact?: boolean }) {
  const intelligence = row.token_intelligence;
  const status = TOKEN_INTELLIGENCE_STATUS[intelligence?.status || "unavailable"] || {
    label: intelligence?.status || "情报状态未知",
    tone: "unavailable",
  };
  const legacyNarrative = uniqueNarrativeLines(row.narrative_reasons, row.early_conviction_reason, row.gold_dog_rationale);
  const legacyRisks = uniqueNarrativeLines(row.risk_deduction_reasons, row.gmgn_risk_flags, row.risk_deduction_summary);
  const judgement = intelligence?.one_line_judgement || legacyNarrative[0] || row.reason || "暂无可用判断";
  const narrative = intelligence?.ai_narrative || intelligence?.project_narrative || legacyNarrative.join("。") || "现有证据不足以形成项目叙事。";
  const attention = intelligenceItems(intelligence?.attention_evidence);
  const wallets = intelligenceItems(intelligence?.smart_wallets);
  const aiRisks = intelligenceItems(intelligence?.ai_risk_flags);
  const risks = aiRisks.length ? aiRisks : intelligenceItems(intelligence?.risks).length ? intelligenceItems(intelligence?.risks) : legacyRisks;
  const missing = intelligenceItems(intelligence?.missing_evidence);
  const identity = intelligence?.identity;
  const alternates = intelligenceItems(identity?.alternate_contracts);
  const structuredSources = Array.isArray(intelligence?.sources)
    ? intelligence.sources.filter((source): source is NonNullable<TokenIntelligence["sources"]>[number] => Boolean(source && typeof source === "object"))
    : [];
  const knownSourceUrls = new Set(structuredSources.map((source) => safeEvidenceUrl(source.url)).filter(Boolean));
  const supplementalSources = (Array.isArray(intelligence?.evidence_urls) ? intelligence.evidence_urls : [])
    .map(safeEvidenceUrl)
    .filter((url): url is string => Boolean(url && !knownSourceUrls.has(url)))
    .map((url, index) => ({ source_id: `ai-evidence-${index + 1}`, title: "AI 引用证据", url, observed_at: intelligence?.ai_analyzed_at }));
  const sources = [...structuredSources, ...supplementalSources];
  const gapLabel = (intelligence?.official_ca_status || identity?.official_status) === "unknown"
    ? "待补官方 CA"
    : !sources.length ? "待补官网 / 社交来源"
      : !wallets.length ? "待补钱包证据" : missing.length ? `仍缺 ${missing.length} 项` : "核心证据齐全";

  if (compact) {
    return (
      <span className={`token-intelligence-compact ${status.tone}`}>
        <span className="token-intelligence-status">{status.label} · {gapLabel}</span>
        <span className="token-intelligence-judgement">{judgement}</span>
        <span className="token-intelligence-compact-meta">
          <span>{identityStatusText(intelligence?.official_ca_status || identity?.official_status)}</span>
          <span>{wallets.length ? `钱包证据 ${wallets.length}` : "钱包证据 0"}</span>
          <span>风险 {risks.length}</span>
          <span>缺失 {missing.length}</span>
          <span>来源 {sources.length}</span>
          <span>{evidenceTimeText(intelligence?.generated_at)}</span>
        </span>
      </span>
    );
  }

  return (
    <section className={`token-intelligence-panel ${status.tone}`} aria-label="代币情报">
      <header className="token-intelligence-head">
        <span className="token-intelligence-status">{status.label}</span>
        <small>{intelligence?.ai_analyzed_at ? "AI 分析" : "证据更新"} {evidenceTimeText(intelligence?.ai_analyzed_at || intelligence?.generated_at)}</small>
      </header>
      <div className="token-intelligence-thesis">
        <span>一句话判断</span>
        <b>{judgement}</b>
      </div>
      <div className="token-intelligence-grid">
        <section>
          <span>项目叙事</span>
          <p>{narrative}</p>
          {attention.length ? <ul>{attention.map((line) => <li key={line}>{line}</li>)}</ul> : <small>注意力证据待补</small>}
        </section>
        <section>
          <span>聪明钱证据</span>
          {wallets.length ? <ul>{wallets.map((line) => <li key={line}>{line}</li>)}</ul> : <SmartMoneySummary evidence={row.smart_money_evidence} compact />}
        </section>
        <section>
          <span>官方身份与多 CA</span>
          <b>{identityStatusText(intelligence?.official_ca_status || identity?.official_status)}</b>
          <div className="token-intelligence-signals">
            <small className={intelligence?.ca_conflict ? "danger" : ""}>CA {intelligence?.ca_conflict == null ? "待核验" : intelligence.ca_conflict ? "冲突" : "未见冲突"}</small>
            <small>社交来源 {intelligence?.social_source_count == null ? "未采集" : intelligence.social_source_count}</small>
            <small>刷量 {intelligence?.wash_score == null ? "未计算" : `${intelligence.wash_score.toFixed(0)}分`}</small>
            <small>交易加速 {intelligence?.flow_acceleration == null ? "未计算" : `${intelligence.flow_acceleration.toFixed(2)}x`}</small>
          </div>
          {identity?.official_contract ? <code>{identity.official_contract}</code> : <small>官方合约待确认</small>}
          {alternates.length ? <div className="token-intelligence-contracts"><small>其他合约</small>{alternates.map((contract) => <code key={contract}>{contract}</code>)}</div> : null}
        </section>
        <section className={risks.length ? "has-risk" : ""}>
          <span>风险与缺失证据</span>
          {risks.length ? <ul>{risks.map((line) => <li key={line}>{line}</li>)}</ul> : <small>暂无已记录硬风险</small>}
          {missing.length ? <div className="token-intelligence-missing"><small>仍缺</small>{missing.map((line) => <em key={line}>{line}</em>)}</div> : null}
        </section>
      </div>
      <footer className="token-intelligence-sources">
        <span>来源</span>
        {sources.length ? sources.map((source, index) => {
          const url = safeEvidenceUrl(source.url);
          const title = source.title || `来源 ${index + 1}`;
          const detail = [source.source_id, evidenceTimeText(source.observed_at)].filter(Boolean).join(" · ");
          return url ? <a key={`${url}-${index}`} href={url} target="_blank" rel="noopener noreferrer">{title}<small>{detail}</small><ExternalLink size={12} aria-hidden="true" /></a> : <span key={`${title}-${index}`}>{title}<small>{detail}</small></span>;
        }) : <small>暂无可验证外部来源</small>}
      </footer>
    </section>
  );
}

function TokenNarrativeDetails({
  row,
  rec,
  poolReason,
  alertReason,
  riskText,
  expanded = false,
}: {
  row: RadarRow;
  rec: ReturnType<typeof recommendation>;
  poolReason: string;
  alertReason: string;
  riskText: string;
  expanded?: boolean;
}) {
  const conviction = earlyConvictionOf(row);
  const heat = heatPriorityOf(row);
  const narrativeTags = [...new Set([...(row.narrative_tags || []), ...(row.early_conviction_theme_tags || [])])];
  const narrativeLines = uniqueNarrativeLines(
    row.narrative_reasons,
    row.early_conviction_reason,
    row.gold_dog_rationale,
  );
  const catalystLines = uniqueNarrativeLines(
    row.priority_battlefield_reasons,
    battlefieldReasonText(row),
    row.meme_seed_terms?.length ? `链下热梗：${row.meme_seed_terms.join(" / ")}` : "",
    row.source_repeat_flags?.length ? `重复命中：${row.source_repeat_flags.join(" / ")}` : "",
  );
  const riskLines = uniqueNarrativeLines(
    row.risk_deduction_reasons,
    row.gmgn_risk_flags,
    rec.risk,
    riskText,
  );
  const nextStep = conviction.nextStep || heat.nextStep || row.recommendation_next_step || alertReason;
  return (
    <details className="gold-tier-narrative" open={expanded}>
      <summary>
        <span><Sparkles size={14} aria-hidden="true" />完整研判</span>
        <ChevronDown size={15} aria-hidden="true" />
      </summary>
      <div className="gold-tier-narrative-body">
        <section className="narrative-section narrative-thesis">
          <span>一句话判断</span>
          <b>{rec.action || watchStageLabel(row)}</b>
          <p>{rec.reason || poolReason}</p>
        </section>
        <section className="narrative-section">
          <span>叙事与催化</span>
          <b>{narrativeTags.length ? narrativeTags.join(" / ") : "暂未识别明确主题"}</b>
          <p>{narrativeLines.length ? narrativeLines.join("。") : "目前只有行情与来源信号，没有足够的故事或传播证据。"}</p>
          {catalystLines.length ? <p>{catalystLines.join("。")}</p> : null}
        </section>
        <section className="narrative-section">
          <span>来源与资金</span>
          <b>{sourceText(row)}</b>
          <p>{poolReason}</p>
          <SmartMoneySummary evidence={row.smart_money_evidence} />
        </section>
        <section className={`narrative-section ${riskLines.length ? "has-risk" : ""}`}>
          <span>风险与下一步</span>
          <b>{riskLines.length ? riskLines.join(" / ") : "暂未发现已记录的硬风险"}</b>
          <p>{nextStep}</p>
          <small>提醒状态：{alertReason}</small>
        </section>
      </div>
    </details>
  );
}

function TokenDecisionBrief({
  row,
  rec,
  poolReason,
  alertReason,
  riskText,
}: {
  row: RadarRow;
  rec: ReturnType<typeof recommendation>;
  poolReason: string;
  alertReason: string;
  riskText: string;
}) {
  const conviction = earlyConvictionOf(row);
  const heat = heatPriorityOf(row);
  const intelligence = row.token_intelligence;
  const narrativeTags = [...new Set([...(row.narrative_tags || []), ...(row.early_conviction_theme_tags || [])])];
  const legacyNarrativeLines = uniqueNarrativeLines(row.narrative_reasons, row.early_conviction_reason, row.gold_dog_rationale);
  const narrativeLines = intelligence?.project_narrative ? [intelligence.project_narrative] : legacyNarrativeLines;
  const legacyCatalystLines = uniqueNarrativeLines(
    row.priority_battlefield_reasons,
    battlefieldReasonText(row),
    row.meme_seed_terms?.length ? `链下热梗：${row.meme_seed_terms.join(" / ")}` : "",
  );
  const attentionLines = intelligenceItems(intelligence?.attention_evidence);
  const catalystLines = attentionLines.length ? attentionLines : legacyCatalystLines;
  const intelligenceRisks = intelligenceItems(intelligence?.risks);
  const riskLines = intelligenceRisks.length ? intelligenceRisks : uniqueNarrativeLines(row.risk_deduction_reasons, row.gmgn_risk_flags, rec.risk, riskText);
  const nextStep = conviction.nextStep || heat.nextStep || row.recommendation_next_step || alertReason;
  const judgement = intelligence?.one_line_judgement || rec.reason || poolReason;
  return (
    <div className="gold-tier-decision">
      <section className="gold-tier-thesis">
        <span>当前判断</span>
        <b>{judgement}</b>
        <p>{rec.action || watchStageLabel(row)}</p>
      </section>
      <section className="gold-tier-story">
        <span>完整叙事</span>
        <b>{narrativeTags.length ? narrativeTags.join(" / ") : "暂未识别明确主题"}</b>
        <p>{narrativeLines.length ? narrativeLines.join("。") : "目前只有行情与来源信号，没有足够的故事或传播证据。"}</p>
        {catalystLines.length ? <small>{catalystLines.join("。")}</small> : null}
      </section>
      <div className="gold-tier-decision-grid">
        <section>
          <span>来源与资金</span>
          <b>{sourceText(row)}</b>
          <SmartMoneySummary evidence={row.smart_money_evidence} compact />
        </section>
        <section className={riskLines.length ? "has-risk" : ""}>
          <span>风险与下一步</span>
          <b>{riskLines.length ? riskLines.join(" / ") : "暂未发现已记录的硬风险"}</b>
          <p>{nextStep}</p>
        </section>
      </div>
    </div>
  );
}

function GoldTierPanel({
  items,
  summary,
  backtest,
  paperTrading,
  onSelect,
  activeTier,
}: {
  items: Array<{ row: RadarRow; rec: ReturnType<typeof recommendation> }>;
  summary?: GoldWatchSummary;
  backtest?: GoldBacktestSummary;
  paperTrading?: PaperTradingSummary;
  activeTier?: GoldTierKey;
  onSelect: (row: RadarRow) => void;
}) {
  const grouped = items.reduce<Record<GoldTierKey, Array<{ row: RadarRow; rec: ReturnType<typeof recommendation> }>>>(
    (acc, item) => {
      acc[goldTierOf(item.row)].push(item);
      return acc;
    },
    { discovered: [], early: [], confirmed: [], revival: [], review: [] },
  );
  const tiers: GoldTierKey[] = ["confirmed", "early", "discovered", "revival", "review"];
  const initialTier = activeTier || tiers.find((tier) => grouped[tier].length > 0) || "confirmed";
  const [selectedTier, setSelectedTier] = useState<GoldTierKey>(initialTier);
  const [selectedTokenKey, setSelectedTokenKey] = useState("");
  const [page, setPage] = useState(0);
  const visibleTier = activeTier || selectedTier;
  const pageSize = 8;

  return (
    <section className="gold-tier-panel">
      <details className="radar-history-details">
        <summary>历史追踪与回测</summary>
        <GoldBacktestStrip backtest={backtest} paperTrading={paperTrading} />
        <GoldWatchSummaryStrip summary={summary} />
      </details>
      {!activeTier && (
        <nav className="gold-tier-tabs" aria-label="聚合候选阶段">
          {tiers.map((tier) => {
            const copy = goldTierCopy(tier);
            return (
              <button
                key={tier}
                type="button"
                className={`${tier} ${visibleTier === tier ? "active" : ""}`}
                aria-pressed={visibleTier === tier}
                onClick={() => {
                  setSelectedTier(tier);
                  setSelectedTokenKey("");
                  setPage(0);
                }}
              >
                <span>{copy.label}</span>
                <strong>{grouped[tier].length}</strong>
              </button>
            );
          })}
        </nav>
      )}
      {tiers.filter((tier) => tier === visibleTier).map((tier) => {
        const copy = goldTierCopy(tier);
        const tierItems = grouped[tier];
        const pageCount = Math.max(1, Math.ceil(tierItems.length / pageSize));
        const currentPage = Math.min(page, pageCount - 1);
        const visibleTierItems = tierItems.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
        const selectedItem = tierItems.find(({ row }) => rowKey(row) === selectedTokenKey) || visibleTierItems[0];
        return (
          <div key={tier} className={`gold-tier-card ${tier}`}>
            <div className="gold-tier-card-heading">
              <div>
                <div className="gold-tier-head">
                  <span>{copy.label}</span>
                  <strong>{tierItems.length}</strong>
                </div>
                <b>{copy.range}</b>
                <small>{copy.rule}</small>
              </div>
              {tierItems.length > pageSize ? (
                <div className="gold-tier-pagination" aria-label={`${copy.label}分页`}>
                  <span>{currentPage + 1} / {pageCount}</span>
                  <button
                    type="button"
                    aria-label="上一页"
                    disabled={currentPage === 0}
                    onClick={() => {
                      const nextPage = Math.max(0, currentPage - 1);
                      setPage(nextPage);
                      setSelectedTokenKey(rowKey(tierItems[nextPage * pageSize]?.row || {}));
                    }}
                  ><ChevronLeft size={16} /></button>
                  <button
                    type="button"
                    aria-label="下一页"
                    disabled={currentPage >= pageCount - 1}
                    onClick={() => {
                      const nextPage = Math.min(pageCount - 1, currentPage + 1);
                      setPage(nextPage);
                      setSelectedTokenKey(rowKey(tierItems[nextPage * pageSize]?.row || {}));
                    }}
                  ><ChevronRight size={16} /></button>
                </div>
              ) : null}
            </div>
            {tierItems.length ? (
              <div className="gold-tier-workspace">
                <div className="gold-tier-list" role="listbox" aria-label={`${copy.label}候选`}>
                  {visibleTierItems.map(({ row }) => {
                    const currentMcap = marketCapOf(row);
                    const firstSeenMcap = firstSeenMcapOf(row);
                    const conviction = earlyConvictionOf(row);
                    const heat = heatPriorityOf(row);
                    const active = rowKey(row) === rowKey(selectedItem?.row || {});
                    return (
                      <button
                        key={`${tier}-${rowKey(row)}`}
                        type="button"
                        role="option"
                        aria-selected={active}
                        className={`gold-tier-list-item ${active ? "active" : ""}`}
                        onClick={() => setSelectedTokenKey(rowKey(row))}
                      >
                        <span className="gold-tier-token-identity">
                          <TokenIdentity row={row} compact />
                          {conviction.level || heat.level ? (
                            <span className={`heat-priority-flag ${conviction.level ? "early-conviction" : heat.level}`} title={conviction.reason || heat.reason}>
                              {conviction.label || heat.label}
                            </span>
                          ) : null}
                        </span>
                        <span className="gold-tier-list-market">
                          <b>{fmtMoney(currentMcap)}</b>
                          <small>首次发现 {firstSeenMcap ? fmtMoney(firstSeenMcap) : "市值未记录"} · 池龄 {compactHours(row.pair_age_hours)}</small>
                        </span>
                        <span className="gold-tier-list-stage">
                          <b>{pipelineText(row)}</b>
                          <small>{goldProgressText(row, summary?.min_confirmations)} · {quoteStatusText(row)}</small>
                        </span>
                        <TokenIntelligencePanel row={row} compact />
                      </button>
                    );
                  })}
                </div>
                {selectedItem ? (() => {
                  const { row, rec } = selectedItem;
                  const currentMcap = marketCapOf(row);
                  const firstSeenMcap = firstSeenMcapOf(row);
                  const poolReason = poolEntryReason(row, rec.reason);
                  const alertReason = alertGateReason(row);
                  const riskText = riskDeductionText(row);
                  const captureText = pipelineText(row);
                  return (
                    <article className="gold-tier-detail" aria-label={`${row.symbol || row.name || "候选"}完整研判`}>
                      <header className="gold-tier-detail-head">
                        <div>
                          <TokenIdentity row={row} />
                          <span className="gold-tier-detail-stage">{watchStageLabel(row)} · {pipelineText(row)}</span>
                        </div>
                        <TokenMarketLinks row={row} />
                      </header>
                      <div className="gold-tier-detail-metrics">
                        <span><small>{quoteStatusText(row)}</small><b>{fmtMoney(currentMcap)}</b></span>
                        <span><small>首次发现市值</small><b>{firstSeenMcap ? fmtMoney(firstSeenMcap) : "未记录"}</b></span>
                        <span><small>发现时间 / 池龄</small><b>{firstSeenTimeOf(row)} · {compactHours(row.pair_age_hours)}</b></span>
                        <span><small>首次确认</small><b>{firstConfirmationText(row, currentMcap)}</b></span>
                      </div>
                      <TokenDecisionBrief row={row} rec={rec} poolReason={poolReason} alertReason={alertReason} riskText={riskText} />
                      <TokenIntelligencePanel row={row} />
                      <details className="gold-tier-evidence-details">
                        <summary><span><ShieldCheck size={14} aria-hidden="true" />核验与模型明细</span><ChevronDown size={15} aria-hidden="true" /></summary>
                        <div className="gold-tier-evidence-body">
                          <RatingV2Comparison row={row} />
                          <SmartMoneySummary evidence={row.smart_money_evidence} />
                          <ConfirmationEvidence row={row} />
                          <div className="gold-tier-ops">
                            <span title={poolReason}><small>进池</small><b>{poolReason}</b></span>
                            <span title={alertReason}><small>提醒状态</small><b>{alertReason}</b></span>
                            <span title={riskText}><small>风险扣分</small><b>{riskText}</b></span>
                            <span title={captureText}><small>归因</small><b>{captureText}</b></span>
                            <span title={okxSourceText(row)}><small>OKX</small><b>{okxSourceText(row)}</b></span>
                          </div>
                        </div>
                      </details>
                      <button type="button" className="gold-tier-open-detail" onClick={() => onSelect(row)}><BarChart3 size={15} aria-hidden="true" />打开行情与全部数据</button>
                    </article>
                  );
                })() : null}
              </div>
            ) : (
              <div className="gold-tier-empty">{goldTierEmptyText(tier)}</div>
            )}
          </div>
        );
      })}
    </section>
  );
}

function ReplayBoard({ report, onSelect }: { report?: RadarReport; onSelect: (row: RadarRow) => void }) {
  const summary = report?.meta?.replay || {};
  const boards = report?.meta?.replay_boards || {};
  const actionStats = Object.entries(summary.by_action || {})
    .sort(([, a], [, b]) => Number(b.count || 0) - Number(a.count || 0))
    .slice(0, 6);
  const groups = [
    { key: "best_hits", title: "最近命中", rows: boards.best_hits || [] },
    { key: "worst_misses", title: "最差判断", rows: boards.worst_misses || [] },
    { key: "risk_avoided", title: "避险有效", rows: boards.risk_avoided || [] },
  ];
  return (
    <div className="replay-board">
      <div className="replay-summary-strip">
        <StatCard label="跟踪中" value={String(summary.tracked_count || 0)} detail="已进入复盘池" />
        <StatCard label="可评估推荐" value={String(summary.actionable_count || 0)} detail="主推 / 埋伏" />
        <StatCard label="命中率" value={plainPct(summary.hit_rate_pct)} detail={`${summary.hit_count || 0} 命中 / ${summary.miss_count || 0} 失误`} />
        <StatCard label="避险有效" value={String(summary.risk_avoided_count || 0)} detail="高危后走弱" />
      </div>
      <section className="action-win-panel">
        <div className="action-win-head">
          <h3>动作胜率</h3>
          <span>按系统动作统计 1h / 6h / 24h 表现</span>
        </div>
        {actionStats.length ? (
          <div className="action-win-grid">
            {actionStats.map(([action, item]) => (
              <div key={action} className="action-win-card">
                <b>{action}</b>
                <span>样本 {item.count || 0} / 命中 {plainPct(item.hit_rate_pct)}</span>
                <small>
                  1h {fmtPct(item.avg_return_1h_pct)} / 6h {fmtPct(item.avg_return_6h_pct)} / 24h {fmtPct(item.avg_return_24h_pct)}
                </small>
              </div>
            ))}
          </div>
        ) : (
          <p className="empty-action-win">样本还不够，继续跑几轮后会自动出现胜率。</p>
        )}
      </section>
      <div className="replay-lanes">
        {groups.map((group) => (
          <section key={group.key} className="replay-lane">
            <h3>{group.title}</h3>
            {group.rows.length ? (
              group.rows.map((row) => (
                <button key={`${group.key}-${rowKey(row)}`} type="button" className="replay-item" onClick={() => onSelect(row)}>
                  <span className="ticker-symbol">{row.symbol || "--"}</span>
                  <span>{replayStatusLabel(row.hit_status)}</span>
                  <b className={Number(row.return_since_first_pct || 0) >= 0 ? "up" : "down"}>{fmtPct(row.return_since_first_pct)}</b>
                </button>
              ))
            ) : (
              <p>暂无数据</p>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

function DetailPanel({ row }: { row?: RadarRow }) {
  const [tab, setTab] = useState("market");
  if (!row) {
    return (
      <aside className="detail-panel">
        <h2>单币情报</h2>
        <p>从中间榜单点一个币，这里会给出中文结论、原因和风险。</p>
      </aside>
    );
  }
  const rec = recommendation(row);
  const s = signal(row);
  const conviction = earlyConvictionOf(row);
  const heat = heatPriorityOf(row);
  const replay = row.replay;
  const ratingV2 = row.rating_v2;
  const metrics = [
    ["旧评级分", fmtFixed(ratingV2?.legacy_score ?? scoreOf(row))],
    ["V2综合分", ratingV2?.status === "scored" ? fmtFixed(ratingV2.score) : "采集中"],
    ["V2分差", typeof ratingV2?.delta === "number" ? `${ratingV2.delta >= 0 ? "+" : ""}${ratingV2.delta.toFixed(0)}` : "--"],
    ["V2概率", `2x ${modelProbability(ratingV2?.probabilities?.["2x"])} / 3x ${modelProbability(ratingV2?.probabilities?.["3x"])} / 5x ${modelProbability(ratingV2?.probabilities?.["5x"])} / 10x ${modelProbability(ratingV2?.probabilities?.["10x"])}`],
    ["早期信心层", conviction.label || "--"],
    ["早期信心分析", conviction.reason || "--"],
    ["早期下一步", conviction.nextStep || "--"],
    ["热度级别", heat.label || "--"],
    ["热度分析", heat.reason || "--"],
    ["热度下一步", heat.nextStep || "--"],
    ["首次发现后倍数", heat.mcapMultiple ? `${heat.mcapMultiple.toFixed(2)}x` : "--"],
    ["阶段", row.stage || row.alpha_stage_label || "--"],
    ["方向", row.direction || "--"],
    ["动作", row.action || "--"],
    ["关键位", fmtPrice(row.key_level)],
    ["失效位", fmtPrice(row.invalid_level)],
    ["持仓处理", row.holding_text || "--"],
    ["保护规则", row.protection_text || "--"],
    ["市值", quoteAwareMetricText(row, fmtMoney(marketCapOf(row)))],
    ["流动性", quoteAwareMetricText(row, fmtMoney(liquidityOf(row)))],
    ["成交", quoteAwareMetricText(row, fmtMoney(volumeOf(row)))],
    ["池龄", shortAge(row.pair_age_hours)],
    ["5m涨幅", quoteAwareMetricText(row, fmtPct(row.change_m5))],
    ["1h涨幅", quoteAwareMetricText(row, fmtPct(row.change_h1))],
    [row.oi_basis === "quantity" ? "数量OI / 1h" : "OI / 旧口径", fmtPct(row.oi_change_1h_pct)],
    ["价值OI / 1h", fmtPct(row.oi_value_change_1h_pct)],
    ["最近结算费率", fmtPct(row.funding_rate_pct)],
    ["历史结算间隔", row.funding_interval_hours ? `${row.funding_interval_hours}小时` : "--"],
    ["费率 / 折算8h", fmtPct(row.funding_rate_8h_pct)],
    ["费率结算时间", row.funding_settled_at_ms ? new Date(row.funding_settled_at_ms).toLocaleString() : "--"],
    ["异常分（非买入分）", fmtFixed(row.dealer_score)],
    ["活跃度", fmtFixed(row.dealer_activity_score)],
    ["机会参考分（待验证）", row.dealer_opportunity_score == null ? "数据不足" : fmtFixed(row.dealer_opportunity_score)],
    ["筹码检查", row.dealer_risk_status === "checked" ? "已检查" : row.dealer_risk_status === "blocked" ? "风险拦截" : "待补数据"],
    ["Top10筹码", plainPct(row.top10_holder_pct)],
    ["最大钱包", plainPct(row.max_holder_pct)],
    ["筹码来源", row.holder_source || "--"],
    ["筹码采集时间", row.holder_observed_at ? new Date(row.holder_observed_at).toLocaleString() : "--"],
    ["发现来源", sourceText(row)],
    ["处理/补充", enrichmentText(row)],
    ["上所想象", battlefieldLabel(row)],
    ["入场加分", row.priority_battlefield_score == null ? "--" : fmtFixed(row.priority_battlefield_score)],
    ["催化原因", battlefieldReasonText(row)],
    ["发现层", discoveryLayerText(row)],
    ["二筛来源", screeningText(row)],
    ["确认来源", row.watch_source_confirmation_sources?.length ? row.watch_source_confirmation_sources.join(" + ") : "--"],
    ["确认状态", row.confirmation_reason || row.watch_source_confirmation_reason || (row.watch_source_confirmation_ok ? "来源确认已满足" : "--")],
    ["OKX面板", row.okx_source_panel === "signal" ? "信号板块" : row.okx_source_panel === "trenches" ? "Trenches" : "--"],
    ["OKX信号类型", row.okx_signal_type_label || row.okx_signal_type || "--"],
    ["OKX触发钱包", row.okx_trigger_wallet_count == null ? "--" : String(row.okx_trigger_wallet_count)],
    ["OKX买入金额", fmtMoney(Number(row.okx_signal_amount_usd))],
    ["OKX卖出比例", plainPct(Number(row.okx_signal_sold_ratio_pct))],
    ["OKX信号价格", fmtMoney(Number(row.okx_signal_price_usd))],
    ["OKX信号时间", fmtTime(row.okx_signal_timestamp)],
    ["老币复活分", row.old_meme_revival_score == null ? "--" : fmtFixed(row.old_meme_revival_score)],
    ["复活依据", row.old_meme_revival_reasons?.length ? row.old_meme_revival_reasons.join(" / ") : "--"],
    ["成交活跃度", row.alpha_buyflow_score == null ? "--" : `${fmtFixed(row.alpha_buyflow_score)} / ${row.alpha_buyflow_label || "--"}`],
    ["成交解释", row.alpha_buyflow_explain || "--"],
    ["买卖笔数比", fmtFixed(row.alpha_buyflow_buy_sell_ratio, 2)],
    ["买盘量市比", row.alpha_buyflow_volume_to_mcap_pct == null ? "--" : `${fmtFixed(row.alpha_buyflow_volume_to_mcap_pct, 1)}%`],
    ["庄家类型", row.dealer_label || "--"],
    ["庄家解释", row.dealer_explain || "--"],
    ["量市比", row.dealer_volume_to_mcap_pct == null ? "--" : `${fmtFixed(row.dealer_volume_to_mcap_pct, 1)}%`],
    ["合约量市比", row.dealer_contract_volume_to_mcap_pct == null ? "--" : `${fmtFixed(row.dealer_contract_volume_to_mcap_pct, 1)}%`],
    ["妖币阶段", row.alpha_stage_label || "--"],
    ["妖币类型", row.alpha_label || "--"],
    ["阶段解释", row.alpha_explain || "--"],
    ["雷达层级", watchStageLabel(row)],
    ["首次确认市值", firstConfirmationText(row)],
    ["首次确认时间", row.watch_first_confirmed_at || row.watch_alerted_at || "--"],
    ["首次发现市值", firstSeenMcapOf(row) ? fmtMoney(firstSeenMcapOf(row)) : "未记录"],
    ["金狗阶段", row.entry_stage || "--"],
    ["上车等级", row.entry_level || "--"],
    ["金狗分", fmtFixed(row.gold_dog_score)],
    ["Gem分", row.cliff_gem_score == null ? "--" : `${fmtFixed(row.cliff_gem_score)} / ${row.cliff_gem_label || "--"}`],
    ["风险分", row.cliff_risk_score == null ? "--" : `${fmtFixed(row.cliff_risk_score)} / ${row.cliff_risk_label || "--"}`],
    ["热度分", row.cliff_hype_score == null ? "--" : `${fmtFixed(row.cliff_hype_score)} / ${row.cliff_hype_label || "--"}`],
    ["上车分", fmtFixed(row.entry_score)],
    ["新鲜度", fmtFixed(row.freshness_score)],
    ["筹码质量", fmtFixed(row.holder_quality_score)],
    ["安全过滤", fmtFixed(row.security_filter_score)],
    ["池子/池龄", fmtFixed(row.liquidity_age_score)],
    ["Dev信誉", fmtFixed(row.dev_trust_score)],
    ["平台标签热度", fmtFixed(row.smart_kol_score)],
    ["机器人过滤", fmtFixed(row.bot_manipulation_score)],
    ["专业分", fmtFixed(row.pro_signal_score)],
    ["专业信号", row.pro_signal_tags?.length ? row.pro_signal_tags.join(" / ") : "--"],
    ["GMGN技能分", fmtFixed(row.gmgn_skill_score)],
    ["GMGN技能", row.gmgn_skill_tags?.length ? row.gmgn_skill_tags.join(" / ") : "--"],
    ["技能类别", row.gmgn_skill_categories?.length ? row.gmgn_skill_categories.join(" / ") : "--"],
    ["叙事分", fmtFixed(row.narrative_score)],
    ["叙事", row.narrative_tags?.length ? row.narrative_tags.join(" / ") : "--"],
    ["链下热梗分", fmtFixed(row.meme_seed_score)],
    ["链下热梗", row.meme_seed_terms?.length ? row.meme_seed_terms.join(" / ") : "--"],
    ["热梗闸口", row.meme_seed_candidate ? row.meme_seed_gate || "seed_only" : "--"],
    ["推荐理由", row.gold_dog_rationale?.length ? row.gold_dog_rationale.join(" / ") : "--"],
    ["风险过滤", fmtFixed(row.risk_filter_score)],
    ["承接质量", fmtFixed(row.absorption_score)],
    ["回测校准", row.replay_score_adjustment === undefined ? "--" : `${row.replay_score_adjustment > 0 ? "+" : ""}${row.replay_score_adjustment}`],
    ["回测原因", row.replay_calibration_reason || "--"],
    ["独立来源数", discoverySourceCount(row) == null ? "不可用" : String(discoverySourceCount(row))],
    ["平台标注钱包", String(row.smart_money ?? "--")],
    ["KOL", String(row.kol ?? "--")],
    ["GMGN风险", row.gmgn_risk_flags?.length ? row.gmgn_risk_flags.join(" / ") : "--"],
  ];

  const marketLabels = new Set(["市值", "流动性", "成交", "池龄", "5m涨幅", "1h涨幅", "数量OI / 1h", "OI / 旧口径", "价值OI / 1h", "最近结算费率", "历史结算间隔", "费率 / 折算8h", "费率结算时间", "成交活跃度", "成交解释", "买卖笔数比", "买盘量市比", "量市比", "合约量市比"]);
  const riskLabels = new Set(["筹码检查", "Top10筹码", "最大钱包", "风险分", "筹码质量", "安全过滤", "Dev信誉", "机器人过滤", "风险过滤", "GMGN风险", "失效位", "保护规则"]);
  riskLabels.add("筹码来源");
  riskLabels.add("筹码采集时间");
  const visibleMetrics = metrics.filter(([label, value]) => value !== "--" && value !== "" && (tab === "market" ? marketLabels.has(label) : tab === "risk" ? riskLabels.has(label) : !marketLabels.has(label) && !riskLabels.has(label)));
  const missing = [["Top10筹码", row.top10_holder_pct], ["最大钱包", row.max_holder_pct]].filter(([, value]) => value == null).map(([label]) => label);
  const headlineMetrics = [["市值", quoteAwareMetricText(row, fmtMoney(marketCapOf(row)))], ["流动性", quoteAwareMetricText(row, fmtMoney(liquidityOf(row)))], ["24h成交", quoteAwareMetricText(row, fmtMoney(volumeOf(row)))], [row.oi_basis === "quantity" ? "数量OI / 1h" : "OI / 旧口径", fmtPct(row.oi_change_1h_pct)], ["费率 / 折算8h", fmtPct(row.funding_rate_8h_pct)]];

  return (
    <aside className="detail-panel compact-detail">
      <div className="detail-head">
        <div>
          <span className={`signal ${s.tone}`}>{s.label}</span>
          <h2>{row.symbol || "--"}</h2>
          <p>{row.name || row.chain || "Binance Alpha 项目"}</p>
        </div>
        <div className="detail-score"><span>{row.dealer_score != null ? "异常分 · 非买入分" : "筛选分"}</span><b>{fmtFixed(row.dealer_score ?? scoreOf(row))}</b></div>
        <TokenMarketLinks row={row} />
      </div>
      <section className={`verdict ${rec.bucket}`}>
        <span>当前判断</span>
        <h3>{rec.action}</h3>
        <p>{rec.reason}</p>
        <small>{rec.risk}</small>
        {missing.length > 0 && <small>待补数据：{missing.join("、")}</small>}
      </section>
      <div className="detail-key-metrics">{headlineMetrics.map(([label, value]) => <div key={label}><span>{label}</span><b>{value}</b></div>)}</div>
      <EvidenceOverview row={row} />
      <TokenIntelligencePanel row={row} />
      <SmartMoneySummary evidence={row.smart_money_evidence} />
      <ConfirmationEvidence row={row} />
      <div className="detail-tabs" role="tablist" aria-label="币种详情">
        {[["market", "行情与资金"], ["risk", "筹码与风险"], ["signals", "信号与复盘"]].map(([id, label]) => <button key={id} id={`detail-tab-${id}`} type="button" role="tab" aria-selected={tab === id} aria-controls="detail-tab-content" onClick={() => setTab(id)}>{label}</button>)}
      </div>
      <section id="detail-tab-content" role="tabpanel" aria-labelledby={`detail-tab-${tab}`} className="detail-tab-content">
      {tab === "market" && <TokenPricePanel row={row} />}
      <div className="metric-grid">
        {visibleMetrics.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <b>{value}</b>
          </div>
        ))}
      </div>
      {tab === "risk" && <section className="reason-box"><h3>判断依据</h3><p>{rec.risk || rec.reason}</p>{missing.length > 0 && <p>缺少{missing.join("、")}，尚不能确认筹码是否集中。</p>}{row.holder_source?.startsWith("goplus_") && <p>GoPlus 前十持仓地址占比，包含合约、交易所及池子；最大地址不等于单一庄家。采集于 {row.holder_observed_at ? new Date(row.holder_observed_at).toLocaleString() : "时间未知"}。</p>}</section>}
      {tab === "signals" && <section className="reason-box replay-box">
        <h3>复盘</h3>
        {replay ? (
          <div className="replay-grid">
            <span>
              状态 <b>{replayStatusLabel(replay.hit_status)}</b>
            </span>
            <span>
              首次价 <b>{fmtPrice(replay.first_price_usd)}</b>
            </span>
            <span>
              当前价 <b>{fmtPrice(replay.latest_price_usd)}</b>
            </span>
            <span>
              总变化 <b className={Number(replay.return_since_first_pct || 0) >= 0 ? "up" : "down"}>{fmtPct(replay.return_since_first_pct)}</b>
            </span>
            <span>
              1h <b>{fmtPct(replay.return_1h_pct)}</b>
            </span>
            <span>
              6h <b>{fmtPct(replay.return_6h_pct)}</b>
            </span>
            <span>
              24h <b>{fmtPct(replay.return_24h_pct)}</b>
            </span>
          </div>
        ) : (
          <p>刚进入观察，还没有足够复盘数据。</p>
        )}
      </section>}
      </section>
    </aside>
  );
}

export default function App() {
  const [view, setView] = useState<ViewKey>("recommend");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<RadarRow | undefined>();
  const [liveAlerts, setLiveAlerts] = useState<LiveAlert[]>([]);
  const [monitorAlerts, setMonitorAlerts] = useState<MonitorAlert[]>([]);
  const [popupEnabled, setPopupEnabled] = useState(() => storedBoolean(LIVE_ALERT_POPUP_STORAGE_KEY, true));
  const [soundEnabled, setSoundEnabled] = useState(() => storedBoolean(LIVE_ALERT_SOUND_STORAGE_KEY, false));
  const [soundStatus, setSoundStatus] = useState<LiveAlertSoundStatus>("未测试");
  const [browserNoticeEnabled, setBrowserNoticeEnabled] = useState(() => storedBoolean(LIVE_ALERT_BROWSER_STORAGE_KEY, false));
  const [gmgnAutoOpenEnabled, setGmgnAutoOpenEnabled] = useState(() => storedBoolean(LIVE_ALERT_GMGN_OPEN_STORAGE_KEY, false));
  const [walletAddress, setWalletAddress] = useState<string | null>(() => storedText(WALLET_ADDRESS_STORAGE_KEY));
  const [walletChain, setWalletChain] = useState<string | null>(() => storedText(WALLET_CHAIN_STORAGE_KEY));
  const [walletStatus, setWalletStatus] = useState("未连接");
  const [notificationPermission, setNotificationPermission] = useState<NotificationPermission | "unsupported">(() =>
    "Notification" in window ? Notification.permission : "unsupported",
  );
  const [latestAlertCount, setLatestAlertCount] = useState(0);
  const seenAlertIdsRef = useRef<Set<string>>(new Set());
  const latestSeenAlertRef = useRef<Map<string, LiveAlert>>(new Map());
  const alertBaselineReadyRef = useRef(false);
  const monitorAlertBaselineReadyRef = useRef(false);
  const seenMonitorAlertIdsRef = useRef<Set<string>>(new Set());
  const audioInteractedRef = useRef(false);
  const legacyAlertsActiveRef = useRef(legacyLiveAlertsEnabled(view));
  const monitorAlertsActiveRef = useRef(view === "memePotential");
  const reportUrl = useMemo(() => configuredReportUrl(), []);
  const { data, isLoading, isError, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ["report", reportUrl],
    queryFn: () => fetchReport(reportUrl),
    refetchInterval: view === "memePotential" ? false : REPORT_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    refetchOnReconnect: true,
    refetchOnWindowFocus: true,
    retry: 1,
    staleTime: 0,
  });
  const monitorQueryKey = useMemo(() => ["monitor-v3", reportUrl] as const, [reportUrl]);
  const monitorStreamLive = useMonitorV3Stream(reportUrl, monitorQueryKey);
  const { data: liveMonitorData, dataUpdatedAt: monitorDataUpdatedAt } = useQuery({
    queryKey: monitorQueryKey,
    queryFn: () => fetchMonitorReport(reportUrl),
    // SSE pushes live updates on Pages; keep a slow poll only as fallback.
    refetchInterval: monitorStreamLive ? false : MONITOR_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: !monitorStreamLive,
    refetchOnReconnect: true,
    refetchOnWindowFocus: !monitorStreamLive,
    retry: 0,
    staleTime: 0,
  });
  const rows = usePreparedRows(data);
  const monitorV3 = useMemo(
    () => parseMonitorV3Snapshot(liveMonitorData?.monitor_v3 ?? data?.monitor_v3),
    [data?.monitor_v3, liveMonitorData?.monitor_v3],
  );
  const monitorTokenIntelligence = useMemo(() => buildTokenIntelligenceIndex([
    ...(liveMonitorData?.monitor_intelligence_rows || []),
    ...(data?.meme_rows || []),
    ...(data?.meme_heat_rows || []),
    ...(data?.meme_conviction_rows || []),
    ...(data?.meme_potential_rows || []),
    ...(data?.meme_shadow_rows || []),
    ...(data?.meme_watch_universe || []),
  ]), [data, liveMonitorData?.monitor_intelligence_rows]);
  const monitorTokenImages = useMemo(() => {
    const index: Record<string, string> = {};
    const candidates = [
      ...(data?.meme_rows || []),
      ...(data?.meme_heat_rows || []),
      ...(data?.meme_conviction_rows || []),
      ...(data?.meme_potential_rows || []),
      ...(data?.meme_shadow_rows || []),
      ...(data?.meme_watch_universe || []),
    ];
    candidates.forEach((row) => {
      const address = String(row.contract_address || row.token_address || "").trim();
      const chain = String(row.chain || row.chain_id || "").trim();
      const image = tokenIconUrl(row);
      if (!chain || !address || !image) return;
      index[tokenIntelligenceKey(chain, address)] = image;
    });
    return index;
  }, [data]);

  useEffect(() => {
    const enabled = legacyLiveAlertsEnabled(view);
    legacyAlertsActiveRef.current = enabled;
    monitorAlertsActiveRef.current = true;
    if (!enabled && !soundEnabled) stopLegacyLiveAlertAudio();
  }, [soundEnabled, view]);

  useEffect(() => {
    if (!soundEnabled) return undefined;
    const unlockOnce = () => {
      audioInteractedRef.current = true;
      void unlockLiveAlertAudio();
    };
    window.addEventListener("pointerdown", unlockOnce, { once: true });
    window.addEventListener("keydown", unlockOnce, { once: true });
    window.addEventListener("touchstart", unlockOnce, { once: true });
    return () => {
      window.removeEventListener("pointerdown", unlockOnce);
      window.removeEventListener("keydown", unlockOnce);
      window.removeEventListener("touchstart", unlockOnce);
    };
  }, [soundEnabled, view]);

  useEffect(() => {
    if (!legacyLiveAlertsEnabled(view)) {
      setLiveAlerts([]);
      setLatestAlertCount(0);
      return;
    }
    if (!data) return;
    const alerts = buildLiveAlerts(data);
    const activeIds = new Set(alerts.map((alert) => alert.id));
    setLiveAlerts((current) => current.filter((alert) => activeIds.has(alert.id)));
    let fresh: LiveAlert[];
    if (!alertBaselineReadyRef.current) {
      seenAlertIdsRef.current = new Set(alerts.map((alert) => alert.id));
      latestSeenAlertRef.current = new Map(alerts.map((alert) => [alertIdentity(alert.row), alert]));
      alertBaselineReadyRef.current = true;
      // A page opened just after a trigger should still show that trigger once.
      fresh = startupLiveAlerts(alerts);
    } else {
      fresh = alerts.filter((alert) => !seenAlertIdsRef.current.has(alert.id) && isNewerAlert(alert, latestSeenAlertRef.current.get(alertIdentity(alert.row))));
    }
    alerts.forEach((alert) => seenAlertIdsRef.current.add(alert.id));
    fresh.forEach((alert) => latestSeenAlertRef.current.set(alertIdentity(alert.row), alert));
    if (!fresh.length) {
      setLatestAlertCount(0);
      return;
    }

    const ordered = fresh.sort((a, b) => liveAlertRank(a) - liveAlertRank(b) || scoreOf(b.row) - scoreOf(a.row));
    setLatestAlertCount(ordered.length);
    if (popupEnabled) {
      setLiveAlerts((current) => [...ordered, ...current]);
    }
    if (soundEnabled && audioInteractedRef.current && !monitorV3) {
      void playLiveAlertVoice(ordered[0].tier, data.meta?.voice_alert?.voice_file).then(async (voicePlayed) => {
        if (!legacyAlertsActiveRef.current) return;
        const played = voicePlayed || (await playLiveAlertSound(ordered[0].tier));
        if (!legacyAlertsActiveRef.current) {
          stopLegacyLiveAlertAudio();
          return;
        }
        if (played) {
          setSoundStatus("已播");
        } else {
          setSoundStatus("浏览器未放行");
        }
      });
    }
    if (browserNoticeEnabled && notificationPermission === "granted" && !monitorV3) {
      ordered.forEach((alert) => {
        try {
          new Notification(alert.title, {
            body: `${alert.reason} · ${fmtMoney(marketCapOf(alert.row))}`,
            tag: alert.id,
          });
        } catch {
          // Notification support varies across embedded browsers.
        }
      });
    }
    if (gmgnAutoOpenEnabled && ordered[0].tier !== "invalidated") {
      openGmgnTokenPage(ordered[0].row);
    }

    const previousTitle = document.title;
    document.title = `(${ordered.length}) Alpha 雷达`;
    const timer = window.setTimeout(() => {
      document.title = previousTitle;
      setLatestAlertCount(0);
    }, 12_000);
    return () => {
      window.clearTimeout(timer);
      document.title = previousTitle;
    };
  }, [browserNoticeEnabled, data, gmgnAutoOpenEnabled, monitorV3, notificationPermission, popupEnabled, soundEnabled, view]);

  useEffect(() => {
    if (!monitorV3) return;
    const now = Date.now();
    const alerts = activeMonitorAlerts(monitorV3, now);
    const activeIds = new Set(alerts.map(monitorAlertDisplayKey));
    setMonitorAlerts((current) => current.filter((alert) => activeIds.has(monitorAlertDisplayKey(alert))));

    let fresh: MonitorAlert[];
    if (!monitorAlertBaselineReadyRef.current) {
      seenMonitorAlertIdsRef.current = new Set(alerts.map(monitorAlertDisplayKey));
      monitorAlertBaselineReadyRef.current = true;
      fresh = startupMonitorAlerts(alerts, now);
    } else {
      fresh = startupMonitorAlerts(
        alerts.filter((alert) => !seenMonitorAlertIdsRef.current.has(monitorAlertDisplayKey(alert))),
        now,
      );
      alerts.forEach((alert) => seenMonitorAlertIdsRef.current.add(monitorAlertDisplayKey(alert)));
    }
    if (!fresh.length) return;

    if (popupEnabled) {
      setMonitorAlerts((current) => {
        const merged = new Map(current.map((alert) => [monitorAlertDisplayKey(alert), alert]));
        fresh.forEach((alert) => merged.set(monitorAlertDisplayKey(alert), alert));
        return [...merged.values()].sort(
          (left, right) => Date.parse(right.observed_at) - Date.parse(left.observed_at),
        );
      });
    }
    const lead = fresh[0];
    if (soundEnabled && audioInteractedRef.current) {
      const tier = monitorAlertSoundTier(lead);
      const speechText = monitorAlertSpeechText(lead);
      void Promise.resolve(speakLiveAlertText(speechText)).then(async (speechPlayed) => {
        if (!monitorAlertsActiveRef.current) return;
        const played = speechPlayed || (await playLiveAlertVoice(tier)) || (await playLiveAlertSound(tier));
        setSoundStatus(played ? "已播" : "浏览器未放行");
      });
    }
    if (browserNoticeEnabled && notificationPermission === "granted") {
      fresh.forEach((alert) => {
        try {
          new Notification(monitorAlertTitle(alert), {
            body: monitorAlertBody(alert),
            tag: monitorAlertDisplayKey(alert),
          });
        } catch {
          // Notification support varies across embedded browsers.
        }
      });
    }

    const previousTitle = document.title;
    document.title = `(${fresh.length}) MEME V3 提醒`;
    const timer = window.setTimeout(() => {
      document.title = previousTitle;
    }, 12_000);
    return () => {
      window.clearTimeout(timer);
      document.title = previousTitle;
    };
  }, [browserNoticeEnabled, monitorV3, notificationPermission, popupEnabled, soundEnabled]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setLiveAlerts((current) => current.filter((alert) => alert.expiresAt > Date.now()));
      setMonitorAlerts((current) =>
        current.filter((alert) => {
          const observedAt = Date.parse(alert.observed_at);
          return Number.isFinite(observedAt) && Date.now() - observedAt <= 15 * 60_000;
        }),
      );
    }, REPORT_REFETCH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);

  function handleTogglePopup() {
    setPopupEnabled((current) => {
      const next = !current;
      setStoredBoolean(LIVE_ALERT_POPUP_STORAGE_KEY, next);
      return next;
    });
  }

  function handleToggleSound() {
    const next = !soundEnabled;
    setSoundEnabled(next);
    setStoredBoolean(LIVE_ALERT_SOUND_STORAGE_KEY, next);
    if (next) void handleTestSound();
  }

  async function handleTestSound() {
    audioInteractedRef.current = true;
    if (!("AudioContext" in window) && !("webkitAudioContext" in window) && !("Audio" in window)) {
      setSoundStatus("不支持");
      return;
    }
    if (!soundEnabled) {
      setSoundEnabled(true);
      setStoredBoolean(LIVE_ALERT_SOUND_STORAGE_KEY, true);
    }
    try {
      const unlocked = await unlockLiveAlertAudio();
      const played = (await playLiveAlertVoice("discovered")) || (await playLiveAlertSound("discovered"));
      setSoundStatus(unlocked || played ? "已播" : "浏览器未放行");
    } catch {
      setSoundStatus("浏览器未放行");
    }
  }

  async function handleRequestBrowserNotice() {
    if (!("Notification" in window)) {
      setNotificationPermission("unsupported");
      setBrowserNoticeEnabled(false);
      setStoredBoolean(LIVE_ALERT_BROWSER_STORAGE_KEY, false);
      return;
    }
    const permission = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
    setNotificationPermission(permission);
    const enabled = permission === "granted";
    setBrowserNoticeEnabled(enabled);
    setStoredBoolean(LIVE_ALERT_BROWSER_STORAGE_KEY, enabled);
  }

  function handleToggleBrowserNotice() {
    if (browserNoticeEnabled) {
      setBrowserNoticeEnabled(false);
      setStoredBoolean(LIVE_ALERT_BROWSER_STORAGE_KEY, false);
      return;
    }
    void handleRequestBrowserNotice();
  }

  async function handleConnectWallet() {
    if (!window.ethereum) {
      setWalletStatus("未检测到浏览器钱包");
      return;
    }
    setWalletStatus("连接中");
    try {
      const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
      const chainId = await window.ethereum.request({ method: "eth_chainId" });
      const address = Array.isArray(accounts) && typeof accounts[0] === "string" ? accounts[0] : "";
      if (!/^0x[0-9a-fA-F]{40}$/.test(address)) {
        setWalletStatus("地址无效");
        return;
      }
      const chain = String(chainId || "").toLowerCase() === "0x38" ? "BSC" : String(chainId || "未知链");
      setWalletAddress(address);
      setWalletChain(chain);
      setWalletStatus("已连接");
      setStoredText(WALLET_ADDRESS_STORAGE_KEY, address);
      setStoredText(WALLET_CHAIN_STORAGE_KEY, chain);
    } catch {
      setWalletStatus("连接未完成");
    }
  }

  function handleDisconnectWallet() {
    setWalletAddress(null);
    setWalletChain(null);
    setWalletStatus("未连接");
    setStoredText(WALLET_ADDRESS_STORAGE_KEY, null);
    setStoredText(WALLET_CHAIN_STORAGE_KEY, null);
  }

  function handleToggleGmgnAutoOpen() {
    setGmgnAutoOpenEnabled((current) => {
      const next = !current;
      setStoredBoolean(LIVE_ALERT_GMGN_OPEN_STORAGE_KEY, next);
      return next;
    });
  }

  function handleDismissLiveAlert(id: string) {
    setLiveAlerts((current) => current.filter((alert) => alert.id !== id));
  }

  function handleDismissMonitorAlert(key: string) {
    setMonitorAlerts((current) => current.filter((alert) => monitorAlertDisplayKey(alert) !== key));
  }

  function handleSelectLiveAlert(row: RadarRow) {
    setSelected(row);
    if (view !== "memePotential") setView("memePotential");
  }

  function handleOpenMonitorMarket(row: MonitorTokenView) {
    const url = monitorMarketUrl(row);
    if (!url) return;
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function handleOpenMonitorAlertMarket(alert: MonitorAlert) {
    const url = monitorAlertMarketUrl(alert);
    if (!url) return;
    window.open(url, "_blank", "noopener,noreferrer");
  }

  const currentRows = useMemo(() => {
    const raw =
      view === "memePotential"
        ? []
        : view === "meme"
          ? rows.meme
          : view === "alpha"
            ? rows.alpha
            : view === "dealer"
              ? rows.dealer
              : view === "alerts"
                ? rows.alerts
                : rows.recommend.map((item) => item.row);
    const q = query.trim().toLowerCase();
    return q ? raw.filter((row) => `${row.symbol || ""} ${row.name || ""} ${row.chain || ""}`.toLowerCase().includes(q)) : raw;
  }, [query, rows, view]);

  const liveQuoteTargets = useMemo(
    () => view === "memePotential" ? monitorLiveQuoteTargets(monitorV3) : liveQuoteTargetsFromRows(currentRows, 80),
    [currentRows, monitorV3, view],
  );
  const liveQuoteTargetKey = liveQuoteTargets.map(liveQuoteKey).join("|");
  const liveQuotes = useQuery({
    queryKey: ["live-dex-quotes", liveQuoteTargetKey],
    queryFn: () => fetchLiveDexQuotes(liveQuoteTargets),
    enabled: Boolean(liveQuoteTargets.length),
    retry: 0,
    staleTime: 5_000,
    refetchInterval: 12_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const liveCurrentRows = useMemo(
    () => currentRows.map((row) => rowWithLiveQuoteMap(row, liveQuotes.data)),
    [currentRows, liveQuotes.data],
  );
  const monitorDisplayQuotes = useMemo(() => {
    const index: LiveDexQuoteMap = {};
    for (const token of monitorV3?.tokens || []) {
      const key = liveQuoteKey({ kind: "token", chain: token.identity.chain, address: token.identity.contract_address });
      if (liveQuotes.data?.[key]) index[token.id] = liveQuotes.data[key];
    }
    return index;
  }, [liveQuotes.data, monitorV3]);
  const liveRowByKey = useMemo(() => new Map(liveCurrentRows.map((row) => [rowKey(row), row])), [liveCurrentRows]);
  const currentRowKeySet = useMemo(() => new Set(currentRows.map(rowKey)), [currentRows]);
  const liveGoldTierItems = useMemo(
    () =>
      rows.memePotential
        .filter(({ row }) => currentRowKeySet.has(rowKey(row)))
        .map((item) => ({ ...item, row: liveRowByKey.get(rowKey(item.row)) || item.row })),
    [currentRowKeySet, liveRowByKey, rows.memePotential],
  );
  const liveRecommendItems = useMemo(
    () => rows.recommend.map((item) => ({ ...item, row: liveRowByKey.get(rowKey(item.row)) || item.row })),
    [liveRowByKey, rows.recommend],
  );
  const validatedRows = useMemo(() => validatedReportRows(data), [data]);
  const focus = selected ? rowWithLiveQuoteMap({ ...selected, ...liveRowByKey.get(rowKey(selected)), ...validatedRows.get(alertIdentity(selected)) }, liveQuotes.data) : undefined;
  const meta = data?.meta || {};
  const coverage = meta.holder_coverage || {};
  const localPullTime = formatLocalPullTime(
    view === "memePotential" ? monitorDataUpdatedAt : dataUpdatedAt,
  );
  const workspaceUpdatedAt = view === "memePotential"
    ? monitorV3?.observed_at || meta.report_generated_at || meta.generated_at || "--"
    : meta.report_generated_at || meta.generated_at || "--";
  const scanState = isLoading ? "加载中" : isError ? "失败" : isFetching ? "刷新中" : "在线";
  const activeGoldCount = rows.memePotential.filter(({ row }) => goldTierOf(row) !== "review").length;
  return (
    <main className={`ot-app-shell alpha-terminal ${view === "memePotential" || view === "meme" ? "radar-operational" : ""}`}>
      <div className="terminal-scan" />
      <aside className="left-rail">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            <span className="brand-orbit" />
            <Radar className="brand-sigil" size={20} strokeWidth={1.8} />
          </span>
          <div>
            <strong>Alpha 雷达</strong>
            <small>Meme · Alpha · 庄家监控</small>
          </div>
        </div>
        <nav>
          {(Object.keys(VIEW_META) as ViewKey[]).map((key) => (
            <button key={key} type="button" aria-label={VIEW_META[key].label} title={VIEW_META[key].label} className={view === key ? "active" : ""} onClick={() => { setView(key); setSelected(undefined); }}>
              {key === "recommend" && <Sparkles size={16} />}
              {key === "memePotential" && <Star size={16} />}
              {key === "meme" && <Flame size={16} />}
              {key === "alpha" && <Gauge size={16} />}
              {key === "dealer" && <ShieldCheck size={16} />}
              {key === "alerts" && <Bell size={16} />}
              {key === "replay" && <BarChart3 size={16} />}
              <span>{VIEW_META[key].label}</span>
            </button>
          ))}
        </nav>
        <section className="rail-card">
          <span>扫描状态</span>
          <strong>{scanState}</strong>
          <small>{view === "memePotential" ? `${MONITOR_REFETCH_INTERVAL_MS / 1000}秒实时刷新` : `${REPORT_REFETCH_INTERVAL_MS / 1000}秒自动刷新`} · 拉取 {localPullTime}</small>
        </section>
        {view !== "memePotential" && <section className="rail-card">
          <span>筹码覆盖</span>
          <strong>{plainPct(coverage.coverage_pct)}</strong>
          <small>
            {coverage.with_top10 || 0} / {coverage.unique_tokens || 0}
          </small>
        </section>}
      </aside>

      <section className="terminal-layout">
        <header className="top-strip">
          <div>
            <b>Alpha 雷达工作台</b>
            <span>
              更新于 {workspaceUpdatedAt} · 拉取 {localPullTime}
              {isFetching ? " · 刷新中" : ""}
            </span>
          </div>
          {view !== "memePotential" && <div className="search-box">
            <Search size={16} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜币名 / 链 / 合约" />
          </div>}
        </header>

        {!selected && view !== "memePotential" && <section className="hero-console">
          <div>
            <span className="status-pill">
              <Activity size={14} /> 只读模式
            </span>
            <h1>{VIEW_META[view].title}</h1>
            <p>{VIEW_META[view].desc}</p>
          </div>
          <div className="hero-stats">
            <StatCard label="当前金狗候选" value={String(activeGoldCount)} detail="确认 / 早鸟 / 新发现 / 复活" />
            <StatCard label="Meme热度" value={String(rows.meme.length)} detail="市场全局扫描" />
            <StatCard label="Alpha候选" value={String(rows.alpha.length)} detail="来自 Binance Alpha" />
            <StatCard label="庄家雷达" value={String(rows.dealer.length)} detail="Alpha 内部排序" />
          </div>
        </section>}

        {!selected && view !== "memePotential" && (
          <SystemModelPanel
            report={data}
            showMonitoring={view === "meme"}
            showRating={false}
            popupEnabled={popupEnabled}
            soundEnabled={soundEnabled}
            soundStatus={soundStatus}
            browserEnabled={browserNoticeEnabled}
            gmgnAutoOpenEnabled={gmgnAutoOpenEnabled}
            notificationPermission={notificationPermission}
            latestCount={latestAlertCount}
            onTogglePopup={handleTogglePopup}
            onToggleSound={handleToggleSound}
            onTestSound={handleTestSound}
            onRequestBrowserNotice={handleRequestBrowserNotice}
            onToggleGmgnAutoOpen={handleToggleGmgnAutoOpen}
            walletAddress={walletAddress}
            walletChain={walletChain}
            walletStatus={walletStatus}
            onConnectWallet={handleConnectWallet}
            onDisconnectWallet={handleDisconnectWallet}
          />
        )}

        <section className="workbench full-width-workbench">
          <section className="center-board" hidden={Boolean(selected)}>
            {view === "replay" ? (
              <>
                <RatingV2Panel rating={meta.rating_v2} />
                <ReplayBoard report={data} onSelect={setSelected} />
              </>
            ) : view === "memePotential" ? (
              <MemeMonitorView
                snapshot={monitorV3}
                imageByToken={monitorTokenImages}
                quoteByToken={monitorDisplayQuotes}
                baselineByToken={liveMonitorData?.monitor_baselines || {}}
                intelligenceByToken={monitorTokenIntelligence}
                onOpenMarket={handleOpenMonitorMarket}
                activeAlerts={popupEnabled ? monitorAlerts : []}
                onDismissAlert={handleDismissMonitorAlert}
                onOpenAlertMarket={handleOpenMonitorAlertMarket}
                alertControls={{
                  popupEnabled,
                  soundEnabled,
                  browserNoticeEnabled,
                  onTogglePopup: handleTogglePopup,
                  onToggleSound: handleToggleSound,
                  onToggleBrowserNotice: handleToggleBrowserNotice,
                }}
              />
            ) : view === "recommend" ? (
              <>
                <RecommendationTape items={liveRecommendItems} onSelect={setSelected} mode="default" />
              </>
            ) : (
              <ScreenerTape rows={liveCurrentRows} selected={focus} onSelect={setSelected} variant={view} />
            )}
            {view !== "memePotential" && <section className="chart-panel">
              <div>
                <h3>信号热度</h3>
                <span>按当前榜单分数绘制</span>
              </div>
              <MiniTape rows={liveCurrentRows} />
            </section>}
          </section>
          {selected && <section className="expanded-detail">
            <button type="button" className="detail-back" onClick={() => setSelected(undefined)}><ArrowLeft size={18} />返回列表</button>
            <DetailPanel key={focus ? rowKey(focus) : "empty"} row={focus} />
          </section>}
        </section>
      </section>
      {view !== "memePotential" && <LiveAlertDock alerts={liveAlerts} onSelect={handleSelectLiveAlert} onDismiss={handleDismissLiveAlert} />}
    </main>
  );
}
