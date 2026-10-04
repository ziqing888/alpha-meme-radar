import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test } from 'node:test';
import { main, runLiveDaemonOnce } from '../src/okx_dex_live_daemon.mjs';
import { terminalControlPath, acquireDaemonLock } from '../src/terminal_control_protocol.mjs';

const WALLET = '0x' + '1'.repeat(40);
const TOKEN = '0x' + '2'.repeat(40);
const POOL = '0x' + '3'.repeat(40);
const ENV = { BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1',
  OKX_ALLOW_LEGACY_STRATEGY_INPUT: '1' };
const write = (file, data) => fs.writeFileSync(file, JSON.stringify(
  data.positions ? { wallet: WALLET, chain: 'bsc', ...data } : data));
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-daemon-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const inputPath = path.join(dir, 'bsc-execution-input.json');
  const statePath = path.join(dir, 'okx-dex-sdk-live-state.json');
  const statusPath = path.join(dir, 'okx-dex-sdk-live-status.json');
  const controlPath = terminalControlPath(dir, 'bsc');
  const argv = ['--live', '--amount-usd', '5', '--input', inputPath, '--state', statePath, '--status', statusPath];
  write(inputPath, { signals: [], quotes: [] });
  write(statePath, { version: 1, positions: {}, closed: [], fills: [], seen: {} });
  const command = (action, extra = {}) => ({ schema_version: 1, request_id: randomUUID(),
    chain: 'bsc', wallet: WALLET, expected_pid: process.pid, action, created_at: new Date().toISOString(),
    ...(action === 'apply' ? { amount_usd: '2.5' } : {}), ...extra });
  const deps = {
    preflight: async () => ({ ready: true, wallet: WALLET }),
    executeBuy: async () => { throw new Error('unexpected_buy_in_test'); },
    executeSell: async () => { throw new Error('unexpected_sell_in_test'); },
    fetch: async () => { throw new Error('unexpected_network_in_test'); },
    fetchNativeUsdPrice: async () => 500,
    pendingTransactions: async () => 0,
  };
  return { dir, inputPath, statePath, statusPath, controlPath, argv, command, deps };
}

function signal(price = 0.001) {
  const now = new Date().toISOString();
  return { chain: 'bsc', symbol: 'TEST', contract_address: TOKEN, pool_address: POOL,
    execution_arm: 'first_discovery', signal_at: now, first_seen_at: now, quote_observed_at: now,
    quote_status: 'fresh', price_usd: price, mcap: 60000, first_mcap_usd: 50000,
    markup_from_first: 1.2, liquidity: 50000, change_m5: 10, change_h1: 40,
    execution_candidate_score: 65, sell_count5m: 5,
    source_labels: ['OKX', 'DS'], source_count: 2, gmgn_risk_flags: [], watch_status: 'watch' };
}

test('startup publishes ready support before preflight; every final status keeps the receipt', async t => {
  const f = fixture(t);
  let checked = false;
  await main([...f.argv, '--once'], ENV, { deps: { ...f.deps, preflight: async () => {
    const ack = read(f.statusPath).terminal_control;
    assert.equal(ack.supported, true);
    assert.equal(ack.state, 'ready');
    assert.equal(ack.pid, process.pid);
    assert.equal(ack.wallet, WALLET);
    assert.equal(ack.amount_usd, '5');
    assert.equal(ack.request_id, null);
    checked = true;
    return { ready: false, reason: 'fixture_blocked' };
  } }, log: () => {} });
  assert.equal(checked, true);
  assert.equal(read(f.statusPath).terminal_control.state, 'ready');
});

test('apply is acknowledged before buying and changes only the next entry size', async t => {
  const f = fixture(t);
  write(f.inputPath, { signals: [signal()], quotes: [] });
  const request = f.command('apply');
  write(f.controlPath, request);
  let amount;
  const code = await main([...f.argv, '--once'], ENV, { deps: { ...f.deps,
    executeBuy: async params => {
      amount = params.amountNativeAtomic;
      assert.equal(read(f.statusPath).terminal_control.request_id, request.request_id);
      assert.equal(read(f.statusPath).terminal_control.state, 'applied');
      assert.equal(read(f.statePath).terminal_pending, 1);
      return { state: 'buy_confirmed', bought_atomic: '1000', buy_tx: '0x' + 'a'.repeat(64) };
    },
  }, log: () => {} });
  assert.equal(code, 0);
  assert.equal(amount, '5000000000000000');
  assert.equal(read(f.statePath).terminal_pending, 0);
  assert.equal(read(f.statusPath).terminal_control.amount_usd, '2.5');
});

test('pause still runs exits and preserves the receipt on a position-close status', async t => {
  const f = fixture(t);
  const now = new Date().toISOString();
  write(f.statePath, { version: 1, positions: { token: { token: TOKEN, chain: 'bsc', pool: POOL,
    symbol: 'TEST', entry_price_usd: 0.001, remaining_atomic: '1000', original_atomic: '1000',
    entry_native_atomic: '1000000', entry_at: now } }, closed: [], fills: [], seen: {} });
  write(f.controlPath, f.command('pause'));
  await main([...f.argv, '--once'], ENV, { deps: { ...f.deps,
    fetchPositionQuote: async () => ({ price_usd: 0.00075, quote_at: now, quote_status: 'fresh' }),
    executeSell: async () => ({ state: 'sell_confirmed', sold_atomic: '1000', sell_tx: '0x' + 'b'.repeat(64) }),
  }, log: () => {} });
  const status = read(f.statusPath);
  assert.equal(status.status, 'position_closed');
  assert.equal(status.exit_reason, 'stop_loss');
  assert.equal(status.terminal_control.exit_only, true);
  assert.equal(status.terminal_control.action, 'pause');
});

test('main remembers configuration across rounds, resumes, then stops without another trade round', async t => {
  const f = fixture(t);
  write(f.controlPath, f.command('pause'));
  let rounds = 0;
  let sleeps = 0;
  const observed = [];
  const code = await main(f.argv, ENV, { deps: f.deps, log: () => {},
    runOnce: async options => {
      rounds++;
      observed.push([options.amountUsd, options.exitOnly]);
      return runLiveDaemonOnce(options);
    },
    sleep: async () => {
      sleeps++;
      if (sleeps === 1) write(f.controlPath, f.command('apply', { amount_usd: '7' }));
      else if (sleeps === 2) write(f.controlPath, f.command('resume'));
      else if (sleeps === 3) write(f.controlPath, f.command('stop'));
      else throw new Error('loop_failed_to_stop');
    },
  });
  assert.equal(code, 0);
  assert.equal(rounds, 3);
  assert.deepEqual(observed, [['5', true], ['7', true], ['7', false]]);
  assert.equal(read(f.statusPath).status, 'stopped');
  assert.equal(read(f.statusPath).terminal_control.reason, 'stopped');
});

test('uncertain execution is persisted and blocks stop even if the position ledger is empty', async t => {
  const f = fixture(t);
  write(f.inputPath, { signals: [signal()], quotes: [] });
  const result = await runLiveDaemonOnce({ env: ENV, ...f, live: true, amountUsd: '5',
    deps: { ...f.deps, executeBuy: async () => { throw new Error('buy_receipt_failed'); } } });
  assert.equal(result.status, 'blocked');
  assert.equal(read(f.statePath).terminal_pending, 1);
  write(f.inputPath, { signals: [], quotes: [] });
  write(f.controlPath, f.command('stop'));
  await main([...f.argv, '--once'], ENV, { deps: f.deps, log: () => {} });
  assert.equal(read(f.statusPath).terminal_control.state, 'rejected');
  assert.equal(read(f.statusPath).terminal_control.reason, 'pending_transactions');
});

test('malformed commands and mismatched PID preserve CLI config without leaking command data', async t => {
  const f = fixture(t);
  fs.writeFileSync(f.controlPath, '{SECRET');
  await main([...f.argv, '--once'], ENV, { deps: f.deps, log: () => {} });
  let ack = read(f.statusPath).terminal_control;
  assert.equal(ack.state, 'rejected');
  assert.equal(ack.reason, 'invalid_json');
  assert.equal(ack.amount_usd, '5');
  assert.equal(JSON.stringify(ack).includes('SECRET'), false);
  write(f.controlPath, f.command('apply', { expected_pid: process.pid + 1 }));
  await main([...f.argv, '--once'], ENV, { deps: f.deps, log: () => {} });
  ack = read(f.statusPath).terminal_control;
  assert.equal(ack.reason, 'pid_mismatch');
  assert.equal(ack.amount_usd, '5');
});

test('main singleton rejects a second owner without overwriting the first status and releases on exit', async t => {
  const f = fixture(t);
  const lockPath = path.join(f.dir, 'okx-dex-sdk-daemon.lock');
  const release = acquireDaemonLock(lockPath, { chain: 'bsc', wallet: WALLET });
  const sentinel = { status: 'existing_owner' };
  write(f.statusPath, sentinel);
  await assert.rejects(main([...f.argv, '--once'], ENV, { deps: f.deps, log: () => {} }), /daemon_already_locked/);
  assert.deepEqual(read(f.statusPath), sentinel);
  release();
  await main([...f.argv, '--once'], ENV, { deps: f.deps, log: () => {} });
  assert.equal(fs.existsSync(lockPath), false);
});

test('daemon lock reclaims a matching owner only after its PID is proven absent', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpha-lock-'));
  const lockPath = path.join(dir, 'daemon.lock');
  fs.writeFileSync(lockPath, JSON.stringify({
    lock_id: 'old-lock', pid: 424242, chain: 'bsc', wallet: WALLET,
    created_at: new Date(Date.now() - 60_000).toISOString(),
  }));

  const release = acquireDaemonLock(lockPath, {
    chain: 'bsc', wallet: WALLET, pid: 12345, isProcessAlive: () => false,
  });

  assert.equal(JSON.parse(fs.readFileSync(lockPath, 'utf8')).pid, 12345);
  release();
  assert.equal(fs.existsSync(lockPath), false);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('long-round heartbeat refreshes current receipt without consuming commands until the next boundary', async t => {
  const f = fixture(t);
  const apply = f.command('apply', { amount_usd: '7' });
  const pause = f.command('pause');
  write(f.controlPath, apply);
  let timer;
  let rounds = 0;
  const timers = [];
  await main(f.argv, ENV, { deps: f.deps, log: () => {},
    setInterval: (callback, ms) => {
      assert.equal(ms, 10_000);
      timer = { tick: callback, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearInterval: handle => { handle.cleared = true; },
    runOnce: async options => {
      rounds++;
      assert.ok(timer, 'heartbeat must be installed while runOnce is pending');
      assert.equal(options.amountUsd, '7');
      assert.equal(options.exitOnly, rounds === 2);
      write(f.controlPath, rounds === 1 ? pause : f.command('stop'));
      const old = read(f.statusPath);
      old.terminal_control.updated_at = '2000-01-01T00:00:00.000Z';
      write(f.statusPath, old);
      timer.tick();
      const heartbeat = read(f.statusPath);
      assert.equal(heartbeat.status, 'checking');
      assert.equal(heartbeat.terminal_control.supported, true);
      assert.equal(heartbeat.terminal_control.request_id, rounds === 1 ? apply.request_id : pause.request_id);
      assert.equal(heartbeat.terminal_control.amount_usd, '7');
      assert.equal(heartbeat.terminal_control.exit_only, rounds === 2);
      assert.ok(Math.abs(Date.now() - Date.parse(heartbeat.terminal_control.updated_at)) < 1000);
      return runLiveDaemonOnce(options);
    },
    sleep: async () => {
      assert.equal(timer.cleared, true, 'heartbeat must be cleared before inter-round sleep');
      const completed = read(f.statusPath);
      timer.tick();
      assert.deepEqual(read(f.statusPath), completed, 'a stale callback cannot overwrite the completed status');
      assert.ok(rounds <= 2);
    },
  });
  assert.equal(rounds, 2);
  assert.equal(timers.length, 2);
  assert.ok(timers.every(handle => handle.cleared));
  const stopped = read(f.statusPath);
  timers.forEach(handle => handle.tick());
  assert.equal(stopped.status, 'stopped');
  assert.deepEqual(read(f.statusPath), stopped);
});

test('runOnce rejection clears its heartbeat and releases the singleton lock', async t => {
  const f = fixture(t);
  let callback;
  let cleared = false;
  const handle = {};
  await assert.rejects(main(f.argv, ENV, { deps: f.deps, log: () => {},
    setInterval: tick => { callback = tick; return handle; },
    clearInterval: value => { assert.equal(value, handle); cleared = true; },
    runOnce: async () => { throw new Error('fixture_round_failed'); },
  }), /fixture_round_failed/);
  assert.equal(cleared, true);
  assert.equal(fs.existsSync(path.join(f.dir, 'okx-dex-sdk-daemon.lock')), false);
  const finalStatus = read(f.statusPath);
  callback();
  assert.deepEqual(read(f.statusPath), finalStatus);
});

test('a heartbeat write failure does not interrupt the pending settlement round', async t => {
  const f = fixture(t);
  let tick;
  let cleared = false;
  await main([...f.argv, '--once'], ENV, { deps: f.deps, log: () => {},
    setInterval: callback => { tick = callback; return 1; },
    clearInterval: () => { cleared = true; },
    runOnce: async options => {
      fs.unlinkSync(f.statusPath);
      fs.mkdirSync(f.statusPath);
      assert.doesNotThrow(() => tick());
      fs.rmdirSync(f.statusPath);
      return runLiveDaemonOnce(options);
    },
  });
  assert.equal(cleared, true);
  assert.equal(read(f.statusPath).status, 'waiting_for_strategy_candidate');
});
