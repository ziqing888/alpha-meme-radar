export type PaperValuation = {
  equity_usd?: number | null;
  valuation_status?: "fresh" | "stale" | "unavailable";
  stale_positions?: number;
  unpriced_positions?: number;
};

export function paperValuationLabel(summary?: PaperValuation): string {
  if (summary?.valuation_status === "unavailable") return "估值不可用";
  if (summary?.equity_usd == null) return "估值待核验";
  if (summary.valuation_status === "stale") return "含过期估值";
  return summary.valuation_status === "fresh" ? "估值新鲜" : "估值状态未知";
}
