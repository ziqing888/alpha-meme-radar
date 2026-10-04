import assert from 'node:assert/strict';
import { test } from 'node:test';
import { paperValuationLabel } from '../src/paper-valuation.ts';

test('paper equity distinguishes missing, stale and verified zero', () => {
  assert.equal(paperValuationLabel(undefined), '估值待核验');
  assert.equal(paperValuationLabel({ equity_usd: null, valuation_status: 'unavailable' }), '估值不可用');
  assert.equal(paperValuationLabel({ equity_usd: 1100, valuation_status: 'stale' }), '含过期估值');
  assert.equal(paperValuationLabel({ equity_usd: 0, valuation_status: 'fresh' }), '估值新鲜');
  assert.equal(paperValuationLabel({ equity_usd: 1100 }), '估值状态未知');
});
