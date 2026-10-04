"""Pure qualification of dated GMGN profit-history evidence; no scanner imports.

Rows retain their complete input stats/activity. This validates reported history,
not independent fee-inclusive accounting or suitability for copying trades.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math
import re


POLICY_ID = "gmgn_profit_history_v1"
POLICY = {
    "id": POLICY_ID, "required_status": "profit_history_supported",
    "evidence_max_age_days": 3,
    "30d": {"profit_usd_gt": 500, "token_count_min": 20, "sell_count_min": 20},
    "7d": {"profit_usd_gt": 0, "token_count_min": 5, "sell_count_min": 5},
    "activity": {"observed_token_count_min": 5, "tokens_with_buy_and_sell_min": 3,
                 "sell_cost_basis_coverage_min": .8, "sells_with_reported_cost_basis_min": 10,
                 "profitable_tokens_in_observed_sell_sample_min": 3,
                 "largest_token_trade_share_max": .75,
                 "largest_positive_token_margin_share_max": .8,
                 "sample_reported_sale_margin_usd_gt": 0},
    "ranking": "round-robin chains; descending 30d then 7d reported realized profit within chain",
    "label_only_qualification": False, "legacy_fallback": False,
    "accounting": "upstream realized profit and sampled sale margins; fee inclusion not independently reconciled",
}
CHAINS = {"bsc": "bsc", "bnb": "bsc", "56": "bsc", "robinhood": "robinhood",
          "4663": "robinhood", "base": "base", "8453": "base", "sol": "sol",
          "solana": "sol", "501": "sol", "eth": "eth", "ethereum": "eth", "1": "eth"}
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def wallet_identity(chain, address):
    """Return a supported canonical chain/address, preserving Solana case."""
    chain = CHAINS.get(str(chain).strip().lower())
    if not chain or not isinstance(address, str):
        return None
    if chain != "sol":
        return (chain, address.lower()) if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) and int(address[2:], 16) else None
    if not 32 <= len(address) <= 44 or any(c not in BASE58 for c in address):
        return None
    value = 0
    for char in address:
        value = value * 58 + BASE58.index(char)
    size = len(address) - len(address.lstrip("1")) + (value.bit_length() + 7) // 8
    return (chain, address) if size == 32 else None


def _count(value, minimum):
    value = _number(value)
    return value is not None and value.is_integer() and value >= minimum


def _qualified(row, now):
    if not isinstance(row, dict) or row.get("status") != POLICY["required_status"]:
        return False
    identity = wallet_identity(row.get("chain"), row.get("address"))
    checked = _time(row.get("evidence_checked_at"))
    if not identity or not checked or not 0 <= (now - checked).total_seconds() <= 3 * 86400:
        return False
    for period in ("30d", "7d"):
        stats = row.get("stats_" + period)
        if not isinstance(stats, dict):
            return False
        if "wallet" in stats and wallet_identity(identity[0], stats["wallet"]) != identity:
            return False
        if "period_requested" in stats and stats["period_requested"] != period:
            return False
        rule = POLICY[period]
        profit = _number(stats.get("realized_profit_usd"))
        if (profit is None or profit <= rule["profit_usd_gt"] or
                not _count(stats.get("token_count"), rule["token_count_min"]) or
                not _count(stats.get("sell_count"), rule["sell_count_min"])):
            return False
    activity = row.get("activity")
    if not isinstance(activity, dict):
        return False
    for name in ("observed_token_count", "tokens_with_buy_and_sell",
                 "sells_with_reported_cost_basis", "profitable_tokens_in_observed_sell_sample"):
        if not _count(activity.get(name), POLICY["activity"][name + "_min"]):
            return False
    for name, lower, upper in (("sell_cost_basis_coverage", .8, 1),
                                ("largest_token_trade_share", 0, .75),
                                ("largest_positive_token_margin_share", 0, .8)):
        value = _number(activity.get(name))
        if value is None or value <= 0 or not lower <= value <= upper:
            return False
    margin = _number(activity.get("sample_reported_sale_margin_usd"))
    if margin is None or margin <= 0:
        return False
    return all(_number(activity[name]) <= _number(activity["observed_token_count"])
               for name in ("tokens_with_buy_and_sell", "profitable_tokens_in_observed_sell_sample"))


def qualified_wallets(payload: dict, now_iso: str, limit: int = 50) -> list[dict]:
    """Recheck every row; dates come only from row evidence_checked_at.

    `wallets` is authoritative if present, including an empty list. Otherwise
    `supported_wallets` is accepted, subject to the exact same validation.
    Invalid input returns an empty list. Output is detached from the input.
    """
    now = _time(now_iso)
    if not now or not isinstance(payload, dict) or isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    rows = payload.get("wallets", payload.get("supported_wallets", []))
    if not isinstance(rows, list):
        return []
    unique = {}
    for row in rows:
        if not _qualified(row, now):
            continue
        key = wallet_identity(row["chain"], row["address"])
        previous = unique.get(key)
        if previous is None or _time(row["evidence_checked_at"]) > _time(previous["evidence_checked_at"]):
            unique[key] = row
    groups = {}
    for key, row in unique.items():
        copy = deepcopy(row)
        copy.update(chain=key[0], address=key[1], quality_policy=POLICY_ID)
        groups.setdefault(key[0], []).append(copy)
    for group in groups.values():
        group.sort(key=lambda r: (-_number(r["stats_30d"]["realized_profit_usd"]),
                                 -_number(r["stats_7d"]["realized_profit_usd"]), r["address"]))
    result = [groups[chain][i] for i in range(max(map(len, groups.values()), default=0))
              for chain in sorted(groups) if i < len(groups[chain])][:limit]
    for rank, row in enumerate(result, 1):
        row["rank"] = rank
    return result
