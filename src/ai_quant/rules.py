from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum

from .securities import Board, Security, SecurityType, TradingStatus


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class FindingLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class AccountPermissions:
    risk_warning: bool | None = None


@dataclass(frozen=True)
class OrderRequest:
    security: Security
    side: Side
    quantity: int
    limit_price: Decimal
    trade_date: date
    available_sell_quantity: int = 0
    available_cash: Decimal | None = None
    commission_rate: Decimal | None = None
    commission_fixed_per_order: Decimal | None = None
    previous_close: Decimal | None = None
    reference_price: Decimal | None = None
    continuous_auction: bool = False


@dataclass(frozen=True)
class RuleFinding:
    code: str
    level: FindingLevel
    message: str


@dataclass(frozen=True)
class ValidationResult:
    rule_version: str
    findings: tuple[RuleFinding, ...]

    @property
    def executable(self) -> bool:
        return not any(item.level is FindingLevel.ERROR for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_version": self.rule_version,
            "executable": self.executable,
            "findings": [
                {"code": item.code, "level": item.level.value, "message": item.message}
                for item in self.findings
            ],
        }


@dataclass(frozen=True)
class RuleVersion:
    version: str
    effective_from: date
    effective_to: date | None
    risk_warning_price_limit: Decimal

    def applies(self, value: date) -> bool:
        return value >= self.effective_from and (
            self.effective_to is None or value <= self.effective_to
        )


class RuleBook:
    """Versioned structural exchange checks, not investment-risk decisions."""

    versions = (
        RuleVersion(
            version="cn-a-share-2023-08-28",
            effective_from=date(2023, 8, 28),
            effective_to=date(2026, 7, 5),
            risk_warning_price_limit=Decimal("0.05"),
        ),
        RuleVersion(
            version="cn-a-share-2026-07-06",
            effective_from=date(2026, 7, 6),
            effective_to=None,
            risk_warning_price_limit=Decimal("0.10"),
        ),
    )

    def version_for(self, trade_date: date) -> RuleVersion:
        for version in self.versions:
            if version.applies(trade_date):
                return version
        raise ValueError(f"没有覆盖 {trade_date.isoformat()} 的交易规则版本")

    def validate(
        self,
        order: OrderRequest,
        permissions: AccountPermissions,
    ) -> ValidationResult:
        version = self.version_for(order.trade_date)
        findings: list[RuleFinding] = []

        def error(code: str, message: str) -> None:
            findings.append(RuleFinding(code, FindingLevel.ERROR, message))

        def warning(code: str, message: str) -> None:
            findings.append(RuleFinding(code, FindingLevel.WARNING, message))

        security = order.security
        if security.security_type is SecurityType.STOCK and security.board is not Board.MAIN:
            error("OUTSIDE_UNIVERSE", "股票不属于沪深主板范围")
        if security.trading_status is TradingStatus.SUSPENDED:
            error("SUSPENDED", "证券当前停牌")
        elif security.trading_status is TradingStatus.UNKNOWN:
            error("TRADING_STATUS_UNKNOWN", "证券交易状态未知，禁止生成可执行订单")

        if order.quantity <= 0:
            error("INVALID_QUANTITY", "委托数量必须大于 0")
        elif order.side is Side.BUY and order.quantity % security.buy_lot_size != 0:
            error("BUY_LOT", f"买入数量必须是 {security.buy_lot_size} 的整数倍")
        elif order.side is Side.SELL:
            if order.quantity > order.available_sell_quantity:
                error("INSUFFICIENT_SELLABLE", "卖出数量超过当日可卖数量")
            if (
                order.quantity % security.buy_lot_size != 0
                and order.quantity != order.available_sell_quantity
            ):
                error("ODD_LOT_PARTIAL_SELL", "零股只能在一次委托中全部卖出")

        if security.risk_warning is None:
            error("RISK_WARNING_STATUS_UNKNOWN", "证券风险警示状态未知")
        elif security.risk_warning and permissions.risk_warning is not True:
            error("RISK_WARNING_PERMISSION", "风险警示证券交易权限未确认")

        if order.limit_price <= 0:
            error("INVALID_PRICE", "委托价格必须大于 0")
        elif not _is_tick_aligned(order.limit_price, security.price_tick):
            error("PRICE_TICK", f"委托价格必须符合 {security.price_tick} 的最小变动单位")

        ratio = security.price_limit_ratio
        if (
            ratio is None
            and security.security_type is SecurityType.STOCK
            and security.risk_warning is not None
        ):
            ratio = (
                version.risk_warning_price_limit
                if security.risk_warning
                else Decimal("0.10")
            )
        if order.previous_close is not None and not security.price_limit_exempt:
            if ratio is None:
                error("PRICE_LIMIT_UNKNOWN", "该证券涨跌幅限制未知")
            else:
                upper = _round_to_tick(order.previous_close * (Decimal("1") + ratio), security.price_tick)
                lower = _round_to_tick(order.previous_close * (Decimal("1") - ratio), security.price_tick)
                if order.limit_price > upper or order.limit_price < lower:
                    error("OUTSIDE_PRICE_LIMIT", f"委托价格超出 [{lower}, {upper}] 涨跌幅范围")
        elif order.previous_close is None and not security.price_limit_exempt:
            warning("PREVIOUS_CLOSE_MISSING", "缺少昨收价，尚未完成涨跌幅合法性校验")

        if order.continuous_auction:
            if order.reference_price is None:
                error("REFERENCE_PRICE_MISSING", "连续竞价价格笼子校验缺少基准价格")
            else:
                ten_ticks = security.price_tick * 10
                if order.side is Side.BUY:
                    maximum = max(order.reference_price * Decimal("1.02"), order.reference_price + ten_ticks)
                    if order.limit_price > maximum:
                        error("PRICE_CAGE", f"买入价格超过价格笼子上限 {maximum}")
                else:
                    minimum = min(order.reference_price * Decimal("0.98"), order.reference_price - ten_ticks)
                    if order.limit_price < minimum:
                        error("PRICE_CAGE", f"卖出价格低于价格笼子下限 {minimum}")

        if order.side is Side.BUY and order.available_cash is not None:
            notional = order.limit_price * order.quantity
            if order.commission_fixed_per_order is not None:
                required_cash = notional + order.commission_fixed_per_order
            elif order.commission_rate is None:
                warning("COMMISSION_RATE_MISSING", "免五已确认，但佣金率未知；仅按成交金额校验现金")
                required_cash = notional
            else:
                required_cash = notional * (Decimal("1") + order.commission_rate)
            if required_cash > order.available_cash:
                error("INSUFFICIENT_CASH", "可用资金不足")

        return ValidationResult(version.version, tuple(findings))


def _is_tick_aligned(price: Decimal, tick: Decimal) -> bool:
    return price % tick == 0


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
