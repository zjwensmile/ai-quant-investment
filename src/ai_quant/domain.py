from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class AgentStage(StrEnum):
    DISCOVERY = "discovery"
    RESEARCH = "research"
    CHALLENGE = "challenge"
    PORTFOLIO = "portfolio"
    RISK = "risk"
    REVIEW = "review"


class SchemaKind(StrEnum):
    CONCEPTS = "concepts"
    CANDIDATE_POOL = "candidate_pool"
    CHANLUN_SELECTION = "chanlun_selection"
    RESEARCH = "research"
    NEWS = "news"
    CHALLENGE = "challenge"
    PORTFOLIO = "portfolio"
    RISK = "risk"
    MONTHLY_REVIEW = "monthly_review"
    MODULE_RESEARCH = "module_research"
    MODULE_PLAN = "module_plan"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display_name: str
    stage: AgentStage
    model: str
    reasoning_effort: str
    schema_kind: SchemaKind
    mission: str
    strategy: str = "MARKET"
    requires_web_research: bool = False

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["stage"] = self.stage.value
        data["schema_kind"] = self.schema_kind.value
        return data
