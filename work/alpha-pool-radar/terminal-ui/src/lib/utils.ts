import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function shortAddress(address: string) {
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

export function formatUsd(
  value: number | null | undefined,
  digits = 2,
  approx = false
) {
  if (value == null || Number.isNaN(value)) return null;

  let formatted: string;

  if (value > 0 && value < 0.01) {
    if (value < 0.000001) {
      formatted = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        notation: "scientific",
        maximumFractionDigits: 2,
      }).format(value);
    } else {
      const microDigits = Math.min(10, Math.ceil(-Math.log10(value)) + 2);
      formatted = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: microDigits,
        maximumFractionDigits: microDigits,
      }).format(value);
    }
  } else {
    formatted = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }

  return approx ? `~${formatted}` : formatted;
}

export function formatHeadroom(
  headroomEth: number,
  minBaseBalanceEth: number,
): { text: string; status: "ok" | "warning" | "critical" } {
  if (headroomEth < 0) {
    return {
      text: `${Math.abs(headroomEth).toFixed(4)} ETH below ${minBaseBalanceEth} ETH stop — bot stopping`,
      status: "critical",
    };
  }

  if (headroomEth < minBaseBalanceEth * 0.2) {
    return {
      text: `${headroomEth.toFixed(4)} ETH headroom — low, add ETH soon`,
      status: "warning",
    };
  }

  return {
    text: `${headroomEth.toFixed(4)} ETH headroom above ${minBaseBalanceEth} ETH stop`,
    status: "ok",
  };
}

export function formatCompactUsd(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return null;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatTickerUsd(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return null;
  if (value >= 1000) {
    return `$${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)}K`;
  }
  return formatUsd(value) ?? null;
}

export function formatCostPerVolumeUsd(cents: number | null | undefined) {
  if (cents == null || Number.isNaN(cents)) return null;
  if (cents < 0.1) return "<0.1¢";
  if (cents < 10) return `${cents.toFixed(1)}¢`;
  return `${Math.round(cents)}¢`;
}

export function formatPercent(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return null;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function formatEth(value: number | null | undefined, digits = 4) {
  if (value == null || Number.isNaN(value)) return null;
  return `${value.toFixed(digits)} ETH`;
}

export function formatDuration(ms: number | null | undefined, precise = false) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;

  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;

  if (h > 0) return precise ? `${h}h ${m}m ${s}s` : `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function formatPlanTimeframe(duration: {
  ms: number;
  minMs: number;
  maxMs: number;
}) {
  const avg = formatDuration(duration.ms);
  if (!avg) return null;

  const min = formatDuration(duration.minMs);
  const max = formatDuration(duration.maxMs);
  if (min && max && duration.maxMs - duration.minMs > duration.ms * 0.15) {
    return `~${avg} (${min}–${max})`;
  }
  return `~${avg}`;
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

export function formatRatePerHour(
  count: number,
  uptimeMs: number | null | undefined,
) {
  if (uptimeMs == null || uptimeMs < 60_000) return null;
  const perHour = (count / uptimeMs) * 3_600_000;
  return perHour >= 10 ? perHour.toFixed(0) : perHour.toFixed(1);
}
