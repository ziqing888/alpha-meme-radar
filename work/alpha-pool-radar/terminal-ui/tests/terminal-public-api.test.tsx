import { describe, expect, it } from 'vitest';

import { resolveApiRoot } from '@/lib/terminal-api';

describe('public read-only API configuration', () => {
  it('accepts an HTTPS terminal API root from the URL', () => {
    expect(resolveApiRoot('?api=https%3A%2F%2Fradar.trycloudflare.com%2Fapi%2Fterminal')).toEqual({
      root: 'https://radar.trycloudflare.com/api/terminal',
      publicReadOnly: true,
    });
  });

  it.each([
    '?api=http%3A%2F%2Fradar.trycloudflare.com%2Fapi%2Fterminal',
    '?api=https%3A%2F%2Fradar.trycloudflare.com%2Fapi%2Fprivate',
    '?api=not-a-url',
    '',
  ])('falls back to the local API for invalid input: %s', (search) => {
    expect(resolveApiRoot(search)).toEqual({
      root: '/api/terminal',
      publicReadOnly: false,
    });
  });
});
