import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { staticReportUrl } from './static-report-origin.mjs';

describe('static report origin', () => {
  it('builds the fallback only from a canonical configured HTTPS origin', () => {
    assert.equal(staticReportUrl('https://radar.example'), 'https://radar.example/report.json');
    assert.equal(staticReportUrl('https://radar.example/'), 'https://radar.example/report.json');
  });

  it('rejects missing, credentialed, non-HTTPS and non-origin values', () => {
    assert.equal(staticReportUrl(''), null);
    assert.equal(staticReportUrl('http://radar.example'), null);
    assert.equal(staticReportUrl('https://user:pass@radar.example'), null);
    assert.equal(staticReportUrl('https://radar.example/private'), null);
    assert.equal(staticReportUrl('https://127.0.0.1'), null);
  });
});
