import type { ComponentProps } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Strategy from '@/components/terminal-strategy';
import { terminalApi } from '@/lib/terminal-api';

type Props = ComponentProps<typeof Strategy>;
const base: Props = {
  chain: 'bsc', wallet: 'mock-wallet-a', verified: true,
  runtime: { chain: 'bsc', running: true, pid: 11, amount_usd: 1, exit_only: false },
  control: { chain: 'bsc', supported: true, state: 'ready' },
  onChanged: () => {},
};

beforeEach(() => { vi.spyOn(terminalApi, 'post').mockResolvedValue({}); });

describe('R5 confirmation identity', () => {
  it.each<[string, Partial<Props>]>([
    ['PID', { runtime: { ...base.runtime!, pid: 22 } }],
    ['wallet', { wallet: 'mock-wallet-b' }],
    ['verification', { verified: false }],
    ['support', { control: { ...base.control!, supported: false } }],
    ['running state', { runtime: { ...base.runtime!, running: false } }],
    ['entry mode', { runtime: { ...base.runtime!, exit_only: true } }],
  ])('requires a new confirmation after %s changes', async (_, change) => {
    const user = userEvent.setup();
    const { rerender } = render(<Strategy {...base} />);
    await user.click(screen.getByRole('button', { name: '停止执行器' }));
    expect(screen.getByRole('dialog')).toBeTruthy();
    rerender(<Strategy {...base} {...change} />);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText(/重新确认/)).toBeTruthy();
    rerender(<Strategy {...base} />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(terminalApi.post).not.toHaveBeenCalled();
  });

  it('submits the confirmed amount even if polling updates the draft', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Strategy {...base} draft="5" />);
    await user.click(screen.getByRole('button', { name: '应用金额' }));
    rerender(<Strategy {...base} draft="25" />);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/调整为 1 U/)).toBeTruthy();
    await user.click(within(dialog).getByRole('button', { name: '确认应用金额' }));
    expect(terminalApi.post).toHaveBeenCalledExactlyOnceWith('strategy/command', {
      chain: 'bsc', action: 'apply', wallet_address: 'mock-wallet-a',
      expected_pid: 11, amount_usd: '1', request_id: expect.any(String),
    });
  });

  it('freezes a start amount while the stopped runtime is polled', async () => {
    const user = userEvent.setup();
    const props = { ...base, runtime: undefined };
    const { rerender } = render(<Strategy {...props} draft="5" />);
    await user.click(screen.getByRole('button', { name: '启动' }));
    rerender(<Strategy {...props} draft="25" />);
    await user.click(screen.getByRole('button', { name: '确认启动自动交易' }));
    expect(terminalApi.post).toHaveBeenCalledWith('strategy/command', expect.objectContaining({
      action: 'start', expected_pid: 0, amount_usd: '1', wallet_address: base.wallet,
    }));
  });

  it('guards the apply submit handler when verification is unavailable', () => {
    render(<Strategy {...base} verified={false} />);
    fireEvent.submit(screen.getByRole('spinbutton').closest('form')!);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(terminalApi.post).not.toHaveBeenCalled();
  });

  it('only targets the replacement worker after explicitly reopening confirmation', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Strategy {...base} />);
    await user.click(screen.getByRole('button', { name: '停止执行器' }));
    rerender(<Strategy {...base} runtime={{ ...base.runtime!, pid: 22 }} />);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(terminalApi.post).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '停止执行器' }));
    await user.click(screen.getByRole('button', { name: '确认停止执行器' }));
    expect(terminalApi.post).toHaveBeenCalledExactlyOnceWith('strategy/command', {
      chain: base.chain, action: 'stop', wallet_address: base.wallet,
      expected_pid: 22, request_id: expect.any(String),
    });
  });

  it('returns focus to the exact strategy entry button on Escape', async () => {
    const user = userEvent.setup();
    render(<Strategy {...base} />);
    const opener = screen.getByRole('button', { name: '停止执行器' });
    await user.click(opener);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });
});

describe('chain V2 strategy visibility', () => {
  it('shows the BSC policy and keeps configured and effective amounts separate', () => {
    render(<Strategy {...({
      ...base,
      runtime: {
        ...base.runtime,
        configured_amount_usd: '1',
        effective_amount_usd: null,
        executor_acknowledged_at: null,
      },
      strategy: {
        strategy_version: 'chain_v2',
        signal_stage: 'aggregate_early_bird',
        entry_route: 'bsc_aggregate_early_bird',
        order_notional_usd: 1,
      },
    } as any)} />);

    expect(screen.getByText('聚合早鸟 · 1 U')).toBeTruthy();
    expect(screen.getByText('计划策略')).toBeTruthy();
    expect(screen.getByText('执行器生效策略').parentElement?.textContent).toContain('待确认');
    expect(screen.queryByText('已生效')).toBeNull();
    expect(screen.getByText('配置金额').parentElement?.textContent).toContain('1 U');
    expect(screen.getByText('执行器实际金额').parentElement?.textContent).toContain('待确认');
    expect(screen.getByText('执行器确认').parentElement?.textContent).toContain('待确认');
  });

  it('shows the Robinhood policy and an acknowledged effective amount without using the draft', () => {
    render(<Strategy {...({
      ...base,
      chain: 'robinhood',
      draft: '9',
      runtime: {
        chain: 'robinhood', running: true, pid: 22,
        configured_amount_usd: '2', effective_amount_usd: '2',
        executor_acknowledged_at: '2026-09-09T03:04:05Z',
      },
      control: { chain: 'robinhood', supported: true, state: 'ready' },
      strategy: {
        strategy_version: 'chain_v2',
        signal_stage: 'aggregate_discovery',
        entry_route: 'robinhood_aggregate_discovery',
        order_notional_usd: 2,
      },
      effectiveStrategy: {
        strategy_version: 'chain_v2',
        signal_stage: 'aggregate_discovery',
        entry_route: 'robinhood_aggregate_discovery',
        order_notional_usd: 2,
      },
    } as any)} />);

    expect(screen.getByText('聚合发现 · 2 U')).toBeTruthy();
    expect(screen.getByText('执行器生效策略').parentElement?.textContent).toContain('chain_v2 · 聚合发现');
    expect(screen.getByText('配置金额').parentElement?.textContent).toContain('2 U');
    expect(screen.getByText('执行器实际金额').parentElement?.textContent).toContain('2 U');
    expect(screen.getByText('执行器确认').parentElement?.textContent).not.toContain('待确认');
  });

  it('renders entry and risk thresholds from the backend strategy contract', () => {
    render(<Strategy {...({
      ...base,
      strategy: {
        strategy_version: 'chain_v2',
        signal_stage: 'aggregate_early_bird',
        order_notional_usd: 1,
        min_rank_score: 63,
        min_independent_sources: 3,
        first_mcap_min_usd: 12_000,
        first_mcap_max_usd: 88_000,
        max_pair_age_hours: 4,
        max_entry_delay_seconds: 420,
        max_markup_from_first: 1.35,
        min_liquidity_usd: 23_456,
        max_round_trip_loss_pct: 7.5,
        min_sellable_cycles: 2,
        requires_observed_sell: true,
        max_open_positions: 4,
        max_open_notional_usd: 8,
        daily_realized_loss_stop_usd: 3,
      },
    } as any)} />);

    expect(screen.getByText('最低评分').parentElement?.textContent).toContain('63');
    expect(screen.getByText('独立来源').parentElement?.textContent).toContain('3');
    expect(screen.getByText('首次市值').parentElement?.textContent).toContain('$12K–$88K');
    expect(screen.getByText('入场窗口').parentElement?.textContent).toContain('7 分钟');
    expect(screen.getByText('最低流动性').parentElement?.textContent).toContain('$23.5K');
    expect(screen.getByText('最大往返损耗').parentElement?.textContent).toContain('7.5%');
    expect(screen.getByText('仓位上限').parentElement?.textContent).toContain('4 仓 / $8');
    expect(screen.getByText('当日已实现止损').parentElement?.textContent).toContain('-$3');
  });
});
