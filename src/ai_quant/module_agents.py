"""Independent research mandates; these agents never join the legacy daily workflow."""
from dataclasses import replace
from .domain import AgentSpec, AgentStage, SchemaKind
from .schemas import EVIDENCE_ITEM


def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
NUMBER = {"type": "number"}
STRINGS = {"type": "array", "items": TEXT}
NULLABLE_NUMBER = {"type": ["number", "null"]}
NULLABLE_TEXT = {"type": ["string", "null"]}
VALUATION_SCENARIO = obj({
    "per_share_metric": NULLABLE_NUMBER, "multiple": NULLABLE_NUMBER,
    "fair_price": NULLABLE_NUMBER, "assumptions": TEXT, "evidence_refs": STRINGS,
})
VALUATION = obj({
    "symbol": {"type": "string", "pattern": "^[0-9]{6}$"}, "name": TEXT,
    "report_period": NULLABLE_TEXT, "basis": TEXT,
    "method": {"type": "string", "enum": ["normalized_pe", "forward_pe", "p_fcf", "insufficient_evidence"]},
    "scenarios": obj({key: VALUATION_SCENARIO for key in ("bear", "base", "bull")}),
    "fair_value_min": NULLABLE_NUMBER, "fair_value_max": NULLABLE_NUMBER,
    "margin_of_safety": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    "entry_min": NULLABLE_NUMBER, "entry_max": NULLABLE_NUMBER, "buy_condition": TEXT,
    "take_profit": NULLABLE_NUMBER, "stop_loss": NULLABLE_NUMBER,
    "invalidation": TEXT, "expires_at": NULLABLE_TEXT, "evidence_gaps": STRINGS,
})
FINDING = obj({
    "symbol": {"type": ["string", "null"]},
    "verdict": {"type": "string", "enum": ["support", "oppose", "conditional", "unknown"]},
    "summary": TEXT, "buy_condition": TEXT, "invalidation": TEXT,
    "reply_to": STRINGS,
    "supporting_evidence": {"type": "array", "items": EVIDENCE_ITEM},
    "opposing_evidence": {"type": "array", "items": EVIDENCE_ITEM},
    "evidence_gaps": STRINGS,
})
MODULE_RESEARCH_SCHEMA = obj({
    "agent_id": TEXT, "as_of_time": TEXT, "summary": TEXT,
    "findings": {"type": "array", "items": FINDING}, "disagreements": STRINGS,
    "follow_up_requests": {"type": "array", "maxItems": 3, "items": obj({
        "symbol": {"type": "string", "pattern": "^[0-9]{6}$"}, "researcher_id": TEXT,
        "question": TEXT, "decision_impact": TEXT,
        "required_sources": {"type": "array", "minItems": 1, "items": TEXT},
    })},
})
PLAN_POSITION = obj({
    "symbol": {"type": "string", "pattern": "^[0-9]{6}$"}, "name": TEXT,
    "target_weight": {"type": "number", "minimum": 0, "maximum": 1},
    "entry_min": NUMBER, "entry_max": NUMBER, "take_profit": NUMBER,
    "stop_loss": NUMBER, "expires_at": TEXT, "reason": TEXT,
    "invalidation": TEXT, "evidence_refs": STRINGS,
})
MODULE_PLAN_SCHEMA = obj({
    "agent_id": TEXT, "as_of_time": TEXT,
    "status": {"type": "string", "enum": ["approve", "defer"]},
    "summary": TEXT,
    "positions": {"type": "array", "items": PLAN_POSITION},
    "valuations": {"type": "array", "items": VALUATION},
    "cash_weight": {"type": "number", "minimum": 0, "maximum": 1},
    "risks": STRINGS, "discussion_resolution": STRINGS,
    "exit_plan": obj({"high_open": TEXT, "flat_open": TEXT, "low_open": TEXT,
                      "time_exit": TEXT, "unfilled_or_halted": TEXT}),
    "t_suggestions": {"type": "array", "items": obj({
        "symbol": TEXT, "buy_price": NUMBER, "sell_price": NUMBER, "stop_loss": NUMBER,
        "expires_at": TEXT, "reason": TEXT,
    })},
})

MANDATES = {
    "value": [
        ("business", "成长潜力与商业质量研究员", "gpt-5.6-sol", "发现尚未完全兑现的成长潜力：产品渗透、新客户导入、订单履约转收入、产品竞争力、竞争壁垒与定价权、产能投资回报。以公告、客户和行业需求证据区分可兑现增长与叙事，列催化、时间路径及失败情景；合同不等于收入，目标不等于已经兑现，历史高同比不能直接称为市场尚未发现。不能等未来增长全部兑现才研究，也不能把高增速当质量保证；回应候选research_questions中的具体业务问题。"),
        ("accounts", "财报与增长兑现研究员", "gpt-5.6-sol", "以同口径财报为锚，分析新增收入转化为毛利、利润和经营现金流的路径、营运资本与融资需求；区分扩张投入和不可持续亏损。金融企业用行业口径，区分年化YTD PE与TTM；缺多年数据、ROIC、负债时明确缺口，不假定稳定。"),
        ("valuation", "市场预期与估值研究员", "gpt-6-astra", "比较当前市场价格隐含预期与可解释增长情景，识别尚未计价的潜力及过度乐观定价。基于财报、需求兑现和同业/历史倍数给合理价值、入手条件、催化及失效条件，休市仍可提出限价条件计划。"),
    ],
    "closing": [
        ("tape", "尾盘量价研究员", "gpt-5.6-sol", "用已观测分钟数据核验VWAP、相对强弱、近高位置和成交额。区分前日准备池与今日14:45-14:55确认。缺分钟数据不可把日K当尾盘信号。"),
        ("events", "隔夜事件研究员", "gpt-5.6-sol", "核验公告、减持解禁、财报、监管及海外风险的隔夜窗口，关注利好已兑现。查未来已公布日程，不使用截点之后发生的事实。"),
        ("overnight", "次日退出研究员", "gpt-6-astra", "评估次日高开/平开/低开及无法成交情景，考虑手续费、印花税和滑点。A股T+1不能当日止损，次日高开绝非保证，无分钟历史就没有已验证胜率。"),
    ],
    "etf": [
        ("macro", "行业景气与全球宏观研究员", "gpt-5.6-sol", "主动寻找中国、香港、美国的高景气行业：核验订单或终端需求、产品价格与库存、产能利用率、收入利润修订、现金回报、政策及催化兑现时间。区分景气上行、复苏、见顶与衰退，比较当前定价已反映多少增长；短期涨幅、新闻热度和低基数同比不能单独证明高景气。结合周期、利率、汇率、国际事件与最新可得美股/港股表现，给候选行业相对排序、支持和反对证据及失效指标。分清海外休市与时差，不把过期净值作实时IOPV。"),
        ("products", "ETF产品与流动性研究员", "gpt-5.6-sol", "核验实际基金规模及日期、跟踪指数、成交额/价差/折溢价、费率与跟踪误差。仅沪深上市ETF，避免同指数重复暴露。交易所T+0属性未知不能推断。"),
        ("allocation", "周期配置与做T研究员", "gpt-6-astra", "10万元长期投资规划，总仓位从冻结scan.policy.etf_min_weight/etf_max_weight约束内动态选择，etf_target_weight仅软参考，须用周期、估值、趋势与事件解释本轮总仓及增减仓触发。不能惯性全现金。用实际波动、回撤与底层资产判断风险，行业及跨境ETF不因ETF名称就自动低波动。做T仅小卫星仓，保留核心；无持仓不能编造可卖库存。"),
    ],
}


def module_agents(module):
    agents = [AgentSpec(f"{module}_{key}", name, AgentStage.RESEARCH, model, "medium",
                        SchemaKind.MODULE_RESEARCH, mission, requires_web_research=True)
              for key, name, model, mission in MANDATES[module]]
    agents += [
        AgentSpec(f"{module}_challenge", "交叉质询与反证", AgentStage.CHALLENGE,
                  "gpt-5.6-sol", "medium", SchemaKind.MODULE_RESEARCH,
                  "阅读所有专业观点，逐条指出相互矛盾、证据不足与价值陷阱，reply_to引用被质询agent_id。给出可解决分歧的条件，不能只重复摘要。"),
        AgentSpec(f"{module}_portfolio", "买点与组合决策", AgentStage.PORTFOLIO,
                  "gpt-6-astra", "high", SchemaKind.MODULE_PLAN,
                  "综合专业观点及反证，discussion_resolution逐项回应争议。明确何价买/何时等待/止盈止损/有效期。只用给定候选与账户约束。非价值模块valuations为空数组。"),
        AgentSpec(f"{module}_risk", "独立风险裁决", AgentStage.RISK,
                  "gpt-6-astra", "medium", SchemaKind.MODULE_PLAN,
                  "复核正式组合提案与原始证据。相对portfolio正式提案不得新增股票/扩大仓位/放宽止损/延长有效期；专业研究员尚待裁决的成套价格与止损只是建议。ETF总仓必须符合冻结scan.policy仓位上下限；无法批准就defer并说明修正，不伪装成全现金建议。非价值模块valuations为空数组。"),
    ]
    if module == "etf":
        mandate = (" 本账户以高景气行业争取超额收益，并保留中国、香港、美国核心配置。"
                   "行业配置是主动收益目标，不是仅进入候选讨论。以冻结scan.policy.sector_target_weight作为行业ETF合计仓位的软参考，"
                   "按本轮证据增减，既无强制最低行业仓位，也不新增固定5%的行业仓位上限。优先比较2至4个不同盈利驱动的行业，候选不足明确缺口。"
                   "宏观研究提出景气排序，产品研究核验指数是否真实表达该行业、成份集中和规模流动性，配置研究比较行业组合与宽基基线的预期收益来源、回撤情景和机会成本。"
                   "同一行业与宽基部分重合可作为有意超配，须量化已披露重合与未知部分、合并科技等共同风险后决定权重，不能因重合或波动大一律否决。"
                   "组合discussion_resolution必须逐一说明候选行业的景气证据、估值与拥挤风险、目标权重（不配则0）、加减仓及退出触发，并汇总行业仓位与软参考的偏离原因。"
                   "有证据支持时在positions给出行业ETF具体配置；全行业零配置必须说明各主要候选的实质反证或可补缺口，不能以宽基更稳健代替判断。"
                   "行业ETF分类用于产品配置，纳指/科创50等成长宽基不能冒充专门行业仓位，仍须合并底层行业暴露；高景气不等于任何价格都值得买，也不能承诺高收益。"
                   "历史全宽基结论及临时5%建议是旧背景，不是本轮硬约束；风险角色仍只能复核本轮正式portfolio，不能自行新增行业仓位。"
                   "禁止新增全部债券类ETF，"
                   "包括国债、地方债、政策性金融债、信用债、公司债、短融、可转债、科创债；货币/现金类ETF也不配置。"
                   "已有禁品只可估值、持有或减持，不可加仓或做T买回，不能为凑目标仓位突破禁令。"
                   "先遵守冻结scan.policy.excluded_equity_regions：被用户排除的底层市场不得新增配置，也不得为全球分散硬凑；有旧仓只讨论其风险与减持，不加仓或做T买回。"
                   "在用户允许的市场比较A股、美股及其他已给定海外市场；港股仅在未被排除时参与配置，纳斯达克和标普500均须说明是否配置及理由。"
                   "纳指与标普成份重合需合并风险，不能把多只同市场ETF当作均衡全球配置。海外时差或暂缺IOPV仅限制执行，"
                   "不是永久排除海外资产的理由；可以给出溢价、估值、汇率和事件约束下的条件计划。"
                   "ETF长期配置入场价属于执行风险参数，不等同个股内在公允价值；可基于真实冻结收盘价、可解释的追价限额，"
                   "结合下一交易日实时IOPV、买卖价差及折溢价条件设定入场区间，说明价格锚日期和限额依据。"
                   "尚未发生的开盘数据应写成触发前必须核验的条件，不得假称已经取得；不能以休市无实时IOPV或账户未确认单独defer。"
                   "真实缺失产品规模、确切跟踪指数或底层市场穿透证据，或触犯禁债及仓位约束，仍不能绕过。"
                   "证据支持时允许approve条件配置；条件无法得到可核验依据或存在未解决的实质风险时仍可defer，不得为获得批准而降低标准。"
                   "比较中国、香港、美国宽基及行业组合，在discussion_resolution记录各市场取舍、动态总仓理由和增减仓触发。"
                   "exit_plan逐项写明下一交易日高开/平开/低开触发、限价与不成交处理；休市输出条件计划，不能把条件当已触发。"
                   "组合与风险输出valuations为空数组。")
        agents = [replace(agent, mission=agent.mission + mandate) for agent in agents]
    elif module == "value":
        mandate = (" 逐只研究给定候选的成长潜力与商业质量，不默认优先热门公司；示例公司不代表用户点名或质量背书，不能因高PE或短期亏损直接等同价值陷阱。"
                   "合理价值和可买价必须基于财报与市场定价，禁止用均线、技术支撑或简单现价折扣代替估值。"
                   "核验财报期、TTM/YTD/正常化或预测期及稀释股本，预测必须区分事实与假设；比较同业及自身历史估值并注明来源和日期。"
                   "送转股、增发或A/H股本变化时，不得直接拼接不同股本口径的历史EPS；优先以可比归母利润和核验过的相应总股本复算每股指标，报告期加权股本不冒充当下股本。"
                   "未来业绩是可解释假设，不要求预先实现；有财报锚、需求依据和可比倍数支持即可提出条件买入，不能因预测有不确定性一概defer。"
                   "各情景解释增长、盈利兑现、倍数和安全边际的经济依据与敏感性，禁止无依据统一八折。"
                   "研究findings用证据ID说明熊/基准/牛每股盈利或自由现金流及倍数，财报和倍数均须有可追溯证据；缺证据明确未知。"
                   "组合与风险必须在valuations逐只保留所有给定候选的估值研究，与positions独立，defer和不建仓也不得删掉研究。"
                   "valuations.report_period用财报截止日期YYYY-MM-DD，不得晚于研究截止；basis注明人民币每股、历史财报口径及未来预测期。"
                   "method用normalized_pe/forward_pe/p_fcf，无依据用insufficient_evidence。"
                   "scenarios的bear/base/bull各给per_share_metric、multiple、fair_price、assumptions和evidence_refs，"
                   "fair_price=per_share_metric×multiple并四舍五入到最小价位；fair_value_min/max取熊/牛价格；entry_max=基准fair_price×(1-margin_of_safety)向下取最小价位。"
                   "各情景evidence_refs使用真实agent_id:evidence_id且包含本标的证据，开仓估值至少有一项有日期的公司或官方原始证据锚。"
                   "PE的EPS须为正，无法使用PE时可核验每股自由现金流，仍无可靠依据则数字及财报期/有效期可null并列evidence_gaps，禁止捏造。"
                   "充分财报与定价证据时逐股给entry_min至entry_max分批介入研究区间，buy_condition解释下限对应的估值情景与经济依据，禁止用0、均线、任意现价折扣或机械99%拼凑区间；无法论证下限就null并明确缺口。"
                   "下限表示本次研究价带，价格低于下限时复核新增事件与基本面，不代表更便宜就永远不买。"
                   "说明buy_condition、估值止盈take_profit、风险止损stop_loss、基本面invalidation及expires_at；完整区间须满足stop_loss<entry_min<=entry_max<take_profit，研究价不自动成为批准买单。")
        agents = [replace(agent, mission=agent.mission + mandate) for agent in agents]
    if module in {"value", "etf"}:
        planning = (" 研究批准与账户执行分离：账户净值、入金或权限未确认时，仍按已知budget/capital_budget/planned_capital_cny形成具体条件研究配置，明确资金为规划假设。"
                    "不得仅因账户净值未知而defer或停止估值；真实执行另须确认现金、持仓、权限和报价。"
                    "高单价先减少股数并遵守交易最低单位；预算不足最低单位则数量为0、说明最低资金需求和执行阻碍，仍保留合理价值与入手条件，不因此否定公司研究。")
        agents = [replace(agent, mission=agent.mission + planning) for agent in agents]
    return agents


def follow_up_agent(module, researcher_id):
    specialist = next((agent for agent in module_agents(module)[:3]
                       if agent.agent_id == researcher_id), None)
    if specialist is None:
        raise ValueError("补证只能交给本模块已有专业研究角色")
    return replace(specialist, agent_id=researcher_id + "_recheck",
                   display_name=specialist.display_name + " · 定向补证",
                   mission=specialist.mission + " 本轮仅回答分配的关键补证问题，不重复全量选股或完整三情景估值。明确已解决/未解决及来源失败，不能再发起补证请求。")
