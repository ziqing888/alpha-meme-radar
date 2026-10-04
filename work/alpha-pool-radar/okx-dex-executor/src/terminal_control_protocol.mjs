/**
 * Single-slot local mailbox. Producers atomically replace the command JSON and
 * wait for its UUID receipt before writing another command. No process spawning,
 * signing, configuration-file writes, or transaction submission is implemented.
 * Commands expire after 120s, tolerate at most 5s future clock skew, and must not
 * predate this process. UUIDs (including rejections) are remembered for its life.
 * A retry returns the original decision with CURRENT effective settings; a
 * changed payload under the same UUID is rejected. Use a new UUID to retry a
 * rejected stop after reconciliation. Settings are process-local, not a change
 * to launcher defaults. stop never liquidates and never changes entry settings.
 */
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export const COMMAND_TTL_MS = 120_000;
const FUTURE_SKEW_MS = 5_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const ACTIONS = new Set(['apply', 'pause', 'resume', 'stop']);
const CHAINS = { bsc: 56n, robinhood: 4663n };
const FIELDS = new Set(['schema_version', 'request_id', 'chain', 'wallet', 'action', 'amount_usd', 'created_at', 'expected_pid']);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const validWallet = value => typeof value === 'string' && ADDRESS.test(value) && !/^0x0{40}$/i.test(value);
const iso = value => new Date(value).toISOString();

function usdAmount(value) {
  if (typeof value !== 'string' || !/^\d{1,3}(?:\.\d{1,18})?$/.test(value)) return null;
  const [whole, fraction = ''] = value.split('.');
  const units = BigInt(whole) * 10n ** 18n + BigInt(fraction.padEnd(18, '0'));
  return units >= 10n ** 17n && units <= 100n * 10n ** 18n ? value : null;
}

export function terminalControlPath(outputs, chain) {
  if (!Object.hasOwn(CHAINS, chain)) throw new Error('unsupported_chain');
  return path.join(outputs, `okx-dex-sdk${chain === 'bsc' ? '' : `-${chain}`}-terminal-control.json`);
}

function processAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return true;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error?.code !== 'ESRCH'; }
}

function reclaimStaleLock(file, expected, isAlive) {
  let raw;
  let prior;
  try {
    if (fs.statSync(file).size > 4096) return false;
    raw = fs.readFileSync(file, 'utf8');
    prior = JSON.parse(raw);
  } catch { return false; }
  if (!object(prior) || prior.chain !== expected.chain
      || String(prior.wallet || '').toLowerCase() !== String(expected.wallet || '').toLowerCase()
      || isAlive(prior.pid)) return false;
  try {
    if (fs.readFileSync(file, 'utf8') !== raw) return false;
    fs.unlinkSync(file);
    return true;
  } catch { return false; }
}

/** Exclusive main-loop ownership. A crash lock is reclaimed only when its
 * chain and wallet match and the recorded PID is proven absent. */
export function acquireDaemonLock(file, { chain, wallet, pid = process.pid, isProcessAlive = processAlive }) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const owner = { lock_id: randomUUID(), pid, chain, wallet, created_at: new Date().toISOString() };
  let fd;
  try { fd = fs.openSync(file, 'wx', 0o600); }
  catch (error) {
    if (error.code !== 'EEXIST' || !reclaimStaleLock(file, owner, isProcessAlive)) {
      throw new Error(error.code === 'EEXIST' ? 'daemon_already_locked' : 'daemon_lock_unavailable');
    }
    try { fd = fs.openSync(file, 'wx', 0o600); }
    catch { throw new Error('daemon_already_locked'); }
  }
  try {
    fs.writeFileSync(fd, JSON.stringify(owner));
    fs.fsyncSync(fd);
  } catch {
    fs.closeSync(fd);
    fs.unlinkSync(file);
    throw new Error('daemon_lock_unavailable');
  }
  fs.closeSync(fd);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    try {
      if (JSON.parse(fs.readFileSync(file, 'utf8')).lock_id === owner.lock_id) fs.unlinkSync(file);
    } catch { /* A missing/replaced lock is not ours to remove. */ }
  };
}

export function readTerminalCommand(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    if (!fs.fstatSync(fd).isFile()) throw new Error('command_unreadable');
    const buffer = Buffer.alloc(8193);
    const length = fs.readSync(fd, buffer, 0, buffer.length, 0);
    if (length > 8192) throw new Error('command_too_large');
    try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length))); }
    catch { throw new Error('invalid_json'); }
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    const code = ['command_too_large', 'invalid_json'].includes(error.message) ? error.message : 'command_unreadable';
    throw new Error(code);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function localStopReason(account) {
  if (!object(account) || account.version !== 1 || !object(account.positions)) return 'account_state_unavailable';
  if (Object.keys(account.positions).length) return 'open_positions';
  for (const key of ['terminal_pending', 'pending_orders', 'pending']) {
    const value = account[key];
    if (value === undefined) continue;
    const count = Array.isArray(value) ? value.length : object(value) ? Object.keys(value).length : value;
    if (!Number.isSafeInteger(count) || count < 0) return 'account_state_unavailable';
    if (count > 0) return 'pending_transactions';
  }
  if (account.orders !== undefined) {
    if (!object(account.orders)) return 'account_state_unavailable';
    for (const order of Object.values(account.orders)) {
      if (!object(order) || !['filled', 'failed', 'rejected', 'confirmed', 'cancelled'].includes(order.status)
          || order.fill_anomaly) return 'pending_transactions';
    }
  }
  return null;
}

/** Read-only nonce check, invoked only for a valid stop with no local positions. */
export async function fetchPendingTransactions({ env, chain, wallet, fetch: fetchImpl = globalThis.fetch }) {
  try {
    if (!Object.hasOwn(CHAINS, chain) || !validWallet(wallet)) throw new Error();
    const url = new URL(env.EVM_RPC_URL || env.BSC_RPC_URL);
    if (!['https:', 'http:'].includes(url.protocol)) throw new Error();
    const response = await fetchImpl(url.href, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: AbortSignal.timeout(6_000),
      body: JSON.stringify([
        { jsonrpc: '2.0', id: 1, method: 'eth_chainId', params: [] },
        { jsonrpc: '2.0', id: 2, method: 'eth_getTransactionCount', params: [wallet, 'latest'] },
        { jsonrpc: '2.0', id: 3, method: 'eth_getTransactionCount', params: [wallet, 'pending'] },
      ]),
    });
    if (!response.ok) throw new Error();
    const rows = await response.json();
    if (!Array.isArray(rows) || rows.length !== 3) throw new Error();
    const values = new Map();
    for (const row of rows) {
      if (!object(row) || row.error || ![1, 2, 3].includes(row.id) || values.has(row.id)
          || typeof row.result !== 'string' || !/^0x[0-9a-f]{1,64}$/i.test(row.result)) throw new Error();
      values.set(row.id, BigInt(row.result));
    }
    const count = values.get(3) - values.get(2);
    if (values.get(1) !== CHAINS[chain] || count < 0n || count > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error();
    return Number(count);
  } catch { throw new Error('pending_state_unavailable'); }
}

export class TerminalControlProtocol {
  constructor({ chain, wallet, pid = process.pid, startedAt = new Date(Date.now() - process.uptime() * 1000),
    amountUsd = null, amountNativeAtomic = null, exitOnly = false }) {
    this.chain = chain;
    this.wallet = validWallet(wallet) ? wallet.toLowerCase() : null;
    this.pid = pid;
    this.startedAt = +new Date(startedAt);
    if (amountUsd !== null && amountUsd !== '' && usdAmount(amountUsd) === null) throw new Error('invalid_usd_order_size');
    this.amountUsd = usdAmount(amountUsd);
    this.amountNativeAtomic = this.amountUsd ? null : amountNativeAtomic;
    this.exitOnly = exitOnly === true;
    this.stopRequested = false;
    this.seen = new Map();
    this.last = { request_id: null, action: null, state: 'ready' };
  }

  options() {
    return { amountUsd: this.amountUsd, amountNativeAtomic: this.amountNativeAtomic, exitOnly: this.exitOnly };
  }

  receipt(now = new Date()) {
    return { supported: true, pid: this.pid, chain: this.chain, wallet: this.wallet,
      amount_usd: this.amountUsd, amount_native_atomic: this.amountNativeAtomic, exit_only: this.exitOnly,
      ...this.last, updated_at: iso(now) };
  }

  rejectRead(reason, now = new Date()) {
    this.last = { request_id: null, action: null, state: 'rejected',
      reason: ['command_too_large', 'invalid_json', 'command_unreadable'].includes(reason) ? reason : 'command_unreadable' };
    return this.receipt(now);
  }

  async consume(command, { now = new Date(), account, pendingTransactions } = {}) {
    if (command === null) return this.receipt(now);
    const id = typeof command?.request_id === 'string' && UUID.test(command.request_id) ? command.request_id.toLowerCase() : null;
    const action = ACTIONS.has(command?.action) ? command.action : null;
    const fingerprint = object(command) ? JSON.stringify(Object.keys(command).sort().map(key => [key, command[key]])) : '';
    const previous = id && this.seen.get(id);
    if (previous) {
      this.last = previous.fingerprint === fingerprint ? { ...previous.decision, duplicate: true }
        : { request_id: id, action, state: 'rejected', reason: 'request_id_conflict' };
      return this.receipt(now);
    }
    let reason = null;
    if (!object(command) || Object.keys(command).some(key => !FIELDS.has(key)) || command.schema_version !== 1 || !id || !action) {
      reason = 'invalid_command';
    } else if (command.chain !== this.chain) reason = 'chain_mismatch';
    else if (!this.wallet || !validWallet(command.wallet) || command.wallet.toLowerCase() !== this.wallet) reason = 'wallet_mismatch';
    else if (!Number.isSafeInteger(command.expected_pid) || command.expected_pid <= 0 || command.expected_pid !== this.pid) reason = 'pid_mismatch';
    else {
      const created = typeof command.created_at === 'string'
        && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(command.created_at)
        ? Date.parse(command.created_at) : NaN;
      if (!Number.isFinite(created)) reason = 'invalid_created_at';
      else if (+now - created > COMMAND_TTL_MS) reason = 'request_expired';
      else if (created - +now > FUTURE_SKEW_MS) reason = 'request_in_future';
      else if (!Number.isFinite(this.startedAt) || created < this.startedAt) reason = 'request_predates_process';
      else if (action === 'apply' ? usdAmount(command.amount_usd) === null : Object.hasOwn(command, 'amount_usd')) reason = 'invalid_amount_usd';
      else if (this.stopRequested) reason = 'already_stopped';
    }
    if (!reason && action === 'stop') {
      reason = localStopReason(account);
      if (!reason) {
        try {
          const count = await pendingTransactions();
          reason = !Number.isSafeInteger(count) || count < 0 ? 'pending_state_unavailable'
            : count > 0 ? 'pending_transactions' : null;
        } catch { reason = 'pending_state_unavailable'; }
      }
    }
    if (!reason) {
      if (action === 'apply') { this.amountUsd = command.amount_usd; this.amountNativeAtomic = null; }
      if (action === 'pause') this.exitOnly = true;
      if (action === 'resume') this.exitOnly = false;
      if (action === 'stop') this.stopRequested = true;
    }
    this.last = { request_id: id, action, state: reason ? 'rejected' : 'applied',
      reason: reason || (action === 'stop' ? 'stopped' : action === 'apply' ? 'amount_applied'
        : action === 'pause' ? 'entries_paused' : 'entries_resumed'), duplicate: false };
    if (id) this.seen.set(id, { fingerprint, decision: this.last });
    return this.receipt(now);
  }
}
