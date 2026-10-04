import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { chainName, nativeSymbol, time, type Order } from "@/lib/terminal-api";

export default function Performance({ orders }: { orders: Order[] }) {
  return (
    <div className="performance">
      {(["bsc", "robinhood"] as const).map((chain) => {
        let total = 0;
        const sells = orders.filter(
          (o) => o.chain === chain && o.side === "sell",
        );
        const points = sells
          .filter((o) => o.pnl_native != null)
          .sort((a, b) => (a.time || "").localeCompare(b.time || ""))
          .map((o) => ({
            time: time(o.time),
            pnl: (total += o.pnl_native || 0),
          }));
        return (
          <section key={chain}>
            <div className="row-between">
              <b>
                {chainName(chain)} · {nativeSymbol(chain)}
              </b>
              <small>
                已记账 {points.length} / {sells.length} 笔卖出
              </small>
            </div>
            {points.length ? (
              <div className="chart-area">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={points}
                    margin={{ top: 15, right: 12, bottom: 0, left: 0 }}
                  >
                    <XAxis dataKey="time" hide />
                    <YAxis
                      width={78}
                      tick={{ fontSize: 10, fill: "#9da5ac" }}
                      tickFormatter={(v) => Number(v).toPrecision(3)}
                    />
                    <Tooltip
                      contentStyle={{
                        background: "#161a1d",
                        border: "1px solid #343a40",
                      }}
                      labelStyle={{ color: "#aaa" }}
                    />
                    <Line
                      type="stepAfter"
                      dataKey="pnl"
                      name="累计已记账盈亏"
                      stroke={chain === "bsc" ? "#e8b75c" : "#58c5a5"}
                      dot={points.length < 3}
                      strokeWidth={2}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <div className="empty">暂无完整结算数据</div>
            )}
          </section>
        );
      })}
    </div>
  );
}
