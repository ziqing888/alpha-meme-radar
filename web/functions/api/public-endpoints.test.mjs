import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { onRequestGet as getBatchQuotes } from './dex-prices.js';
import { onRequestGet as getMonitorStream } from './monitor-stream.js';
import vercelBatchHandler from '../../../work/alpha-pool-radar/vercel_api/dex-prices.js';

describe('retired public amplification endpoints', () => {
  it('does not open a per-client storage polling loop', async () => {
    let reads = 0;
    const response = await getMonitorStream({
      env: { MONITOR_R2: { get: async () => { reads += 1; return null; } } },
      request: new Request('https://radar.example/api/monitor-stream'),
    });

    assert.equal(response.status, 410);
    assert.equal(reads, 0);
  });

  it('does not fan one anonymous request out to upstream quote requests', async () => {
    const originalFetch = globalThis.fetch;
    let upstreamCalls = 0;
    globalThis.fetch = async () => {
      upstreamCalls += 1;
      throw new Error('upstream fetch must not run');
    };
    try {
      const response = await getBatchQuotes({
        request: new Request('https://radar.example/api/dex-prices?targets=token:bsc:0xabc'),
      });
      assert.equal(response.status, 410);
      assert.equal(upstreamCalls, 0);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('retires the legacy Vercel batch quote proxy too', async () => {
    const result = { statusCode: 0, body: '', headers: {}, setHeader(key, value) { this.headers[key] = value; }, end(value) { this.body = value; } };
    await vercelBatchHandler({ method: 'GET', query: { targets: 'token:bsc:0xabc' } }, result);

    assert.equal(result.statusCode, 410);
    assert.equal(JSON.parse(result.body).error, 'batch_quote_endpoint_retired');
  });
});
