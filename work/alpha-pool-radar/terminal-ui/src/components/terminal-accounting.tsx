import type { RefObject } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  atomic,
  executionReasons,
  native,
  nativeSymbol,
  time,
  type Order,
} from "@/lib/terminal-api";

const status = (s?: string) =>
  s === "verified"
    ? "已核验"
    : s === "unavailable"
      ? "证据不可用"
      : s === "stale"
        ? "待重新核验"
        : "待核验";
const reasons: Record<string, string> = {
  ...executionReasons,
  awaiting_verification: "等待回查",
  trace_unavailable_or_invalid: "RPC 未提供有效内部转账证据",
  fifo_coverage_unavailable: "交易历史未完整覆盖，成本待核验",
  fifo_not_verified: "成本分摊待核验",
  public_rpc_unavailable: "链上查询暂不可用",
  rpc_unavailable: "链上查询暂不可用",
};
export default function Accounting({
  order,
  onClose,
  open = !!order,
  returnFocusRef,
  fallbackFocusRef,
}: {
  order: Order | null;
  onClose: () => void;
  open?: boolean;
  returnFocusRef?: RefObject<HTMLElement | null>;
  fallbackFocusRef?: RefObject<HTMLElement | null>;
}) {
  const a = order?.accounting;
  const value = (n?: string | null) =>
    `${native(atomic(n || undefined))} ${order ? nativeSymbol(order.chain) : ""}`;
  return (
    <Dialog
      open={open}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent returnFocusRef={returnFocusRef} fallbackFocusRef={fallbackFocusRef}>
        <DialogHeader>
          <DialogTitle>{order ? `${order.symbol} · ` : ""}链上记账</DialogTitle>
          <DialogDescription>
            {order ? `${order.side === "buy" ? "买入" : "卖出"} · ${time(order.time)}` : "最新成交状态"}
          </DialogDescription>
        </DialogHeader>
        {!order ? (
          <div className="detail-body" role="status">
            成交已不在最新快照中，请核对成交记录。
          </div>
        ) : <div className="detail-body">
          <dl>
            <div>
              <dt>交易回执</dt>
              <dd>{status(a?.receipt_status)}</dd>
            </div>
            <div>
              <dt>内部资金转移</dt>
              <dd>{status(a?.trace_status)}</dd>
            </div>
            <div>
              <dt>成本分摊</dt>
              <dd>{status(a?.fifo_status)}</dd>
            </div>
            <div>
              <dt>本笔 Gas（含回执附加费）</dt>
              <dd>{value(a?.gas_native_atomic)}</dd>
            </div>
            <div>
              <dt>原生币实际支付</dt>
              <dd>{value(a?.native_spent_atomic)}</dd>
            </div>
            <div>
              <dt>卖出净到账</dt>
              <dd>{value(a?.net_native_received_atomic)}</dd>
            </div>
            <div>
              <dt>分摊成本</dt>
              <dd>{value(a?.cost_basis_native_atomic)}</dd>
            </div>
            <div>
              <dt>核验交易盈亏</dt>
              <dd>{value(a?.pnl_native_atomic)}</dd>
            </div>
            <div>
              <dt>核验时间</dt>
              <dd>{time(a?.verified_at)}</dd>
            </div>
          </dl>
          <p className="form-message">
            {reasons[a?.reason || ""] || a?.reason || "等待链上证据"}
            ；独立授权交易费用未计入本回查口径。
          </p>
        </div>}
      </DialogContent>
    </Dialog>
  );
}
