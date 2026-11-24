from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .order_plans import _expired


class PortfolioScenarioBuilder:
    """Builds discrete, lot-aware portfolio menus; it does not choose the scenario."""

    new_cash_fractions = (
        ("small_satellite", 0.10),
        ("measured_addition", 0.25),
        ("diversified_addition", 0.40),
    )

    def build(
        self,
        snapshot: dict[str, Any],
        research_outputs: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        budget = float(snapshot.get("account", {}).get("capital_budget") or 0)
        securities = {item["symbol"]: item for item in snapshot.get("securities", [])}
        quotes = snapshot.get("quotes", {})
        positions = {
            str(item["symbol"]): item
            for item in snapshot.get("account", {}).get("positions", [])
            if int(item.get("quantity") or 0) > 0
        }
        scenarios = [self._cash_only(snapshot)]
        if positions and budget > 0:
            scenarios.append(self._current_holdings(snapshot, positions, quotes, budget))
            reduction = self._holding_reduction(snapshot, research_outputs, positions, quotes, budget)
            if reduction:
                scenarios.append(reduction)
        ranked = self._ranked_decisions(
            research_outputs, securities, quotes, snapshot.get("candidates", {}), positions,
            snapshot.get("as_of_time")
        )
        if budget <= 0 or not ranked:
            return scenarios
        available_slots = max(min(3, 6 - len(positions)), 0)
        picks = [item for item in ranked if item["symbol"] in positions or available_slots]
        if not picks:
            return scenarios
        cash_confirmed = snapshot.get("account", {}).get("cash") is not None
        for scenario_id, cash_fraction in self.new_cash_fractions:
            scenario = self._allocation(
                snapshot,
                scenario_id,
                cash_fraction,
                picks,
                positions,
                quotes,
                securities,
                budget,
            )
            scenario["planning_only"] = not cash_confirmed
            scenario["blockers"] = (
                [] if cash_confirmed else ["券商可用现金尚未确认，禁止生成可执行买单"]
            )
            scenarios.append(scenario)
        return scenarios

    def _holding_reduction(self, snapshot, research_outputs, positions, quotes, budget):
        scenario = self._current_holdings(snapshot, positions, quotes, budget)
        changed = False
        for target in scenario["target_positions"]:
            symbol = target["symbol"]
            bearish = [
                decision for output in research_outputs.values()
                for decision in output.get("decisions", [])
                if decision.get("symbol") == symbol
                and decision.get("strategy") == "HOLDINGS"
                and decision.get("direction") == "bearish"
                and not (snapshot.get("as_of_time") and _expired(
                    decision.get("forecast", {}).get("signal_expires_at"), snapshot["as_of_time"]
                ))
            ]
            available = int(positions[symbol].get("available_quantity") or 0)
            quantity = int(positions[symbol].get("quantity") or 0)
            price = float((quotes.get(symbol) or {}).get("last") or 0)
            if not bearish or available <= 0 or price <= 0:
                continue
            remaining = max(quantity - min(available, quantity), 0)
            target.update({
                "target_weight": remaining * price / budget,
                "action": "sell" if remaining == 0 else "reduce",
                "decision_ids": [item["decision_id"] for item in bearish],
                "expires_at": min((item.get("forecast", {}).get("signal_expires_at") for item in bearish
                                   if item.get("forecast", {}).get("signal_expires_at")), default=None),
                "invalidation_conditions": list(dict.fromkeys(
                    value for item in bearish for value in item.get("invalidation_conditions", [])
                )),
            })
            scenario["order_targets"][symbol] = remaining
            changed = True
        if not changed:
            return None
        scenario.update({
            "scenario_id": "evidence_based_reduction",
            "cash_weight": max(1 - sum(item["target_weight"] for item in scenario["target_positions"]), 0),
            "rotation_sequence": "仅规划有看空依据且已可卖的持仓减仓；人工确认卖出成交并更新账户后，重新研究新仓机会。卖出前不预支资金或名额。",
        })
        return scenario

    @staticmethod
    def _cash_only(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "scenario_id": "cash_only",
            "cash_weight": 1.0,
            "target_positions": [],
            "order_targets": {},
            "planning_only": False,
            "blockers": [],
            "account_version": snapshot.get("account", {}).get("version", "unconfigured"),
        }

    @staticmethod
    def _current_holdings(snapshot, positions, quotes, budget):
        targets = []
        order_targets = {}
        invested = 0.0
        for symbol, position in sorted(positions.items()):
            quantity = int(position.get("quantity") or 0)
            price = float((quotes.get(symbol) or {}).get("last") or 0)
            weight = quantity * price / budget if price > 0 else 0.0
            invested += weight
            targets.append(
                {
                    "symbol": symbol,
                    "target_weight": weight,
                    "action": "hold",
                    "price_min": price or None,
                    "price_max": price or None,
                    "expires_at": None,
                    "decision_ids": [],
                    "invalidation_conditions": [],
                }
            )
            order_targets[symbol] = quantity
        return {
            "scenario_id": "current_holdings",
            "cash_weight": max(1.0 - invested, 0.0),
            "target_positions": targets,
            "order_targets": order_targets,
            "planning_only": False,
            "blockers": [],
            "account_version": snapshot.get("account", {}).get("version", "unconfigured"),
        }

    @staticmethod
    def _ranked_decisions(research_outputs, securities, quotes, candidates, positions=None, as_of_time=None):
        selected = {
            (str(item.get("symbol")), source): item
            for source, values in candidates.items()
            for item in values
        }
        grouped: dict[str, dict[str, Any]] = {}
        for agent_id, output in research_outputs.items():
            for decision in output.get("decisions", []):
                symbol = decision.get("symbol")
                source = str(decision.get("strategy") or "")
                if not symbol or symbol not in securities or symbol not in quotes:
                    continue
                candidate = selected.get((str(symbol), source))
                if candidate is None or (source == "HOLDINGS" and symbol not in (positions or {})):
                    continue
                if candidate.get("entry_eligible") is not True:
                    continue
                if decision.get("direction") != "bullish":
                    continue
                security = securities[symbol]
                if security.get("trading_status") != "tradable":
                    continue
                if security.get("security_type") == "etf" and (
                    security.get("same_day_turnaround") is None
                    or security.get("price_limit_ratio") is None
                ):
                    continue
                forecast = decision.get("forecast", {})
                if forecast.get("signal_expires_at") and as_of_time:
                    if _expired(forecast["signal_expires_at"], as_of_time):
                        continue
                expected = forecast.get("expected_return_net")
                score = (
                    float(expected)
                    if expected is not None
                    else float(forecast.get("p_up", 0)) - float(forecast.get("p_down", 0))
                )
                if score <= 0:
                    continue
                current = grouped.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "source": source,
                        "score": 0.0,
                        "supporting_agents": [],
                        "decision_ids": [],
                        "max_action_prices": [],
                        "expiries": [],
                        "invalidation_conditions": [],
                    },
                )
                current["score"] += score
                current["supporting_agents"].append(agent_id)
                current["decision_ids"].append(decision["decision_id"])
                if forecast.get("max_action_price") is not None:
                    current["max_action_prices"].append(float(forecast["max_action_price"]))
                if forecast.get("signal_expires_at"):
                    current["expiries"].append(str(forecast["signal_expires_at"]))
                current["invalidation_conditions"].extend(
                    decision.get("invalidation_conditions", [])
                )
        values = list(grouped.values())
        for item in values:
            item["score"] += 0.01 * min(len(set(item["supporting_agents"])), 3)
            security = securities[item["symbol"]]
            if item["source"] == "DEFENSIVE" and security.get("security_type") == "etf":
                item["score"] += 0.02
        return sorted(values, key=lambda item: (-item["score"], item["symbol"]))

    @staticmethod
    def _allocation(
        snapshot,
        scenario_id,
        cash_fraction,
        picks,
        positions,
        quotes,
        securities,
        budget,
    ):
        targets = []
        order_targets = {}
        invested_value = 0.0
        for symbol, position in sorted(positions.items()):
            quantity = int(position.get("quantity") or 0)
            price = float((quotes.get(symbol) or {}).get("last") or 0)
            value = quantity * price
            invested_value += value
            targets.append({
                "symbol": symbol, "target_weight": value / budget,
                "action": "hold", "price_min": price or None,
                "price_max": price or None, "expires_at": None,
                "decision_ids": [], "invalidation_conditions": [],
            })
            order_targets[symbol] = quantity
        remaining_budget = max(budget - invested_value, 0.0)
        confirmed_cash = snapshot.get("account", {}).get("cash")
        account_cash = (
            float(confirmed_cash) if confirmed_cash is not None else remaining_budget
        )
        new_budget = min(account_cash, remaining_budget) * cash_fraction
        # Scan the full ranking: an unaffordable lot must not consume a slot.
        allocation_slots = max(min(3, 6 - len(positions)), 0)
        add_slots = sum(item["symbol"] in positions for item in picks)
        per_symbol_budget = new_budget / max(min(3, allocation_slots + add_slots, len(picks)), 1)
        tactical_used = 0.0
        allocated_count = 0
        new_count = 0
        spent = 0.0
        for pick in picks:
            symbol = pick["symbol"]
            if allocated_count >= 3:
                break
            if symbol not in positions and new_count >= allocation_slots:
                continue
            price = float((quotes.get(symbol) or {}).get("last") or 0)
            if price <= 0:
                continue
            if pick["max_action_prices"] and price > min(pick["max_action_prices"]):
                continue
            lot = int(securities[symbol].get("buy_lot_size") or 100)
            maximum_weight = (
                0.30 if securities[symbol].get("security_type") == "etf" else 0.20
            )
            current_quantity = int((positions.get(symbol) or {}).get("quantity") or 0)
            current_value = current_quantity * price
            remaining_symbol_capacity = max(budget * maximum_weight - current_value, 0.0)
            # A fractional split smaller than one lot must not suppress a lot
            # that fits the scenario's actual remaining cash and weight limits.
            symbol_budget = min(max(per_symbol_budget, price * lot + 1.0),
                                remaining_symbol_capacity, new_budget - spent)
            if pick.get("source") == "TACTICAL":
                symbol_budget = min(symbol_budget, max(budget * 0.10 - tactical_used, 0.0))
            target_quantity = math.floor(max(symbol_budget - 1.0, 0) / (price * lot)) * lot
            if target_quantity <= 0:
                continue
            allocated_count += 1
            new_count += int(symbol not in positions)
            if current_quantity:
                target_quantity += current_quantity
            action = (
                "buy"
                if current_quantity == 0
                else "add" if target_quantity > current_quantity else "hold"
                if target_quantity == current_quantity
                else "reduce"
            )
            value = target_quantity * price
            added_value = max(target_quantity - current_quantity, 0) * price
            invested_value += added_value
            spent += added_value
            if pick.get("source") == "TACTICAL":
                tactical_used += added_value
            maximum = min(pick["max_action_prices"]) if pick["max_action_prices"] else price
            expiry = min(pick["expiries"]) if pick["expiries"] else None
            target = {
                    "symbol": symbol,
                    "target_weight": value / budget,
                    "action": action,
                    "price_min": price,
                    "price_max": maximum,
                    "expires_at": expiry,
                    "decision_ids": sorted(set(pick["decision_ids"])),
                    "invalidation_conditions": list(
                        dict.fromkeys(pick["invalidation_conditions"])
                    ),
                }
            targets = [item for item in targets if item["symbol"] != symbol]
            targets.append(target)
            order_targets[symbol] = target_quantity
        return {
            "scenario_id": scenario_id,
            "cash_weight": max(1.0 - invested_value / budget, 0.0),
            "target_positions": targets,
            "order_targets": order_targets,
            "planning_only": False,
            "blockers": [],
            "account_version": snapshot.get("account", {}).get("version", "unconfigured"),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "candidate_source_by_symbol": {
                item["symbol"]: item.get("source") for item in picks
            },
            "portfolio_constraints": {
                "maximum_total_positions": 6,
                "maximum_new_positions": 3,
                "maximum_single_stock_weight": 0.20,
                "maximum_single_etf_weight": 0.30,
                "maximum_tactical_weight": 0.10,
                "defensive_etf_core_preferred_after_agent_confirmation": True,
                "preserve_existing_holdings_without_sell_evidence": True,
            },
        }
