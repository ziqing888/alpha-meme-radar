import { useEffect, useRef, useState } from "react";
import { Play, Pause, Square, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  chainName,
  executionReasons,
  terminalApi,
  time,
  type Chain,
  type Runtime,
  type ControlState,
  type StrategyView,
} from "@/lib/terminal-api";

type Action = "start" | "stop" | "pause" | "resume" | "apply";
type Confirmation = Readonly<{
  chain: Chain;
  wallet: string;
  pid: number;
  action: Action;
  amount: string;
  running: boolean;
  exitOnly: boolean;
}>;
const labels: Record<Action, string> = {
  start: "启动自动交易",
  stop: "停止执行器",
  pause: "暂停开仓",
  resume: "恢复开仓",
  apply: "应用金额",
};
const errors: Record<string, string> = {
  ...executionReasons,
  command_delivery_unknown: "指令投递结果未知，请核对回执后再操作。",
  start_outcome_unknown: "启动结果未知，请核对运行进程后再操作。",
  worker_upgrade_required: "当前执行器为旧版，重启后才能接收网页控制。",
  worker_heartbeat_stale: "执行器心跳已过期，当前参数待核实。",
  already_running: "执行器已在运行，没有重复启动。",
  already_starting: "执行器正在启动，请等候状态回执。",
  command_pending: "上一条指令正在等待执行器回执。",
  worker_changed: "运行实例已变化，请刷新后重试。",
  process_state_unavailable: "无法核实运行进程，请稍后重试。",
  wallet_address_mismatch: "钱包已变化，请刷新页面。",
  worker_not_running: "执行器未运行。",
  start_failed: "执行器启动失败，请检查本机配置。",
  open_positions: "仍有持仓，请使用暂停开仓以继续管理退出。",
  pending_orders: "仍有待确认交易，暂不能停止。",
};
const states: Record<string, string> = {
  pending: "指令已提交，等待执行器回执",
  applied: "最近控制指令已确认",
  rejected: "执行器拒绝了该指令",
  starting: "正在启动，等待进程和心跳",
  timed_out: "未收到回执，未确认生效",
  start_failed: "启动失败，未发现运行进程",
  legacy_worker: "旧版进程运行中，网页控制需重启执行器后启用",
  heartbeat_stale: "执行器心跳过期，参数待核实",
  conflict: "检测到同链多个执行器，控制已锁定",
  outcome_unknown: "进程已退出，未确认指令结果",
};

const shown = (value?: number | null) => value == null ? "待确认" : String(value);
const compactUsd = (value?: number | null) => {
  if (value == null) return "待确认";
  const compact = value >= 1_000_000
    ? `${Number((value / 1_000_000).toFixed(1))}M`
    : value >= 1_000
      ? `${Number((value / 1_000).toFixed(1))}K`
      : String(value);
  return `$${compact}`;
};
const minutes = (seconds?: number | null) =>
  seconds == null ? "待确认" : `${Number((seconds / 60).toFixed(1))} 分钟`;
const percent = (value?: number | null) => value == null ? "待确认" : `${value}%`;

export default function Strategy({
  chain,
  runtime,
  verified,
  control,
  strategy,
  effectiveStrategy,
  wallet,
  onChanged,
}: {
  chain: Chain;
  runtime?: Runtime;
  verified: boolean;
  draft?: string | null;
  control?: ControlState;
  strategy?: StrategyView | null;
  effectiveStrategy?: StrategyView | null;
  wallet: string | null;
  onChanged: () => unknown;
}) {
  const configuredAmount = chain === "bsc" ? "1" : "2";
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const [confirm, setConfirm] = useState<Confirmation | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const sectionRef = useRef<HTMLElement | null>(null);
  const matched = submitted != null && control?.request_id === submitted;
  const finished =
    matched &&
    [
      "applied",
      "rejected",
      "timed_out",
      "start_failed",
      "outcome_unknown",
    ].includes(control?.state || "");
  const awaitingOwn = submitted != null && !finished && !expired;
  useEffect(() => {
    if (!submitted || finished) return;
    const timer = setTimeout(() => setExpired(true), 125_000);
    return () => clearTimeout(timer);
  }, [submitted, finished]);
  const waiting =
    busy ||
    awaitingOwn ||
    control?.state === "pending" ||
    control?.state === "starting";
  const status = awaitingOwn
    ? "指令已提交，等待本次执行器回执"
    : expired && !finished
      ? "本次指令未收到回执，未确认生效"
      : states[control?.state || ""] || "";
  const canCommand = verified && Boolean(wallet) && !waiting;
  const running = Boolean(runtime?.running);
  const canApply = canCommand && running && control?.supported;
  const confirmationValid = Boolean(
    confirm && canCommand &&
    confirm.chain === chain && confirm.wallet === wallet &&
    confirm.pid === (runtime?.pid || 0) && confirm.running === running &&
    confirm.exitOnly === Boolean(runtime?.exit_only) &&
    (confirm.action === "start" ? !running : running && control?.supported),
  );
  useEffect(() => {
    if (confirm && !confirmationValid) {
      setConfirm(null);
      setMessage("运行状态或确认目标已变化，请核对后重新确认。");
    }
  }, [confirm, confirmationValid]);
  function beginConfirmation(action: Action, opener: HTMLElement | null) {
    if (!canCommand || !wallet) return;
    if (action === "start" ? running : !running || !control?.supported) return;
    returnFocusRef.current = opener;
    setMessage("");
    setConfirm({
      chain, wallet, pid: runtime?.pid || 0, action,
      amount: configuredAmount,
      running, exitOnly: Boolean(runtime?.exit_only),
    });
  }
  async function submit() {
    if (!confirm || !confirmationValid) return;
    const target = confirm;
    setConfirm(null);
    setBusy(true);
    setMessage("");
    const requestId = crypto.randomUUID();
    setSubmitted(requestId);
    setExpired(false);
    try {
      await terminalApi.post("strategy/command", {
        chain: target.chain,
        action: target.action,
        wallet_address: target.wallet,
        expected_pid: target.pid,
        request_id: requestId,
        ...(["start", "apply"].includes(target.action) ? { amount_usd: target.amount } : {}),
      });
      onChanged();
    } catch (e) {
      setSubmitted(null);
      const key = e instanceof Error ? e.message : "operation_failed";
      setMessage(errors[key] || key);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="strategy-item" ref={sectionRef} tabIndex={-1}>
      <div className="row-between">
        <div>
          <h2>{chainName(chain)}</h2>
          <span className="strategy-kicker">计划策略</span>
          <p className="strategy-summary">
            {strategy?.signal_stage
              ? `${signalStage(strategy.signal_stage)} · ${strategy.order_notional_usd ?? (chain === "bsc" ? 1 : 2)} U`
              : "策略参数待确认"}
          </p>
        </div>
        <Badge
          className="status-badge"
          variant={
            !verified || (running && runtime?.exit_only)
              ? "warn"
              : running
                ? "live"
                : "outline"
          }
        >
          <span className="status-dot" aria-hidden="true" />
          {!verified ? "待核实" : running ? "运行中" : "未运行"}
        </Badge>
      </div>
      <dl>
        <div>
          <dt>配置金额</dt>
          <dd>{configuredAmount != null ? `${configuredAmount} U` : "待确认"}</dd>
        </div>
        <div>
          <dt>执行器实际金额</dt>
          <dd>{runtime?.effective_amount_usd != null ? `${runtime.effective_amount_usd} U` : "待确认"}</dd>
        </div>
        <div>
          <dt>执行器确认</dt>
          <dd>{runtime?.executor_acknowledged_at ? time(runtime.executor_acknowledged_at) : "待确认"}</dd>
        </div>
        <div>
          <dt>执行器生效策略</dt>
          <dd>
            {effectiveStrategy?.strategy_version
              ? `${effectiveStrategy.strategy_version} · ${signalStage(effectiveStrategy.signal_stage || "")}`
              : "待确认"}
          </dd>
        </div>
        <div>
          <dt>当前模式</dt>
          <dd>
            {!verified
              ? "待核实"
              : running
                ? runtime?.exit_only
                  ? "只管理持仓"
                  : "自动开仓"
                : "未运行"}
          </dd>
        </div>
        <div>
          <dt>核对时间</dt>
          <dd>{time(runtime?.verified_at)}</dd>
        </div>
      </dl>
      <div className="strategy-actions">
        {!running ? (
          <Button disabled={!canCommand} onClick={(e) => beginConfirmation("start", e.currentTarget)}>
            <Play size={15} />
            启动
          </Button>
        ) : (
          <Button
            variant="outline"
            disabled={!canCommand || !control?.supported}
            onClick={(e) => beginConfirmation(runtime?.exit_only ? "resume" : "pause", e.currentTarget)}
          >
            {runtime?.exit_only ? <Play size={15} /> : <Pause size={15} />}{" "}
            {runtime?.exit_only ? "恢复开仓" : "暂停开仓"}
          </Button>
        )}
        <Button
          variant="destructive"
          disabled={!canCommand || !running || !control?.supported}
          onClick={(e) => beginConfirmation("stop", e.currentTarget)}
        >
          <Square size={14} />
          停止执行器
        </Button>
      </div>
      <p
        className="form-message control-result"
        data-state={
          awaitingOwn
            ? "pending"
            : expired && !finished
              ? "timed_out"
              : control?.state
        }
        role="status"
      >
        {status}
        {!awaitingOwn && control?.reason
          ? ` · ${errors[control.reason] || control.reason}`
          : ""}
      </p>
      <div className="rule-list">
        <h3>实际入场门槛</h3>
        <dl className="strategy-thresholds">
          <div><dt>最低评分</dt><dd>{shown(strategy?.min_rank_score)}</dd></div>
          <div><dt>独立来源</dt><dd>≥ {shown(strategy?.min_independent_sources)}</dd></div>
          <div>
            <dt>首次市值</dt>
            <dd>{compactUsd(strategy?.first_mcap_min_usd)}–{compactUsd(strategy?.first_mcap_max_usd)}</dd>
          </div>
          <div><dt>池龄上限</dt><dd>{strategy?.max_pair_age_hours == null ? "待确认" : `${strategy.max_pair_age_hours} 小时`}</dd></div>
          <div><dt>入场窗口</dt><dd>≤ {minutes(strategy?.max_entry_delay_seconds)}</dd></div>
          <div><dt>最高追价</dt><dd>{strategy?.max_markup_from_first == null ? "待确认" : `≤ ${strategy.max_markup_from_first}x`}</dd></div>
          <div><dt>最低流动性</dt><dd>≥ {compactUsd(strategy?.min_liquidity_usd)}</dd></div>
          <div><dt>最大往返损耗</dt><dd>≤ {percent(strategy?.max_round_trip_loss_pct)}</dd></div>
          <div>
            <dt>可卖验证</dt>
            <dd>
              {strategy?.min_sellable_cycles == null
                ? "待确认"
                : `${strategy.min_sellable_cycles} 轮${strategy.requires_observed_sell ? " + 真实卖单" : ""}`}
            </dd>
          </div>
          <div>
            <dt>仓位上限</dt>
            <dd>{strategy?.max_open_positions == null ? "待确认" : `${strategy.max_open_positions} 仓 / ${compactUsd(strategy.max_open_notional_usd)}`}</dd>
          </div>
          <div>
            <dt>当日已实现止损</dt>
            <dd>{strategy?.daily_realized_loss_stop_usd == null ? "待确认" : `-${compactUsd(strategy.daily_realized_loss_stop_usd)}`}</dd>
          </div>
        </dl>
        <h3>退出规则</h3>
        <p>
          {chain === "bsc"
            ? "止损 22% · 2x 卖出 50% · 剩余 50% 回撤 40% · 3x/5x 仅影子统计"
            : "止损 22% · 2x 卖出 50% · 3x 卖出 10% · 5x 卖出 10% · 剩余 30% 回撤 40%"}
        </p>
        <p>首盈前最长 90 分钟 · Runner 最长 24 小时</p>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          beginConfirmation("apply", (e.nativeEvent as SubmitEvent).submitter ||
            (document.activeElement instanceof HTMLElement ? document.activeElement : null));
        }}
      >
        <label>
          单笔金额（U）
          <input
            type="number"
            step="1"
            min={configuredAmount}
            max={configuredAmount}
            required
            readOnly
            value={configuredAmount}
          />
        </label>
        <Button type="submit" disabled={!canApply}>
          <Check size={15} />
          应用金额
        </Button>
      </form>
      <p className="form-message" role="status">
        {message}
      </p>
      <Dialog
        open={!!confirm}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
      >
        <DialogContent returnFocusRef={returnFocusRef} fallbackFocusRef={sectionRef}>
          <DialogHeader>
            <DialogTitle>
              {confirm && labels[confirm.action]} · {chainName(confirm?.chain || chain)}
            </DialogTitle>
            <DialogDescription>
              {confirm?.action === "pause"
                ? "停止新增买入，已有持仓继续按策略卖出。"
                : confirm?.action === "stop"
                  ? "仅在零持仓、无待确认交易时停止。"
                  : confirm?.action === "apply"
                    ? `下一笔买入金额调整为 ${confirm.amount} U，已有持仓和退出规则不变。`
                    : `按现有策略自动买卖，单笔 ${confirm?.amount || "当前配置"} U。`}
            </DialogDescription>
          </DialogHeader>
          <div className="control-confirm">
            <Button onClick={() => void submit()} disabled={!confirmationValid}>
              确认{confirm && labels[confirm.action]}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function signalStage(stage: string) {
  return stage === "aggregate_discovery"
    ? "聚合发现"
    : stage === "aggregate_early_bird"
      ? "聚合早鸟"
      : stage;
}
