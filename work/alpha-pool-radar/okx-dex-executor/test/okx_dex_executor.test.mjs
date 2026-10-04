import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { describe, it } from 'node:test';
import { keccak256 } from 'ethers';

import {
  BSC_CHAIN_ID,
  ROBINHOOD_CHAIN_ID,
  NATIVE_EVM_TOKEN,
  configureFetchProxy,
  executeBuy,
  executeSell,
  executionChain,
  preflight,
  quoteSellValue,
  redact,
  runRoundTrip,
  selectStrategyCandidate,
} from '../src/okx_dex_executor.mjs';

const WALLET = '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A';
const TOKEN = '0x' + '2'.repeat(40);
const SECRET_ENV = {
  OKX_API_KEY: 'api-secret-value',
  OKX_SECRET_KEY: 'secret-key-value',
  OKX_PASSPHRASE: 'passphrase-value',
  OKX_PROJECT_ID: 'project-id-value',
  BSC_PRIVATE_KEY: '0x' + '1'.repeat(64),
  BSC_RPC_URL: 'https://bsc-rpc.example',
  BSC_WALLET_ADDRESS: WALLET,
  OKX_ECONOMIC_GUARD_ENABLED: '0',
};
const ROBINHOOD_ENV = {
  ...SECRET_ENV,
  EXECUTION_CHAIN_NAME: 'robinhood',
  EVM_RPC_URL: 'https://rpc.mainnet.chain.robinhood.com',
  EVM_PRIVATE_KEY: SECRET_ENV.BSC_PRIVATE_KEY,
  EVM_WALLET_ADDRESS: WALLET,
};

describe('settlement and execution progress', () => {
  const signed = '0x010203';
  const hash = keccak256(signed);
  const approvalHash = '0x' + 'a'.repeat(64);
  function fixture(side, overrides = {}) {
    const balances = side === 'buy' ? ['0', '1000'] : ['1000', '0'];
    return {
      env: { ...SECRET_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      live: true, amountNativeAtomic: '1000', tokenAmountAtomic: '1000',
      candidate: { contract_address: TOKEN },
      position: { contract_address: TOKEN, remaining_atomic: '1000' },
      deps: {
        loadSdk: async () => ({ ethers: { keccak256 } }),
        createClient: () => ({ client: {}, wallet: { address: WALLET } }),
        nativeBalance: async () => '10000',
        tokenBalance: async () => balances.shift(),
        getSwapData: async () => ({ tx: {} }),
        executeApproval: async () => ({ alreadyApproved: true }),
        signTransaction: async () => signed,
        broadcastTransaction: async () => ({ txHash: hash }),
        waitTransaction: async () => ({ status: 1 }),
        ...overrides,
      },
    };
  }

  for (const mode of ['zero', 'negative', 'before_failed', 'after_failed']) {
    it(`does not invent a sold amount for ${mode} settlement`, async () => {
      let reads = 0;
      const result = await executeSell(fixture('sell', { tokenBalance: async () => {
        reads++;
        if ((mode === 'before_failed' && reads === 1) || (mode === 'after_failed' && reads === 2)) throw new Error('balance_unavailable');
        return mode === 'negative' && reads === 2 ? '1100' : '1000';
      } }));
      assert.equal(result.state, 'sell_settlement_unverified');
      assert.equal(result.sell_tx, hash);
      assert.equal(result.requested_sold_atomic, '1000');
      assert.equal(Object.hasOwn(result, 'sold_atomic'), false);
    });
  }

  it('reuses a fresh prepared sell swap instead of requesting a duplicate quote', async () => {
    const preparedSwap = { routerResult: {
      fromTokenAmount: '1000', toTokenAmount: '875', priceImpactPercent: '1.25',
    }, tx: {} };
    const options = fixture('sell', {
      getSwapData: async () => { throw new Error('duplicate_quote_requested'); },
    });

    const result = await executeSell({ ...options, preparedSwap });

    assert.equal(result.state, 'sell_confirmed');
  });

  it('refreshes a prepared sell swap when a partial exit changes the input amount', async () => {
    let quoteRequests = 0;
    const preparedSwap = { routerResult: {
      fromTokenAmount: '1000', toTokenAmount: '875', priceImpactPercent: '1.25',
    }, tx: {} };
    const options = fixture('sell', {
      getSwapData: async (_client, params) => {
        quoteRequests++;
        assert.equal(params.amount, '800');
        return { routerResult: {
          fromTokenAmount: '800', toTokenAmount: '700', priceImpactPercent: '1.25',
        }, tx: {} };
      },
    });

    const result = await executeSell({ ...options, tokenAmountAtomic: '800', preparedSwap });

    assert.equal(result.state, 'sell_confirmed');
    assert.equal(quoteRequests, 1);
  });

  for (const side of ['buy', 'sell']) {
    const execute = side === 'buy' ? executeBuy : executeSell;
    it(`awaits ordered ${side} progress with the locally computed hash`, async () => {
      const events = [];
      const options = fixture(side, {
        broadcastTransaction: async () => {
          const prepared = events.at(-1);
          assert.deepEqual({ stage: prepared.stage, side: prepared.side, tx_hash: prepared.tx_hash },
            { stage: 'prepared', side, tx_hash: hash });
          assert.deepEqual(prepared.wallet_balances_before, {
            native_atomic: '10000', token_atomic: side === 'buy' ? '0' : '1000',
          });
          return { txHash: hash };
        },
        waitTransaction: async () => {
          assert.equal(events.at(-1).stage, 'broadcast');
          return { status: 1 };
        },
      });
      await execute({ ...options, onExecutionProgress: async event => {
        await Promise.resolve();
        events.push(event);
      } });
      assert.deepEqual(events.filter(event => event.side === side).map(({ stage, side: eventSide, tx_hash }) => ({ stage, side: eventSide, tx_hash })),
        ['prepared', 'broadcast', 'confirmed'].map(stage => ({ stage, side, tx_hash: hash })));
    });

    for (const stage of ['prepared', 'broadcast', 'confirmed']) {
      it(`propagates ${side} ${stage} persistence failure without further submission`, async () => {
        let broadcasts = 0;
        const options = fixture(side, { broadcastTransaction: async () => { broadcasts++; return { txHash: hash }; } });
        await assert.rejects(execute({ ...options, onExecutionProgress: async event => {
          if (event.side === side && event.stage === stage) throw new Error('disk_full');
        } }), /execution_progress_failed/);
        assert.equal(broadcasts, stage === 'prepared' ? 0 : 1);
      });
    }
  }

  it('falls back to BSC RPC with the same signed transaction when OKX broadcast fails', async () => {
    const rpcPayloads = [];
    const options = fixture('buy', {
      broadcastTransaction: undefined,
      createClient: () => ({
        client: {
          dex: {
            broadcastTransaction: async () => { throw new Error('provider_unavailable'); },
          },
        },
        wallet: {
          address: WALLET,
          provider: {
            broadcastTransaction: async (payload) => {
              rpcPayloads.push(payload);
              return { hash };
            },
          },
        },
      }),
    });

    const result = await executeBuy(options);

    assert.equal(result.state, 'buy_confirmed');
    assert.equal(result.buy_tx, hash);
    assert.deepEqual(rpcPayloads, [signed]);
  });

  it('journals an opaque approval before entering the SDK and then records its hash', async () => {
    const events = [];
    const options = fixture('sell', { executeApproval: async () => {
      assert.deepEqual(events.map(({ stage, side }) => ({ stage, side })), [{ stage: 'prepared', side: 'approval' }]);
      assert.deepEqual(events[0].wallet_balances_before, { native_atomic: '10000', token_atomic: '1000' });
      return { transactionHash: approvalHash };
    } });
    await executeSell({ ...options, onExecutionProgress: async event => { events.push(event); } });
    assert.deepEqual(events.filter(event => event.side === 'approval').map(({ stage, side, tx_hash }) => ({ stage, side, ...(tx_hash ? { tx_hash } : {}) })), [
      { stage: 'prepared', side: 'approval' },
      { stage: 'broadcast', side: 'approval', tx_hash: approvalHash },
      { stage: 'confirmed', side: 'approval', tx_hash: approvalHash },
    ]);
  });

  for (const stage of ['prepared', 'broadcast', 'confirmed']) {
    it(`stops before swap when approval ${stage} persistence fails`, async () => {
      let approvals = 0;
      let swaps = 0;
      const options = fixture('sell', {
        executeApproval: async () => { approvals++; return { transactionHash: approvalHash }; },
        broadcastTransaction: async () => { swaps++; return { txHash: hash }; },
      });
      await assert.rejects(executeSell({ ...options, onExecutionProgress: async event => {
        if (event.side === 'approval' && event.stage === stage) throw new Error('disk_full');
      } }), /execution_progress_failed/);
      assert.equal(approvals, stage === 'prepared' ? 0 : 1);
      assert.equal(swaps, 0);
    });
  }

  it('does not report execution progress for dry-run', async () => {
    for (const side of ['buy', 'sell']) {
      const execute = side === 'buy' ? executeBuy : executeSell;
      await execute({ ...fixture(side), live: false, onExecutionProgress: () => assert.fail('dry_run_progress') });
    }
  });

  for (const chain of ['bsc', 'robinhood']) {
    for (const side of ['buy', 'sell']) {
      it(`uses pending nonce for ${chain} ${side} instead of latest or quote nonce`, async () => {
        const reads = [];
        let request;
        const wallet = { address: WALLET,
          provider: { getTransactionCount: async (address, blockTag) => {
            reads.push([address, blockTag]);
            return blockTag === 'pending' ? 8 : 7;
          } },
          signTransaction: async tx => { request = tx; return signed; },
        };
        const options = fixture(side, {
          createClient: () => ({ client: {}, wallet }),
          signTransaction: undefined,
          simulateTransaction: async () => null,
          getSwapData: async () => ({ tx: {
            to: TOKEN, value: '0', data: '0x', gas: '21000', nonce: 2,
            ...(chain === 'bsc' ? { gasPrice: '1' } : { maxFeePerGas: '2', maxPriorityFeePerGas: '1' }),
          } }),
        });
        options.env = { ...options.env, EXECUTION_CHAIN_NAME: chain };
        const execute = side === 'buy' ? executeBuy : executeSell;
        await execute(options);
        assert.equal(request.nonce, 8);
        assert.deepEqual(reads, [[WALLET, 'pending']]);
      });
    }
  }

  for (const side of ['buy', 'sell']) {
    it(`does not sign or broadcast ${side} when pending nonce lookup fails`, async () => {
      let signedCount = 0;
      let broadcasts = 0;
      const options = fixture(side, {
        createClient: () => ({ client: {}, wallet: { address: WALLET,
          provider: { getTransactionCount: async (_address, blockTag) => {
            if (blockTag === 'pending') throw new Error('nonce_unavailable');
            return 7;
          } },
          signTransaction: async () => { signedCount++; return signed; },
        } }),
        signTransaction: undefined,
        broadcastTransaction: async () => { broadcasts++; return { txHash: hash }; },
      });
      const execute = side === 'buy' ? executeBuy : executeSell;
      await assert.rejects(execute(options), new RegExp(`${side}_sign_failed`));
      assert.equal(signedCount, 0);
      assert.equal(broadcasts, 0);
    });
  }

  for (const failNonce of [false, true]) {
    it(`uses pending nonce for direct approval without latest fallback (failure=${failNonce})`, async () => {
      const reads = [];
      let request;
      let broadcasts = 0;
      const wallet = { address: WALLET, signTransaction: async tx => { request = tx; return signed; }, provider: {
        estimateGas: async () => 50000n,
        getFeeData: async () => ({ gasPrice: 100n }),
        getTransactionCount: async (address, blockTag) => {
          reads.push([address, blockTag]);
          if (blockTag === 'pending' && failNonce) throw new Error('nonce_unavailable');
          return blockTag === 'pending' ? 8 : 7;
        },
        broadcastTransaction: async () => { broadcasts++; return { hash }; },
      } };
      const options = fixture('sell', {
        executeApproval: undefined,
        createClient: () => ({ client: {}, wallet }),
        loadSdk: async () => ({ ethers: { keccak256, Interface: class { encodeFunctionData() { return '0x1234'; } } } }),
        getApprovalTarget: async () => '0x' + '6'.repeat(40),
        tokenAllowance: async () => '0',
        simulateTransaction: async () => null,
      });
      options.env = { ...options.env, EXECUTION_CHAIN_NAME: 'robinhood' };
      if (failNonce) {
        await assert.rejects(executeSell(options), /sell_approval_failed/);
        assert.equal(request, undefined);
        assert.equal(broadcasts, 0);
      } else {
        await executeSell(options);
        assert.equal(request.nonce, 8);
        assert.equal(broadcasts, 1);
      }
      assert.deepEqual(reads, [[WALLET, 'pending']]);
    });
  }

  for (const failPrepared of [false, true]) {
    it(`records the Robinhood approval local hash before RPC broadcast (failure=${failPrepared})`, async () => {
      const events = [];
      let approvalBroadcasts = 0;
      const wallet = { address: WALLET, signTransaction: async () => signed, provider: {
        estimateGas: async () => 50000n,
        getFeeData: async () => ({ gasPrice: 100n }),
        getTransactionCount: async () => 1,
        broadcastTransaction: async () => {
          assert.deepEqual(events.at(-1), { stage: 'prepared', side: 'approval', tx_hash: hash });
          approvalBroadcasts++;
          return { hash };
        },
      } };
      const options = fixture('sell', {
        executeApproval: undefined,
        createClient: () => ({ client: {}, wallet }),
        loadSdk: async () => ({ ethers: { keccak256, Interface: class { encodeFunctionData() { return '0x1234'; } } } }),
        getApprovalTarget: async () => '0x' + '6'.repeat(40),
        tokenAllowance: async () => '0',
        simulateTransaction: async () => null,
      });
      options.env = { ...options.env, EXECUTION_CHAIN_NAME: 'robinhood' };
      options.onExecutionProgress = async event => {
        events.push(event);
        if (failPrepared && event.side === 'approval' && event.stage === 'prepared') throw new Error('disk_full');
      };
      if (failPrepared) {
        await assert.rejects(executeSell(options), /execution_progress_failed/);
        assert.equal(approvalBroadcasts, 0);
      } else {
        await executeSell(options);
        assert.deepEqual(events.filter(event => event.side === 'approval'),
          ['prepared', 'broadcast', 'confirmed'].map(stage => ({ stage, side: 'approval', tx_hash: hash })));
      }
    });
  }
});

describe('fetch proxy', () => {
  it('uses HTTPS_PROXY for Node fetch without exposing credentials', async () => {
    const calls = [];
    const result = await configureFetchProxy({
      HTTPS_PROXY: 'http://127.0.0.1:7897',
    }, {
      importUndici: async () => ({
        ProxyAgent: class {
          constructor(url) {
            calls.push(['agent', url]);
          }
        },
        setGlobalDispatcher: (agent) => calls.push(['dispatcher', agent.constructor.name]),
      }),
    });
    assert.deepEqual(result, { enabled: true, proxy: 'http://127.0.0.1:7897/' });
    assert.deepEqual(calls, [['agent', 'http://127.0.0.1:7897'], ['dispatcher', 'ProxyAgent']]);
  });

  it('leaves fetch unchanged when no proxy is configured', async () => {
    const result = await configureFetchProxy({}, {
      importUndici: async () => {
        throw new Error('undici_should_not_load');
      },
    });
    assert.deepEqual(result, { enabled: false });
  });
});

describe('preflight', () => {
  it('selects Robinhood mainnet without changing the EVM wallet identity', async () => {
    const calls = [];
    assert.deepEqual(executionChain(ROBINHOOD_ENV), { name: 'robinhood', chainId: ROBINHOOD_CHAIN_ID });
    const result = await preflight(ROBINHOOD_ENV, {
      remote: true,
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: WALLET }),
      }),
      createClient: () => ({ client: { dex: { getChainData: async (chainIndex) => {
        calls.push(chainIndex);
        return { code: '0', data: [{ chainIndex }] };
      } } }, wallet: { address: WALLET } }),
    });
    assert.equal(result.ready, true);
    assert.equal(result.chain, 'robinhood');
    assert.equal(result.chain_id, ROBINHOOD_CHAIN_ID);
    assert.deepEqual(calls, [String(ROBINHOOD_CHAIN_ID)]);
  });

  it('can be imported from a node eval health check without running main', () => {
    const result = spawnSync(process.execPath, [
      '--input-type=module',
      '-e',
      "import('./src/okx_dex_executor.mjs').then(()=>console.log('import_ok'))",
    ], { cwd: new URL('..', import.meta.url), encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /import_ok/);
  });

  it('runs the CLI entrypoint when the file is invoked directly', () => {
    const result = spawnSync(process.execPath, [
      'src/okx_dex_executor.mjs',
      '--preflight',
    ], {
      cwd: new URL('..', import.meta.url),
      encoding: 'utf8',
      env: { ...process.env, OKX_API_KEY: '', OKX_SECRET_KEY: '', OKX_PROJECT_ID: '' },
    });
    assert.notEqual(result.stdout.trim(), '');
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.ready, false);
  });

  it('passes remote preflight mode from the CLI to preflight', () => {
    const result = spawnSync(process.execPath, [
      'src/okx_dex_executor.mjs',
      '--preflight',
      '--remote-preflight',
    ], {
      cwd: new URL('..', import.meta.url),
      encoding: 'utf8',
      env: { ...process.env, OKX_API_KEY: '', OKX_SECRET_KEY: '', OKX_PROJECT_ID: '' },
    });
    assert.notEqual(result.stdout.trim(), '');
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.ready, false);
  });

  it('reports missing OKX project id before trying SDK or signing', async () => {
    const env = { ...SECRET_ENV };
    delete env.OKX_PROJECT_ID;
    const result = await preflight(env, {
      loadSdk: async () => {
        throw new Error('sdk_should_not_load');
      },
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'missing_okx_project_id');
    assert.equal(result.live_started, false);
    assert.equal(JSON.stringify(result).includes(SECRET_ENV.OKX_API_KEY), false);
  });

  it('validates BSC identity and exposes only secret-free fields', async () => {
    const result = await preflight(SECRET_ENV, {
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: WALLET }),
      }),
    });
    assert.equal(result.ready, true);
    assert.equal(result.chain_id, BSC_CHAIN_ID);
    assert.equal(result.wallet, WALLET.toLowerCase());
    assert.equal(JSON.stringify(result).includes(SECRET_ENV.BSC_PRIVATE_KEY.slice(2, 18)), false);
  });

  it('rejects a private key that derives a different wallet address', async () => {
    const result = await preflight(SECRET_ENV, {
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: '0x' + '9'.repeat(40) }),
      }),
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'wallet_private_key_mismatch');
  });

  it('remote preflight validates the OKX project through a read-only BSC SDK call', async () => {
    const calls = [];
    const result = await preflight(SECRET_ENV, {
      remote: true,
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: WALLET }),
      }),
      createClient: () => ({ client: { dex: { getChainData: async (chainIndex) => {
        calls.push(chainIndex);
        return { code: '0', data: [{ chainIndex: '56' }] };
      } } }, wallet: { address: WALLET } }),
    });
    assert.equal(result.ready, true);
    assert.equal(result.remote_checked, true);
    assert.deepEqual(calls, ['56']);
  });

  it('remote preflight rejects a bad OKX project id', async () => {
    const result = await preflight(SECRET_ENV, {
      remote: true,
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: WALLET }),
      }),
      createClient: () => ({ client: { dex: { getChainData: async () => ({ code: '401', data: [] }) } }, wallet: { address: WALLET } }),
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'okx_project_auth_failed');
  });

  it('remote preflight maps SDK request exceptions to project auth failure', async () => {
    const result = await preflight(SECRET_ENV, {
      remote: true,
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: WALLET }),
      }),
      createClient: () => ({ client: { dex: { getChainData: async () => {
        throw new Error('Request failed with status code 401');
      } } }, wallet: { address: WALLET } }),
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'okx_project_auth_failed');
  });

  it('remote preflight exposes OKX auth code without leaking credentials', async () => {
    const result = await preflight(SECRET_ENV, {
      remote: true,
      loadSdk: async () => ({
        OKXDexClient: class {},
        ethers: { JsonRpcProvider: class {} },
        createEVMWallet: () => ({ address: WALLET }),
      }),
      createClient: () => ({ client: { dex: { getChainData: async () => {
        const error = new Error('Request failed with status code 401');
        error.response = {
          status: 401,
          data: {
            code: '50105',
            msg: 'Invalid OK-ACCESS-PASSPHRASE',
            apiKey: SECRET_ENV.OKX_API_KEY,
          },
        };
        throw error;
      } } }, wallet: { address: WALLET } }),
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'okx_project_auth_failed');
    assert.equal(result.okx_status, 401);
    assert.equal(result.okx_code, '50105');
    assert.equal(result.okx_msg, 'Invalid OK-ACCESS-PASSPHRASE');
    assert.equal(JSON.stringify(result).includes(SECRET_ENV.OKX_API_KEY), false);
  });
});

describe('redact', () => {
  it('removes known secret-shaped fields recursively', () => {
    assert.deepEqual(redact({
      ok: true,
      privateKey: 'secret',
      nested: { OK_ACCESS_SIGN: 'sig', tx: { data: '0xabc' } },
    }), {
      ok: true,
      nested: { tx: { data: '0xabc' } },
    });
  });
});

describe('strategy selection', () => {
  it('selects the first fresh BSC executable signal', () => {
    const candidate = selectStrategyCandidate({
      signals: [
        { chain: 'robinhood', symbol: 'NO' },
        { chain: 'bsc', symbol: 'BNC4', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40), execution_arm: 'narrative_breakout' },
      ],
    });
    assert.equal(candidate.symbol, 'BNC4');
    assert.equal(candidate.contract_address, TOKEN.toLowerCase());
  });

  it('selects a Robinhood signal with its 32-byte pool identity', () => {
    const pool = '0x' + '3'.repeat(64);
    const candidate = selectStrategyCandidate({
      signals: [{ chain: 'robinhood', symbol: 'ASHIBA', contract_address: TOKEN, pair_address: pool }],
    }, 'robinhood');
    assert.equal(candidate.symbol, 'ASHIBA');
    assert.equal(candidate.pool_address, pool);
  });
});

describe('round trip', () => {
  function guardedBuyFixture(reverseNativeAtomic) {
    const tokenBalances = ['0', '900'];
    const nativeBalances = ['2000', '950'];
    let quotes = 0;
    let signed = false;
    let broadcasted = false;
    const options = {
      env: {
        ...SECRET_ENV,
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
        OKX_ECONOMIC_GUARD_ENABLED: '1',
        OKX_MAX_ROUND_TRIP_LOSS_PERCENT: '10',
        OKX_MAX_PRICE_IMPACT_PERCENT: '8',
        OKX_ESTIMATED_APPROVAL_GAS_UNITS: '10',
      },
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000',
      live: true,
      deps: {
        loadSdk: async () => ({ ethers: { keccak256 } }),
        createClient: () => ({ client: {}, wallet: { address: WALLET } }),
        nativeBalance: async () => nativeBalances.shift(),
        tokenBalance: async () => tokenBalances.shift(),
        getSwapData: async (_client, params) => {
          quotes++;
          const buying = params.fromTokenAddress === NATIVE_EVM_TOKEN;
          return {
            routerResult: {
              fromTokenAmount: params.amount,
              toTokenAmount: buying ? '900' : reverseNativeAtomic,
              priceImpactPercent: buying ? '1.5' : '2.5',
              toToken: { isHoneyPot: false },
            },
            tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: buying ? '1' : '0', gas: '5', gasPrice: '1' },
          };
        },
        simulateTransaction: async () => 21_000n,
        signTransaction: async () => { signed = true; return '0xsigned'; },
        broadcastTransaction: async () => { broadcasted = true; return { txHash: '0x' + '5'.repeat(64), orderId: 'buy-1' }; },
        waitTransaction: async () => ({ status: 1 }),
      },
    };
    return { options, state: () => ({ quotes, signed, broadcasted }) };
  }

  it('rejects a live buy before signing when the quoted round trip loss exceeds the cap', async () => {
    const fixture = guardedBuyFixture('700');

    await assert.rejects(() => executeBuy(fixture.options), { message: 'buy_round_trip_loss_exceeded' });

    assert.deepEqual(fixture.state(), { quotes: 2, signed: false, broadcasted: false });
  });

  it('enables the economic guard by default for live buys', async () => {
    const fixture = guardedBuyFixture('950');
    delete fixture.options.env.OKX_ECONOMIC_GUARD_ENABLED;

    const result = await executeBuy(fixture.options);

    assert.equal(result.pretrade_round_trip_loss_percent, 7);
    assert.deepEqual(fixture.state(), { quotes: 2, signed: true, broadcasted: true });
  });

  it('records the pre-trade economics when the quoted round trip is acceptable', async () => {
    const fixture = guardedBuyFixture('950');

    const result = await executeBuy(fixture.options);

    assert.equal(result.pretrade_expected_token_atomic, '900');
    assert.equal(result.pretrade_round_trip_native_atomic, '950');
    assert.equal(result.pretrade_round_trip_loss_percent, 7);
    assert.equal(result.pretrade_round_trip_gas_native_atomic, '20');
    assert.equal(result.pretrade_approval_gas_native_atomic, '10');
    assert.equal(result.buy_price_impact_percent, 1.5);
    assert.equal(result.sell_price_impact_percent, 2.5);
    assert.deepEqual(fixture.state(), { quotes: 2, signed: true, broadcasted: true });
  });

  it('rejects a live buy when approval gas makes the round trip uneconomic', async () => {
    const fixture = guardedBuyFixture('950');
    fixture.options.env.OKX_ESTIMATED_APPROVAL_GAS_UNITS = '60';

    await assert.rejects(() => executeBuy(fixture.options), { message: 'buy_round_trip_loss_exceeded' });

    assert.deepEqual(fixture.state(), { quotes: 2, signed: false, broadcasted: false });
  });

  it('rejects a guarded live buy when either quote omits usable gas data', async () => {
    const fixture = guardedBuyFixture('950');
    const original = fixture.options.deps.getSwapData;
    fixture.options.deps.getSwapData = async (client, params) => {
      const swap = await original(client, params);
      if (params.fromTokenAddress !== NATIVE_EVM_TOKEN) delete swap.tx.gasPrice;
      return swap;
    };

    await assert.rejects(() => executeBuy(fixture.options), { message: 'buy_economics_unavailable' });

    assert.deepEqual(fixture.state(), { quotes: 2, signed: false, broadcasted: false });
  });

  it('quotes the executable native value of an existing token position without signing', async () => {
    let requested = null;
    const result = await quoteSellValue({
      env: SECRET_ENV,
      position: { symbol: 'MEME', contract_address: TOKEN, remaining_atomic: '900' },
      deps: {
        preflight: async () => ({ ready: true, wallet: WALLET }),
        loadSdk: async () => ({}),
        createClient: () => ({ client: {}, wallet: { address: WALLET }, chain: executionChain(SECRET_ENV) }),
        getSwapData: async (_client, params) => {
          requested = params;
          return {
            routerResult: {
              fromTokenAmount: '900', toTokenAmount: '875', priceImpactPercent: '-1.25',
              fromToken: { isHoneyPot: false },
            },
            tx: { gas: '5', gasPrice: '2' },
          };
        },
      },
    });

    assert.equal(requested.fromTokenAddress, TOKEN.toLowerCase());
    assert.equal(requested.toTokenAddress, NATIVE_EVM_TOKEN);
    assert.equal(requested.amount, '900');
    assert.equal(result.expected_native_atomic, '875');
    assert.equal(result.estimated_net_native_atomic, '865');
    assert.equal(result.estimated_gas_native_atomic, '10');
    assert.equal(result.price_impact_percent, 0);
  });

  it('passes Robinhood chain 4663 into quote, signing and broadcast', async () => {
    const calls = [];
    const result = await runRoundTrip({
      env: {
        ...ROBINHOOD_ENV,
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
        OKX_SLIPPAGE_PERCENT: '5',
      },
      candidate: { symbol: 'ASHIBA', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(64) },
      amountNativeAtomic: '250000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET }, chain: executionChain(ROBINHOOD_ENV) }),
        nativeBalance: async () => '1000000000000000',
        getSwapData: async (_client, params) => {
          calls.push(['quote', params.chainIndex]);
          assert.equal(params.slippagePercent, '1');
          assert.equal(params.autoSlippage, true);
          assert.equal(params.maxAutoSlippagePercent, '5');
          return { tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } };
        },
        simulateTransaction: async () => {
          calls.push(['simulate', '4663']);
          return 30_000n;
        },
        signTransaction: async (_wallet, tx, chainId) => {
          calls.push(['sign', chainId]);
          assert.equal(tx.gas, '36000');
          return '0xsigned';
        },
        broadcastTransaction: async (_client, payload) => {
          calls.push(['broadcast', payload.chainIndex]);
          return { txHash: '0x' + '5'.repeat(64), orderId: 'rh-order' };
        },
      },
    });
    assert.equal(result.state, 'buy_broadcasted');
    assert.equal(result.chain_id, ROBINHOOD_CHAIN_ID);
    assert.deepEqual(calls, [['quote', '4663'], ['simulate', '4663'], ['sign', 4663], ['broadcast', '4663']]);
  });

  it('reports the exact live buy stage when token balance lookup fails', async () => {
    await assert.rejects(() => executeBuy({
      env: {
        ...SECRET_ENV,
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
      },
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET } }),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } }),
        nativeBalance: async () => '2000000000000000',
        tokenBalance: async () => { throw new Error('upstream timeout'); },
      },
    }), { message: 'buy_balance_before_failed' });
  });

  it('does not request a live quote when the selected chain has no native gas balance', async () => {
    let quoted = false;
    await assert.rejects(() => executeBuy({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'ASHIBA', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(64) },
      amountNativeAtomic: '250000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET }, chain: executionChain(ROBINHOOD_ENV) }),
        nativeBalance: async () => '0',
        getSwapData: async () => { quoted = true; },
      },
    }), { message: 'insufficient_native_balance' });
    assert.equal(quoted, false);
  });

  it('broadcasts Robinhood swaps through its RPC instead of the unsupported OKX gateway', async () => {
    const txHash = '0x' + 'a'.repeat(64);
    const calls = [];
    const balances = ['0', '1000'];
    const result = await executeBuy({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'ASHIBA', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(64) },
      amountNativeAtomic: '250000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({
          client: { dex: { broadcastTransaction: async () => { throw new Error('okx_gateway_should_not_run'); } } },
          wallet: {
            address: WALLET,
            provider: {
              estimateGas: async () => 21_000n,
              broadcastTransaction: async (signedTx) => {
                calls.push(['rpc_broadcast', signedTx]);
                return { hash: txHash };
              },
            },
          },
          chain: executionChain(ROBINHOOD_ENV),
        }),
        nativeBalance: async () => '1000000000000000',
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } }),
        signTransaction: async () => '0xsigned',
        tokenBalance: async () => balances.shift(),
        waitTransaction: async (_wallet, hash) => calls.push(['wait', hash]),
      },
    });

    assert.equal(result.state, 'buy_confirmed');
    assert.equal(result.buy_tx, txHash);
    assert.equal(result.order_id, null);
    assert.deepEqual(calls, [['rpc_broadcast', '0xsigned'], ['wait', txHash]]);
  });

  it('classifies a nested Robinhood RPC broadcast rejection', async () => {
    const rpcError = Object.assign(new Error('could not coalesce error'), {
      info: { error: { code: -32000, message: 'intrinsic gas too low: have 230400, want 333286' } },
    });
    await assert.rejects(() => executeBuy({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'ASHIBA', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(64) },
      amountNativeAtomic: '250000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET }, chain: executionChain(ROBINHOOD_ENV) }),
        nativeBalance: async () => '1000000000000000',
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } }),
        simulateTransaction: async () => 21_000n,
        tokenBalance: async () => '0',
        signTransaction: async () => '0xsigned',
        broadcastTransaction: async () => { throw rpcError; },
      },
    }), (error) => {
      assert.equal(error.message, 'buy_broadcast_failed');
      assert.equal(error.detail_reason, 'rpc_gas_limit_rejected');
      assert.equal(error.rpc_code, -32000);
      assert.equal(error.rpc_message, 'intrinsic gas too low: have 230400, want 333286');
      return true;
    });
  });

  it('signs a Robinhood gas-price quote as a legacy transaction', async () => {
    let signedRequest = null;
    const result = await runRoundTrip({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'ASHIBA', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(64) },
      amountNativeAtomic: '250000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({
          client: { dex: {} },
          wallet: {
            address: WALLET,
            provider: { getTransactionCount: async () => 0 },
            signTransaction: async (request) => {
              signedRequest = request;
              return '0xsigned';
            },
          },
          chain: executionChain(ROBINHOOD_ENV),
        }),
        nativeBalance: async () => '1000000000000000',
        getSwapData: async () => ({
          tx: {
            to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000',
            gasPrice: '247846000', maxPriorityFeePerGas: '10',
          },
        }),
        simulateTransaction: async () => 21_000n,
        broadcastTransaction: async () => ({ txHash: '0x' + '5'.repeat(64), orderId: null }),
      },
    });

    assert.equal(result.state, 'buy_broadcasted');
    assert.equal(signedRequest.type, 0);
    assert.equal(signedRequest.gasPrice, '247846000');
    assert.equal(signedRequest.maxPriorityFeePerGas, undefined);
  });

  it('does not sign a BSC swap that fails the pre-broadcast simulation', async () => {
    let signed = false;
    let broadcasted = false;
    await assert.rejects(() => executeBuy({
      env: { ...SECRET_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET } }),
        nativeBalance: async () => '2000000000000000',
        tokenBalance: async () => '0',
        getSwapData: async () => ({
          tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' },
        }),
        simulateTransaction: async () => { throw new Error('adaptor call failed'); },
        signTransaction: async () => { signed = true; return '0xsigned'; },
        broadcastTransaction: async () => { broadcasted = true; return { txHash: '0x' + '5'.repeat(64) }; },
      },
    }), { message: 'buy_simulation_failed' });
    assert.equal(signed, false);
    assert.equal(broadcasted, false);
  });

  it('does not sign a Robinhood swap that fails the pre-broadcast simulation', async () => {
    let signed = false;
    await assert.rejects(() => executeBuy({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'ASHIBA', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(64) },
      amountNativeAtomic: '250000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET }, chain: executionChain(ROBINHOOD_ENV) }),
        nativeBalance: async () => '1000000000000000',
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } }),
        simulateTransaction: async () => { throw new Error('execution reverted: Min return not reached'); },
        tokenBalance: async () => '0',
        signTransaction: async () => { signed = true; },
      },
    }), { message: 'buy_simulation_failed' });
    assert.equal(signed, false);
  });

  it('does not sign a Robinhood sell that fails the pre-broadcast simulation', async () => {
    let signed = false;
    await assert.rejects(() => executeSell({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      position: { symbol: 'ASHIBA', contract_address: TOKEN, remaining_atomic: '1000' },
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET }, chain: executionChain(ROBINHOOD_ENV) }),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '0', gas: '21000', gasPrice: '1' } }),
        executeApproval: async () => ({ alreadyApproved: true }),
        simulateTransaction: async () => { throw new Error('execution reverted: Min return not reached'); },
        signTransaction: async () => { signed = true; },
      },
    }), { message: 'sell_simulation_failed' });
    assert.equal(signed, false);
  });

  it('reports the exact live sell stage when the swap quote fails', async () => {
    await assert.rejects(() => executeSell({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      position: { symbol: 'ASHIBA', contract_address: TOKEN, remaining_atomic: '1000' },
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET }, chain: executionChain(ROBINHOOD_ENV) }),
        getSwapData: async () => { throw new Error('upstream timeout'); },
      },
    }), { message: 'sell_quote_failed' });
  });

  it('measures confirmed buy spend and gas from native wallet settlement', async () => {
    const nativeBalances = ['2000000000000000', '999000000000000'];
    const tokenBalances = ['0', '1000'];
    const result = await executeBuy({
      env: { ...SECRET_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET } }),
        nativeBalance: async () => nativeBalances.shift(),
        tokenBalance: async () => tokenBalances.shift(),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } }),
        signTransaction: async () => '0xsigned',
        broadcastTransaction: async () => ({ txHash: '0x' + '5'.repeat(64), orderId: 'order-1' }),
        waitTransaction: async () => ({ status: 1, gasUsed: 21_000n, gasPrice: 2n }),
      },
    });

    assert.equal(result.native_spent_atomic, '1001000000000000');
    assert.equal(result.gas_native_atomic, '42000');
  });

  it('measures confirmed sell proceeds net of approval and swap gas', async () => {
    const nativeBalances = ['1000000000000000', '1800000000000000'];
    const receipts = [
      { status: 1, gasUsed: 10n, gasPrice: 1n },
      { status: 1, gasUsed: 10n, gasPrice: 2n },
    ];
    const result = await executeSell({
      env: { ...SECRET_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      position: { symbol: 'MEME', contract_address: TOKEN, remaining_atomic: '1000' },
      tokenAmountAtomic: '1000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET } }),
        nativeBalance: async () => nativeBalances.shift(),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '0', gas: '21000', gasPrice: '1' } }),
        executeApproval: async () => ({ transactionHash: '0x' + 'a'.repeat(64) }),
        simulateTransaction: async () => 21_000n,
        signTransaction: async () => '0xsigned',
        broadcastTransaction: async () => ({ txHash: '0x' + 'b'.repeat(64), orderId: 'sell-1' }),
        waitTransaction: async () => receipts.shift(),
      },
    });

    assert.equal(result.net_native_received_atomic, '800000000000000');
    assert.equal(result.gas_native_atomic, '30');
    assert.equal(result.native_received_atomic, '800000000000030');
  });

  it('reports the actual token debit when a sell settles short of the request', async () => {
    const tokenBalances = ['1500', '600'];
    const result = await executeSell({
      env: { ...SECRET_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      position: { symbol: 'MEME', contract_address: TOKEN, remaining_atomic: '1000' },
      tokenAmountAtomic: '1000', live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET } }),
        tokenBalance: async () => tokenBalances.shift(),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '0', gas: '21000', gasPrice: '1' } }),
        executeApproval: async () => ({ alreadyApproved: true }),
        simulateTransaction: async () => 21_000n,
        signTransaction: async () => '0xsigned',
        broadcastTransaction: async () => ({ txHash: '0x' + 'b'.repeat(64), orderId: 'sell-1' }),
        waitTransaction: async () => ({ status: 1 }),
      },
    });

    assert.equal(result.requested_sold_atomic, '1000');
    assert.equal(result.sold_atomic, '900');
  });

  it('uses local RPC approval for a Robinhood sell', async () => {
    const calls = [];
    const tokenBalances = ['1000', '0'];
    const approvalHash = '0x' + 'a'.repeat(64);
    const sellHash = '0x' + 'b'.repeat(64);
    const result = await executeSell({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      position: { symbol: 'ASHIBA', contract_address: TOKEN, remaining_atomic: '1000' },
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({
          client: { dex: { executeApproval: async () => { throw new Error('sdk_approval_should_not_run'); } } },
          wallet: { address: WALLET },
          chain: executionChain(ROBINHOOD_ENV),
        }),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '0', gas: '21000', gasPrice: '1' } }),
        getApprovalTarget: async () => '0x' + '6'.repeat(40),
        tokenAllowance: async () => '0',
        tokenBalance: async () => tokenBalances.shift(),
        sendApprovalTransaction: async (_wallet, token, spender, amount) => {
          calls.push(['approve', token, spender, amount]);
          return { transactionHash: approvalHash };
        },
        simulateTransaction: async () => 21_000n,
        signTransaction: async () => '0xsigned',
        broadcastTransaction: async () => ({ txHash: sellHash, orderId: null }),
        waitTransaction: async (_wallet, hash) => calls.push(['wait', hash]),
      },
    });

    assert.equal(result.state, 'sell_confirmed');
    assert.equal(result.approval_tx, approvalHash);
    assert.deepEqual(calls[0], ['approve', TOKEN.toLowerCase(), '0x' + '6'.repeat(40), '1000']);
    assert.equal(calls.some((item) => item[0] === 'wait' && item[1] === approvalHash), true);
  });

  it('buffers the Robinhood approval gas price above the provider estimate', async () => {
    let approvalRequest = null;
    class FakeContract {
      async allowance() { return 0n; }
    }
    class FakeInterface {
      encodeFunctionData() { return '0xabcdef'; }
    }
    const wallet = {
      address: WALLET,
      provider: {
        estimateGas: async () => 50_000n,
        getFeeData: async () => ({ gasPrice: 100n }),
        getTransactionCount: async () => 7,
        broadcastTransaction: async () => ({ hash: '0x' + 'c'.repeat(64) }),
      },
      signTransaction: async (request) => {
        approvalRequest = request;
        return '0xapproval';
      },
    };

    await executeSell({
      env: { ...ROBINHOOD_ENV, OKX_LIVE_ENABLED: '1', OKX_ALLOW_AUTOMATED_TRADES: '1' },
      position: { symbol: 'ASHIBA', contract_address: TOKEN, remaining_atomic: '1000' },
      live: true,
      deps: {
        loadSdk: async () => ({
          OKXDexClient: class {},
          createEVMWallet() {},
          ethers: { Contract: FakeContract, Interface: FakeInterface },
        }),
        createClient: () => ({
          client: { dex: { getChainData: async () => ({ data: [{ dexTokenApproveAddress: '0x' + '6'.repeat(40) }] }) } },
          wallet,
          chain: executionChain(ROBINHOOD_ENV),
        }),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '0', gas: '21000', gasPrice: '1' } }),
        simulateTransaction: async () => 21_000n,
        signTransaction: async () => '0xsell',
        broadcastTransaction: async () => ({ txHash: '0x' + 'd'.repeat(64), orderId: null }),
        waitTransaction: async () => ({ status: 1 }),
      },
    });

    assert.equal(approvalRequest.gasPrice, 150n);
    assert.equal(approvalRequest.gasLimit, 60_000n);
  });

  it('does not sign or broadcast unless live gates are explicit', async () => {
    let broadcasted = false;
    const result = await runRoundTrip({
      env: SECRET_ENV,
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000000000000000',
      live: false,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ dex: {} }),
        getSwapData: async () => ({ tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } }),
        signTransaction: async () => {
          throw new Error('sign_should_not_run');
        },
        broadcastTransaction: async () => {
          broadcasted = true;
        },
      },
    });
    assert.equal(result.state, 'dry_run_ready');
    assert.equal(broadcasted, false);
  });

  it('uses OKX BSC swap data and broadcasts only under live gates', async () => {
    const calls = [];
    const result = await runRoundTrip({
      env: {
        ...SECRET_ENV,
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
      },
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000000000000000',
      live: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ dex: {} }),
        nativeBalance: async () => '2000000000000000',
        getSwapData: async (_client, params) => {
          calls.push(['swapData', params]);
          return { tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } };
        },
        signTransaction: async (tx) => {
          calls.push(['sign', tx]);
          return '0xsigned';
        },
        broadcastTransaction: async (_client, payload) => {
          calls.push(['broadcast', payload]);
          return { txHash: '0x' + '5'.repeat(64), orderId: 'order-1' };
        },
      },
    });
    assert.equal(result.state, 'buy_broadcasted');
    assert.equal(calls[0][1].chainIndex, String(BSC_CHAIN_ID));
    assert.equal(calls[0][1].fromTokenAddress, NATIVE_EVM_TOKEN);
    assert.equal(calls[0][1].toTokenAddress, TOKEN.toLowerCase());
    assert.equal(calls[2][1].chainIndex, String(BSC_CHAIN_ID));
  });

  it('can run a live round trip by selling only the received token delta', async () => {
    const calls = [];
    const balances = ['100', '5100', '5100'];
    const result = await runRoundTrip({
      env: {
        ...SECRET_ENV,
        OKX_LIVE_ENABLED: '1',
        OKX_ALLOW_AUTOMATED_TRADES: '1',
      },
      candidate: { symbol: 'MEME', contract_address: TOKEN, pool_address: '0x' + '3'.repeat(40) },
      amountNativeAtomic: '1000000000000000',
      live: true,
      roundTrip: true,
      deps: {
        loadSdk: async () => ({ OKXDexClient: class {}, createEVMWallet() {} }),
        createClient: () => ({ client: { dex: {} }, wallet: { address: WALLET } }),
        nativeBalance: async () => '2000000000000000',
        getSwapData: async (_client, params) => {
          calls.push(['swapData', params.fromTokenAddress, params.toTokenAddress, params.amount]);
          return { tx: { to: '0x' + '4'.repeat(40), data: '0xabc', value: '1', gas: '21000', gasPrice: '1' } };
        },
        signTransaction: async () => '0xsigned',
        broadcastTransaction: async (_client, payload) => {
          const txHash = '0x' + String(calls.length).repeat(64);
          calls.push(['broadcast', payload.chainIndex]);
          return { txHash, orderId: `order-${calls.length}` };
        },
        waitTransaction: async (hash) => {
          calls.push(['wait', hash]);
          return { status: 1 };
        },
        tokenBalance: async () => balances.shift(),
        executeApproval: async (_client, params) => {
          calls.push(['approve', params.tokenContractAddress, params.approveAmount]);
          return { transactionHash: '0x' + 'a'.repeat(64) };
        },
      },
    });
    assert.equal(result.state, 'round_trip_completed');
    assert.equal(result.bought_atomic, '5000');
    assert.equal(calls.some((c) => c[0] === 'approve' && c[2] === '5000'), true);
    assert.equal(calls.some((c) => c[0] === 'swapData' && c[1] === TOKEN.toLowerCase() && c[2] === NATIVE_EVM_TOKEN), true);
  });
});
