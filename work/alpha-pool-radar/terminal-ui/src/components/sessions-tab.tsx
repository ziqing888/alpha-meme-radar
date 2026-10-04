import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { api } from '@/lib/api';
import { formatDateTime, formatEth, shortAddress } from '@/lib/utils';

function statusTone(status: string) {
  switch (status) {
    case 'active':
      return 'text-[var(--color-ok)]';
    case 'completed':
      return 'text-[var(--color-info)]';
    case 'stopped':
    default:
      return 'text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]';
  }
}

export function SessionsTab() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.getSessions(),
    refetchInterval: 15_000,
  });

  if (isLoading) {
    return <p className="text-sm text-[var(--color-ink-muted)]">Loading sessions…</p>;
  }

  if (error) {
    return (
      <p className="text-sm text-[var(--color-danger)]" role="alert">
        {(error as Error).message}
      </p>
    );
  }

  const sessions = data?.sessions ?? [];

  if (sessions.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Sessions</CardTitle>
          <CardDescription>Past and active bot runs stored in the local database</CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
            No sessions yet. Start the bot to create one.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Sessions</CardTitle>
        <CardDescription>Past and active bot runs with per-session totals</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {sessions.map((session) => (
          <div
            key={session.id}
            className="rounded-xl border border-[var(--color-wire)] px-4 py-3 dark:border-[var(--color-wire-dark)]"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 space-y-1">
                <p className="font-mono text-sm text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                  {shortAddress(session.targetTokenAddress)}
                </p>
                <p className="text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                  {session.startedAt ? formatDateTime(session.startedAt) : '—'}
                  {session.endedAt ? ` → ${formatDateTime(session.endedAt)}` : ''}
                </p>
              </div>
              <p className={`text-xs font-medium uppercase tracking-[0.12em] ${statusTone(session.status ?? 'stopped')}`}>
                {session.isActive ? 'active' : session.status ?? 'stopped'}
              </p>
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
              <div>
                <dt className="text-[0.65rem] uppercase tracking-[0.12em] text-[var(--color-ink-muted)]">Cycles</dt>
                <dd className="font-mono tabular-nums">{session.totals.cycles}</dd>
              </div>
              <div>
                <dt className="text-[0.65rem] uppercase tracking-[0.12em] text-[var(--color-ink-muted)]">Volume</dt>
                <dd className="font-mono tabular-nums">{formatEth(session.totals.volumeEth, 4) ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-[0.65rem] uppercase tracking-[0.12em] text-[var(--color-ink-muted)]">Buys</dt>
                <dd className="font-mono tabular-nums">{session.totals.buys}</dd>
              </div>
              <div>
                <dt className="text-[0.65rem] uppercase tracking-[0.12em] text-[var(--color-ink-muted)]">Sells</dt>
                <dd className="font-mono tabular-nums">{session.totals.sells}</dd>
              </div>
              <div>
                <dt className="text-[0.65rem] uppercase tracking-[0.12em] text-[var(--color-ink-muted)]">Wallets</dt>
                <dd className="font-mono tabular-nums">{session.subWalletNum}</dd>
              </div>
            </dl>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
