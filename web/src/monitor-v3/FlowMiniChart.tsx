import { formatUsd } from "./formatters";
import type { MonitorMarketPoint } from "./types";

function finiteValues(values: Array<number | null>): number[] {
  return values.filter((value): value is number => value !== null && Number.isFinite(value));
}

function points(values: Array<number | null>, width: number, height: number): string {
  const known = finiteValues(values);
  if (known.length === 0) return "";
  const min = Math.min(...known);
  const max = Math.max(...known);
  const range = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  return values
    .map((value, index) => {
      if (value === null || !Number.isFinite(value)) return null;
      const x = values.length > 1 ? index * step : width / 2;
      const y = height - ((value - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .filter((value): value is string => value !== null)
    .join(" ");
}

export function FlowMiniChart({ snapshots }: { snapshots: MonitorMarketPoint[] }) {
  const flow = snapshots.map((snapshot) => snapshot.net_buy_flow_1m_usd);
  const liquidity = snapshots.map((snapshot) => snapshot.liquidity_usd);
  const hasFlow = finiteValues(flow).length > 0;
  const hasLiquidity = finiteValues(liquidity).length > 0;

  return (
    <section className="monitor-v3-detail-section monitor-v3-chart-section" aria-labelledby="monitor-v3-chart-title">
      <h3 id="monitor-v3-chart-title">资金流与流动性</h3>
      <div className="monitor-v3-chart" role="img" aria-label="资金流与流动性走势">
        {hasFlow || hasLiquidity ? (
          <svg viewBox="0 0 520 150" preserveAspectRatio="none" aria-hidden="true">
            <line x1="0" y1="75" x2="520" y2="75" className="chart-baseline" />
            {hasLiquidity && <polyline points={points(liquidity, 520, 140)} className="chart-liquidity" />}
            {hasFlow && <polyline points={points(flow, 520, 140)} className="chart-flow" />}
          </svg>
        ) : (
          <span className="monitor-v3-unavailable">暂无可绘制样本</span>
        )}
      </div>
      <div className="monitor-v3-chart-legend">
        <span className="is-flow">净买入流 {formatUsd(flow[flow.length - 1])}</span>
        <span className="is-liquidity">流动性 {formatUsd(liquidity[liquidity.length - 1])}</span>
        <span>样本 {snapshots.length}</span>
      </div>
    </section>
  );
}
