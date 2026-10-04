export const UNAVAILABLE_LABEL = "不可用";

export function formatNullableNumber(
  value: number | null | undefined,
  options: Intl.NumberFormatOptions = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return UNAVAILABLE_LABEL;
  }
  return new Intl.NumberFormat("en-US", options).format(value);
}

export function formatUsd(value: number | null | undefined): string {
  const maximumFractionDigits = value !== null && value !== undefined && Math.abs(value) < 1 ? 8 : 2;
  return formatNullableNumber(value, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits,
  });
}
