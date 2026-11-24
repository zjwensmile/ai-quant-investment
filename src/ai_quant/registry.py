from __future__ import annotations

from .domain import AgentSpec, AgentStage, SchemaKind


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "monthly_concept_selector",
        "月度概念配置 Agent",
        AgentStage.DISCOVERY,
        "gpt-5.6-sol",
        "high",
        SchemaKind.CONCEPTS,
        "每月独立选择3至5个值得持续研究的A股/ETF概念，综合宏观环境、产业周期、市场结构与风险，不追逐单日热点。",
        requires_web_research=True,
    ),
    AgentSpec(
        "chanlun_candidate_selector",
        "缠论候选选择 Agent",
        AgentStage.DISCOVERY,
        "gpt-5.6-sol",
        "medium",
        SchemaKind.CHANLUN_SELECTION,
        "只根据程序已提供的日线缠论信号、流动性、财务指标及月度背景安排研究优先级，"
        "20条以内全部进入两轮研究；大池最多选择20条，逐项记录未入选依据。"
        "本阶段不得联网或重复公司尽调；未提供的市场地位、行业景气及基本面明确待第一波专业Agent核验。"
        "排序不是买入审批，不得引入证券、信号或新的外部事实。",
        requires_web_research=False,
    ),
    AgentSpec(
        "macro_market",
        "宏观市场 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "high",
        SchemaKind.RESEARCH,
        "判断指数趋势、宏观政策环境和风险偏好，并逐一说明这些因素如何影响每只缠论候选及现有持仓。",
        requires_web_research=True,
    ),
    AgentSpec(
        "market_profitability",
        "赚钱效应 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "medium",
        SchemaKind.RESEARCH,
        "基于程序计算的成交额、宽度、强势股和板块扩散事实判断赚钱效应，并逐一对应缠论候选及持仓。",
    ),
    AgentSpec(
        "market_sentiment",
        "市场情绪 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "medium",
        SchemaKind.RESEARCH,
        "结合第一轮事实，对每只缠论候选及持仓的新闻和讨论做拥挤度、定价程度与退潮风险判断。",
        requires_web_research=True,
    ),
    AgentSpec(
        "theme_supply_chain",
        "题材与产业链 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "high",
        SchemaKind.RESEARCH,
        "结合第一轮事实，逐一核验缠论候选及持仓的产业链影响与扩散路径，并识别拥挤和追涨风险。",
        requires_web_research=True,
    ),
    AgentSpec(
        "fundamentals_disclosure",
        "基本面与公告 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "high",
        SchemaKind.RESEARCH,
        "逐一复核现有持仓和缠论候选的时点财报、估值、现金流、公告和经营稳定性。",
        None,
        True,
    ),
    AgentSpec(
        "news_event",
        "新闻事件 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "high",
        SchemaKind.NEWS,
        "逐一核验缠论候选和现有持仓的非公告新闻、来源、时效、定价程度与重复传播。",
        None,
        True,
    ),
    AgentSpec(
        "trend_swing",
        "趋势与波动 Agent",
        AgentStage.RESEARCH,
        "gpt-6-astra",
        "high",
        SchemaKind.RESEARCH,
        "结合第一轮事实，逐一审核缠论候选和持仓的趋势、波动、回撤、流动性和稳定性。",
        None,
    ),
    AgentSpec(
        "etf_rotation",
        "ETF 工具 Agent",
        AgentStage.RESEARCH,
        "gpt-5.6-sol",
        "medium",
        SchemaKind.RESEARCH,
        "结合第一轮事实，逐一审核缠论候选及持仓中的ETF是否适合作为核心、低波、行业或分散工具；对股票明确标注本角色不适用并给中性覆盖，不能把工具类型不适用当成投资看空证据。",
        None,
    ),
    AgentSpec(
        "contrarian_review",
        "反方审查 Agent",
        AgentStage.CHALLENGE,
        "gpt-6-astra",
        "high",
        SchemaKind.CHALLENGE,
        "针对高优先级机会寻找最强反证、共同来源和历史失败模式。",
    ),
    AgentSpec(
        "portfolio_controller",
        "组合主控 Agent",
        AgentStage.PORTFOLIO,
        "gpt-6-astra",
        "high",
        SchemaKind.PORTFOLIO,
        "比较现金、现仓和新机会，只从程序提供的可行组合方案中选择并解释。",
    ),
    AgentSpec(
        "risk_controller",
        "独立风控 Agent",
        AgentStage.RISK,
        "gpt-6-astra",
        "xhigh",
        SchemaKind.RISK,
        "独立审核组合情景风险，只能通过、缩减、暂缓或否决。",
    ),
    AgentSpec(
        "monthly_review",
        "月度复盘 Agent",
        AgentStage.REVIEW,
        "gpt-5.6-sol",
        "high",
        SchemaKind.MONTHLY_REVIEW,
        "进行策略、组合、风控和执行归因，提出有证据的版本变更建议。",
    ),
)


def all_agents() -> tuple[AgentSpec, ...]:
    return AGENTS


def agents_for_stage(stage: AgentStage) -> tuple[AgentSpec, ...]:
    return tuple(agent for agent in AGENTS if agent.stage is stage)


def get_agent(agent_id: str) -> AgentSpec:
    for agent in AGENTS:
        if agent.agent_id == agent_id:
            return agent
    raise KeyError(f"Unknown agent: {agent_id}")
