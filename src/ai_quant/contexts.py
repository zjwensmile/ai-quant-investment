from __future__ import annotations

from typing import Any

from .domain import AgentSpec, AgentStage


class ContextBuilder:
    """Builds the smallest relevant, auditable view for each Agent."""

    _candidate_routes = {
        "chanlun_candidate_selector": (
            "CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY",
        ),
        "portfolio_controller": (),
        "risk_controller": (),
    }

    def build(self, agent: AgentSpec, snapshot: dict[str, Any]) -> dict[str, Any]:
        base: dict[str, Any] = {
            "snapshot_id": snapshot["snapshot_id"],
            "as_of_time": snapshot["as_of_time"],
            "market": snapshot.get("market", {}),
            "data_quality": snapshot.get("data_quality", {}),
            "feature_coverage": snapshot.get("feature_coverage", {}),
            "research_policy": {
                "breadth_universe": "market.breadth and market.regime describe the researchable stock+ETF sample with historical features. market.all_stock_distribution describes the full A-share stock distribution. Never label a sample ratio as the full A-share ratio; state the corresponding denominator.",
                "direction_basis": "Investment evidence within this Agent's role; a research decision is not trade approval.",
                "execution_restrictions": "research_only, stale_for_execution, closed sessions and execution_eligible=false restrict execution, not investment direction.",
                "chanlun_reference_validation": "reference_validation_passed is comparative reference-test metadata, not an active-mode enablement gate or a security-specific bearish fact. Assess actual signal structure, mode and entry_eligible separately.",
            },
        }
        if agent.agent_id == "monthly_concept_selector":
            base["account"] = snapshot.get("account", {})
            base["events"] = snapshot.get("events", [])
            base["concept_scope"] = (
                "Provide market and industry background only. Concepts do not select securities "
                "and cannot add to the Chan-lun candidate pool."
            )
            return base

        routes = self._candidate_routes.get(
            agent.agent_id,
            tuple(snapshot.get("candidates", {})),
        )
        candidate_data = (
            snapshot.get("chanlun_daily_candidates", {})
            if agent.agent_id == "chanlun_candidate_selector"
            else snapshot.get("candidates", {})
        )
        candidates = {route: candidate_data.get(route, []) for route in routes}
        symbols = {item["symbol"] for values in candidates.values() for item in values}
        if agent.stage is AgentStage.CHALLENGE:
            symbols.update(snapshot.get("portfolio_context", {}).get("held_symbols", []))
        base.update(
            {
                "candidates": candidates,
                "account": snapshot.get("account", {}),
                "portfolio_context": snapshot.get("portfolio_context", {}),
                "securities": {
                    item["symbol"]: item
                    for item in snapshot.get("securities", [])
                    if item["symbol"] in symbols
                },
                "quotes": {
                    symbol: quote
                    for symbol, quote in snapshot.get("quotes", {}).items()
                    if symbol in symbols
                },
                "events": snapshot.get("events", []),
            }
        )
        return base
