import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import Strategy from '@/components/terminal-strategy';
import Accounting from '@/components/terminal-accounting';
import { reason, terminalApi } from '@/lib/terminal-api';

const cases = [
  ['execution_reconciliation_required', '成交待核验，暂不重复提交'],
  ['ledger_wallet_unverified', '历史账本钱包待核验'],
  ['ledger_wallet_mismatch', '历史账本钱包与当前钱包不一致'],
  ['ledger_chain_mismatch', '历史账本网络与当前网络不一致'],
  ['sell_settlement_unverified', '卖出数量待核验，保留持仓'],
  ['market_cap_unavailable', '流通市值缺失，仅 FDV 不能作为入场依据'],
];

const v2Cases = [
  ['missing_token_identity', '缺少链与合约身份'],
  ['unsupported_chain', '当前网络不受策略支持'],
  ['missing_first_snapshot', '缺少不可变首次发现快照'],
  ['first_snapshot_identity_mismatch', '首次发现快照与当前链或合约不一致'],
  ['current_metrics_unavailable', '当前价格或市值不可用'],
  ['robinhood_rank_unavailable', 'Robinhood 发现排名不可用'],
  ['chain_v2_contract_incomplete', 'V2 候选合同字段不完整'],
  ['exact_entry_tradeability_timeout', '精确交易性检查超时，跳过本轮候选'],
  ['buy_price_impact_exceeded', '买入价格冲击超过策略上限'],
  ['bsc_rank_unavailable', 'BSC 发现排名不可用'],
  ['bsc_shadow_only', 'BSC 聚合发现仅作影子观察'],
  ['bsc_mcap_shadow_only', 'BSC 该市值区间仅作影子观察'],
  ['robinhood_signal_stage', 'Robinhood 信号阶段不符合入场路线'],
  ['robinhood_first_mcap_range', 'Robinhood 首次市值不在 10K–300K'],
  ['bsc_first_mcap_range', 'BSC 首次市值不在 10K–100K'],
  ['robinhood_entry_window', 'Robinhood 已超过 10 分钟入场窗口'],
  ['bsc_entry_window', 'BSC 已超过 10 分钟入场窗口'],
  ['robinhood_markup_limit', 'Robinhood 相对首次价格涨幅超过 1.25x'],
  ['bsc_markup_limit', 'BSC 相对首次价格涨幅超过 1.5x'],
  ['robinhood_liquidity_minimum', 'Robinhood 流动性低于 8K'],
  ['bsc_liquidity_minimum', 'BSC 流动性低于 8K'],
  ['robinhood_buy_route', 'Robinhood 买入路线不可用'],
  ['robinhood_sell_route', 'Robinhood 卖出路线不可用'],
  ['bsc_buy_route', 'BSC 买入路线不可用'],
  ['bsc_sell_route', 'BSC 卖出路线不可用'],
  ['robinhood_round_trip_loss_limit', 'Robinhood 预计往返损耗超过 25%'],
  ['bsc_round_trip_loss_limit', 'BSC 预计往返损耗超过 25%'],
  ['robinhood_buy_impact_limit', 'Robinhood 买入冲击超过 12%'],
  ['bsc_sell_impact_limit', 'BSC 卖出冲击超过 12%'],
  ['robinhood_hard_risk', 'Robinhood 合约硬风险未通过'],
  ['bsc_hard_risk', 'BSC 合约硬风险未通过'],
  ['bsc_sellability_confirmation', 'BSC 尚未完成两轮可卖确认'],
  ['bsc_observed_sell_required', 'BSC 尚未观察到真实卖单'],
];

it.each(v2Cases)('translates the V2 rejection %s', (code, text) => {
  expect(reason(code)).toBe(text);
});

it.each(cases)('explains %s in records and disabled strategy controls', (code, text) => {
  expect(reason(code)).toBe(text);
  render(<Strategy chain="bsc" wallet="mock-wallet" verified={false}
    runtime={{ chain: 'bsc', running: true, pid: 11 }} onChanged={() => {}}
    control={{ chain: 'bsc', supported: false, state: 'rejected', reason: code }} />);
  expect(screen.getByText(new RegExp(text))).toBeTruthy();
  for (const name of ['停止执行器', '暂停开仓', '应用金额']) {
    expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
  }
});

it.each(cases)('explains a rejected command with %s without retrying', async (code, text) => {
  const post = vi.spyOn(terminalApi, 'post').mockRejectedValue(new Error(code));
  const user = userEvent.setup();
  render(<Strategy chain="bsc" wallet="mock-wallet" verified
    runtime={{ chain: 'bsc', running: true, pid: 11 }} onChanged={() => {}}
    control={{ chain: 'bsc', supported: true, state: 'ready' }} />);
  await user.click(screen.getByRole('button', { name: '停止执行器' }));
  await user.click(screen.getByRole('button', { name: '确认停止执行器' }));
  expect(await screen.findByText(text)).toBeTruthy();
  expect(post).toHaveBeenCalledTimes(1);
});

it.each(cases)('explains %s in accounting details', (code, text) => {
  render(<Accounting onClose={() => {}} order={{
    chain: 'bsc', token: 'mock-token', symbol: 'MOCK', side: 'sell',
    accounting: { status: 'unavailable', reason: code },
  }} />);
  expect(within(screen.getByRole('dialog')).getByText(new RegExp(text))).toBeTruthy();
});
