import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { atomicWrite, classifyPendingIntentEvidence, defaultPaths, main, runLiveDaemonOnce, strategyEntryGate } from '../src/okx_dex_live_daemon.mjs';

const wallet = `0x${'1'.repeat(40)}`;
const token = `0x${'2'.repeat(40)}`;
const pool = `0x${'3'.repeat(40)}`;
const hash = `0x${'4'.repeat(64)}`;
const id = `bsc:${token}`;
const now = new Date('2026-09-09T04:00:00Z');
const stamp = now.toISOString();
const env = { BSC_WALLET_ADDRESS: wallet, OKX_ALLOW_LEGACY_STRATEGY_INPUT: '1' };

test('atomic status writes retry transient Windows sharing violations', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'atomic-write-retry-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const target = path.join(dir, 'status.json');
  fs.writeFileSync(target, '{}');
  const rename = fs.renameSync;
  let attempts = 0;
  t.mock.method(fs, 'renameSync', (source, destination) => {
    attempts += 1;
    if (attempts < 3) {
      const error = new Error('simulated sharing violation');
      error.code = 'EPERM';
      throw error;
    }
    return rename(source, destination);
  });

  atomicWrite(target, { status: 'checking' }, { retryDelayMs: 0 });

  assert.equal(attempts, 3);
  assert.deepEqual(JSON.parse(fs.readFileSync(target, 'utf8')), { status: 'checking' });
});

function fixture(t, { position = false, price = 0.001, state = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'settlement-regression-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const paths = {
    inputPath: path.join(dir, 'input.json'), statePath: path.join(dir, 'state.json'),
    statusPath: path.join(dir, 'status.json'),
  };
  const row = {
    chain: 'bsc', symbol: 'TEST', contract_address: token, pool_address: pool,
    execution_arm: 'first_discovery', signal_at: stamp, first_seen_at: stamp,
    quote_observed_at: stamp, quote_status: 'fresh', price_usd: price,
    mcap: 60000, first_mcap_usd: 50000, markup_from_first: 1.2,
    liquidity: 50000, change_m5: 10, change_h1: 40, sell_count5m: 5,
    execution_candidate_score: 65,
    source_labels: ['OKX', 'DS'], source_count: 2, gmgn_risk_flags: [],
  };
  const held = {
    id, chain: 'bsc', symbol: 'TEST', contract_address: token, pool_address: pool,
    entry_at: stamp, entry_price_usd: 0.001, high_price_usd: 0.001,
    remaining_atomic: '1000', original_atomic: '1000', entry_native_atomic: '1000',
  };
  const initial = { wallet, chain: 'bsc', positions: position ? { [id]: held } : {}, seen: {}, fills: [], closed: [], ...state };
  fs.writeFileSync(paths.inputPath, JSON.stringify({ signals: [row], quotes: [row] }));
  fs.writeFileSync(paths.statePath, JSON.stringify(initial));
  return { paths, initial, read: () => JSON.parse(fs.readFileSync(paths.statePath, 'utf8')),
    run: (deps, live = true) => runLiveDaemonOnce({ ...paths, env, now, live,
      amountNativeAtomic: '1000', deps: { preflight: async () => ({ ready: true, wallet }), ...deps } }) };
}

test('receipt timeout blocks another buy across daemon rounds and keeps the intent', async t => {
  const f = fixture(t);
  let calls = 0;
  const deps = { executeBuy: async () => { calls++; throw new Error('buy_receipt_failed'); } };
  await f.run(deps);
  const second = await f.run(deps);
  assert.equal(calls, 1);
  assert.equal(second.reason, 'execution_reconciliation_required');
  assert.equal(f.read().terminal_pending, 1);
  assert.equal(f.read().execution_intents[id].side, 'buy');
  assert.equal(Object.keys(f.read().positions).length, 0);
});

test('a blocked preflight with no broadcast does not leave a phantom pending intent', async t => {
  const f = fixture(t);
  let calls = 0;
  const deps = { executeBuy: async () => {
    calls++;
    return { state: 'blocked', reason: 'sdk_preflight_failed', live_started: false };
  } };

  const first = await f.run(deps);
  const afterFirst = f.read();
  const second = await f.run(deps);

  assert.equal(first.status, 'buy_blocked');
  assert.equal(afterFirst.terminal_pending, 0);
  assert.equal(afterFirst.execution_intents[id].state, 'not_submitted');
  assert.notEqual(second.reason, 'execution_reconciliation_required');
  assert.equal(calls, 2);
});

test('pending intent evidence classifies reverted, confirmed and unknown outcomes deterministically', () => {
  const intent = { state: 'pending', side: 'buy', transactions: [{ side: 'buy', stage: 'broadcast', tx_hash: hash }] };
  assert.deepEqual(classifyPendingIntentEvidence(intent, { receipt: { transactionHash: hash, status: 0 } }), {
    classification: 'reverted', tx_hash: hash,
  });
  assert.deepEqual(classifyPendingIntentEvidence(intent, {
    receipt: { transactionHash: hash, status: 1 },
    wallet_deltas: { token_atomic: '1000', native_atomic: '-900' },
  }), {
    classification: 'confirmed', tx_hash: hash, token_delta_atomic: '1000', native_delta_atomic: '-900',
  });
  assert.equal(classifyPendingIntentEvidence(intent, {
    receipt: { transactionHash: hash, status: 1 },
  }).classification, 'unknown');
});

test('startup releases a reverted pending intent proven by its receipt', async t => {
  const f = fixture(t, { state: {
    terminal_pending: 1,
    execution_intents: { [id]: { state: 'pending', side: 'buy', transactions: [{ side: 'buy', stage: 'broadcast', tx_hash: hash }] } },
  } });
  fs.writeFileSync(f.paths.inputPath, JSON.stringify({ signals: [], quotes: [] }));
  await f.run({ pendingIntentEvidence: async () => ({ receipt: { transactionHash: hash, status: 0 } }) });
  const state = f.read();
  assert.equal(state.terminal_pending, 0);
  assert.equal(state.execution_intents[id].state, 'reverted');
  assert.equal(state.execution_intents[id].recovery.classification, 'reverted');
});

test('startup records a confirmed pending intent but keeps accounting fail-closed', async t => {
  const f = fixture(t, { state: {
    terminal_pending: 1,
    execution_intents: { [id]: { state: 'pending', side: 'buy', transactions: [{ side: 'buy', stage: 'broadcast', tx_hash: hash }] } },
  } });
  fs.writeFileSync(f.paths.inputPath, JSON.stringify({ signals: [], quotes: [] }));
  const result = await f.run({ pendingIntentEvidence: async () => ({
    receipt: { transactionHash: hash, status: 1 },
    wallet_deltas: { token_atomic: '1000', native_atomic: '-900' },
  }) });
  const state = f.read();
  assert.equal(result.reason, 'execution_reconciliation_required');
  assert.equal(state.terminal_pending, 1);
  assert.equal(state.execution_intents[id].state, 'confirmed_unaccounted');
  assert.equal(state.execution_intents[id].recovery.token_delta_atomic, '1000');
  assert.equal(Object.keys(state.positions).length, 0);
});

test('startup leaves incomplete pending-intent evidence unknown and fail-closed', async t => {
  const f = fixture(t, { state: {
    terminal_pending: 1,
    execution_intents: { [id]: { state: 'pending', side: 'buy', transactions: [{ side: 'buy', stage: 'broadcast', tx_hash: hash }] } },
  } });
  fs.writeFileSync(f.paths.inputPath, JSON.stringify({ signals: [], quotes: [] }));
  const result = await f.run({ pendingIntentEvidence: async () => ({ receipt: { transactionHash: hash, status: 1 } }) });
  assert.equal(result.reason, 'execution_reconciliation_required');
  assert.equal(f.read().execution_intents[id].state, 'pending');
});

test('broadcast identity is durable before receipt waiting fails', async t => {
  const f = fixture(t);
  await f.run({ executeBuy: async ({ onExecutionProgress }) => {
    await onExecutionProgress({ side: 'buy', stage: 'prepared', tx_hash: hash });
    assert.equal(f.read().execution_intents[id].transactions[0].tx_hash, hash);
    await onExecutionProgress({ side: 'buy', stage: 'broadcast', tx_hash: hash });
    throw new Error('buy_receipt_failed');
  } });
  assert.equal(f.read().execution_intents[id].transactions[0].stage, 'broadcast');
});

test('legacy unidentified pending state prevents another submission', async t => {
  const f = fixture(t, { state: { terminal_pending: 1 } });
  let calls = 0;
  const result = await f.run({ executeBuy: async () => { calls++; } });
  assert.equal(result.reason, 'execution_reconciliation_required');
  assert.equal(calls, 0);
});

test('unknown sell quantity preserves holdings and is not retried', async t => {
  const f = fixture(t, { position: true, price: 0.0005 });
  let calls = 0;
  const deps = { executeSell: async () => { calls++; return {
    state: 'sell_settlement_unverified', sell_tx: hash,
  }; } };
  await f.run(deps);
  await f.run(deps);
  assert.equal(calls, 1);
  assert.equal(f.read().positions[id].remaining_atomic, '1000');
  assert.equal(f.read().fills.length, 0);
});

test('a malformed confirmed sell cannot clear its pending intent or fabricate quantity', async t => {
  const f = fixture(t, { position: true, price: 0.0005 });
  await f.run({ executeSell: async () => ({ state: 'sell_confirmed', sell_tx: hash }) });
  assert.equal(f.read().positions[id].remaining_atomic, '1000');
  assert.equal(f.read().terminal_pending, 1);
  assert.equal(f.read().fills.length, 0);
});

for (const price of [0.0005, 0.001]) {
  test(`dry-run never writes a supplied live ledger (price ${price})`, async t => {
    const f = fixture(t, { position: true, price });
    const before = fs.readFileSync(f.paths.statePath, 'utf8');
    await f.run({ executeSell: async () => ({ state: 'dry_run_ready' }) }, false);
    assert.equal(fs.readFileSync(f.paths.statePath, 'utf8'), before);
  });
}

test('confirmed settlement commits the fill and releases its intent together', async t => {
  const f = fixture(t);
  await f.run({ executeBuy: async () => ({ state: 'buy_confirmed', buy_tx: hash, bought_atomic: '1000' }) });
  assert.equal(f.read().terminal_pending, 0);
  assert.equal(f.read().execution_intents[id].state, 'settled');
  assert.equal(f.read().positions[id].remaining_atomic, '1000');
  assert.equal(f.read().wallet, wallet);
});

test('a mined transaction awaiting accounting does not disable a different position exit', async t => {
  const other = `bsc:0x${'7'.repeat(40)}`;
  const f = fixture(t, { position: true, price: 0.0005, state: {
    terminal_pending: 1, execution_intents: { [other]: { state: 'pending', side: 'buy',
      transactions: [{ stage: 'confirmed', side: 'buy', tx_hash: hash }] } },
  } });
  let calls = 0;
  const result = await f.run({ executeSell: async () => {
    calls++; return { state: 'sell_confirmed', sell_tx: hash, sold_atomic: '1000' };
  } });
  assert.equal(calls, 1);
  assert.equal(result.status, 'position_closed');
  assert.equal(f.read().terminal_pending, 1);
});

test('an uncertain account nonce blocks broadcasts but continues valuing other holdings', async t => {
  const other = `bsc:0x${'7'.repeat(40)}`;
  const f = fixture(t, { position: true, price: 0.0005, state: {
    terminal_pending: 1, execution_intents: { [other]: { state: 'pending', side: 'buy',
      transactions: [{ stage: 'broadcast', side: 'buy', tx_hash: hash }] } },
  } });
  let calls = 0;
  const result = await f.run({ executeSell: async () => {
    calls++; return { state: 'sell_confirmed', sell_tx: hash, sold_atomic: '1000' };
  } });
  assert.equal(calls, 0);
  assert.equal(result.reason, 'execution_reconciliation_required');
  assert.equal(f.read().positions[id].remaining_atomic, '1000');
  assert.equal(f.read().positions[id].last_price_usd, 0.0005);
});

test('a wallet-bound ledger cannot be executed using a different configured wallet', async t => {
  const f = fixture(t, { state: { wallet: `0x${'8'.repeat(40)}` } });
  let calls = 0;
  const result = await f.run({ executeBuy: async () => { calls++; } });
  assert.equal(result.reason, 'ledger_wallet_mismatch');
  assert.equal(calls, 0);
});

test('historical positions without a wallet binding require explicit verification', async t => {
  const f = fixture(t, { position: true, state: { wallet: null } });
  const before = fs.readFileSync(f.paths.statePath, 'utf8');
  const result = await f.run({});
  assert.equal(result.reason, 'ledger_wallet_unverified');
  assert.equal(fs.readFileSync(f.paths.statePath, 'utf8'), before);
});

test('default dry-run state and status are separate from both live chains', () => {
  for (const chain of ['bsc', 'robinhood']) {
    assert.notEqual(defaultPaths(chain, false).statePath, defaultPaths(chain, true).statePath);
    assert.notEqual(defaultPaths(chain, false).statusPath, defaultPaths(chain, true).statusPath);
    assert.equal(defaultPaths(chain, false).inputPath, defaultPaths(chain, true).inputPath);
  }
});

test('dry-run main ignores real control commands and leaves the account byte-identical', async t => {
  const f = fixture(t, { position: true });
  const dir = path.dirname(f.paths.statePath);
  const commandPath = path.join(dir, 'okx-dex-sdk-terminal-control.json');
  fs.writeFileSync(commandPath, JSON.stringify({ schema_version: 1, action: 'pause',
    chain: 'bsc', wallet, expected_pid: process.pid, created_at: new Date().toISOString(),
    request_id: '12345678-1234-4234-8234-123456789012' }));
  const before = fs.readFileSync(f.paths.statePath, 'utf8');
  await main(['--once', '--input', f.paths.inputPath, '--state', f.paths.statePath,
    '--status', f.paths.statusPath], env, { log: () => {}, deps: {
    preflight: async () => ({ ready: true, wallet }),
    fetchPositionQuote: async () => ({ price_usd: 0.001, quote_status: 'fresh', quote_at: stamp }),
    executeSell: async () => ({ state: 'dry_run_ready' }),
  } });
  assert.equal(fs.readFileSync(f.paths.statePath, 'utf8'), before);
  assert.equal(JSON.parse(fs.readFileSync(f.paths.statusPath, 'utf8')).terminal_control.request_id, null);
});

test('a pending sell only permits the unrelated position to be sold', async t => {
  const f = fixture(t, { position: true, price: 0.0005, state: {
    terminal_pending: 1, execution_intents: { [id]: { state: 'pending', side: 'sell' } },
  } });
  let calls = 0;
  const result = await f.run({ executeSell: async () => { calls++; } });
  assert.equal(calls, 0);
  assert.equal(result.reason, 'execution_reconciliation_required');
  assert.equal(f.read().positions[id].remaining_atomic, '1000');
});

test('failure to save settlement keeps the previously durable pending marker', async t => {
  const f = fixture(t);
  const rename = fs.renameSync;
  let rejectWrites = false;
  t.mock.method(fs, 'renameSync', (source, destination) => {
    if (rejectWrites && destination === f.paths.statePath) throw new Error('fixture_write_failed');
    return rename(source, destination);
  });
  await f.run({ executeBuy: async () => {
    rejectWrites = true;
    return { state: 'buy_confirmed', buy_tx: hash, bought_atomic: '1000' };
  } });
  assert.equal(f.read().terminal_pending, 1);
  assert.equal(f.read().execution_intents[id].state, 'pending');
  rejectWrites = false;
  let calls = 0;
  await f.run({ executeBuy: async () => { calls++; } });
  assert.equal(calls, 0);
});

test('FDV-only quotes do not borrow an old candidate market cap or silently pass the live gate', () => {
  const candidate = { execution_arm: 'first_discovery', signal_at: stamp, first_seen_at: stamp,
    first_mcap_usd: 50000, markup_from_first: 1, mcap: 50000,
    execution_candidate_score: 65, sell_count5m: 5 };
  const quote = { price_usd: 0.001, liquidity: 50000, quote_status: 'fresh', quote_at: stamp,
    change_m5: 10, change_h1: 40, mcap: null, market_cap: null, fdv: 50000, valuation_type: 'fdv' };
  assert.equal(strategyEntryGate(candidate, quote, now).reason, 'market_cap_unavailable');
  assert.equal(strategyEntryGate(candidate, { ...quote, mcap: 50000 }, now).reason, 'market_cap_unavailable');
  assert.equal(strategyEntryGate(candidate, { ...quote, mcap: 50000, valuation_type: 'market_cap' }, now).accepted, true);
});

test('intent data is flushed to disk before the execution callback is entered', async t => {
  const f = fixture(t);
  const fsync = fs.fsyncSync;
  let flushes = 0;
  t.mock.method(fs, 'fsyncSync', fd => { flushes++; return fsync(fd); });
  let flushedBeforeExecution = false;
  await f.run({ executeBuy: async ({ onExecutionProgress }) => {
    flushedBeforeExecution = flushes > 0;
    const intent = f.read().execution_intents[id];
    assert.equal(intent.contract_address, token);
    assert.equal(intent.pool_address, pool);
    assert.equal(intent.candidate_snapshot.contract_address, token);
    await onExecutionProgress({ side: 'buy', stage: 'prepared', tx_hash: hash,
      wallet_balances_before: { token_atomic: '0', native_atomic: '1000' } });
    throw new Error('fixture_before_broadcast');
  } });
  assert.equal(flushedBeforeExecution, true);
  assert.deepEqual(f.read().execution_intents[id].wallet_balances_before,
    { token_atomic: '0', native_atomic: '1000' });
});

test('a proven pre-signing quote failure can retry without leaving phantom pending transactions', async t => {
  const f = fixture(t);
  let calls = 0;
  const deps = { executeBuy: async () => { calls++; throw new Error('buy_quote_failed'); } };
  await f.run(deps);
  await f.run(deps);
  assert.equal(calls, 2);
  assert.equal(f.read().terminal_pending, 0);
  assert.equal(f.read().execution_intents[id].state, 'not_submitted');
});

test('a confirmed approval followed by sell simulation failure is safely retried', async t => {
  const f = fixture(t, { position: true, price: 0.003 });
  let calls = 0;
  const deps = { executeSell: async ({ onExecutionProgress }) => {
    calls++;
    if (calls === 1) {
      await onExecutionProgress({ side: 'approval', stage: 'confirmed', tx_hash: hash });
      throw new Error('sell_simulation_failed');
    }
    return { state: 'sell_confirmed', sell_tx: hash, sold_atomic: '800' };
  } };
  await f.run(deps);
  const afterFailure = f.read();
  assert.equal(afterFailure.terminal_pending, 0);
  assert.equal(afterFailure.execution_intents[id].state, 'not_submitted');
  const result = await f.run(deps);
  assert.equal(calls, 2);
  assert.equal(result.status, 'position_reduced');
  assert.equal(f.read().positions[id].remaining_atomic, '200');
});

test('daemon startup retries a stale partial exit even after price falls below its trigger', async t => {
  const f = fixture(t, { position: true, price: 0.0015, state: {
    terminal_pending: 1,
    execution_intents: { [id]: {
      state: 'pending', side: 'sell', reason: 'sell_simulation_failed',
      amount_atomic: '800', created_at: stamp,
      transactions: [{ side: 'approval', stage: 'confirmed', tx_hash: hash }],
    } },
  } });
  let calls = 0;
  const result = await f.run({ executeSell: async () => {
    calls++;
    return { state: 'sell_confirmed', sell_tx: hash, sold_atomic: '800' };
  } });
  assert.equal(calls, 1);
  assert.equal(result.status, 'position_reduced');
  assert.equal(f.read().terminal_pending, 0);
  assert.equal(f.read().positions[id].remaining_atomic, '200');
  assert.equal(f.read().positions[id].tp1_hit, true);
  assert.equal(Object.hasOwn(f.read().positions[id], 'retry_exit'), false);
});
