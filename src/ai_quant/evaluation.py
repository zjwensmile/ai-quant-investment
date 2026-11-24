from __future__ import annotations

import statistics
from datetime import date, datetime
from typing import Any

from .features import _signal_closes
from .market_store import MarketStore
from .store import AuditStore, utc_now


METHOD_VERSION = "trading-session-horizon-action-adjusted-v1"
CURRENT_SOURCES = {"HOLDINGS", "QUALITY_VALUE", "DEFENSIVE", "TACTICAL", "MARKET"}


class EvaluationEngine:
    def __init__(self, audit_store: AuditStore, market_store: MarketStore) -> None:
        self.audit_store = audit_store
        self.market_store = market_store

    def evaluate_due(self, as_of: date) -> dict[str, int]:
        completed = 0
        pending = 0
        for prediction in self.audit_store.pending_predictions():
            outcome = self._evaluate(prediction, as_of)
            if outcome is None:
                pending += 1
                continue
            self.audit_store.record_prediction_outcome(outcome)
            completed += 1
        return {"completed": completed, "pending": pending}

    def _evaluate(
        self, prediction: dict[str, Any], as_of: date
    ) -> dict[str, Any] | None:
        cutoff = date.fromisoformat(str(prediction["price_cutoff_date"]))
        baseline = self.market_store.bars(
            str(prediction["symbol"]), "raw", 1, cutoff
        )
        if not baseline:
            return None
        future = self.market_store.bars_between(
            str(prediction["symbol"]), cutoff.fromordinal(cutoff.toordinal() + 1), as_of
        )
        bars = baseline + future
        horizon = int(prediction["horizon_days"])
        if horizon <= 0 or len(bars) <= horizon:
            return None
        selected = bars[: horizon + 1]
        end_date = date.fromisoformat(str(selected[-1]["trade_date"]))
        actions = self.market_store.corporate_actions(str(prediction["symbol"]), end_date)
        closes, _ = _signal_closes(selected, actions)
        start_price = float(selected[0]["close"])
        end_price = float(selected[-1]["close"])
        realized = closes[-1] / closes[0] - 1.0
        actual_class = _closest_scenario(prediction, realized)
        if actual_class is None:
            status = "return_only"
            brier = None
        else:
            status = "completed"
            brier = _brier(prediction, actual_class)
        return {
            "prediction_id": prediction["prediction_id"],
            "status": status,
            "start_trade_date": selected[0]["trade_date"],
            "end_trade_date": selected[-1]["trade_date"],
            "start_price": start_price,
            "end_price": end_price,
            "realized_return": realized,
            "actual_class": actual_class,
            "brier_score": brier,
            "evaluated_at": utc_now(),
            "method_version": METHOD_VERSION,
        }


def evaluation_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strategy = str(row["strategy"])
        source = strategy if strategy in CURRENT_SOURCES else "旧模型历史归档（不参与新选股）"
        groups.setdefault(source, []).append(row)
    by_source = {
        strategy: _summary(items) for strategy, items in sorted(groups.items())
    }
    return {
        "method_version": METHOD_VERSION,
        "total_predictions": len(rows),
        "evaluated": sum(row.get("outcome_status") is not None for row in rows),
        "by_source": by_source,
        "by_strategy": by_source,
        "by_agent": _group_summary(rows, "agent_id"),
        "by_horizon_sessions": _group_summary(rows, "horizon_days"),
        "by_benchmark": _group_summary(rows, "benchmark"),
        "by_portfolio_outcome": _group_summary(rows, "portfolio_outcome"),
        "by_risk_outcome": _group_summary(rows, "risk_outcome"),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("realized_return") is not None]
    briers = [float(row["brier_score"]) for row in evaluated if row.get("brier_score") is not None]
    hits = [
        (row["direction"] == "bullish" and float(row["realized_return"]) > 0)
        or (row["direction"] == "bearish" and float(row["realized_return"]) < 0)
        or (row["direction"] == "neutral" and row.get("actual_class") == "neutral")
        for row in evaluated
    ]
    selected = [row for row in rows if str(row.get("portfolio_outcome")) == "selected"]
    risk_rejected = [
        row for row in rows
        if str(row.get("risk_outcome") or "").lower()
        in {"rejected", "reject", "reduced", "defer", "veto"}
    ]
    ordered_returns = [
        float(row["realized_return"])
        for row in sorted(
            evaluated,
            key=lambda item: str(item.get("end_trade_date") or item.get("created_at") or ""),
        )
    ]
    return {
        "predictions": len(rows),
        "evaluated": len(evaluated),
        "mean_realized_return": (
            statistics.fmean(float(row["realized_return"]) for row in evaluated)
            if evaluated else None
        ),
        "directional_hit_rate": statistics.fmean(hits) if hits else None,
        "maximum_drawdown": _maximum_drawdown(ordered_returns),
        "portfolio_selection_rate": len(selected) / len(rows) if rows else None,
        "risk_rejection_rate": len(risk_rejected) / len(rows) if rows else None,
        "mean_brier_score": statistics.fmean(briers) if briers else None,
        "p_up_reliability": _reliability(evaluated),
        "calibration_status": "cold_start" if len(briers) < 30 else "reportable",
    }


def _maximum_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    equity = peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum = min(maximum, equity / peak - 1.0)
    return maximum


def _group_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or "unclassified"), []).append(row)
    return {name: _summary(items) for name, items in sorted(groups.items())}


def _reliability(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        upper = lower + 0.2
        members = [
            row for row in rows
            if lower <= float(row["p_up"]) < upper
            or (upper == 1.0 and float(row["p_up"]) == 1.0)
        ]
        if members:
            bins.append({
                "range": f"[{lower:.1f},{upper:.1f}{']' if upper == 1.0 else ')'}",
                "count": len(members),
                "mean_predicted_p_up": statistics.fmean(float(row["p_up"]) for row in members),
                "observed_up_frequency": statistics.fmean(
                    row.get("actual_class") == "up" for row in members
                ),
            })
    return bins


def _closest_scenario(prediction: dict[str, Any], realized: float) -> str | None:
    scenarios = {
        "up": prediction.get("return_up"),
        "neutral": prediction.get("return_neutral"),
        "down": prediction.get("return_down"),
    }
    if any(value is None for value in scenarios.values()):
        return None
    return min(scenarios, key=lambda name: abs(realized - float(scenarios[name])))


def _brier(prediction: dict[str, Any], actual: str) -> float:
    probabilities = {
        "up": float(prediction["p_up"]),
        "neutral": float(prediction["p_neutral"]),
        "down": float(prediction["p_down"]),
    }
    return sum((probability - float(name == actual)) ** 2 for name, probability in probabilities.items())
