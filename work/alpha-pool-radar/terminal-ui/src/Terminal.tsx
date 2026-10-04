import { lazy, Suspense, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  ChevronDown,
  ExternalLink,
  History,
  Radar,
  RefreshCw,
  Settings2,
  TerminalSquare,
  Wallet,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CopyAddressButton } from "@/components/copy-address-button";
import Strategy from "@/components/terminal-strategy";
import Accounting from "@/components/terminal-accounting";
import {
  atomic,
  chainName,
  executionReasons,
  gmgn,
  native,
  nativeSymbol,
  pct,
  reason,
  short,
  terminalApi,
  time,
  usd,
  type Chain,
  type Candidate,
  type Position,
  type Runtime,
  type Order,
  type Snapshot,
} from "@/lib/terminal-api";

const Performance = lazy(() => import("./components/terminal-performance"));
const WalletPanel = lazy(() => import("./components/terminal-wallet"));
const empty: Snapshot = {
  updated_at: "",
  wallet: null,
  chains: [],
  positions: [],
  orders: [],
  candidates: [],
  events: [],
  balances: [],
};
const tone = (n?: number | null) =>
  n == null ? "" : n > 0 ? "positive" : n < 0 ? "negative" : "";
const positionKey = (p: Position) =>
  JSON.stringify([p.chain, p.token.toLowerCase(), p.entry_at || ""]);
const orderKey = (o: Order) =>
  JSON.stringify([o.chain, o.tx_hash?.toLowerCase() || "", o.token.toLowerCase(),
    o.side, o.tx_hash ? "" : o.time || ""]);
const candidateKey = (c: Candidate) =>
  c.token ? JSON.stringify([c.chain.toLowerCase(), c.token.toLowerCase()]) : null;
function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <Activity size={20} />
      <span>{text}</span>
    </div>
  );
}
function Token({ symbol, chain }: { symbol: string; chain: string }) {
  return (
    <div className="token">
      <span className={`token-icon ${chain}`}>
        {(symbol || "?").slice(0, 2).toUpperCase()}
      </span>
      <span>
        <b>{symbol}</b>
        <small>{chainName(chain)}</small>
      </span>
    </div>
  );
}
function Size({ r }: { r?: Runtime }) {
  return (
    <>
      {r?.effective_amount_usd != null ? `${r.effective_amount_usd} U` : "待确认"}
    </>
  );
}

export default function Terminal() {
  const publicReadOnly = terminalApi.publicReadOnly;
  const [tab, setTab] = useState(() => {
    const v = new URLSearchParams(location.search).get("tab");
    const allowed = publicReadOnly
      ? ["overview", "discovery"]
      : ["overview", "discovery", "config", "wallets"];
    return allowed.includes(v || "")
      ? v!
      : "overview";
  });
  const [chain, setChain] = useState("all");
  const [filter, setFilter] = useState("");
  const [logs, setLogs] = useState(false);
  const [chart, setChart] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedOrderKey, setSelectedOrderKey] = useState<string | null>(null);
  const [selectedCandidateKey, setSelectedCandidateKey] = useState<string | null>(null);
  const positionFocusRef = useRef<HTMLElement | null>(null);
  const orderFocusRef = useRef<HTMLElement | null>(null);
  const candidateFocusRef = useRef<HTMLElement | null>(null);
  const workspaceRef = useRef<HTMLElement | null>(null);
  const [orderLimit, setOrderLimit] = useState(25);
  const query = useQuery({
    queryKey: ["terminal"],
    queryFn: terminalApi.snapshot,
    refetchInterval: 3000,
    retry: 1,
  });
  const runtime = useQuery({
    queryKey: ["terminal-runtime"],
    queryFn: terminalApi.runtime,
    refetchInterval: 5000,
    retry: 1,
  });
  const data = query.data || empty;
  const selected = data.positions.find((p) => positionKey(p) === selectedKey) || null;
  const selectedOrder = data.orders.find((o) => orderKey(o) === selectedOrderKey) || null;
  const selectedCandidate = data.candidates.find((c) => candidateKey(c) === selectedCandidateKey) || null;
  const match = (r: { chain?: string; symbol?: string }) =>
    (chain === "all" || r.chain === chain) &&
    (!filter || r.symbol?.toLowerCase().includes(filter.toLowerCase()));
  const positions = data.positions.filter(match);
  const orders = data.orders
    .filter((o) => o.status === "filled")
    .filter(match)
    .slice()
    .sort((a, b) => (b.time || "").localeCompare(a.time || ""));
  const candidates = Array.from(new Map(
    data.candidates.filter(match).map((c, index) => [
      candidateKey(c) || JSON.stringify([c.chain, c.symbol, c.signal_at || "", index]), c,
    ]),
  ).values());
  const activeCandidates = candidates.filter((candidate) => candidate.candidate_kind === "active");
  const reviewCandidates = candidates.filter((candidate) => candidate.candidate_kind !== "active");
  const allRuntime = runtime.data?.runtime || [];
  const visibleChains = data.chains.filter(
    (c) => chain === "all" || c.chain === chain,
  );
  const unverifiedLedgers = (["bsc", "robinhood"] as Chain[])
    .filter((c) => chain === "all" || c === chain)
    .filter((c) => data.chains.find((s) => s.chain === c)?.ledger_identity_status !== "verified");
  const ledgerPending = unverifiedLedgers.length > 0;
  const balances = data.balances.filter(
    (c) => chain === "all" || c.chain === chain,
  );
  const closeCount = orders.filter((o) => o.side === "sell").length;
  function switchTab(value: string) {
    setTab(value);
    const url = new URL(location.href);
    url.searchParams.set("tab", value);
    history.replaceState(null, "", url);
  }
  const connected = !query.isError && Boolean(query.data);
  return (
    <div className="terminal-app">
      <a className="skip-link" href="#workspace">
        跳到内容
      </a>
      <header className="terminal-header">
        <div className="brand">
          <Radar size={26} />
          <div>
            <strong>MEME 雷达</strong>
            <span>交易终端</span>
          </div>
          <Badge variant="outline">实盘</Badge>
        </div>
        <div className="header-right">
          <span className={`connection ${connected ? "online" : "offline"}`}>
            <i />
            {connected ? "已连接" : query.isLoading ? "连接中" : "连接中断"}
          </span>
          <span className="updated">{time(data.updated_at)}</span>
          <Button
            variant="ghost"
            size="sm"
            title="刷新数据"
            aria-label="刷新数据"
            disabled={query.isFetching}
            onClick={() => {
              void query.refetch();
              void runtime.refetch();
            }}
          >
            <RefreshCw size={16} className={query.isFetching ? "spin" : ""} />
          </Button>
        </div>
      </header>
      <Tabs value={tab} onValueChange={switchTab}>
        <div className="nav-band">
          <TabsList aria-label="交易终端页面">
            <TabsTrigger value="overview">
              <Activity size={15} />
              交易
            </TabsTrigger>
            <TabsTrigger value="discovery">
              <Radar size={15} />
              发现
            </TabsTrigger>
            {!publicReadOnly && (
              <TabsTrigger value="config">
                <Settings2 size={15} />
                策略
              </TabsTrigger>
            )}
            {!publicReadOnly && (
              <TabsTrigger value="wallets">
                <Wallet size={15} />
                钱包
              </TabsTrigger>
            )}
          </TabsList>
          <label className="chain-select">
            <span>网络</span>
            <select
              aria-label="筛选网络"
              value={chain}
              onChange={(e) => setChain(e.target.value)}
            >
              <option value="all">全部网络</option>
              <option value="bsc">BSC</option>
              <option value="robinhood">Robinhood</option>
            </select>
            <ChevronDown size={14} />
          </label>
        </div>
        <main id="workspace" ref={workspaceRef} tabIndex={-1}>
          {publicReadOnly && (
            <div className="readonly-band">
              公网只读实时视图 · 交易控制和钱包管理仅在本机开放
            </div>
          )}
          {query.isError && (
            <div className="error-band" role="alert">
              连接中断，以下为最后收到的数据。{query.error.message}
            </div>
          )}
          {data.system_health && !data.system_health.ok && (
            <div className="error-band" role="alert">
              实盘链路异常：
              {data.system_health.issues
                .filter((issue) => issue.affects_live_entries || issue.affects_live_exits)
                .map((issue) => issue.message)
                .join(" ")}
            </div>
          )}
          {Boolean(runtime.data?.unassigned_workers) && (
            <div className="error-band" role="alert">
              存在链归属未核实的运行进程，执行状态待核对。
            </div>
          )}
          {query.data && ledgerPending && (
            <div className="error-band" role="alert">
              账本待核验：持仓、成交及盈亏可能不完整，不能据此认定空仓。
              {unverifiedLedgers.map((c) => (
                <div key={c}>
                  {chainName(c)}：
                  {data.chains.find((s) => s.chain === c)?.ledger_identity_status === "mismatch"
                    ? "账本钱包或网络与当前配置不一致，请核对归属。"
                    : "账本身份尚未核验或旧版未提供状态；旧账本需核验钱包与网络归属后迁移。"}
                </div>
              ))}
            </div>
          )}
          <TabsContent value="overview">
            <div className="metric-band">
              <div>
                <span>原生币可用余额</span>
                {(["bsc", "robinhood"] as Chain[])
                  .filter((c) => chain === "all" || c === chain)
                  .map((c) => {
                    const balance = balances.find((b) => b.chain === c);
                    return (
                      <div className="native-pnl native-balance" key={c}>
                        <b>{native(balance?.native_balance)}</b>
                        <small>
                          {nativeSymbol(c)}
                          {balance?.status === "stale"
                            ? " · 已过期"
                            : balance?.native_balance == null
                              ? " · 未获取"
                              : ""}
                        </small>
                      </div>
                    );
                  })}
              </div>
              <div>
                <span>当前持仓</span>
                <strong>
                  {ledgerPending ? "待核验" : positions.length}
                  {!ledgerPending && <em>个</em>}
                </strong>
                <small>
                  {chain === "all" ? "两条链合计" : chainName(chain)}
                </small>
              </div>
              <div>
                <span>{data.session ? "V2 已实现盈亏 · 原生币" : "已实现盈亏 · 原生币"}</span>
                {visibleChains.map((c) => {
                  const pnlVerified =
                    c.ledger_identity_status === "verified" &&
                    c.realized_pnl_status === "verified";
                  const pending = c.realized_pnl_pending_count;
                  return (
                    <div
                      className={`native-pnl ${pnlVerified ? tone(c.realized_pnl_native) : ""}`}
                      key={c.chain}
                    >
                      <b>
                        {pnlVerified
                          ? native(c.realized_pnl_native)
                          : `待核验${pending != null ? ` ${pending}项` : ""}`}
                      </b>
                      <small>
                        {nativeSymbol(c.chain)}
                        {pnlVerified && c.realized_pnl_method === "receipt_fifo_native_equivalent_v1"
                          ? ` · 链上回执补算${c.manual_reconciled_sales ? ` · 含手动卖出 ${c.manual_reconciled_sales} 笔` : ""}`
                          : ""}
                        {!pnlVerified && (c.unverified_sell_fills || c.unreconciled_external_sales)
                          ? ` · 结算 ${c.unverified_sell_fills || 0} · 手动 ${c.unreconciled_external_sales || 0}`
                          : ""}
                      </small>
                    </div>
                  );
                })}
                <small>仅显示已完整核验或逐笔链上回执补算的卖出结算</small>
              </div>
              <div>
                <span>{data.session ? "V2 成交记录" : "成交记录"}</span>
                <strong>
                  {ledgerPending ? "待核验" : orders.length}
                  {!ledgerPending && <em>笔</em>}
                </strong>
                <small>
                  {ledgerPending ? "账本核验前不确认成交总数" : `买入 ${orders.length - closeCount} · 卖出 ${closeCount}`}
                </small>
              </div>
            </div>
            <div className="trading-layout">
              <section className="primary">
                <div className="section-heading">
                  <h2>
                    当前持仓 <span>{ledgerPending ? "待核验" : positions.length}</span>
                  </h2>
                  <input
                    aria-label="搜索币种"
                    placeholder="搜索币种"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  />
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>币种</th>
                        <th>买入参考价</th>
                        <th>最新价</th>
                        <th>价格变化</th>
                        <th>投入</th>
                        <th>退出状态</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {positions.map((p) => (
                        <tr
                          key={positionKey(p)}
                          onClick={(e) => {
                            positionFocusRef.current = e.currentTarget.querySelector("button");
                            setSelectedKey(positionKey(p));
                          }}
                          className="clickable"
                        >
                          <td>
                            <Token symbol={p.symbol} chain={p.chain} />
                          </td>
                          <td>{price(p.entry_price_usd)}</td>
                          <td>
                            {price(p.current_price_usd)}
                            <small>{time(p.last_quote_at)}</small>
                          </td>
                          <td className={tone(p.return_pct)}>
                            {pct(p.return_pct)}
                          </td>
                          <td>
                            {p.entry_notional_usd == null
                              ? `${native(atomic(p.entry_native_atomic))} ${nativeSymbol(p.chain)}`
                              : usd(p.entry_notional_usd)}
                          </td>
                          <td>{reason(p.exit_status)}</td>
                          <td>
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={`查看 ${p.symbol}`}
                              title="持仓详情"
                            >
                              <ExternalLink size={15} />
                            </Button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!positions.length && (
                    <Empty
                      text={query.isLoading ? "正在读取持仓" : ledgerPending ? "账本待核验，暂不能确认持仓数量" : "暂无持仓"}
                    />
                  )}
                </div>
                <div className="section-heading">
                  <h2>
                    <History size={17} />
                    最近成交 <span>{ledgerPending ? "待核验" : orders.length}</span>
                  </h2>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setChart(!chart)}
                  >
                    {chart ? "收起" : "查看"}收益记录
                  </Button>
                </div>
                {data.accounting && (
                  <p className="accounting-progress" role="status">
                    链上回查：回执 {data.accounting.receipt_covered}/
                    {data.accounting.total} · Gas {data.accounting.gas_covered}/
                    {data.accounting.total} · 可核验盈亏{" "}
                    {data.accounting.pnl_covered} 笔
                    {data.accounting.persistence_status === "error"
                      ? " · 记录保存失败"
                      : ""}
                  </p>
                )}
                {chart && (
                  <Suspense fallback={<Empty text="加载图表" />}>
                    <Performance orders={orders} />
                  </Suspense>
                )}
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>时间 / 币种</th>
                        <th>方向</th>
                        <th>实际支付 / 净到账</th>
                        <th>Gas</th>
                        <th>账本盈亏</th>
                        <th>原因</th>
                        <th>链上记账</th>
                        <th>交易</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.slice(0, orderLimit).map((o) => (
                        <tr key={orderKey(o)}>
                          <td>
                            <b>{o.symbol}</b>
                            <small>
                              {time(o.time)} · {chainName(o.chain)}
                            </small>
                          </td>
                          <td>
                            <span
                              className={`side ${o.side === "buy" ? "positive" : "negative"}`}
                            >
                              {o.side === "buy" ? (
                                <ArrowDownLeft size={14} />
                              ) : (
                                <ArrowUpRight size={14} />
                              )}{" "}
                              {o.side === "buy" ? "买入" : "卖出"}
                            </span>
                          </td>
                          <td>
                            {native(
                              atomic(
                                o.side === "buy"
                                  ? o.native_spent_atomic
                                  : o.net_native_received_atomic,
                              ),
                            )}
                            <small>{nativeSymbol(o.chain)}</small>
                          </td>
                          <td>{native(atomic(o.gas_native_atomic))}</td>
                          <td className={tone(o.pnl_native)}>
                            {native(o.pnl_native)}
                            <small>
                              {o.pnl_native == null
                                ? "未完整记账"
                                : nativeSymbol(o.chain)}
                            </small>
                          </td>
                          <td>
                            {o.side === "buy"
                              ? "策略开仓"
                              : reason(o.exit_reason)}
                          </td>
                          <td>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={(e) => {
                                orderFocusRef.current = e.currentTarget;
                                setSelectedOrderKey(orderKey(o));
                              }}
                            >
                              {o.accounting?.receipt_status === "verified"
                                ? "回执已核验"
                                : "查看核验"}
                            </Button>
                          </td>
                          <td>
                            {o.explorer_url && (
                              <a
                                aria-label={`查看 ${o.symbol} 交易`}
                                title="区块浏览器"
                                href={o.explorer_url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                <ExternalLink size={15} />
                              </a>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!orders.length && <Empty text={ledgerPending ? "账本待核验，暂不能确认成交记录" : "暂无已确认成交"} />}
                </div>
                {orders.length > orderLimit && (
                  <Button
                    variant="outline"
                    className="load-more"
                    onClick={() => setOrderLimit(orderLimit + 25)}
                  >
                    加载更多
                  </Button>
                )}
              </section>
              <aside className="runtime-panel">
                <h2>执行状态</h2>
                {(["bsc", "robinhood"] as Chain[])
                  .filter((c) => chain === "all" || c === chain)
                  .map((c) => {
                    const status = data.chains.find((s) => s.chain === c);
                    const r = allRuntime.find((s) => s.chain === c);
                    return (
                      <div className="chain-runtime" key={c}>
                        <div className="row-between">
                          <b>{chainName(c)}</b>
                          <Badge
                            className="status-badge"
                            variant={
                              !runtime.data?.verified ||
                              runtime.isError ||
                              r?.exit_only
                                ? "warn"
                                : r?.running
                                  ? "live"
                                  : "outline"
                            }
                          >
                            <span className="status-dot" aria-hidden="true" />
                            {!runtime.data?.verified || runtime.isError
                              ? "待核实"
                              : r?.running
                                ? "运行中"
                                : "未运行"}
                          </Badge>
                        </div>
                        <dl>
                          <div>
                            <dt>单笔实际金额</dt>
                            <dd>
                              <Size r={r} />
                            </dd>
                          </div>
                          <div>
                            <dt>开仓状态</dt>
                            <dd
                              className={
                                !runtime.data?.verified || runtime.isError
                                  ? "warning"
                                  : r?.running
                                    ? r.exit_only
                                      ? "warning"
                                      : "positive"
                                    : undefined
                              }
                            >
                              {!runtime.data?.verified || runtime.isError
                                ? "待核实"
                                : r?.running
                                  ? r.exit_only
                                    ? "已暂停"
                                    : "已开启"
                                  : "—"}
                            </dd>
                          </div>
                          <div>
                            <dt>持仓</dt>
                            <dd>{status?.ledger_identity_status === "verified" ? status.open_positions ?? "—" : "待核验"}</dd>
                          </div>
                          <div>
                            <dt>行情更新</dt>
                            <dd>{time(status?.updated_at)}</dd>
                          </div>
                        </dl>
                        <p>
                          {executionReasons[status?.activity_reason || ""] ||
                            status?.activity_message ||
                            reason(status?.activity_reason) ||
                            "等待执行状态"}
                        </p>
                      </div>
                    );
                  })}
                <Button variant="outline" onClick={() => switchTab("config")}>
                  <Settings2 size={15} />
                  查看策略配置
                </Button>
              </aside>
            </div>
          </TabsContent>
          <TabsContent value="discovery">
            <div className="section-heading">
              <div>
                <h2>
                  当前可执行候选 <span>{activeCandidates.length}</span>
                </h2>
                <p>这里只统计已进入执行输入的实时信号</p>
              </div>
              <input
                aria-label="搜索候选币"
                placeholder="搜索币种"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </div>
            <section aria-label="当前可执行候选">
              <CandidateTable
                rows={activeCandidates}
                onOpen={(candidate, opener) => {
                  candidateFocusRef.current = opener;
                  setSelectedCandidateKey(candidateKey(candidate));
                }}
              />
              {!activeCandidates.length && <Empty text="当前没有通过全部入场条件的实时候选" />}
            </section>
            <details className="filtered-candidates">
              <summary>最近淘汰与影子记录 <span>{reviewCandidates.length}</span></summary>
              <CandidateTable
                rows={reviewCandidates}
                onOpen={(candidate, opener) => {
                  candidateFocusRef.current = opener;
                  setSelectedCandidateKey(candidateKey(candidate));
                }}
              />
              {!reviewCandidates.length && <Empty text="暂无最近淘汰或影子记录" />}
            </details>
          </TabsContent>
          {!publicReadOnly && <TabsContent value="config">
            <div className="section-heading">
              <div>
                <h2>策略配置</h2>
                <p>按链查看执行器实际参数</p>
              </div>
            </div>
            <div className="strategy-grid">
              {(["bsc", "robinhood"] as Chain[]).map((c) => (
                <Strategy
                  chain={c}
                  wallet={data.wallet}
                  control={runtime.data?.controls?.find((r) => r.chain === c)}
                  onChanged={() => runtime.refetch()}
                  draft={
                    runtime.data?.drafts?.find((r) => r.chain === c)?.amount_usd
                  }
                  verified={runtime.data?.verified === true && !runtime.isError}
                  runtime={allRuntime.find((r) => r.chain === c)}
                  strategy={
                    data.chains.find((s) => s.chain === c)?.configured_strategy
                  }
                  effectiveStrategy={data.chains.find((s) => s.chain === c)?.effective_strategy}
                  key={c}
                />
              ))}
            </div>
          </TabsContent>}
          {!publicReadOnly && <TabsContent value="wallets">
            <Suspense fallback={<Empty text="加载钱包" />}>
              <WalletPanel
                wallet={data.wallet}
                balances={data.balances}
                onSaved={() => query.refetch()}
              />
            </Suspense>
          </TabsContent>}
        </main>
      </Tabs>
      <footer>
        <span>
          本机交易终端 <span className="footer-divider">/</span>{" "}
          {short(data.wallet)}
        </span>
        <Button variant="ghost" size="sm" onClick={() => setLogs(!logs)}>
          <TerminalSquare size={15} />
          事件记录 <Badge variant="outline">{data.events.length}</Badge>
        </Button>
      </footer>
      {logs && (
        <section className="log-drawer">
          <div className="section-heading">
            <h2>事件记录</h2>
            <Button
              variant="ghost"
              size="sm"
              aria-label="关闭日志"
              onClick={() => setLogs(false)}
            >
              <X size={16} />
            </Button>
          </div>
          <div className="log-lines">
            {data.events.filter(match).map((e, i) => (
              <div key={i}>
                <time>{time(e.time)}</time>
                <b>{e.chain ? chainName(e.chain) : "系统"}</b>
                <span>
                  {e.symbol} {e.message || reason(e.type)}
                </span>
              </div>
            ))}
            {!data.events.length && <Empty text="暂无事件" />}
          </div>
        </section>
      )}
      <Accounting
        open={selectedOrderKey != null}
        order={selectedOrder}
        onClose={() => setSelectedOrderKey(null)}
        returnFocusRef={orderFocusRef}
        fallbackFocusRef={workspaceRef}
      />
      <Dialog
        open={selectedKey != null}
        onOpenChange={(v) => {
          if (!v) setSelectedKey(null);
        }}
      >
        <DialogContent returnFocusRef={positionFocusRef} fallbackFocusRef={workspaceRef}>
          <DialogHeader>
            <DialogTitle>{selected ? `${selected.symbol} · ` : ""}持仓详情</DialogTitle>
            <DialogDescription>
              {selected ? chainName(selected.chain) : "最新持仓状态"}
            </DialogDescription>
          </DialogHeader>
          {!selected && (
            <div className="detail-body" role="status">
              持仓已不在最新快照中，请核对最近成交与执行状态。
            </div>
          )}
          {selected && (
            <div className="detail-body">
              <div className="row-between">
                <code>{short(selected.token)}</code>
                <CopyAddressButton value={selected.token} label="合约地址" />
              </div>
              <dl>
                {selected.strategy && (
                  <>
                    <div>
                      <dt>入场策略</dt>
                      <dd>{signalStage(selected.strategy.signal_stage)}</dd>
                    </div>
                    <div>
                      <dt>发现排名</dt>
                      <dd>{selected.strategy.rank_score ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>策略版本</dt>
                      <dd>{selected.strategy.strategy_version || "—"}</dd>
                    </div>
                  </>
                )}
                <div>
                  <dt>入场时间</dt>
                  <dd>{time(selected.entry_at)}</dd>
                </div>
                <div>
                  <dt>投入</dt>
                  <dd>
                    {native(atomic(selected.entry_native_atomic))}{" "}
                    {nativeSymbol(selected.chain)}
                  </dd>
                </div>
                <div>
                  <dt>原始最小单位数量</dt>
                  <dd>{selected.remaining_atomic || "—"}</dd>
                </div>
                <div>
                  <dt>价格变化</dt>
                  <dd className={tone(selected.return_pct)}>
                    {pct(selected.return_pct)}
                  </dd>
                </div>
                <div>
                  <dt>首次止盈</dt>
                  <dd>{selected.tp1_hit ? "已触发" : "未触发"}</dd>
                </div>
                <div>
                  <dt>第二次止盈</dt>
                  <dd>{selected.tp2_hit ? "已触发" : "未触发"}</dd>
                </div>
                <div>
                  <dt>第三次止盈</dt>
                  <dd>{selected.tp3_hit ? "已触发" : "未触发"}</dd>
                </div>
                <div>
                  <dt>当前退出状态</dt>
                  <dd>{reason(selected.exit_status)}</dd>
                </div>
              </dl>
              <a
                href={gmgn(selected.chain, selected.token)}
                target="_blank"
                rel="noreferrer"
                className="gmgn-link"
              >
                打开 GMGN <ExternalLink size={15} />
              </a>
            </div>
          )}
        </DialogContent>
      </Dialog>
      <Dialog
        open={selectedCandidateKey != null}
        onOpenChange={(v) => {
          if (!v) setSelectedCandidateKey(null);
        }}
      >
        <DialogContent returnFocusRef={candidateFocusRef} fallbackFocusRef={workspaceRef}>
          <DialogHeader>
            <DialogTitle>
              {selectedCandidate ? `${selectedCandidate.symbol} · ` : ""}候选详情
            </DialogTitle>
            <DialogDescription>
              {selectedCandidate ? chainName(selectedCandidate.chain) : "最新候选状态"}
            </DialogDescription>
          </DialogHeader>
          {!selectedCandidate && (
            <div className="detail-body" role="status">
              候选已不在最新快照中，请核对发现记录与执行状态。
            </div>
          )}
          {selectedCandidate?.token && (
            <div className="detail-body candidate-detail">
              <div className="address-line">
                <code>{selectedCandidate.token}</code>
                <CopyAddressButton value={selectedCandidate.token} label="候选合约地址" />
              </div>
              <section className="detail-section">
                <h3>策略路由</h3>
                <dl>
                  <div><dt>策略版本</dt><dd>{selectedCandidate.strategy?.strategy_version || "旧版"}</dd></div>
                  <div><dt>信号阶段</dt><dd>{signalStage(selectedCandidate.strategy?.signal_stage)}</dd></div>
                  <div><dt>候选状态</dt><dd>{executionModeLabel(selectedCandidate.strategy?.execution_mode)}</dd></div>
                  <div><dt>入场路线</dt><dd>{selectedCandidate.strategy?.entry_route || "—"}</dd></div>
                  <div><dt>发现排名</dt><dd>{selectedCandidate.strategy?.rank_score ?? "—"}</dd></div>
                  <div><dt>旧评分</dt><dd>{selectedCandidate.score ?? "—"}</dd></div>
                  <div><dt>首次市值</dt><dd>{usd(selectedCandidate.strategy?.first_mcap_usd ?? selectedCandidate.first_mcap_usd)}</dd></div>
                  <div><dt>当前市值</dt><dd>{usd(selectedCandidate.strategy?.current_mcap_usd ?? selectedCandidate.mcap)}</dd></div>
                  <div><dt>入场延迟</dt><dd>{duration(selectedCandidate.strategy?.entry_delay_seconds)}</dd></div>
                  <div><dt>相对首次价格</dt><dd>{multiple(selectedCandidate.strategy?.markup_from_first)}</dd></div>
                </dl>
              </section>
              <section className="detail-section">
                <h3>可交易性</h3>
                <dl>
                  <div>
                    <dt>状态</dt>
                    <dd className={tradeabilityTone(selectedCandidate.tradeability?.status)}>
                      {tradeabilityLabel(selectedCandidate.tradeability?.status)}
                    </dd>
                  </div>
                  <div><dt>拒绝原因</dt><dd>{reason(selectedCandidate.tradeability?.reject_reason || selectedCandidate.strategy?.reject_reason)}</dd></div>
                  <div><dt>可用流动性</dt><dd>{usd(selectedCandidate.tradeability?.liquidity_usd ?? selectedCandidate.liquidity)}</dd></div>
                  <div><dt>买入冲击</dt><dd>{plainPct(selectedCandidate.tradeability?.buy_impact_pct)}</dd></div>
                  <div><dt>卖出冲击</dt><dd>{plainPct(selectedCandidate.tradeability?.sell_impact_pct)}</dd></div>
                  <div><dt>预计往返损耗</dt><dd>{plainPct(selectedCandidate.tradeability?.round_trip_loss_pct)}</dd></div>
                  <div><dt>报价时间</dt><dd>{time(selectedCandidate.tradeability?.quote_at || undefined)}</dd></div>
                </dl>
              </section>
              <section className="detail-section">
                <h3>结果模型</h3>
                <dl>
                  <div><dt>P(2x)</dt><dd>{probability(selectedCandidate.model?.p_2x)}</dd></div>
                  <div><dt>P(5x)</dt><dd>{probability(selectedCandidate.model?.p_5x)}</dd></div>
                  <div><dt>预期净收益</dt><dd>{plainPct(selectedCandidate.model?.expected_net_return_pct, true)}</dd></div>
                  <div><dt>样本数</dt><dd>{selectedCandidate.model?.sample_count ?? "样本收集中"}</dd></div>
                </dl>
              </section>
              {Object.keys(selectedCandidate.strategy?.rank_components || {}).length > 0 && (
                <section className="detail-section">
                  <h3>排名构成</h3>
                  <div className="evidence-list">
                    {Object.entries(selectedCandidate.strategy?.rank_components || {}).map(([key, value]) => (
                      <span key={key}><b>{key}</b>{value}</span>
                    ))}
                  </div>
                </section>
              )}
              {Object.keys(selectedCandidate.strategy?.policy_checks || {}).length > 0 && (
                <section className="detail-section">
                  <h3>策略检查</h3>
                  <div className="evidence-list">
                    {Object.entries(selectedCandidate.strategy?.policy_checks || {}).map(([key, value]) => (
                      <span key={key} className={value ? "positive" : "negative"}>
                        <b>{key}</b>{value ? "通过" : "未通过"}
                      </span>
                    ))}
                  </div>
                </section>
              )}
              <a
                href={gmgn(selectedCandidate.chain, selectedCandidate.token)}
                target="_blank"
                rel="noreferrer"
                className="gmgn-link"
              >
                打开 GMGN <ExternalLink size={15} />
              </a>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function price(n?: number | null) {
  return n == null
    ? "—"
    : `$${n.toLocaleString("en-US", { maximumSignificantDigits: 6 })}`;
}

function signalStage(stage?: string | null) {
  return stage === "aggregate_discovery"
    ? "聚合发现"
    : stage === "aggregate_early_bird"
      ? "聚合早鸟"
      : stage || "—";
}

function executionModeLabel(mode?: string | null) {
  return mode === "shadow"
    ? "影子观察"
    : mode === "live_candidate"
      ? "等待执行器评估"
      : mode || "—";
}

function CandidateTable({
  rows,
  onOpen,
}: {
  rows: Candidate[];
  onOpen: (candidate: Candidate, opener: HTMLElement) => void;
}) {
  if (!rows.length) return null;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>项目</th>
            <th>市值</th>
            <th>首次市值</th>
            <th>流动性</th>
            <th>发现排名</th>
            <th>可交易性</th>
            <th>入场阶段</th>
            <th>来源数</th>
            <th>入场判断</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((candidate, index) => (
            <tr key={candidateKey(candidate) || `${candidate.chain}:${candidate.symbol}:${index}`}>
              <td><Token symbol={candidate.symbol} chain={candidate.chain} /></td>
              <td>{usd(candidate.mcap)}</td>
              <td>{usd(candidate.first_mcap_usd)}</td>
              <td>{usd(candidate.liquidity)}</td>
              <td className="rank-cell">
                {candidate.strategy?.rank_score ?? "—"}
                {candidate.score != null && <small>旧评分 {candidate.score}</small>}
              </td>
              <td>
                <span className={`evidence-status ${tradeabilityTone(candidate.tradeability?.status)}`}>
                  {tradeabilityLabel(candidate.tradeability?.status)}
                </span>
                {candidate.tradeability?.reject_reason && (
                  <small>{reason(candidate.tradeability.reject_reason)}</small>
                )}
              </td>
              <td>
                {signalStage(candidate.strategy?.signal_stage)}
                <small className={candidate.strategy?.execution_mode === "shadow" ? "warning" : undefined}>
                  {executionModeLabel(candidate.strategy?.execution_mode)}
                </small>
              </td>
              <td>{candidate.source_count ?? "—"}</td>
              <td className="reason-cell">
                {reason(candidate.reason)}
                <small>{time(candidate.signal_at)}</small>
              </td>
              <td>
                <div className="candidate-actions">
                  {candidate.token && (
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`查看 ${candidate.symbol} 候选详情`}
                      title="候选详情"
                      onClick={(event) => onOpen(candidate, event.currentTarget)}
                    >
                      <Radar size={15} />
                    </Button>
                  )}
                  {candidate.token && (
                    <a
                      href={gmgn(candidate.chain, candidate.token)}
                      target="_blank"
                      rel="noreferrer"
                      title="GMGN"
                      aria-label={`打开 ${candidate.symbol} GMGN`}
                    >
                      <ExternalLink size={16} />
                    </a>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function tradeabilityLabel(status?: string | null) {
  return status === "passed"
    ? "通过"
    : status === "failed"
      ? "失败"
      : status === "unavailable"
        ? "不可用"
        : "待评估";
}

function tradeabilityTone(status?: string | null) {
  return status === "passed"
    ? "positive"
    : status === "failed"
      ? "negative"
      : status === "unavailable"
        ? "warning"
        : "";
}

function probability(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "样本收集中";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value * 100)}%`;
}

function plainPct(value?: number | null, signed = false) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${signed && value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function duration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  return seconds < 60 ? `${Math.round(seconds)} 秒` : `${(seconds / 60).toFixed(1)} 分钟`;
}

function multiple(value?: number | null) {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(2)}x`;
}
