"""Opt-in GMGN execution with a durable, account-bound fill ledger.

The radar supplies candidates, never orders. GMGN receipts alone change holdings.
An ambiguous submission remains reserved until its provider order is reconciled.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from alpha_bsc_execution_policy import ExecutionConfig, entry_decision, exit_decision

ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = Path.home() / ".config" / "alpha-radar" / "gmgn-live"
NATIVE = "0x" + "0" * 40
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
ORDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
PENDING = {"submitting", "pending", "unknown", "reconcile_required"}
CN = timezone(timedelta(hours=8))
FEE_RESERVE = Decimal("0.50")


class ExecutionError(ValueError):
    pass


def decimal(value: Any, *, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ExecutionError("invalid_decimal") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ExecutionError("invalid_decimal")
    return result


def atoms(value: Any, *, positive=True) -> int:
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,78}", text):
        raise ExecutionError("invalid_atomic_amount")
    amount = int(text)
    if amount >= 2**256 or (positive and amount == 0):
        raise ExecutionError("invalid_atomic_amount")
    return amount


def address(value: Any) -> str:
    text = str(value or "")
    if not ADDRESS.fullmatch(text):
        raise ExecutionError("invalid_address")
    return text.lower()


def stamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except ValueError:
        raise ExecutionError("invalid_timestamp") from None


@contextmanager
def account_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            raise ExecutionError("account_worker_already_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class GmgnLiveWorker:
    def __init__(self, input_path: Path, transport: Any, *, state_root: Path = STATE_ROOT,
                 enabled: bool = False, slippage_percent: str = "5", clock=None, provider: str = "gmgn"):
        self.input_path = Path(input_path)
        self.transport = transport
        self.wallet = address(transport.wallet_address)
        self.directory = Path(state_root) / self.wallet
        self.directory.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        if provider not in {"gmgn", "okx"}:
            raise ExecutionError("invalid_execution_provider")
        self.provider = provider
        self.slippage = decimal(slippage_percent, positive=True)
        if self.slippage > 10 or self.slippage != int(self.slippage):
            raise ExecutionError("slippage_must_be_integer_1_to_10")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.config = ExecutionConfig.paper()
        self.issues: list[dict] = []

    def _issue(self, code: str, token: str = ""):
        self.issues.append({"reason": code, "token": token})

    def _save(self):
        with self.db:
            self.db.execute("UPDATE ledger SET data=? WHERE id=1", (json.dumps(self.state, allow_nan=False),))

    def _open(self):
        self.db = sqlite3.connect(self.directory / "ledger.sqlite3", timeout=0)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        row = self.db.execute("SELECT data FROM ledger WHERE id=1").fetchone()
        if row is None:
            self.state = {"version": 1, "wallet": self.wallet, "provider": self.provider, "orders": {}, "positions": {},
                          "cash_usd_estimate": "100", "realized_pnl_usd_estimate": "0", "daily_losses": {}}
            with self.db:
                self.db.execute("INSERT INTO ledger VALUES(1,?)", (json.dumps(self.state),))
        else:
            self.state = json.loads(row[0])
        if self.state.get("version") != 1 or self.state.get("wallet") != self.wallet:
            raise ExecutionError("ledger_identity_mismatch")
        if not all(isinstance(self.state.get(k), dict) for k in ("orders", "positions", "daily_losses")):
            raise ExecutionError("invalid_ledger")
        # Legacy populated ledgers belong to GMGN. Never reinterpret their order IDs as chain hashes.
        legacy_empty = (not self.state["orders"] and not self.state["positions"] and
                        not self.state["daily_losses"] and self.state.get("cash_usd_estimate") == "100")
        bound_provider = self.state.get("provider", self.provider if legacy_empty else "gmgn")
        if bound_provider != self.provider:
            raise ExecutionError("ledger_provider_mismatch")
        if "provider" not in self.state:
            self.state["provider"] = self.provider
            self._save()
        decimal(self.state["cash_usd_estimate"])
        for key, position in self.state["positions"].items():
            if address(key) != key or position["token"] != key:
                raise ExecutionError("invalid_position_identity")
            if atoms(position["remaining_atomic"], positive=False) > atoms(position["original_atomic"]):
                raise ExecutionError("invalid_position_quantity")
            if position["buy_order"] not in self.state["orders"]:
                raise ExecutionError("missing_entry_order")

    def _report(self, status: str) -> dict:
        report = {"status": status, "enabled": self.enabled, "wallet": self.wallet, "provider": self.provider,
                  "updated_at": self.clock().isoformat(), "issues": self.issues,
                  "open_positions": len(self.state["positions"]),
                  "pending_orders": sum(o["status"] in PENDING for o in self.state["orders"].values()),
                  "cash_usd_estimate": self.state["cash_usd_estimate"],
                  "realized_pnl_usd_estimate": self.state["realized_pnl_usd_estimate"],
                  "unreported_provider_fees": True,
                  "orders": list(self.state["orders"].values()), "positions": list(self.state["positions"].values())}
        target = self.directory / "status.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, target)
        return report

    def _quote(self, token: str, side: str, amount: int) -> dict:
        started = self.clock()
        q = self.transport.quote(token, side, str(amount), str(self.slippage))
        if not isinstance(q, dict) or q.get("status") in {"error", "unknown", "failed"}:
            raise ExecutionError("quote_unavailable")
        if (self.clock() - started).total_seconds() > 10:
            raise ExecutionError("quote_request_too_slow")
        expected = (NATIVE, token) if side == "buy" else (token, NATIVE)
        if (address(q.get("input_token")), address(q.get("output_token"))) != expected:
            raise ExecutionError("quote_identity_mismatch")
        if atoms(q["input_amount"]) != amount:
            raise ExecutionError("quote_amount_mismatch")
        output, minimum = atoms(q["output_amount"]), atoms(q["min_output_amount"])
        if minimum > output or minimum < int(Decimal(output) * (1 - self.slippage / 100)):
            raise ExecutionError("quote_slippage_exceeded")
        return {**q, "received_at": self.clock().isoformat(), "deadline": time.monotonic() + 10}

    def _balance(self, token: str) -> int:
        result = self.transport.balance(token)
        if address(result.get("token", token)) != token:
            raise ExecutionError("balance_identity_mismatch")
        return atoms(result["amount_atomic"], positive=False)

    def _submit(self, token: str, side: str, amount: int, row: dict, q: dict,
                native_price: Decimal, reason: str):
        if not self.enabled:
            raise ExecutionError("live_disabled")
        if (self.clock() - stamp(q["received_at"])).total_seconds() > 10:
            raise ExecutionError("quote_expired_before_submit")
        if side == "buy":
            # One first-discovery attempt per token, independent of changing quote/pool timestamps.
            key = hashlib.sha256(f"{self.wallet}:bsc:{token}:buy".encode()).hexdigest()
        else:
            pos = self.state["positions"][token]
            previous = [o for o in self.state["orders"].values() if o["token"] == token and o["side"] == "sell"]
            failures = [o for o in previous if o["status"] in {"failed", "rejected"}]
            if failures and (self.clock() - stamp(failures[-1]["created_at"])).total_seconds() < 60:
                self._issue("exit_retry_cooldown", token)
                return
            # Definitive pre-send rejections spend no gas and may recover later.
            if sum(o["status"] == "failed" for o in failures) >= 3:
                self._issue("exit_retry_limit_requires_attention", token)
                return
            key = hashlib.sha256(f"{pos['buy_order']}:{pos['remaining_atomic']}:{reason}:{len(previous)}".encode()).hexdigest()
        if key in self.state["orders"]:
            return
        order = {"key": key, "side": side, "token": token, "amount_atomic": str(amount),
                 "minimum_output_atomic": str(q["min_output_amount"]), "status": "submitting",
                 "created_at": self.clock().isoformat(), "reason": reason, "native_price_usd": str(native_price),
                 "reserved_usd": str(Decimal(amount) / 10**18 * native_price + FEE_RESERVE) if side == "buy" else "0",
                 "pool_address": address(row.get("pool_address") or row.get("pair_address")),
                 "execution_arm": row.get("execution_arm", ""), "symbol": str(row.get("symbol", ""))[:40]}
        self.state["orders"][key] = order
        self._save()  # Persist BEFORE the external side effect; a crash now must never resubmit.
        try:
            deadline = q["deadline"]
            if side == "buy":
                signal_remaining = self.config.max_signal_age_seconds - (self.clock() - stamp(row["signal_at"])).total_seconds()
                source_remaining = self.config.max_quote_age_seconds - (self.clock() - stamp(row.get("quote_at") or row.get("quote_observed_at"))).total_seconds()
                first_remaining = self.config.max_first_observation_minutes * 60 - (self.clock() - stamp(row["first_seen_at"])).total_seconds()
                deadline = min(deadline, time.monotonic() + min(signal_remaining, source_remaining, first_remaining))
            options = {"deadline": deadline}
            if getattr(self.transport, "durable_prepare", False):
                def prepared(tx_hash):
                    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(tx_hash)):
                        raise ExecutionError("invalid_prepared_hash")
                    if order.get("order_id") and order["order_id"] != tx_hash.lower():
                        raise ExecutionError("prepared_hash_changed")
                    order.update(order_id=tx_hash.lower(), status="pending")
                    self._save()
                options["on_prepared"] = prepared
            result = self.transport.submit(token, side, str(amount), str(self.slippage), **options)
        except Exception as error:
            provider_id = getattr(error, "order_id", None)
            if order.get("order_id"):
                order["status"] = "pending"
            elif provider_id and ORDER_ID.fullmatch(str(provider_id)):
                order.update(order_id=str(provider_id), status="pending")
            elif getattr(error, "ambiguous", True) is False:
                order["status"] = "rejected"
            else:
                order["status"] = "unknown"
            self._save()
            self._issue("submission_ambiguous_do_not_retry" if order["status"] != "rejected" else "submission_rejected_before_send", token)
            return
        provider_id = result.get("order_id") if isinstance(result, dict) else None
        if order.get("order_id") and str(provider_id).lower() != order["order_id"]:
            order["status"] = "reconcile_required"
            self._issue("prepared_hash_response_mismatch", token)
        elif provider_id and ORDER_ID.fullmatch(str(provider_id)):
            order["order_id"] = str(provider_id)
            order["status"] = "pending"
        else:
            # Even an HTTP error may arrive after the provider accepted the swap.
            order["status"] = "unknown"
            self._issue("missing_order_id_reconcile_required", token)
        self._save()

    def _apply_receipt(self, order: dict, receipt: dict):
        token = order["token"]
        if str(receipt.get("order_id")) != order["order_id"]:
            raise ExecutionError("receipt_order_mismatch")
        if receipt.get("wallet_address") and address(receipt["wallet_address"]) != self.wallet:
            raise ExecutionError("receipt_wallet_mismatch")
        if receipt.get("chain") and receipt["chain"] != "bsc":
            raise ExecutionError("receipt_chain_mismatch")
        if receipt.get("status") == "rejected" and receipt.get("broadcast_attempted") is False:
            if (address(receipt.get("wallet_address")) != self.wallet or receipt.get("chain") != "bsc"
                    or decimal(receipt.get("gas_usd", "0")) != 0):
                raise ExecutionError("invalid_presend_cancellation")
            order.update(status="rejected", cancellation_reason="cancelled_before_broadcast")
            return
        if receipt.get("status") == "failed":
            gas = decimal(receipt["gas_usd"]) if receipt.get("gas_usd") is not None else FEE_RESERVE
            order["status"] = "failed"
            order["failure_fee_reserve_usd"] = str(gas)
            self._loss(gas)
            self.state["cash_usd_estimate"] = str(max(Decimal(0), decimal(self.state["cash_usd_estimate"]) - gas))
            return
        if receipt.get("status") != "confirmed":
            return
        tx = str(receipt.get("tx_hash") or "").lower()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx):
            raise ExecutionError("missing_receipt_transaction")
        if any(o.get("tx_hash") == tx for o in self.state["orders"].values() if o is not order):
            raise ExecutionError("duplicate_receipt_transaction")
        expected = (NATIVE, token) if order["side"] == "buy" else (token, NATIVE)
        if (address(receipt["input_token"]), address(receipt["output_token"])) != expected:
            raise ExecutionError("receipt_token_mismatch")
        used, received = atoms(receipt["input_amount"]), atoms(receipt["output_amount"])
        if used > atoms(order["amount_atomic"]):
            raise ExecutionError("receipt_overspend")
        input_decimals, output_decimals = receipt["input_decimals"], receipt["output_decimals"]
        if any(type(n) is not int or not 0 <= n <= 36 for n in (input_decimals, output_decimals)):
            raise ExecutionError("receipt_decimals_missing")
        if (input_decimals if order["side"] == "buy" else output_decimals) != 18:
            raise ExecutionError("native_decimals_mismatch")
        # USD values are estimates marked separately; atomic holdings come ONLY from the receipt.
        native_price = decimal(order["native_price_usd"], positive=True)
        gas = decimal(receipt["gas_usd"]) if receipt.get("gas_usd") is not None else FEE_RESERVE
        now = self.clock().isoformat()
        if order["side"] == "buy":
            cost = Decimal(used) / 10**18 * native_price + gas
            fill_price = Decimal(used) / 10**18 * native_price / (Decimal(received) / 10**output_decimals)
            if token in self.state["positions"]:
                raise ExecutionError("duplicate_fill_position")
            self.state["positions"][token] = {
                "token": token, "chain": "bsc", "contract_address": token, "pool_address": order["pool_address"],
                "symbol": order["symbol"], "execution_arm": order["execution_arm"], "buy_order": order["key"],
                "entry_at": order["created_at"], "entry_price_usd": str(fill_price), "high_price_usd": str(fill_price),
                "original_atomic": str(received), "remaining_atomic": str(received), "decimals": output_decimals,
                "remaining_cost_usd": str(cost), "cost_native_atomic": str(used), "tp1_hit": False}
            self.state["cash_usd_estimate"] = str(max(Decimal(0), decimal(self.state["cash_usd_estimate"]) - cost))
        else:
            position = self.state["positions"][token]
            remaining = atoms(position["remaining_atomic"])
            if used > remaining or input_decimals != position["decimals"]:
                raise ExecutionError("receipt_exceeds_position")
            allocated = decimal(position["remaining_cost_usd"]) * Decimal(used) / remaining
            proceeds = Decimal(received) / 10**18 * native_price - gas
            pnl = proceeds - allocated
            position["remaining_atomic"] = str(remaining - used)
            position["remaining_cost_usd"] = str(decimal(position["remaining_cost_usd"]) - allocated)
            if order["reason"] == "take_profit_1":
                sold = atoms(position["original_atomic"]) - (remaining - used)
                position["tp1_hit"] = sold >= atoms(position["original_atomic"]) * 80 // 100
            if remaining == used:
                del self.state["positions"][token]
            self.state["cash_usd_estimate"] = str(max(Decimal(0), decimal(self.state["cash_usd_estimate"]) + proceeds))
            self.state["realized_pnl_usd_estimate"] = str(Decimal(self.state["realized_pnl_usd_estimate"]) + pnl)
            if pnl < 0:
                self._loss(-pnl)
            order["realized_pnl_usd_estimate"] = str(pnl)
        order.update(status="filled", filled_at=now, tx_hash=tx, input_amount=str(used), output_amount=str(received),
                     gas_usd=str(gas), gas_estimated=receipt.get("gas_usd") is None)
        if received < atoms(order["minimum_output_atomic"]):
            # Record actual balance even if the provider violated the minimum; pause new buys.
            order["fill_anomaly"] = "below_minimum_output"

    def _loss(self, amount: Decimal):
        day = self.clock().astimezone(CN).date().isoformat()
        self.state["daily_losses"][day] = str(decimal(self.state["daily_losses"].get(day, "0")) + amount)

    def _sync_provider_fees(self):
        if not hasattr(self.transport, "fees"):
            return True
        before = copy.deepcopy(self.state)
        try:
            events = self.transport.fees()
            if not isinstance(events, list):
                raise ExecutionError("invalid_fee_events")
            recorded = self.state.setdefault("provider_fees", {})
            for event in events:
                key, fee = str(event["id"]), decimal(event["gas_usd"])
                if not ORDER_ID.fullmatch(key):
                    raise ExecutionError("invalid_fee_identity")
                if key in recorded:
                    if decimal(recorded[key]) != fee:
                        raise ExecutionError("provider_fee_changed")
                    continue
                recorded[key] = str(fee)
                self._loss(fee)
                self.state["cash_usd_estimate"] = str(max(Decimal(0), decimal(self.state["cash_usd_estimate"]) - fee))
                self.state["realized_pnl_usd_estimate"] = str(Decimal(self.state["realized_pnl_usd_estimate"]) - fee)
            self._save()
            return True
        except Exception:
            self.state = before
            self._issue("provider_fee_reconciliation_failed")
            return False

    def _reconcile(self):
        for key in list(self.state["orders"]):
            order = self.state["orders"][key]
            if order["status"] not in PENDING:
                continue
            if not order.get("order_id"):
                order["status"] = "unknown"
                self._issue("submission_needs_order_id", order["token"])
                continue
            before = copy.deepcopy(self.state)
            try:
                receipt = self.transport.order(order["order_id"])
                self._apply_receipt(order, receipt)
            except Exception:
                self.state = before
                order = self.state["orders"][key]
                order["status"] = "reconcile_required"
                self._issue("receipt_unavailable_or_invalid", order["token"])
            self._save()

    def _market(self) -> Decimal:
        started = self.clock()
        price = decimal(self.transport.market()["native_price_usd"], positive=True)
        if (self.clock() - started).total_seconds() > 10:
            raise ExecutionError("native_price_stale")
        return price

    def _exits(self, native_price: Decimal):
        for token, position in list(self.state["positions"].items()):
            if any(o["token"] == token and o["status"] in PENDING for o in self.state["orders"].values()):
                continue
            try:
                remaining = atoms(position["remaining_atomic"])
                if self._balance(token) < remaining:
                    self._issue("wallet_position_mismatch", token)
                    continue
                native_price = self._market()
                if hasattr(self.transport, "set_context"):
                    self.transport.set_context(token, position["pool_address"])
                q = self._quote(token, "sell", remaining)
                native_price = decimal(q.get("native_price_usd") or native_price, positive=True)
                price = Decimal(atoms(q["output_amount"])) / 10**18 * native_price / (Decimal(remaining) / 10**position["decimals"])
                position["high_price_usd"] = str(max(price, decimal(position["high_price_usd"])))
                position["remaining_fraction"] = float(Decimal(remaining) / atoms(position["original_atomic"]))
                quote = {"chain": "bsc", "contract_address": token, "pool_address": position["pool_address"],
                         "price_usd": str(price), "quote_status": "fresh", "quote_at": self.clock().isoformat()}
                decision = exit_decision(position, quote, self.clock(), self.config)
                if not decision.exit:
                    continue
                if decision.reason == "take_profit_1":
                    original = atoms(position["original_atomic"])
                    amount = min(remaining, max(0, original * 80 // 100 - (original - remaining)))
                    if amount == 0:
                        position["tp1_hit"] = True
                        continue
                    if amount != remaining:
                        q = self._quote(token, "sell", amount)
                else:
                    amount = remaining
                if hasattr(self.transport, "ensure_allowance"):
                    allowance = self.transport.ensure_allowance(token, str(amount))
                    if not isinstance(allowance, dict) or allowance.get("ready") is not True:
                        self._issue("allowance_pending", token)
                        continue
                    q = self._quote(token, "sell", amount)
                self._submit(token, "sell", amount, position, q, native_price, decision.reason)
            except Exception:
                self._issue("exit_check_failed", token)
        self._save()

    def _entries(self, native_price: Decimal):
        if (self.directory / "PAUSE_ENTRIES").exists():
            self._issue("entries_paused")
            return
        if any(o["status"] in {"unknown", "submitting", "reconcile_required"} or o.get("fill_anomaly")
               for o in self.state["orders"].values()):
            self._issue("entries_paused_for_reconciliation")
            return
        try:
            payload = json.loads(self.input_path.read_text(encoding="utf-8-sig"))
            age = (self.clock() - stamp(payload["updated_at"])).total_seconds()
            if not 0 <= age <= 30:
                raise ExecutionError("stale_input")
            signals, quotes = payload["signals"], payload["quotes"]
            if not isinstance(signals, list) or not isinstance(quotes, list):
                raise ExecutionError("invalid_input")
        except Exception:
            self._issue("input_missing_invalid_or_stale")
            return
        for raw in signals:
            token = ""
            try:
                if hasattr(self.transport, "ready_for_entries") and self.transport.ready_for_entries() is not True:
                    self._issue("provider_busy_or_unverified")
                    break
                token = address(raw.get("contract_address") or raw.get("token_address") or raw.get("address"))
                if token == NATIVE or any(o["token"] == token for o in self.state["orders"].values()):
                    continue
                pool = address(raw.get("pool_address") or raw.get("pair_address"))
                matches = [q for q in quotes if q.get("chain") == "bsc" and
                           str(q.get("contract_address") or q.get("token_address") or q.get("address")).lower() == token and
                           str(q.get("pool_address") or q.get("pair_address")).lower() == pool]
                if len(matches) != 1:
                    self._issue("missing_unique_pool_quote", token)
                    continue
                q = matches[0]
                # Only market fields may override a candidate's strategy evidence.
                row = {**raw, **{k: q[k] for k in ("price_usd", "price", "liquidity_usd", "liquidity", "quote_status", "quote_at", "quote_observed_at") if k in q},
                       "contract_address": token, "pool_address": pool}
                pending = [o for o in self.state["orders"].values() if o["side"] == "buy" and o["status"] in PENDING]
                positions = self.state["positions"]
                reserved = sum((decimal(o["reserved_usd"]) for o in pending), Decimal(0))
                exposure = sum((min(Decimal(5), decimal(p["remaining_cost_usd"])) for p in positions.values()), Decimal(0))
                day = self.clock().astimezone(CN).date().isoformat()
                row.update(open_positions=len(positions) + len(pending), open_count=len(positions) + len(pending), current_exposure_usd=float(exposure + len(pending) * Decimal(5)),
                           daily_loss_usd=float(decimal(self.state["daily_losses"].get(day, "0"))),
                           existing_token_keys=[f"bsc:{t}" for t in positions])
                decision = entry_decision(row, self.clock(), self.config)
                if not decision.accepted:
                    self._issue(decision.reason, token)
                    continue
                if decimal(self.state["cash_usd_estimate"]) - reserved < Decimal(5) + FEE_RESERVE:
                    self._issue("capital_limit", token)
                    continue
                if self.transport.security(token).get("safe") is not True:
                    self._issue("security_not_passed", token)
                    continue
                # Reprice after security/balance calls, so each order is bounded in USD, not fixed BNB.
                balance = self._balance(NATIVE)
                native_price = self._market()
                amount = int(Decimal(5) / native_price * 10**18)
                gas_reserve = int(FEE_RESERVE / native_price * 10**18)
                if amount <= 0 or balance < amount + gas_reserve:
                    self._issue("insufficient_bnb_and_gas", token)
                    continue
                if hasattr(self.transport, "set_context"):
                    self.transport.set_context(token, pool)
                route = self._quote(token, "buy", amount)
                if not entry_decision(row, self.clock(), self.config).accepted:
                    self._issue("signal_expired_during_preflight", token)
                    continue
                self._submit(token, "buy", amount, row, route, native_price, decision.reason)
                if any(o["status"] in {"unknown", "submitting", "reconcile_required"} for o in self.state["orders"].values()):
                    break
            except Exception:
                self._issue("entry_check_failed", token)

    def run_once(self) -> dict:
        self.issues = []
        with account_lock(self.directory / "worker.lock"), localcontext() as ctx:
            ctx.prec = 90
            try:
                self._open()
                if not self.enabled:
                    return self._report("disabled")
                self._reconcile()
                fees_ready = self._sync_provider_fees()
                try:
                    native_price = self._market()
                except Exception:
                    self._issue("native_price_unavailable")
                    return self._report("degraded")
                self._exits(native_price)
                if self._sync_provider_fees() and fees_ready:
                    self._entries(native_price)
                self._save()
                return self._report("attention" if self.issues else "running")
            finally:
                if hasattr(self, "db"):
                    self.db.close()

    def attach_order(self, intent_key: str, provider_id: str) -> dict:
        """Operator recovery after an ambiguous submit. Queries only; never submits."""
        if not ORDER_ID.fullmatch(provider_id):
            raise ExecutionError("invalid_order_id")
        with account_lock(self.directory / "worker.lock"), localcontext() as ctx:
            ctx.prec = 90
            try:
                self._open()
                order = self.state["orders"].get(intent_key)
                if not order or order["status"] not in PENDING or order.get("order_id"):
                    raise ExecutionError("intent_not_attachable")
                if any(o.get("order_id") == provider_id for o in self.state["orders"].values()):
                    raise ExecutionError("provider_order_already_attached")
                receipt = self.transport.order(provider_id)
                # Require wallet evidence, not just a matching token ticker or amount.
                wallet = receipt.get("wallet_address")
                if not wallet and receipt.get("tx_hash") and hasattr(self.transport, "transaction_sender"):
                    wallet = self.transport.transaction_sender(receipt["tx_hash"])
                if address(wallet) != self.wallet:
                    raise ExecutionError("recovery_wallet_unverified")
                executed_at = stamp(receipt.get("created_at"))
                if not stamp(order["created_at"]) - timedelta(seconds=2) <= executed_at <= self.clock():
                    raise ExecutionError("recovery_execution_time_mismatch")
                expected = (NATIVE, order["token"]) if order["side"] == "buy" else (order["token"], NATIVE)
                if receipt.get("status") == "failed" and not receipt.get("input_token"):
                    raise ExecutionError("failed_recovery_requires_provider_request_evidence")
                if (address(receipt.get("input_token")), address(receipt.get("output_token"))) != expected:
                    raise ExecutionError("recovery_token_mismatch")
                if atoms(receipt["input_amount"]) > atoms(order["amount_atomic"]):
                    raise ExecutionError("recovery_amount_mismatch")
                before = copy.deepcopy(self.state)
                try:
                    order.update(order_id=provider_id, status="pending")
                    self._apply_receipt(order, receipt)
                except Exception:
                    self.state = before
                    raise
                self._save()
                return self._report("reconciled_without_submission")
            finally:
                if hasattr(self, "db"):
                    self.db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=ROOT / "outputs" / "bsc-execution-input.json")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--attach-order")
    parser.add_argument("--intent-key")
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args()
    if not 2 <= args.interval <= 60:
        parser.error("interval must be 2..60 seconds")
    if bool(args.attach_order) != bool(args.intent_key) or (args.attach_order and args.live):
        parser.error("recovery requires --attach-order and --intent-key without --live")
    from alpha_gmgn_live_transport import GmgnLiveTransport
    enabled = False
    try:
        transport = GmgnLiveTransport()
        if hasattr(transport, "preflight"):
            transport.preflight()
        enabled = args.live and os.environ.get("GMGN_LIVE_ENABLED") == "1" and os.environ.get("GMGN_ALLOW_AUTOMATED_TRADES") == "1"
        if args.live and not enabled:
            raise ExecutionError("explicit_live_environment_required")
        worker = GmgnLiveWorker(args.input, transport, enabled=enabled,
                               slippage_percent=os.environ.get("GMGN_SLIPPAGE_PERCENT", "5"))
        if args.check:
            print(json.dumps({"configured": True, "live_started": False, "wallet": worker.wallet,
                              "ledger": str(worker.directory), "provider_permissions_verified": False}))
            return 0
        if args.attach_order:
            print(json.dumps(worker.attach_order(args.intent_key, args.attach_order)))
            return 0
        while True:
            result = worker.run_once()
            print(json.dumps({k: v for k, v in result.items() if k not in {"orders", "positions"}}), flush=True)
            if args.once or not enabled:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, ExecutionError) else getattr(error, "code", "configuration_or_ledger_error")
        print(json.dumps({"status": "stopped", "reason": reason, "live_started": enabled,
                          "check_pending_orders": enabled}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
