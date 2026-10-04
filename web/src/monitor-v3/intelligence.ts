import type { MonitorTokenIntelligence } from "./types";
import { normalizeMonitorChain } from "./selectors";

export interface IntelligenceSourceRow {
  chain?: unknown;
  chain_id?: unknown;
  token_address?: unknown;
  contract_address?: unknown;
  token_intelligence?: MonitorTokenIntelligence | null;
}

function normalizedChain(value: unknown): string {
  const chain = normalizeMonitorChain(value);
  return ({
    "1": "ethereum",
    eth: "ethereum",
    ethereum: "ethereum",
    "56": "bsc",
    bnb: "bsc",
    bsc: "bsc",
    "binance-smart-chain": "bsc",
    "501": "solana",
    sol: "solana",
    solana: "solana",
    "8453": "base",
    base: "base",
    robinhood: "robinhood",
  } as Record<string, string>)[chain] ?? chain;
}

function normalizedAddress(value: unknown): string {
  const address = String(value ?? "").trim();
  return /^0x/i.test(address) ? address.toLowerCase() : address;
}

export function tokenIntelligenceKey(chain: unknown, address: unknown): string {
  const normalizedChainName = normalizedChain(chain);
  const normalizedContract = normalizedAddress(address);
  return normalizedChainName && normalizedContract
    ? `${normalizedChainName}:${normalizedContract}`
    : "";
}

function intelligenceRank(value: MonitorTokenIntelligence): number {
  return ({ ready: 4, partial: 3, pending: 2, unavailable: 1 } as Record<string, number>)[value.status ?? ""] ?? 0;
}

function intelligenceTime(value: MonitorTokenIntelligence): number {
  const raw = value.ai_analyzed_at ?? value.generated_at;
  if (raw == null || raw === "") return 0;
  const numeric = typeof raw === "number" ? raw : /^\d+(\.\d+)?$/.test(raw) ? Number(raw) : NaN;
  const parsed = Number.isFinite(numeric) ? (numeric < 1e12 ? numeric * 1000 : numeric) : Date.parse(String(raw));
  return Number.isFinite(parsed) ? parsed : 0;
}

export function buildTokenIntelligenceIndex(
  rows: IntelligenceSourceRow[],
): Record<string, MonitorTokenIntelligence> {
  const index: Record<string, MonitorTokenIntelligence> = {};

  for (const row of rows) {
    const intelligence = row.token_intelligence;
    if (!intelligence) continue;
    const key = tokenIntelligenceKey(
      row.chain ?? row.chain_id,
      row.token_address ?? row.contract_address,
    );
    if (!key) continue;

    const current = index[key];
    if (
      !current ||
      intelligenceRank(intelligence) > intelligenceRank(current) ||
      (intelligenceRank(intelligence) === intelligenceRank(current) &&
        intelligenceTime(intelligence) >= intelligenceTime(current))
    ) {
      index[key] = intelligence;
    }
  }

  return index;
}
