from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .execution import transaction_fees
from .rules import AccountPermissions, OrderRequest, RuleBook, Side
from .securities import Board, Exchange, Security, SecurityType, TradingStatus
from .sessions import session_state


def validate_order_plan_account_version(
    plan: dict[str, Any], account: dict[str, Any] | None
) -> None:
    if account is None:
        raise ValueError("当前账户账本不可用，不能推进订单状态")
    if str(plan.get("account_version")) != str(account.get("version")):
        raise ValueError("订单计划使用的账户版本已失效，请重新运行完整预测")


class OrderPlanBuilder:
    def __init__(
        self,
        commission_fixed_per_order: Decimal = Decimal("1"),
        stock_sell_stamp_duty_rate: Decimal = Decimal("0.0005"),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.commission_fixed_per_order = commission_fixed_per_order
        self.stock_sell_stamp_duty_rate = stock_sell_stamp_duty_rate
        self.clock = clock or (lambda: datetime.now().astimezone())

    def build(
        self,
        run_id: str,
        snapshot: dict[str, Any],
        risk_output: dict[str, Any],
    ) -> dict[str, Any]:
        account = snapshot.get("account", {})
        generated_at = self.clock()
        if risk_output.get("decision") in {"defer", "veto"}:
            return {
                "plan_id": f"plan-{uuid.uuid4().hex}",
                "run_id": run_id,
                "account_version": account.get("version", "unconfigured"),
                "manual_execution_only": True,
                "generated_at": generated_at.isoformat(timespec="seconds"),
                "as_of_time": snapshot["as_of_time"],
                "daily_bar_cutoff": snapshot.get("feature_coverage", {}).get(
                    "daily_bar_cutoff", snapshot["as_of_time"][:10]
                ),
                "status": "no_action",
                "orders": [],
                "blockers": ["独立风控暂缓或否决，本次不得生成交易"],
                "risk_decision": risk_output.get("decision"),
            }
        budget = Decimal(str(account.get("capital_budget") or 0))
        cash = account.get("cash")
        positions = {
            str(item["symbol"]): item for item in account.get("positions", [])
        }
        securities = {item["symbol"]: item for item in snapshot.get("securities", [])}
        quotes = snapshot.get("quotes", {})
        approved = {
            item["symbol"]: item for item in risk_output.get("approved_positions", [])
        }
        rows = []
        for symbol in sorted(set(positions) | set(approved)):
            security_data = securities.get(symbol)
            quote = quotes.get(symbol, {})
            price = quote.get("last")
            if security_data is None or price is None or float(price) <= 0:
                rows.append(
                    {
                        "order_id": f"order-{uuid.uuid4().hex}",
                        "symbol": symbol,
                        "status": "blocked",
                        "side": None,
                        "quantity": 0,
                        "findings": ["缺少证券主数据或有效价格"],
                        "security_type": (security_data or {}).get("security_type"),
                        "price_limit_ratio": (security_data or {}).get("price_limit_ratio"),
                    }
                )
                continue
            security = _security(security_data)
            target = approved.get(symbol)
            target_weight = Decimal(str((target or {}).get("target_weight") or 0))
            current = int((positions.get(symbol) or {}).get("quantity") or 0)
            decimal_price = Decimal(str(price))
            target_value = budget * target_weight if budget > 0 else Decimal("0")
            lot_value = decimal_price * security.buy_lot_size
            target_quantity = int(target_value // lot_value) * security.buy_lot_size
            # Weights passed through JSON floats can lose a few trailing digits.
            # Correct only sub-cent serialization noise, never a real budget shortfall.
            next_lot_value = decimal_price * (target_quantity + security.buy_lot_size)
            if (0 < next_lot_value - target_value <= Decimal("0.00000001")
                    and next_lot_value <= budget):
                target_quantity += security.buy_lot_size
            if ((target or {}).get("action") == "hold"
                    and abs(target_value - decimal_price * current) <= Decimal("0.00000001")):
                target_quantity = current
            delta = target_quantity - current
            if delta == 0:
                continue
            side = Side.BUY if delta > 0 else Side.SELL
            quantity = abs(delta)
            target_limit = (
                (target or {}).get("price_max")
                if side is Side.BUY
                else (target or {}).get("price_min")
            )
            limit_price = Decimal(str(target_limit or price)).quantize(security.price_tick)
            if side is Side.BUY and limit_price > 0:
                remaining_target_value = max(target_value - current * limit_price, Decimal("0"))
                allowed_quantity = int(remaining_target_value // (limit_price * security.buy_lot_size)) * security.buy_lot_size
                next_target_value = (current + allowed_quantity + security.buy_lot_size) * limit_price
                if (0 < next_target_value - target_value <= Decimal("0.00000001")
                        and next_target_value <= budget):
                    allowed_quantity += security.buy_lot_size
                # Reprice only the proposed buy; a higher limit cannot force a sale
                # or make the resulting position exceed its approved notional.
                quantity = min(quantity // security.buy_lot_size * security.buy_lot_size, allowed_quantity)
                if quantity == 0:
                    continue
                target_quantity = current + quantity
            available_quantity = int(
                (positions.get(symbol) or {}).get("available_quantity") or 0
            )
            validation = RuleBook().validate(
                OrderRequest(
                    security=security,
                    side=side,
                    quantity=quantity,
                    limit_price=limit_price,
                    trade_date=date.fromisoformat(snapshot["as_of_time"][:10]),
                    available_sell_quantity=available_quantity,
                    available_cash=Decimal(str(cash)) if cash is not None else None,
                    commission_fixed_per_order=self.commission_fixed_per_order,
                    previous_close=(
                        Decimal(str(quote["previous_close"]))
                        if quote.get("previous_close") is not None
                        else None
                    ),
                    reference_price=Decimal(str(price)),
                    continuous_auction=snapshot.get("market", {}).get("session")
                    == "intraday",
                ),
                AccountPermissions(
                    risk_warning=account.get("permissions", {}).get(
                        "risk_warning_stocks", False
                    )
                ),
            )
            findings = [item.message for item in validation.findings]
            execution_blockers = []
            if snapshot.get("market", {}).get("execution_eligible") is not True:
                execution_blockers.append("当前时段或安全数据不允许执行")
            execution_blockers.extend(_publication_blockers(snapshot, quote, generated_at))
            if side is Side.BUY and cash is None:
                execution_blockers.append("券商可用现金尚未确认")
            if _expired((target or {}).get("expires_at"), generated_at.isoformat()):
                execution_blockers.append("策略信号已过期，必须重新研究")
            findings.extend(execution_blockers)
            executable = validation.executable and not execution_blockers
            notional = limit_price * quantity
            fees = transaction_fees(
                security.security_type,
                side,
                notional,
                commission_fixed_per_order=self.commission_fixed_per_order,
                stock_sell_stamp_duty_rate=self.stock_sell_stamp_duty_rate,
            )
            rows.append(
                {
                    "order_id": f"order-{uuid.uuid4().hex}",
                    "symbol": symbol,
                    "name": security.name,
                    "security_type": security.security_type.value,
                    "price_limit_ratio": (
                        str(security.price_limit_ratio)
                        if security.price_limit_ratio is not None else None
                    ),
                    "side": side.value,
                    "quantity": quantity,
                    "limit_price": str(limit_price),
                    "target_quantity": target_quantity,
                    "current_quantity": current,
                    "estimated_notional": str(notional),
                    "estimated_fees": {key: str(value) for key, value in fees.items()},
                    "status": "ready" if executable else "blocked",
                    "rule_version": validation.rule_version,
                    "findings": findings,
                    "expires_at": (target or {}).get("expires_at"),
                    "decision_ids": (target or {}).get("decision_ids", []),
                    "quote_available_at": quote.get("available_at"),
                    "trigger_conditions": ["计划未过期且价格满足限价"],
                    "cancel_conditions": (target or {}).get(
                        "invalidation_conditions", []
                    ),
                    "priority": len(rows) + 1,
                    "depends_on": [],
                }
            )
        if cash is not None:
            remaining_cash = Decimal(str(cash))
            for row in rows:
                if row.get("side") != Side.BUY.value or row["status"] != "ready":
                    continue
                required = Decimal(str(row["estimated_notional"])) + Decimal(
                    str(row["estimated_fees"]["total"])
                )
                if required > remaining_cash:
                    row["status"] = "blocked"
                    row["findings"].append("组合内多笔订单累计可用资金不足")
                else:
                    remaining_cash -= required
        return {
            "plan_id": f"plan-{uuid.uuid4().hex}",
            "run_id": run_id,
            "account_version": account.get("version", "unconfigured"),
            "manual_execution_only": True,
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "as_of_time": snapshot["as_of_time"],
            "daily_bar_cutoff": snapshot.get("feature_coverage", {}).get(
                "daily_bar_cutoff", snapshot["as_of_time"][:10]
            ),
            "status": (
                "ready"
                if rows and all(item["status"] == "ready" for item in rows)
                else "blocked" if rows else "no_action"
            ),
            "orders": rows,
            "cash_remaining_after_ready_buys": (
                str(remaining_cash) if cash is not None else None
            ),
        }


def _publication_blockers(snapshot: dict[str, Any], quote: dict[str, Any], now: datetime) -> list[str]:
    """Recheck execution at publication; long research cannot keep an old quote executable."""
    quality = snapshot.get("data_quality", {})
    blockers = []
    if quality.get("status") != "valid" or quality.get("stale_for_execution"):
        blockers.append("行情仅供研究或执行安全数据未确认")
    try:
        observed = datetime.fromisoformat(str(quote.get("available_at") or quality.get("available_at")))
        snapshot_time = datetime.fromisoformat(snapshot["as_of_time"])
        if observed.tzinfo is None or snapshot_time.tzinfo is None or now.tzinfo is None:
            raise ValueError("missing timezone")
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        same_day = local_now.date() == snapshot_time.astimezone(ZoneInfo("Asia/Shanghai")).date()
        session = session_state(local_now, same_day and snapshot.get("market", {}).get("is_trading_day") is True)
        if not session.execution_eligible:
            blockers.append("发布时已离开快照对应交易日的可执行时段")
        age = (now - observed).total_seconds()
        maximum_age = min(int(quality.get("quote_max_age_seconds") or 300), 300)
        if age < -5 or age > maximum_age:
            blockers.append(f"发布时行情已过期或时间异常（{age:.0f}秒），请刷新行情后重新分析")
    except (TypeError, ValueError, KeyError):
        blockers.append("发布时无法核实行情时间与交易时段")
    return blockers


def _security(item: dict[str, Any]) -> Security:
    return Security(
        symbol=str(item["symbol"]),
        name=str(item["name"]),
        exchange=Exchange(item["exchange"]),
        security_type=SecurityType(item["security_type"]),
        board=Board(item["board"]),
        risk_warning=item.get("risk_warning"),
        trading_status=TradingStatus(item["trading_status"]),
        price_tick=Decimal(str(item["price_tick"])),
        buy_lot_size=int(item.get("buy_lot_size") or 100),
        same_day_turnaround=item.get("same_day_turnaround"),
        price_limit_ratio=(
            Decimal(str(item["price_limit_ratio"]))
            if item.get("price_limit_ratio") is not None
            else None
        ),
        price_limit_exempt=bool(item.get("price_limit_exempt", False)),
        source=str(item.get("source", "unknown")),
        available_at=item.get("available_at"),
    )


def _expired(expires_at: object, as_of_time: object) -> bool:
    if expires_at is None:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at))
        current = datetime.fromisoformat(str(as_of_time))
    except ValueError:
        return True
    if (expiry.tzinfo is None) != (current.tzinfo is None):
        expiry = expiry.replace(tzinfo=None)
        current = current.replace(tzinfo=None)
    return current >= expiry
