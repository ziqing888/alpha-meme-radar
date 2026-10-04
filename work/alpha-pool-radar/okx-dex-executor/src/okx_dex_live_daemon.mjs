import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { TerminalControlProtocol, terminalControlPath, readTerminalCommand,
  fetchPendingTransactions, acquireDaemonLock } from './terminal_control_protocol.mjs';

import {
  executeBuy,
  executeSell,
  executionChain,
  fetchPendingIntentEvidence,
  preflight,
  quoteBuyTradeability,
  quoteSellValue,
  readWalletTokenBalance,
  redact,
  selectStrategyCandidate,
} from './okx_dex_executor.mjs';

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const POOL_ID = /^0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/;
const STATE_VERSION = 1;
const DEFAULT_AMOUNT_NATIVE_ATOMIC = '1000000000000000';
const DEFAULT_MAX_OPEN_POSITIONS = 3;
const STOP_LOSS_PCT = 22;
const TP1_RETURN_PCT = 100;
const TP1_ORIGINAL_FRACTION = 0.5;
const TP2_RETURN_PCT = 200;
const TP2_ORIGINAL_FRACTION = 0.1;
const TP3_RETURN_PCT = 400;
const TP3_ORIGINAL_FRACTION = 0.1;
const TRAIL_DRAWDOWN_PCT = 40;
const INITIAL_TIME_STOP_MS = 90 * 60 * 1000;
const RUNNER_TIME_STOP_MS = 24 * 60 * 60 * 1000;
const STRATEGY_VERSION = 'chain_v2';
const EXACT_ENTRY_QUOTE_MAX_AGE_SECONDS = 30;
const EXACT_ENTRY_TRADEABILITY_TIMEOUT_MS = 12_000;
const SELLABILITY_SEQUENCE_MAX_GAP_SECONDS = 120;
const EMERGENCY_LIQUIDITY_DROP_PCT = 30;
const EMERGENCY_SELL_IMPACT_PCT = 15;
const EMERGENCY_ROUTE_DIVERGENCE_PCT = 35;
const ALTERNATE_ROUTE_MIN_IMPROVEMENT_PCT = 5;
const PRE_SUBMISSION_FAILURES = new Set([
  'buy_quote_failed', 'buy_native_balance_failed', 'insufficient_native_balance',
  'buy_balance_before_failed', 'buy_simulation_failed', 'buy_sign_failed', 'sell_quote_failed',
  'sell_simulation_failed', 'sell_sign_failed', 'sdk_preflight_failed',
  'buy_approval_failed', 'sell_approval_failed',
  'buy_economics_unavailable', 'buy_round_trip_loss_exceeded', 'buy_price_impact_exceeded',
  'sell_price_impact_exceeded', 'insufficient_native_gas_balance', 'buy_honeypot_detected',
]);
const unresolvedIntent = intent => intent && !['settled', 'not_submitted', 'reverted'].includes(intent.state);

function normalizedReceiptStatus(status) {
  if (status === 0 || status === 0n || status === '0' || status === '0x0') return 0;
  if (status === 1 || status === 1n || status === '1' || status === '0x1') return 1;
  return null;
}

export function classifyPendingIntentEvidence(intent, evidence) {
  const unknown = { classification: 'unknown' };
  if (!intent || !['buy', 'sell'].includes(intent.side) || !evidence?.receipt) return unknown;
  const txHash = String(evidence.receipt.transactionHash || evidence.receipt.hash || '').toLowerCase();
  if (!/^0x[0-9a-f]{64}$/.test(txHash)) return unknown;
  const matchingTx = Array.isArray(intent.transactions) && intent.transactions.some(tx =>
    tx?.side === intent.side && ['broadcast', 'confirmed'].includes(tx.stage)
      && String(tx.tx_hash || '').toLowerCase() === txHash);
  if (!matchingTx) return unknown;
  const status = normalizedReceiptStatus(evidence.receipt.status);
  if (status === 0) return { classification: 'reverted', tx_hash: txHash };
  if (status !== 1) return unknown;
  const tokenDelta = evidence.wallet_deltas?.token_atomic;
  const nativeDelta = evidence.wallet_deltas?.native_atomic;
  if (typeof tokenDelta !== 'string' || !/^-?\d+$/.test(tokenDelta)
      || typeof nativeDelta !== 'string' || !/^-?\d+$/.test(nativeDelta)) return unknown;
  const tokenAtomic = BigInt(tokenDelta);
  if ((intent.side === 'buy' && tokenAtomic <= 0n) || (intent.side === 'sell' && tokenAtomic >= 0n)) return unknown;
  return { classification: 'confirmed', tx_hash: txHash,
    token_delta_atomic: tokenAtomic.toString(), native_delta_atomic: BigInt(nativeDelta).toString() };
}

function executableSellQuote(quote) {
  if (!quote || !String(quote.route_id || '').trim()) return false;
  const net = atomicBigInt(quote.estimated_net_native_atomic ?? quote.expected_native_atomic, -1n);
  return net > 0n;
}

function alternateMateriallyBetter(primary, alternate) {
  if (!executableSellQuote(alternate)) return false;
  const alternateRoute = String(alternate?.route_id || '').trim();
  const primaryRoute = String(primary?.route_id || '').trim();
  if (!alternateRoute || (primaryRoute && alternateRoute === primaryRoute)) return false;
  if (!executableSellQuote(primary)) return true;
  const primaryNet = atomicBigInt(primary.estimated_net_native_atomic ?? primary.expected_native_atomic);
  const alternateNet = atomicBigInt(alternate.estimated_net_native_atomic ?? alternate.expected_native_atomic);
  return alternateNet * 100n >= primaryNet * BigInt(100 + ALTERNATE_ROUTE_MIN_IMPROVEMENT_PCT);
}

function retryableBeforeSwapBroadcast(intent) {
  if (!intent || !PRE_SUBMISSION_FAILURES.has(intent.reason)) return false;
  const transactions = Array.isArray(intent.transactions) ? intent.transactions : [];
  return transactions.every(tx => tx?.side === 'approval' && tx?.stage === 'confirmed');
}

function preserveRetryableExit(state, key, intent, now) {
  if (intent?.side !== 'sell') return;
  const position = state.positions?.[key];
  if (!position) return;
  try {
    const amount = BigInt(intent.amount_atomic);
    const remaining = BigInt(position.remaining_atomic);
    if (amount <= 0n || amount > remaining) return;
    position.retry_exit = {
      amount_atomic: amount.toString(),
      reason: intent.exit_reason || (amount < remaining ? 'take_profit_1' : 'retry_pending_exit'),
      triggered_at: intent.created_at || nowIso(now),
    };
  } catch {
    // Invalid legacy intent data remains visible for manual reconciliation.
  }
}

function moduleRoot() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, '..', '..', '..', '..');
}

export function defaultPaths(chainName = 'bsc', live = true) {
  const root = moduleRoot();
  const prefix = chainName === 'bsc' ? 'bsc' : chainName;
  const mode = live ? 'live' : 'dry-run';
  const stateStem = chainName === 'bsc' ? `okx-dex-sdk-${mode}` : `okx-dex-sdk-${chainName}-${mode}`;
  return {
    inputPath: path.join(root, 'outputs', `${prefix}-execution-input.json`),
    statePath: path.join(root, 'outputs', `${stateStem}-state.json`),
    statusPath: path.join(root, 'outputs', `${stateStem}-status.json`),
  };
}

function definedOptions(options) {
  return Object.fromEntries(Object.entries(options).filter(([, value]) => value !== undefined));
}

function nowIso(now) {
  return (now instanceof Date ? now : new Date(now)).toISOString();
}

const TRANSIENT_RENAME_ERRORS = new Set(['EACCES', 'EBUSY', 'EPERM']);
const renameWaitBuffer = new Int32Array(new SharedArrayBuffer(4));

export function atomicWrite(file, payload, { renameRetries = 40, retryDelayMs = 25 } = {}) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
  const fd = fs.openSync(temporary, 'w', 0o600);
  try {
    fs.writeFileSync(fd, `${JSON.stringify(redact(payload), null, 2)}\n`);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  for (let attempt = 0; ; attempt += 1) {
    try {
      fs.renameSync(temporary, file);
      break;
    } catch (error) {
      if (!TRANSIENT_RENAME_ERRORS.has(error?.code) || attempt >= renameRetries) throw error;
      if (retryDelayMs > 0) Atomics.wait(renameWaitBuffer, 0, 0, retryDelayMs);
    }
  }
}

function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function publicError(code) {
  const text = String(code || '');
  return /^[a-z][a-z0-9_]{0,100}$/.test(text) ? text : 'sdk_daemon_error';
}

function publicDiagnostic(error) {
  const detailReason = publicError(error?.detail_reason);
  const rpcCode = Number(error?.rpc_code);
  const rpcMessage = String(error?.rpc_message || '')
    .replace(/[\r\n\t]/g, ' ')
    .replace(/0x[0-9a-f]{40,}/gi, '[hex]')
    .trim()
    .slice(0, 200);
  return {
    ...(detailReason !== 'sdk_daemon_error' ? { detail_reason: detailReason } : {}),
    ...(Number.isFinite(rpcCode) ? { rpc_code: rpcCode } : {}),
    ...(rpcMessage ? { rpc_message: rpcMessage } : {}),
  };
}

function address(value, code = 'invalid_address') {
  const text = String(value || '').trim();
  if (!ADDRESS.test(text) || /^0x0{40}$/i.test(text)) throw new Error(code);
  return text.toLowerCase();
}

function poolId(value, code = 'invalid_pool') {
  const text = String(value || '').trim();
  if (!POOL_ID.test(text) || /^0x0+$/i.test(text)) throw new Error(code);
  return text.toLowerCase();
}

function positionId(row, chainName) {
  return `${chainName}:${address(row.contract_address || row.token, 'invalid_token')}`;
}

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

async function fetchNativeUsdPrice(chainName, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch;
  if (typeof fetchImpl !== 'function') throw new Error('native_usd_price_unavailable');
  const instrument = chainName === 'bsc' ? 'BNB-USDT' : 'ETH-USDT';
  try {
    const response = await fetchImpl(`https://www.okx.com/api/v5/market/ticker?instId=${instrument}`, {
      headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' },
      signal: AbortSignal.timeout(6_000),
    });
    if (!response?.ok) throw new Error('native_usd_price_unavailable');
    const payload = await response.json();
    const price = number(payload?.data?.[0]?.last);
    if (price <= 0) throw new Error('native_usd_price_unavailable');
    return price;
  } catch (error) {
    if (error?.message === 'native_usd_price_unavailable') throw error;
    throw new Error('native_usd_price_unavailable');
  }
}

function nativeAtomicForUsd(notionalUsd, nativePriceUsd) {
  const notional = number(notionalUsd);
  const price = number(nativePriceUsd);
  if (notional <= 0 || notional > 100 || price <= 0) throw new Error('invalid_usd_order_size');
  const atomic = BigInt(Math.floor((notional / price) * 1e18));
  if (atomic <= 0n) throw new Error('invalid_usd_order_size');
  return atomic.toString();
}

function uintString(value, code = 'invalid_amount') {
  const text = String(value || '').trim();
  if (!/^[0-9]{1,78}$/.test(text) || BigInt(text) <= 0n) throw new Error(code);
  return text;
}

function quoteIdentity(row) {
  return [
    String(row?.chain || '').toLowerCase(),
    String(row?.contract_address || row?.token_address || row?.address || row?.token || '').toLowerCase(),
    String(row?.pool_address || row?.pair_address || row?.pool || '').toLowerCase(),
  ].join(':');
}

function legacyStrategyInputAllowed(env) {
  return String(env?.OKX_ALLOW_LEGACY_STRATEGY_INPUT || '').trim() === '1';
}

function quoteMap(payload) {
  const quotes = new Map();
  for (const quote of Array.isArray(payload?.quotes) ? payload.quotes : []) {
    quotes.set(quoteIdentity(quote), quote);
  }
  return quotes;
}

function candidateQuote(candidate, quotes) {
  return quotes.get(quoteIdentity(candidate)) || candidate;
}

async function fetchPositionQuote(position, chainName, now, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch;
  if (typeof fetchImpl !== 'function') return null;
  const token = address(position?.contract_address || position?.token, 'invalid_token');
  const url = `https://api.dexscreener.com/tokens/v1/${encodeURIComponent(chainName)}/${token}`;
  try {
    const response = await fetchImpl(url, {
      headers: {
        Accept: 'application/json',
        'Cache-Control': 'no-cache',
        'User-Agent': 'AlphaRadar/2.0',
      },
      signal: AbortSignal.timeout(6_000),
    });
    if (!response?.ok) return null;
    const payload = await response.json();
    const matches = (Array.isArray(payload) ? payload : []).filter((pair) => (
      String(pair?.chainId || '').toLowerCase() === chainName
      && String(pair?.baseToken?.address || '').toLowerCase() === token
      && number(pair?.priceUsd) > 0
      && number(pair?.liquidity?.usd) > 0
    ));
    if (!matches.length) return null;
    const pinnedPool = String(position?.pool_address || position?.pool || '').toLowerCase();
    const pinned = matches.find((pair) => String(pair?.pairAddress || '').toLowerCase() === pinnedPool);
    const pair = pinned || matches.sort((left, right) => number(right?.liquidity?.usd) - number(left?.liquidity?.usd))[0];
    return {
      chain: chainName,
      contract_address: token,
      pool_address: String(pair?.pairAddress || pinnedPool).toLowerCase(),
      price_usd: number(pair.priceUsd),
      mcap: number(pair.marketCap) > 0 ? number(pair.marketCap) : null,
      market_cap: number(pair.marketCap) > 0 ? number(pair.marketCap) : null,
      fdv: number(pair.fdv) > 0 ? number(pair.fdv) : null,
      valuation_type: number(pair.marketCap) > 0 ? 'market_cap' : number(pair.fdv) > 0 ? 'fdv' : 'unavailable',
      market_cap_source: number(pair.marketCap) > 0 ? 'dexscreener' : null,
      liquidity: number(pair?.liquidity?.usd),
      change_m5: number(pair?.priceChange?.m5, -999),
      change_h1: number(pair?.priceChange?.h1, -999),
      quote_status: 'fresh',
      quote_source: 'dexscreener_position_refresh',
      quote_at: nowIso(now),
    };
  } catch {
    return null;
  }
}

function ageSeconds(value, now) {
  const parsed = Date.parse(String(value || ''));
  if (!Number.isFinite(parsed)) return null;
  return (now.getTime() - parsed) / 1000;
}

function sourceGroups(row) {
  const values = [];
  for (const field of ['source_labels', 'sources', 'source_groups', 'source_names']) {
    const value = row?.[field];
    if (Array.isArray(value)) values.push(...value);
    else if (value) values.push(value);
  }
  if (row?.source_hit_counts && typeof row.source_hit_counts === 'object') {
    values.push(...Object.keys(row.source_hit_counts));
  }
  const groups = new Set();
  for (const raw of values) {
    const value = String(raw || '').trim().toLowerCase();
    if (value.includes('okx')) groups.add('okx');
    if (value.includes('gmgn')) groups.add('gmgn');
    if (value.includes('dexscreener') || value === 'ds') groups.add('ds');
  }
  return groups;
}

export function chainEntryPolicy(chainName) {
  if (chainName === 'robinhood') return Object.freeze({
    chain: 'robinhood', signalStage: 'aggregate_early_bird', entryRoute: 'robinhood_aggregate_early_bird',
    amountUsd: 2, maxAgeSeconds: 600, minFirstMcapUsd: 10_000, maxFirstMcapUsd: 300_000,
    maxMarkup: 1.25, minLiquidityUsd: 8_000, maxRoundTripLossPct: 25,
    maxBuyImpactPct: 12, maxSellImpactPct: null, minSellableCycles: 1,
    requiresObservedSell: false, maxOpenPositions: 3, maxOpenNotionalUsd: 15,
    dailyLossStopUsd: 5,
  });
  if (chainName === 'bsc') return Object.freeze({
    chain: 'bsc', signalStage: 'aggregate_early_bird', entryRoute: 'bsc_aggregate_early_bird',
    amountUsd: 1, maxAgeSeconds: 600, minFirstMcapUsd: 10_000, maxFirstMcapUsd: 100_000,
    maxMarkup: 1.5, minLiquidityUsd: 8_000, maxRoundTripLossPct: 25,
    maxBuyImpactPct: null, maxSellImpactPct: 12, minSellableCycles: 2,
    requiresObservedSell: true, maxOpenPositions: 2, maxOpenNotionalUsd: 2,
    dailyLossStopUsd: 2,
  });
  throw new Error('unsupported_execution_chain');
}

const V2_REQUIRED_FIELDS = Object.freeze([
  'strategy_version', 'signal_stage', 'entry_route', 'execution_mode', 'rank_score',
  'rank_components', 'first_seen_at', 'first_price_usd', 'first_mcap_usd',
  'current_price_usd', 'current_mcap_usd', 'entry_delay_seconds', 'markup_from_first',
]);
const PROMOTED_SIGNAL_STAGES = new Set(['aggregate_early_bird', 'aggregate_confirmation']);

function acceptedSignalStage(stage, policy) {
  return stage === policy.signalStage || PROMOTED_SIGNAL_STAGES.has(stage);
}

function expectedEntryRoute(chainName, stage, policy) {
  return PROMOTED_SIGNAL_STAGES.has(stage) ? `${chainName}_${stage}` : policy.entryRoute;
}

function v2ContractComplete(candidate) {
  if (!candidate || V2_REQUIRED_FIELDS.some(field => !Object.hasOwn(candidate, field))) return false;
  if (candidate.strategy_version !== STRATEGY_VERSION) return false;
  if (!Number.isFinite(Number(candidate.rank_score))) return false;
  if (!candidate.rank_components || typeof candidate.rank_components !== 'object' || Array.isArray(candidate.rank_components)) return false;
  return true;
}

function v2EntryGate(candidate, quote, now, chainName, { allowDiscoveryReference = false } = {}) {
  if (!Object.hasOwn(candidate || {}, 'rank_score') || candidate?.rank_score === null
      || candidate?.rank_score === '' || !Number.isFinite(Number(candidate?.rank_score))) {
    return { accepted: false, reason: `${chainName}_rank_unavailable` };
  }
  if (!v2ContractComplete(candidate)) return { accepted: false, reason: 'chain_v2_contract_incomplete' };
  const policy = chainEntryPolicy(chainName);
  if (String(candidate.chain || '').toLowerCase() !== chainName) return { accepted: false, reason: 'chain_v2_chain_mismatch' };
  if (chainName === 'bsc' && (candidate.execution_mode === 'shadow' || candidate.signal_stage === 'aggregate_discovery')) {
    return { accepted: false, reason: 'bsc_shadow_only' };
  }
  if (candidate.execution_mode !== 'live_candidate') return { accepted: false, reason: 'chain_v2_execution_mode_required' };
  if (!acceptedSignalStage(candidate.signal_stage, policy)) return { accepted: false, reason: `${chainName}_stage_required` };
  if (candidate.entry_route !== expectedEntryRoute(chainName, candidate.signal_stage, policy)) {
    return { accepted: false, reason: `${chainName}_route_required` };
  }
  const hardRisk = candidate?.policy_checks?.hard_risk;
  if ((chainName === 'bsc' && hardRisk !== true)
      || (chainName === 'robinhood' && hardRisk === false)) {
    return { accepted: false, reason: 'chain_v2_policy_checks_required' };
  }
  const quoteAge = ageSeconds(quote?.quote_at || quote?.quote_observed_at || candidate?.quote_at, now);
  const quoteStatus = String(quote?.quote_status || '').toLowerCase();
  const quoteAgeLimit = allowDiscoveryReference && quoteStatus === 'discovery_reference'
    ? policy.maxAgeSeconds
    : EXACT_ENTRY_QUOTE_MAX_AGE_SECONDS;
  const acceptedQuoteStatus = quoteStatus === 'fresh'
    || (allowDiscoveryReference && quoteStatus === 'discovery_reference');
  if (quoteAge === null || quoteAge < 0 || quoteAge > quoteAgeLimit || !acceptedQuoteStatus) {
    return { accepted: false, reason: 'stale_quote' };
  }
  const firstPrice = number(candidate.first_price_usd, NaN);
  const currentPrice = number(quote?.price_usd ?? candidate.current_price_usd, NaN);
  const firstMcap = number(candidate.first_mcap_usd, NaN);
  const currentMcap = number(quote?.mcap ?? quote?.market_cap ?? candidate.current_mcap_usd, NaN);
  const delay = ageSeconds(candidate.first_seen_at, now);
  const markup = currentPrice / firstPrice;
  const liquidity = number(quote?.liquidity_usd ?? quote?.liquidity ?? candidate.current_liquidity_usd ?? candidate.liquidity, NaN);
  if (![firstPrice, currentPrice, firstMcap, currentMcap, delay, markup, liquidity].every(Number.isFinite)) {
    return { accepted: false, reason: 'chain_v2_contract_incomplete' };
  }
  if (firstPrice <= 0 || currentPrice <= 0 || currentMcap <= 0) return { accepted: false, reason: 'invalid_price' };
  if (delay < 0 || delay > policy.maxAgeSeconds) return { accepted: false, reason: `${chainName}_entry_window` };
  if (firstMcap < policy.minFirstMcapUsd || firstMcap > policy.maxFirstMcapUsd) return { accepted: false, reason: `${chainName}_first_mcap_range` };
  if (markup <= 0 || markup > policy.maxMarkup) return { accepted: false, reason: `${chainName}_markup_limit` };
  if (liquidity < policy.minLiquidityUsd) return { accepted: false, reason: `${chainName}_liquidity_minimum` };
  if (policy.requiresObservedSell && number(quote?.sell_count5m ?? candidate.sell_count5m, 0) < 1) {
    return { accepted: false, reason: 'bsc_observed_sell_required' };
  }
  const riskFlags = candidate?.gmgn_risk_flags || candidate?.risk_flags;
  if ((Array.isArray(riskFlags) && riskFlags.length) || (!Array.isArray(riskFlags) && riskFlags)) return { accepted: false, reason: 'risk_flags' };
  return { accepted: true, reason: 'accepted' };
}

export function strategyEntryGate(candidate, quote, now = new Date(), chainName = 'bsc') {
  if (candidate?.strategy_version === STRATEGY_VERSION) return v2EntryGate(candidate, quote, now, chainName);
  const arm = String(candidate?.execution_arm || '').trim();
  if (!['first_discovery', 'narrative_breakout'].includes(arm)) return { accepted: false, reason: 'strict_first_discovery_only' };
  if (chainName === 'bsc' && arm !== 'first_discovery') {
    return { accepted: false, reason: 'bsc_validated_first_discovery_only' };
  }

  const signalAge = ageSeconds(candidate?.signal_at, now);
  if (signalAge === null || signalAge < 0 || signalAge > 2_700) return { accepted: false, reason: 'stale_signal' };
  const quoteAge = ageSeconds(quote?.quote_at || quote?.quote_observed_at || candidate?.quote_at || candidate?.quote_observed_at, now);
  if (quoteAge === null || quoteAge < 0 || quoteAge > 30) return { accepted: false, reason: 'stale_quote' };
  if (String(quote?.quote_status || candidate?.quote_status || '').toLowerCase() !== 'fresh') return { accepted: false, reason: 'quote_not_fresh' };

  const price = number(quote?.price_usd ?? quote?.price ?? candidate?.price_usd ?? candidate?.price);
  const liquidity = number(quote?.liquidity_usd ?? quote?.liquidity ?? candidate?.liquidity_usd ?? candidate?.liquidity);
  if (price <= 0) return { accepted: false, reason: 'invalid_price' };
  if (liquidity < 8_000) return { accepted: false, reason: 'low_liquidity' };

  const firstAge = ageSeconds(candidate?.first_seen_at, now);
  if (firstAge === null || firstAge < 0 || firstAge > 45 * 60) return { accepted: false, reason: 'first_discovery_window' };
  const valuation = ['mcap', 'market_cap', 'fdv', 'valuation_type'].some(key => Object.hasOwn(quote || {}, key))
    ? quote : candidate;
  const mcap = number(valuation?.mcap ?? valuation?.market_cap);
  if (mcap <= 0 || (valuation?.valuation_type && valuation.valuation_type !== 'market_cap')) {
    return { accepted: false, reason: 'market_cap_unavailable' };
  }
  if (chainName === 'bsc') {
    const firstMcap = number(candidate?.first_mcap_usd ?? candidate?.first_seen_mcap ?? candidate?.first_mcap);
    const markup = number(candidate?.markup_from_first, firstMcap > 0 ? mcap / firstMcap : 0);
    if (firstMcap < 10_000 || firstMcap > 100_000) return { accepted: false, reason: 'bsc_validated_first_mcap' };
    if (markup <= 0 || markup > 2.2) return { accepted: false, reason: 'bsc_validated_markup' };
    if (liquidity < 20_000) return { accepted: false, reason: 'bsc_liquidity_gate' };
    const score = number(candidate?.execution_candidate_score ?? candidate?.entry_score ?? candidate?.score, -1);
    if (score < 60 || score >= 70) return { accepted: false, reason: 'bsc_score_gate' };
    const bscM5 = number(quote?.change_m5 ?? candidate?.change_m5, -999);
    if (bscM5 < 0 || bscM5 > 30) return { accepted: false, reason: 'bsc_momentum_gate' };
    const observedSells = number(quote?.sell_count5m ?? candidate?.sell_count5m, -1);
    if (observedSells < 1) return { accepted: false, reason: 'bsc_no_observed_sells' };
  }
  if (arm === 'first_discovery') {
    if (chainName !== 'bsc' && (mcap < 10_000 || mcap > 300_000)) return { accepted: false, reason: 'first_discovery_mcap' };
  } else {
    if (mcap <= 300_000 || mcap > 1_000_000) return { accepted: false, reason: 'narrative_breakout_mcap' };
    if (liquidity < 30_000) return { accepted: false, reason: 'narrative_breakout_liquidity' };
    const groups = sourceGroups(candidate);
    if (number(candidate?.source_count) < 2 || !groups.has('okx') || (!groups.has('gmgn') && !groups.has('ds'))) {
      return { accepted: false, reason: 'narrative_breakout_sources' };
    }
  }

  if (String(candidate?.watch_status || '').toLowerCase() === 'invalidated') return { accepted: false, reason: 'invalidated_status' };
  const riskFlags = candidate?.gmgn_risk_flags || candidate?.risk_flags;
  if ((Array.isArray(riskFlags) && riskFlags.length) || (!Array.isArray(riskFlags) && riskFlags)) return { accepted: false, reason: 'risk_flags' };

  const changeM5 = number(quote?.change_m5 ?? candidate?.change_m5, -999);
  const changeH1 = number(quote?.change_h1 ?? candidate?.change_h1, -999);
  const maxH1 = arm === 'narrative_breakout' ? 100 : 80;
  if (changeM5 < -25 || changeM5 > 45 || changeH1 < -20 || changeH1 > maxH1) return { accepted: false, reason: 'momentum_gate' };
  return { accepted: true, reason: 'accepted' };
}

function eligibleCandidate(input, state, quotes, chainName, now, requireV2 = true) {
  let rejectedReason = null;
  let matched = false;
  for (const row of Array.isArray(input?.signals) ? input.signals : []) {
    if (String(row?.chain || '').toLowerCase() !== chainName) continue;
    matched = true;
    if (requireV2 && row?.strategy_version !== STRATEGY_VERSION) {
      rejectedReason ||= 'chain_v2_candidate_required';
      continue;
    }
    let candidate;
    try {
      candidate = selectStrategyCandidate({ signals: [row] }, chainName);
    } catch (error) {
      rejectedReason ||= publicError(error.message);
      continue;
    }
    const id = positionId(candidate, chainName);
    if (state.positions[id] || state.seen[id]) {
      rejectedReason ||= 'duplicate_token';
      continue;
    }
    const quote = candidateQuote(candidate, quotes);
    const gate = strategyEntryGate(candidate, quote, now, chainName);
    if (!gate.accepted) {
      rejectedReason ||= gate.reason;
      continue;
    }
    return { candidate, quote, id, rejectedReason: null, matched: true };
  }
  return { candidate: null, quote: null, id: null, rejectedReason, matched };
}

function discoveryReferenceQuote(candidate, now) {
  const snapshot = candidate?.discovery_snapshot;
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
    return { quote: null, reason: 'discovery_snapshot_unavailable' };
  }
  const quoteAt = snapshot.snapshot_at || snapshot.observed_at || snapshot.quote_at
    || snapshot.quote_observed_at || snapshot.first_seen_at;
  const quoteAge = ageSeconds(quoteAt, now);
  const chainName = String(candidate?.chain || '').toLowerCase();
  let maxReferenceAge;
  try {
    maxReferenceAge = chainEntryPolicy(chainName).maxAgeSeconds;
  } catch {
    return { quote: null, reason: 'unsupported_execution_chain' };
  }
  if (quoteAge === null || quoteAge < 0 || quoteAge > maxReferenceAge) {
    return { quote: null, reason: 'stale_discovery_snapshot' };
  }
  const quote = {
    ...snapshot,
    chain: snapshot.chain || candidate.chain,
    contract_address: snapshot.contract_address || snapshot.token_address || candidate.contract_address,
    pool_address: snapshot.pool_address || snapshot.pair_address || candidate.pool_address,
    quote_at: quoteAt,
    quote_status: 'discovery_reference',
  };
  if (quoteIdentity(quote) !== quoteIdentity(candidate)) {
    return { quote: null, reason: 'discovery_snapshot_identity_mismatch' };
  }
  const marketCap = number(quote.mcap ?? quote.market_cap, NaN);
  const price = number(quote.price_usd ?? quote.price, NaN);
  const liquidity = number(quote.liquidity_usd ?? quote.liquidity, NaN);
  if (!Number.isFinite(marketCap) || marketCap <= 0 || quote.valuation_type !== 'market_cap') {
    return { quote: null, reason: 'market_cap_unavailable' };
  }
  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(liquidity) || liquidity <= 0) {
    return { quote: null, reason: 'discovery_snapshot_incomplete' };
  }
  return { quote, reason: null };
}

function eligiblePreflightCandidate(input, state, chainName, now) {
  let rejectedReason = null;
  let matched = false;
  for (const row of Array.isArray(input?.preflight_signals) ? input.preflight_signals : []) {
    if (String(row?.chain || '').toLowerCase() !== chainName) continue;
    matched = true;
    if (row?.strategy_version !== STRATEGY_VERSION) {
      rejectedReason ||= 'chain_v2_candidate_required';
      continue;
    }
    let candidate;
    try {
      candidate = selectStrategyCandidate({ signals: [row] }, chainName);
    } catch (error) {
      rejectedReason ||= publicError(error.message);
      continue;
    }
    const id = positionId(candidate, chainName);
    if (state.positions[id] || state.seen[id]) {
      rejectedReason ||= 'duplicate_token';
      continue;
    }
    const reference = discoveryReferenceQuote(candidate, now);
    if (!reference.quote) {
      rejectedReason ||= reference.reason;
      continue;
    }
    const gate = v2EntryGate(candidate, reference.quote, now, chainName, { allowDiscoveryReference: true });
    if (!gate.accepted) {
      rejectedReason ||= gate.reason;
      continue;
    }
    return { candidate, quote: reference.quote, id, rejectedReason: null, matched: true };
  }
  return { candidate: null, quote: null, id: null, rejectedReason, matched };
}

function defaultState() {
  return {
    version: STATE_VERSION,
    positions: {},
    closed: [],
    fills: [],
    seen: {},
    realized_pnl_native_atomic: '0',
    daily_realized_pnl_by_chain: {},
    entry_sellability_confirmations: {},
  };
}

function dayKey(now) {
  return nowIso(now).slice(0, 10);
}

function dailyLossOverride(env, chainName, now) {
  const chain = String(env.OKX_DAILY_LOSS_OVERRIDE_CHAIN || '').trim().toLowerCase();
  const date = String(env.OKX_DAILY_LOSS_OVERRIDE_DATE || '').trim();
  return {
    active: chain === chainName && date === dayKey(now),
    chain,
    date,
  };
}

function currentDailyPnl(state, chainName, now) {
  state.daily_realized_pnl_by_chain ||= {};
  const today = dayKey(now);
  let record = state.daily_realized_pnl_by_chain[chainName];
  if (!record || record.date !== today) {
    const legacy = record ? 0 : number(state.daily_realized_pnl_usd, 0);
    const legacyDate = String(state.daily_realized_pnl_date || '');
    record = { chain: chainName, date: today, pnl_usd: record ? 0 : legacy,
      status: !record && legacy !== 0 && legacyDate !== today ? 'unverified' : 'verified' };
    state.daily_realized_pnl_by_chain[chainName] = record;
  }
  state.daily_realized_pnl_usd = number(record.pnl_usd, 0);
  state.daily_realized_pnl_date = today;
  return record;
}

function exactEntryEvidence(candidate, evidence, now) {
  if (!evidence || quoteIdentity(evidence) !== quoteIdentity(candidate)) return { accepted: false, reason: 'exact_entry_identity_mismatch' };
  const quoteAge = ageSeconds(evidence.quote_at, now);
  const exactPrice = number(evidence.exact_buy_price_usd, NaN);
  const expectedToken = atomicBigInt(evidence.pretrade_expected_token_atomic, -1n);
  const expectedSell = atomicBigInt(evidence.pretrade_round_trip_native_atomic, -1n);
  const expectedNet = atomicBigInt(evidence.pretrade_round_trip_net_native_atomic, -1n);
  if (quoteAge === null || quoteAge < -5 || quoteAge > EXACT_ENTRY_QUOTE_MAX_AGE_SECONDS) {
    return { accepted: false, reason: 'exact_entry_quote_stale' };
  }
  if (!Number.isFinite(exactPrice) || exactPrice <= 0 || expectedToken <= 0n || expectedSell <= 0n || expectedNet <= 0n
      || !String(evidence.buy_route_id || '').trim() || !String(evidence.sell_route_id || '').trim()) {
    return { accepted: false, reason: 'exact_entry_quote_unavailable' };
  }
  return { accepted: true, exactPrice };
}

function exactEntryTradeabilityTimeoutMs(env) {
  const configured = Number(env?.OKX_ENTRY_TRADEABILITY_TIMEOUT_MS);
  if (Number.isFinite(configured) && configured >= 1_000 && configured <= 60_000) return configured;
  return EXACT_ENTRY_TRADEABILITY_TIMEOUT_MS;
}

async function withTimeout(promise, timeoutMs, code) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(code)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

function updateBscSellabilityConfirmation(state, candidate, evidence, now) {
  state.entry_sellability_confirmations ||= {};
  const key = quoteIdentity(candidate);
  const previous = state.entry_sellability_confirmations[key];
  const quoteTime = Date.parse(evidence.quote_at);
  const previousTime = Date.parse(previous?.last_quote_at || '');
  const sequential = previous && Number.isFinite(previousTime) && quoteTime > previousTime
    && (quoteTime - previousTime) / 1000 <= SELLABILITY_SEQUENCE_MAX_GAP_SECONDS;
  const record = {
    chain: String(candidate.chain).toLowerCase(),
    contract_address: address(candidate.contract_address, 'invalid_token'),
    pool_address: poolId(candidate.pool_address, 'invalid_pool'),
    consecutive: sequential ? previous.consecutive + 1 : previous && quoteTime === previousTime ? previous.consecutive : 1,
    last_quote_at: evidence.quote_at,
    last_confirmed_at: nowIso(now),
    buy_route_id: String(evidence.buy_route_id),
    sell_route_id: String(evidence.sell_route_id),
  };
  state.entry_sellability_confirmations[key] = record;
  return record;
}

function clearBscSellabilityConfirmation(state, candidate) {
  state.entry_sellability_confirmations ||= {};
  delete state.entry_sellability_confirmations[quoteIdentity(candidate)];
}

function atomicBigInt(value, fallback = 0n) {
  try { return BigInt(String(value ?? fallback)); } catch { return fallback; }
}

function nativeAtomicPnlUsd(amountAtomic, nativePriceUsd) {
  const price = number(nativePriceUsd, NaN);
  if (!Number.isFinite(price) || price <= 0) return null;
  return Number(amountAtomic) / 1e18 * price;
}

function publicCandidateRejections(input) {
  const result = [];
  for (const item of Array.isArray(input?.rejections) ? input.rejections : []) {
    if (!item || typeof item !== 'object') continue;
    const symbol = String(item.symbol || 'MEME').replace(/[\r\n\t]/g, ' ').trim().slice(0, 32);
    const rejectReason = String(item.reject_reason || '').replace(/[\r\n\t]/g, ' ').trim().slice(0, 240);
    if (!rejectReason) continue;
    result.push({ symbol, reject_reason: rejectReason, score: number(item.score) });
    if (result.length >= 5) break;
  }
  return result;
}

export function exitDecision(position, quote, now, executableQuote = null) {
  const entry = number(position.entry_price_usd);
  const price = number(quote?.price_usd ?? quote?.price);
  if (entry <= 0 || price <= 0) return { exit: false, reason: 'invalid_price' };
  const high = Math.max(entry, number(position.high_price_usd, entry), price);
  position.high_price_usd = high;
  const referenceRet = (price / entry - 1) * 100;
  position.last_reference_return_pct = referenceRet;
  let ret = referenceRet;
  let executable = false;
  if (executableQuote?.expected_native_atomic !== undefined) {
    const entrySpent = atomicBigInt(position.entry_native_spent_atomic || position.entry_native_atomic);
    const realizedCost = atomicBigInt(position.realized_cost_basis_native_atomic);
    const remainingCost = entrySpent - realizedCost;
    const expectedNative = atomicBigInt(
      executableQuote.estimated_net_native_atomic ?? executableQuote.expected_native_atomic,
    );
    if (remainingCost <= 0n) return { exit: false, reason: 'invalid_executable_value' };
    ret = expectedNative <= 0n
      ? -100
      : Number((expectedNative - remainingCost) * 1_000_000n / remainingCost) / 10_000;
    executable = true;
    position.high_executable_return_pct = Math.max(number(position.high_executable_return_pct, ret), ret);
  }
  position.last_executable_return_pct = executable ? ret : null;
  if (ret <= -STOP_LOSS_PCT) return { exit: true, reason: 'stop_loss', fraction: 1, return_pct: ret };
  const v2 = position.strategy_version === STRATEGY_VERSION;
  const chainName = String(position.chain || 'bsc').toLowerCase();
  if (v2 && !executable) return { exit: false, reason: 'executable_exit_quote_required', return_pct: null };
  if (!position.tp1_hit && ret >= TP1_RETURN_PCT) {
    return {
      exit: true,
      reason: 'take_profit_1',
      fraction: TP1_ORIGINAL_FRACTION,
      fraction_basis: 'original',
      target_original_fraction: 0.5,
      return_pct: ret,
    };
  }
  if (v2 && chainName === 'bsc') {
    if (ret >= TP2_RETURN_PCT) position.shadow_tp2_observed = true;
    if (ret >= TP3_RETURN_PCT) position.shadow_tp3_observed = true;
  }
  if ((!v2 || chainName === 'robinhood') && position.tp1_hit && !position.tp2_hit && ret >= TP2_RETURN_PCT) {
    return {
      exit: true,
      reason: 'take_profit_2',
      fraction: TP2_ORIGINAL_FRACTION,
      fraction_basis: 'original',
      target_original_fraction: 0.6,
      return_pct: ret,
    };
  }
  if ((!v2 || chainName === 'robinhood') && position.tp1_hit && position.tp2_hit && !position.tp3_hit && ret >= TP3_RETURN_PCT) {
    return {
      exit: true,
      reason: 'take_profit_3',
      fraction: TP3_ORIGINAL_FRACTION,
      fraction_basis: 'original',
      target_original_fraction: 0.7,
      return_pct: ret,
    };
  }
  const drawdown = executable
    ? ((1 + ret / 100) / (1 + number(position.high_executable_return_pct, ret) / 100) - 1) * 100
    : (price / high - 1) * 100;
  if (position.tp1_hit && drawdown <= -TRAIL_DRAWDOWN_PCT) {
    return { exit: true, reason: 'trailing_stop', fraction: 1, return_pct: ret };
  }
  const entryAt = Date.parse(position.entry_at || '');
  const ageMs = Number.isFinite(entryAt) ? now.getTime() - entryAt : null;
  if (!position.tp1_hit && ageMs !== null && ageMs >= INITIAL_TIME_STOP_MS) {
    return { exit: true, reason: 'time_stop', fraction: 1, return_pct: ret };
  }
  if (position.tp1_hit && ageMs !== null && ageMs >= RUNNER_TIME_STOP_MS) {
    return { exit: true, reason: 'runner_time_stop', fraction: 1, return_pct: ret };
  }
  return { exit: false, reason: 'hold', return_pct: ret };
}

export function sellAmount(position, decision) {
  const remaining = BigInt(uintString(position.remaining_atomic));
  if (decision?.target_original_fraction !== undefined) {
    const original = atomicBigInt(position.original_atomic, remaining);
    const confirmed = atomicBigInt(position.confirmed_sold_atomic, original - remaining);
    const target = original * BigInt(Math.round(number(decision.target_original_fraction) * 10_000)) / 10_000n;
    const requested = target > confirmed ? target - confirmed : 0n;
    const amount = requested > remaining ? remaining : requested;
    if (amount <= 0n) throw new Error('invalid_sell_amount');
    return amount.toString();
  }
  const fraction = typeof decision === 'object' ? decision?.fraction : decision;
  const numerator = Math.max(0, Math.min(10_000, Math.floor(number(fraction, 1) * 10_000)));
  const basis = decision?.fraction_basis === 'original'
    ? atomicBigInt(position.original_atomic, remaining)
    : remaining;
  const requested = basis * BigInt(numerator) / 10_000n;
  const amount = requested > remaining ? remaining : requested;
  if (amount <= 0n) throw new Error('invalid_sell_amount');
  return amount.toString();
}

function partialExitCostTooHigh(position, decision, executableQuote, env) {
  if (!String(decision?.reason || '').startsWith('take_profit_') || !executableQuote) return false;
  try {
    const gross = BigInt(uintString(executableQuote.expected_native_atomic));
    const gas = BigInt(uintString(executableQuote.estimated_gas_native_atomic));
    const configured = String(env.OKX_MIN_PARTIAL_EXIT_GAS_MULTIPLE || '10');
    if (!/^\d{1,3}$/.test(configured)) return true;
    const multiple = BigInt(configured);
    if (multiple <= 0n || multiple > 100n || gas <= 0n) return true;
    return gross < gas * multiple;
  } catch {
    return true;
  }
}

function normalizeTakeProfitStages(position) {
  const remaining = atomicBigInt(position.remaining_atomic);
  const original = atomicBigInt(position.original_atomic, remaining);
  const confirmed = atomicBigInt(position.confirmed_sold_atomic, original - remaining);
  position.confirmed_sold_atomic = confirmed.toString();
  position.tp1_hit = position.tp1_hit === true || (original > 0n && confirmed * 10n >= original * 5n);
  if (position.strategy_version === STRATEGY_VERSION && position.chain === 'bsc') {
    position.tp2_hit = false;
    position.tp3_hit = false;
  } else {
    position.tp2_hit = position.tp2_hit === true || (original > 0n && confirmed * 10n >= original * 6n);
    position.tp3_hit = position.tp3_hit === true || (original > 0n && confirmed * 10n >= original * 7n);
  }
}

export function emergencyExitDecision(position, quote, executableQuote) {
  const previousLiquidity = number(position?.last_usable_liquidity_usd, NaN);
  const liquidity = number(quote?.liquidity_usd ?? quote?.liquidity, NaN);
  if (Number.isFinite(previousLiquidity) && previousLiquidity > 0 && Number.isFinite(liquidity)
      && liquidity <= previousLiquidity * (1 - EMERGENCY_LIQUIDITY_DROP_PCT / 100)) {
    return { exit: true, reason: 'emergency_liquidity_drop', fraction: 1 };
  }
  const impact = number(executableQuote?.price_impact_percent, NaN);
  if (Number.isFinite(impact) && impact > EMERGENCY_SELL_IMPACT_PCT) {
    return { exit: true, reason: 'emergency_sell_impact', fraction: 1 };
  }
  const net = atomicBigInt(executableQuote?.estimated_net_native_atomic, -1n);
  const reference = atomicBigInt(executableQuote?.reference_native_atomic, -1n);
  if (net >= 0n && reference > 0n && net * 100n <= reference * BigInt(100 - EMERGENCY_ROUTE_DIVERGENCE_PCT)) {
    return { exit: true, reason: 'emergency_route_divergence', fraction: 1 };
  }
  const priorRoute = String(position?.last_primary_route_id || '');
  const route = String(executableQuote?.route_id || '');
  if (priorRoute && !route) return { exit: true, reason: 'emergency_primary_route_missing', fraction: 1 };
  return { exit: false, reason: 'no_emergency' };
}

function referenceNativeValue(position, quote) {
  const entryPrice = number(position?.entry_price_usd, NaN);
  const currentPrice = number(quote?.price_usd ?? quote?.price, NaN);
  const entrySpent = atomicBigInt(position?.entry_native_spent_atomic || position?.entry_native_atomic);
  const remainingCost = entrySpent - atomicBigInt(position?.realized_cost_basis_native_atomic);
  if (!Number.isFinite(entryPrice) || entryPrice <= 0 || !Number.isFinite(currentPrice)
      || currentPrice < 0 || remainingCost <= 0n) return null;
  const scaledRatio = BigInt(Math.max(0, Math.floor(currentPrice / entryPrice * 1_000_000)));
  return (remainingCost * scaledRatio / 1_000_000n).toString();
}

function partialExitValidation(position, quote, decision, exactQuote, amount) {
  if (!executableSellQuote(exactQuote)) return { accepted: false, reason: 'exact_partial_exit_quote_unavailable' };
  const remaining = atomicBigInt(position.remaining_atomic);
  const sold = atomicBigInt(amount);
  const entrySpent = atomicBigInt(position.entry_native_spent_atomic || position.entry_native_atomic);
  const remainingCost = entrySpent - atomicBigInt(position.realized_cost_basis_native_atomic);
  if (remaining <= 0n || sold <= 0n || sold > remaining || remainingCost <= 0n) {
    return { accepted: false, reason: 'exact_partial_exit_quote_unavailable' };
  }
  const cost = remainingCost * sold / remaining;
  const net = atomicBigInt(exactQuote.estimated_net_native_atomic ?? exactQuote.expected_native_atomic, -1n);
  if (cost <= 0n || net < 0n) return { accepted: false, reason: 'exact_partial_exit_quote_unavailable' };
  const returnPct = Number((net - cost) * 1_000_000n / cost) / 10_000;
  const trigger = decision.reason === 'take_profit_1' ? TP1_RETURN_PCT
    : decision.reason === 'take_profit_2' ? TP2_RETURN_PCT
    : decision.reason === 'take_profit_3' ? TP3_RETURN_PCT : null;
  if (trigger !== null && returnPct < trigger) return { accepted: false, reason: 'partial_exit_trigger_not_met' };
  if (number(exactQuote.price_impact_percent, Infinity) > EMERGENCY_SELL_IMPACT_PCT) {
    return { accepted: false, reason: 'partial_exit_impact_exceeded' };
  }
  const quotedReference = exactQuote.reference_native_atomic === undefined
    ? null : atomicBigInt(exactQuote.reference_native_atomic, -1n);
  const fullReference = atomicBigInt(referenceNativeValue(position, quote), -1n);
  const partialReference = quotedReference !== null ? quotedReference
    : fullReference > 0n ? fullReference * sold / remaining : -1n;
  if (partialReference <= 0n || net * 100n <= partialReference * BigInt(100 - EMERGENCY_ROUTE_DIVERGENCE_PCT)) {
    return { accepted: false, reason: 'partial_exit_route_divergence' };
  }
  return { accepted: true, returnPct };
}

function reconcileExternalReduction(state, position, actualBalance, now) {
  const recorded = BigInt(uintString(position.remaining_atomic));
  if (actualBalance >= recorded) return false;
  const reduced = recorded - actualBalance;
  const entrySpent = atomicBigInt(position.entry_native_spent_atomic || position.entry_native_atomic);
  const realizedCostBefore = atomicBigInt(position.realized_cost_basis_native_atomic);
  const unreconciledCost = entrySpent > realizedCostBefore ? entrySpent - realizedCostBefore : 0n;
  const costBasis = recorded > 0n ? unreconciledCost * reduced / recorded : 0n;
  position.remaining_atomic = actualBalance.toString();
  position.realized_cost_basis_native_atomic = (realizedCostBefore + costBasis).toString();
  normalizeTakeProfitStages(position);
  position.last_external_reconciliation_at = nowIso(now);
  position.external_reduction_atomic = (
    atomicBigInt(position.external_reduction_atomic) + reduced
  ).toString();
  position.accounting_status = 'external_proceeds_unreconciled';
  delete position.retry_exit;
  state.external_reconciliations ||= [];
  state.external_reconciliations.push({
    chain: position.chain,
    symbol: String(position.symbol || 'MEME').slice(0, 32),
    token: address(position.contract_address || position.token, 'invalid_token'),
    time: nowIso(now),
    recorded_before_atomic: recorded.toString(),
    actual_after_atomic: actualBalance.toString(),
    reduced_atomic: reduced.toString(),
    cost_basis_native_atomic: costBasis.toString(),
    reason: 'external_wallet_reduction',
    proceeds_status: 'unreconciled',
  });
  return true;
}

function writeStatus(statusPath, payload) {
  atomicWrite(statusPath, { updated_at: nowIso(new Date()), provider: 'okx-dex-sdk', ...payload });
}

function terminalFor(options, env, chain) {
  const configuredUsd = String(options.amountUsd || env.OKX_ORDER_NOTIONAL_USD || '').trim();
  const configuredAtomic = String(options.amountNativeAtomic || env.OKX_TRADE_NATIVE_ATOMIC || '').trim();
  const fixedUsd = !configuredUsd && !configuredAtomic && !legacyStrategyInputAllowed(env)
    ? String(chainEntryPolicy(chain.name).amountUsd)
    : configuredUsd || null;
  return new TerminalControlProtocol({ chain: chain.name,
    wallet: env.EVM_WALLET_ADDRESS || env.BSC_WALLET_ADDRESS,
    amountUsd: fixedUsd,
    amountNativeAtomic: fixedUsd ? null : configuredAtomic || DEFAULT_AMOUNT_NATIVE_ATOMIC,
    exitOnly: options.exitOnly === true });
}

function effectiveStrategyFields(chain) {
  const policy = chainEntryPolicy(chain.name);
  return { strategy_version: STRATEGY_VERSION, signal_stage: policy.signalStage, entry_route: policy.entryRoute };
}

function fixedAmountMatches(terminal, chain) {
  const receipt = terminal.receipt();
  return receipt.amount_native_atomic === null
    && number(receipt.amount_usd, NaN) === chainEntryPolicy(chain.name).amountUsd;
}

function attachEffectiveStrategy(result, terminal, chain, { effective = true } = {}) {
  const fields = effective ? effectiveStrategyFields(chain) : {};
  Object.assign(result, fields);
  result.terminal_control = { ...terminal.receipt(), ...fields };
  return result;
}

export async function runLiveDaemonOnce(options = {}) {
  const env = options.env || process.env;
  const chain = executionChain(env);
  const live = options.live === true;
  const paths = { ...defaultPaths(chain.name, live), ...definedOptions(options) };
  const now = options.now instanceof Date ? options.now : new Date(options.now || Date.now());
  const deps = options.deps || {};
  const terminal = options.terminalControl || terminalFor(options, env, chain);
  const strictV2 = !legacyStrategyInputAllowed(env);
  const amountMatches = !strictV2 || fixedAmountMatches(terminal, chain);
  const lossOverride = dailyLossOverride(env, chain.name, now);
  const publish = result => {
    if (options.daemonRunning === true) result.daemon_running = true;
    if (lossOverride.active) {
      result.daily_loss_override_active = true;
      result.daily_loss_override_chain = lossOverride.chain;
      result.daily_loss_override_date = lossOverride.date;
    }
    attachEffectiveStrategy(result, terminal, chain, { effective: strictV2 && amountMatches });
    writeStatus(paths.statusPath, result);
  };
  try {
    if (!amountMatches) {
      const result = { status: 'blocked', reason: 'chain_v2_order_size_conflict', live_started: false };
      publish(result);
      return result;
    }
    const check = await (deps.preflight || preflight)(env);
    if (!check?.ready) {
      const result = { status: 'blocked', reason: check?.reason || 'sdk_preflight_failed', live_started: false };
      publish(result);
      return result;
    }

    const input = readJson(paths.inputPath, { signals: [], quotes: [] });
    const state = readJson(paths.statePath, defaultState());
    const wallet = address(check.wallet || env.EVM_WALLET_ADDRESS || env.BSC_WALLET_ADDRESS);
    if (state.wallet && address(state.wallet) !== wallet) throw new Error('ledger_wallet_mismatch');
    if (state.chain && state.chain !== chain.name) throw new Error('ledger_chain_mismatch');
    state.version = STATE_VERSION;
    state.positions ||= {};
    state.closed ||= [];
    state.fills ||= [];
    state.seen ||= {};
    if (!state.wallet && (Object.keys(state.positions).length || state.fills.length || state.closed.length)) {
      throw new Error('ledger_wallet_unverified');
    }
    state.wallet = wallet;
    state.chain = chain.name;
    state.execution_intents ??= {};
    if (!state.execution_intents || typeof state.execution_intents !== 'object' || Array.isArray(state.execution_intents)) {
      throw new Error('invalid_pending_ledger');
    }
    let pendingCount = state.terminal_pending ?? 0;
    if (!Number.isSafeInteger(pendingCount) || pendingCount < 0) throw new Error('invalid_pending_ledger');
    let unresolved = () => Object.values(state.execution_intents).filter(unresolvedIntent);
    if (live) {
      let recoveryChanged = false;
      const pendingEvidence = deps.pendingIntentEvidence || fetchPendingIntentEvidence;
      if (typeof pendingEvidence === 'function') {
        for (const [key, intent] of Object.entries(state.execution_intents)) {
          if (intent?.state !== 'pending') continue;
          let classification = { classification: 'unknown' };
          try {
            const evidence = await pendingEvidence({ env, chain: chain.name, wallet, key, intent,
              deps: deps.pendingEvidenceDeps || {} });
            classification = classifyPendingIntentEvidence(intent, evidence);
          } catch {
            // Missing or unreadable evidence remains pending and fail-closed.
          }
          if (classification.classification === 'unknown') continue;
          intent.recovery = { ...classification, classified_at: nowIso(now) };
          if (classification.classification === 'reverted') {
            if (pendingCount <= 0) throw new Error('invalid_pending_ledger');
            intent.state = 'reverted';
            pendingCount -= 1;
          } else {
            intent.state = 'confirmed_unaccounted';
          }
          recoveryChanged = true;
        }
      }
      if (recoveryChanged) {
        state.terminal_pending = pendingCount;
        atomicWrite(paths.statePath, state);
        unresolved = () => Object.values(state.execution_intents).filter(unresolvedIntent);
      }
      const retryable = Object.entries(state.execution_intents)
        .filter(([, intent]) => unresolvedIntent(intent) && retryableBeforeSwapBroadcast(intent));
      if (retryable.length) {
        if (pendingCount < retryable.length) throw new Error('invalid_pending_ledger');
        for (const [key, intent] of retryable) {
          preserveRetryableExit(state, key, intent, now);
          intent.state = 'not_submitted';
          intent.retryable_at = nowIso(now);
        }
        pendingCount -= retryable.length;
        state.terminal_pending = pendingCount;
        atomicWrite(paths.statePath, state);
        unresolved = () => Object.values(state.execution_intents).filter(unresolvedIntent);
      }
    }
    const legacyPending = pendingCount > unresolved().length;
    if (pendingCount < unresolved().length) throw new Error('invalid_pending_ledger');
    const nonceUncertain = legacyPending || unresolved().some(intent =>
      !Array.isArray(intent.transactions) || !intent.transactions.length ||
      intent.transactions.some(tx => tx.stage !== 'confirmed'));
    const saveState = () => { if (live) atomicWrite(paths.statePath, state); };
    state.realized_pnl_native_atomic = atomicBigInt(state.realized_pnl_native_atomic).toString();
    const dailyPnl = currentDailyPnl(state, chain.name, now);
    // Commit the intent before signing; clear it only in the same write as its fill.
    const trackedExecution = async (execute, params, key, side) => {
      let intent;
      if (live) {
        const pending = state.terminal_pending ?? 0;
        if (pending >= Number.MAX_SAFE_INTEGER) throw new Error('invalid_pending_ledger');
        if (legacyPending || unresolvedIntent(state.execution_intents[key])) {
          throw new Error('execution_reconciliation_required');
        }
        const snapshotSource = params.candidate || params.position || {};
        intent = { state: 'pending', side, wallet, chain: chain.name, token: key,
          contract_address: address(snapshotSource.contract_address || snapshotSource.token, 'invalid_token'),
          ...(snapshotSource.pool_address || snapshotSource.pool ? {
            pool_address: poolId(snapshotSource.pool_address || snapshotSource.pool, 'invalid_pool'),
          } : {}),
          created_at: nowIso(now), amount_atomic: params.tokenAmountAtomic || params.amountNativeAtomic,
          ...(params.exitReason ? { exit_reason: params.exitReason } : {}),
          candidate_snapshot: redact({
            chain: chain.name,
            symbol: snapshotSource.symbol,
            contract_address: snapshotSource.contract_address || snapshotSource.token,
            pool_address: snapshotSource.pool_address || snapshotSource.pool,
            strategy_version: snapshotSource.strategy_version,
            signal_stage: snapshotSource.signal_stage,
            entry_route: snapshotSource.entry_route,
            entry_price_usd: params.entryPriceUsd ?? snapshotSource.entry_price_usd,
            entry_notional_usd: params.entryNotionalUsd ?? snapshotSource.entry_notional_usd,
            entry_native_price_usd: params.entryNativePriceUsd ?? snapshotSource.entry_native_price_usd,
          }),
          transactions: [] };
        state.execution_intents[key] = intent;
        state.terminal_pending = pending + 1;
        saveState();
      }
      const onExecutionProgress = async progress => {
        if (!live) return;
        if (!['buy', 'sell', 'approval'].includes(progress?.side) ||
            !['prepared', 'broadcast', 'confirmed'].includes(progress?.stage)) {
          throw new Error('invalid_execution_progress');
        }
        const txHash = progress.tx_hash;
        if (txHash !== undefined && !/^0x[0-9a-fA-F]{64}$/.test(txHash)) throw new Error('invalid_execution_hash');
        const existing = intent.transactions.find(item => item.side === progress.side &&
          (item.tx_hash === txHash?.toLowerCase() || (!item.tx_hash && progress.stage !== 'prepared')));
        const transaction = { side: progress.side, stage: progress.stage,
          ...(txHash ? { tx_hash: txHash.toLowerCase() } : {}), updated_at: nowIso(new Date()) };
        if (existing) Object.assign(existing, transaction);
        else intent.transactions.push(transaction);
        if (progress.wallet_balances_before
            && /^\d+$/.test(String(progress.wallet_balances_before.token_atomic || ''))
            && /^\d+$/.test(String(progress.wallet_balances_before.native_atomic || ''))) {
          intent.wallet_balances_before = {
            token_atomic: String(progress.wallet_balances_before.token_atomic),
            native_atomic: String(progress.wallet_balances_before.native_atomic),
          };
        }
        saveState();
      };
      try {
        const result = await execute({ ...params, onExecutionProgress });
        if (live) {
          const txHash = result?.buy_tx || result?.sell_tx;
          if (/^0x[0-9a-fA-F]{64}$/.test(txHash || '') && !intent.transactions.some(item => item.tx_hash === txHash.toLowerCase())) {
            await onExecutionProgress({ side, stage: 'broadcast', tx_hash: txHash });
          }
          intent.result_state = publicError(result?.state);
          intent.reason = publicError(result?.reason);
          if (result?.state === 'blocked' && retryableBeforeSwapBroadcast(intent)) {
            preserveRetryableExit(state, key, intent, now);
            intent.state = 'not_submitted';
            state.terminal_pending -= 1;
          }
          saveState();
        }
        return result;
      } catch (error) {
        if (live) {
          intent.reason = publicError(error.message);
          Object.assign(intent, publicDiagnostic(error));
          if (retryableBeforeSwapBroadcast(intent)) {
            preserveRetryableExit(state, key, intent, now);
            intent.state = 'not_submitted';
            state.terminal_pending -= 1;
          }
          saveState();
        }
        throw error;
      }
    };
    const settle = key => {
      state.execution_intents[key].state = 'settled';
      state.execution_intents[key].settled_at = nowIso(now);
      state.terminal_pending -= 1;
    };
    const quotes = quoteMap(input);
    const missingPositionQuotes = [];
    const missingExecutableQuotes = [];
    const missingBalanceChecks = [];
    const holdingPositions = [];
    let deferredHoldingReason = null;

    for (const [id, position] of Object.entries(state.positions)) {
      normalizeTakeProfitStages(position);
      const intentKey = positionId(position, chain.name);
      if (live && String(env.OKX_POSITION_BALANCE_RECONCILE || '') === '1') {
        try {
          const actualBalance = BigInt(await (deps.readWalletTokenBalance || readWalletTokenBalance)({
            env,
            token: position.contract_address || position.token,
            walletAddress: wallet,
            deps: deps.balanceDeps || {},
          }));
          if (reconcileExternalReduction(state, position, actualBalance, now)) saveState();
          if (actualBalance === 0n) {
            position.exit_status = 'external_wallet_reduction';
            delete state.positions[id];
            state.closed.push({
              ...position,
              closed_at: nowIso(now),
              exit_reason: 'external_wallet_reduction',
              return_pct: null,
            });
            saveState();
            continue;
          }
        } catch {
          position.exit_status = 'position_balance_unavailable';
          missingBalanceChecks.push({ id, symbol: String(position.symbol || 'MEME').slice(0, 32) });
          continue;
        }
      }
      let quote = quotes.get(quoteIdentity(position));
      const positionQuoteAge = ageSeconds(quote?.quote_at || quote?.quote_observed_at, now);
      const positionQuoteFresh = quote
        && positionQuoteAge !== null
        && positionQuoteAge >= 0
        && positionQuoteAge <= 20
        && String(quote?.quote_status || '').toLowerCase() === 'fresh';
      if (!positionQuoteFresh) {
        quote = await (deps.fetchPositionQuote || fetchPositionQuote)(position, chain.name, now, { fetch: deps.fetch });
      }
      if (!quote) {
        missingPositionQuotes.push({ id, symbol: String(position.symbol || 'MEME').slice(0, 32) });
        continue;
      }
      let executableQuote = null;
      let primaryExecutableQuote = null;
      let primaryQuoteUnavailable = false;
      const v2Position = position.strategy_version === STRATEGY_VERSION;
      if (v2Position || String(env.OKX_EXECUTABLE_EXIT_ENABLED || '') === '1') {
        try {
          executableQuote = await (deps.quoteSellValue || quoteSellValue)({
            env,
            position,
            tokenAmountAtomic: position.remaining_atomic,
            deps: deps.sellQuoteDeps || {},
          });
          primaryExecutableQuote = executableQuote;
          position.last_executable_value_native_atomic = String(executableQuote.expected_native_atomic);
          position.last_executable_net_value_native_atomic = String(
            executableQuote.estimated_net_native_atomic ?? executableQuote.expected_native_atomic,
          );
          position.last_executable_quote_at = executableQuote.quote_at || nowIso(now);
          position.last_sell_price_impact_percent = number(executableQuote.price_impact_percent);
          if (executableQuote.reference_native_atomic === undefined) {
            const referenceValue = referenceNativeValue(position, quote);
            if (referenceValue !== null) executableQuote.reference_native_atomic = referenceValue;
          }
        } catch {
          position.exit_status = 'executable_exit_quote_unavailable';
          missingExecutableQuotes.push({ id, symbol: String(position.symbol || 'MEME').slice(0, 32) });
          primaryQuoteUnavailable = true;
          if (!v2Position) continue;
        }
      }
      let decision = exitDecision(position, quote, now, executableQuote);
      const detectedEmergency = v2Position
        ? (primaryQuoteUnavailable
          ? { exit: true, reason: 'emergency_primary_route_missing', fraction: 1 }
          : emergencyExitDecision(position, quote, executableQuote))
        : { exit: false };
      const emergency = v2Position && position.emergency_exit?.reason
        ? { exit: true, reason: position.emergency_exit.reason, fraction: 1 }
        : detectedEmergency;
      if (emergency.exit) {
        position.emergency_exit ||= {
          reason: emergency.reason,
          triggered_at: nowIso(now),
          prior_route_id: position.last_primary_route_id || null,
          liquidity_baseline_usd: number(position.last_usable_liquidity_usd, null),
        };
        position.emergency_exit.last_attempt_at = nowIso(now);
        position.emergency_exit.attempts = number(position.emergency_exit.attempts, 0) + 1;
        decision = { ...decision, ...emergency, return_pct: decision.return_pct };
        saveState();
        const alternateDexIds = String(env.OKX_ALTERNATE_DEX_IDS || '').trim();
        const maySubmit = !nonceUncertain && !unresolvedIntent(state.execution_intents[intentKey]);
        let selectedQuote = executableSellQuote(primaryExecutableQuote) ? primaryExecutableQuote : null;
        if (maySubmit && alternateDexIds) {
          try {
            const alternateQuote = await (deps.quoteSellValue || quoteSellValue)({
              env,
              position,
              tokenAmountAtomic: position.remaining_atomic,
              routePreference: 'alternate',
              deps: deps.sellQuoteDeps || {},
            });
            position.emergency_exit.alternate_route_id = alternateQuote.route_id || null;
            position.emergency_exit.alternate_quote_at = alternateQuote.quote_at || nowIso(now);
            const primaryRoute = String(primaryExecutableQuote?.route_id || position.emergency_exit.prior_route_id || '').trim();
            if (String(alternateQuote.route_id || '').trim() === primaryRoute) {
              position.emergency_exit.alternate_route_conflict = true;
            } else if (alternateMateriallyBetter(primaryExecutableQuote, alternateQuote)) selectedQuote = alternateQuote;
            saveState();
          } catch {
            // A failed optional alternate does not invalidate an executable primary quote.
          }
        }
        if (maySubmit && !selectedQuote) {
          decision = { ...decision, exit: false, reason: 'emergency_alternate_route_unavailable' };
          deferredHoldingReason = 'emergency_alternate_route_unavailable';
        } else if (selectedQuote) {
          executableQuote = selectedQuote;
          position.emergency_exit.selected_route_id = selectedQuote.route_id || null;
          position.emergency_exit.selected_route = selectedQuote === primaryExecutableQuote ? 'primary' : 'alternate';
          const missingIndex = missingExecutableQuotes.findIndex(item => item.id === id);
          if (missingIndex >= 0) missingExecutableQuotes.splice(missingIndex, 1);
          saveState();
        }
      }
      const currentLiquidity = number(quote?.liquidity_usd ?? quote?.liquidity, NaN);
      if (!position.emergency_exit && Number.isFinite(currentLiquidity) && currentLiquidity > 0) {
        position.last_usable_liquidity_usd = currentLiquidity;
      }
      if (primaryExecutableQuote?.route_id) position.last_primary_route_id = String(primaryExecutableQuote.route_id);
      let retryAmount = null;
      if (position.retry_exit) {
        try {
          const amount = BigInt(position.retry_exit.amount_atomic);
          const remaining = BigInt(position.remaining_atomic);
          if (amount > 0n && amount <= remaining) {
            retryAmount = amount.toString();
            decision = { ...decision, exit: true, reason: position.retry_exit.reason || 'retry_pending_exit' };
          } else {
            delete position.retry_exit;
          }
        } catch {
          delete position.retry_exit;
        }
      }
      let amount = null;
      let executionQuote = executableQuote;
      if (decision.exit) amount = retryAmount ?? sellAmount(position, decision);
      if (decision.exit && executableQuote && amount !== String(position.remaining_atomic)) {
        try {
          executionQuote = await (deps.quoteSellValue || quoteSellValue)({
            env,
            position,
            tokenAmountAtomic: amount,
            deps: deps.sellQuoteDeps || {},
          });
          if (v2Position && !retryAmount) {
            const validation = partialExitValidation(position, quote, decision, executionQuote, amount);
            if (!validation.accepted) {
              decision = { ...decision, exit: false, reason: validation.reason };
              deferredHoldingReason = validation.reason;
            } else {
              decision = { ...decision, return_pct: validation.returnPct };
            }
          }
        } catch {
          decision = { ...decision, exit: false, reason: 'exact_partial_exit_quote_unavailable' };
          deferredHoldingReason = 'exact_partial_exit_quote_unavailable';
        }
      }
      if (!retryAmount && decision.exit && partialExitCostTooHigh(position, decision, executionQuote, env)) {
        decision = { ...decision, exit: false, reason: 'partial_exit_cost_too_high' };
        deferredHoldingReason = 'partial_exit_cost_too_high';
      }
      const entryPrice = number(position.entry_price_usd);
      const currentPrice = number(quote?.price_usd ?? quote?.price);
      position.last_price_usd = currentPrice;
      position.last_quote_at = nowIso(now);
      position.last_return_pct = decision.return_pct;
      position.exit_status = decision.reason;
      holdingPositions.push({
        symbol: String(position.symbol || 'MEME').slice(0, 32),
        return_pct: decision.return_pct,
        high_return_pct: entryPrice > 0 ? (number(position.high_price_usd, entryPrice) / entryPrice - 1) * 100 : null,
        age_minutes: Math.max(0, number((now.getTime() - Date.parse(position.entry_at || '')) / 60_000)),
        tp1_hit: position.tp1_hit === true,
        tp2_hit: position.tp2_hit === true,
        tp3_hit: position.tp3_hit === true,
      });
      if (nonceUncertain || unresolvedIntent(state.execution_intents[intentKey])) {
        position.exit_status = 'execution_reconciliation_required';
        continue;
      }
      if (!decision.exit) continue;
      if (!amount) amount = retryAmount ?? sellAmount(position, decision);
      const sell = await trackedExecution(deps.executeSell || executeSell, {
        env,
        position,
        tokenAmountAtomic: amount,
        preparedSwap: executionQuote?.prepared_swap || null,
        exitReason: decision.reason,
        live,
        deps: deps.sellDeps || {},
      }, intentKey, 'sell');
      if (!live) {
        const result = { status: sell?.state || 'dry_run_ready', live_started: false, exit_reason: decision.reason };
        publish(result);
        return result;
      }
      if (sell?.state !== 'sell_confirmed') {
        const result = { status: 'sell_blocked', reason: sell?.reason || sell?.state || 'sell_failed', live_started: false, exit_reason: decision.reason };
        saveState();
        publish(result);
        return result;
      }
      const priorRemaining = BigInt(position.remaining_atomic);
      const reportedSold = BigInt(uintString(sell?.sold_atomic, 'sell_fill_not_detected'));
      if (!/^0x[0-9a-fA-F]{64}$/.test(sell?.sell_tx || '')) throw new Error('sell_fill_not_detected');
      const confirmedSold = reportedSold > priorRemaining ? priorRemaining : reportedSold;
      const remaining = priorRemaining - confirmedSold;
      position.remaining_atomic = remaining.toString();
      position.confirmed_sold_atomic = (atomicBigInt(position.confirmed_sold_atomic) + confirmedSold).toString();
      normalizeTakeProfitStages(position);
      delete position.retry_exit;
      position.last_sell_tx = sell?.sell_tx || null;
      position.last_exit_reason = decision.reason;
      const entrySpent = atomicBigInt(position.entry_native_spent_atomic || position.entry_native_atomic);
      const realizedCostBefore = atomicBigInt(position.realized_cost_basis_native_atomic);
      const originalAmount = atomicBigInt(position.original_atomic, confirmedSold);
      const amountAtomic = confirmedSold;
      const costBasis = remaining <= 0n
        ? entrySpent - realizedCostBefore
        : entrySpent * amountAtomic / originalAmount;
      position.realized_cost_basis_native_atomic = (realizedCostBefore + costBasis).toString();
      const netNativeReceived = sell?.net_native_received_atomic === undefined
        ? null
        : atomicBigInt(sell.net_native_received_atomic);
      let fillPnl = null;
      if (netNativeReceived !== null) {
        fillPnl = netNativeReceived - costBasis;
        position.realized_pnl_native_atomic = (atomicBigInt(position.realized_pnl_native_atomic) + fillPnl).toString();
        state.realized_pnl_native_atomic = (atomicBigInt(state.realized_pnl_native_atomic) + fillPnl).toString();
        let nativePriceUsd = number(sell?.native_price_usd ?? position.entry_native_price_usd, NaN);
        if (typeof deps.fetchNativeUsdPrice === 'function') {
          try { nativePriceUsd = await deps.fetchNativeUsdPrice(chain.name, { fetch: deps.fetch }); } catch { /* fail closed below */ }
        }
        const pnlUsd = nativeAtomicPnlUsd(fillPnl, nativePriceUsd);
        if (pnlUsd === null) dailyPnl.status = 'unverified';
        else dailyPnl.pnl_usd = number(dailyPnl.pnl_usd, 0) + pnlUsd;
        state.daily_realized_pnl_usd = number(dailyPnl.pnl_usd, 0);
      }
      state.fills.push({
        chain: chain.name,
        symbol: String(position.symbol || 'MEME').slice(0, 32),
        token: address(position.contract_address || position.token, 'invalid_token'),
        side: 'sell',
        tx_hash: sell?.sell_tx || null,
        time: nowIso(now),
        exit_reason: decision.reason,
        fraction: decision.fraction,
        amount_atomic: confirmedSold.toString(),
        return_pct: decision.return_pct,
        ...(sell?.native_received_atomic !== undefined ? { native_received_atomic: String(sell.native_received_atomic) } : {}),
        ...(netNativeReceived !== null ? { net_native_received_atomic: netNativeReceived.toString() } : {}),
        ...(sell?.gas_native_atomic !== undefined ? { gas_native_atomic: String(sell.gas_native_atomic) } : {}),
        cost_basis_native_atomic: costBasis.toString(),
        ...(fillPnl !== null ? { pnl_native_atomic: fillPnl.toString() } : {}),
      });
      if (remaining <= 0n) {
        delete state.positions[id];
        state.closed.push({
          ...position,
          closed_at: nowIso(now),
          exit_reason: decision.reason,
          sell_tx: sell?.sell_tx || null,
          return_pct: decision.return_pct,
        });
      }
      settle(intentKey);
      saveState();
      const result = { status: remaining <= 0n ? 'position_closed' : 'position_reduced', live_started: live, exit_reason: decision.reason, sell_tx: sell?.sell_tx || null };
      publish(result);
      return result;
    }

    if (legacyPending || unresolved().length) {
      const result = { status: 'risk_paused', reason: 'execution_reconciliation_required',
        live_started: false, pending_count: state.terminal_pending,
        holding_positions: holdingPositions };
      saveState();
      publish(result);
      return result;
    }

    if (missingPositionQuotes.length) {
      const result = {
        status: 'risk_paused',
        reason: 'position_quote_unavailable',
        live_started: false,
        symbols: missingPositionQuotes.map((item) => item.symbol),
      };
      saveState();
      publish(result);
      return result;
    }

    if (missingExecutableQuotes.length) {
      const result = {
        status: 'risk_paused',
        reason: 'executable_exit_quote_unavailable',
        live_started: false,
        symbols: missingExecutableQuotes.map((item) => item.symbol),
      };
      saveState();
      publish(result);
      return result;
    }

    if (missingBalanceChecks.length) {
      const result = {
        status: 'risk_paused',
        reason: 'position_balance_unavailable',
        live_started: false,
        symbols: missingBalanceChecks.map((item) => item.symbol),
      };
      saveState();
      publish(result);
      return result;
    }

    if (options.exitOnly === true) {
      const result = {
        status: 'exit_only',
        reason: 'new_entries_disabled',
        live_started: live,
        holding_positions: holdingPositions,
      };
      saveState();
      publish(result);
      return result;
    }

    if (strictV2 && input?.strategy_version !== STRATEGY_VERSION) {
      const result = { status: 'risk_paused', reason: 'chain_v2_input_required', live_started: false };
      saveState();
      publish(result);
      return result;
    }

    for (const candidate of Array.isArray(input?.signals) ? input.signals : []) {
      if (String(candidate?.chain || '').toLowerCase() !== chain.name) continue;
      const quote = candidateQuote(candidate, quotes);
      const quoteAge = ageSeconds(quote?.quote_at || quote?.quote_observed_at, now);
      if (quoteAge !== null && quoteAge >= 0 && quoteAge <= 20
          && String(quote?.quote_status || '').toLowerCase() === 'fresh') continue;
      const refreshed = await (deps.fetchPositionQuote || fetchPositionQuote)(candidate, chain.name, now, { fetch: deps.fetch });
      if (!refreshed || quoteIdentity(refreshed) !== quoteIdentity(candidate)) continue;
      quotes.set(quoteIdentity(candidate), refreshed);
    }

    let selection = eligibleCandidate(input, state, quotes, chain.name, now, strictV2);
    let selectionSource = 'signals';
    if (!selection.candidate) {
      const preflightSelection = eligiblePreflightCandidate(input, state, chain.name, now);
      if (preflightSelection.candidate || (!selection.matched && preflightSelection.matched)) {
        selection = preflightSelection;
        selectionSource = 'preflight_signals';
      }
    }
    if (!selection.candidate) {
      const candidateRejections = publicCandidateRejections(input);
      const result = Object.keys(state.positions).length
        ? { status: 'holding_existing_position', reason: deferredHoldingReason || 'exit_conditions_not_met', live_started: live, holding_positions: holdingPositions, candidate_rejections: candidateRejections }
        : selection.matched
        ? { status: 'risk_paused', reason: selection.rejectedReason || 'strategy_candidate_unavailable', live_started: false, candidate_rejections: candidateRejections }
        : { status: 'waiting_for_strategy_candidate', reason: 'strategy_candidate_unavailable', live_started: false, candidate_rejections: candidateRejections };
      saveState();
      publish(result);
      return result;
    }
    const { candidate, quote, id } = selection;
    const v2Policy = candidate.strategy_version === STRATEGY_VERSION ? chainEntryPolicy(chain.name) : null;
    const configuredMaxPositions = number(env.OKX_MAX_OPEN_POSITIONS, DEFAULT_MAX_OPEN_POSITIONS);
    const maxOpenPositions = v2Policy ? Math.min(v2Policy.maxOpenPositions, configuredMaxPositions) : configuredMaxPositions;
    if (Object.keys(state.positions).length >= maxOpenPositions) {
      const result = { status: 'risk_paused', reason: 'max_open_positions', live_started: false };
      publish(result);
      return result;
    }
    if (v2Policy) {
      const openPositions = Object.values(state.positions);
      if (openPositions.some(position => !Number.isFinite(Number(position.entry_notional_usd))
          || Number(position.entry_notional_usd) <= 0)) {
        const result = { status: 'risk_paused', reason: 'open_notional_unverified', live_started: false };
        publish(result);
        return result;
      }
      const openNotionalUsd = openPositions
        .reduce((total, position) => total + number(position.entry_notional_usd), 0);
      if (openNotionalUsd + v2Policy.amountUsd > v2Policy.maxOpenNotionalUsd) {
        const result = { status: 'risk_paused', reason: 'max_open_notional', live_started: false };
        publish(result);
        return result;
      }
      if (dailyPnl.status !== 'verified') {
        const result = { status: 'risk_paused', reason: 'daily_realized_pnl_unverified', live_started: false };
        publish(result);
        return result;
      }
      const dailyRealizedPnlUsd = number(dailyPnl.pnl_usd, 0);
      if (dailyRealizedPnlUsd <= -v2Policy.dailyLossStopUsd && !lossOverride.active) {
        const result = { status: 'risk_paused', reason: 'daily_realized_loss_stop', live_started: false };
        publish(result);
        return result;
      }
    }
    const configuredUsd = String(options.amountUsd || env.OKX_ORDER_NOTIONAL_USD || '').trim();
    let entryNotionalUsd = null;
    let entryNativePriceUsd = null;
    let entryPriceUsd = number(quote?.price_usd ?? quote?.price);
    let amountNativeAtomic;
    if (v2Policy) {
      if (configuredUsd && number(configuredUsd, NaN) !== v2Policy.amountUsd) throw new Error('chain_v2_order_size_conflict');
      entryNotionalUsd = v2Policy.amountUsd;
      entryNativePriceUsd = await (deps.fetchNativeUsdPrice || fetchNativeUsdPrice)(chain.name, { fetch: deps.fetch });
      amountNativeAtomic = nativeAtomicForUsd(entryNotionalUsd, entryNativePriceUsd);
    } else if (configuredUsd) {
      entryNotionalUsd = number(configuredUsd);
      entryNativePriceUsd = await (deps.fetchNativeUsdPrice || fetchNativeUsdPrice)(chain.name, { fetch: deps.fetch });
      amountNativeAtomic = nativeAtomicForUsd(entryNotionalUsd, entryNativePriceUsd);
    } else {
      amountNativeAtomic = uintString(options.amountNativeAtomic || env.OKX_TRADE_NATIVE_ATOMIC || DEFAULT_AMOUNT_NATIVE_ATOMIC);
    }
    if (v2Policy) {
      let tradeability;
      try {
        tradeability = await withTimeout((deps.quoteBuyTradeability || quoteBuyTradeability)({
          env, candidate, amountNativeAtomic, deps: deps.buyQuoteDeps || {},
        }), exactEntryTradeabilityTimeoutMs(env), 'exact_entry_tradeability_timeout');
      } catch (error) {
        const result = { status: 'risk_paused', reason: publicError(error.message || 'exact_entry_quote_unavailable'),
          live_started: false, symbol: candidate.symbol };
        if (chain.name === 'bsc') {
          clearBscSellabilityConfirmation(state, candidate);
        }
        saveState();
        publish(result);
        return result;
      }
      // Network preflight can take seconds. In production, compare the returned
      // quote with the clock after it arrives, not the round's start time.
      const exactNow = Object.hasOwn(options, 'now') ? now : new Date();
      const exact = exactEntryEvidence(candidate, tradeability, exactNow);
      if (!exact.accepted) {
        const result = { status: 'risk_paused', reason: exact.reason, live_started: false, symbol: candidate.symbol };
        if (chain.name === 'bsc') {
          clearBscSellabilityConfirmation(state, candidate);
        }
        saveState();
        publish(result);
        return result;
      }
      const exactQuote = {
        ...quote,
        price_usd: exact.exactPrice,
        quote_at: tradeability.quote_at,
        quote_status: 'fresh',
      };
      const exactGate = strategyEntryGate(candidate, exactQuote, exactNow, chain.name);
      if (!exactGate.accepted) {
        const result = { status: 'risk_paused', reason: exactGate.reason, live_started: false, symbol: candidate.symbol };
        if (chain.name === 'bsc') clearBscSellabilityConfirmation(state, candidate);
        saveState();
        publish(result);
        return result;
      }
      if (selectionSource === 'preflight_signals' && candidate.entry_authorized !== true) {
        const result = { status: 'risk_paused', reason: 'entry_not_authorized', live_started: false, symbol: candidate.symbol };
        if (chain.name === 'bsc') clearBscSellabilityConfirmation(state, candidate);
        saveState();
        publish(result);
        return result;
      }
      entryPriceUsd = exact.exactPrice;
      if (chain.name === 'bsc') {
        const confirmation = updateBscSellabilityConfirmation(state, candidate, tradeability, exactNow);
        saveState();
        if (confirmation.consecutive < v2Policy.minSellableCycles) {
          const result = { status: 'risk_paused', reason: 'bsc_sellability_confirmation_pending',
            live_started: false, symbol: candidate.symbol, sellability_confirmations: confirmation.consecutive };
          publish(result);
          return result;
        }
      }
    }
    const buy = await trackedExecution(deps.executeBuy || executeBuy, {
      env,
      candidate,
      amountNativeAtomic,
      entryPriceUsd,
      entryNotionalUsd,
      entryNativePriceUsd,
      live,
      deps: deps.buyDeps || {},
    }, id, 'buy');
    if (buy?.state !== 'buy_confirmed' && live) {
      const result = { status: 'buy_blocked', reason: buy?.reason || buy?.state || 'buy_failed', live_started: false, symbol: candidate.symbol };
      publish(result);
      return result;
    }
    if (!live) {
      const result = { status: buy?.state || 'dry_run_ready', live_started: false, symbol: candidate.symbol };
      publish(result);
      return result;
    }
    const boughtAtomic = uintString(buy.bought_atomic, 'buy_fill_not_detected');
    if (!/^0x[0-9a-fA-F]{64}$/.test(buy?.buy_tx || '')) throw new Error('buy_fill_not_detected');
    const entryPrice = entryPriceUsd;
    state.positions[id] = {
      id,
      chain: chain.name,
      symbol: String(candidate.symbol || 'MEME').slice(0, 32),
      token: address(candidate.contract_address, 'invalid_token'),
      contract_address: address(candidate.contract_address, 'invalid_token'),
      pool: poolId(candidate.pool_address, 'invalid_pool'),
      pool_address: poolId(candidate.pool_address, 'invalid_pool'),
      entry_at: nowIso(now),
      entry_price_usd: entryPrice,
      high_price_usd: entryPrice,
      remaining_atomic: boughtAtomic,
      original_atomic: boughtAtomic,
      entry_native_atomic: buy.entry_native_atomic || amountNativeAtomic,
      entry_native_spent_atomic: buy.native_spent_atomic || buy.entry_native_atomic || amountNativeAtomic,
      entry_gas_native_atomic: buy.gas_native_atomic || '0',
      ...(buy.pretrade_expected_token_atomic !== undefined ? { pretrade_expected_token_atomic: String(buy.pretrade_expected_token_atomic) } : {}),
      ...(buy.pretrade_round_trip_native_atomic !== undefined ? { pretrade_round_trip_native_atomic: String(buy.pretrade_round_trip_native_atomic) } : {}),
      ...(buy.pretrade_round_trip_loss_percent !== undefined ? { pretrade_round_trip_loss_percent: number(buy.pretrade_round_trip_loss_percent) } : {}),
      ...(buy.buy_price_impact_percent !== undefined ? { buy_price_impact_percent: number(buy.buy_price_impact_percent) } : {}),
      ...(buy.sell_price_impact_percent !== undefined ? { sell_price_impact_percent: number(buy.sell_price_impact_percent) } : {}),
      ...(entryNotionalUsd !== null ? { entry_notional_usd: entryNotionalUsd } : {}),
      ...(entryNativePriceUsd !== null ? { entry_native_price_usd: entryNativePriceUsd } : {}),
      realized_cost_basis_native_atomic: '0',
      realized_pnl_native_atomic: '0',
      buy_tx: buy.buy_tx || null,
      order_id: buy.order_id || null,
      tp1_hit: false,
      tp2_hit: false,
      tp3_hit: false,
      confirmed_sold_atomic: '0',
      ...(v2Policy ? {
        strategy_version: STRATEGY_VERSION,
        signal_stage: candidate.signal_stage,
        entry_route: candidate.entry_route,
        rank_score: number(candidate.rank_score),
        rank_components: redact(candidate.rank_components),
        entry_policy: redact(v2Policy),
        policy_checks: redact(candidate.policy_checks),
      } : {}),
    };
    state.fills.push({
      chain: chain.name,
      symbol: String(candidate.symbol || 'MEME').slice(0, 32),
      token: address(candidate.contract_address, 'invalid_token'),
      side: 'buy',
      tx_hash: buy.buy_tx || null,
      time: nowIso(now),
      amount_atomic: boughtAtomic,
      ...(buy.native_spent_atomic !== undefined ? { native_spent_atomic: String(buy.native_spent_atomic) } : {}),
      ...(buy.gas_native_atomic !== undefined ? { gas_native_atomic: String(buy.gas_native_atomic) } : {}),
    });
    state.seen[id] = nowIso(now);
    settle(id);
    saveState();
    const result = { status: 'position_opened', live_started: true, position: redact(state.positions[id]), buy_tx: buy.buy_tx || null };
    publish(result);
    return result;
  } catch (error) {
    const result = {
      status: 'blocked',
      provider: 'okx-dex-sdk',
      reason: publicError(error.message),
      live_started: false,
      ...publicDiagnostic(error),
    };
    publish(result);
    return result;
  }
}

function parseArgs(argv) {
  const args = new Set(argv);
  const value = (name) => {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : '';
  };
  return {
    live: args.has('--live'),
    once: args.has('--once'),
    exitOnly: args.has('--exit-only'),
    inputPath: value('--input') || undefined,
    statePath: value('--state') || undefined,
    statusPath: value('--status') || undefined,
    amountNativeAtomic: value('--amount-native-atomic') || undefined,
    amountUsd: value('--amount-usd') || undefined,
    intervalSeconds: number(value('--interval-seconds'), 1),
  };
}

export async function main(argv = process.argv.slice(2), env = process.env, hooks = {}) {
  const args = parseArgs(argv);
  const chain = executionChain(env);
  const paths = { ...defaultPaths(chain.name, args.live), ...definedOptions(args) };
  const commandPath = terminalControlPath(path.dirname(paths.statusPath), chain.name)
    .replace('-terminal-control.json', args.live ? '-terminal-control.json' : '-dry-run-terminal-control.json');
  const terminal = terminalFor(args, env, chain);
  const strictV2 = !legacyStrategyInputAllowed(env);
  const release = acquireDaemonLock(commandPath.replace('-terminal-control.json', '-daemon.lock'),
    { chain: chain.name, wallet: terminal.wallet });
  const log = hooks.log || (result => console.log(JSON.stringify(redact(result))));
  const sleep = hooks.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const startInterval = hooks.setInterval || setInterval;
  const cancelInterval = hooks.clearInterval || clearInterval;
  const publish = (status, reason = null) => {
    const amountMatches = !strictV2 || fixedAmountMatches(terminal, chain);
    const result = attachEffectiveStrategy({ status, daemon_running: true, live_started: false, ...(reason ? { reason } : {}) }, terminal, chain,
      { effective: strictV2 && amountMatches });
    writeStatus(paths.statusPath, result);
    return result;
  };
  try {
    if (strictV2 && !fixedAmountMatches(terminal, chain)) {
      log(publish('blocked', 'chain_v2_order_size_conflict'));
      return 1;
    }
    publish('starting');
    while (true) {
      let account;
      try { account = readJson(paths.statePath, defaultState()); } catch { account = null; }
      let command;
      try { command = args.live ? readTerminalCommand(commandPath) : undefined; }
      catch (error) { terminal.rejectRead(error.message); }
      if (command !== undefined) {
        await terminal.consume(command, { account,
          pendingTransactions: () => (hooks.deps?.pendingTransactions || fetchPendingTransactions)({
            env, chain: chain.name, wallet: terminal.wallet, fetch: hooks.deps?.fetch,
          }),
        });
      }
      if (terminal.stopRequested) {
        log(publish('stopped'));
        return 0;
      }
      if (strictV2 && !fixedAmountMatches(terminal, chain)) {
        log(publish('blocked', 'chain_v2_order_size_conflict'));
        if (args.once) return 1;
        await sleep(Math.max(1, args.intervalSeconds) * 1000);
        continue;
      }
      // Publish acknowledgement before preflight/quotes/signing can take time.
      publish('checking');
      let roundActive = true;
      const heartbeat = startInterval(() => {
        if (!roundActive) return;
        try { publish('checking'); }
        catch { /* A status write failure must not interrupt in-flight settlement. */ }
      }, 10_000);
      let result;
      try {
        result = await (hooks.runOnce || runLiveDaemonOnce)({ env, ...args, daemonRunning: true,
          ...terminal.options(), terminalControl: terminal, deps: hooks.deps });
      } finally {
        roundActive = false;
        cancelInterval(heartbeat);
      }
      log(result);
      if (args.once) return result.status === 'blocked' ? 1 : 0;
      await sleep(Math.max(1, args.intervalSeconds) * 1000);
    }
  } finally {
    release();
  }
}

const modulePath = fileURLToPath(import.meta.url);
const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath && path.normalize(modulePath).toLowerCase() === path.normalize(invokedPath).toLowerCase()) {
  process.exitCode = await main();
}
