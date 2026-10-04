import { useMemo, useState } from 'react';
import {
  CartesianGrid,
  ComposedChart,
  Line,
  Bar,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TimeseriesPoint } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { cn, formatEth, formatUsd } from '@/lib/utils';

type Currency = 'eth' | 'usd';

type ChartPoint = TimeseriesPoint & {
  volume: number;
  base: number;
};

function formatAxisValue(value: number, currency: Currency) {
  if (currency === 'eth') return Number(value).toFixed(3);
  if (Math.abs(value) >= 1000) {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(value);
  }
  return formatUsd(value, 0) ?? `$${value.toFixed(0)}`;
}

export function SessionChart({
  points,
  ethUsd,
}: {
  points: TimeseriesPoint[];
  ethUsd?: number | null;
}) {
  const [currency, setCurrency] = useState<Currency>('eth');
  const usdAvailable = ethUsd != null && !Number.isNaN(ethUsd);

  const chartData = useMemo<ChartPoint[]>(() => {
    return points.map((point) => ({
      ...point,
      volume:
        currency === 'usd' && usdAvailable
          ? point.volumeEth * ethUsd
          : point.volumeEth,
      base:
        currency === 'usd' && usdAvailable
          ? point.baseEth * ethUsd
          : point.baseEth,
    }));
  }, [points, currency, ethUsd, usdAvailable]);

  if (points.length === 0) {
    return (
      <p className="py-10 text-center text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
        No cycle data yet. Complete a cycle to see the chart.
      </p>
    );
  }

  const unitLabel = currency === 'usd' ? 'USD' : 'ETH';

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <div
          className="inline-flex rounded-lg border border-[var(--color-wire)] bg-[var(--color-surface)] p-0.5 dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]"
          role="group"
          aria-label="Chart currency"
        >
          <Button
            type="button"
            size="sm"
            variant={currency === 'eth' ? 'default' : 'ghost'}
            className={cn('h-8 min-h-8 min-w-[3.25rem] px-3')}
            onClick={() => setCurrency('eth')}
          >
            ETH
          </Button>
          <Button
            type="button"
            size="sm"
            variant={currency === 'usd' ? 'default' : 'ghost'}
            className={cn('h-8 min-h-8 min-w-[3.25rem] px-3')}
            onClick={() => setCurrency('usd')}
            disabled={!usdAvailable}
            title={usdAvailable ? undefined : 'USD rate unavailable'}
          >
            USD
          </Button>
        </div>
      </div>
      <div className="h-52 w-full sm:h-64">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="var(--color-wire)" vertical={false} />
            <XAxis
              dataKey="cycle"
              tick={{ fill: 'var(--color-ink-muted)', fontSize: 12 }}
              tickLine={false}
              axisLine={{ stroke: 'var(--color-wire)' }}
            />
            <YAxis
              yAxisId="volume"
              tick={{ fill: 'var(--color-ink-muted)', fontSize: 12 }}
              tickLine={false}
              axisLine={false}
              width={52}
              tickFormatter={(value) => formatAxisValue(Number(value), currency)}
            />
            <YAxis
              yAxisId="base"
              orientation="right"
              tick={{ fill: 'var(--color-ink-muted)', fontSize: 12 }}
              tickLine={false}
              axisLine={false}
              width={52}
              tickFormatter={(value) => formatAxisValue(Number(value), currency)}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--color-surface)',
                border: '1px solid var(--color-wire)',
                borderRadius: 8,
                color: 'var(--color-ink)',
              }}
              formatter={(value, name) => {
                const numeric = Number(value);
                if (name === 'volume') {
                  return currency === 'usd'
                    ? [formatUsd(numeric, 2, true) ?? '—', `Volume (${unitLabel})`]
                    : [formatEth(numeric, 4), 'Volume'];
                }
                if (name === 'base') {
                  return currency === 'usd'
                    ? [formatUsd(numeric, 2, true) ?? '—', `Base balance (${unitLabel})`]
                    : [`${numeric.toFixed(4)} ETH`, 'Base balance'];
                }
                return [value, name];
              }}
              labelFormatter={(label) => `Cycle ${label}`}
            />
            <Bar
              yAxisId="volume"
              dataKey="volume"
              fill="var(--color-chart-volume)"
              fillOpacity={0.85}
              radius={[3, 3, 0, 0]}
              maxBarSize={28}
            />
            <Line
              yAxisId="base"
              type="monotone"
              dataKey="base"
              stroke="var(--color-chart-balance)"
              strokeWidth={2}
              dot={{ r: 2, fill: 'var(--color-chart-balance)' }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="flex flex-wrap gap-4 text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
        <span className="inline-flex items-center gap-1.5">
          <span
            className="h-2.5 w-2.5 rounded-sm"
            style={{ background: 'var(--color-chart-volume)', opacity: 0.85 }}
          />
          Volume / cycle ({unitLabel})
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="h-0.5 w-3 rounded-full"
            style={{ background: 'var(--color-chart-balance)' }}
          />
          Base balance ({unitLabel})
        </span>
        {currency === 'usd' && usdAvailable ? (
          <span className="text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
            USD uses current ETH price (~{formatUsd(ethUsd, 0)})
          </span>
        ) : null}
      </div>
    </div>
  );
}
