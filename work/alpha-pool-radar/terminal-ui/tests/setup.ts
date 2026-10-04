import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, vi } from 'vitest';

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => {
    throw new Error('Network access is forbidden in frontend regression tests');
  }));
});

afterEach(() => {
  expect(globalThis.fetch).not.toHaveBeenCalled();
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
