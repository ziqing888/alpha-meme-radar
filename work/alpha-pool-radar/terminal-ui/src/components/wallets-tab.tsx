import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ExternalLink } from 'lucide-react';
import { useEffect, useState } from 'react';
import { SessionChart } from '@/components/session-chart';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { api, type RecoveryScope } from '@/lib/api';
import { cn, formatDateTime, shortAddress } from '@/lib/utils';

function walletStatusTone(status: string) {
  switch (status) {
    case 'gathered':
      return 'border-[color-mix(in_srgb,var(--color-ok)_35%,var(--color-wire))] bg-[var(--color-ok-subtle)] text-[var(--color-ok)]';
    case 'sold':
    case 'bought':
      return 'border-[color-mix(in_srgb,var(--color-info)_35%,var(--color-wire))] bg-[var(--color-info-subtle)] text-[var(--color-info)]';
    case 'funded':
      return 'border-[color-mix(in_srgb,var(--color-chart-volume)_35%,var(--color-wire))] bg-[var(--color-info-subtle)] text-[var(--color-chart-volume)]';
    case 'pending':
    default:
      return 'border-[var(--color-wire)] text-[var(--color-ink-muted)] dark:border-[var(--color-wire-dark)]';
  }
}

function WalletStatusBadge({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'rounded-full border px-2 py-0.5 text-[0.65rem] font-medium uppercase tracking-[0.08em]',
        walletStatusTone(status)
      )}
    >
      {status}
    </span>
  );
}

function WalletsTable({
  explorerTxUrl,
  cycle,
}: {
  explorerTxUrl: string;
  cycle: NonNullable<Awaited<ReturnType<typeof api.getWallets>>['cycle']>;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--color-wire)] dark:border-[var(--color-wire-dark)]">
      <table className="min-w-full text-left text-sm">
        <thead className="border-b border-[var(--color-wire)] bg-[var(--color-surface)] text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]">
          <tr>
            <th className="px-3 py-2">#</th>
            <th className="px-3 py-2">Address</th>
            <th className="px-3 py-2">Trade</th>
            <th className="px-3 py-2">Funded</th>
            <th className="px-3 py-2">Balance</th>
            <th className="px-3 py-2">Status</th>
            <th className="px-3 py-2">Last tx</th>
          </tr>
        </thead>
        <tbody>
          {cycle.wallets.map((wallet) => (
            <tr
              key={wallet.address}
              className="border-b border-[var(--color-wire)] last:border-0 dark:border-[var(--color-wire-dark)]"
            >
              <td className="px-3 py-2 font-mono tabular-nums">{wallet.index}</td>
              <td className="px-3 py-2 font-mono text-xs" translate="no">
                {shortAddress(wallet.address)}
              </td>
              <td className="px-3 py-2 font-mono tabular-nums">{wallet.amount}</td>
              <td className="px-3 py-2 font-mono tabular-nums">
                {wallet.funded && wallet.funded !== 0 ? String(wallet.funded) : '—'}
              </td>
              <td className="px-3 py-2 font-mono tabular-nums">
                {wallet.balanceEth != null ? (
                  <span
                    className={Number(wallet.balanceEth) > 0.00001 ? 'tone-warn' : undefined}
                  >
                    {Number(wallet.balanceEth).toFixed(5)}
                  </span>
                ) : (
                  '—'
                )}
              </td>
              <td className="px-3 py-2">
                <WalletStatusBadge status={wallet.status} />
              </td>
              <td className="px-3 py-2">
                {wallet.lastTxHash ? (
                  <a
                    href={`${explorerTxUrl}${wallet.lastTxHash}`}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="inline-flex items-center gap-1 font-mono text-xs text-[var(--color-info)] underline-offset-2 hover:underline"
                  >
                    <span translate="no">{shortAddress(wallet.lastTxHash)}</span>
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                  </a>
                ) : (
                  '—'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function WalletsTab() {
  const queryClient = useQueryClient();
  const [selectedCycleFile, setSelectedCycleFile] = useState<string | undefined>();

  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: api.getStatus,
    refetchInterval: 10_000,
  });
  const { data: timeseries } = useQuery({
    queryKey: ['timeseries'],
    queryFn: async () => (await api.getTimeseries()).points,
    refetchInterval: 10_000,
  });

  const {
    data: wallets,
    isLoading: walletsLoading,
    error: walletsError,
  } = useQuery({
    queryKey: ['wallets', selectedCycleFile],
    queryFn: () => api.getWallets(selectedCycleFile),
    refetchInterval: 10_000,
  });

  useEffect(() => {
    if (!wallets?.cycle?.file) return;
    setSelectedCycleFile((current) => current ?? wallets.cycle!.file);
  }, [wallets?.cycle?.file]);

  const recoverOpts = {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['status'] });
      queryClient.invalidateQueries({ queryKey: ['wallets'] });
    },
  };

  const gather = useMutation({
    mutationFn: (scope: RecoveryScope) => api.gatherFunds(scope),
    ...recoverOpts,
  });

  const liquidate = useMutation({
    mutationFn: (scope: RecoveryScope) => api.liquidateFunds(scope),
    ...recoverOpts,
  });

  const recoveryBusy = gather.isPending || liquidate.isPending;
  const activeCycleFile = selectedCycleFile ?? wallets?.cycle?.file;
  const cycleCount = wallets?.cycles.length ?? 0;
  const activeCycle = wallets?.cycle;

  function scopeForSelectedCycle(): RecoveryScope | null {
    if (!activeCycleFile) return null;
    return { file: activeCycleFile };
  }

  function runGather() {
    const scope = scopeForSelectedCycle();
    if (!scope) return;
    gather.mutate(scope);
  }

  function runLiquidate() {
    const scope = scopeForSelectedCycle();
    if (!scope) return;
    liquidate.mutate(scope);
  }

  function runAllFiles(action: 'gather' | 'liquidate') {
    if (
      !window.confirm(
        `${action === 'gather' ? 'Gather' : 'Liquidate'} all ${cycleCount} cycle file(s)? ` +
          'This can take several minutes.'
      )
    ) {
      return;
    }
    const mutate = action === 'gather' ? gather.mutate : liquidate.mutate;
    mutate({ all: true });
  }

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader>
          <CardTitle>Session Volume</CardTitle>
          <CardDescription>Per-cycle volume (bars) and base wallet balance (line)</CardDescription>
        </CardHeader>
        <CardContent>
          <SessionChart points={timeseries ?? []} ethUsd={status?.wallet.ethUsd} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="gap-3">
          <div>
            <CardTitle>Wallets</CardTitle>
            {activeCycle ? (
              <p className="mt-1 text-sm text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                {activeCycle.startedAt ? formatDateTime(activeCycle.startedAt) : 'Unknown start'}
                <span className="text-[var(--color-ink-muted)]">
                  {' '}
                  · {activeCycle.wallets.length} wallets
                </span>
              </p>
            ) : (
              <CardDescription>Sub-wallet cycles from bot runs</CardDescription>
            )}
          </div>

          {cycleCount > 0 ? (
            <div className="flex flex-col gap-2">
              <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center">
                <select
                  aria-label="Cycle file"
                  className="h-10 w-full rounded-md border border-[var(--color-wire)] bg-transparent px-3 text-sm text-[var(--color-ink)] dark:border-[var(--color-wire-dark)] dark:text-[var(--color-ink-dark)]"
                  value={activeCycleFile ?? ''}
                  onChange={(event) => setSelectedCycleFile(event.target.value || undefined)}
                  disabled={recoveryBusy}
                >
                  {(wallets?.cycles ?? []).map((item) => (
                    <option key={item.file} value={item.file}>
                      {item.file}
                    </option>
                  ))}
                </select>
                <Button
                  type="button"
                  variant="outline"
                  className="h-10 w-full sm:w-auto"
                  onClick={runGather}
                  disabled={recoveryBusy || !activeCycleFile}
                >
                  {gather.isPending ? 'Gathering…' : 'Gather'}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  className="h-10 w-full sm:w-auto"
                  onClick={runLiquidate}
                  disabled={recoveryBusy || !activeCycleFile}
                >
                  {liquidate.isPending ? 'Liquidating…' : 'Liquidate'}
                </Button>
              </div>
              {cycleCount > 1 ? (
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--color-ink-muted)]">
                  <button
                    type="button"
                    className="text-[var(--color-info)] underline-offset-2 hover:underline disabled:opacity-50"
                    disabled={recoveryBusy}
                    onClick={() => runAllFiles('gather')}
                  >
                    Gather all {cycleCount} files
                  </button>
                  <button
                    type="button"
                    className="text-[var(--color-info)] underline-offset-2 hover:underline disabled:opacity-50"
                    disabled={recoveryBusy}
                    onClick={() => runAllFiles('liquidate')}
                  >
                    Liquidate all {cycleCount} files
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </CardHeader>

        <CardContent className="space-y-3">
          {gather.isSuccess ? (
            <p className="text-sm text-[var(--color-ok)]" role="status">
              Gather: recovered {gather.data.recoveredEth} ETH from {gather.data.gathered} wallet(s)
            </p>
          ) : null}
          {gather.isError ? (
            <p className="text-sm text-[var(--color-danger)]" role="alert">
              Gather: {(gather.error as Error).message}
            </p>
          ) : null}
          {liquidate.isSuccess ? (
            <p className="text-sm text-[var(--color-ok)]" role="status">
              Liquidate: sold {liquidate.data.sold}, gathered {liquidate.data.gathered} wallet(s),
              recovered {liquidate.data.recoveredEth} ETH
              {liquidate.data.sellFailed > 0
                ? ` (${liquidate.data.sellFailed} sell failure(s))`
                : ''}
            </p>
          ) : null}
          {liquidate.isError ? (
            <p className="text-sm text-[var(--color-danger)]" role="alert">
              Liquidate: {(liquidate.error as Error).message}
            </p>
          ) : null}
          {walletsLoading ? (
            <p className="text-sm text-[var(--color-ink-muted)]">Loading wallets…</p>
          ) : walletsError || !wallets?.cycle ? (
            <p className="text-sm text-[var(--color-ink-muted)]">
              No wallet cycles found yet. Start the bot to generate wallets.
            </p>
          ) : (
            <WalletsTable cycle={wallets.cycle} explorerTxUrl={wallets.explorerTxUrl} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
