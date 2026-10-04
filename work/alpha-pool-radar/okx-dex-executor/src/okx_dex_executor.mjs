import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const BSC_CHAIN_ID = 56;
export const ROBINHOOD_CHAIN_ID = 4663;
export const NATIVE_EVM_TOKEN = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const POOL_ID = /^0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/;
const UINT = /^[0-9]{1,78}$/;
const CHAINS = Object.freeze({
  bsc: Object.freeze({ name: 'bsc', chainId: BSC_CHAIN_ID }),
  robinhood: Object.freeze({ name: 'robinhood', chainId: ROBINHOOD_CHAIN_ID }),
});
const SECRET_NAMES = new Set([
  'apikey',
  'privatekey',
  'secret',
  'secretkey',
  'signature',
  'rawtransaction',
  'authorization',
  'okaccesssign',
  'okaccesssecretkey',
  'okaccesspassphrase',
  'passphrase',
  'mnemonic',
  'seed',
]);

function cleanKey(key) {
  return String(key).replace(/[-_]/g, '').toLowerCase();
}

export function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') {
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      if (!SECRET_NAMES.has(cleanKey(key))) result[key] = redact(item);
    }
    return result;
  }
  return value;
}

function publicError(code) {
  const text = String(code || '');
  return /^[a-z][a-z0-9_]{0,100}$/.test(text) ? text : 'executor_error';
}

function publicOkxText(value) {
  const text = String(value ?? '').replace(/[\r\n\t]/g, ' ').trim();
  return text ? text.slice(0, 200) : undefined;
}

function proxyUrl(env) {
  return envText(env, 'HTTPS_PROXY') || envText(env, 'HTTP_PROXY') || envText(env, 'ALL_PROXY');
}

function publicProxyUrl(value) {
  try {
    const url = new URL(value);
    url.username = '';
    url.password = '';
    return url.toString();
  } catch {
    return undefined;
  }
}

export async function configureFetchProxy(env = process.env, deps = {}) {
  const rawProxy = proxyUrl(env);
  if (!rawProxy) return { enabled: false };
  let parsed;
  try {
    parsed = new URL(rawProxy);
  } catch {
    throw new Error('invalid_fetch_proxy');
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('invalid_fetch_proxy');
  const importUndici = deps.importUndici || (() => import('undici'));
  const { ProxyAgent, setGlobalDispatcher } = await importUndici();
  setGlobalDispatcher(new ProxyAgent(rawProxy));
  return { enabled: true, proxy: publicProxyUrl(rawProxy) };
}

function okxAuthFailure(source) {
  const data = source?.response?.data || source?.data || {};
  const payload = {
    ready: false,
    live_started: false,
    provider: 'okx-dex-sdk',
    reason: 'okx_project_auth_failed',
  };
  const status = source?.response?.status ?? source?.status;
  const code = data?.code ?? source?.code;
  const msg = data?.msg ?? data?.message ?? source?.message;
  if (Number.isInteger(status)) payload.okx_status = status;
  const safeCode = publicOkxText(code);
  const safeMsg = publicOkxText(msg);
  if (safeCode) payload.okx_code = safeCode;
  if (safeMsg) payload.okx_msg = safeMsg;
  return redact(payload);
}

function envText(env, name) {
  const value = env?.[name];
  return typeof value === 'string' ? value.trim() : '';
}

function passphrase(env) {
  return envText(env, 'OKX_API_PASSPHRASE') || envText(env, 'OKX_PASSPHRASE');
}

function requireAddress(value, code) {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!ADDRESS.test(text) || /^0x0{40}$/i.test(text)) throw new Error(code);
  return text.toLowerCase();
}

function requirePoolId(value, code) {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!POOL_ID.test(text) || /^0x0+$/i.test(text)) throw new Error(code);
  return text.toLowerCase();
}

export function executionChain(env = process.env) {
  const name = envText(env, 'EXECUTION_CHAIN_NAME').toLowerCase() || 'bsc';
  const chain = CHAINS[name];
  if (!chain) throw new Error('unsupported_execution_chain');
  return chain;
}

function evmEnv(env, genericName, legacyName, code) {
  return envText(env, genericName) || requireEnv(env, legacyName, code);
}

function requireUint(value, code = 'invalid_amount') {
  const text = String(value ?? '').trim();
  if (!UINT.test(text) || BigInt(text) <= 0n || BigInt(text) >= (1n << 256n)) throw new Error(code);
  return text;
}

function requireEnv(env, name, code) {
  const value = envText(env, name);
  if (!value) throw new Error(code);
  return value;
}

export async function loadSdk() {
  const sdk = await import('@okx-dex/okx-dex-sdk');
  const walletModule = await import('@okx-dex/okx-dex-sdk/dist/core/evm-wallet.js');
  const ethersModule = await import('ethers');
  return { ...sdk, ...walletModule, ethers: ethersModule.ethers };
}

export async function preflight(env = process.env, deps = {}) {
  try {
    await configureFetchProxy(env, deps);
    const chain = executionChain(env);
    const wallet = requireAddress(evmEnv(env, 'EVM_WALLET_ADDRESS', 'BSC_WALLET_ADDRESS', 'missing_evm_wallet'), 'invalid_evm_wallet');
    requireEnv(env, 'OKX_API_KEY', 'missing_okx_api_key');
    requireEnv(env, 'OKX_SECRET_KEY', 'missing_okx_secret_key');
    if (!passphrase(env)) throw new Error('missing_okx_passphrase');
    requireEnv(env, 'OKX_PROJECT_ID', 'missing_okx_project_id');
    const rpcUrl = evmEnv(env, 'EVM_RPC_URL', 'BSC_RPC_URL', 'missing_evm_rpc_url');
    const privateKey = evmEnv(env, 'EVM_PRIVATE_KEY', 'BSC_PRIVATE_KEY', 'missing_evm_private_key');
    let sdk = null;
    if (deps.loadSdk !== false) {
      sdk = await (deps.loadSdk || loadSdk)();
      if (sdk?.createEVMWallet && sdk?.ethers?.JsonRpcProvider) {
        const provider = new sdk.ethers.JsonRpcProvider(rpcUrl, { name: chain.name, chainId: chain.chainId }, { staticNetwork: true });
        const derived = sdk.createEVMWallet(privateKey, provider);
        if (String(derived?.address || '').toLowerCase() !== wallet) {
          throw new Error('wallet_private_key_mismatch');
        }
      }
    }
    if (deps.remote) {
      sdk ||= await (deps.loadSdk || loadSdk)();
      const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
      let response;
      try {
        response = await created.client.dex.getChainData(String(chain.chainId));
      } catch (error) {
        return okxAuthFailure(error);
      }
      const ok = String(response?.code) === '0' && Array.isArray(response?.data);
      if (!ok) return okxAuthFailure(response);
      return { ready: true, live_started: false, provider: 'okx-dex-sdk', chain: chain.name, chain_id: chain.chainId, wallet, remote_checked: true };
    }
    return { ready: true, live_started: false, provider: 'okx-dex-sdk', chain: chain.name, chain_id: chain.chainId, wallet };
  } catch (error) {
    return { ready: false, live_started: false, provider: 'okx-dex-sdk', reason: publicError(error.message) };
  }
}

export function selectStrategyCandidate(payload, chainName = 'bsc') {
  const rows = Array.isArray(payload?.signals) ? payload.signals : [];
  for (const row of rows) {
    if (String(row?.chain || '').toLowerCase() !== chainName) continue;
    const token = requireAddress(row.contract_address || row.token || row.address, 'invalid_token');
    const pool = requirePoolId(row.pool_address || row.pair_address || row.pool, 'invalid_pool');
    if (token === NATIVE_EVM_TOKEN.toLowerCase()) continue;
    return { ...row, contract_address: token, pool_address: pool, symbol: String(row.symbol || 'MEME').slice(0, 32) };
  }
  throw new Error('strategy_candidate_unavailable');
}

function createClient(env, sdk) {
  const chain = executionChain(env);
  const rpcUrl = evmEnv(env, 'EVM_RPC_URL', 'BSC_RPC_URL', 'missing_evm_rpc_url');
  const privateKey = evmEnv(env, 'EVM_PRIVATE_KEY', 'BSC_PRIVATE_KEY', 'missing_evm_private_key');
  const provider = new sdk.ethers.JsonRpcProvider(rpcUrl, { name: chain.name, chainId: chain.chainId }, { staticNetwork: true });
  const wallet = sdk.createEVMWallet(privateKey, provider);
  const expected = requireAddress(envText(env, 'EVM_WALLET_ADDRESS') || envText(env, 'BSC_WALLET_ADDRESS') || wallet.address, 'missing_evm_wallet');
  if (wallet.address.toLowerCase() !== expected) throw new Error('wallet_private_key_mismatch');
  const client = new sdk.OKXDexClient({
    apiKey: requireEnv(env, 'OKX_API_KEY', 'missing_okx_api_key'),
    secretKey: requireEnv(env, 'OKX_SECRET_KEY', 'missing_okx_secret_key'),
    apiPassphrase: passphrase(env),
    projectId: requireEnv(env, 'OKX_PROJECT_ID', 'missing_okx_project_id'),
    evm: { wallet },
  });
  return { client, wallet, chain };
}

async function defaultGetSwapData(client, params) {
  const response = await client.dex.getSwapData(params);
  const row = response?.data?.[0];
  if (!row?.tx) throw new Error('okx_swap_data_unavailable');
  return row;
}

async function defaultSignTransaction(wallet, tx, chainId) {
  const nonce = await wallet.provider.getTransactionCount(wallet.address, 'pending');
  const request = {
    to: tx.to,
    value: tx.value ?? '0',
    data: tx.data,
    gasLimit: tx.gas,
    nonce,
    chainId,
  };
  if (tx.gasPrice != null) {
    request.type = 0;
    request.gasPrice = tx.gasPrice;
  } else {
    request.type = 2;
    request.maxFeePerGas = tx.maxFeePerGas;
    request.maxPriorityFeePerGas = tx.maxPriorityFeePerGas;
  }
  return wallet.signTransaction(request);
}

async function defaultBroadcastTransaction(client, payload) {
  const response = await client.dex.broadcastTransaction(payload);
  const row = response?.data?.[0];
  if (!row?.txHash) throw new Error('okx_broadcast_failed');
  return { txHash: row.txHash, orderId: row.orderId || null };
}

async function defaultRpcBroadcastTransaction(wallet, payload) {
  const response = await wallet.provider.broadcastTransaction(payload.signedTx);
  if (!response?.hash) throw new Error('rpc_broadcast_failed');
  return { txHash: response.hash, orderId: null };
}

async function defaultBscBroadcastTransaction(client, wallet, payload) {
  try {
    return await defaultBroadcastTransaction(client, payload);
  } catch {
    return defaultRpcBroadcastTransaction(wallet, payload);
  }
}

async function defaultSimulateTransaction(wallet, tx) {
  const request = {
    from: wallet.address,
    to: tx.to,
    value: tx.value ?? '0',
    data: tx.data,
  };
  if (tx.gasPrice != null) {
    request.gasPrice = tx.gasPrice;
  } else {
    if (tx.maxFeePerGas != null) request.maxFeePerGas = tx.maxFeePerGas;
    if (tx.maxPriorityFeePerGas != null) request.maxPriorityFeePerGas = tx.maxPriorityFeePerGas;
  }
  return wallet.provider.estimateGas(request);
}

async function simulateTransactionFor(chain, wallet, tx, deps, code) {
  if (!deps.simulateTransaction && typeof wallet?.provider?.estimateGas !== 'function') return tx;
  const simulateTransaction = deps.simulateTransaction || defaultSimulateTransaction;
  const estimate = await executionStep(code, () => simulateTransaction(wallet, tx, chain.chainId));
  if (estimate == null) return tx;
  const quotedGas = BigInt(tx.gas ?? tx.gasLimit ?? '0');
  const bufferedGas = (BigInt(estimate) * 120n + 99n) / 100n;
  return { ...tx, gas: (bufferedGas > quotedGas ? bufferedGas : quotedGas).toString() };
}

function broadcastTransactionFor(chain, wallet, deps) {
  if (deps.broadcastTransaction) return deps.broadcastTransaction;
  if (chain.name === 'robinhood') {
    return (_client, payload) => defaultRpcBroadcastTransaction(wallet, payload);
  }
  return (client, payload) => defaultBscBroadcastTransaction(client, wallet, payload);
}

const ERC20_ABI = [
  'function balanceOf(address owner) view returns (uint256)',
  'function allowance(address owner, address spender) view returns (uint256)',
  'function approve(address spender, uint256 amount) returns (bool)',
];

async function defaultWaitTransaction(wallet, txHash) {
  const receipt = await wallet.provider.waitForTransaction(txHash, 1, 120000);
  if (!receipt || receipt.status !== 1) throw new Error('transaction_receipt_failed');
  return receipt;
}

async function tokenBalance(wallet, token, sdk) {
  const contract = new sdk.ethers.Contract(token, ERC20_ABI, wallet.provider);
  const balance = await contract.balanceOf(wallet.address);
  return balance.toString();
}

export async function readWalletTokenBalance({ env = process.env, token, walletAddress, deps = {} }) {
  const sdk = await (deps.loadSdk || loadSdk)();
  const chain = executionChain(env);
  const rpcUrl = evmEnv(env, 'EVM_RPC_URL', 'BSC_RPC_URL', 'missing_evm_rpc_url');
  const owner = requireAddress(
    walletAddress || envText(env, 'EVM_WALLET_ADDRESS') || envText(env, 'BSC_WALLET_ADDRESS'),
    'missing_evm_wallet',
  );
  const provider = deps.provider || new sdk.ethers.JsonRpcProvider(
    rpcUrl,
    { name: chain.name, chainId: chain.chainId },
    { staticNetwork: true },
  );
  return tokenBalance({ address: owner, provider }, requireAddress(token, 'invalid_token'), sdk);
}

async function defaultExecuteApproval(client, params) {
  const result = await client.dex.executeApproval(params);
  if (!result?.transactionHash && !result?.alreadyApproved) throw new Error('approval_failed');
  return result;
}

async function defaultGetApprovalTarget(client, chainId) {
  const response = await client.dex.getChainData(String(chainId));
  return requireAddress(response?.data?.[0]?.dexTokenApproveAddress, 'approval_target_unavailable');
}

async function defaultTokenAllowance(wallet, token, spender, sdk) {
  const contract = new sdk.ethers.Contract(token, ERC20_ABI, wallet.provider);
  return (await contract.allowance(wallet.address, spender)).toString();
}

async function approvalRequiredForQuote(client, wallet, chain, token, amount, sdk, deps) {
  if (typeof deps.approvalRequired === 'function') {
    return deps.approvalRequired({ chain, token, amount, wallet: wallet.address });
  }
  if (!wallet?.provider || !sdk?.ethers?.Contract) return true;
  const target = requireAddress(
    await (deps.getApprovalTarget || defaultGetApprovalTarget)(client, chain.chainId),
    'approval_target_unavailable',
  );
  const allowance = BigInt(await (deps.tokenAllowance || defaultTokenAllowance)(wallet, token, target, sdk));
  return allowance < BigInt(amount);
}

async function defaultSendApprovalTransaction(wallet, token, spender, amount, chain, sdk, onExecutionProgress) {
  const data = new sdk.ethers.Interface(ERC20_ABI).encodeFunctionData('approve', [spender, amount]);
  const estimate = await wallet.provider.estimateGas({ from: wallet.address, to: token, data, value: 0 });
  const feeData = await wallet.provider.getFeeData();
  if (feeData?.gasPrice == null) throw new Error('approval_fee_unavailable');
  const gasPrice = (BigInt(feeData.gasPrice) * 150n + 99n) / 100n;
  const nonce = await wallet.provider.getTransactionCount(wallet.address, 'pending');
  const signedTx = await wallet.signTransaction({
    type: 0,
    chainId: chain.chainId,
    nonce,
    to: token,
    data,
    value: 0,
    gasLimit: (BigInt(estimate) * 120n + 99n) / 100n,
    gasPrice,
  });
  await preparedProgress(onExecutionProgress, 'approval', signedTx, sdk);
  const response = await wallet.provider.broadcastTransaction(signedTx);
  if (!response?.hash) throw new Error('approval_broadcast_failed');
  return { transactionHash: response.hash };
}

async function directRpcApproval(client, params, wallet, chain, sdk, deps, onExecutionProgress) {
  const target = requireAddress(
    await (deps.getApprovalTarget || defaultGetApprovalTarget)(client, chain.chainId),
    'approval_target_unavailable',
  );
  const allowance = BigInt(await (deps.tokenAllowance || defaultTokenAllowance)(
    wallet,
    requireAddress(params.tokenContractAddress, 'invalid_token'),
    target,
    sdk,
  ));
  if (allowance >= BigInt(params.approveAmount)) return { alreadyApproved: true, transactionHash: '' };
  if (deps.sendApprovalTransaction) await executionProgress(onExecutionProgress, 'prepared', 'approval', null);
  return (deps.sendApprovalTransaction || defaultSendApprovalTransaction)(
    wallet,
    requireAddress(params.tokenContractAddress, 'invalid_token'),
    target,
    params.approveAmount,
    chain,
    sdk,
    onExecutionProgress,
  );
}

async function buildSwap(client, params, deps) {
  return (deps.getSwapData || defaultGetSwapData)(client, params);
}

function executionDiagnostic(error) {
  const rpcError = error?.info?.error || error?.error || error?.cause?.info?.error || error?.cause?.error;
  const rawMessage = rpcError?.message || error?.shortMessage || error?.reason || error?.message || '';
  const rpcMessage = String(rawMessage)
    .replace(/[\r\n\t]/g, ' ')
    .replace(/0x[0-9a-f]{40,}/gi, '[hex]')
    .trim()
    .slice(0, 200);
  const text = rpcMessage.toLowerCase();
  let detailReason = 'rpc_broadcast_rejected';
  if (/intrinsic gas|gas limit too low|exceeds allowance/.test(text)) detailReason = 'rpc_gas_limit_rejected';
  else if (/nonce too low/.test(text)) detailReason = 'rpc_nonce_too_low';
  else if (/underpriced/.test(text)) detailReason = 'rpc_transaction_underpriced';
  else if (/insufficient funds/.test(text)) detailReason = 'rpc_insufficient_funds';
  else if (/min return not reached/.test(text)) detailReason = 'rpc_min_return_not_reached';
  else if (/base fee|max fee per gas/.test(text)) detailReason = 'rpc_fee_too_low';
  else if (/invalid sender|invalid signature/.test(text)) detailReason = 'rpc_invalid_signature';
  else if (/execution reverted/.test(text)) detailReason = 'rpc_execution_reverted';
  const rpcCode = Number(rpcError?.code ?? error?.code);
  return {
    detail_reason: detailReason,
    ...(Number.isFinite(rpcCode) ? { rpc_code: rpcCode } : {}),
    ...(rpcMessage ? { rpc_message: rpcMessage } : {}),
  };
}

class ExecutionProgressError extends Error {
  constructor() { super('execution_progress_failed'); }
}

async function executionProgress(callback, stage, side, txHash, evidence = {}) {
  if (callback === undefined) return;
  try { await callback({ stage, side, ...(txHash ? { tx_hash: txHash } : {}), ...evidence }); }
  catch { throw new ExecutionProgressError(); }
}

async function preparedProgress(callback, side, signedTx, sdk, evidence = {}) {
  if (callback === undefined) return;
  const hash = (sdk.ethers?.keccak256 || (await import('ethers')).keccak256)(signedTx);
  await executionProgress(callback, 'prepared', side, hash, evidence);
}

async function executionStep(code, action) {
  try {
    return await action();
  } catch (error) {
    if (error instanceof ExecutionProgressError) throw error;
    const wrapped = new Error(code);
    wrapped.cause = error;
    Object.assign(wrapped, executionDiagnostic(error));
    throw wrapped;
  }
}

function receiptFeeAtomic(receipt) {
  if (!receipt || typeof receipt !== 'object') return null;
  try {
    if (receipt.fee !== undefined && receipt.fee !== null) return BigInt(receipt.fee);
    const gasUsed = receipt.gasUsed;
    const gasPrice = receipt.gasPrice ?? receipt.effectiveGasPrice;
    if (gasUsed !== undefined && gasUsed !== null && gasPrice !== undefined && gasPrice !== null) {
      return BigInt(gasUsed) * BigInt(gasPrice);
    }
  } catch {
    return null;
  }
  return null;
}

function swapSlippageParams(env, chain) {
  const configured = envText(env, 'OKX_SLIPPAGE_PERCENT') || '1';
  if (chain.name === 'robinhood' && Number(configured) > 1) {
    return {
      slippagePercent: '1',
      autoSlippage: true,
      maxAutoSlippagePercent: configured,
    };
  }
  return { slippagePercent: configured, autoSlippage: false };
}

function economicGuardEnabled(env) {
  return envText(env, 'OKX_ECONOMIC_GUARD_ENABLED') !== '0';
}

function configuredPercent(env, name, fallback) {
  const value = Number(envText(env, name) || fallback);
  if (!Number.isFinite(value) || value <= 0 || value > 100) throw new Error('buy_economics_unavailable');
  return value;
}

function quoteRouter(swap) {
  const router = swap?.routerResult;
  if (!router || typeof router !== 'object') throw new Error('buy_economics_unavailable');
  return router;
}

function quoteImpact(router) {
  const value = Number(router?.priceImpactPercent);
  if (!Number.isFinite(value)) throw new Error('buy_economics_unavailable');
  return Math.max(0, value);
}

function truthyTokenFlag(value) {
  return value === true || String(value || '').trim().toLowerCase() === 'true';
}

function percentDifference(input, output) {
  const scaled = (input - output) * 1_000_000n / input;
  return Number(scaled) / 10_000;
}

function estimatedSwapGasAtomic(swap, { required = false } = {}) {
  try {
    const gasValue = swap?.tx?.gas;
    const gasPriceValue = swap?.tx?.gasPrice ?? swap?.tx?.maxFeePerGas;
    if (required && (gasValue === undefined || gasPriceValue === undefined)) {
      throw new Error('buy_economics_unavailable');
    }
    const gas = BigInt(String(gasValue ?? '0'));
    const gasPrice = BigInt(String(gasPriceValue ?? '0'));
    if (gas <= 0n || gasPrice <= 0n) {
      if (required) throw new Error('buy_economics_unavailable');
      return 0n;
    }
    return gas * gasPrice;
  } catch (error) {
    if (required) throw new Error('buy_economics_unavailable', { cause: error });
    return 0n;
  }
}

function estimatedApprovalGasAtomic(sellSwap, env) {
  try {
    const unitsText = envText(env, 'OKX_ESTIMATED_APPROVAL_GAS_UNITS') || '65000';
    if (!/^\d{1,12}$/.test(unitsText)) throw new Error();
    const units = BigInt(unitsText);
    const gasPriceValue = sellSwap?.tx?.gasPrice ?? sellSwap?.tx?.maxFeePerGas;
    if (units <= 0n || gasPriceValue === undefined) throw new Error();
    const gasPrice = BigInt(String(gasPriceValue));
    if (gasPrice <= 0n) throw new Error();
    return units * gasPrice;
  } catch (error) {
    throw new Error('buy_economics_unavailable', { cause: error });
  }
}

export function assessRoundTripEconomics({ env = process.env, amountNativeAtomic, buySwap, sellSwap, approvalRequired = true }) {
  const input = BigInt(requireUint(amountNativeAtomic));
  const buyRouter = quoteRouter(buySwap);
  const sellRouter = quoteRouter(sellSwap);
  const expectedToken = BigInt(requireUint(buyRouter.toTokenAmount, 'buy_economics_unavailable'));
  const returnedNative = BigInt(requireUint(sellRouter.toTokenAmount, 'buy_economics_unavailable'));
  const buyGas = estimatedSwapGasAtomic(buySwap, { required: true });
  const sellGas = estimatedSwapGasAtomic(sellSwap, { required: true });
  const approvalGas = approvalRequired ? estimatedApprovalGasAtomic(sellSwap, env) : 0n;
  const estimatedGas = buyGas + sellGas + approvalGas;
  const estimatedNetNative = returnedNative > estimatedGas ? returnedNative - estimatedGas : 0n;
  if (truthyTokenFlag(buyRouter?.toToken?.isHoneyPot) || truthyTokenFlag(sellRouter?.fromToken?.isHoneyPot)) {
    throw new Error('buy_honeypot_detected');
  }
  const buyImpact = quoteImpact(buyRouter);
  const sellImpact = quoteImpact(sellRouter);
  const chain = executionChain(env);
  const configuredImpact = configuredPercent(env, 'OKX_MAX_PRICE_IMPACT_PERCENT', '12');
  const hardBuyImpact = chain.name === 'robinhood' ? 12 : null;
  const hardSellImpact = chain.name === 'bsc' ? 12 : null;
  const maxBuyImpact = hardBuyImpact === null ? configuredImpact : Math.min(configuredImpact, hardBuyImpact);
  const maxSellImpact = hardSellImpact === null ? configuredImpact : Math.min(configuredImpact, hardSellImpact);
  if (buyImpact > maxBuyImpact) throw new Error('buy_price_impact_exceeded');
  if (sellImpact > maxSellImpact) throw new Error('sell_price_impact_exceeded');
  const lossPercent = percentDifference(input, estimatedNetNative);
  const hardLoss = 25;
  const maxLoss = Math.min(configuredPercent(env, 'OKX_MAX_ROUND_TRIP_LOSS_PERCENT', String(hardLoss)), hardLoss);
  if (lossPercent > maxLoss) throw new Error('buy_round_trip_loss_exceeded');
  return {
    pretrade_expected_token_atomic: expectedToken.toString(),
    pretrade_round_trip_native_atomic: returnedNative.toString(),
    pretrade_round_trip_net_native_atomic: estimatedNetNative.toString(),
    pretrade_round_trip_gas_native_atomic: estimatedGas.toString(),
    pretrade_buy_gas_native_atomic: buyGas.toString(),
    required_native_balance_atomic: (input + estimatedGas).toString(),
    pretrade_approval_gas_native_atomic: approvalGas.toString(),
    pretrade_round_trip_loss_percent: lossPercent,
    buy_price_impact_percent: buyImpact,
    sell_price_impact_percent: sellImpact,
  };
}

async function validateBuyEconomics(client, params, buySwap, env, chain, deps, wallet, sdk) {
  const buyRouter = quoteRouter(buySwap);
  const expectedToken = requireUint(buyRouter.toTokenAmount, 'buy_economics_unavailable');
  let sellSwap;
  try {
    sellSwap = await buildSwap(client, {
      chainIndex: String(chain.chainId),
      fromTokenAddress: params.toTokenAddress,
      toTokenAddress: NATIVE_EVM_TOKEN,
      amount: expectedToken,
      ...swapSlippageParams(env, chain),
      userWalletAddress: params.userWalletAddress,
      swapReceiverAddress: params.swapReceiverAddress,
    }, deps);
  } catch (error) {
    const wrapped = new Error('buy_economics_unavailable');
    wrapped.cause = error;
    throw wrapped;
  }
  const approvalRequired = await approvalRequiredForQuote(
    client, wallet, chain, params.toTokenAddress, expectedToken, sdk, deps,
  );
  const economics = assessRoundTripEconomics({ env, amountNativeAtomic: params.amount, buySwap, sellSwap, approvalRequired });
  const buyRoute = quoteRouter(buySwap);
  const sellRoute = quoteRouter(sellSwap);
  const priceFields = [
    buyRoute?.toTokenUsdPrice,
    buyRoute?.toTokenPrice,
    buyRoute?.toToken?.tokenUnitPrice,
    buyRoute?.toToken?.usdPrice,
    buyRoute?.toToken?.price,
  ];
  const exactBuyPriceUsd = priceFields.map(Number).find(value => Number.isFinite(value) && value > 0);
  return {
    ...economics,
    ...(exactBuyPriceUsd ? { exact_buy_price_usd: exactBuyPriceUsd } : {}),
    buy_route_id: String(buyRoute.router || buyRoute.routeId || buyRoute.dexRouterList?.[0]?.dexProtocol?.[0]?.dexName || ''),
    sell_route_id: String(sellRoute.router || sellRoute.routeId || sellRoute.dexRouterList?.[0]?.dexProtocol?.[0]?.dexName || ''),
  };
}

export async function quoteBuyTradeability({ env = process.env, candidate, amountNativeAtomic, deps = {} }) {
  const check = await (deps.preflight || preflight)(env, { loadSdk: deps.loadSdk || loadSdk });
  if (!check?.ready) throw new Error('buy_economics_unavailable');
  const sdk = await (deps.loadSdk || loadSdk)();
  const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
  const client = created.client;
  const wallet = created.wallet || { address: check.wallet };
  const chain = created.chain || executionChain(env);
  const token = requireAddress(candidate?.contract_address, 'invalid_token');
  const pool = requirePoolId(candidate?.pool_address, 'invalid_pool');
  const amount = requireUint(amountNativeAtomic, 'invalid_amount');
  const params = {
    chainIndex: String(chain.chainId),
    fromTokenAddress: NATIVE_EVM_TOKEN,
    toTokenAddress: token,
    amount,
    ...swapSlippageParams(env, chain),
    userWalletAddress: wallet.address || check.wallet,
    swapReceiverAddress: wallet.address || check.wallet,
  };
  const buySwap = await executionStep('buy_quote_failed', () => buildSwap(client, params, deps));
  const economics = await validateBuyEconomics(client, params, buySwap, env, chain, deps, wallet, sdk);
  if (!economics.exact_buy_price_usd || !economics.buy_route_id || !economics.sell_route_id) {
    throw new Error('buy_economics_unavailable');
  }
  return {
    chain: chain.name,
    contract_address: token,
    pool_address: pool,
    quote_at: new Date().toISOString(),
    ...economics,
  };
}

export async function fetchPendingIntentEvidence({ env = process.env, intent, deps = {} }) {
  const transaction = Array.isArray(intent?.transactions)
    ? [...intent.transactions].reverse().find(item => ['broadcast', 'confirmed'].includes(item?.stage)
      && /^0x[0-9a-fA-F]{64}$/.test(item?.tx_hash || ''))
    : null;
  if (!transaction) return {};
  const check = await (deps.preflight || preflight)(env, { loadSdk: deps.loadSdk || loadSdk });
  if (!check?.ready) return {};
  const sdk = await (deps.loadSdk || loadSdk)();
  const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
  const wallet = created.wallet || { address: check.wallet };
  const provider = wallet?.provider;
  if (!provider?.getTransactionReceipt) return {};
  const receipt = await provider.getTransactionReceipt(transaction.tx_hash);
  if (!receipt) return {};
  const status = receipt.status;
  if (status === 0 || status === 0n || status === '0' || status === '0x0') return { receipt };
  const snapshot = intent?.wallet_balances_before;
  if (!snapshot || !/^[0-9]+$/.test(String(snapshot.token_atomic || ''))
      || !/^[0-9]+$/.test(String(snapshot.native_atomic || ''))) return { receipt };
  const token = requireAddress(intent.contract_address || intent?.candidate_snapshot?.contract_address, 'invalid_token');
  const currentToken = BigInt(await (deps.tokenBalance || ((currentWallet, currentToken) => tokenBalance(currentWallet, currentToken, sdk)))(wallet, token));
  const currentNative = BigInt(await (deps.nativeBalance || ((currentWallet) => currentWallet.provider.getBalance(currentWallet.address)))(wallet));
  return {
    receipt,
    wallet_deltas: {
      token_atomic: (currentToken - BigInt(snapshot.token_atomic)).toString(),
      native_atomic: (currentNative - BigInt(snapshot.native_atomic)).toString(),
    },
  };
}

export async function quoteSellValue({ env = process.env, position, tokenAmountAtomic, routePreference = null, deps = {} }) {
  const check = await (deps.preflight || preflight)(env, { loadSdk: deps.loadSdk || loadSdk });
  if (!check?.ready) throw new Error('sell_economics_unavailable');
  const sdk = await (deps.loadSdk || loadSdk)();
  const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
  const client = created.client;
  const wallet = created.wallet || { address: check.wallet };
  const chain = created.chain || executionChain(env);
  const token = requireAddress(position?.contract_address || position?.token, 'invalid_token');
  const amount = requireUint(tokenAmountAtomic || position?.remaining_atomic, 'invalid_amount');
  let swap;
  try {
    const alternateDexIds = routePreference === 'alternate' ? envText(env, 'OKX_ALTERNATE_DEX_IDS') : '';
    swap = await buildSwap(client, {
      chainIndex: String(chain.chainId),
      fromTokenAddress: token,
      toTokenAddress: NATIVE_EVM_TOKEN,
      amount,
      ...swapSlippageParams(env, chain),
      userWalletAddress: wallet.address || check.wallet,
      swapReceiverAddress: wallet.address || check.wallet,
      ...(alternateDexIds ? { dexIds: alternateDexIds } : {}),
    }, deps);
    const router = quoteRouter(swap);
    const expectedNative = BigInt(requireUint(router.toTokenAmount, 'sell_economics_unavailable'));
    const v2 = position?.strategy_version === 'chain_v2';
    const estimatedSwapGas = estimatedSwapGasAtomic(swap, { required: v2 });
    const approvalRequired = v2
      ? await approvalRequiredForQuote(client, wallet, chain, token, amount, sdk, deps)
      : false;
    const estimatedApprovalGas = approvalRequired ? estimatedApprovalGasAtomic(swap, env) : 0n;
    const estimatedGas = estimatedSwapGas + estimatedApprovalGas;
    const estimatedNetNative = expectedNative > estimatedGas ? expectedNative - estimatedGas : 0n;
    return {
      expected_native_atomic: expectedNative.toString(),
      estimated_net_native_atomic: estimatedNetNative.toString(),
      estimated_gas_native_atomic: estimatedGas.toString(),
      estimated_approval_gas_native_atomic: estimatedApprovalGas.toString(),
      price_impact_percent: quoteImpact(router),
      route_id: String(router.router || router.routeId || router.dexRouterList?.[0]?.dexProtocol?.[0]?.dexName || ''),
      quote_at: new Date().toISOString(),
      prepared_swap: swap,
    };
  } catch (error) {
    if (error?.message === 'sell_economics_unavailable') throw error;
    const wrapped = new Error('sell_economics_unavailable');
    wrapped.cause = error;
    throw wrapped;
  }
}

export async function runRoundTrip({ env = process.env, candidate, amountNativeAtomic, live = false, roundTrip = false, deps = {} }) {
  const check = await preflight(env, { loadSdk: deps.loadSdk || loadSdk });
  if (!check.ready) return { ...check, state: 'blocked' };
  const sdk = await (deps.loadSdk || loadSdk)();
  const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
  const client = created.client;
  const wallet = created.wallet || { address: check.wallet };
  const chain = created.chain || executionChain(env);
  const token = requireAddress(candidate?.contract_address, 'invalid_token');
  const amount = requireUint(amountNativeAtomic || envText(env, 'OKX_TEST_NATIVE_ATOMIC') || '1000000000000000');
  const params = {
    chainIndex: String(chain.chainId),
    fromTokenAddress: NATIVE_EVM_TOKEN,
    toTokenAddress: token,
    amount,
    ...swapSlippageParams(env, chain),
    userWalletAddress: wallet.address || check.wallet,
    swapReceiverAddress: wallet.address || check.wallet,
  };
  if (live && (envText(env, 'OKX_LIVE_ENABLED') !== '1' || envText(env, 'OKX_ALLOW_AUTOMATED_TRADES') !== '1')) {
    return { state: 'blocked', reason: 'explicit_live_environment_required', live_started: false, wallet: check.wallet };
  }
  let nativeBalanceBefore = null;
  if (live) {
    const readNativeBalance = deps.nativeBalance || ((currentWallet) => currentWallet.provider.getBalance(currentWallet.address));
    nativeBalanceBefore = BigInt(await executionStep('buy_native_balance_failed', () => readNativeBalance(wallet)));
    if (nativeBalanceBefore < BigInt(amount)) throw new Error('insufficient_native_balance');
  }
  const swap = await executionStep('buy_quote_failed', () => buildSwap(client, params, deps));
  const economics = live && (candidate?.strategy_version === 'chain_v2' || economicGuardEnabled(env))
    ? await validateBuyEconomics(client, params, swap, env, chain, deps, wallet, sdk)
    : null;
  if (live && economics && nativeBalanceBefore < BigInt(economics.required_native_balance_atomic)) {
    throw new Error('insufficient_native_gas_balance');
  }
  if (!live) {
    return {
      state: 'dry_run_ready',
      live_started: false,
      provider: 'okx-dex-sdk',
      wallet: check.wallet,
      chain: chain.name,
      chain_id: chain.chainId,
      symbol: candidate?.symbol || 'MEME',
      token,
      swap: redact({ routerResult: swap.routerResult, tx: swap.tx }),
    };
  }
  const readBalance = deps.tokenBalance || ((w, t) => tokenBalance(w, t, sdk));
  const waitTransaction = deps.waitTransaction || defaultWaitTransaction;
  const executeApproval = deps.executeApproval || (chain.name === 'robinhood'
    ? (_client, approvalParams) => directRpcApproval(client, approvalParams, wallet, chain, sdk, deps)
    : defaultExecuteApproval);
  const signTransaction = deps.signTransaction || defaultSignTransaction;
  const broadcastTransaction = broadcastTransactionFor(chain, wallet, deps);
  const before = roundTrip ? BigInt(await readBalance(wallet, token)) : 0n;
  const buyTx = await simulateTransactionFor(chain, wallet, swap.tx, deps, 'buy_simulation_failed');
  const signedTx = await signTransaction(wallet, buyTx, chain.chainId);
  const broadcast = await broadcastTransaction(client, {
    signedTx,
    chainIndex: String(chain.chainId),
    address: wallet.address || check.wallet,
    enableMevProtection: true,
  });
  if (!roundTrip) {
    return {
      state: 'buy_broadcasted',
      live_started: true,
      provider: 'okx-dex-sdk',
      wallet: check.wallet,
      chain: chain.name,
      chain_id: chain.chainId,
      symbol: candidate?.symbol || 'MEME',
      token,
      buy_tx: broadcast.txHash,
      order_id: broadcast.orderId,
    };
  }
  await waitTransaction(wallet, broadcast.txHash);
  const afterBuy = BigInt(await readBalance(wallet, token));
  const bought = afterBuy - before;
  if (bought <= 0n) throw new Error('buy_fill_not_detected');
  const approval = await executeApproval(client, {
    chainIndex: String(chain.chainId),
    tokenContractAddress: token,
    approveAmount: bought.toString(),
  });
  if (approval.transactionHash) await waitTransaction(wallet, approval.transactionHash);
  const sellSwap = await buildSwap(client, {
    chainIndex: String(chain.chainId),
    fromTokenAddress: token,
    toTokenAddress: NATIVE_EVM_TOKEN,
    amount: bought.toString(),
    ...swapSlippageParams(env, chain),
    userWalletAddress: wallet.address || check.wallet,
    swapReceiverAddress: wallet.address || check.wallet,
  }, deps);
  const sellTx = await simulateTransactionFor(chain, wallet, sellSwap.tx, deps, 'sell_simulation_failed');
  const signedSell = await signTransaction(wallet, sellTx, chain.chainId);
  const sellBroadcast = await broadcastTransaction(client, {
    signedTx: signedSell,
    chainIndex: String(chain.chainId),
    address: wallet.address || check.wallet,
    enableMevProtection: true,
  });
  await waitTransaction(wallet, sellBroadcast.txHash);
  return {
    state: 'round_trip_completed',
    live_started: true,
    provider: 'okx-dex-sdk',
    wallet: check.wallet,
    chain: chain.name,
    chain_id: chain.chainId,
    symbol: candidate?.symbol || 'MEME',
    token,
    buy_tx: broadcast.txHash,
    sell_tx: sellBroadcast.txHash,
    order_id: broadcast.orderId,
    sell_order_id: sellBroadcast.orderId,
    approval_tx: approval.transactionHash || null,
    bought_atomic: bought.toString(),
  };
}

export async function executeBuy({ env = process.env, candidate, amountNativeAtomic, live = false, deps = {}, onExecutionProgress }) {
  const check = await preflight(env, { loadSdk: deps.loadSdk || loadSdk });
  if (!check.ready) return { ...check, state: 'blocked' };
  const sdk = await (deps.loadSdk || loadSdk)();
  const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
  const client = created.client;
  const wallet = created.wallet || { address: check.wallet };
  const chain = created.chain || executionChain(env);
  const token = requireAddress(candidate?.contract_address, 'invalid_token');
  const amount = requireUint(amountNativeAtomic || envText(env, 'OKX_TRADE_NATIVE_ATOMIC') || '1000000000000000');
  const params = {
    chainIndex: String(chain.chainId),
    fromTokenAddress: NATIVE_EVM_TOKEN,
    toTokenAddress: token,
    amount,
    ...swapSlippageParams(env, chain),
    userWalletAddress: wallet.address || check.wallet,
    swapReceiverAddress: wallet.address || check.wallet,
  };
  if (live && (envText(env, 'OKX_LIVE_ENABLED') !== '1' || envText(env, 'OKX_ALLOW_AUTOMATED_TRADES') !== '1')) {
    return { state: 'blocked', reason: 'explicit_live_environment_required', live_started: false, wallet: check.wallet };
  }
  const readNativeBalance = deps.nativeBalance || ((currentWallet) => currentWallet.provider.getBalance(currentWallet.address));
  let nativeBalanceBefore = null;
  if (live) {
    nativeBalanceBefore = BigInt(await executionStep('buy_native_balance_failed', () => readNativeBalance(wallet)));
    if (nativeBalanceBefore < BigInt(amount)) throw new Error('insufficient_native_balance');
  }
  const swap = await executionStep('buy_quote_failed', () => buildSwap(client, params, deps));
  const economics = live && (candidate?.strategy_version === 'chain_v2' || economicGuardEnabled(env))
    ? await validateBuyEconomics(client, params, swap, env, chain, deps, wallet, sdk)
    : null;
  if (live && economics && nativeBalanceBefore < BigInt(economics.required_native_balance_atomic)) {
    throw new Error('insufficient_native_gas_balance');
  }
  if (!live) {
    return {
      state: 'dry_run_ready',
      live_started: false,
      provider: 'okx-dex-sdk',
      wallet: check.wallet,
      chain: chain.name,
      chain_id: chain.chainId,
      symbol: candidate?.symbol || 'MEME',
      token,
      swap: redact({ routerResult: swap.routerResult, tx: swap.tx }),
    };
  }
  const readBalance = deps.tokenBalance || ((w, t) => tokenBalance(w, t, sdk));
  const waitTransaction = deps.waitTransaction || defaultWaitTransaction;
  const signTransaction = deps.signTransaction || defaultSignTransaction;
  const broadcastTransaction = broadcastTransactionFor(chain, wallet, deps);
  const before = BigInt(await executionStep('buy_balance_before_failed', () => readBalance(wallet, token)));
  const buyTx = await simulateTransactionFor(chain, wallet, swap.tx, deps, 'buy_simulation_failed');
  const signedTx = await executionStep('buy_sign_failed', () => signTransaction(wallet, buyTx, chain.chainId));
  await preparedProgress(onExecutionProgress, 'buy', signedTx, sdk, {
    wallet_balances_before: {
      token_atomic: before.toString(),
      native_atomic: nativeBalanceBefore.toString(),
    },
  });
  const broadcast = await executionStep('buy_broadcast_failed', () => broadcastTransaction(client, {
    signedTx,
    chainIndex: String(chain.chainId),
    address: wallet.address || check.wallet,
    enableMevProtection: true,
  }));
  await executionProgress(onExecutionProgress, 'broadcast', 'buy', broadcast.txHash);
  const receipt = await executionStep('buy_receipt_failed', () => waitTransaction(wallet, broadcast.txHash));
  await executionProgress(onExecutionProgress, 'confirmed', 'buy', broadcast.txHash);
  const after = BigInt(await executionStep('buy_balance_after_failed', () => readBalance(wallet, token)));
  const bought = after - before;
  if (bought <= 0n) throw new Error('buy_fill_not_detected');
  let nativeSpent = null;
  try {
    const nativeBalanceAfter = BigInt(await readNativeBalance(wallet));
    nativeSpent = nativeBalanceBefore - nativeBalanceAfter;
  } catch {
    nativeSpent = null;
  }
  const gasNative = receiptFeeAtomic(receipt);
  return {
    state: 'buy_confirmed',
    live_started: true,
    provider: 'okx-dex-sdk',
    wallet: check.wallet,
    chain: chain.name,
    chain_id: chain.chainId,
    symbol: candidate?.symbol || 'MEME',
    token,
    buy_tx: broadcast.txHash,
    order_id: broadcast.orderId,
    bought_atomic: bought.toString(),
    entry_native_atomic: amount,
    ...(nativeSpent !== null && nativeSpent >= 0n ? { native_spent_atomic: nativeSpent.toString() } : {}),
    ...(gasNative !== null ? { gas_native_atomic: gasNative.toString() } : {}),
    ...(economics || {}),
  };
}

export async function executeSell({
  env = process.env,
  position,
  tokenAmountAtomic,
  preparedSwap = null,
  live = false,
  deps = {},
  onExecutionProgress,
}) {
  const check = await preflight(env, { loadSdk: deps.loadSdk || loadSdk });
  if (!check.ready) return { ...check, state: 'blocked' };
  const sdk = await (deps.loadSdk || loadSdk)();
  const created = deps.createClient ? deps.createClient(env, sdk) : createClient(env, sdk);
  const client = created.client;
  const wallet = created.wallet || { address: check.wallet };
  const chain = created.chain || executionChain(env);
  const token = requireAddress(position?.contract_address || position?.token, 'invalid_token');
  const amount = requireUint(tokenAmountAtomic || position?.remaining_atomic, 'invalid_amount');
  const params = {
    chainIndex: String(chain.chainId),
    fromTokenAddress: token,
    toTokenAddress: NATIVE_EVM_TOKEN,
    amount,
    ...swapSlippageParams(env, chain),
    userWalletAddress: wallet.address || check.wallet,
    swapReceiverAddress: wallet.address || check.wallet,
  };
  const preparedAmount = String(preparedSwap?.routerResult?.fromTokenAmount ?? '');
  const reusableSwap = preparedSwap?.routerResult && preparedSwap?.tx && preparedAmount === amount
    ? preparedSwap
    : null;
  const swap = reusableSwap || await executionStep('sell_quote_failed', () => buildSwap(client, params, deps));
  if (!live) {
    return {
      state: 'dry_run_ready',
      live_started: false,
      provider: 'okx-dex-sdk',
      wallet: check.wallet,
      chain: chain.name,
      chain_id: chain.chainId,
      symbol: position?.symbol || 'MEME',
      token,
      swap: redact({ routerResult: swap.routerResult, tx: swap.tx }),
    };
  }
  if (envText(env, 'OKX_LIVE_ENABLED') !== '1' || envText(env, 'OKX_ALLOW_AUTOMATED_TRADES') !== '1') {
    return { state: 'blocked', reason: 'explicit_live_environment_required', live_started: false, wallet: check.wallet };
  }
  const waitTransaction = deps.waitTransaction || defaultWaitTransaction;
  const readTokenBalance = deps.tokenBalance || ((currentWallet, currentToken) => tokenBalance(currentWallet, currentToken, sdk));
  const executeApproval = deps.executeApproval || (chain.name === 'robinhood'
    ? (_client, approvalParams) => directRpcApproval(client, approvalParams, wallet, chain, sdk, deps, onExecutionProgress)
    : defaultExecuteApproval);
  const signTransaction = deps.signTransaction || defaultSignTransaction;
  const broadcastTransaction = broadcastTransactionFor(chain, wallet, deps);
  const readNativeBalance = deps.nativeBalance || (wallet?.provider?.getBalance
    ? ((currentWallet) => currentWallet.provider.getBalance(currentWallet.address))
    : null);
  let nativeBalanceBefore = null;
  if (readNativeBalance) {
    try { nativeBalanceBefore = BigInt(await readNativeBalance(wallet)); } catch { nativeBalanceBefore = null; }
  }
  let tokenBalanceBefore = null;
  try { tokenBalanceBefore = BigInt(await readTokenBalance(wallet, token)); } catch { tokenBalanceBefore = null; }
  // The SDK approval bundles signing and submission; journal intent before entry.
  if (deps.executeApproval || chain.name !== 'robinhood') {
    await executionProgress(onExecutionProgress, 'prepared', 'approval', null, {
      ...(tokenBalanceBefore !== null && nativeBalanceBefore !== null ? {
        wallet_balances_before: {
          token_atomic: tokenBalanceBefore.toString(),
          native_atomic: nativeBalanceBefore.toString(),
        },
      } : {}),
    });
  }
  const approval = await executionStep('sell_approval_failed', () => executeApproval(client, {
    chainIndex: String(chain.chainId),
    tokenContractAddress: token,
    approveAmount: amount,
  }));
  let approvalReceipt = null;
  if (approval.transactionHash) {
    await executionProgress(onExecutionProgress, 'broadcast', 'approval', approval.transactionHash);
    approvalReceipt = await executionStep('sell_approval_receipt_failed', () => waitTransaction(wallet, approval.transactionHash));
    await executionProgress(onExecutionProgress, 'confirmed', 'approval', approval.transactionHash);
  }
  const sellTx = await simulateTransactionFor(chain, wallet, swap.tx, deps, 'sell_simulation_failed');
  const signedSell = await executionStep('sell_sign_failed', () => signTransaction(wallet, sellTx, chain.chainId));
  await preparedProgress(onExecutionProgress, 'sell', signedSell, sdk, {
    ...(tokenBalanceBefore !== null && nativeBalanceBefore !== null ? {
      wallet_balances_before: {
        token_atomic: tokenBalanceBefore.toString(),
        native_atomic: nativeBalanceBefore.toString(),
      },
    } : {}),
  });
  const sellBroadcast = await executionStep('sell_broadcast_failed', () => broadcastTransaction(client, {
    signedTx: signedSell,
    chainIndex: String(chain.chainId),
    address: wallet.address || check.wallet,
    enableMevProtection: true,
  }));
  await executionProgress(onExecutionProgress, 'broadcast', 'sell', sellBroadcast.txHash);
  const sellReceipt = await executionStep('sell_receipt_failed', () => waitTransaction(wallet, sellBroadcast.txHash));
  await executionProgress(onExecutionProgress, 'confirmed', 'sell', sellBroadcast.txHash);
  let actualSold = null;
  if (tokenBalanceBefore !== null) {
    try {
      const tokenBalanceAfter = BigInt(await readTokenBalance(wallet, token));
      const debit = tokenBalanceBefore - tokenBalanceAfter;
      if (debit > 0n) actualSold = debit;
    } catch {
      actualSold = null;
    }
  }
  let netNativeReceived = null;
  if (readNativeBalance && nativeBalanceBefore !== null) {
    try {
      const nativeBalanceAfter = BigInt(await readNativeBalance(wallet));
      netNativeReceived = nativeBalanceAfter - nativeBalanceBefore;
    } catch {
      netNativeReceived = null;
    }
  }
  const approvalGas = receiptFeeAtomic(approvalReceipt) || 0n;
  const sellGas = receiptFeeAtomic(sellReceipt) || 0n;
  const gasNative = approvalGas + sellGas;
  const grossNativeReceived = netNativeReceived === null ? null : netNativeReceived + gasNative;
  return {
    state: actualSold === null ? 'sell_settlement_unverified' : 'sell_confirmed',
    live_started: true,
    provider: 'okx-dex-sdk',
    wallet: check.wallet,
    chain: chain.name,
    chain_id: chain.chainId,
    symbol: position?.symbol || 'MEME',
    token,
    sell_tx: sellBroadcast.txHash,
    sell_order_id: sellBroadcast.orderId,
    approval_tx: approval.transactionHash || null,
    requested_sold_atomic: amount,
    ...(actualSold !== null ? { sold_atomic: actualSold.toString() } : {}),
    ...(netNativeReceived !== null ? { net_native_received_atomic: netNativeReceived.toString() } : {}),
    ...(grossNativeReceived !== null ? { native_received_atomic: grossNativeReceived.toString() } : {}),
    gas_native_atomic: gasNative.toString(),
  };
}

function parseArgs(argv) {
  const args = new Set(argv);
  const value = (name) => {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : '';
  };
  return {
    preflight: args.has('--preflight'),
    remotePreflight: args.has('--remote-preflight'),
    strategyMeme: args.has('--strategy-meme'),
    live: args.has('--live'),
    roundTrip: args.has('--round-trip'),
    input: value('--input'),
    token: value('--token'),
    pool: value('--pool'),
    symbol: value('--symbol') || 'MEME',
    amount: value('--amount-native-atomic'),
  };
}

export async function main(argv = process.argv.slice(2), env = process.env) {
  const args = parseArgs(argv);
  try {
    if (args.preflight) {
      console.log(JSON.stringify(await preflight(env, { remote: args.remotePreflight })));
      return 0;
    }
    let candidate;
    if (args.strategyMeme) {
      const here = path.dirname(fileURLToPath(import.meta.url));
      const root = path.resolve(here, '..', '..', '..', '..');
      const chain = executionChain(env);
      const input = args.input || path.join(root, 'outputs', `${chain.name}-execution-input.json`);
      candidate = selectStrategyCandidate(JSON.parse(fs.readFileSync(input, 'utf8')), chain.name);
    } else {
      candidate = {
        symbol: args.symbol,
        contract_address: requireAddress(args.token, 'invalid_token'),
        pool_address: args.pool ? requireAddress(args.pool, 'invalid_pool') : '0x0000000000000000000000000000000000000001',
      };
    }
    const result = await runRoundTrip({ env, candidate, amountNativeAtomic: args.amount, live: args.live, roundTrip: args.roundTrip });
    console.log(JSON.stringify(redact(result)));
    return ['buy_broadcasted', 'round_trip_completed', 'dry_run_ready'].includes(result.state) ? 0 : 1;
  } catch (error) {
    console.log(JSON.stringify({ state: 'blocked', provider: 'okx-dex-sdk', reason: publicError(error.message), live_started: false }));
    return 1;
  }
}

const modulePath = fileURLToPath(import.meta.url);
const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath && path.normalize(modulePath).toLowerCase() === path.normalize(invokedPath).toLowerCase()) {
  process.exitCode = await main();
}
