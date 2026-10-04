import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { loadSdk } from '../src/okx_dex_executor.mjs';

const lockUrl = new URL('../package-lock.json', import.meta.url);

test('dependency lock excludes the legacy Ethereum signing stack', async () => {
  const lock = JSON.parse(await readFile(lockUrl, 'utf8'));
  const packagePaths = Object.keys(lock.packages ?? {});

  assert.equal(lock.packages['node_modules/@okxweb3/coin-ethereum']?.version, '2.4.12');
  assert.equal(lock.packages['node_modules/protobufjs']?.version, '7.6.5');

  for (const legacyPackage of [
    'node_modules/eth-sig-util',
    'node_modules/ethereumjs-util',
    'node_modules/ethereumjs-abi',
  ]) {
    assert.equal(
      packagePaths.some((packagePath) => packagePath.endsWith(legacyPackage)),
      false,
      `${legacyPackage} must not be present in the executor dependency tree`,
    );
  }
});

test('installed OKX SDK still creates an EVM wallet and signs locally', async () => {
  const sdk = await loadSdk();
  const privateKey = `0x${'1'.repeat(64)}`;
  const wallet = sdk.createEVMWallet(privateKey, null);
  const client = new sdk.OKXDexClient({
    apiKey: 'offline-test',
    secretKey: 'offline-test',
    apiPassphrase: 'offline-test',
    projectId: 'offline-test',
    evm: { wallet },
  });

  assert.equal(wallet.address, '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A');
  assert.match(await wallet.signMessage('offline-smoke-test'), /^0x[0-9a-f]{130}$/i);
  assert.equal(typeof client.dex.getQuote, 'function');
  assert.equal(typeof client.dex.getSwapData, 'function');
});
