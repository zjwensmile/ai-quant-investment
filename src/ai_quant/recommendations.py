from __future__ import annotations

from typing import Any

from .portfolio import PortfolioScenarioBuilder
from .order_plans import _expired


def risk_price_guidance(risk: dict[str, Any], symbol: str, *,
                       approved: bool, as_of_time: str | None = None) -> dict[str, Any]:
    source = "approved_positions" if approved else "monitored_positions"
    position = next((item for item in risk.get(source, []) if str(item.get("symbol")) == symbol), {})
    complete = position.get("take_profit_price") is not None and position.get("stop_loss_price") is not None
    return {
        "take_profit_price": position.get("take_profit_price"),
        "stop_loss_price": position.get("stop_loss_price"),
        "risk_price_rationale": position.get("risk_price_rationale"),
        "risk_price_source": source if position else None,
        "risk_analysis_as_of": risk.get("as_of_time") or as_of_time,
        "risk_price_status": "available" if complete else "pending_risk",
    }


def research_opportunities(snapshot: dict[str, Any], outputs: dict[str, Any],
                           portfolio: dict[str, Any], risk: dict[str, Any],
                           plan: dict[str, Any], as_of_time: str | None = None) -> list[dict[str, Any]]:
    """Present recorded opinions; a bullish opinion is never an approval."""
    rows: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    securities = {str(item["symbol"]): item for item in snapshot.get("securities", [])}
    held = {str(item["symbol"]) for item in snapshot.get("account", {}).get("positions", [])
            if int(item.get("quantity") or 0) > 0}

    def row_for(symbol, source, signal):
        return rows.setdefault((symbol, source, signal), {
            "symbol": symbol, "name": securities.get(symbol, {}).get("name", symbol),
            "source": source, "signal_id": signal, "opinions": [], "selection_reason": None,
        })

    for source, candidates in snapshot.get("candidates", {}).items():
        for item in candidates:
            row_for(str(item["symbol"]), source, item.get("signal_id"))
    for source, candidates in snapshot.get("chanlun_daily_candidates", {}).items():
        for item in candidates:
            row_for(str(item["symbol"]), source, item.get("signal_id"))
    for item in outputs.get("chanlun_candidate_selector", {}).get("excluded_candidates", []):
        row_for(str(item["symbol"]), item["candidate_source"], item.get("signal_id"))["selection_reason"] = item.get("reason")
    for agent_id, output in outputs.items():
        for item in output.get("decisions", []):
            if not item.get("symbol") or item.get("direction") not in {"bullish", "neutral", "bearish"}:
                continue
            row_for(str(item["symbol"]), str(item.get("strategy")), item.get("signal_id"))["opinions"].append({
                "agent_id": agent_id, "decision_id": item.get("decision_id"),
                "direction": item["direction"], "summary": item.get("summary"),
                "forecast": item.get("forecast", {}),
                "invalidation_conditions": item.get("invalidation_conditions", []),
                "uncertainty_sources": item.get("uncertainty_sources", []),
            })
    menus = PortfolioScenarioBuilder().build(snapshot, outputs)
    menu_ids = {value for scenario in menus for target in scenario.get("target_positions", [])
                if target.get("action") in {"buy", "add"} for value in target.get("decision_ids", [])}
    selected_positions = {str(item["symbol"]): item for item in portfolio.get("target_positions", [])
                          if item.get("action") in {"buy", "add", "increase", "open"}}
    approved_positions = {str(item["symbol"]): item for item in risk.get("approved_positions", [])
                          if item.get("action") in {"buy", "add", "increase", "open"}}
    orders = {str(item["symbol"]): item for item in plan.get("orders", []) if item.get("side") == "buy"}
    for row in rows.values():
        ids = {item["decision_id"] for item in row["opinions"]}
        directions = {item["direction"] for item in row["opinions"]}
        proposed = selected_positions.get(row["symbol"], {})
        selected = bool(ids & set(proposed.get("decision_ids", [])))
        passed = selected and row["symbol"] in approved_positions and risk.get("decision") in {"approve", "reduce"}
        position = approved_positions[row["symbol"]] if passed else proposed if selected else {}
        order = orders.get(row["symbol"], {})
        blockers = list(order.get("findings", []))
        bullish = [item for item in row["opinions"] if item["direction"] == "bullish"]
        moment = as_of_time or snapshot.get("as_of_time")
        price = float(snapshot.get("quotes", {}).get(row["symbol"], {}).get("last") or 0)
        if selected:
            expired = bool(moment and _expired(position.get("expires_at"), moment))
            outside_price = bool(price > 0 and (
                (position.get("price_max") is not None and price > float(position["price_max"]))
                or (position.get("price_min") is not None and price < float(position["price_min"]))
            ))
        else:
            active = [item for item in bullish if not (
                moment and _expired(item["forecast"].get("signal_expires_at"), moment)
            )]
            expired = bool(bullish and not active)
            limits = [float(item["forecast"]["max_action_price"]) for item in active
                      if item["forecast"].get("max_action_price") is not None]
            # Conflicting opinions remain separate; one stricter opinion does
            # not establish a common entry ceiling for the other opinions.
            outside_price = bool(limits and len(limits) == len(active) and all(price > limit for limit in limits))
        if expired:
            blockers.append("已选买入方案已过期，须重新研究后再判断" if selected else "看多预测已过期，须重新研究后再判断")
        elif outside_price:
            blockers.append("当前价格不在已选买入方案的价格区间内，等待条件满足并重新核验" if selected
                            else "当前价格高于各条有效看多研究各自给出的买入上限，等待条件满足并重新核验")
        if "bullish" in directions and not selected and not ids & menu_ids:
            blockers.append("已达六个持仓，新仓需等待有依据的减仓成交后重新分析" if len(held) >= 6 and row["symbol"] not in held
                            else "未进入买入菜单：需满足来源资格、交易属性、整手资金及单标的仓位限制")
        row.update({
            **risk_price_guidance(risk, row["symbol"], approved=passed,
                                 as_of_time=snapshot.get("as_of_time")),
            "entry_condition_source": "approved_positions" if passed else "portfolio_target" if selected else None,
            "price_min": position.get("price_min"),
            "price_max": position.get("price_max"),
            "expires_at": position.get("expires_at"),
            "research_status": "mixed" if len(directions) > 1 else next(iter(directions), "not_researched"),
            "portfolio_selected": selected,
            "risk_status": "approved" if passed else str(risk.get("decision")) if selected else "not_submitted",
            "execution_status": str(order.get("status")) if passed and order and not expired and not outside_price else "not_ready",
            "signal_state": "expired" if expired else "price_not_reached" if outside_price else "current",
            "status": "expired" if expired else "research_watch" if outside_price else "approved_buy" if passed else "portfolio_proposed" if selected else "research_watch" if "bullish" in directions else "research_only",
            "blockers": blockers,
        })
    return sorted(rows.values(), key=lambda row: (row["symbol"], row["source"], row["signal_id"] or ""))
