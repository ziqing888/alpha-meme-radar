import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';

import {
  chainEntryPolicy,
  emergencyExitDecision,
  exitDecision,
  main,
  runLiveDaemonOnce,
  sellAmount,
  strategyEntryGate,
} from '../src/okx_dex_live_daemon.mjs';

const WALLET = '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A'.toLowerCase();
const TOKEN = `0x${'2'.repeat(40)}`;
const BSC_POOL = `0x${'3'.repeat(40)}`;
const ROBINHOOD_POOL = `0x${'4'.repeat(64)}`;

function v2Candidate(chain, overrides = {}) {
  const now = overrides.now || new Date().toISOString();
  const robinhood = chain === 'robinhood';
  const stage = 'aggregate_early_bird';
  const route = robinhood ? 'robinhood_aggregate_early_bird' : 'bsc_aggregate_early_bird';
  const firstMcap = robinhood ? 200_000 : 50_000;
  return {
    chain,
    symbol: 'V2MEME',
    contract_address: TOKEN,
    pool_address: robinhood ? ROBINHOOD_POOL : BSC_POOL,
    strategy_version: 'chain_v2',
    signal_stage: stage,
    entry_route: route,
    execution_mode: 'live_candidate',
    rank_score: 95,
    rank_components: { timing: 25, liquidity: 20 },
    first_seen_at: now,
    first_price_usd: 0.001,
    first_mcap_usd: firstMcap,
    current_price_usd: 0.0011,
    current_mcap_usd: firstMcap * 1.1,
    entry_delay_seconds: 30,
    markup_from_first: 1.1,
    policy_checks: {
      identity: true,
      supported_chain: true,
      first_snapshot: true,
      current_metrics: true,
      signal_stage: true,
      first_mcap: true,
      entry_age: true,
      markup: true,
      liquidity: true,
      buy_route: true,
      sell_route: true,
      round_trip_loss: true,
      buy_impact: true,
      sell_impact: true,
      hard_risk: true,
      sellability_confirmation: true,
      observed_sell: true,
    },
    eligible: true,
    reject_reason: '',
    quote_status: 'fresh',
    quote_at: now,
    price_usd: 0.0011,
    mcap: firstMcap * 1.1,
    valuation_type: 'market_cap',
    liquidity: 50_000,
    round_trip_loss_pct: robinhood ? 10 : 7,
    buy_price_impact_pct: 4,
    sell_price_impact_pct: 4,
    sellable_cycles: robinhood ? 1 : 2,
    sell_count5m: 2,
    gmgn_risk_flags: [],
    ...overrides,
  };
}

function tempPaths(chain) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `chain-v2-${chain}-`));
  return {
    inputPath: path.join(dir, `${chain}-input.json`),
    statePath: path.join(dir, `${chain}-state.json`),
    statusPath: path.join(dir, `${chain}-status.json`),
  };
}

function exactEntryEvidence(row, now = new Date(), overrides = {}) {
  return {
    chain: row.chain,
    contract_address: row.contract_address,
    pool_address: row.pool_address,
    quote_at: now.toISOString(),
    exact_buy_price_usd: row.current_price_usd,
    buy_route_id: 'buy-route',
    sell_route_id: 'sell-route',
    pretrade_expected_token_atomic: '1000',
    pretrade_round_trip_native_atomic: '900',
    pretrade_round_trip_net_native_atomic: '880',
    ...overrides,
  };
}

function preflightCandidate(chain, now = new Date(), overrides = {}) {
  const row = v2Candidate(chain, { now: now.toISOString(), quote_status: 'unavailable' });
  const { discovery_snapshot: snapshotOverrides = {}, ...candidateOverrides } = overrides;
  return {
    ...row,
    entry_authorized: true,
    ...candidateOverrides,
    discovery_snapshot: {
      chain: row.chain,
      contract_address: row.contract_address,
      pool_address: row.pool_address,
      first_seen_at: now.toISOString(),
      price_usd: row.current_price_usd,
      mcap: row.current_mcap_usd,
      market_cap: row.current_mcap_usd,
      valuation_type: 'market_cap',
      liquidity: row.liquidity,
      sell_count5m: row.sell_count5m,
      ...snapshotOverrides,
    },
  };
}

describe('chain_v2 entry contract', () => {
  it('uses immutable chain policy limits and fixed notionals', () => {
    assert.deepEqual(chainEntryPolicy('robinhood'), {
      chain: 'robinhood', signalStage: 'aggregate_early_bird', entryRoute: 'robinhood_aggregate_early_bird',
      amountUsd: 2, maxAgeSeconds: 600, minFirstMcapUsd: 10_000, maxFirstMcapUsd: 300_000,
      maxMarkup: 1.25, minLiquidityUsd: 8_000, maxRoundTripLossPct: 25,
      maxBuyImpactPct: 12, maxSellImpactPct: null, minSellableCycles: 1, requiresObservedSell: false,
      maxOpenPositions: 3, maxOpenNotionalUsd: 15, dailyLossStopUsd: 5,
    });
    assert.equal(chainEntryPolicy('bsc').amountUsd, 1);
    assert.equal(chainEntryPolicy('bsc').signalStage, 'aggregate_early_bird');
    assert.equal(chainEntryPolicy('bsc').minLiquidityUsd, 8_000);
    assert.equal(chainEntryPolicy('bsc').maxRoundTripLossPct, 25);
    assert.equal(chainEntryPolicy('bsc').maxSellImpactPct, 12);
  });

  it('accepts high rank scores because rank never authorizes a trade', () => {
    const now = new Date();
    for (const chain of ['bsc', 'robinhood']) {
      for (const rank_score of [70, 95, 100]) {
        const row = v2Candidate(chain, { now: now.toISOString(), rank_score });
        assert.deepEqual(strategyEntryGate(row, row, now, chain), { accepted: true, reason: 'accepted' });
      }
    }
  });

  it('fails closed on an incomplete v2 contract', () => {
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString() });
    delete row.first_price_usd;
    assert.equal(strategyEntryGate(row, row, now, 'bsc').reason, 'chain_v2_contract_incomplete');
  });

  it('uses the chain rank-unavailable reason without thresholding valid ranks', () => {
    const now = new Date();
    for (const chain of ['bsc', 'robinhood']) {
      const row = v2Candidate(chain, { now: now.toISOString() });
      delete row.rank_score;
      assert.equal(strategyEntryGate(row, row, now, chain).reason, `${chain}_rank_unavailable`);
      row.rank_score = null;
      assert.equal(strategyEntryGate(row, row, now, chain).reason, `${chain}_rank_unavailable`);
    }
  });

  it('keeps discovery out of live execution on both chains', () => {
    const now = new Date();
    const robinhood = v2Candidate('robinhood', {
      now: now.toISOString(), signal_stage: 'aggregate_discovery',
      entry_route: 'robinhood_aggregate_discovery',
    });
    assert.equal(strategyEntryGate(robinhood, robinhood, now, 'robinhood').reason, 'robinhood_stage_required');
    const bsc = v2Candidate('bsc', {
      now: now.toISOString(), signal_stage: 'aggregate_discovery',
      entry_route: 'bsc_aggregate_discovery_shadow', execution_mode: 'shadow', eligible: false,
    });
    assert.equal(strategyEntryGate(bsc, bsc, now, 'bsc').reason, 'bsc_shadow_only');
  });

  it('accepts monitor-promoted early-bird and confirmation routes on both chains', () => {
    const now = new Date();
    for (const chain of ['bsc', 'robinhood']) {
      for (const stage of ['aggregate_early_bird', 'aggregate_confirmation']) {
        const row = v2Candidate(chain, {
          now: now.toISOString(),
          signal_stage: stage,
          entry_route: `${chain}_${stage}`,
        });
        assert.deepEqual(strategyEntryGate(row, row, now, chain), {
          accepted: true,
          reason: 'accepted',
        });
      }
    }
  });

  it('requires BSC upstream hard-risk but delegates sellability to exact executor quotes', () => {
    const now = new Date();
    for (const policy_checks of [
      {},
      { hard_risk: false, sellability_confirmation: true },
    ]) {
      const row = v2Candidate('bsc', { now: now.toISOString(), policy_checks });
      assert.equal(strategyEntryGate(row, row, now, 'bsc').reason, 'chain_v2_policy_checks_required');
    }
    for (const sellability_confirmation of [undefined, null, false]) {
      const row = v2Candidate('bsc', {
        now: now.toISOString(),
        policy_checks: { hard_risk: true, sellability_confirmation },
      });
      assert.equal(strategyEntryGate(row, row, now, 'bsc').accepted, true);
    }
  });

  it('defers missing Robinhood contract fields to exact round-trip evidence but rejects explicit risk', () => {
    const now = new Date();
    const deferred = v2Candidate('robinhood', {
      now: now.toISOString(), policy_checks: { hard_risk: null },
    });
    const rejected = v2Candidate('robinhood', {
      now: now.toISOString(), policy_checks: { hard_risk: false },
    });
    assert.equal(strategyEntryGate(deferred, deferred, now, 'robinhood').accepted, true);
    assert.equal(strategyEntryGate(rejected, rejected, now, 'robinhood').reason, 'chain_v2_policy_checks_required');
  });

  it('recomputes entry age and markup instead of trusting candidate-provided values', () => {
    const now = new Date('2026-09-09T10:00:00.000Z');
    const stale = v2Candidate('bsc', {
      now: now.toISOString(), first_seen_at: new Date(now.getTime() - 601_000).toISOString(),
      entry_delay_seconds: 1,
    });
    assert.equal(strategyEntryGate(stale, stale, now, 'bsc').reason, 'bsc_entry_window');

    const chased = v2Candidate('bsc', {
      now: now.toISOString(), first_price_usd: 0.001, current_price_usd: 0.01,
      price_usd: 0.01, markup_from_first: 1.01,
    });
    assert.equal(strategyEntryGate(chased, chased, now, 'bsc').reason, 'bsc_markup_limit');
  });

  it('requires top-level and candidate chain_v2 contracts by default', async () => {
    for (const [payload, reason] of [
      [{ signals: [v2Candidate('bsc')], quotes: [] }, 'chain_v2_input_required'],
      [{ strategy_version: 'chain_v2', signals: [{ ...v2Candidate('bsc'), strategy_version: undefined }], quotes: [] }, 'chain_v2_candidate_required'],
    ]) {
      const paths = tempPaths('bsc');
      fs.writeFileSync(paths.inputPath, JSON.stringify(payload));
      let bought = false;
      const result = await runLiveDaemonOnce({
        env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
        ...paths, live: true,
        deps: {
          preflight: async () => ({ ready: true, wallet: WALLET }),
          executeBuy: async () => { bought = true; },
        },
      });
      assert.equal(result.reason, reason);
      assert.equal(bought, false);
    }
  });

  it('preflights a fresh discovery reference without an external quote before buying', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date('2026-09-11T04:00:00.000Z');
    const row = preflightCandidate('robinhood', now);
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    const calls = [];
    const result = await runLiveDaemonOnce({
      env: {
        EVM_WALLET_ADDRESS: WALLET,
        EXECUTION_CHAIN_NAME: 'robinhood',
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
      },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 2_000,
        quoteBuyTradeability: async () => {
          calls.push('quote');
          return exactEntryEvidence(row, now);
        },
        executeBuy: async () => {
          calls.push('buy');
          return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' };
        },
      },
    });
    assert.equal(result.status, 'position_opened');
    assert.deepEqual(calls, ['quote', 'buy']);
  });

  it('checks an async exact quote against the time after the quote returns', async () => {
    const paths = tempPaths('robinhood');
    const capturedAt = new Date();
    const row = v2Candidate('robinhood', { now: capturedAt.toISOString() });
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [row], quotes: [row], preflight_signals: [],
    }));
    let bought = false;
    const result = await runLiveDaemonOnce({
      env: {
        EVM_WALLET_ADDRESS: WALLET,
        EXECUTION_CHAIN_NAME: 'robinhood',
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
      },
      ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 2_000,
        quoteBuyTradeability: async () => {
          await new Promise(resolve => setTimeout(resolve, 20));
          return exactEntryEvidence(row, new Date());
        },
        executeBuy: async () => {
          bought = true;
          return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' };
        },
      },
    });
    assert.equal(result.status, 'position_opened');
    assert.equal(bought, true);
  });

  it('rechecks a discovery reference with the exact OKX price before buying', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date('2026-09-11T04:00:00.000Z');
    const row = preflightCandidate('robinhood', now);
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    let bought = false;
    const result = await runLiveDaemonOnce({
      env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 2_000,
        quoteBuyTradeability: async () => exactEntryEvidence(row, now, { exact_buy_price_usd: 0.01 }),
        executeBuy: async () => { bought = true; },
      },
    });
    assert.equal(result.reason, 'robinhood_markup_limit');
    assert.equal(bought, false);
  });

  it('does not buy a discovery reference when exact evidence has no sell route', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date('2026-09-11T04:00:00.000Z');
    const row = preflightCandidate('robinhood', now);
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    let bought = false;
    const result = await runLiveDaemonOnce({
      env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 2_000,
        quoteBuyTradeability: async () => exactEntryEvidence(row, now, { sell_route_id: '' }),
        executeBuy: async () => { bought = true; },
      },
    });
    assert.equal(result.reason, 'exact_entry_quote_unavailable');
    assert.equal(bought, false);
  });

  it('preflights an authorized discovery reference throughout the entry window', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date('2026-09-11T04:05:00.000Z');
    const capturedAt = new Date(now.getTime() - 300_000);
    const row = preflightCandidate('robinhood', capturedAt);
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    let quoted = false;
    let bought = false;
    const result = await runLiveDaemonOnce({
      env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 2_000,
        quoteBuyTradeability: async () => {
          quoted = true;
          return exactEntryEvidence(row, now);
        },
        executeBuy: async () => {
          bought = true;
          return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' };
        },
      },
    });
    assert.equal(result.status, 'position_opened');
    assert.equal(quoted, true);
    assert.equal(bought, true);
  });

  it('does not preflight a discovery reference outside the policy entry window', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date('2026-09-11T04:10:01.000Z');
    const capturedAt = new Date(now.getTime() - 601_000);
    const row = preflightCandidate('robinhood', capturedAt);
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    let quoted = false;
    const result = await runLiveDaemonOnce({
      env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteBuyTradeability: async () => { quoted = true; },
      },
    });
    assert.equal(result.reason, 'stale_discovery_snapshot');
    assert.equal(quoted, false);
  });

  it('preflights but does not buy a discovery reference without entry authorization', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date('2026-09-11T04:00:00.000Z');
    const row = preflightCandidate('robinhood', now, { entry_authorized: false });
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    let quoted = false;
    let bought = false;
    const result = await runLiveDaemonOnce({
      env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 2_000,
        quoteBuyTradeability: async () => { quoted = true; return exactEntryEvidence(row, now); },
        executeBuy: async () => { bought = true; },
      },
    });
    assert.equal(result.reason, 'entry_not_authorized');
    assert.equal(quoted, true);
    assert.equal(bought, false);
  });

  it('requires two distinct exact sellability confirmations for a BSC discovery reference', async () => {
    const paths = tempPaths('bsc');
    const start = new Date('2026-09-11T04:00:00.000Z');
    const row = preflightCandidate('bsc', start);
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [], quotes: [], preflight_signals: [row],
    }));
    let quoteNumber = 0;
    let buys = 0;
    const options = {
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 500,
        quoteBuyTradeability: async () => {
          quoteNumber += 1;
          return exactEntryEvidence(row, new Date(start.getTime() + quoteNumber * 1_000));
        },
        executeBuy: async () => {
          buys += 1;
          return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' };
        },
      },
    };
    const first = await runLiveDaemonOnce({ ...options, now: new Date(start.getTime() + 1_000) });
    assert.equal(first.reason, 'bsc_sellability_confirmation_pending');
    assert.equal(buys, 0);
    const second = await runLiveDaemonOnce({ ...options, now: new Date(start.getTime() + 2_000) });
    assert.equal(second.status, 'position_opened');
    assert.equal(buys, 1);
  });

  for (const [chain, amountUsd, nativePrice, expectedAtomic] of [
    ['bsc', 1, 500, '2000000000000000'],
    ['robinhood', 2, 2_000, '1000000000000000'],
  ]) {
    it(`submits the fixed ${amountUsd} USD ${chain} notional and persists the strategy`, async () => {
      const paths = tempPaths(chain);
      const row = v2Candidate(chain);
      fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [row], quotes: [row] }));
      let submitted;
      let quoteSequence = 0;
      const options = {
        env: {
          BSC_WALLET_ADDRESS: WALLET,
          EVM_WALLET_ADDRESS: WALLET,
          EXECUTION_CHAIN_NAME: chain,
          OKX_LIVE_ENABLED: '1',
          OKX_ALLOW_AUTOMATED_TRADES: '1',
        },
        ...paths,
        live: true,
        deps: {
          preflight: async () => ({ ready: true, wallet: WALLET }),
          fetchNativeUsdPrice: async () => nativePrice,
          quoteBuyTradeability: async () => exactEntryEvidence(row, new Date(Date.now() - 1_000 + quoteSequence++)),
          executeBuy: async ({ amountNativeAtomic }) => {
            submitted = amountNativeAtomic;
            return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' };
          },
        },
      };
      if (chain === 'bsc') {
        const first = await runLiveDaemonOnce(options);
        assert.equal(first.reason, 'bsc_sellability_confirmation_pending');
        assert.equal(submitted, undefined);
      }
      const result = await runLiveDaemonOnce(options);
      assert.equal(result.status, 'position_opened');
      assert.equal(submitted, expectedAtomic);
      const state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
      const position = Object.values(state.positions)[0];
      assert.equal(position.strategy_version, 'chain_v2');
      assert.equal(position.entry_notional_usd, amountUsd);
      assert.equal(position.signal_stage, row.signal_stage);
      assert.equal(position.entry_route, row.entry_route);
    });
  }

  it('rejects a conflicting v2 order-size override', async () => {
    const paths = tempPaths('bsc');
    const row = v2Candidate('bsc');
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [row], quotes: [row] }));
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true, amountUsd: '5',
      deps: { preflight: async () => ({ ready: true, wallet: WALLET }) },
    });
    assert.equal(result.reason, 'chain_v2_order_size_conflict');
  });

  it('persists BSC exact sellability by chain, contract and pool and rejects duplicate quote timestamps', async () => {
    const paths = tempPaths('bsc');
    const now = new Date('2026-09-09T10:00:00.000Z');
    const row = v2Candidate('bsc', { now: now.toISOString() });
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [row], quotes: [row] }));
    let quoteAt = now.toISOString();
    let buys = 0;
    const options = {
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 500,
        quoteBuyTradeability: async () => exactEntryEvidence(row, now, { quote_at: quoteAt }),
        executeBuy: async () => { buys += 1; return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' }; },
      },
    };
    assert.equal((await runLiveDaemonOnce(options)).reason, 'bsc_sellability_confirmation_pending');
    assert.equal((await runLiveDaemonOnce(options)).reason, 'bsc_sellability_confirmation_pending');
    assert.equal(buys, 0);
    quoteAt = new Date(now.getTime() + 1_000).toISOString();
    const result = await runLiveDaemonOnce({ ...options, now: new Date(now.getTime() + 1_000) });
    assert.equal(result.status, 'position_opened');
    assert.equal(buys, 1);
    const state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    const keys = Object.keys(state.entry_sellability_confirmations);
    assert.deepEqual(keys, [`bsc:${TOKEN.toLowerCase()}:${BSC_POOL.toLowerCase()}`]);
    assert.equal(state.entry_sellability_confirmations[keys[0]].consecutive, 2);
  });

  it('clears BSC sellability progress when an intervening exact gate fails', async () => {
    const paths = tempPaths('bsc');
    const start = new Date('2026-09-09T10:00:00.000Z');
    const row = v2Candidate('bsc', { now: start.toISOString() });
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [row], quotes: [row] }));
    let quoteNumber = 0;
    let buys = 0;
    const options = {
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 500,
        quoteBuyTradeability: async () => {
          quoteNumber += 1;
          const quoteAt = new Date(start.getTime() + quoteNumber * 1_000);
          return exactEntryEvidence(row, quoteAt, { exact_buy_price_usd: quoteNumber === 2 ? 0.01 : 0.0011 });
        },
        executeBuy: async () => { buys += 1; return { state: 'buy_confirmed', buy_tx: `0x${'5'.repeat(64)}`, bought_atomic: '1000' }; },
      },
    };

    assert.equal((await runLiveDaemonOnce({ ...options, now: new Date(start.getTime() + 1_000) })).reason,
      'bsc_sellability_confirmation_pending');
    assert.equal((await runLiveDaemonOnce({ ...options, now: new Date(start.getTime() + 2_000) })).reason,
      'bsc_markup_limit');
    assert.equal((await runLiveDaemonOnce({ ...options, now: new Date(start.getTime() + 3_000) })).reason,
      'bsc_sellability_confirmation_pending');
    assert.equal(buys, 0);
    const state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    const record = Object.values(state.entry_sellability_confirmations)[0];
    assert.equal(record.consecutive, 1);
  });

  it('rejects a 10x chase revealed only by the refreshed exact buy quote', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), first_price_usd: 0.001,
      current_price_usd: 0.0011, price_usd: 0.0011, markup_from_first: 1.1 });
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [row], quotes: [row] }));
    let bought = false;
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 500,
        quoteBuyTradeability: async () => exactEntryEvidence(row, now, { exact_buy_price_usd: 0.01 }),
        executeBuy: async () => { bought = true; },
      },
    });
    assert.equal(result.reason, 'bsc_markup_limit');
    assert.equal(bought, false);
  });

  it('does not publish an effective v2 receipt when the fixed chain amount conflicts', async () => {
    const paths = tempPaths('bsc');
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [] }));
    const code = await main([
      '--live', '--once', '--amount-usd', '5', '--input', paths.inputPath,
      '--state', paths.statePath, '--status', paths.statusPath,
    ], { BSC_WALLET_ADDRESS: WALLET }, { log: () => {}, deps: {} });
    assert.equal(code, 1);
    const status = JSON.parse(fs.readFileSync(paths.statusPath, 'utf8'));
    assert.equal(status.reason, 'chain_v2_order_size_conflict');
    assert.equal(Object.hasOwn(status, 'strategy_version'), false);
    assert.equal(Object.hasOwn(status.terminal_control, 'strategy_version'), false);
  });

  it('treats legacy positions with unknown notional as unverified exposure', async () => {
    const paths = tempPaths('bsc');
    const row = v2Candidate('bsc');
    const key = `bsc:${'0x' + '9'.repeat(40)}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [row], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { id: key, chain: 'bsc', contract_address: `0x${'9'.repeat(40)}`, pool_address: BSC_POOL,
        entry_price_usd: 0.001, high_price_usd: 0.001, original_atomic: '1000', remaining_atomic: '1000',
        entry_native_spent_atomic: '1000', realized_cost_basis_native_atomic: '0', entry_at: new Date().toISOString() } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchPositionQuote: async () => ({ ...row, contract_address: `0x${'9'.repeat(40)}` }),
        quoteSellValue: async () => ({ expected_native_atomic: '1000', estimated_net_native_atomic: '1000', route_id: 'primary', price_impact_percent: 1 }),
      },
    });
    assert.equal(result.reason, 'open_notional_unverified');
  });

  it('publishes the effective chain_v2 policy in status and terminal control receipts', async () => {
    const paths = tempPaths('bsc');
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [] }));
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true,
      deps: { preflight: async () => ({ ready: true, wallet: WALLET }) },
    });
    const status = JSON.parse(fs.readFileSync(paths.statusPath, 'utf8'));
    for (const payload of [result, status]) {
      assert.equal(payload.strategy_version, 'chain_v2');
      assert.equal(payload.signal_stage, 'aggregate_early_bird');
      assert.equal(payload.entry_route, 'bsc_aggregate_early_bird');
      assert.equal(payload.terminal_control.strategy_version, 'chain_v2');
      assert.equal(payload.terminal_control.signal_stage, 'aggregate_early_bird');
      assert.equal(payload.terminal_control.entry_route, 'bsc_aggregate_early_bird');
    }
  });
});

describe('chain_v2 exits', () => {
  function position(chain, overrides = {}) {
    return {
      chain, strategy_version: 'chain_v2', entry_price_usd: 1, high_price_usd: 1,
      original_atomic: '1000', remaining_atomic: '1000', entry_native_spent_atomic: '1000',
      realized_cost_basis_native_atomic: '0', confirmed_sold_atomic: '0', entry_at: new Date().toISOString(),
      tp1_hit: false, tp2_hit: false, tp3_hit: false, ...overrides,
    };
  }

  it('uses cumulative confirmed quantity for a short first take-profit fill', () => {
    const p = position('robinhood', { remaining_atomic: '700', confirmed_sold_atomic: '300', realized_cost_basis_native_atomic: '300' });
    const decision = exitDecision(p, { price_usd: 2.1 }, new Date(), { expected_native_atomic: '1400', estimated_net_native_atomic: '1400' });
    assert.equal(decision.reason, 'take_profit_1');
    assert.equal(sellAmount(p, decision), '200');
  });

  it('persists daily realized USD loss by chain and date, stops the next entry, then resets next day', async () => {
    const paths = tempPaths('bsc');
    const now = new Date('2026-09-09T10:00:00.000Z');
    const heldToken = `0x${'8'.repeat(40)}`;
    const heldKey = `bsc:${heldToken}`;
    const candidateToken = `0x${'7'.repeat(40)}`;
    const candidatePool = `0x${'6'.repeat(40)}`;
    const candidate = v2Candidate('bsc', {
      now: now.toISOString(), contract_address: candidateToken, pool_address: candidatePool,
    });
    const heldQuote = { ...v2Candidate('bsc', { now: now.toISOString() }), contract_address: heldToken,
      pool_address: BSC_POOL, price_usd: 0.5, current_price_usd: 0.5 };
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [candidate], quotes: [heldQuote, candidate] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [heldKey]: {
        id: heldKey, chain: 'bsc', strategy_version: 'chain_v2', symbol: 'LOSS', contract_address: heldToken,
        token: heldToken, pool_address: BSC_POOL, pool: BSC_POOL, entry_at: now.toISOString(),
        entry_price_usd: 1, high_price_usd: 1, original_atomic: '1000', remaining_atomic: '1000',
        confirmed_sold_atomic: '0', entry_native_spent_atomic: '1000000000000000000',
        realized_cost_basis_native_atomic: '0', entry_notional_usd: 1, entry_native_price_usd: 10,
      } }, closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const env = { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' };
    const deps = {
      preflight: async () => ({ ready: true, wallet: WALLET }),
      quoteSellValue: async () => ({ expected_native_atomic: '700000000000000000', estimated_net_native_atomic: '700000000000000000',
        route_id: 'primary', price_impact_percent: 1, quote_at: now.toISOString() }),
      executeSell: async () => ({ state: 'sell_confirmed', sell_tx: `0x${'9'.repeat(64)}`, sold_atomic: '1000',
        net_native_received_atomic: '700000000000000000' }),
      fetchNativeUsdPrice: async () => 10,
      quoteBuyTradeability: async ({ candidate: row }) => exactEntryEvidence(row, new Date('2026-09-10T10:00:00.000Z')),
    };
    assert.equal((await runLiveDaemonOnce({ env, ...paths, live: true, now, deps })).status, 'position_closed');
    let state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    assert.equal(state.daily_realized_pnl_by_chain.bsc.date, '2026-09-09');
    assert.equal(state.daily_realized_pnl_by_chain.bsc.pnl_usd, -3);
    assert.equal((await runLiveDaemonOnce({ env, ...paths, live: true, now, deps })).reason, 'daily_realized_loss_stop');

    const nextDay = new Date('2026-09-10T10:00:00.000Z');
    const nextCandidate = v2Candidate('bsc', { now: nextDay.toISOString(), contract_address: candidateToken, pool_address: candidatePool });
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [nextCandidate], quotes: [nextCandidate] }));
    assert.equal((await runLiveDaemonOnce({ env, ...paths, live: true, now: nextDay, deps })).reason,
      'bsc_sellability_confirmation_pending');
    state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    assert.equal(state.daily_realized_pnl_by_chain.bsc.date, '2026-09-10');
    assert.equal(state.daily_realized_pnl_by_chain.bsc.pnl_usd, 0);
  });

  it('allows a date-bound chain-specific daily loss override without clearing realized pnl', async () => {
    const paths = tempPaths('bsc');
    const now = new Date('2026-09-11T04:00:00.000Z');
    const candidate = v2Candidate('bsc', { now: now.toISOString() });
    fs.writeFileSync(paths.inputPath, JSON.stringify({
      strategy_version: 'chain_v2', signals: [candidate], quotes: [candidate],
    }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', positions: {}, closed: [], fills: [], seen: {},
      terminal_pending: 0, execution_intents: {}, realized_pnl_native_atomic: '0',
      daily_realized_pnl_by_chain: {
        bsc: { chain: 'bsc', date: '2026-09-11', pnl_usd: -3, status: 'verified' },
      },
    }));
    const env = {
      BSC_WALLET_ADDRESS: WALLET,
      OKX_LIVE_ENABLED: '1',
      OKX_ALLOW_AUTOMATED_TRADES: '1',
      OKX_DAILY_LOSS_OVERRIDE_CHAIN: 'bsc',
      OKX_DAILY_LOSS_OVERRIDE_DATE: '2026-09-11',
    };
    const result = await runLiveDaemonOnce({
      env, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        fetchNativeUsdPrice: async () => 10,
        quoteBuyTradeability: async ({ candidate: row }) => exactEntryEvidence(row, now),
      },
    });

    assert.equal(result.reason, 'bsc_sellability_confirmation_pending');
    assert.equal(result.daily_loss_override_active, true);
    const state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    assert.equal(state.daily_realized_pnl_by_chain.bsc.pnl_usd, -3);
  });

  it('keeps a short-filled stage pending and submits only the residual target next round', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date();
    const row = v2Candidate('robinhood', { now: now.toISOString(), price_usd: 2.1, current_price_usd: 2.1 });
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'robinhood', terminal_pending: 0, execution_intents: {},
      positions: {
        [`robinhood:${TOKEN.toLowerCase()}`]: {
          id: `robinhood:${TOKEN.toLowerCase()}`, chain: 'robinhood', strategy_version: 'chain_v2',
          symbol: 'V2MEME', token: TOKEN.toLowerCase(), contract_address: TOKEN.toLowerCase(),
          pool: ROBINHOOD_POOL.toLowerCase(), pool_address: ROBINHOOD_POOL.toLowerCase(),
          entry_price_usd: 1, high_price_usd: 1, original_atomic: '1000', remaining_atomic: '1000',
          confirmed_sold_atomic: '0', entry_native_spent_atomic: '1000', realized_cost_basis_native_atomic: '0',
          entry_at: now.toISOString(), tp1_hit: false, tp2_hit: false, tp3_hit: false,
        },
      }, closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const submitted = [];
    const deps = {
      preflight: async () => ({ ready: true, wallet: WALLET }),
      quoteSellValue: async ({ tokenAmountAtomic }) => ({
        expected_native_atomic: String(Number(tokenAmountAtomic) * 2),
        estimated_net_native_atomic: String(Number(tokenAmountAtomic) * 2),
        estimated_gas_native_atomic: '1', price_impact_percent: 2, route_id: 'primary',
        quote_at: now.toISOString(), prepared_swap: { routerResult: { fromTokenAmount: tokenAmountAtomic }, tx: {} },
      }),
      executeSell: async ({ tokenAmountAtomic }) => {
        submitted.push(tokenAmountAtomic);
        const sold = submitted.length === 1 ? '300' : tokenAmountAtomic;
        return { state: 'sell_confirmed', sell_tx: `0x${String(submitted.length).repeat(64)}`, sold_atomic: sold };
      },
    };
    await runLiveDaemonOnce({ env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood', OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' }, ...paths, live: true, now, deps });
    let state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    assert.equal(Object.values(state.positions)[0].tp1_hit, false);
    assert.equal(Object.values(state.positions)[0].confirmed_sold_atomic, '300');
    await runLiveDaemonOnce({ env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood', OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' }, ...paths, live: true, now, deps });
    state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    assert.deepEqual(submitted, ['500', '200']);
    assert.equal(Object.values(state.positions)[0].tp1_hit, true);
    assert.equal(Object.values(state.positions)[0].remaining_atomic, '500');
  });

  it('does not broadcast an emergency exit while the token has a pending intent', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 60_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 1,
      execution_intents: { [key]: { state: 'pending', side: 'sell', wallet: WALLET, chain: 'bsc', token: key, amount_atomic: '1000', transactions: [{ side: 'sell', stage: 'broadcast', tx_hash: `0x${'9'.repeat(64)}` }] } },
      positions: { [key]: { id: key, chain: 'bsc', strategy_version: 'chain_v2', symbol: 'V2MEME', token: TOKEN.toLowerCase(), contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(), entry_price_usd: 1, high_price_usd: 1, original_atomic: '1000', remaining_atomic: '1000', confirmed_sold_atomic: '0', entry_native_spent_atomic: '1000', realized_cost_basis_native_atomic: '0', entry_at: now.toISOString(), last_usable_liquidity_usd: 100_000, tp1_hit: false, tp2_hit: false, tp3_hit: false } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    let broadcasts = 0;
    const routeRequests = [];
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1', OKX_ALTERNATE_DEX_IDS: 'alt-dex' }, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async ({ routePreference }) => {
          routeRequests.push(routePreference || 'primary');
          return { expected_native_atomic: '900', estimated_net_native_atomic: '900', estimated_gas_native_atomic: '1', price_impact_percent: 2, route_id: routePreference || 'primary' };
        },
        executeSell: async () => { broadcasts += 1; },
      },
    });
    assert.equal(result.reason, 'execution_reconciliation_required');
    assert.equal(broadcasts, 0);
    assert.deepEqual(routeRequests, ['primary']);
    const state = JSON.parse(fs.readFileSync(paths.statePath, 'utf8'));
    assert.equal(state.positions[key].emergency_exit.reason, 'emergency_liquidity_drop');
  });

  it('persists emergency state then refreshes the configured alternate route before selling', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 60_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { id: key, chain: 'bsc', strategy_version: 'chain_v2', symbol: 'V2MEME', token: TOKEN.toLowerCase(), contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(), entry_price_usd: 1, high_price_usd: 1, original_atomic: '1000', remaining_atomic: '1000', confirmed_sold_atomic: '0', entry_native_spent_atomic: '1000', realized_cost_basis_native_atomic: '0', entry_at: now.toISOString(), last_usable_liquidity_usd: 100_000, last_primary_route_id: 'primary', tp1_hit: false, tp2_hit: false, tp3_hit: false } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const routeRequests = [];
    let usedPreparedSwap = null;
    const alternateSwap = { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'alternate' } };
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1', OKX_ALTERNATE_DEX_IDS: 'alt-dex' }, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async ({ routePreference }) => {
          routeRequests.push(routePreference || 'primary');
          return { expected_native_atomic: routePreference ? '1000' : '900', estimated_net_native_atomic: routePreference ? '1000' : '900', estimated_gas_native_atomic: '1', price_impact_percent: 2, route_id: routePreference ? 'alternate' : 'primary', prepared_swap: routePreference ? alternateSwap : null };
        },
        executeSell: async ({ tokenAmountAtomic, preparedSwap }) => {
          usedPreparedSwap = preparedSwap;
          return { state: 'sell_confirmed', sell_tx: `0x${'8'.repeat(64)}`, sold_atomic: tokenAmountAtomic };
        },
      },
    });
    assert.equal(result.exit_reason, 'emergency_liquidity_drop');
    assert.deepEqual(routeRequests, ['primary', 'alternate']);
    assert.equal(usedPreparedSwap, alternateSwap);
  });

  it('keeps a persisted emergency latched, preserves its liquidity baseline and retries next round', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 90_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { ...position('bsc'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(),
        contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(),
        entry_notional_usd: 1, last_usable_liquidity_usd: 100_000,
        emergency_exit: { reason: 'emergency_liquidity_drop', triggered_at: now.toISOString(), liquidity_baseline_usd: 100_000 } } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    let attempts = 0;
    const options = {
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' }, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async () => ({ expected_native_atomic: '900', estimated_net_native_atomic: '900',
          route_id: 'primary', price_impact_percent: 2, prepared_swap: { routerResult: { fromTokenAmount: '1000' }, tx: {} } }),
        executeSell: async ({ tokenAmountAtomic }) => {
          attempts += 1;
          if (attempts === 1) throw new Error('sell_quote_failed');
          return { state: 'sell_confirmed', sell_tx: `0x${'4'.repeat(64)}`, sold_atomic: tokenAmountAtomic };
        },
      },
    };
    await runLiveDaemonOnce(options);
    assert.equal((await runLiveDaemonOnce(options)).status, 'position_closed');
    assert.equal(attempts, 2);
    const closed = JSON.parse(fs.readFileSync(paths.statePath, 'utf8')).closed[0];
    assert.equal(closed.emergency_exit.liquidity_baseline_usd, 100_000);
    assert.equal(closed.last_usable_liquidity_usd, 100_000);
  });

  it('revalidates an exact partial quote and cancels take profit when executable return no longer qualifies', async () => {
    const paths = tempPaths('robinhood');
    const now = new Date();
    const row = v2Candidate('robinhood', { now: now.toISOString(), price_usd: 2.2, current_price_usd: 2.2 });
    const key = `robinhood:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'robinhood', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { ...position('robinhood'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(),
        contract_address: TOKEN.toLowerCase(), pool: ROBINHOOD_POOL.toLowerCase(), pool_address: ROBINHOOD_POOL.toLowerCase(),
        entry_notional_usd: 5 } }, closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    let sold = false;
    const result = await runLiveDaemonOnce({
      env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood', OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async ({ tokenAmountAtomic }) => tokenAmountAtomic === '1000'
          ? { expected_native_atomic: '2200', estimated_net_native_atomic: '2200', route_id: 'primary', price_impact_percent: 2 }
          : { expected_native_atomic: '800', estimated_net_native_atomic: '800', route_id: 'primary', price_impact_percent: 2 },
        executeSell: async () => { sold = true; },
      },
    });
    assert.equal(result.status, 'holding_existing_position');
    assert.equal(result.reason, 'partial_exit_trigger_not_met');
    assert.equal(sold, false);
  });

  for (const scenario of [
    { name: 'impact exceeds 15 percent', full: '2200', exact: '1100', impact: 16, reason: 'partial_exit_impact_exceeded' },
    { name: 'exact route value diverges from price evidence', full: '4000', exact: '1100', impact: 2, reason: 'partial_exit_route_divergence' },
  ]) {
    it(`blocks a partial take profit when ${scenario.name}`, async () => {
      const paths = tempPaths('robinhood');
      const now = new Date();
      const row = v2Candidate('robinhood', { now: now.toISOString(), price_usd: Number(scenario.full) / 1000,
        current_price_usd: Number(scenario.full) / 1000 });
      const key = `robinhood:${TOKEN.toLowerCase()}`;
      fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
      fs.writeFileSync(paths.statePath, JSON.stringify({
        version: 1, wallet: WALLET, chain: 'robinhood', terminal_pending: 0, execution_intents: {},
        positions: { [key]: { ...position('robinhood'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(),
          contract_address: TOKEN.toLowerCase(), pool: ROBINHOOD_POOL.toLowerCase(), pool_address: ROBINHOOD_POOL.toLowerCase(),
          entry_notional_usd: 5 } }, closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
      }));
      let sold = false;
      const result = await runLiveDaemonOnce({
        env: { EVM_WALLET_ADDRESS: WALLET, EXECUTION_CHAIN_NAME: 'robinhood', OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
        ...paths, live: true, now,
        deps: {
          preflight: async () => ({ ready: true, wallet: WALLET }),
          quoteSellValue: async ({ tokenAmountAtomic }) => tokenAmountAtomic === '1000'
            ? { expected_native_atomic: scenario.full, estimated_net_native_atomic: scenario.full, route_id: 'primary', price_impact_percent: 2 }
            : { expected_native_atomic: scenario.exact, estimated_net_native_atomic: scenario.exact, route_id: 'primary', price_impact_percent: scenario.impact },
          executeSell: async () => { sold = true; },
        },
      });
      assert.equal(result.reason, scenario.reason);
      assert.equal(sold, false);
    });
  }

  it('sells immediately on an executable primary emergency route without alternate configuration', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 60_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { ...position('bsc'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(), contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(), last_usable_liquidity_usd: 100_000, last_primary_route_id: 'primary' } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const primarySwap = { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'primary' } };
    let usedPreparedSwap = null;
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' }, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async () => ({ expected_native_atomic: '900', estimated_net_native_atomic: '900', estimated_gas_native_atomic: '1', price_impact_percent: 2, route_id: 'primary', prepared_swap: primarySwap }),
        executeSell: async ({ tokenAmountAtomic, preparedSwap }) => {
          usedPreparedSwap = preparedSwap;
          return { state: 'sell_confirmed', sell_tx: `0x${'7'.repeat(64)}`, sold_atomic: tokenAmountAtomic };
        },
      },
    });
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'emergency_liquidity_drop');
    assert.equal(usedPreparedSwap, primarySwap);
  });

  it('keeps the executable primary route when a configured alternate is not materially better', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 60_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { ...position('bsc'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(), contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(), last_usable_liquidity_usd: 100_000, last_primary_route_id: 'primary' } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const primarySwap = { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'primary' } };
    const alternateSwap = { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'alternate' } };
    let usedPreparedSwap = null;
    await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1', OKX_ALTERNATE_DEX_IDS: 'alt-dex' }, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async ({ routePreference }) => routePreference === 'alternate'
          ? { expected_native_atomic: '920', estimated_net_native_atomic: '920', estimated_gas_native_atomic: '1', price_impact_percent: 2, route_id: 'alternate', prepared_swap: alternateSwap }
          : { expected_native_atomic: '900', estimated_net_native_atomic: '900', estimated_gas_native_atomic: '1', price_impact_percent: 2, route_id: 'primary', prepared_swap: primarySwap },
        executeSell: async ({ tokenAmountAtomic, preparedSwap }) => {
          usedPreparedSwap = preparedSwap;
          return { state: 'sell_confirmed', sell_tx: `0x${'6'.repeat(64)}`, sold_atomic: tokenAmountAtomic };
        },
      },
    });
    assert.equal(usedPreparedSwap, primarySwap);
  });

  it('never treats the primary route id as an alternate route', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 60_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { ...position('bsc'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(),
        contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(),
        entry_notional_usd: 1, last_usable_liquidity_usd: 100_000, last_primary_route_id: 'same' } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const primarySwap = { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'primary' } };
    let used;
    await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1', OKX_ALTERNATE_DEX_IDS: 'alt' },
      ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async ({ routePreference }) => ({ expected_native_atomic: routePreference ? '1000' : '900',
          estimated_net_native_atomic: routePreference ? '1000' : '900', route_id: 'same', price_impact_percent: 2,
          prepared_swap: routePreference ? { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'fake-alt' } } : primarySwap }),
        executeSell: async ({ tokenAmountAtomic, preparedSwap }) => { used = preparedSwap; return {
          state: 'sell_confirmed', sell_tx: `0x${'3'.repeat(64)}`, sold_atomic: tokenAmountAtomic,
        }; },
      },
    });
    assert.equal(used, primarySwap);
  });

  it('uses a configured alternate when the primary exact sell quote is unavailable', async () => {
    const paths = tempPaths('bsc');
    const now = new Date();
    const row = v2Candidate('bsc', { now: now.toISOString(), price_usd: 1, current_price_usd: 1, liquidity: 60_000 });
    const key = `bsc:${TOKEN.toLowerCase()}`;
    fs.writeFileSync(paths.inputPath, JSON.stringify({ strategy_version: 'chain_v2', signals: [], quotes: [row] }));
    fs.writeFileSync(paths.statePath, JSON.stringify({
      version: 1, wallet: WALLET, chain: 'bsc', terminal_pending: 0, execution_intents: {},
      positions: { [key]: { ...position('bsc'), id: key, symbol: 'V2MEME', token: TOKEN.toLowerCase(), contract_address: TOKEN.toLowerCase(), pool: BSC_POOL.toLowerCase(), pool_address: BSC_POOL.toLowerCase(), last_usable_liquidity_usd: 100_000, last_primary_route_id: 'primary' } },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    }));
    const alternateSwap = { routerResult: { fromTokenAmount: '1000' }, tx: { route: 'alternate' } };
    const routeRequests = [];
    let usedPreparedSwap = null;
    const result = await runLiveDaemonOnce({
      env: { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1', OKX_ALTERNATE_DEX_IDS: 'alt-dex' }, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        quoteSellValue: async ({ routePreference }) => {
          routeRequests.push(routePreference || 'primary');
          if (!routePreference) throw new Error('primary unavailable');
          return { expected_native_atomic: '850', estimated_net_native_atomic: '850', estimated_gas_native_atomic: '1', price_impact_percent: 3, route_id: 'alternate', prepared_swap: alternateSwap };
        },
        executeSell: async ({ tokenAmountAtomic, preparedSwap }) => {
          usedPreparedSwap = preparedSwap;
          return { state: 'sell_confirmed', sell_tx: `0x${'5'.repeat(64)}`, sold_atomic: tokenAmountAtomic };
        },
      },
    });
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'emergency_primary_route_missing');
    assert.deepEqual(routeRequests, ['primary', 'alternate']);
    assert.equal(usedPreparedSwap, alternateSwap);
  });

  it('keeps BSC 3x and 5x as shadow observations without another sale', () => {
    const p = position('bsc', { remaining_atomic: '500', confirmed_sold_atomic: '500', realized_cost_basis_native_atomic: '500', tp1_hit: true });
    const decision = exitDecision(p, { price_usd: 5.1 }, new Date(), { expected_native_atomic: '2550', estimated_net_native_atomic: '2550' });
    assert.equal(decision.exit, false);
    assert.equal(decision.reason, 'hold');
    assert.equal(p.shadow_tp2_observed, true);
    assert.equal(p.shadow_tp3_observed, true);
  });

  it('keeps Robinhood cumulative targets at 50, 60 and 70 percent', () => {
    const p = position('robinhood', { remaining_atomic: '500', confirmed_sold_atomic: '500', realized_cost_basis_native_atomic: '500', tp1_hit: true });
    let decision = exitDecision(p, { price_usd: 3.1 }, new Date(), { expected_native_atomic: '1550', estimated_net_native_atomic: '1550' });
    assert.equal(sellAmount(p, decision), '100');
    p.remaining_atomic = '400'; p.confirmed_sold_atomic = '600'; p.realized_cost_basis_native_atomic = '600'; p.tp2_hit = true;
    decision = exitDecision(p, { price_usd: 5.1 }, new Date(), { expected_native_atomic: '2040', estimated_net_native_atomic: '2040' });
    assert.equal(sellAmount(p, decision), '100');
  });

  it('detects every configured emergency trigger', () => {
    const base = position('bsc', { last_usable_liquidity_usd: 100_000, last_primary_route_id: 'primary' });
    assert.equal(emergencyExitDecision(base, { liquidity: 69_000 }, { route_id: 'primary', price_impact_percent: 2, estimated_net_native_atomic: '900', reference_native_atomic: '1000' }).reason, 'emergency_liquidity_drop');
    assert.equal(emergencyExitDecision(base, { liquidity: 100_000 }, { route_id: 'primary', price_impact_percent: 16, estimated_net_native_atomic: '900', reference_native_atomic: '1000' }).reason, 'emergency_sell_impact');
    assert.equal(emergencyExitDecision(base, { liquidity: 100_000 }, { route_id: 'primary', price_impact_percent: 2, estimated_net_native_atomic: '640', reference_native_atomic: '1000' }).reason, 'emergency_route_divergence');
    assert.equal(emergencyExitDecision(base, { liquidity: 100_000 }, { route_id: null, price_impact_percent: 2, estimated_net_native_atomic: '900', reference_native_atomic: '1000' }).reason, 'emergency_primary_route_missing');
  });
});
