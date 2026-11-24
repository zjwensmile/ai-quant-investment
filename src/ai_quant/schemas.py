from __future__ import annotations

from typing import Any

from .domain import SchemaKind


EVIDENCE_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_id": {"type": "string"},
        "claim": {"type": "string"},
        "source": {"type": "string"},
        "published_at": {"type": ["string", "null"]},
        "available_at": {"type": ["string", "null"]},
        "quality": {
            "type": "string",
            "enum": [
                "official_primary", "company_primary", "major_media",
                "secondary", "community", "unknown",
            ],
        },
    },
    "required": [
        "evidence_id",
        "claim",
        "source",
        "published_at",
        "available_at",
        "quality",
    ],
    "additionalProperties": False,
}


CONCEPTS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string", "minLength": 1},
        "as_of_time": {"type": "string", "minLength": 1},
        "month": {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"},
        "concepts": {"type": "array", "minItems": 3, "maxItems": 5, "items": {
            "type": "object",
            "properties": {
                "concept_id": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1},
                "thesis": {"type": "string", "minLength": 1}, "market_environment": {"type": "string", "minLength": 1},
                "selection_criteria": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "supporting_evidence": {"type": "array", "items": EVIDENCE_ITEM},
            },
            "required": ["concept_id", "name", "thesis", "market_environment",
                         "selection_criteria", "risks", "supporting_evidence"],
            "additionalProperties": False,
        }},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["agent_id", "as_of_time", "month", "concepts", "summary"],
    "additionalProperties": False,
}


CANDIDATE_POOL_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string", "minLength": 1}, "as_of_time": {"type": "string", "minLength": 1},
        "concept_month": {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"},
        "candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "pattern": "^[0-9]{6}$"}, "name": {"type": "string", "minLength": 1},
                "concept_id": {"type": "string", "minLength": 1}, "concept_name": {"type": "string", "minLength": 1},
                "selection_rationale": {"type": "string", "minLength": 1},
                "market_position": {"type": "string", "minLength": 1}, "industry_position": {"type": "string", "minLength": 1},
                "quality_value_checks": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "number", "minimum": 0, "maximum": 1},
                "supporting_evidence": {"type": "array", "items": EVIDENCE_ITEM},
            },
            "required": ["symbol", "name", "concept_id", "concept_name",
                         "selection_rationale", "market_position", "industry_position",
                         "quality_value_checks", "risks", "priority", "supporting_evidence"],
            "additionalProperties": False,
        }},
        "rejected_ideas": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["agent_id", "as_of_time", "concept_month", "candidates",
                 "rejected_ideas", "summary"],
    "additionalProperties": False,
}


CHANLUN_SELECTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string", "minLength": 1},
        "as_of_time": {"type": "string", "minLength": 1},
        "candidates": {"type": "array", "maxItems": 20, "items": {
            "type": "object",
            "properties": {
                "candidate_source": {"type": "string", "enum": [
                    "CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY",
                ]},
                "symbol": {"type": "string", "pattern": "^[0-9]{6}$"},
                "signal_id": {"type": "string", "minLength": 1},
                "market_position": {"type": "string", "minLength": 1},
                "industry_prosperity": {"type": "string", "minLength": 1},
                "fundamental_assessment": {"type": "string", "minLength": 1},
                "liquidity_assessment": {"type": "string", "minLength": 1},
                "signal_freshness": {"type": "string", "minLength": 1},
                "selection_rationale": {"type": "string", "minLength": 1},
                "priority": {"type": "number", "minimum": 0, "maximum": 1},
                "supporting_evidence": {"type": "array", "items": EVIDENCE_ITEM},
            },
            "required": [
                "candidate_source", "symbol", "signal_id", "market_position",
                "industry_prosperity", "fundamental_assessment", "liquidity_assessment",
                "signal_freshness", "selection_rationale", "priority", "supporting_evidence",
            ],
            "additionalProperties": False,
        }},
        "excluded_candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "candidate_source": {"type": "string", "enum": [
                    "CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY",
                ]},
                "symbol": {"type": "string", "pattern": "^[0-9]{6}$"},
                "signal_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["candidate_source", "symbol", "signal_id", "reason"],
            "additionalProperties": False,
        }},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["agent_id", "as_of_time", "candidates", "excluded_candidates", "summary"],
    "additionalProperties": False,
}


FORECAST: dict[str, Any] = {
    "type": "object",
    "properties": {
        "horizon_days": {"type": "integer", "minimum": 0},
        "benchmark": {"type": "string"},
        "reference_price": {"type": ["number", "null"]},
        "max_action_price": {"type": ["number", "null"]},
        "p_up": {"type": "number", "minimum": 0, "maximum": 1},
        "p_neutral": {"type": "number", "minimum": 0, "maximum": 1},
        "p_down": {"type": "number", "minimum": 0, "maximum": 1},
        "return_up": {"type": ["number", "null"]},
        "return_neutral": {"type": ["number", "null"]},
        "return_down": {"type": ["number", "null"]},
        "expected_return_net": {"type": ["number", "null"]},
        "signal_expires_at": {"type": ["string", "null"]},
    },
    "required": [
        "horizon_days",
        "benchmark",
        "reference_price",
        "max_action_price",
        "p_up",
        "p_neutral",
        "p_down",
        "return_up",
        "return_neutral",
        "return_down",
        "expected_return_net",
        "signal_expires_at",
    ],
    "additionalProperties": False,
}


RESEARCH_DECISION: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision_id": {"type": "string"},
        "subject": {"type": "string"},
        "symbol": {"type": ["string", "null"]},
        "strategy": {
            "type": "string",
            "enum": [
                "HOLDINGS", "QUALITY_VALUE", "DEFENSIVE", "TACTICAL", "MARKET"
                , "AGENT_SELECTED", "CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY"
            ],
        },
        "signal_id": {"type": ["string", "null"]},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "forecast": FORECAST,
        "supporting_evidence": {"type": "array", "items": EVIDENCE_ITEM},
        "opposing_evidence": {"type": "array", "items": EVIDENCE_ITEM},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
        "data_quality": {"type": "string"},
        "uncertainty_sources": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "decision_id",
        "subject",
        "symbol",
        "strategy",
        "signal_id",
        "direction",
        "forecast",
        "supporting_evidence",
        "opposing_evidence",
        "invalidation_conditions",
        "data_quality",
        "uncertainty_sources",
        "summary",
    ],
    "additionalProperties": False,
}


CANDIDATE_COVERAGE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "pattern": "^[0-9]{6}$"},
        "signal_id": {"type": ["string", "null"]},
        "conclusion": {
            "type": "string", "enum": ["supportive", "mixed", "adverse", "no_material_news"]
        },
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["symbol", "signal_id", "conclusion", "summary", "risks"],
    "additionalProperties": False,
}


RESEARCH_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "as_of_time": {"type": "string"},
        "decisions": {"type": "array", "items": RESEARCH_DECISION},
        "rejected_candidates": {"type": "array", "items": {"type": "string"}},
        "overall_data_quality": {"type": "string"},
        "overall_summary": {"type": "string"},
    },
    "required": [
        "agent_id",
        "as_of_time",
        "decisions",
        "rejected_candidates",
        "overall_data_quality",
        "overall_summary",
    ],
    "additionalProperties": False,
}


NEWS_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "news_id": {"type": "string"},
        "title": {"type": "string"},
        "url": {"type": "string"},
        "source_name": {"type": "string"},
        "is_primary_source": {"type": "boolean"},
        "published_at": {"type": ["string", "null"]},
        "available_at": {"type": "string"},
        "affected_symbols": {"type": "array", "items": {"type": "string"}},
        "affected_candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "pattern": "^[0-9]{6}$"},
                "signal_id": {"type": ["string", "null"]},
            },
            "required": ["symbol", "signal_id"],
            "additionalProperties": False,
        }},
        "direction": {
            "type": "string",
            "enum": ["positive", "negative", "mixed", "neutral"],
        },
        "novelty": {"type": "number", "minimum": 0, "maximum": 1},
        "materiality": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "priced_in": {"type": "number", "minimum": 0, "maximum": 1},
        "impact_horizon": {"type": "string"},
        "core_reason": {"type": "string"},
        "contrary_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "news_id",
        "title",
        "url",
        "source_name",
        "is_primary_source",
        "published_at",
        "available_at",
        "affected_symbols",
        "affected_candidates",
        "direction",
        "novelty",
        "materiality",
        "confidence",
        "priced_in",
        "impact_horizon",
        "core_reason",
        "contrary_evidence",
    ],
    "additionalProperties": False,
}


NEWS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "as_of_time": {"type": "string"},
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "core_news": {"type": "array", "items": NEWS_ITEM},
        "candidate_coverage": {"type": "array", "items": CANDIDATE_COVERAGE},
        "rejected_noise": {"type": "array", "items": {"type": "string"}},
        "data_quality": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": [
        "agent_id",
        "as_of_time",
        "search_queries",
        "core_news",
        "candidate_coverage",
        "rejected_noise",
        "data_quality",
        "summary",
    ],
    "additionalProperties": False,
}


CHALLENGE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "as_of_time": {"type": "string"},
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_agent_id": {"type": "string"},
                    "target_decision_ids": {
                        "type": "array", "items": {"type": "string"}
                    },
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "argument": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "recommended_action": {"type": "string"},
                },
                "required": [
                    "target_agent_id",
                    "target_decision_ids",
                    "severity",
                    "argument",
                    "evidence_ids",
                    "recommended_action",
                ],
                "additionalProperties": False,
            },
        },
        "shared_source_risks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["agent_id", "as_of_time", "challenges", "shared_source_risks", "summary"],
    "additionalProperties": False,
}


POSITION: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "target_weight": {"type": "number", "minimum": 0, "maximum": 1},
        "action": {"type": "string", "enum": ["buy", "add", "hold", "reduce", "sell"]},
        "price_min": {"type": ["number", "null"]},
        "price_max": {"type": ["number", "null"]},
        "expires_at": {"type": ["string", "null"]},
        "decision_ids": {"type": "array", "items": {"type": "string"}},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "symbol",
        "target_weight",
        "action",
        "price_min",
        "price_max",
        "expires_at",
        "decision_ids",
        "invalidation_conditions",
    ],
    "additionalProperties": False,
}


RISK_POSITION: dict[str, Any] = {
    "type": "object",
    "properties": {
        **POSITION["properties"],
        "take_profit_price": {"type": "number", "exclusiveMinimum": 0},
        "stop_loss_price": {"type": "number", "exclusiveMinimum": 0},
        "risk_price_rationale": {"type": "string"},
    },
    "required": [
        *POSITION["required"],
        "take_profit_price",
        "stop_loss_price",
        "risk_price_rationale",
    ],
    "additionalProperties": False,
}


RISK_MONITOR_POSITION: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "take_profit_price": {"type": "number", "exclusiveMinimum": 0},
        "stop_loss_price": {"type": "number", "exclusiveMinimum": 0},
        "risk_price_rationale": {"type": "string"},
    },
    "required": [
        "symbol",
        "take_profit_price",
        "stop_loss_price",
        "risk_price_rationale",
    ],
    "additionalProperties": False,
}


PORTFOLIO_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "as_of_time": {"type": "string"},
        "account_version": {"type": "string"},
        "selected_scenario": {"type": "string"},
        "cash_weight": {"type": "number", "minimum": 0, "maximum": 1},
        "target_positions": {"type": "array", "items": POSITION},
        "worst_case": {"type": "string"},
        "alternative": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": [
        "agent_id",
        "as_of_time",
        "account_version",
        "selected_scenario",
        "cash_weight",
        "target_positions",
        "worst_case",
        "alternative",
        "rationale",
    ],
    "additionalProperties": False,
}


RISK_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "as_of_time": {"type": "string"},
        "decision": {"type": "string", "enum": ["approve", "reduce", "defer", "veto"]},
        "approved_cash_weight": {"type": "number", "minimum": 0, "maximum": 1},
        "approved_positions": {"type": "array", "items": RISK_POSITION},
        "monitored_positions": {
            "type": "array", "items": RISK_MONITOR_POSITION
        },
        "hard_rule_findings": {"type": "array", "items": {"type": "string"}},
        "scenario_risks": {"type": "array", "items": {"type": "string"}},
        "required_changes": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": [
        "agent_id",
        "as_of_time",
        "decision",
        "approved_cash_weight",
        "approved_positions",
        "monitored_positions",
        "hard_rule_findings",
        "scenario_risks",
        "required_changes",
        "rationale",
    ],
    "additionalProperties": False,
}


MONTHLY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "period": {"type": "string"},
        "strategy_attribution": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "strategy": {"type": "string"},
                    "attribution": {"type": "string"},
                },
                "required": ["strategy", "attribution"],
                "additionalProperties": False,
            },
        },
        "portfolio_findings": {"type": "array", "items": {"type": "string"}},
        "risk_findings": {"type": "array", "items": {"type": "string"}},
        "execution_findings": {"type": "array", "items": {"type": "string"}},
        "proposed_changes": {"type": "array", "items": {"type": "string"}},
        "insufficient_evidence": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "agent_id",
        "period",
        "strategy_attribution",
        "portfolio_findings",
        "risk_findings",
        "execution_findings",
        "proposed_changes",
        "insufficient_evidence",
        "summary",
    ],
    "additionalProperties": False,
}


SCHEMAS = {
    SchemaKind.CONCEPTS: CONCEPTS_SCHEMA,
    SchemaKind.CANDIDATE_POOL: CANDIDATE_POOL_SCHEMA,
    SchemaKind.CHANLUN_SELECTION: CHANLUN_SELECTION_SCHEMA,
    SchemaKind.RESEARCH: RESEARCH_SCHEMA,
    SchemaKind.NEWS: NEWS_SCHEMA,
    SchemaKind.CHALLENGE: CHALLENGE_SCHEMA,
    SchemaKind.PORTFOLIO: PORTFOLIO_SCHEMA,
    SchemaKind.RISK: RISK_SCHEMA,
    SchemaKind.MONTHLY_REVIEW: MONTHLY_SCHEMA,
}


def schema_for(kind: SchemaKind) -> dict[str, Any]:
    if kind in {SchemaKind.MODULE_RESEARCH, SchemaKind.MODULE_PLAN}:
        from .module_agents import MODULE_PLAN_SCHEMA, MODULE_RESEARCH_SCHEMA
        return MODULE_RESEARCH_SCHEMA if kind is SchemaKind.MODULE_RESEARCH else MODULE_PLAN_SCHEMA
    return SCHEMAS[kind]


def validate_output_shape(kind: SchemaKind, output: dict[str, Any]) -> None:
    required = SCHEMAS[kind]["required"]
    missing = [key for key in required if key not in output]
    if missing:
        raise ValueError(f"Missing required output fields: {', '.join(missing)}")

    if kind is SchemaKind.RESEARCH:
        decision_ids: set[str] = set()
        for decision in output.get("decisions", []):
            decision_id = str(decision.get("decision_id", ""))
            if not decision_id or decision_id in decision_ids:
                raise ValueError("Research decision_id must be present and unique per Agent output")
            decision_ids.add(decision_id)
            if "signal_id" not in decision:
                raise ValueError("Research decisions must include signal_id (null when not Chan-lun)")
            forecast = decision.get("forecast", {})
            probability_sum = sum(
                float(forecast.get(key, 0))
                for key in ("p_up", "p_neutral", "p_down")
            )
            if abs(probability_sum - 1.0) > 1e-6:
                raise ValueError(
                    f"Forecast probabilities must sum to 1 for {decision_id}, got {probability_sum}"
                )

    if kind is SchemaKind.CONCEPTS:
        concepts = output.get("concepts", [])
        if not 3 <= len(concepts) <= 5:
            raise ValueError("Monthly concept Agent must return 3-5 concepts")
        if str(output.get("month")) != _month_from_as_of(output.get("as_of_time")):
            raise ValueError("Concept month must match the Agent as_of_time month")
        concept_ids: set[str] = set()
        for concept in concepts:
            concept_id = str(concept.get("concept_id") or "").strip()
            if not concept_id or concept_id in concept_ids:
                raise ValueError("Concept IDs must be present and unique per Agent output")
            concept_ids.add(concept_id)

    if kind is SchemaKind.CANDIDATE_POOL:
        if str(output.get("concept_month")) != _month_from_as_of(output.get("as_of_time")):
            raise ValueError("Candidate concept_month must match the Agent as_of_time month")
        symbols: set[str] = set()
        for candidate in output.get("candidates", []):
            symbol = str(candidate.get("symbol") or "")
            if len(symbol) != 6 or not symbol.isdigit():
                raise ValueError("Candidate symbol must be a six-digit A-share or ETF code")
            if symbol in symbols:
                raise ValueError("Candidate symbols must be unique per Agent output")
            symbols.add(symbol)

    if kind is SchemaKind.CHANLUN_SELECTION:
        candidates = output.get("candidates", [])
        if len(candidates) > 20:
            raise ValueError("Chan-lun selector may return at most 20 candidates")
        identities: set[tuple[str, str, str]] = set()
        symbols: set[str] = set()
        for candidate in candidates:
            source = str(candidate.get("candidate_source") or "")
            symbol = str(candidate.get("symbol") or "")
            signal_id = str(candidate.get("signal_id") or "").strip()
            identity = (source, symbol, signal_id)
            if source not in {"CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY"}:
                raise ValueError("Chan-lun selector candidate_source is invalid")
            if len(symbol) != 6 or not symbol.isdigit() or not signal_id:
                raise ValueError("Chan-lun selector requires a six-digit symbol and signal_id")
            if identity in identities or symbol in symbols:
                raise ValueError("Chan-lun selector candidates must have unique identities and symbols")
            identities.add(identity)
            symbols.add(symbol)

    if kind in {SchemaKind.PORTFOLIO, SchemaKind.RISK}:
        position_key = "target_positions" if kind is SchemaKind.PORTFOLIO else "approved_positions"
        cash_key = "cash_weight" if kind is SchemaKind.PORTFOLIO else "approved_cash_weight"
        allocated = float(output.get(cash_key, 0)) + sum(
            float(position.get("target_weight", 0)) for position in output.get(position_key, [])
        )
        if allocated > 1.000001:
            raise ValueError(f"Portfolio weights exceed 1, got {allocated}")


def _month_from_as_of(value: object) -> str:
    text = str(value or "")
    if len(text) < 7 or text[4:5] != "-":
        raise ValueError("Agent as_of_time must begin with YYYY-MM")
    return text[:7]
