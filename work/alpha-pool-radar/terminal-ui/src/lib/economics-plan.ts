export type PlanningBasis = {
  tradeEth: number;
  subWalletNum: number;
  parallelWallets: number;
  dexFeeBps: number;
  slippageBps: number;
  gasBurnedPerWalletEth: number;
  gasReservePerWalletEth: number;
  ethUsd: number | null;
};

export type EconomicsPlanBreakdown = {
  dexEth: number;
  slippageEth: number;
  gasEth: number;
  totalEth: number;
  dexUsd: number | null;
  slippageUsd: number | null;
  gasUsd: number | null;
  totalUsd: number | null;
};

export type EconomicsPlan = {
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
  breakdown: EconomicsPlanBreakdown;
  ops: {
    gasReservePerWalletEth: number;
    peakSlots: number;
    gasReservePeakEth: number;
    gasReservePeakUsd: number | null;
    workingCapitalPeakEth: number;
    workingCapitalPeakUsd: number | null;
  };
};

export type TimingParams = {
  parallelWallets: number;
  subWalletNum: number;
  tradeScheduleMode: 'pipeline' | 'mixed';
  cyclePauseMs: number;
  txConfirmMs?: number;
  buyStaggerMinMs?: number;
  buyStaggerMaxMs?: number;
  sellDelayMinMs?: number;
  sellDelayMaxMs?: number;
  mixedBuyStaggerMinMs?: number;
  mixedBuyStaggerMaxMs?: number;
  mixedSellDelayMinMs?: number;
  mixedSellDelayMaxMs?: number;
};

export type PlanDuration = {
  ms: number;
  minMs: number;
  maxMs: number;
};

const DEFAULT_TX_CONFIRM_MS = 3_000;
const WALLET_GEN_MS = 2_000;

function avg(min: number, max: number) {
  return (min + max) / 2;
}

function pickDelay(
  minMs: number,
  maxMs: number,
  mode: 'avg' | 'min' | 'max'
) {
  if (mode === 'min') return minMs;
  if (mode === 'max') return maxMs;
  return avg(minMs, maxMs);
}

function walletsInCycle(
  cycleIndex: number,
  totalWallets: number,
  subWalletNum: number
) {
  return Math.min(subWalletNum, totalWallets - cycleIndex * subWalletNum);
}

function estimatePipelineBatchMs(
  batchSize: number,
  timing: TimingParams,
  mode: 'avg' | 'min' | 'max'
) {
  const tx = timing.txConfirmMs ?? DEFAULT_TX_CONFIRM_MS;
  const buyStagger = pickDelay(
    timing.buyStaggerMinMs ?? 0,
    timing.buyStaggerMaxMs ?? 4_000,
    mode
  );
  const sellDelay = pickDelay(
    timing.sellDelayMinMs ?? 0,
    timing.sellDelayMaxMs ?? 12_000,
    mode
  );
  const fundMs = batchSize * tx;
  const tradeMs = buyStagger + sellDelay + 3 * tx;
  return fundMs + tradeMs;
}

function estimateBotCycleMs(
  walletsInCycle: number,
  timing: TimingParams,
  mode: 'avg' | 'min' | 'max'
) {
  if (walletsInCycle <= 0) return 0;

  const tx = timing.txConfirmMs ?? DEFAULT_TX_CONFIRM_MS;

  if (timing.tradeScheduleMode === 'mixed') {
    const buyStagger = pickDelay(
      timing.mixedBuyStaggerMinMs ?? 0,
      timing.mixedBuyStaggerMaxMs ?? 30_000,
      mode
    );
    const sellDelay = pickDelay(
      timing.mixedSellDelayMinMs ?? 10_000,
      timing.mixedSellDelayMaxMs ?? 90_000,
      mode
    );
    return WALLET_GEN_MS + tx + buyStagger + sellDelay + 3 * tx;
  }

  const parallel = Math.max(1, timing.parallelWallets);
  let total = WALLET_GEN_MS;
  for (let i = 0; i < walletsInCycle; i += parallel) {
    const batchSize = Math.min(parallel, walletsInCycle - i);
    total += estimatePipelineBatchMs(batchSize, timing, mode);
  }
  return total;
}

export function estimatePlanDuration(
  plan: Pick<EconomicsPlan, 'wallets' | 'cycles'>,
  timing: TimingParams
): PlanDuration {
  let avgMs = 0;
  let minMs = 0;
  let maxMs = 0;

  for (let cycle = 0; cycle < plan.cycles; cycle++) {
    const wallets = walletsInCycle(cycle, plan.wallets, timing.subWalletNum);
    avgMs += estimateBotCycleMs(wallets, timing, 'avg');
    minMs += estimateBotCycleMs(wallets, timing, 'min');
    maxMs += estimateBotCycleMs(wallets, timing, 'max');

    if (cycle < plan.cycles - 1) {
      avgMs += timing.cyclePauseMs;
      minMs += timing.cyclePauseMs;
      maxMs += timing.cyclePauseMs;
    }
  }

  return {
    ms: Math.round(avgMs),
    minMs: Math.round(minMs),
    maxMs: Math.round(maxMs),
  };
}

function toUsd(eth: number, ethUsd: number | null): number | null {
  return ethUsd != null ? eth * ethUsd : null;
}

function roundEth(value: number): number {
  return Number(value.toFixed(6));
}

function breakdownFromParts(
  dexEth: number,
  slippageEth: number,
  gasEth: number,
  ethUsd: number | null
): EconomicsPlanBreakdown {
  const totalEth = dexEth + slippageEth + gasEth;
  return {
    dexEth: roundEth(dexEth),
    slippageEth: roundEth(slippageEth),
    gasEth: roundEth(gasEth),
    totalEth: roundEth(totalEth),
    dexUsd: toUsd(dexEth, ethUsd),
    slippageUsd: toUsd(slippageEth, ethUsd),
    gasUsd: toUsd(gasEth, ethUsd),
    totalUsd: toUsd(totalEth, ethUsd),
  };
}

function planOps(basis: PlanningBasis, wallets: number) {
  const peakSlots = Math.min(wallets, Math.max(1, basis.parallelWallets));
  const gasReservePeakEth = peakSlots * basis.gasReservePerWalletEth;
  const workingCapitalPeakEth = peakSlots * (basis.tradeEth + basis.gasReservePerWalletEth);
  return {
    gasReservePerWalletEth: roundEth(basis.gasReservePerWalletEth),
    peakSlots,
    gasReservePeakEth: roundEth(gasReservePeakEth),
    gasReservePeakUsd: toUsd(gasReservePeakEth, basis.ethUsd),
    workingCapitalPeakEth: roundEth(workingCapitalPeakEth),
    workingCapitalPeakUsd: toUsd(workingCapitalPeakEth, basis.ethUsd),
  };
}

export function planFromTargetVolume(
  basis: PlanningBasis,
  targetVolumeEth: number
): EconomicsPlan | null {
  if (!Number.isFinite(targetVolumeEth) || targetVolumeEth <= 0) return null;

  const volumePerWalletEth = basis.tradeEth * 2;
  if (volumePerWalletEth <= 0) return null;

  const wallets = Math.max(1, Math.ceil(targetVolumeEth / volumePerWalletEth));
  const actualVolumeEth = wallets * volumePerWalletEth;
  const cycles = Math.max(1, Math.ceil(wallets / basis.subWalletNum));

  const dexEth = actualVolumeEth * (basis.dexFeeBps / 10_000);
  const slippageEth = actualVolumeEth * (basis.slippageBps / 10_000);
  const gasEth = wallets * basis.gasBurnedPerWalletEth;

  return {
    mode: 'volume',
    inputEth: roundEth(targetVolumeEth),
    inputUsd: toUsd(targetVolumeEth, basis.ethUsd),
    targetVolumeEth: roundEth(actualVolumeEth),
    targetVolumeUsd: toUsd(actualVolumeEth, basis.ethUsd),
    budgetEth: roundEth(dexEth + slippageEth + gasEth),
    budgetUsd: toUsd(dexEth + slippageEth + gasEth, basis.ethUsd),
    wallets,
    cycles,
    volumePerWalletEth: roundEth(volumePerWalletEth),
    volumePerCycleEth: roundEth(volumePerWalletEth * basis.subWalletNum),
    breakdown: breakdownFromParts(dexEth, slippageEth, gasEth, basis.ethUsd),
    ops: planOps(basis, wallets),
  };
}

export function planFromBudget(basis: PlanningBasis, budgetEth: number): EconomicsPlan | null {
  if (!Number.isFinite(budgetEth) || budgetEth <= 0) return null;

  const volumePerWalletEth = basis.tradeEth * 2;
  if (volumePerWalletEth <= 0) return null;

  const variableBps = basis.dexFeeBps + basis.slippageBps;
  const variablePerWallet = volumePerWalletEth * (variableBps / 10_000);
  const costPerWallet = variablePerWallet + basis.gasBurnedPerWalletEth;
  if (costPerWallet <= 0) return null;

  let remaining = budgetEth;
  let wallets = 0;
  let volumeEth = 0;
  let dexEth = 0;
  let slippageEth = 0;
  let gasEth = 0;

  while (remaining + 1e-12 >= costPerWallet) {
    remaining -= costPerWallet;
    wallets += 1;
    volumeEth += volumePerWalletEth;
    dexEth += volumePerWalletEth * (basis.dexFeeBps / 10_000);
    slippageEth += volumePerWalletEth * (basis.slippageBps / 10_000);
    gasEth += basis.gasBurnedPerWalletEth;
  }

  if (wallets === 0) return null;

  const cycles = Math.max(1, Math.ceil(wallets / basis.subWalletNum));

  return {
    mode: 'budget',
    inputEth: roundEth(budgetEth),
    inputUsd: toUsd(budgetEth, basis.ethUsd),
    targetVolumeEth: roundEth(volumeEth),
    targetVolumeUsd: toUsd(volumeEth, basis.ethUsd),
    budgetEth: roundEth(dexEth + slippageEth + gasEth),
    budgetUsd: toUsd(dexEth + slippageEth + gasEth, basis.ethUsd),
    wallets,
    cycles,
    volumePerWalletEth: roundEth(volumePerWalletEth),
    volumePerCycleEth: roundEth(volumePerWalletEth * basis.subWalletNum),
    breakdown: breakdownFromParts(dexEth, slippageEth, gasEth, basis.ethUsd),
    ops: planOps(basis, wallets),
  };
}
