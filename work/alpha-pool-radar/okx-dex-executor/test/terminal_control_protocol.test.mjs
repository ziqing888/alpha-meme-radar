import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { TerminalControlProtocol, terminalControlPath, readTerminalCommand,
  fetchPendingTransactions } from '../src/terminal_control_protocol.mjs';

const WALLET = '0x' + '1'.repeat(40);
const NOW = new Date('2026-09-09T04:00:00.000Z');
const context = () => ({ now: NOW, account: { version: 1, positions: {} }, pendingTransactions: async () => 0 });
const protocol = (extra = {}) => new TerminalControlProtocol({ chain: 'bsc', wallet: WALLET,
  pid: 123, startedAt: new Date(NOW - 240_000), amountUsd: '5', exitOnly: false, ...extra });
const command = (action = 'apply', extra = {}) => ({ schema_version: 1, request_id: randomUUID(),
  chain: 'bsc', wallet: WALLET, action, ...(action === 'apply' ? { amount_usd: '2.5' } : {}),
  created_at: NOW.toISOString(), expected_pid: 123, ...extra });

test('apply changes only next-entry amount; pause/resume retain amount and never arm live', async () => {
  const p = protocol({ amountNativeAtomic: '1000' });
  let receipt = await p.consume(command(), context());
  assert.equal(receipt.state, 'applied');
  assert.equal(receipt.amount_usd, '2.5');
  assert.equal(receipt.amount_native_atomic, null);
  assert.equal(receipt.exit_only, false);
  assert.equal(receipt.pid, 123);
  receipt = await p.consume(command('pause'), context());
  assert.equal(receipt.exit_only, true);
  assert.equal(receipt.amount_usd, '2.5');
  receipt = await p.consume(command('resume'), context());
  assert.equal(receipt.exit_only, false);
  assert.equal(receipt.amount_usd, '2.5');
  assert.equal(Object.hasOwn(p.options(), 'live'), false);
});

test('invalid commands cannot replace effective options and diagnostics contain no arbitrary data', async () => {
  for (const patch of [
    { schema_version: 2 }, { request_id: 'SECRET-DIAGNOSTIC' }, { chain: 'robinhood' },
    { wallet: '0x' + '2'.repeat(40) }, { expected_pid: 124 }, { expected_pid: '123' },
    { created_at: new Date(NOW - 120_001).toISOString() },
    { created_at: new Date(+NOW + 5_001).toISOString() }, { created_at: '2026-09-09' },
    { amount_usd: 5 }, { amount_usd: '0.09' }, { amount_usd: '100.000000000000000001' },
    { amount_usd: 'NaN' }, { amount_usd: '1e1' }, { amount_usd: '' },
    { action: 'SECRET-DIAGNOSTIC' }, { secret: 'SECRET-DIAGNOSTIC' },
  ]) {
    const p = protocol();
    const before = p.options();
    const receipt = await p.consume(command('apply', patch), context());
    assert.equal(receipt.state, 'rejected', JSON.stringify(patch));
    assert.deepEqual(p.options(), before);
    assert.equal(JSON.stringify(receipt).includes('SECRET-DIAGNOSTIC'), false);
  }
});

test('accepts inclusive amount boundaries and rejects amounts attached to other actions', async () => {
  for (const amount_usd of ['0.1', '100', '100.000000000000000000']) {
    const p = protocol();
    assert.equal((await p.consume(command('apply', { amount_usd }), context())).state, 'applied');
  }
  for (const action of ['pause', 'resume', 'stop']) {
    assert.equal((await protocol().consume(command(action, { amount_usd: '3' }), context())).state, 'rejected');
  }
});

test('deduplicates old UUIDs even after a newer command and never reapplies their amount', async () => {
  const p = protocol();
  const first = command();
  await p.consume(first, context());
  await p.consume(command('apply', { amount_usd: '8' }), context());
  const again = await p.consume(first, { ...context(), now: new Date(+NOW + 180_000) });
  assert.equal(again.state, 'applied');
  assert.equal(again.duplicate, true);
  assert.equal(again.amount_usd, '8');
  assert.equal(p.options().amountUsd, '8');
  const conflict = await p.consume({ ...first, amount_usd: '9' }, context());
  assert.equal(conflict.reason, 'request_id_conflict');
  assert.equal(p.options().amountUsd, '8');
});

test('PID reuse and requests preceding this process cannot affect a new daemon', async () => {
  const p = protocol({ startedAt: new Date(+NOW + 1) });
  assert.equal((await p.consume(command(), context())).reason, 'request_predates_process');
});

test('accepts Python datetime.isoformat microseconds with an explicit UTC offset', async () => {
  const p = protocol({ startedAt: new Date('2026-09-09T03:31:00.000Z') });
  const receipt = await p.consume(command('apply', {
    created_at: '2026-09-09T03:32:01.123456+00:00', amount_usd: '3.5',
  }), { ...context(), now: new Date('2026-09-09T03:32:02.000Z') });
  assert.equal(receipt.state, 'applied');
  assert.equal(receipt.amount_usd, '3.5');
  assert.equal(receipt.reason, 'amount_applied');
});

test('TTL accepts the full 120-second window and rejects the next millisecond', async () => {
  for (const [age, state] of [[120_000, 'applied'], [120_001, 'rejected']]) {
    const p = protocol();
    const receipt = await p.consume(command('pause', {
      created_at: new Date(+NOW - age).toISOString(),
    }), context());
    assert.equal(receipt.state, state);
  }
});

test('stop requires an empty valid ledger, zero local pending and verified zero remote pending', async () => {
  for (const [account, reason] of [
    [{ version: 1, positions: { token: {} } }, 'open_positions'],
    [{ version: 1, positions: {}, terminal_pending: 1 }, 'pending_transactions'],
    [{ version: 1, positions: {}, pending_orders: 1 }, 'pending_transactions'],
    [{ version: 1, positions: {}, pending: { tx: {} } }, 'pending_transactions'],
    [{ version: 1, positions: {}, orders: { tx: { status: 'unknown' } } }, 'pending_transactions'],
    [{ version: 1, positions: {}, terminal_pending: -1 }, 'account_state_unavailable'],
    [{}, 'account_state_unavailable'], [null, 'account_state_unavailable'],
  ]) {
    const p = protocol();
    const receipt = await p.consume(command('stop'), { ...context(), account });
    assert.equal(receipt.reason, reason);
    assert.equal(p.stopRequested, false);
    assert.deepEqual(p.options(), { amountUsd: '5', amountNativeAtomic: null, exitOnly: false });
  }
  for (const remote of [1, null, false, -1]) {
    const p = protocol();
    assert.equal((await p.consume(command('stop'), { ...context(), pendingTransactions: async () => remote })).state, 'rejected');
    assert.equal(p.stopRequested, false);
  }
  const p = protocol();
  const receipt = await p.consume(command('stop'), context());
  assert.equal(receipt.state, 'applied');
  assert.equal(receipt.reason, 'stopped');
  assert.equal(p.stopRequested, true);
});

test('rejected stop remains rejected for the same UUID after holdings disappear', async () => {
  const p = protocol();
  const stop = command('stop');
  await p.consume(stop, { ...context(), account: { version: 1, positions: { token: {} } } });
  const receipt = await p.consume(stop, context());
  assert.equal(receipt.state, 'rejected');
  assert.equal(receipt.reason, 'open_positions');
  assert.equal(receipt.duplicate, true);
  assert.equal(p.stopRequested, false);
});

test('unavailable RPC rejects stop without leaking exceptions or switching to pause', async () => {
  const p = protocol();
  const receipt = await p.consume(command('stop'), { ...context(), pendingTransactions: async () => {
    throw new Error('SECRET-DIAGNOSTIC');
  } });
  assert.equal(receipt.reason, 'pending_state_unavailable');
  assert.equal(receipt.exit_only, false);
  assert.equal(JSON.stringify(receipt).includes('SECRET-DIAGNOSTIC'), false);
});

test('control paths match the producer protocol and reads are bounded/no-write', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'terminal-protocol-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  assert.equal(terminalControlPath(dir, 'bsc'), path.join(dir, 'okx-dex-sdk-terminal-control.json'));
  assert.equal(terminalControlPath(dir, 'robinhood'), path.join(dir, 'okx-dex-sdk-robinhood-terminal-control.json'));
  const file = terminalControlPath(dir, 'bsc');
  assert.equal(readTerminalCommand(file), null);
  fs.writeFileSync(file, '\ufeff' + JSON.stringify(command()));
  const before = fs.readFileSync(file);
  assert.equal(readTerminalCommand(file).action, 'apply');
  assert.deepEqual(fs.readFileSync(file), before);
  fs.writeFileSync(file, ' '.repeat(8193));
  assert.throws(() => readTerminalCommand(file), /command_too_large/);
  fs.writeFileSync(file, '{');
  assert.throws(() => readTerminalCommand(file), /invalid_json/);
});

test('RPC pending check uses only read methods, validates chain and nonce order', async () => {
  const env = { BSC_RPC_URL: 'https://fixture.invalid' };
  const fetch = async (_url, options) => {
    const requests = JSON.parse(options.body);
    assert.deepEqual(requests.map(r => r.method), ['eth_chainId', 'eth_getTransactionCount', 'eth_getTransactionCount']);
    assert.deepEqual(requests[1].params, [WALLET, 'latest']);
    assert.deepEqual(requests[2].params, [WALLET, 'pending']);
    return { ok: true, json: async () => [
      { jsonrpc: '2.0', id: 1, result: '0x38' },
      { jsonrpc: '2.0', id: 2, result: '0x10' },
      { jsonrpc: '2.0', id: 3, result: '0x12' },
    ] };
  };
  assert.equal(await fetchPendingTransactions({ env, chain: 'bsc', wallet: WALLET, fetch }), 2);
  await assert.rejects(fetchPendingTransactions({ env, chain: 'robinhood', wallet: WALLET, fetch }), /pending_state_unavailable/);
});
