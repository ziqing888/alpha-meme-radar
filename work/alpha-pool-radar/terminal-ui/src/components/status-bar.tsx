import { useQuery } from '@tanstack/react-query';
import { BarChart3, Bot, Coins, Server, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { SiEthereum } from 'react-icons/si';
import { api } from '@/lib/api';
import { cn, formatCompactUsd, formatEth, formatTickerUsd, formatUsd } from '@/lib/utils';

function FooterItem({
  icon: Icon,
  iconClassName,
  label,
  value,
  highlight = false,
  mark,
}: {
  icon?: LucideIcon;
  iconClassName?: string;
  label?: string;
  value: string;
  highlight?: boolean;
  mark?: ReactNode;
}) {
  return (
    <div className="flex shrink-0 items-center gap-1.5 whitespace-nowrap px-2.5 py-1 text-[11px] sm:px-3">
      {mark ??
        (Icon ? (
          <Icon
            className={cn(
              'h-3.5 w-3.5 shrink-0 text-[var(--color-ink-muted)] dark:text-[#8a8a8a]',
              iconClassName
            )}
            aria-hidden="true"
          />
        ) : null)}
      {label ? (
        <span className="text-[var(--color-ink-muted)] dark:text-[#8a8a8a]">
          {label}
        </span>
      ) : null}
      <span
        className={cn(
          'font-medium tabular-nums text-[var(--color-ink)] dark:text-[#e8e8e8]',
          highlight && 'text-[#7eb6ff] dark:text-[#7eb6ff]'
        )}
      >
        {value}
      </span>
    </div>
  );
}

function FooterDivider() {
  return (
    <span
      className="mx-0.5 h-4 w-px shrink-0 bg-[var(--color-wire)] dark:bg-[#2a2a2a]"
      aria-hidden="true"
    />
  );
}

export function StatusBar() {
  const { data, isError, isLoading, isFetching } = useQuery({
    queryKey: ['status'],
    queryFn: api.getStatus,
    refetchInterval: 5_000,
    retry: 1,
  });

  const apiOnline = !isError && !isLoading;
  const apiLabel = isError
    ? 'Offline'
    : isLoading
      ? 'Connecting'
      : isFetching
        ? 'Syncing'
        : 'Online';

  const botRunning = data?.bot.running ?? false;
  const botLabel = botRunning ? 'Running' : 'Stopped';

  const ethPrice = formatTickerUsd(data?.wallet.ethUsd ?? null);
  const sessionVolumeEth = data?.stats.totals?.volumeEth;
  const sessionVolumeLabel =
    sessionVolumeEth != null
      ? (formatEth(sessionVolumeEth, 3) ?? '—')
      : '—';

  const token = data?.token;
  const tokenPrice = formatUsd(token?.priceUsd ?? null) ?? '—';
  const marketCap = formatCompactUsd(token?.marketCapUsd);

  return (
    <footer
      className="fixed inset-x-0 bottom-0 z-50 rounded-t-2xl border-t border-[var(--color-wire)] bg-[var(--color-surface)] shadow-[0_-10px_40px_rgba(0,0,0,0.12)] dark:border-[#2a2a2a] dark:bg-[#111111] dark:shadow-[0_-12px_48px_rgba(0,0,0,0.45)]"
      style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
      aria-label="Live status"
    >
      <div className="flex h-11 items-center overflow-x-auto px-2 [-ms-overflow-style:none] [scrollbar-width:none] sm:px-3 [&::-webkit-scrollbar]:hidden">
        <div className="flex min-w-max flex-1 items-center">
          <FooterItem icon={Server} label="API" value={apiLabel} />
          <FooterDivider />
          <FooterItem
            icon={Bot}
            iconClassName={
              botRunning ? 'text-[var(--color-ok)] dark:text-[var(--color-ok)]' : undefined
            }
            label="Bot"
            value={botLabel}
          />
          <FooterDivider />
          <FooterItem
            icon={BarChart3}
            label="Volume"
            value={sessionVolumeLabel}
          />
          <FooterDivider />
          {token ? (
            <FooterItem
              icon={Coins}
              label={token.symbol ?? "Token"}
              value={marketCap ? `${tokenPrice} · ${marketCap}` : tokenPrice}
            />
          ) : null}
        </div>

        <div className="ml-auto flex shrink-0 items-center pl-2">
          <FooterDivider />
          <FooterItem
            mark={
              <SiEthereum
                className="h-3.5 w-3.5 shrink-0 text-[#627eea]"
                aria-hidden="true"
              />
            }
            value={ethPrice ?? '—'}
            highlight
          />
          <span className="sr-only">
            API {apiOnline ? 'online' : 'offline'}, bot {botLabel}
          </span>
        </div>
      </div>
    </footer>
  );
}
