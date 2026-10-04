import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import Terminal from '@/Terminal';
import { terminalApi, type Snapshot } from '@/lib/terminal-api';

let client: QueryClient;
let snapshot: Snapshot;
beforeEach(() => {
  window.history.replaceState(null, '', '/');
  snapshot = {
    updated_at: '2026-09-09T01:00:00Z', wallet: 'mock-wallet',
    chains: [], balances: [], candidates: [], events: [],
    positions: [{ chain: 'bsc', token: 'mock-token', symbol: 'MOCK',
      entry_at: '2026-09-09T00:00:00Z', remaining_atomic: '1000', return_pct: 1,
      tp1_hit: false, tp2_hit: false, tp3_hit: false, exit_status: 'hold' }],
    orders: [{ chain: 'bsc', token: 'mock-token', symbol: 'MOCK', side: 'sell',
      tx_hash: 'mock-transaction', time: '2026-09-09T00:30:00Z', status: 'filled',
      accounting: { status: 'pending', receipt_status: 'pending' } }],
  };
  const runtime = { verified: true, runtime: [], controls: [], drafts: [] };
  vi.spyOn(terminalApi, 'snapshot').mockImplementation(async () => snapshot);
  vi.spyOn(terminalApi, 'runtime').mockResolvedValue(runtime);
  vi.spyOn(terminalApi, 'post').mockRejectedValue(new Error('Unexpected mutation'));
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } });
  client.setQueryData(['terminal'], snapshot);
  client.setQueryData(['terminal-runtime'], runtime);
});
afterEach(() => { client.clear(); });

function mount() {
  return render(<QueryClientProvider client={client}><Terminal /></QueryClientProvider>);
}
async function update(patch: Partial<Snapshot>) {
  snapshot = { ...snapshot, ...patch };
  await act(async () => { client.setQueryData(['terminal'], snapshot); });
}

it('R11 derives the selected position from each new snapshot', async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole('button', { name: '查看 MOCK' }));
  await update({ positions: [{ ...snapshot.positions[0], remaining_atomic: '200', return_pct: 100,
    tp1_hit: true, tp2_hit: true, tp3_hit: true }] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getAllByText('已触发')).toHaveLength(3));
  expect(within(screen.getByRole('dialog')).getByText('第二次止盈')).toBeTruthy();
  expect(within(screen.getByRole('dialog')).getByText('第三次止盈')).toBeTruthy();
  expect(within(screen.getByRole('dialog')).getByText('200')).toBeTruthy();
  expect(within(screen.getByRole('dialog')).getByText('+100.00%')).toBeTruthy();
});

it('R11 explicitly shows a missing position, clears selection on close, and uses a focus fallback', async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole('button', { name: '查看 MOCK' }));
  const positions = snapshot.positions;
  await update({ positions: [] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText(/持仓已不在最新快照中/)).toBeTruthy());
  expect(within(screen.getByRole('dialog')).queryByText('1000')).toBeNull();
  await user.keyboard('{Escape}');
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('main')));
  await update({ positions });
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('R11 does not replace a closed position with a new entry of the same token', async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole('button', { name: '查看 MOCK' }));
  await update({ positions: [{ ...snapshot.positions[0], entry_at: '2026-09-09T02:00:00Z' }] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText(/持仓已不在最新快照中/)).toBeTruthy());
});

it('R11 derives accounting evidence from the latest snapshot and reports removal', async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole('button', { name: '查看核验' }));
  await update({ orders: [{ ...snapshot.orders[0], accounting: {
    status: 'verified', receipt_status: 'verified', pnl_native_atomic: '250000000000000000',
  } }] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('已核验')).toBeTruthy());
  expect(within(screen.getByRole('dialog')).getByText('0.25 BNB')).toBeTruthy();
  await update({ orders: [] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText(/成交已不在最新快照中/)).toBeTruthy());
  expect(within(screen.getByRole('dialog')).queryByText('0.25 BNB')).toBeNull();
});

it.each(['position', 'accounting'])('R12 restores focus to the %s detail opener', async kind => {
  const user = userEvent.setup();
  mount();
  const opener = screen.getByRole('button', { name: kind === 'position' ? '查看 MOCK' : '查看核验' });
  await user.click(opener);
  await user.keyboard('{Escape}');
  await waitFor(() => expect(document.activeElement).toBe(opener));
  expect(terminalApi.post).not.toHaveBeenCalled();
});

it('R11 retains a hashless order identity when snapshots reorder entries', async () => {
  const user = userEvent.setup();
  snapshot.orders[0] = { ...snapshot.orders[0], tx_hash: undefined };
  mount();
  await user.click(screen.getByRole('button', { name: '查看核验' }));
  const selected = snapshot.orders[0];
  await update({ orders: [
    { ...selected, token: 'other-token', symbol: 'OTHER' },
    { ...selected, accounting: { status: 'verified', receipt_status: 'verified' } },
  ] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('已核验')).toBeTruthy());
  expect(within(screen.getByRole('dialog')).getByRole('heading').textContent).toContain('MOCK');
});

it('R11 does not match an order on another chain with the same transaction hash', async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole('button', { name: '查看核验' }));
  await update({ orders: [{ ...snapshot.orders[0], chain: 'robinhood' }] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText(/成交已不在最新快照中/)).toBeTruthy());
});

it('R12 returns a mouse-opened position row to its keyboard-accessible detail button', async () => {
  const user = userEvent.setup();
  mount();
  const opener = screen.getByRole('button', { name: '查看 MOCK' });
  await user.click(within(opener.closest('tr')!).getByText('MOCK'));
  await user.keyboard('{Escape}');
  await waitFor(() => expect(document.activeElement).toBe(opener));
});

it.each(['unverified', 'mismatch', undefined])('shows an excluded or legacy ledger (%s) as unknown, not zero holdings', async identity => {
  snapshot.positions = [];
  snapshot.orders = [];
  snapshot.chains = [
    { chain: 'bsc', state: 'degraded', ledger_identity_status: identity, open_positions: 0 },
    { chain: 'robinhood', state: 'running', ledger_identity_status: 'verified', open_positions: 0 },
  ];
  mount();
  expect(screen.getByRole('alert').textContent).toContain('账本待核验');
  expect(screen.queryByText('暂无持仓')).toBeNull();
  expect(screen.queryByText('暂无已确认成交')).toBeNull();
  const metric = screen.getByText('当前持仓', { selector: '.metric-band span' }).parentElement!;
  expect(metric.querySelector('strong')?.textContent).toBe('待核验');
  if (identity === 'mismatch') expect(screen.getByRole('alert').textContent).toContain('不一致');
  else expect(screen.getByRole('alert').textContent).toContain('迁移');
  await update({ chains: snapshot.chains.map(c => ({ ...c, ledger_identity_status: 'verified' })) });
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(metric.querySelector('strong')?.textContent).toBe('0个');
  expect(screen.getByText('暂无持仓')).toBeTruthy();
});

it('uses the known Chinese blocking reason when the backend supplies an English activity message', () => {
  snapshot.chains = [{ chain: 'bsc', state: 'degraded', ledger_identity_status: 'unverified',
    activity_reason: 'ledger_identity_unverified',
    activity_message: 'Ledger wallet and chain require verification before history can be shown.',
  }];
  mount();
  expect(screen.getByText('账本钱包与网络归属待核验')).toBeTruthy();
  expect(screen.queryByText(snapshot.chains[0].activity_message!)).toBeNull();
});

it('keeps discovery qualification separate from executor tradeability', async () => {
  const token = '0x1111111111111111111111111111111111111111';
  window.history.replaceState(null, '', '/?tab=discovery');
  snapshot.candidates = [{
    chain: 'robinhood', token, symbol: 'EARLY', score: 73, candidate_kind: 'active',
    strategy: {
      strategy_version: 'chain_v2', signal_stage: 'aggregate_discovery',
      entry_route: 'robinhood_aggregate_discovery', execution_mode: 'live_candidate',
      rank_score: 95, first_mcap_usd: 20_000, current_mcap_usd: 22_000,
      entry_delay_seconds: 30, markup_from_first: 1.1,
    },
    tradeability: { status: 'unavailable', reject_reason: 'robinhood_sell_route' },
    model: { status: 'collecting', p_2x: null, p_5x: 0, sample_count: 0 },
  } as any];
  const user = userEvent.setup();
  mount();

  expect(screen.getByText('95')).toBeTruthy();
  expect(screen.getByText('不可用')).toBeTruthy();
  expect(screen.getByText(/旧评分 73/)).toBeTruthy();
  expect(screen.queryByText('可执行')).toBeNull();
  expect(screen.queryByText('已买入')).toBeNull();

  await user.click(screen.getByRole('button', { name: '查看 EARLY 候选详情' }));
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('等待执行器评估')).toBeTruthy();
  expect(within(dialog).getByText(token)).toBeTruthy();
  expect(within(dialog).getByText('样本收集中')).toBeTruthy();
  expect(within(dialog).getByText('0%')).toBeTruthy();
  expect(within(dialog).queryByText('可执行')).toBeNull();
  expect(within(dialog).queryByText('已买入')).toBeNull();
});

it('separates executable candidates from shadow and rejected records', () => {
  window.history.replaceState(null, '', '/?tab=discovery');
  const base = {
    chain: 'bsc' as const,
    strategy: { strategy_version: 'chain_v2', signal_stage: 'aggregate_early_bird' },
  };
  snapshot.candidates = [
    { ...base, token: '0x1111111111111111111111111111111111111111', symbol: 'LIVE',
      candidate_kind: 'active', strategy: { ...base.strategy, execution_mode: 'live_candidate', eligible: true } },
    { ...base, token: '0x2222222222222222222222222222222222222222', symbol: 'SHADOW',
      candidate_kind: 'shadow', strategy: { ...base.strategy, execution_mode: 'shadow', eligible: false } },
    { ...base, token: '0x3333333333333333333333333333333333333333', symbol: 'NOPE',
      candidate_kind: 'rejected', strategy: { ...base.strategy, execution_mode: 'rejected', eligible: false } },
  ];
  mount();

  expect(screen.getByRole('heading', { name: /当前可执行候选 1/ })).toBeTruthy();
  const activeRegion = screen.getByRole('region', { name: '当前可执行候选' });
  expect(within(activeRegion).getByText('LIVE')).toBeTruthy();
  expect(within(activeRegion).queryByText('SHADOW')).toBeNull();
  expect(within(activeRegion).queryByText('NOPE')).toBeNull();
  expect(screen.getByText(/最近淘汰与影子记录/).textContent).toContain('2');
});

it('shows shadow candidates explicitly and refreshes candidate details by normalized chain and token', async () => {
  const token = '0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD';
  window.history.replaceState(null, '', '/?tab=discovery');
  snapshot.candidates = [{
    chain: 'bsc', token, symbol: 'SHADOW', candidate_kind: 'shadow',
    strategy: {
      strategy_version: 'chain_v2', signal_stage: 'aggregate_discovery',
      entry_route: 'bsc_aggregate_discovery_shadow', execution_mode: 'shadow',
      rank_score: 88, first_mcap_usd: 30_000, current_mcap_usd: 33_000,
    },
    tradeability: { status: 'pending' },
    model: { status: 'collecting', p_2x: null, p_5x: null, sample_count: 12 },
  } as any];
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByText(/最近淘汰与影子记录/));
  const opener = screen.getByRole('button', { name: '查看 SHADOW 候选详情' });
  expect(screen.getByText('影子观察')).toBeTruthy();
  await user.click(opener);

  await update({ candidates: [{
    ...(snapshot.candidates[0] as any), token: token.toLowerCase(),
    strategy: { ...(snapshot.candidates[0] as any).strategy, rank_score: 99 },
    tradeability: { status: 'failed', reject_reason: 'bsc_sellability_confirmation' },
  }] });
  const dialog = screen.getByRole('dialog');
  await waitFor(() => expect(within(dialog).getByText('99')).toBeTruthy());
  expect(within(dialog).getByText('失败')).toBeTruthy();
  expect(within(dialog).getByText('BSC 尚未完成两轮可卖确认')).toBeTruthy();

  await user.keyboard('{Escape}');
  await waitFor(() => expect(document.activeElement).toBe(opener));
});

it('reports when the selected candidate disappears from the latest snapshot', async () => {
  window.history.replaceState(null, '', '/?tab=discovery');
  snapshot.candidates = [{
    chain: 'bsc', token: '0x2222222222222222222222222222222222222222', symbol: 'GONE',
    candidate_kind: 'shadow',
    strategy: { strategy_version: 'chain_v2', execution_mode: 'shadow', rank_score: 50 },
  } as any];
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByText(/最近淘汰与影子记录/));
  await user.click(screen.getByRole('button', { name: '查看 GONE 候选详情' }));
  await update({ candidates: [] });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText(/候选已不在最新快照中/)).toBeTruthy());
});
