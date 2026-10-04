export type WalletRow = {
  index: number;
  address: string;
  amount: string;
  funded: string | number;
  status: string;
  balanceEth: string | null;
  lastTxHash: string | null;
  lastTxType: string | null;
  fundTxHash: string | null;
  buyTxHash: string | null;
  sellTxHash: string | null;
  gatherTxHash: string | null;
};

export type WalletCycleResponse = {
  cycle: {
    file: string;
    startedAt: string | null;
    wallets: WalletRow[];
  } | null;
  cycles: { file: string; startedAt: string | null }[];
  explorerTxUrl: string;
};

export type TimeseriesPoint = {
  cycle: number;
  funded: number;
  buys: number;
  sells: number;
  volumeEth: number;
  baseEth: number;
};

export type StartBotPayload = {
  targetTokenAddress: string;
  amountMin: number;
  amountMax: number;
  parallelWallets: number;
  subWalletNum: number;
  endlessMode: boolean;
  minBaseBalanceEth: number;
  tradeScheduleMode: 'pipeline' | 'mixed';
  tradeSizeBias: 'max' | 'random';
};

export type BotSession = StartBotPayload & {
  id?: string;
  status?: string;
  startedAt?: string;
  endedAt?: string | null;
};

export type SessionSummary = BotSession & {
  isActive?: boolean;
  totals: {
    cycles: number;
    funded: number;
    buys: number;
    sells: number;
    volumeEth: number;
  };
};

export type RecoveryScope = {
  file?: string;
  all?: boolean;
  latestOnly?: boolean;
};

export type GatherResult = {
  gathered: number;
  skipped: number;
  dust: number;
  recoveredEth: string;
  baseBalanceEth: string;
  filesScanned: number;
};

export type LiquidateResult = GatherResult & {
  sold: number;
  sellFailed: number;
  tokenAddress: string;
};

export type PreflightCosts = {
  dexFeeBps: number;
  estimatedSlippageBps: number;
  estimatedGasBps: number;
  predictedTotalBps: number;
  predictedCentsPerDollar: number;
  targetCentsPerDollar: number;
  targetMet: boolean;
  volumeMultiplier: number;
  estimatedCostUsd100k: number | null;
  estimatedCostUsd30k: number | null;
};

export type PreflightPlanning = {
  tradeEth: number;
  subWalletNum: number;
  parallelWallets: number;
  dexFeeBps: number;
  slippageBps: number;
  gasBurnedPerWalletEth: number;
  gasReservePerWalletEth: number;
  ethUsd: number | null;
};

export type PreflightPlanBreakdown = {
  dexEth: number;
  slippageEth: number;
  gasEth: number;
  totalEth: number;
  dexUsd: number | null;
  slippageUsd: number | null;
  gasUsd: number | null;
  totalUsd: number | null;
};

export type PreflightPlan = {
  mode: 'volume' | 'budget';
  inputEth: number;
  inputUsd: number | null;
  targetVolumeEth: number;
  targetVolumeUsd: number | null;
  budgetEth: number;
  budgetUsd: number | null;
  wallets: number;
  cycles: number;
  volumePerWalletEth: number;
  volumePerCycleEth: number;
  breakdown: PreflightPlanBreakdown;
};

export type PreflightResult = {
  ethUsd: number | null;
  costs: PreflightCosts;
  planning: PreflightPlanning;
  trade: {
    effectiveMinEth: number;
    effectiveMaxEth: number;
    avgTradeEth: number;
  };
  route: {
    version: string;
    feeBpsPerLeg: number;
    poolNativeEth: number | null;
  };
};

export type PreflightParams = {
  token: string;
  amountMin?: number;
  amountMax?: number;
  subWalletNum?: number;
  parallelWallets?: number;
};

export type BotStatus = {
  targetTokenConfigured: boolean;
  session: BotSession | null;
  bot: {
    running: boolean;
    pid: number | null;
    startedAt: string | null;
    uptimeMs: number | null;
    source: 'dashboard' | 'external' | null;
    logPath: string;
  };
  operating: {
    minBaseBalanceEth: number;
    headroomEth: number;
    belowStopThreshold: boolean;
    endlessMode: boolean;
    tradeMode: string;
    walletsPerCycle: number;
  };
  wallet: {
    address: string;
    balanceEth: string;
    balanceUsd: number | null;
    ethUsd: number | null;
    subWallets: {
      totalEth: string;
      totalUsd: number | null;
      walletCount: number;
      withBalance: number;
      cycleFile: string | null;
    };
    combinedEth: string;
    combinedUsd: number | null;
  };
  token: {
    address: string;
    name: string | null;
    symbol: string | null;
    decimals: number;
    totalSupply: string | null;
    priceUsd: number | null;
    priceNative: string | null;
    marketCapUsd: number | null;
    fdvUsd: number | null;
    liquidityUsd: number | null;
    volume24hUsd: number | null;
    priceChange24h: number | null;
    pool: {
      dex: string | null;
      version: string | null;
      pairAddress: string | null;
      url: string | null;
    };
    imageUrl: string | null;
    links: { type: string; url: string }[];
  } | null;
  pool: {
    version: string;
    fee: number | null;
    liquidity: string;
  } | null;
  stats: {
    lastCycle: {
      cycle: number;
      funded: number;
      buys: number;
      sells: number;
      volumeEth: number;
      volumeUsd: number | null;
      baseEth: string;
    } | null;
    totals: {
      cycles: number;
      funded: number;
      buys: number;
      sells: number;
      volumeEth: number;
      volumeUsd: number | null;
    } | null;
    efficiency: {
      startBaseEth: number;
      spentEth: number;
      spentUsd: number | null;
      costPerVolumeUsd: number | null;
    } | null;
  };
};

export type BotConfig = {
  defaults: Omit<StartBotPayload, 'targetTokenAddress'>;
  lastSession: BotSession | null;
  tradeScheduleMode: string;
  parallelWallets: number;
  amountMin: number;
  amountMax: number;
  fee: number;
  subWalletNum: number;
  endlessMode: boolean;
  minBaseBalanceEth: number;
  cyclePauseMs: number;
  buyStaggerMinMs: number;
  buyStaggerMaxMs: number;
  sellDelayMinMs: number;
  sellDelayMaxMs: number;
  mixedBuyStaggerMinMs: number;
  mixedBuyStaggerMaxMs: number;
  mixedSellDelayMinMs: number;
  mixedSellDelayMaxMs: number;
  poolVersionPreference: string;
  poolSelectionMode?: string;
  targetCostPerVolumeBps?: number;
  tradeSizeBias: 'max' | 'random';
  chainId: number;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || 'Request failed');
  }
  return data as T;
}

export const api = {
  getStatus: () => request<BotStatus>('/api/status'),
  getConfig: () => request<BotConfig>('/api/config'),
  getLogs: (lines = 300) => request<{ log: string; path: string }>(`/api/logs?lines=${lines}`),
  getTimeseries: () => request<{ points: TimeseriesPoint[] }>('/api/stats/timeseries'),
  getWallets: (file?: string) =>
    request<WalletCycleResponse>(file ? `/api/wallets?file=${encodeURIComponent(file)}` : '/api/wallets'),
  getPreflight: (params: PreflightParams) => {
    const search = new URLSearchParams({ token: params.token });
    if (params.amountMin != null) search.set('amountMin', String(params.amountMin));
    if (params.amountMax != null) search.set('amountMax', String(params.amountMax));
    if (params.subWalletNum != null) search.set('subWalletNum', String(params.subWalletNum));
    if (params.parallelWallets != null) search.set('parallelWallets', String(params.parallelWallets));
    return request<PreflightResult>(`/api/preflight?${search.toString()}`);
  },
  getSessions: (limit = 50) =>
    request<{ sessions: SessionSummary[] }>(`/api/sessions?limit=${limit}`),
  gatherFunds: (scope: RecoveryScope = { latestOnly: true }) =>
    request<GatherResult>('/api/gather', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scope),
    }),
  liquidateFunds: (scope: RecoveryScope = { latestOnly: true }, tokenAddress?: string) =>
    request<LiquidateResult>('/api/liquidate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...scope, tokenAddress }),
    }),
  startBot: (payload: StartBotPayload) =>
    request('/api/bot/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  stopBot: () => request('/api/bot/stop', { method: 'POST' }),
};
