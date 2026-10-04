import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';

import { runLiveDaemonOnce, strategyEntryGate } from '../src/okx_dex_live_daemon.mjs';

const WALLET = '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A'.toLowerCase();
const TOKEN = '0x' + '2'.repeat(40);
const POOL = '0x' + '3'.repeat(40);
const BASE_ENV = {
  BSC_WALLET_ADDRESS: WALLET,
  OKX_LIVE_ENABLED: '1',
  OKX_ALLOW_AUTOMATED_TRADES: '1',
  OKX_ALLOW_LEGACY_STRATEGY_INPUT: '1',
};

function tempPaths() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'okx-sdk-daemon-'));
  return {
    dir,
    inputPath: path.join(dir, 'bsc-execution-input.json'),
    statePath: path.join(dir, 'sdk-live-state.json'),
    statusPath: path.join(dir, 'sdk-live-status.json'),
  };
}

function writeJson(file, payload) {
  if (payload.positions) payload = { wallet: WALLET, chain: 'bsc', ...payload };
  fs.writeFileSync(file, JSON.stringify(payload, null, 2));
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function candidate({
  token = TOKEN,
  pool = POOL,
  symbol = 'BNC4',
  priceUsd = 0.001,
  now = new Date().toISOString(),
  changeH1 = 40,
  sourceCount = 2,
  arm = 'first_discovery',
  mcap = 60_000,
  firstMcap = 50_000,
  score = 65,
} = {}) {
  return {
    chain: 'bsc',
    symbol,
    contract_address: token,
    pool_address: pool,
    execution_arm: arm,
    signal_at: now,
    first_seen_at: now,
    quote_observed_at: now,
    quote_status: 'fresh',
    price_usd: priceUsd,
    mcap,
    first_mcap_usd: firstMcap,
    execution_candidate_score: score,
    markup_from_first: mcap / firstMcap,
    liquidity: 50_000,
    change_m5: 10,
    change_h1: changeH1,
    source_labels: ['OKX', 'DS'],
    source_count: sourceCount,
    gmgn_risk_flags: [],
    sell_count5m: 5,
    watch_status: 'watch',
  };
}

function signalPayload(priceUsd = 0.001) {
  const now = new Date().toISOString();
  return {
    updated_at: now,
    signals: [candidate({ priceUsd, now })],
    quotes: [{
      chain: 'bsc',
      contract_address: TOKEN,
      pool_address: POOL,
      price_usd: priceUsd,
      quote_status: 'fresh',
      quote_at: now,
    }],
  };
}

describe('OKX DEX SDK live daemon', () => {
  it('rejects a new BSC pool until an actual sell has been observed', () => {
    const now = new Date();
    const row = candidate({ now: now.toISOString() });
    const noSellQuote = { ...row, sell_count5m: 0 };
    const result = strategyEntryGate(noSellQuote, noSellQuote, now, 'bsc');

    assert.equal(result.accepted, false);
    assert.equal(result.reason, 'bsc_no_observed_sells');
  });

  it('applies the chronological holdout thresholds to BSC entries', () => {
    const now = new Date();
    const stamp = now.toISOString();
    const cases = [
      [candidate({ now: stamp, score: 71.58 }), 'bsc_score_gate'],
      [{ ...candidate({ now: stamp }), liquidity: 16_068 }, 'bsc_liquidity_gate'],
      [{ ...candidate({ now: stamp }), change_m5: -17.58 }, 'bsc_momentum_gate'],
    ];

    for (const [row, reason] of cases) {
      const result = strategyEntryGate(row, row, now, 'bsc');
      assert.equal(result.accepted, false);
      assert.equal(result.reason, reason);
    }
  });

  it('waits without signing when the strategy input has no BSC candidate', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, { signals: [], quotes: [] });
    let buyCalled = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => {
          buyCalled = true;
        },
      },
    });

    assert.equal(result.status, 'waiting_for_strategy_candidate');
    assert.equal(buyCalled, false);
    assert.equal(readJson(paths.statusPath).status, 'waiting_for_strategy_candidate');
  });

  it('buys one strategy candidate and records a managed SDK position', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.001));

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      amountNativeAtomic: '1000000000000000',
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async (_params) => ({
          state: 'buy_confirmed',
          buy_tx: '0x' + '5'.repeat(64),
          bought_atomic: '1000000',
          order_id: 'order-1',
        }),
      },
    });

    assert.equal(result.status, 'position_opened');
    assert.equal(result.position.symbol, 'BNC4');
    const state = readJson(paths.statePath);
    const positions = Object.values(state.positions);
    assert.equal(positions.length, 1);
    assert.equal(positions[0].token, TOKEN.toLowerCase());
    assert.equal(positions[0].remaining_atomic, '1000000');
    assert.equal(positions[0].entry_native_atomic, '1000000000000000');
  });

  it('converts the configured USD order size to native atomic units at entry time', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.001));
    let submittedAmount = null;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      amountUsd: '5',
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        fetchNativeUsdPrice: async () => 500,
        executeBuy: async ({ amountNativeAtomic }) => {
          submittedAmount = amountNativeAtomic;
          return {
            state: 'buy_confirmed',
            buy_tx: '0x' + '6'.repeat(64),
            bought_atomic: '1000000',
            order_id: 'order-usd-1',
          };
        },
      },
    });

    assert.equal(result.status, 'position_opened');
    assert.equal(submittedAmount, '10000000000000000');
    const position = Object.values(readJson(paths.statePath).positions)[0];
    assert.equal(position.entry_native_atomic, '10000000000000000');
    assert.equal(position.entry_notional_usd, 5);
    assert.equal(position.entry_native_price_usd, 500);
  });

  it('refreshes an aging candidate quote before the entry gate', async () => {
    const paths = tempPaths();
    const now = new Date();
    const payload = signalPayload(0.001);
    payload.signals[0].signal_at = now.toISOString();
    payload.signals[0].first_seen_at = now.toISOString();
    payload.quotes[0].quote_at = new Date(now.getTime() - 31_000).toISOString();
    writeJson(paths.inputPath, payload);
    let bought = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        fetchPositionQuote: async () => ({
          chain: 'bsc', contract_address: TOKEN, pool_address: POOL,
          price_usd: 0.001, mcap: 60_000, liquidity: 50_000,
          change_m5: 10, change_h1: 40, quote_status: 'fresh', quote_at: now.toISOString(),
        }),
        executeBuy: async () => {
          bought = true;
          return { state: 'buy_confirmed', buy_tx: '0x' + '4'.repeat(64), bought_atomic: '1000000' };
        },
      },
    });

    assert.equal(bought, true);
    assert.equal(result.status, 'position_opened');
  });

  it('persists a classified broadcast failure for operator diagnosis', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.001));
    const failure = Object.assign(new Error('buy_broadcast_failed'), {
      detail_reason: 'rpc_gas_limit_rejected',
      rpc_code: -32000,
      rpc_message: 'intrinsic gas too low',
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => { throw failure; },
      },
    });

    assert.equal(result.reason, 'buy_broadcast_failed');
    assert.equal(result.detail_reason, 'rpc_gas_limit_rejected');
    assert.equal(result.rpc_code, -32000);
    assert.equal(result.rpc_message, 'intrinsic gas too low');
    assert.equal(readJson(paths.statusPath).detail_reason, 'rpc_gas_limit_rejected');
  });

  it('does not buy an overextended candidate', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [candidate({ now: now.toISOString(), changeH1: 1_477 })],
      quotes: [],
    });
    let buyCalled = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      now,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => {
          buyCalled = true;
        },
      },
    });

    assert.equal(result.status, 'risk_paused');
    assert.equal(result.reason, 'momentum_gate');
    assert.equal(buyCalled, false);
  });

  it('does not let an expired watch label veto an explicit execution candidate', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [{ ...candidate({ now: now.toISOString() }), watch_status: 'expired', execution_candidate: true }],
      quotes: [],
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      now,
      live: true,
      amountNativeAtomic: '1000000000000000',
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => ({
          state: 'buy_confirmed',
          buy_tx: '0x' + '8'.repeat(64),
          bought_atomic: '1000000',
          order_id: 'order-expired-watch',
        }),
      },
    });

    assert.equal(result.status, 'position_opened');
    assert.equal(result.position.symbol, 'BNC4');
  });

  it('does not treat aggregated labels as two independent sources', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [{ ...candidate({ now: now.toISOString(), sourceCount: 1, arm: 'narrative_breakout', mcap: 600_000 }), chain: 'robinhood' }],
      quotes: [],
    });
    let buyCalled = false;

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths,
      now,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => {
          buyCalled = true;
        },
      },
    });

    assert.equal(result.status, 'risk_paused');
    assert.equal(result.reason, 'narrative_breakout_sources');
    assert.equal(buyCalled, false);
  });

  it('skips a rejected first row and buys the next eligible candidate', async () => {
    const paths = tempPaths();
    const now = new Date();
    const secondToken = '0x' + '4'.repeat(40);
    const secondPool = '0x' + '7'.repeat(64);
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [
        { ...candidate({ now: now.toISOString(), changeH1: 1_477 }), chain: 'robinhood' },
        { ...candidate({ token: secondToken, pool: secondPool, symbol: 'EARLY', now: now.toISOString() }), chain: 'robinhood' },
      ],
      quotes: [],
    });
    let boughtSymbol = null;

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, EXECUTION_CHAIN_NAME: 'robinhood' },
      ...paths,
      now,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async ({ candidate: row }) => {
          boughtSymbol = row.symbol;
          return {
            state: 'buy_confirmed',
            buy_tx: '0x' + '8'.repeat(64),
            bought_atomic: '1000000',
            order_id: 'order-2',
          };
        },
      },
    });

    assert.equal(result.status, 'position_opened');
    assert.equal(boughtSymbol, 'EARLY');
  });

  it('sells an open position through SDK rules when stop loss is reached', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.00075));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc',
          symbol: 'BNC4',
          token: TOKEN.toLowerCase(),
          pool: POOL.toLowerCase(),
          entry_price_usd: 0.001,
          high_price_usd: 0.001,
          remaining_atomic: '1000000',
          original_atomic: '1000000',
          entry_native_atomic: '1000000000000000',
          tp1_hit: false,
          entry_at: new Date().toISOString(),
        },
      },
      closed: [],
      seen: {},
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async (_params) => ({
          state: 'sell_confirmed',
          sell_tx: '0x' + '6'.repeat(64),
          sold_atomic: '1000000',
          native_out_atomic: '700000000000000',
        }),
      },
    });

    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'stop_loss');
    const state = readJson(paths.statePath);
    assert.equal(Object.values(state.positions).length, 0);
    assert.equal(state.closed[0].sell_tx, '0x' + '6'.repeat(64));
    assert.equal(state.fills.length, 1);
    assert.deepEqual(state.fills[0], {
      chain: 'bsc',
      symbol: 'BNC4',
      token: TOKEN.toLowerCase(),
      side: 'sell',
      tx_hash: '0x' + '6'.repeat(64),
      time: state.closed[0].closed_at,
      exit_reason: 'stop_loss',
      fraction: 1,
      amount_atomic: '1000000',
      return_pct: -25,
      cost_basis_native_atomic: '1000000000000000',
    });
  });

  it('uses executable sell value to stop a position before the display price reaches the stop', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.00095));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`, chain: 'bsc', symbol: 'BNC4',
          token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001,
          remaining_atomic: '1000000', original_atomic: '1000000',
          entry_native_spent_atomic: '1000000000000000',
          realized_cost_basis_native_atomic: '0', tp1_hit: false,
          entry_at: new Date().toISOString(),
        },
      },
      closed: [], seen: {},
    });
    let sold = false;
    const preparedSwap = { routerResult: { toTokenAmount: '850000000000000' }, tx: {} };

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, OKX_EXECUTABLE_EXIT_ENABLED: '1' },
      ...paths,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        quoteSellValue: async () => ({
          expected_native_atomic: '850000000000000',
          estimated_net_native_atomic: '700000000000000',
          price_impact_percent: 2,
          prepared_swap: preparedSwap,
        }),
        executeSell: async (params) => {
          assert.equal(params.preparedSwap, preparedSwap);
          sold = true;
          return {
            state: 'sell_confirmed', sell_tx: '0x' + 'f'.repeat(64), sold_atomic: '1000000',
            net_native_received_atomic: '700000000000000',
          };
        },
      },
    });

    assert.equal(sold, true);
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'stop_loss');
    const state = readJson(paths.statePath);
    assert.equal(state.closed[0].return_pct, -30);
    assert.ok(Math.abs(state.closed[0].last_reference_return_pct + 5) < 1e-9);
  });

  it('treats a zero net executable value as a full loss instead of holding forever', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0011));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.004, remaining_atomic: '1000',
          original_atomic: '1000', entry_native_atomic: '1000000',
          entry_native_spent_atomic: '1000000', realized_cost_basis_native_atomic: '0',
          tp1_hit: true, entry_at: new Date().toISOString(),
        },
      },
      closed: [], seen: {},
    });
    let sold = false;

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, OKX_EXECUTABLE_EXIT_ENABLED: '1' }, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        quoteSellValue: async () => ({
          expected_native_atomic: '100',
          estimated_net_native_atomic: '0',
          price_impact_percent: 99,
        }),
        executeSell: async () => {
          sold = true;
          return { state: 'sell_confirmed', sell_tx: '0x' + '5'.repeat(64), sold_atomic: '1000' };
        },
      },
    });

    assert.equal(sold, true);
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'stop_loss');
    const state = readJson(paths.statePath);
    assert.equal(state.closed[0].last_return_pct, -100);
    assert.ok(state.closed[0].last_reference_return_pct > 0);
  });

  it('persists a partial take-profit fill without closing the position', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0021));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001, remaining_atomic: '1000000',
          original_atomic: '1000000', entry_native_atomic: '1000000000000000',
          tp1_hit: false, entry_at: new Date().toISOString(),
        },
      },
      closed: [], seen: {},
    });

    let submittedAmount = null;
    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async ({ tokenAmountAtomic }) => {
          submittedAmount = tokenAmountAtomic;
          return { state: 'sell_confirmed', sell_tx: '0x' + '7'.repeat(64), sold_atomic: tokenAmountAtomic };
        },
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(submittedAmount, '500000');
    assert.equal(result.status, 'position_reduced');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].remaining_atomic, '500000');
    assert.equal(state.closed.length, 0);
    assert.equal(state.fills.length, 1);
    assert.equal(state.fills[0].exit_reason, 'take_profit_1');
    assert.equal(state.fills[0].amount_atomic, '500000');
    assert.equal(state.fills[0].fraction, 0.5);
  });

  it('sells 10 percent of the original position at 3x and leaves 40 percent', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0031));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.0021, remaining_atomic: '500000',
          original_atomic: '1000000', entry_native_atomic: '1000000000000000',
          entry_native_spent_atomic: '1000000000000000', realized_cost_basis_native_atomic: '500000000000000',
          tp1_hit: true, tp2_hit: false, entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });
    let submittedAmount = null;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async ({ tokenAmountAtomic }) => {
          submittedAmount = tokenAmountAtomic;
          return { state: 'sell_confirmed', sell_tx: '0x' + '8'.repeat(64), sold_atomic: tokenAmountAtomic };
        },
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(result.status, 'position_reduced');
    assert.equal(result.exit_reason, 'take_profit_2');
    assert.equal(submittedAmount, '100000');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].remaining_atomic, '400000');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].tp2_hit, true);
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].tp3_hit, false);
    assert.equal(state.fills[0].fraction, 0.1);
  });

  it('defers a partial take profit when its estimated proceeds are not clearly above gas', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0031));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.0021, remaining_atomic: '500000',
          original_atomic: '1000000', entry_native_atomic: '1000000000000000',
          entry_native_spent_atomic: '1000000000000000', realized_cost_basis_native_atomic: '500000000000000',
          tp1_hit: true, tp2_hit: false, tp3_hit: false, entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });
    let sold = false;

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, OKX_EXECUTABLE_EXIT_ENABLED: '1' }, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        quoteSellValue: async () => ({
          expected_native_atomic: '1700000000000000',
          estimated_net_native_atomic: '1500000000000000',
          estimated_gas_native_atomic: '200000000000000',
        }),
        executeSell: async () => { sold = true; },
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(result.status, 'holding_existing_position');
    assert.equal(result.reason, 'partial_exit_cost_too_high');
    assert.equal(sold, false);
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].tp2_hit, false);
  });

  it('sells another 10 percent of the original position at 5x and leaves a 30 percent runner', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0051));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.0031, remaining_atomic: '400000',
          original_atomic: '1000000', entry_native_atomic: '1000000000000000',
          entry_native_spent_atomic: '1000000000000000', realized_cost_basis_native_atomic: '600000000000000',
          tp1_hit: true, tp2_hit: true, tp3_hit: false, entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });
    let submittedAmount = null;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async ({ tokenAmountAtomic }) => {
          submittedAmount = tokenAmountAtomic;
          return { state: 'sell_confirmed', sell_tx: '0x' + '8'.repeat(64), sold_atomic: tokenAmountAtomic };
        },
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(result.status, 'position_reduced');
    assert.equal(result.exit_reason, 'take_profit_3');
    assert.equal(submittedAmount, '100000');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].remaining_atomic, '300000');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].tp3_hit, true);
    assert.equal(state.fills[0].fraction, 0.1);
  });

  it('keeps a runner beyond 90 minutes and exits it after 24 hours', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, signalPayload(0.003));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.003, remaining_atomic: '300000',
          original_atomic: '1000000', entry_native_atomic: '1000000000000000',
          tp1_hit: true, tp2_hit: true, tp3_hit: true,
          entry_at: new Date(now.getTime() - 2 * 60 * 60 * 1000).toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });
    let sold = false;

    const held = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async () => { sold = true; },
      },
    });

    assert.equal(held.status, 'holding_existing_position');
    assert.equal(sold, false);

    const later = new Date(now.getTime() + 23 * 60 * 60 * 1000);
    const exited = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now: later,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        fetchPositionQuote: async () => ({
          chain: 'bsc', contract_address: TOKEN, pool_address: POOL,
          price_usd: 0.003, quote_status: 'fresh', quote_at: later.toISOString(),
        }),
        executeSell: async ({ tokenAmountAtomic }) => ({
          state: 'sell_confirmed', sell_tx: '0x' + '9'.repeat(64), sold_atomic: tokenAmountAtomic,
        }),
      },
    });

    assert.equal(exited.status, 'position_closed');
    assert.equal(exited.exit_reason, 'runner_time_stop');
  });

  it('does not trail out the runner on a drawdown smaller than 40 percent', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0063));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.01, remaining_atomic: '300000',
          original_atomic: '1000000', entry_native_atomic: '1000000000000000',
          tp1_hit: true, tp2_hit: true, tp3_hit: true, entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });
    let sold = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async () => { sold = true; },
      },
    });

    assert.equal(result.status, 'holding_existing_position');
    assert.equal(sold, false);
  });

  it('reconciles a manual partial sale before evaluating the remaining position', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0005));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.004, remaining_atomic: '1000',
          original_atomic: '1000', entry_native_atomic: '1000000',
          entry_native_spent_atomic: '1000000', realized_cost_basis_native_atomic: '0',
          tp1_hit: false, entry_at: new Date().toISOString(),
          retry_exit: { amount_atomic: '800', reason: 'take_profit_1' },
        },
      },
      closed: [], seen: {},
    });
    let submittedAmount = null;

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, OKX_POSITION_BALANCE_RECONCILE: '1' }, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        readWalletTokenBalance: async () => '500',
        executeSell: async ({ tokenAmountAtomic }) => {
          submittedAmount = tokenAmountAtomic;
          return { state: 'sell_confirmed', sell_tx: '0x' + '6'.repeat(64), sold_atomic: '500' };
        },
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(submittedAmount, '500');
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'stop_loss');
    assert.equal(state.closed[0].tp1_hit, true);
    assert.equal(state.closed[0].retry_exit, undefined);
    assert.equal(state.external_reconciliations.length, 1);
    assert.equal(state.external_reconciliations[0].reduced_atomic, '500');
    assert.equal(state.external_reconciliations[0].cost_basis_native_atomic, '500000');
  });

  it('does not mark the first take-profit after a small external reduction', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.001));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001, remaining_atomic: '1000',
          original_atomic: '1000', entry_native_spent_atomic: '1000000',
          realized_cost_basis_native_atomic: '0', tp1_hit: false,
          entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, OKX_POSITION_BALANCE_RECONCILE: '1' }, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        readWalletTokenBalance: async () => '900',
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(result.status, 'holding_existing_position');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].tp1_hit, false);
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].tp2_hit, false);
  });

  it('does not submit an exit when the live token balance cannot be checked', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.0005));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc', symbol: 'BNC4', token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.004, remaining_atomic: '1000',
          original_atomic: '1000', entry_native_atomic: '1000000',
          tp1_hit: true, entry_at: new Date().toISOString(),
        },
      },
      closed: [], seen: {},
    });
    let sold = false;

    const result = await runLiveDaemonOnce({
      env: { ...BASE_ENV, OKX_POSITION_BALANCE_RECONCILE: '1' }, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        readWalletTokenBalance: async () => { throw new Error('rpc_unavailable'); },
        executeSell: async () => { sold = true; },
      },
    });

    assert.equal(sold, false);
    assert.equal(result.status, 'risk_paused');
    assert.equal(result.reason, 'position_balance_unavailable');
  });

  it('refreshes a missing open-position quote before applying exit rules', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, { signals: [], quotes: [] });
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc',
          symbol: 'BNC4',
          token: TOKEN.toLowerCase(),
          contract_address: TOKEN.toLowerCase(),
          pool: POOL.toLowerCase(),
          pool_address: POOL.toLowerCase(),
          entry_price_usd: 0.001,
          high_price_usd: 0.001,
          remaining_atomic: '1000000',
          original_atomic: '1000000',
          entry_native_atomic: '1000000000000000',
          tp1_hit: false,
          entry_at: new Date().toISOString(),
        },
      },
      closed: [],
      seen: {},
    });
    let sold = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        fetchPositionQuote: async () => ({
          chain: 'bsc',
          contract_address: TOKEN,
          pool_address: POOL,
          price_usd: 0.00075,
          quote_status: 'fresh',
          quote_at: new Date().toISOString(),
        }),
        executeSell: async () => {
          sold = true;
          return {
            state: 'sell_confirmed',
            sell_tx: '0x' + '9'.repeat(64),
            sold_atomic: '1000000',
          };
        },
      },
    });

    assert.equal(sold, true);
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'stop_loss');
  });

  it('refreshes a stale open-position quote before applying exit rules', async () => {
    const paths = tempPaths();
    const now = new Date();
    const payload = signalPayload(0.001);
    payload.quotes[0].quote_at = new Date(now.getTime() - 60_000).toISOString();
    writeJson(paths.inputPath, payload);
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`, chain: 'bsc', symbol: 'BNC4',
          token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001,
          remaining_atomic: '1000', original_atomic: '1000',
          entry_native_atomic: '1000000', tp1_hit: false, entry_at: now.toISOString(),
        },
      },
      closed: [], seen: {},
    });
    let refreshed = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        fetchPositionQuote: async () => {
          refreshed = true;
          return { ...payload.quotes[0], price_usd: 0.00075, quote_at: now.toISOString() };
        },
        executeSell: async () => ({ state: 'sell_confirmed', sell_tx: '0x' + 'd'.repeat(64), sold_atomic: '1000' }),
      },
    });

    assert.equal(refreshed, true);
    assert.equal(result.status, 'position_closed');
    assert.equal(result.exit_reason, 'stop_loss');
  });

  it('pauses new entries when an open position cannot be priced', async () => {
    const paths = tempPaths();
    const nextToken = '0x' + '4'.repeat(40);
    const nextPool = '0x' + '5'.repeat(40);
    const now = new Date().toISOString();
    writeJson(paths.inputPath, {
      updated_at: now,
      signals: [candidate({ token: nextToken, pool: nextPool, now })],
      quotes: [{
        chain: 'bsc',
        contract_address: nextToken,
        pool_address: nextPool,
        price_usd: 0.001,
        quote_status: 'fresh',
        quote_at: now,
      }],
    });
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`,
          chain: 'bsc',
          symbol: 'BNC4',
          token: TOKEN.toLowerCase(),
          contract_address: TOKEN.toLowerCase(),
          pool: POOL.toLowerCase(),
          pool_address: POOL.toLowerCase(),
          entry_price_usd: 0.001,
          high_price_usd: 0.001,
          remaining_atomic: '1000000',
          original_atomic: '1000000',
          entry_native_atomic: '1000000000000000',
          tp1_hit: false,
          entry_at: new Date().toISOString(),
        },
      },
      closed: [],
      seen: {},
    });
    let bought = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV,
      ...paths,
      live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        fetchPositionQuote: async () => null,
        executeBuy: async () => {
          bought = true;
        },
      },
    });

    assert.equal(bought, false);
    assert.equal(result.status, 'risk_paused');
    assert.equal(result.reason, 'position_quote_unavailable');
    assert.deepEqual(result.symbols, ['BNC4']);
  });

  it('publishes strategy rejection details while waiting for a candidate', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, {
      signals: [], quotes: [],
      rejections: [{ symbol: 'BURRITO', reject_reason: '1h +201.5% 不在优化区间', score: 69 }],
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: { preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }) },
    });

    assert.equal(result.status, 'waiting_for_strategy_candidate');
    assert.deepEqual(result.candidate_rejections, [
      { symbol: 'BURRITO', reject_reason: '1h +201.5% 不在优化区间', score: 69 },
    ]);
  });

  it('reports a monitored holding when no exit rule or new entry fires', async () => {
    const paths = tempPaths();
    const entryAt = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    writeJson(paths.inputPath, signalPayload(0.001));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`, chain: 'bsc', symbol: 'OCAT',
          token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001,
          remaining_atomic: '1000000', original_atomic: '1000000',
          entry_native_atomic: '1000000000000000', tp1_hit: false, entry_at: entryAt,
        },
      },
      closed: [], fills: [], seen: { [`bsc:${TOKEN.toLowerCase()}`]: entryAt },
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: { preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }) },
    });

    const state = readJson(paths.statePath);
    assert.equal(result.status, 'holding_existing_position');
    assert.equal(result.reason, 'exit_conditions_not_met');
    assert.equal(result.holding_positions[0].symbol, 'OCAT');
    assert.equal(result.holding_positions[0].return_pct, 0);
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].last_price_usd, 0.001);
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].exit_status, 'hold');
  });

  it('limits BSC entries to the validated first-discovery arm', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [candidate({ now: now.toISOString(), arm: 'narrative_breakout', mcap: 600_000 })],
      quotes: [],
    });
    let bought = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => { bought = true; },
      },
    });

    assert.equal(result.status, 'risk_paused');
    assert.equal(result.reason, 'bsc_validated_first_discovery_only');
    assert.equal(bought, false);
  });

  it('limits BSC entries to the validated first market-cap bucket and markup', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [candidate({ now: now.toISOString(), firstMcap: 150_000, mcap: 180_000 })],
      quotes: [],
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now,
      deps: { preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }) },
    });

    assert.equal(result.status, 'risk_paused');
    assert.equal(result.reason, 'bsc_validated_first_mcap');
  });

  it('uses first-discovery market cap rather than current market cap for the BSC bucket', async () => {
    const paths = tempPaths();
    const now = new Date();
    writeJson(paths.inputPath, {
      updated_at: now.toISOString(),
      signals: [candidate({ now: now.toISOString(), firstMcap: 50_000, mcap: 8_000 })],
      quotes: [],
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, now,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => ({ state: 'buy_confirmed', buy_tx: '0x' + 'e'.repeat(64), bought_atomic: '1000' }),
      },
    });

    assert.equal(result.status, 'position_opened');
  });

  it('disables new entries in exit-only mode while keeping the daemon live', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.001));
    let bought = false;

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true, exitOnly: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeBuy: async () => { bought = true; },
      },
    });

    assert.equal(result.status, 'exit_only');
    assert.equal(result.reason, 'new_entries_disabled');
    assert.equal(result.live_started, true);
    assert.equal(bought, false);
  });

  it('records realized native PnL from confirmed settlement amounts', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.00075));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`, chain: 'bsc', symbol: 'BNC4',
          token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001,
          remaining_atomic: '1000000', original_atomic: '1000000',
          entry_native_atomic: '1000000000000000', entry_native_spent_atomic: '1001000000000000',
          tp1_hit: false, entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {}, realized_pnl_native_atomic: '0',
    });

    await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async () => ({
          state: 'sell_confirmed', sell_tx: '0x' + 'a'.repeat(64), sold_atomic: '1000000',
          net_native_received_atomic: '700000000000000', native_received_atomic: '700100000000000',
          gas_native_atomic: '100000000000',
        }),
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(state.realized_pnl_native_atomic, '-301000000000000');
    assert.equal(state.fills[0].cost_basis_native_atomic, '1001000000000000');
    assert.equal(state.fills[0].pnl_native_atomic, '-301000000000000');
    assert.equal(state.closed[0].realized_pnl_native_atomic, '-301000000000000');
  });

  it('reduces a position by the actual confirmed token debit', async () => {
    const paths = tempPaths();
    writeJson(paths.inputPath, signalPayload(0.00075));
    writeJson(paths.statePath, {
      version: 1,
      positions: {
        [`bsc:${TOKEN.toLowerCase()}`]: {
          id: `bsc:${TOKEN.toLowerCase()}`, chain: 'bsc', symbol: 'BNC4',
          token: TOKEN.toLowerCase(), pool: POOL.toLowerCase(),
          entry_price_usd: 0.001, high_price_usd: 0.001,
          remaining_atomic: '1000', original_atomic: '1000',
          entry_native_spent_atomic: '1000000', tp1_hit: false,
          entry_at: new Date().toISOString(),
        },
      },
      closed: [], fills: [], seen: {},
    });

    const result = await runLiveDaemonOnce({
      env: BASE_ENV, ...paths, live: true,
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET, provider: 'okx-dex-sdk' }),
        executeSell: async () => ({ state: 'sell_confirmed', sell_tx: '0x' + 'c'.repeat(64), sold_atomic: '900' }),
      },
    });

    const state = readJson(paths.statePath);
    assert.equal(result.status, 'position_reduced');
    assert.equal(state.positions[`bsc:${TOKEN.toLowerCase()}`].remaining_atomic, '100');
    assert.equal(state.fills[0].amount_atomic, '900');
    assert.equal(state.closed.length, 0);
  });
});
