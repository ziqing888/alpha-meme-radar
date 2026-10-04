"""Read-only GMGN wallet history screening, distinct from activity rankings."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import time

import alpha_gmgn_smart_money_top50 as gmgn

OUT = Path(__file__).resolve().parents[2] / "outputs"
REFERENCE = "0xaa4e9e8f1e9c03e6de9f5f78680a0f8ae5908709"
CACHE_SECONDS = 3600
QUERY_BUDGET_EXHAUSTED = "query budget exhausted"
CHAIN_ALIASES = {"solana": "sol", "501": "sol", "ethereum": "eth", "1": "eth",
                 "56": "bsc", "bnb": "bsc", "8453": "base", "4663": "robinhood"}


def wallet_identity(chain, address):
    chain = str(chain or "").strip().lower()
    chain = CHAIN_ALIASES.get(chain, chain)
    address = str(address or "").strip()
    if chain not in {"bsc", "eth", "base", "robinhood", "sol"}:
        return chain, address, "invalid_chain"
    pattern = gmgn.SOLANA_ADDRESS_RE if chain == "sol" else gmgn.EVM_ADDRESS_RE
    if not pattern.fullmatch(address):
        return chain, address, "invalid_wallet_address"
    return chain, address if chain == "sol" else address.lower(), None


def cache_mtime(path, max_age_seconds=CACHE_SECONDS):
    try:
        stamp = path.stat().st_mtime
        return stamp if 0 <= time.time() - stamp < max_age_seconds else None
    except OSError:
        return None


def retry_epoch(status):
    if not isinstance(status, dict):
        return 0
    values = [num(status.get("retry_after_epoch")) or 0]
    try:
        stamp = datetime.fromisoformat(str(status.get("retry_after") or "").replace("Z", "+00:00"))
        if stamp.tzinfo:
            values.append(stamp.timestamp())
    except (ValueError, OverflowError):
        pass
    if isinstance(status.get("status"), dict):
        values.append(retry_epoch(status["status"]))
    return max(values)


def num(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def first(row, *keys):
    return next((row[k] for k in keys if row.get(k) is not None), None)


def normalize_stats(raw, period):
    pnl = raw.get("pnl_stat") or {}
    common = raw.get("common") or {}
    return {
        "period_requested": period, "wallet": raw.get("wallet_address"),
        "realized_profit_usd": num(raw.get("realized_profit")),
        "unrealized_profit_usd": num(raw.get("unrealized_profit")),
        "buy_count": num(first(raw, "buy_count", "buy")),
        "sell_count": num(first(raw, "sell_count", "sell")),
        "token_count": num(first(pnl, "token_num") if pnl.get("token_num") is not None else raw.get("token_num")),
        "winrate": num(first(raw, "winrate") if raw.get("winrate") is not None else pnl.get("winrate")),
        "reported_pnl_ratio": num(first(raw, "realized_profit_pnl", "pnl")),
        "reported_buy_fees_usd": num(raw.get("bought_fee")),
        "reported_sell_fees_usd": num(raw.get("sold_fee")),
        "fee_adjusted_profit_usd": None,
        "fee_accounting": "upstream realized profit; fee inclusion not independently reconciled",
        "last_trade_at": num(first(raw, "last_timestamp", "last_active_timestamp")),
        "name": first(common, "name", "nick_name") or common.get("twitter_name") or "",
        "tags": common.get("tags") or [],
    }


def extract_stats(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict) and item.get("wallet_address")]
    if not isinstance(payload, dict):
        return []
    if payload.get("wallet_address"):
        return [payload]
    for key in ("data", "wallets", "stats"):
        if key in payload:
            return extract_stats(payload[key])
    return []


def stats_gate(month, week):
    reasons = []
    for period, row, tokens, sells, profit in (("30d", month, 20, 20, 500), ("7d", week, 5, 5, 0)):
        if not row:
            reasons.append(period + ":missing_stats")
            continue
        if row.get("realized_profit_usd") is None:
            reasons.append(period + ":missing_profit")
        elif row["realized_profit_usd"] <= profit:
            reasons.append(period + ":insufficient_realized_profit")
        if (row.get("token_count") or 0) < tokens:
            reasons.append(period + ":insufficient_token_diversity")
        if (row.get("sell_count") or 0) < sells:
            reasons.append(period + ":insufficient_sells")
    return reasons


def stats_complete(month, week):
    return all(row.get(key) is not None for row in (month, week)
               for key in ("realized_profit_usd", "token_count", "sell_count"))


def activity_summary(rows, complete):
    unique = {}
    for row in rows:
        token = (row.get("token") or {}).get("address")
        tx = first(row, "tx_hash", "transaction_hash")
        side = first(row, "event_type", "type")
        if tx and token:
            unique.setdefault((tx, token, side), row)
    rows = list(unique.values())
    trades = [r for r in rows if first(r, "event_type", "type") in {"buy", "sell"}]
    by_token = defaultdict(set)
    token_counts = Counter()
    margins = defaultdict(float)
    sell_count, matched_sells = 0, 0
    for row in trades:
        token = row["token"]["address"]
        side = first(row, "event_type", "type")
        by_token[token].add(side)
        token_counts[token] += 1
        if side == "sell":
            sell_count += 1
            income, basis = num(row.get("cost_usd")), num(row.get("buy_cost_usd"))
            if income is not None and basis is not None and basis > 0:
                matched_sells += 1
                margins[token] += income - basis
    stamps = sorted(num(r.get("timestamp")) for r in trades if num(r.get("timestamp")) is not None)
    positive = [v for v in margins.values() if v > 0]
    return {
        "observed_trade_count": len(trades), "observed_token_count": len(by_token),
        "tokens_with_buy_and_sell": sum({"buy", "sell"} <= sides for sides in by_token.values()),
        "transfer_event_count": sum("transfer" in str(first(r, "event_type", "type")).lower() for r in rows),
        "history_complete": complete, "independent_net_profit_usd": None,
        "newest_trade_at": max(stamps, default=None), "oldest_trade_at": min(stamps, default=None),
        "median_trade_gap_seconds": statistics.median([b-a for a,b in zip(stamps, stamps[1:])]) if len(stamps)>1 else None,
        "largest_token_trade_share": max(token_counts.values(), default=0) / max(1, len(trades)),
        "sells_with_reported_cost_basis": matched_sells,
        "sell_cost_basis_coverage": matched_sells / sell_count if sell_count else None,
        "profitable_tokens_in_observed_sell_sample": len(positive),
        "largest_positive_token_margin_share": max(positive) / sum(positive) if positive else None,
        "sample_reported_sale_margin_usd": sum(margins.values()) if margins else None,
        "sale_margin_basis": "sell proceeds minus upstream buy_cost_usd; partial history; not independent fee-inclusive PnL",
        "representative_tokens": [{"address": t, "trades": n} for t,n in token_counts.most_common(5)],
    }


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def activity_gate(evidence):
    concerns = []
    if evidence["observed_token_count"] < 5 or evidence["tokens_with_buy_and_sell"] < 3:
        concerns.append("recent_sample_insufficient_multitoken_history")
    if (evidence.get("sell_cost_basis_coverage") or 0) < .8 or evidence.get("sells_with_reported_cost_basis", 0) < 10:
        concerns.append("insufficient_sell_cost_basis_coverage")
    if evidence.get("profitable_tokens_in_observed_sell_sample", 0) < 3:
        concerns.append("insufficient_profitable_token_sample")
    if evidence["largest_token_trade_share"] > .75:
        concerns.append("recent_trades_concentrated_in_one_token")
    if (evidence["largest_positive_token_margin_share"] or 0) > .8:
        concerns.append("recent_positive_margins_dominated_by_one_token")
    if (evidence["sample_reported_sale_margin_usd"] or 0) <= 0:
        concerns.append("recent_cost_basis_sell_sample_not_positive")
    return concerns


def balanced_shortlist(rows, limit):
    groups = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r["address"] != REFERENCE, -r["stats_30d"]["realized_profit_usd"])):
        groups[row["chain"]].append(row)
    return [group[i] for i in range(max(map(len, groups.values()), default=0))
            for group in groups.values() if i < len(group)][:limit]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


class PublicCLI:
    def __init__(self, out_dir=OUT, request_interval=5, max_queries=0):
        if max_queries < 0:
            raise ValueError("max_queries must be non-negative")
        self.max_queries = max_queries
        self.query_count = 0
        self.request_interval = max(2, request_interval)
        self.status_path = out_dir / "gmgn-wallet-profit-query-status.json"
        self.shared_status_path = out_dir / "meme-source-inbox" / "gmgn-wallet-flow.json"
        self.runner, self.env = None, None

    def check_cooldown(self):
        if max(retry_epoch(read(path)) for path in (self.status_path, self.shared_status_path)) > time.time():
            raise RuntimeError("GMGN rate limited; cooldown remains active")

    def prepare(self):
        if self.runner:
            return
        self.check_cooldown()
        runner, _ = gmgn.resolve_gmgn_runner()
        self.env, _ = gmgn.gmgn_env()
        if not runner:
            raise RuntimeError("GMGN CLI unavailable")
        self.check_cooldown()
        check = subprocess.run(runner + ["config", "--check"], env=self.env, capture_output=True, timeout=25,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if check.returncode:
            raise RuntimeError("GMGN config check failed")
        self.runner = runner

    def call(self, args):
        args = list(args)
        if "--chain" not in args or args.index("--chain") + 1 >= len(args):
            raise ValueError("invalid_chain")
        chain_index = args.index("--chain") + 1
        wallet_index = args.index("--wallet") + 1 if "--wallet" in args else None
        wallet = args[wallet_index] if wallet_index is not None and wallet_index < len(args) else ""
        chain, address, error = wallet_identity(args[chain_index], wallet)
        if error and (error == "invalid_chain" or wallet_index is not None):
            raise ValueError(error)
        args[chain_index] = chain
        if wallet_index is not None:
            args[wallet_index] = address
        if self.max_queries and self.query_count >= self.max_queries:
            raise RuntimeError(QUERY_BUDGET_EXHAUSTED)
        self.check_cooldown()
        time.sleep(self.request_interval)
        self.check_cooldown()
        self.prepare()
        self.check_cooldown()
        self.query_count += 1
        result = subprocess.run(self.runner + args + ["--raw"], env=self.env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            message = result.stderr + result.stdout
            if "429" in message or "rate_limit" in message.lower():
                reset = re.search(r'(?:reset_at|resetAt)[^0-9]{0,12}(\d{10})', message)
                retry = max(time.time()+300, float(reset.group(1)) if reset else 0,
                            retry_epoch(read(self.status_path)), retry_epoch(read(self.shared_status_path)))
                write(self.status_path, {**read(self.status_path), "ok":False,"rate_limited":True,"retry_after_epoch":retry,
                                        "retry_after":datetime.fromtimestamp(retry,timezone.utc).isoformat()})
                raise RuntimeError("GMGN rate limited; screening stopped without retry")
            safe = message.replace(self.env.get("GMGN_API_KEY") or "___", "[redacted]")
            raise RuntimeError("GMGN public query failed: " + " ".join(args[:2]) + ": " + safe[-250:])
        return json.loads(result.stdout)


def discover(out_dir, cli, max_wallets, local_only=False):
    candidates = {("bsc", REFERENCE): {"address": REFERENCE, "chain": "bsc", "source": "user_reference"}}
    for row in read(out_dir / "gmgn-smart-money-top50.json").get("wallets", []):
        if row.get("chain") and row.get("address"):
            candidates[(row["chain"], row["address"])] = row
    for path in sorted((out_dir / "meme-source-inbox").glob("*.json")):
        for row in gmgn.extract_json_rows(read(path)):
            chain = first(row, "chain", "chainId", "network")
            for address in gmgn.wallet_addresses_from_row(row):
                candidates.setdefault((chain, address), {"chain": chain, "address": address, "source": path.stem})
    for chain in (() if local_only else ("bsc", "robinhood", "base", "sol")):
        try:
            raw = cli.call(["track", "smartmoney", "--chain", chain, "--limit", "100"])
        except RuntimeError as exc:
            if str(exc) == QUERY_BUDGET_EXHAUSTED or "rate limited" in str(exc):
                break
            raise
        for row in gmgn.extract_json_rows(raw):
            wallet = first(row, "maker", "wallet_address", "maker_address")
            if isinstance(wallet, str) and wallet:
                normalized = "sol" if chain == "solana" else chain
                candidates.setdefault((normalized, wallet), {"chain": normalized, "address": wallet, "source": "gmgn_smartmoney_tag"})
    # Preserve chain diversity instead of letting one high-activity chain fill the scan.
    groups = defaultdict(list)
    seen = set()
    for row in candidates.values():
        chain, address, error = wallet_identity(row.get("chain"), row.get("address"))
        if not error and (chain, address) not in seen:
            seen.add((chain, address))
            groups[chain].append({**row, "chain": chain, "address": address})
    balanced = []
    while any(groups.values()) and len(balanced) < max_wallets:
        for rows in groups.values():
            if rows and len(balanced) < max_wallets:
                balanced.append(rows.pop(0))
    return balanced


def run(args):
    previous_snapshot = read(args.out_dir / "gmgn-profitable-wallets.json")
    resume_after = previous_snapshot.get("activity_resume_after") if isinstance(previous_snapshot, dict) else None
    stats_cache_hours = num(getattr(args, "stats_cache_hours", 1))
    if stats_cache_hours is None or stats_cache_hours <= 0:
        raise ValueError("stats_cache_hours must be positive and finite")
    stats_cache_seconds = stats_cache_hours * 3600
    cli = None if args.cache_only else PublicCLI(args.out_dir, getattr(args, "request_interval", 5), getattr(args, "max_queries", 0))
    cached_candidates = read(args.out_dir / "gmgn-wallet-profit-candidates.json").get("wallets") or []
    if args.cache_only and not cached_candidates:
        raise RuntimeError("No cached candidates for offline screening")
    candidates = cached_candidates if (args.resume or args.cache_only) and cached_candidates else discover(args.out_dir, cli, args.max_wallets)
    if getattr(args, "expand_candidates", False):
        # Expansion is opt-in and only reads existing local discovery datasets.
        candidates = list(candidates)
        known = {wallet_identity(r.get("chain"), r.get("address"))[:2] for r in candidates}
        for row in discover(args.out_dir, None, args.max_wallets, local_only=True):
            key = (row["chain"], row["address"])
            if len(candidates) >= args.max_wallets:
                break
            if key not in known:
                known.add(key)
                candidates.append(row)
    normalized, seen = [], set()
    for row in candidates:
        chain, address, error = wallet_identity(row.get("chain"), row.get("address"))
        if (chain, address) not in seen:
            normalized.append({**row, "chain": chain, "address": address, "identity_error": error})
            seen.add((chain, address))
    candidates = normalized
    write(args.out_dir / "gmgn-wallet-profit-candidates.json", {"wallets": candidates})
    snapshot = {"updated_at": datetime.now(timezone.utc).isoformat(), "candidate_count": len(candidates),
                "activity_resume_after": resume_after,
                "method": "GMGN public 7d/30d stats plus recent activity sample; not independent complete-ledger reconciliation",
                "wallets": [], "errors": [], "screened_count": 0}
    cache = args.out_dir / "wallet-profitability-evidence"
    groups = defaultdict(list)
    for row in candidates:
        if not row["identity_error"]:
            groups[row["chain"]].append(row)
    all_stats = {}
    stats_mtimes = {}
    blocked = args.cache_only
    ordered = [(chain, [rows[i]]) for i in range(max(map(len, groups.values()), default=0))
               for chain, rows in groups.items() if i < len(rows)]
    for chain, rows in ordered:
        # The observed upstream batch response returned only the first wallet.
        # Query individually and still require the returned address to match.
        for offset in range(0, len(rows)):
            batch = rows[offset:offset+1]
            for period in ("30d", "7d"):
                command = ["portfolio", "stats", "--chain", chain, "--period", period, "--wallet"] + [r["address"] for r in batch]
                path = cache / f"stats-{chain}-{batch[0]['address']}-{period}.json"
                try:
                    stamp = cache_mtime(path, stats_cache_seconds)
                    if stamp is not None:
                        raw = read(path)
                    elif blocked or args.activity_only:
                        continue
                    else:
                        raw = cli.call(command)
                        write(path, raw)
                        stamp = cache_mtime(path, stats_cache_seconds)
                except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                    if "rate limited" in str(exc) or str(exc) == QUERY_BUDGET_EXHAUSTED:
                        blocked = True
                    snapshot["errors"].append({"chain":chain,"wallet":batch[0]["address"],"period":period,"error":str(exc)})
                    continue
                for value in extract_stats(raw):
                    returned_chain, returned_address, error = wallet_identity(value.get("chain") or chain, value["wallet_address"])
                    if not error and returned_chain == chain and returned_address == batch[0]["address"] and stamp is not None:
                        key = (chain, returned_address, period)
                        all_stats[key] = normalize_stats(value, period)
                        stats_mtimes[key] = stamp
            print(json.dumps({"chain": chain, "wallet": batch[0]["address"],
                              "stats_available": sum((chain,batch[0]["address"],p) in all_stats for p in ("7d","30d"))}), flush=True)
    for row in candidates:
        month = all_stats.get((row["chain"],row["address"],"30d"), {})
        week = all_stats.get((row["chain"],row["address"],"7d"), {})
        reasons = stats_gate(month, week)
        if row["identity_error"]:
            reasons = [row["identity_error"]]
        row = {"chain": row["chain"], "address": row["address"], "stats_30d": month, "stats_7d": week,
               "evidence_checked_at": None, "query_skipped_reason": row["identity_error"],
               "gate_reasons": reasons, "status": "pending_stats" if not stats_complete(month, week) else ("rejected" if reasons else "positive_stats_pending_activity"),
               "source": "gmgn_public_portfolio", "independent_profit_verified": False}
        snapshot["wallets"].append(row)
    eligible = [r for r in snapshot["wallets"] if not r["gate_reasons"]]
    shortlist = balanced_shortlist(eligible, len(eligible))
    for index, row in enumerate(shortlist):
        if f"{row['chain']}:{row['address']}" == resume_after:
            shortlist = shortlist[index+1:] + shortlist[:index+1]
            break
    # Rotate before limiting network work; cached profiles outside this window still count.
    activity_targets = {f"{r['chain']}:{r['address']}" for r in shortlist[:max(0, args.activity_wallets)]}
    checked_activity = 0
    for row in shortlist:
        activity_key = f"{row['chain']}:{row['address']}"
        cache_only = blocked or activity_key not in activity_targets
        activities, cursor, complete = [], None, False
        pages_read = 0
        cached_prefix = True
        previous_page_mtime = 0
        activity_mtimes = []
        for page in range(args.activity_pages):
            command = ["portfolio", "activity", "--chain",row["chain"],"--wallet",row["address"],"--limit","100"]
            if cursor:
                command += ["--cursor", cursor]
            path = cache / f"activity-{row['chain']}-{row['address']}-{page}.json"
            try:
                stamp = cache_mtime(path)
                if cached_prefix and stamp is not None and stamp >= previous_page_mtime:
                    raw = read(path)
                    request = raw.get("_profitability_request") if isinstance(raw, dict) else None
                    if request is not None and request != {"chain": row["chain"], "wallet": row["address"], "cursor": cursor, "page": page}:
                        if cache_only:
                            break
                        cached_prefix = False
                    previous_page_mtime = stamp
                elif cache_only:
                    break
                else:
                    cached_prefix = False
                if not cached_prefix:
                    # A fresh cursor prefix invalidates the old downstream page cache.
                    before_query = getattr(cli, "query_count", 0)
                    try:
                        raw = cli.call(command)
                    finally:
                        if getattr(cli, "query_count", 0) > before_query:
                            snapshot["activity_resume_after"] = activity_key
                    if not isinstance(raw, dict) or not isinstance(raw.get("activities"), list):
                        raise ValueError("invalid_activity_response")
                    raw = {**raw, "_profitability_request": {"chain": row["chain"], "wallet": row["address"], "cursor": cursor, "page": page}}
                    write(path, raw)
                    stamp = cache_mtime(path)
                if not isinstance(raw, dict) or not isinstance(raw.get("activities"), list) or stamp is None:
                    raise ValueError("invalid_activity_evidence")
            except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                if "rate limited" in str(exc) or str(exc) == QUERY_BUDGET_EXHAUSTED:
                    blocked = True
                snapshot["errors"].append({"wallet":row["address"],"error":str(exc)})
                break
            pages_read += 1
            activity_mtimes.append(stamp)
            activities.extend(raw.get("activities") or [])
            next_cursor = raw.get("next")
            if not next_cursor:
                complete = True
                break
            if next_cursor == cursor:
                break
            cursor = next_cursor
        if not pages_read:
            continue
        evidence = activity_summary(activities, complete)
        checked_activity += 1
        row["activity"] = evidence
        stamps = [stats_mtimes.get((row["chain"], row["address"], period)) for period in ("7d", "30d")]
        if all(stamp is not None for stamp in stamps):
            row["evidence_checked_at"] = datetime.fromtimestamp(min([*stamps, *activity_mtimes]), timezone.utc).isoformat()
        concerns = activity_gate(evidence)
        row["activity_concerns"] = concerns
        daily = ((row["stats_7d"].get("buy_count") or 0) + (row["stats_7d"].get("sell_count") or 0)) / 7
        row["daily_trade_count_7d"] = daily
        row["usage"] = "signal_only_high_frequency" if daily > 100 or (evidence["median_trade_gap_seconds"] or 0) < 30 else "monitor_entries_and_exits"
        row["status"] = "profit_history_supported" if not concerns else "positive_stats_watch_only"
        print(json.dumps({"activity_checked":row["address"],"status":row["status"],"tokens":evidence["observed_token_count"]}),flush=True)
    snapshot["screened_count"] = sum(stats_complete(r["stats_30d"], r["stats_7d"]) for r in snapshot["wallets"])
    snapshot["activity_checked_count"] = checked_activity
    snapshot["partial"] = snapshot["screened_count"] < len(candidates) or checked_activity < sum(not r["gate_reasons"] for r in snapshot["wallets"])
    snapshot["supported_wallets"] = [r for r in snapshot["wallets"] if r["status"] == "profit_history_supported"]
    snapshot["positive_stats_count"] = sum(not r["gate_reasons"] for r in snapshot["wallets"])
    snapshot["query_count"] = getattr(cli, "query_count", 0)
    snapshot["query_budget_exhausted"] = bool(getattr(cli, "max_queries", 0) and snapshot["query_count"] >= cli.max_queries)
    snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
    write(args.out_dir / "gmgn-profitable-wallets.json", snapshot)
    lines = ["# GMGN 盈利钱包核验", "", "统计来源为 GMGN。已实现利润按平台口径展示；尚未独立核对全部成本、税费和完整交易账本。", "",
             f"候选 {len(candidates)} 个，完成双窗口统计 {snapshot['screened_count']} 个；7/30天统计通过 {snapshot['positive_stats_count']} 个；近期流水抽查 {checked_activity} 个；支持进入观察池 {len(snapshot['supported_wallets'])} 个。", "",
             "| 链 | 钱包 | 30天已实现利润 USD | 7天已实现利润 USD | 30天胜率 | 抽查币数 | 结果 |", "|---|---|---:|---:|---:|---:|---|"]
    for r in sorted(snapshot["wallets"],key=lambda r: -(r["stats_30d"].get("realized_profit_usd") or 0)):
        if r["gate_reasons"]:
            continue
        m,w=r["stats_30d"],r["stats_7d"]
        win=f"{m['winrate']:.1%}" if m.get("winrate") is not None else "未知"
        url=f"https://gmgn.ai/{r['chain']}/address/{r['address']}"
        lines.append(f"| {r['chain']} | [{r['address']}]({url}) | {m['realized_profit_usd']:.2f} | {w['realized_profit_usd']:.2f} | {win} | {(r.get('activity') or {}).get('observed_token_count','未查')} | {r['status']} |")
    lines += ["", "## 判定边界", "", "- 7天与30天是重叠窗口，不代表两组独立样本。", "- 活跃钱包标签只用于发现候选，不能替代历史盈亏。", "- 近期流水为有限分页样本，不能作为完整月度胜率或净利润。", "- 高交易频率的钱包仅作发现/资金流参考，不据此推算延迟跟买收益。", "- 未修改旧 Top50 或实盘配置，未调用私钥、持仓签名接口、交易接口或推特采集。"]
    (args.out_dir / "gmgn-profitable-wallets.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return snapshot


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir",type=Path,default=OUT)
    parser.add_argument("--max-wallets",type=int,default=100)
    parser.add_argument("--activity-wallets",type=int,default=16)
    parser.add_argument("--activity-pages",type=int,default=3)
    parser.add_argument("--request-interval",type=float,default=5)
    parser.add_argument("--max-queries",type=int,default=0,help="Maximum data requests per run; 0 is unlimited; config checks are excluded")
    parser.add_argument("--stats-cache-hours",type=float,default=1,help="Reuse stats cache for this many hours; evidence timestamps remain file mtimes")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--expand-candidates",action="store_true",help="Append local discovery candidates on resume, up to --max-wallets; no extra discovery queries")
    parser.add_argument("--cache-only",action="store_true")
    parser.add_argument("--activity-only",action="store_true")
    result=run(parser.parse_args())
    print(json.dumps({k:result[k] for k in ("screened_count","activity_checked_count","positive_stats_count")}),flush=True)
