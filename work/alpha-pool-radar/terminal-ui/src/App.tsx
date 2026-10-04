import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  History,
  ExternalLink,
  Play,
  Settings2,
  Square,
  Terminal,
  Wallet,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { WalletsTab } from "@/components/wallets-tab";
import { SessionsTab } from "@/components/sessions-tab";
import { SessionChart } from "@/components/session-chart";
import { CopyAddressButton } from "@/components/copy-address-button";
import { StartBotDialog } from "@/components/start-bot-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import {
  formatCompactUsd,
  formatCostPerVolumeUsd,
  formatDateTime,
  formatDuration,
  formatEth,
  formatHeadroom,
  formatPercent,
  formatRatePerHour,
  formatUsd,
  shortAddress,
} from "@/lib/utils";

const TAB_IDS = ["overview", "wallets", "sessions", "logs", "config"] as const;
type TabId = (typeof TAB_IDS)[number];

function isTabId(value: string | null): value is TabId {
  return TAB_IDS.includes(value as TabId);
}

function useTabState() {
  const [tab, setTab] = useState<TabId>(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("tab");
    return isTabId(fromUrl) ? fromUrl : "overview";
  });

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    params.set("tab", tab);
    const next = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState(null, "", next);
  }, [tab]);

  return [tab, setTab] as const;
}

function useLiveClock(active: boolean) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);

  return now;
}

function MetaItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0 space-y-0.5 sm:min-w-[7rem]">
      <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
        {label}
      </p>
      <p className="text-sm font-medium text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
        {value}
      </p>
    </div>
  );
}

function EthHero({
  amount,
  digits = 4,
  size = "lg",
}: {
  amount: number | string;
  digits?: number;
  size?: "lg" | "md";
}) {
  const text = typeof amount === "number" ? amount.toFixed(digits) : amount;
  return (
    <p
      className={
        size === "lg"
          ? "font-mono text-xl font-semibold tabular-nums tracking-tight text-[var(--color-ink)] sm:text-2xl dark:text-[var(--color-ink-dark)]"
          : "font-mono text-base font-semibold tabular-nums tracking-tight text-[var(--color-ink)] sm:text-lg dark:text-[var(--color-ink-dark)]"
      }
    >
      {text}
      <span className="ml-1.5 text-sm font-medium text-[var(--color-ink-muted)]">
        ETH
      </span>
    </p>
  );
}

function MetricLabel({ children }: { children: ReactNode }) {
  return (
    <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
      {children}
    </p>
  );
}

function Stat({
  label,
  value,
  mono = true,
  truncate = false,
}: {
  label: string;
  value: string | number;
  mono?: boolean;
  truncate?: boolean;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
        {label}
      </p>
      <p
        className={`text-base font-semibold text-[var(--color-ink)] sm:text-lg dark:text-[var(--color-ink-dark)] ${mono ? "font-mono tabular-nums" : ""} ${truncate ? "truncate" : ""}`}
      >
        {value}
      </p>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="border-b border-[var(--color-wire)] py-3 last:border-0 dark:border-[var(--color-wire-dark)]">
      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <dt className="shrink-0 text-xs text-[var(--color-ink-muted)] sm:text-sm dark:text-[var(--color-ink-muted-dark)]">
          {label}
        </dt>
        <dd className="min-w-0 break-all text-left text-sm text-[var(--color-ink)] sm:text-right dark:text-[var(--color-ink-dark)]">
          {value}
        </dd>
      </div>
    </div>
  );
}

function LoadingPanel() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading dashboard">
      <div className="h-16 animate-pulse rounded-xl border border-[var(--color-wire)] bg-[var(--color-surface)] dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]" />
      <div className="grid gap-4 md:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-36 animate-pulse rounded-xl border border-[var(--color-wire)] bg-[var(--color-surface)] dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]"
          />
        ))}
      </div>
    </div>
  );
}

function ErrorPanel({ message, hint }: { message: string; hint?: string }) {
  return (
    <Card>
      <CardContent className="space-y-2 py-8 text-center" role="alert">
        <p className="text-sm font-medium text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
          {message}
        </p>
        {hint ? (
          <p className="font-mono text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
            {hint}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function StatusAnnouncer({ message }: { message: string }) {
  return (
    <div className="sr-only" aria-live="polite" aria-atomic="true">
      {message}
    </div>
  );
}

function OverviewTab() {
  const queryClient = useQueryClient();
  const [confirmStop, setConfirmStop] = useState(false);
  const [startDialogOpen, setStartDialogOpen] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [marketExpanded, setMarketExpanded] = useState(false);

  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["status"],
    queryFn: api.getStatus,
    refetchInterval: 5000,
  });
  const { data: timeseries } = useQuery({
    queryKey: ["timeseries"],
    queryFn: async () => (await api.getTimeseries()).points,
    refetchInterval: 10_000,
  });
  const tokenAddress =
    data?.session?.targetTokenAddress ?? data?.token?.address ?? null;
  const { data: preflight } = useQuery({
    queryKey: ["preflight", tokenAddress],
    queryFn: () => api.getPreflight({ token: tokenAddress! }),
    enabled: Boolean(tokenAddress),
    refetchInterval: 30_000,
  });
  const now = useLiveClock(data?.bot.running ?? false);

  const stop = useMutation({
    mutationFn: api.stopBot,
    onSuccess: () => {
      setStatusMessage("Bot stopped");
      setConfirmStop(false);
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: () => setStatusMessage("Failed to stop bot"),
  });

  if (isLoading) return <LoadingPanel />;
  if (error || !data) {
    return (
      <ErrorPanel
        message="Can't reach the API"
        hint="Start it with: npm run api  (port 8787)"
      />
    );
  }

  const token = data.token;
  const operating = data.operating;
  const priceChange = token ? formatPercent(token.priceChange24h) : null;
  const uptimeMs =
    data.bot.running && data.bot.startedAt
      ? now - new Date(data.bot.startedAt).getTime()
      : data.bot.uptimeMs;
  const uptime = formatDuration(uptimeMs, true);
  const startedAt = formatDateTime(data.bot.startedAt);
  const lastUpdated = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  }).format(dataUpdatedAt);

  const totals = data.stats.totals;
  const sellFill =
    totals && totals.buys > 0
      ? `${Math.round((totals.sells / totals.buys) * 100)}%`
      : null;
  const tradesPerHour = totals
    ? formatRatePerHour(totals.buys + totals.sells, uptimeMs)
    : null;
  const volumePerHour = totals?.volumeEth
    ? formatRatePerHour(totals.volumeEth, uptimeMs)
    : null;
  const efficiency = data.stats.efficiency;
  const costPerVolume = formatCostPerVolumeUsd(efficiency?.costPerVolumeUsd);
  const efficiencyLine =
    efficiency && totals
      ? [
          `~${efficiency.spentEth.toFixed(4)} ETH spent`,
          formatUsd(efficiency.spentUsd, 2, true),
          costPerVolume ? `${costPerVolume} per $1 volume` : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
  const headroomInfo = operating
    ? formatHeadroom(operating.headroomEth, operating.minBaseBalanceEth)
    : null;
  const subWallets = data.wallet.subWallets;
  const belowStop =
    operating?.belowStopThreshold ??
    (operating != null ? operating.headroomEth < 0 : false);
  const statusVariant =
    data.bot.running && belowStop
      ? "warn"
      : data.bot.running
        ? "live"
        : "outline";
  const statusLabel =
    data.bot.running && belowStop
      ? "Live · low balance"
      : data.bot.running
        ? "Live"
        : "Stopped";

  return (
    <div className="space-y-5">
      <StatusAnnouncer message={statusMessage} />
      <StartBotDialog
        open={startDialogOpen}
        onClose={() => setStartDialogOpen(false)}
        onStarted={() => {
          setStatusMessage("Bot started");
          setConfirmStop(false);
          queryClient.invalidateQueries({ queryKey: ["status"] });
        }}
      />

      <Card>
        <CardContent className="space-y-4 py-4">
          <div className="flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between">
            <div className="min-w-0 space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <Badge variant={statusVariant}>
                  {data.bot.running ? (
                    <>
                      <span
                        className="h-1.5 w-1.5 rounded-full bg-[var(--color-ok)]"
                        aria-hidden="true"
                      />
                      {statusLabel}
                    </>
                  ) : (
                    statusLabel
                  )}
                </Badge>
                {data.bot.running && uptime ? (
                  <span
                    className="font-mono text-sm font-semibold tabular-nums text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]"
                    title="Time since bot process started"
                  >
                    <span className="mr-1.5 font-sans text-xs font-medium text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                      Running
                    </span>
                    {uptime}
                  </span>
                ) : null}
              </div>
              <div className="flex flex-wrap gap-x-5 gap-y-3">
                {data.bot.running ? (
                  <>
                    <MetaItem label="Started" value={startedAt ?? "—"} />
                    <MetaItem
                      label="Process"
                      value={
                        <span translate="no">
                          {data.bot.pid ?? "—"}
                          {data.bot.source === "external"
                            ? " · terminal"
                            : " · dashboard"}
                        </span>
                      }
                    />
                    <MetaItem
                      label="Mode"
                      value={operating?.tradeMode ?? "—"}
                    />
                    {tradesPerHour ? (
                      <MetaItem
                        label="Trades/hr"
                        value={tradesPerHour}
                      />
                    ) : null}
                  </>
                ) : (
                  <MetaItem label="Status" value="No active bot process" />
                )}
              </div>
            </div>
            <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:flex-wrap">
              {!confirmStop ? (
                <>
                  <Button
                    className="w-full sm:w-auto"
                    onClick={() => setStartDialogOpen(true)}
                    disabled={data.bot.running}
                  >
                    <Play className="h-3.5 w-3.5" aria-hidden="true" />
                    Start Bot
                  </Button>
                  <Button
                    className="w-full sm:w-auto"
                    variant={data.bot.running ? "destructive" : "outline"}
                    onClick={() =>
                      data.bot.running ? setConfirmStop(true) : undefined
                    }
                    disabled={!data.bot.running || stop.isPending}
                  >
                    <Square className="h-3.5 w-3.5" aria-hidden="true" />
                    Stop Bot
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    className="w-full sm:w-auto"
                    variant="destructive"
                    onClick={() => stop.mutate()}
                    disabled={stop.isPending}
                    aria-busy={stop.isPending}
                  >
                    {stop.isPending ? "Stopping…" : "Confirm Stop"}
                  </Button>
                  <Button
                    className="w-full sm:w-auto"
                    variant="outline"
                    onClick={() => setConfirmStop(false)}
                  >
                    Cancel
                  </Button>
                </>
              )}
            </div>
          </div>
          <p className="text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
            Updated {lastUpdated}
          </p>
        </CardContent>
      </Card>

      {stop.isError && (
        <p
          className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]"
          role="alert"
        >
          Stop failed — bot may still be running.
        </p>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Wallet
                className="h-3.5 w-3.5 text-[var(--color-ink-muted)]"
                aria-hidden="true"
              />
              <CardTitle>Balance</CardTitle>
            </div>
            <div className="flex min-w-0 items-center gap-1">
              <CardDescription
                className="min-w-0 truncate font-mono"
                translate="no"
              >
                {shortAddress(data.wallet.address)}
              </CardDescription>
              <CopyAddressButton
                value={data.wallet.address}
                label="wallet address"
              />
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-0 sm:divide-x sm:divide-[var(--color-wire)] dark:sm:divide-[var(--color-wire-dark)]">
              <div className="min-w-0 space-y-1 sm:pr-3">
                <MetricLabel>Base wallet</MetricLabel>
                <EthHero amount={Number(data.wallet.balanceEth)} size="md" />
                <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                  {formatUsd(data.wallet.balanceUsd, 2, true) ??
                    "USD rate unavailable"}
                </p>
              </div>
              <div className="min-w-0 space-y-1 border-t border-[var(--color-wire)] pt-4 sm:border-t-0 sm:pt-0 sm:pl-3 dark:border-[var(--color-wire-dark)]">
                <MetricLabel>Sub-wallets (latest)</MetricLabel>
                <EthHero amount={Number(subWallets.totalEth)} size="md" />
                <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                  {formatUsd(subWallets.totalUsd, 2, true) ?? "USD unavailable"}
                  {subWallets.withBalance > 0 ? (
                    <span>
                      {" "}
                      · {subWallets.withBalance}/{subWallets.walletCount}
                    </span>
                  ) : null}
                </p>
              </div>
            </div>
            <div className="space-y-1 border-t border-[var(--color-wire)] pt-3 dark:border-[var(--color-wire-dark)]">
              <MetricLabel>Total ETH</MetricLabel>
              <EthHero amount={Number(data.wallet.combinedEth)} />
              <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                {formatUsd(data.wallet.combinedUsd, 2, true) ?? "USD unavailable"}
              </p>
            </div>
            {data.bot.running && headroomInfo?.text ? (
              <p
                className={`text-xs ${
                  headroomInfo.status === "critical"
                    ? "tone-danger font-medium"
                    : headroomInfo.status === "warning"
                      ? "tone-warn"
                      : "text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]"
                }`}
                role={headroomInfo.status === "critical" ? "alert" : undefined}
              >
                {headroomInfo.text}
              </p>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Activity
                className="h-3.5 w-3.5 text-[var(--color-ink-muted)]"
                aria-hidden="true"
              />
              <CardTitle>Last Cycle</CardTitle>
            </div>
            <CardDescription>
              {data.stats.lastCycle
                ? `Cycle ${data.stats.lastCycle.cycle} complete`
                : "Waiting for first cycle"}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <MetricLabel>Volume</MetricLabel>
              {data.stats.lastCycle ? (
                <EthHero amount={data.stats.lastCycle.volumeEth} />
              ) : (
                <p className="font-mono text-2xl font-semibold tabular-nums tracking-tight text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                  —
                </p>
              )}
              {data.stats.lastCycle?.volumeUsd != null ? (
                <p className="mt-1 text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                  {formatUsd(data.stats.lastCycle.volumeUsd)}
                </p>
              ) : null}
            </div>
            <div className="grid grid-cols-2 gap-3 border-t border-[var(--color-wire)] pt-4 sm:grid-cols-3 sm:gap-4 dark:border-[var(--color-wire-dark)]">
              <Stat label="Funded" value={data.stats.lastCycle?.funded ?? "—"} />
              <Stat label="Buys" value={data.stats.lastCycle?.buys ?? "—"} />
              <Stat label="Sells" value={data.stats.lastCycle?.sells ?? "—"} />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Session</CardTitle>
            <CardDescription>
              Totals from the active session
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {preflight ? (
              <div className="rounded-lg border border-[var(--color-wire)] bg-[var(--color-paper)] px-3 py-2.5 text-sm dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-paper-dark)]">
                <p className="font-medium text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                  Economics: ~{preflight.costs.predictedCentsPerDollar.toFixed(2)}¢ per $1 volume
                </p>
                <p className={preflight.costs.targetMet ? "text-[var(--color-ok)]" : "text-[var(--color-warn)]"}>
                  {preflight.costs.targetMet
                    ? "Within $200 / $100k target"
                    : "Above $200 / $100k target"}
                </p>
              </div>
            ) : null}
            <div>
              <MetricLabel>Total volume</MetricLabel>
              {data.stats.totals ? (
                <EthHero amount={data.stats.totals.volumeEth} />
              ) : (
                <p className="font-mono text-2xl font-semibold tabular-nums tracking-tight text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                  —
                </p>
              )}
              <p className="mt-1 text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                {formatUsd(data.stats.totals?.volumeUsd) ?? "USD unavailable"}
                {volumePerHour
                  ? ` · ${formatEth(Number(volumePerHour), 3)}/hr`
                  : ""}
              </p>
            </div>
            <div className="grid grid-cols-2 gap-4 border-t border-[var(--color-wire)] pt-4 sm:grid-cols-4 dark:border-[var(--color-wire-dark)]">
              <Stat label="Cycles" value={data.stats.totals?.cycles ?? "—"} />
              <Stat label="Funded" value={data.stats.totals?.funded ?? "—"} />
              <Stat label="Buys" value={data.stats.totals?.buys ?? "—"} />
              <Stat label="Sells" value={data.stats.totals?.sells ?? "—"} />
            </div>
            {efficiencyLine ? (
              <p className="text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                {efficiencyLine}
              </p>
            ) : null}
            {sellFill ? (
              <p className="text-xs text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                Sell fill rate{" "}
                <span
                  className={
                    Number(sellFill.replace("%", "")) >= 90
                      ? "tone-ok font-medium"
                      : "tone-warn font-medium"
                  }
                >
                  {sellFill}
                </span>
                {operating
                  ? ` · ${operating.walletsPerCycle} wallets/cycle`
                  : ""}
              </p>
            ) : null}
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Session Progress</CardTitle>
          <CardDescription>
            Volume per cycle and base wallet balance over time
          </CardDescription>
        </CardHeader>
        <CardContent>
          <SessionChart points={timeseries ?? []} ethUsd={data.wallet.ethUsd} />
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        {!token ? (
          <>
            <CardHeader>
              <CardTitle>Target Token</CardTitle>
              <CardDescription>No token configured</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                Click <strong>Start Bot</strong> to choose a token and session
                settings. Wallet credentials stay in{" "}
                <code className="font-mono text-xs">.env</code> only.
              </p>
            </CardContent>
          </>
        ) : (
          <>
        <CardHeader>
          <div className="flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between">
            <div className="flex min-w-0 items-start gap-3">
              {token.imageUrl ? (
                <img
                  src={token.imageUrl}
                  alt=""
                  width={48}
                  height={48}
                  loading="lazy"
                  className="h-12 w-12 shrink-0 rounded-full border border-[var(--color-wire)] object-cover dark:border-[var(--color-wire-dark)]"
                />
              ) : null}
              <div className="min-w-0">
                <p className="text-balance text-lg font-semibold tracking-tight text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]">
                  {token.name ?? "Unknown token"}
                  {token.symbol ? (
                    <span
                      className="ml-2 font-mono text-sm font-medium text-[var(--color-ink-muted)]"
                      translate="no"
                    >
                      {token.symbol}
                    </span>
                  ) : null}
                </p>
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1">
                  <code
                    className="break-all text-xs text-[var(--color-ink-muted)] sm:truncate"
                    translate="no"
                  >
                    {token.address}
                  </code>
                  <CopyAddressButton
                    value={token.address}
                    label="token address"
                  />
                </div>
              </div>
            </div>
            <div className="sm:text-right">
              <p className="font-mono text-xl font-semibold tabular-nums sm:text-2xl">
                {formatUsd(token.priceUsd) ?? "—"}
              </p>
              {priceChange ? (
                <p
                  className={`text-sm font-medium ${
                    (token.priceChange24h ?? 0) >= 0 ? "tone-up" : "tone-down"
                  }`}
                >
                  {priceChange} <span className="text-[var(--color-ink-muted)]">24h</span>
                </p>
              ) : null}
            </div>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <div>
              <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                Market cap
              </p>
              <p className="font-mono text-sm font-semibold tabular-nums">
                {formatCompactUsd(token.marketCapUsd) ?? "—"}
              </p>
            </div>
            <div>
              <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                Liquidity
              </p>
              <p className="font-mono text-sm font-semibold tabular-nums">
                {formatCompactUsd(token.liquidityUsd) ?? "—"}
              </p>
            </div>
            <div>
              <p className="text-[0.65rem] font-medium uppercase tracking-[0.12em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
                24h volume
              </p>
              <p className="font-mono text-sm font-semibold tabular-nums">
                {formatCompactUsd(token.volume24hUsd) ?? "—"}
              </p>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <Button
            type="button"
            variant="outline"
            className="w-full sm:w-auto"
            onClick={() => setMarketExpanded((open) => !open)}
            aria-expanded={marketExpanded}
          >
            {marketExpanded ? "Hide market details" : "Show market details"}
          </Button>

          {marketExpanded ? (
            <>
          <dl className="grid gap-x-10 md:grid-cols-2">
            <InfoRow
              label="Price in ETH"
              value={token.priceNative ? `${token.priceNative} ETH` : "—"}
            />
            <InfoRow
              label="FDV"
              value={formatCompactUsd(token.fdvUsd) ?? "—"}
            />
            <InfoRow
              label="Supply"
              value={
                token.totalSupply
                  ? Number(token.totalSupply).toLocaleString()
                  : "—"
              }
            />
            <InfoRow label="Decimals" value={token.decimals} />
            <InfoRow
              label="Pool"
              value={
                token.pool?.dex
                  ? `${token.pool.dex}${token.pool.version ? ` ${token.pool.version.toUpperCase()}` : ""}`
                  : (data.pool?.version ?? "—")
              }
            />
            <InfoRow
              label="Pair"
              value={
                token.pool?.pairAddress ? (
                  <a
                    href={token.pool.url ?? "#"}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="inline-flex items-center gap-1 font-mono text-xs underline-offset-2 hover:underline"
                  >
                    <span translate="no">
                      {shortAddress(token.pool.pairAddress)}
                    </span>
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                    <span className="sr-only">Open pair on DexScreener</span>
                  </a>
                ) : (
                  "—"
                )
              }
            />
            <InfoRow
              label="Bot route"
              value={
                data.pool
                  ? `${data.pool.version.toUpperCase()}${data.pool.fee != null ? ` · fee ${data.pool.fee}` : ""}`
                  : "—"
              }
            />
          </dl>

          {token.links.length > 0 ? (
            <div className="mt-5 flex flex-wrap gap-2 border-t border-[var(--color-wire)] pt-4 dark:border-[var(--color-wire-dark)]">
              {token.links.map((link) => (
                <a
                  key={link.url}
                  href={link.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="rounded-full border border-[var(--color-wire)] px-3 py-1.5 text-xs capitalize text-[var(--color-ink-muted)] transition-[border-color,color] duration-200 hover:border-[var(--color-ink-muted)] hover:text-[var(--color-ink)] dark:border-[var(--color-wire-dark)] dark:hover:text-[var(--color-ink-dark)]"
                >
                  {link.type}
                </a>
              ))}
            </div>
          ) : null}
            </>
          ) : null}
        </CardContent>
          </>
        )}
      </Card>
    </div>
  );
}

function LogsTab() {
  const LOG_POLL_MS = 2_000;
  const LOG_LINE_LIMIT = 500;

  const [log, setLog] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const preRef = useRef<HTMLPreElement>(null);
  const autoScrollId = "auto-scroll-toggle";

  useEffect(() => {
    let active = true;

    const refresh = () => {
      if (!active || document.visibilityState === "hidden") return;
      api
        .getLogs(LOG_LINE_LIMIT)
        .then((r) => {
          if (active) setLog(r.log);
        })
        .catch(() => {
          if (active) setLog("");
        });
    };

    refresh();
    const id = window.setInterval(refresh, LOG_POLL_MS);
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      active = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  useEffect(() => {
    if (!autoScroll) return;
    const el = preRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [log, autoScroll]);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Terminal
                className="h-3.5 w-3.5 text-[var(--color-ink-muted)]"
                aria-hidden="true"
              />
              <CardTitle>Output</CardTitle>
            </div>
            <CardDescription>Polls the last {LOG_LINE_LIMIT} log lines (memory-safe)</CardDescription>
          </div>
          <label
            htmlFor={autoScrollId}
            className="flex min-h-11 cursor-pointer items-center gap-2 text-xs text-[var(--color-ink-muted)]"
          >
            <input
              id={autoScrollId}
              name="auto-scroll"
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="h-4 w-4 rounded border-[var(--color-wire)]"
            />
            Auto-scroll
          </label>
        </div>
      </CardHeader>
      <CardContent>
        <pre
          ref={preRef}
          className="max-h-[62vh] overflow-auto overscroll-contain rounded-lg border border-[var(--color-wire)] bg-[#141414] p-4 font-mono text-[0.72rem] leading-relaxed text-[#d4d4d4] dark:border-[var(--color-wire-dark)]"
          aria-label="Bot log output"
        >
          {log || "No output yet. Start the bot to see logs here."}
        </pre>
      </CardContent>
    </Card>
  );
}

function ConfigTab() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["config"],
    queryFn: api.getConfig,
  });

  if (isLoading) return <LoadingPanel />;
  if (error || !data)
    return (
      <ErrorPanel
        message="Couldn't load config"
        hint="Check that the API is running"
      />
    );

  const rows: [string, string | number | boolean][] = [
    [
      "Last session token",
      data.lastSession?.targetTokenAddress ?? "None",
    ],
    ["Trade mode", data.tradeScheduleMode],
    [
      "Trade sizes",
      data.lastSession?.tradeSizeBias === 'random' ? 'random' : data.tradeSizeBias === 'random' ? 'random' : 'max',
    ],
    ["Endless loop", data.endlessMode ? "on" : "off"],
    ["Wallets per cycle", data.subWalletNum],
    ["Parallel batch", data.parallelWallets],
    ["Trade min (ETH)", data.amountMin],
    ["Trade max (ETH)", data.amountMax],
    ["Fee reserve (ETH)", data.fee],
    ["Stop below (ETH)", data.minBaseBalanceEth],
    ["Cycle pause (ms)", data.cyclePauseMs],
    ["Buy stagger max (ms)", data.buyStaggerMaxMs],
    ["Sell delay max (ms)", data.sellDelayMaxMs],
    ["Pool preference", data.poolVersionPreference],
    ["Chain ID", data.chainId],
  ];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Settings2
            className="h-3.5 w-3.5 text-[var(--color-ink-muted)]"
            aria-hidden="true"
          />
          <CardTitle>Bot Settings</CardTitle>
        </div>
        <CardDescription>
          Defaults from config.ts — token and session settings are set when you start the bot
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl>
          {rows.map(([key, value]) => (
            <InfoRow
              key={key}
              label={key}
              value={<span className="font-mono">{String(value)}</span>}
            />
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

export default function App() {
  const [tab, setTab] = useTabState();

  return (
    <>
      <a href="#main-content" className="skip-link">
        Skip to main content
      </a>

      <div className="relative mx-auto min-h-dvh max-w-6xl px-4 pb-10 pt-6 sm:px-6 sm:pb-14 sm:pt-8">
        <header className="mb-8 sm:mb-10">
          <div className="flex items-center gap-2.5">
            <img
              src="/logo.png"
              alt=""
              width={28}
              height={28}
              className="h-7 w-7 shrink-0 rounded-md"
            />
            <p className="text-[0.65rem] font-semibold uppercase tracking-[0.2em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
              Robinhood Chain · Volume Bot
            </p>
          </div>
          <h1 className="text-balance mt-2 text-2xl font-semibold tracking-tight text-[var(--color-ink)] sm:text-3xl dark:text-[var(--color-ink-dark)]">
            Operator Console
          </h1>
          <p className="mt-2 max-w-xl text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
            Monitor wallet balance, session trades, and token liquidity. Start
            or stop the bot from here.
          </p>
        </header>

        <main id="main-content">
          <Tabs
            value={tab}
            onValueChange={(value) => isTabId(value) && setTab(value)}
          >
            <TabsList>
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="wallets">Wallets</TabsTrigger>
              <TabsTrigger value="sessions">
                <History className="mr-1.5 hidden h-3.5 w-3.5 sm:inline" aria-hidden="true" />
                Sessions
              </TabsTrigger>
              <TabsTrigger value="logs">Output</TabsTrigger>
              <TabsTrigger value="config">Settings</TabsTrigger>
            </TabsList>

            <TabsContent value="overview">
              <OverviewTab />
            </TabsContent>
            <TabsContent value="wallets">
              <WalletsTab />
            </TabsContent>
            <TabsContent value="sessions">
              <SessionsTab />
            </TabsContent>
            <TabsContent value="logs">
              <LogsTab />
            </TabsContent>
            <TabsContent value="config">
              <ConfigTab />
            </TabsContent>
          </Tabs>
        </main>
      </div>
    </>
  );
}
