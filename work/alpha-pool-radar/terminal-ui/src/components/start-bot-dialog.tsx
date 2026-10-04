import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/input';
import { api, type StartBotPayload } from '@/lib/api';
import { planFromBudget, planFromTargetVolume, estimatePlanDuration } from '@/lib/economics-plan';
import { formatEth, formatPlanTimeframe, formatUsd } from '@/lib/utils';

const TOKEN_RE = /^0x[a-fA-F0-9]{40}$/;

type StartBotDialogProps = {
  open: boolean;
  onClose: () => void;
  onStarted: () => void;
};

type PlanMode = 'volume' | 'budget';

function emptyForm(): StartBotPayload {
  return {
    targetTokenAddress: '',
    amountMin: 0.002,
    amountMax: 0.01,
    parallelWallets: 4,
    subWalletNum: 20,
    endlessMode: true,
    minBaseBalanceEth: 0.005,
    tradeScheduleMode: 'pipeline',
    tradeSizeBias: 'max',
  };
}

function formatUsdApprox(eth: number, ethUsd: number | null | undefined) {
  const usd = ethUsd != null ? eth * ethUsd : null;
  return usd != null ? `≈ ${formatUsd(usd)}` : 'USD unavailable';
}

function PlanBreakdown({
  label,
  eth,
  usd,
  muted = false,
  note,
}: {
  label: string;
  eth: number;
  usd: number | null;
  muted?: boolean;
  note?: string;
}) {
  return (
    <div className="text-sm">
      <div className="flex items-baseline justify-between gap-3">
        <span
          className={
            muted
              ? 'text-[var(--color-ink-muted)]/80 dark:text-[var(--color-ink-muted-dark)]/80'
              : 'text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]'
          }
        >
          {label}
        </span>
        <span className="text-right">
          <span
            className={
              muted
                ? 'font-mono tabular-nums text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]'
                : 'font-mono tabular-nums text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]'
            }
          >
            {formatEth(eth, 4) ?? '—'}
          </span>
          {usd != null ? (
            <span className="ml-2 text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
              {formatUsd(usd)}
            </span>
          ) : null}
        </span>
      </div>
      {note ? (
        <p className="mt-0.5 text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
          {note}
        </p>
      ) : null}
    </div>
  );
}

export function StartBotDialog({ open, onClose, onStarted }: StartBotDialogProps) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<StartBotPayload>(emptyForm);
  const [planMode, setPlanMode] = useState<PlanMode>('volume');
  const [planInputEth, setPlanInputEth] = useState('1');

  const { data: config } = useQuery({
    queryKey: ['config'],
    queryFn: api.getConfig,
    enabled: open,
  });

  useEffect(() => {
    if (!open || !config) return;

    const seed = config.lastSession ?? {
      targetTokenAddress: '',
      ...config.defaults,
    };

    setForm({
      targetTokenAddress: seed.targetTokenAddress ?? '',
      amountMin: seed.amountMin,
      amountMax: seed.amountMax,
      parallelWallets: seed.parallelWallets,
      subWalletNum: seed.subWalletNum,
      endlessMode: seed.endlessMode,
      minBaseBalanceEth: seed.minBaseBalanceEth,
      tradeScheduleMode: seed.tradeScheduleMode,
      tradeSizeBias:
        seed.tradeSizeBias === 'random' ? 'random' : 'max',
    });
    setPlanMode('volume');
    setPlanInputEth('1');
  }, [open, config]);

  const token = form.targetTokenAddress.trim();
  const tokenValid = TOKEN_RE.test(token);

  const { data: preflight, isFetching: preflightLoading } = useQuery({
    queryKey: [
      'preflight',
      token,
      form.amountMin,
      form.amountMax,
      form.subWalletNum,
      form.parallelWallets,
    ],
    queryFn: () =>
      api.getPreflight({
        token,
        amountMin: form.amountMin,
        amountMax: form.amountMax,
        subWalletNum: form.subWalletNum,
        parallelWallets: form.parallelWallets,
      }),
    enabled: open && tokenValid,
    retry: false,
  });

  const planInput = Number(planInputEth);
  const plan = useMemo(() => {
    if (!preflight?.planning || !Number.isFinite(planInput) || planInput <= 0) return null;
    const basis = {
      ...preflight.planning,
      subWalletNum: form.subWalletNum,
      parallelWallets: form.parallelWallets,
    };
    return planMode === 'volume'
      ? planFromTargetVolume(basis, planInput)
      : planFromBudget(basis, planInput);
  }, [preflight?.planning, planInput, planMode, form.subWalletNum, form.parallelWallets]);

  const timeframe = useMemo(() => {
    if (!plan || !config) return null;
    return formatPlanTimeframe(
      estimatePlanDuration(plan, {
        parallelWallets: form.parallelWallets,
        subWalletNum: form.subWalletNum,
        tradeScheduleMode: form.tradeScheduleMode,
        cyclePauseMs: config.cyclePauseMs,
        buyStaggerMinMs: config.buyStaggerMinMs,
        buyStaggerMaxMs: config.buyStaggerMaxMs,
        sellDelayMinMs: config.sellDelayMinMs,
        sellDelayMaxMs: config.sellDelayMaxMs,
        mixedBuyStaggerMinMs: config.mixedBuyStaggerMinMs,
        mixedBuyStaggerMaxMs: config.mixedBuyStaggerMaxMs,
        mixedSellDelayMinMs: config.mixedSellDelayMinMs,
        mixedSellDelayMaxMs: config.mixedSellDelayMaxMs,
      })
    );
  }, [plan, config, form.parallelWallets, form.subWalletNum, form.tradeScheduleMode]);

  const start = useMutation({
    mutationFn: api.startBot,
    onSuccess: () => {
      onStarted();
      queryClient.invalidateQueries({ queryKey: ['status'] });
      onClose();
    },
  });

  const update = <K extends keyof StartBotPayload>(key: K, value: StartBotPayload[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!tokenValid) return;
    start.mutate({ ...form, targetTokenAddress: token });
  };

  const ethUsd = preflight?.ethUsd ?? null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !start.isPending) onClose();
      }}
    >
      <DialogContent
        className="max-w-[calc(100vw-2rem)] sm:max-w-xl"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          document.getElementById('start-token')?.focus();
        }}
        onInteractOutside={(event) => {
          if (start.isPending) event.preventDefault();
        }}
        onEscapeKeyDown={(event) => {
          if (start.isPending) event.preventDefault();
        }}
      >
        <DialogHeader>
          <DialogTitle>Start volume bot</DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4 sm:px-5">
            <Field label="Token address" htmlFor="start-token" hint="ERC-20 on Robinhood Chain">
              <Input
                id="start-token"
                value={form.targetTokenAddress}
                onChange={(e) => update('targetTokenAddress', e.target.value)}
                placeholder="0x…"
                spellCheck={false}
                autoComplete="off"
                required
              />
            </Field>

            {token.length > 0 && !tokenValid ? (
              <p className="text-sm text-[var(--color-danger)]" role="alert">
                Enter a valid 0x address (40 hex characters).
              </p>
            ) : null}

            {tokenValid ? (
              <div className="space-y-4 rounded-lg border border-[var(--color-wire)] bg-[var(--color-paper)] p-3 dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-paper-dark)]">
                <div className="flex gap-2">
                  {(['volume', 'budget'] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => setPlanMode(mode)}
                      className={
                        planMode === mode
                          ? 'rounded-lg bg-[var(--color-ink)] px-3 py-1.5 text-xs font-medium text-[var(--color-paper)] dark:bg-[var(--color-ink-dark)] dark:text-[var(--color-paper-dark)]'
                          : 'rounded-lg border border-[var(--color-wire)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] dark:border-[var(--color-wire-dark)] dark:text-[var(--color-ink-muted-dark)]'
                      }
                    >
                      {mode === 'volume' ? 'Target volume' : 'Budget'}
                    </button>
                  ))}
                </div>

                <Field
                  label={planMode === 'volume' ? 'Target volume (ETH)' : 'Total budget (ETH)'}
                  htmlFor="start-plan-input"
                  hint={
                    planMode === 'volume'
                      ? 'Counted buy + sell volume'
                      : 'Fees + gas you expect to spend'
                  }
                >
                  <Input
                    id="start-plan-input"
                    type="number"
                    step="0.0001"
                    min="0"
                    value={planInputEth}
                    onChange={(e) => setPlanInputEth(e.target.value)}
                  />
                  {Number.isFinite(planInput) && planInput > 0 ? (
                    <p className="mt-1 text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                      {formatUsdApprox(planInput, ethUsd)}
                    </p>
                  ) : null}
                </Field>

                {preflightLoading ? (
                  <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                    Loading economics…
                  </p>
                ) : plan ? (
                  <div className="space-y-3" aria-live="polite">
                    <div className="rounded-lg border border-[var(--color-wire)] bg-[var(--color-surface)] px-3 py-2.5 dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]">
                      {planMode === 'volume' ? (
                        <>
                          <p className="text-sm font-medium text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                            Estimated budget {formatEth(plan.budgetEth, 4)}
                            {plan.budgetUsd != null ? ` · ${formatUsd(plan.budgetUsd)}` : ''}
                          </p>
                          <p className="mt-1 text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                            Delivers {formatEth(plan.targetVolumeEth, 4)} volume across {plan.wallets}{' '}
                            wallets · ~{plan.cycles} cycle{plan.cycles === 1 ? '' : 's'}
                          </p>
                        </>
                      ) : (
                        <>
                          <p className="text-sm font-medium text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                            Predicted volume {formatEth(plan.targetVolumeEth, 4)}
                            {plan.targetVolumeUsd != null ? ` · ${formatUsd(plan.targetVolumeUsd)}` : ''}
                          </p>
                          <p className="mt-1 text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                            Uses {formatEth(plan.budgetEth, 4)} of budget across {plan.wallets} wallets ·
                            ~{plan.cycles} cycle{plan.cycles === 1 ? '' : 's'}
                          </p>
                        </>
                      )}
                      {timeframe ? (
                        <p className="mt-2 text-xs text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                          Expected runtime {timeframe}
                        </p>
                      ) : null}
                    </div>

                    <div className="space-y-2">
                      <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                        Breakdown
                      </p>
                      <PlanBreakdown
                        label="DEX fees"
                        eth={plan.breakdown.dexEth}
                        usd={plan.breakdown.dexUsd}
                      />
                      <PlanBreakdown
                        label="Slippage (est.)"
                        eth={plan.breakdown.slippageEth}
                        usd={plan.breakdown.slippageUsd}
                      />
                      <PlanBreakdown
                        label="Gas (burned est.)"
                        eth={plan.breakdown.gasEth}
                        usd={plan.breakdown.gasUsd}
                      />
                      <PlanBreakdown
                        label="Working capital (peak)"
                        eth={plan.ops.workingCapitalPeakEth}
                        usd={plan.ops.workingCapitalPeakUsd}
                        muted
                        note={`${plan.ops.peakSlots} concurrent wallet${plan.ops.peakSlots === 1 ? '' : 's'} · trade + gas reserve in flight · not in total`}
                      />
                      <PlanBreakdown
                        label="Gas reserve (ops)"
                        eth={plan.ops.gasReservePeakEth}
                        usd={plan.ops.gasReservePeakUsd}
                        muted
                        note={`${formatEth(plan.ops.gasReservePerWalletEth, 4)} per wallet · recycles after each trade`}
                      />
                      <div className="border-t border-[var(--color-wire)] pt-2 dark:border-[var(--color-wire-dark)]">
                        <PlanBreakdown
                          label="Total"
                          eth={plan.breakdown.totalEth}
                          usd={plan.breakdown.totalUsd}
                        />
                      </div>
                    </div>


                    {preflight ? (
                      <p className="text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                        Trade size {formatEth(preflight.trade.effectiveMaxEth, 4)} · pool fee{' '}
                        {preflight.route.feeBpsPerLeg} bps/leg · ~
                        {preflight.costs.predictedCentsPerDollar.toFixed(2)}¢ per $1 volume
                      </p>
                    ) : null}
                  </div>
                ) : (
                  <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                    Enter a positive ETH amount to see the plan.
                  </p>
                )}
              </div>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Trade min (ETH)" htmlFor="start-amount-min">
                <Input
                  id="start-amount-min"
                  type="number"
                  step="0.0001"
                  min="0"
                  value={form.amountMin}
                  onChange={(e) => update('amountMin', Number(e.target.value))}
                  required
                />
              </Field>
              <Field label="Trade max (ETH)" htmlFor="start-amount-max">
                <Input
                  id="start-amount-max"
                  type="number"
                  step="0.0001"
                  min="0"
                  value={form.amountMax}
                  onChange={(e) => update('amountMax', Number(e.target.value))}
                  required
                />
              </Field>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Wallets per cycle" htmlFor="start-wallets">
                <Input
                  id="start-wallets"
                  type="number"
                  min="1"
                  step="1"
                  value={form.subWalletNum}
                  onChange={(e) => update('subWalletNum', Number(e.target.value))}
                  required
                />
              </Field>
              <Field label="Parallel batch" htmlFor="start-parallel">
                <Input
                  id="start-parallel"
                  type="number"
                  min="1"
                  step="1"
                  value={form.parallelWallets}
                  onChange={(e) => update('parallelWallets', Number(e.target.value))}
                  required
                />
              </Field>
            </div>

            <Field label="Stop below (ETH)" htmlFor="start-floor" hint="Base wallet floor">
              <Input
                id="start-floor"
                type="number"
                step="0.001"
                min="0"
                value={form.minBaseBalanceEth}
                onChange={(e) => update('minBaseBalanceEth', Number(e.target.value))}
                required
              />
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Trade mode" htmlFor="start-mode">
                <select
                  id="start-mode"
                  value={form.tradeScheduleMode}
                  onChange={(e) =>
                    update('tradeScheduleMode', e.target.value as StartBotPayload['tradeScheduleMode'])
                  }
                  className="flex h-11 w-full rounded-lg border border-[var(--color-wire)] bg-[var(--color-surface)] px-3 text-sm dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]"
                >
                  <option value="pipeline">Pipeline</option>
                  <option value="mixed">Mixed</option>
                </select>
              </Field>
              <Field label="Endless loop" htmlFor="start-endless">
                <label className="flex h-11 items-center gap-2 text-sm">
                  <input
                    id="start-endless"
                    type="checkbox"
                    checked={form.endlessMode}
                    onChange={(e) => update('endlessMode', e.target.checked)}
                    className="h-4 w-4 rounded border-[var(--color-wire)]"
                  />
                  Repeat until balance floor
                </label>
              </Field>
              <Field
                label="Random trade sizes"
                htmlFor="start-random-sizes"
                hint="Off = always use max trade size (more gas-efficient)"
              >
                <label className="flex h-11 items-center gap-2 text-sm">
                  <input
                    id="start-random-sizes"
                    type="checkbox"
                    checked={form.tradeSizeBias === 'random'}
                    onChange={(e) =>
                      update('tradeSizeBias', e.target.checked ? 'random' : 'max')
                    }
                    className="h-4 w-4 rounded border-[var(--color-wire)]"
                  />
                  Vary size between min and max
                </label>
              </Field>
            </div>
          </div>

          {start.isError ? (
            <p className="px-5 text-sm text-[var(--color-danger)]" role="alert">
              {(start.error as Error).message}
            </p>
          ) : null}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={start.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={!tokenValid || start.isPending} aria-busy={start.isPending}>
              {start.isPending ? 'Starting…' : 'Start volume bot'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
