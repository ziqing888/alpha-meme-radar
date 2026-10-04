import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { keccak256 } from 'ethers';

import {
  NATIVE_EVM_TOKEN,
  assessRoundTripEconomics,
  executionChain,
  executeBuy,
  fetchPendingIntentEvidence,
  quoteBuyTradeability,
  quoteSellValue,
} from '../src/okx_dex_executor.mjs';

const WALLET = '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A';
const TOKEN = `0x${'2'.repeat(40)}`;

function swaps({ buyImpact = 1, sellImpact = 1, gas = true } = {}) {
  const tx = gas ? { gas: '5', gasPrice: '2' } : {};
  return {
    buySwap: { routerResult: { toTokenAmount: '900', priceImpactPercent: String(buyImpact), toToken: {} }, tx },
    sellSwap: { routerResult: { toTokenAmount: '980', priceImpactPercent: String(sellImpact), fromToken: {} }, tx },
  };
}

describe('chain_v2 executor economics', () => {
  it('enforces asymmetric immutable impact limits per chain', () => {
    assert.throws(() => assessRoundTripEconomics({
      env: { EXECUTION_CHAIN_NAME: 'robinhood', OKX_MAX_PRICE_IMPACT_PERCENT: '99', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '1' },
      amountNativeAtomic: '1000', ...swaps({ buyImpact: 12.1, sellImpact: 4 }), approvalRequired: false,
    }), { message: 'buy_price_impact_exceeded' });

    assert.doesNotThrow(() => assessRoundTripEconomics({
      env: { EXECUTION_CHAIN_NAME: 'bsc', OKX_MAX_PRICE_IMPACT_PERCENT: '99', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '1' },
      amountNativeAtomic: '1000', ...swaps({ buyImpact: 12.1, sellImpact: 4 }), approvalRequired: false,
    }));

    assert.throws(() => assessRoundTripEconomics({
      env: { EXECUTION_CHAIN_NAME: 'bsc', OKX_MAX_PRICE_IMPACT_PERCENT: '99', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '1' },
      amountNativeAtomic: '1000', ...swaps({ buyImpact: 1, sellImpact: 12.1 }), approvalRequired: false,
    }), { message: 'sell_price_impact_exceeded' });
  });

  it('does not let environment settings loosen chain round-trip limits', () => {
    assert.throws(() => assessRoundTripEconomics({
      env: { EXECUTION_CHAIN_NAME: 'bsc', OKX_MAX_ROUND_TRIP_LOSS_PERCENT: '99', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '1' },
      amountNativeAtomic: '1000',
      buySwap: { routerResult: { toTokenAmount: '900', priceImpactPercent: '1', toToken: {} }, tx: { gas: '5', gasPrice: '1' } },
      sellSwap: { routerResult: { toTokenAmount: '749', priceImpactPercent: '1', fromToken: {} }, tx: { gas: '5', gasPrice: '1' } },
      approvalRequired: false,
    }), { message: 'buy_round_trip_loss_exceeded' });
  });

  it('forces local economics for v2 even when the legacy guard switch is disabled', async () => {
    let signed = false;
    await assert.rejects(() => executeBuy({
      env: {
        OKX_API_KEY: 'api', OKX_SECRET_KEY: 'secret', OKX_PASSPHRASE: 'pass', OKX_PROJECT_ID: 'project',
        BSC_PRIVATE_KEY: `0x${'1'.repeat(64)}`, BSC_RPC_URL: 'https://bsc-rpc.example',
        BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1',
        OKX_ECONOMIC_GUARD_ENABLED: '0', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '1',
      },
      candidate: { strategy_version: 'chain_v2', contract_address: TOKEN },
      amountNativeAtomic: '1000', live: true,
      deps: {
        loadSdk: async () => ({ ethers: { keccak256 } }),
        createClient: () => ({ client: {}, wallet: { address: WALLET }, chain: executionChain({ EXECUTION_CHAIN_NAME: 'bsc' }) }),
        nativeBalance: async () => '2000',
        tokenBalance: async () => '0',
        approvalRequired: async () => false,
        getSwapData: async (_client, params) => {
          const buying = params.fromTokenAddress === NATIVE_EVM_TOKEN;
          return {
            routerResult: { toTokenAmount: buying ? '900' : '700', priceImpactPercent: '1', toToken: {}, fromToken: {} },
            tx: { to: `0x${'4'.repeat(40)}`, data: '0x', value: buying ? '1' : '0', gas: '5', gasPrice: '1' },
          };
        },
        signTransaction: async () => { signed = true; return '0xsigned'; },
      },
    }), { message: 'buy_round_trip_loss_exceeded' });
    assert.equal(signed, false);
  });

  async function sellQuote(approvalRequired, tx = { gas: '5', gasPrice: '2' }) {
    return quoteSellValue({
      env: { EXECUTION_CHAIN_NAME: 'bsc' },
      position: { strategy_version: 'chain_v2', contract_address: TOKEN, remaining_atomic: '900' },
      tokenAmountAtomic: '400',
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        loadSdk: async () => ({}),
        createClient: () => ({ client: {}, wallet: { address: WALLET }, chain: executionChain({ EXECUTION_CHAIN_NAME: 'bsc' }) }),
        approvalRequired: async () => approvalRequired,
        getSwapData: async (_client, params) => {
          assert.equal(params.amount, '400');
          assert.equal(params.toTokenAddress, NATIVE_EVM_TOKEN);
          return { routerResult: { fromTokenAmount: '400', toTokenAmount: '1000', priceImpactPercent: '2' }, tx };
        },
      },
    });
  }

  it('requires complete gas data for a v2 exact-amount sell quote', async () => {
    await assert.rejects(() => sellQuote(false, {}), { message: 'sell_economics_unavailable' });
  });

  it('includes approval gas only when allowance requires approval', async () => {
    const withoutApproval = await sellQuote(false);
    const withApproval = await sellQuote(true);
    assert.equal(withoutApproval.estimated_gas_native_atomic, '10');
    assert.equal(withoutApproval.estimated_net_native_atomic, '990');
    assert.equal(withApproval.estimated_approval_gas_native_atomic, '130000');
    assert.equal(withApproval.estimated_gas_native_atomic, '130010');
    assert.equal(withApproval.estimated_net_native_atomic, '0');
  });

  it('reserves buy, sell and required approval gas before allowing a buy', () => {
    const economics = assessRoundTripEconomics({
      env: { EXECUTION_CHAIN_NAME: 'bsc', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '5' },
      amountNativeAtomic: '1000',
      buySwap: { routerResult: { toTokenAmount: '900', priceImpactPercent: '1', toToken: {} }, tx: { gas: '10', gasPrice: '2' } },
      sellSwap: { routerResult: { toTokenAmount: '1080', priceImpactPercent: '1', fromToken: {} }, tx: { gas: '20', gasPrice: '2' } },
      approvalRequired: true,
    });
    assert.equal(economics.pretrade_buy_gas_native_atomic, '20');
    assert.equal(economics.pretrade_round_trip_gas_native_atomic, '70');
    assert.equal(economics.required_native_balance_atomic, '1070');
  });

  it('rejects a live buy when balance covers purchase and buy gas but not the complete exit reserve', async () => {
    await assert.rejects(() => executeBuy({
      env: {
        OKX_API_KEY: 'api', OKX_SECRET_KEY: 'secret', OKX_PASSPHRASE: 'pass', OKX_PROJECT_ID: 'project',
        BSC_PRIVATE_KEY: `0x${'1'.repeat(64)}`, BSC_RPC_URL: 'https://bsc-rpc.example',
        BSC_WALLET_ADDRESS: WALLET, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1',
        OKX_ESTIMATED_APPROVAL_GAS_UNITS: '5',
      },
      candidate: { strategy_version: 'chain_v2', contract_address: TOKEN },
      amountNativeAtomic: '1000', live: true,
      deps: {
        loadSdk: async () => ({}),
        createClient: () => ({ client: {}, wallet: { address: WALLET }, chain: executionChain({ EXECUTION_CHAIN_NAME: 'bsc' }) }),
        nativeBalance: async () => '1020',
        approvalRequired: async () => true,
        getSwapData: async (_client, params) => {
          const buying = params.fromTokenAddress === NATIVE_EVM_TOKEN;
          return {
            routerResult: { toTokenAmount: buying ? '900' : '1080', priceImpactPercent: '1', toToken: {}, fromToken: {} },
            tx: { gas: buying ? '10' : '20', gasPrice: '2' },
          };
        },
      },
    }), { message: 'insufficient_native_gas_balance' });
  });

  it('builds a read-only exact buy and reverse-sell evidence bundle', async () => {
    let calls = 0;
    const result = await quoteBuyTradeability({
      env: { EXECUTION_CHAIN_NAME: 'bsc', OKX_ESTIMATED_APPROVAL_GAS_UNITS: '1' },
      candidate: { contract_address: TOKEN, pool_address: `0x${'3'.repeat(40)}` },
      amountNativeAtomic: '1000',
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        loadSdk: async () => ({}),
        createClient: () => ({ client: {}, wallet: { address: WALLET }, chain: executionChain({ EXECUTION_CHAIN_NAME: 'bsc' }) }),
        approvalRequired: async () => false,
        getSwapData: async (_client, params) => {
          calls += 1;
          const buying = params.fromTokenAddress === NATIVE_EVM_TOKEN;
          return { routerResult: { toTokenAmount: buying ? '900' : '1080', priceImpactPercent: '1',
            toTokenUsdPrice: buying ? '0.00125' : undefined, router: buying ? 'buy-route' : 'sell-route',
            toToken: {}, fromToken: {} }, tx: { gas: '5', gasPrice: '1' } };
        },
      },
    });
    assert.equal(calls, 2);
    assert.equal(result.exact_buy_price_usd, 0.00125);
    assert.equal(result.buy_route_id, 'buy-route');
    assert.equal(result.sell_route_id, 'sell-route');
    assert.equal(result.contract_address, TOKEN.toLowerCase());
  });

  it('derives pending receipt wallet deltas from the durable pre-broadcast balance snapshot', async () => {
    const hash = `0x${'4'.repeat(64)}`;
    const result = await fetchPendingIntentEvidence({
      env: {},
      intent: {
        contract_address: TOKEN,
        transactions: [{ side: 'buy', stage: 'broadcast', tx_hash: hash }],
        wallet_balances_before: { token_atomic: '1000', native_atomic: '1000' },
      },
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        loadSdk: async () => ({}),
        createClient: () => ({ client: {}, wallet: { address: WALLET, provider: {
          getTransactionReceipt: async () => ({ transactionHash: hash, status: 1 }),
        } } }),
        tokenBalance: async () => '1200',
        nativeBalance: async () => '900',
      },
    });
    assert.equal(result.receipt.transactionHash, hash);
    assert.deepEqual(result.wallet_deltas, { token_atomic: '200', native_atomic: '-100' });
  });
});
