"""Explicit scans refresh market data without running the legacy full-history workflow."""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .closing_strategy import build_closing_candidates, fetch_closing_minutes, minute_check
from .datahub import DataHub, _quote, _symbol_set
from .features import (FeatureEngine, _daily_bar_cutoff, _compact_fundamentals,
                       _quote_with_derived_valuation, _breadth, _market_regime, _sector_diffusion)
from .module_service import ModuleStore, now_iso
from .providers import AkshareProvider
from .module_refresh import fetch_dated_quotes, refresh_etf_history
from .sessions import session_state


def progress(phase, completed, total):
    target = os.environ.get("AI_QUANT_TASK_PROGRESS_FILE")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"phase": phase, "current_stage": completed,
                                    "total_stages": total, "updated_at": now_iso()}, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    print(phase, flush=True)


def light_enrich(snapshot, settings, market_store):
    """Reuse only same-completed-session features with no later price/action revisions."""
    cutoff = _daily_bar_cutoff(snapshot)
    paths = sorted((settings.runtime_dir / "snapshots").glob("enriched-20*.json"), reverse=True)
    for path in paths[:5]:
        previous = json.loads(path.read_text(encoding="utf-8"))
        cached_at = previous.get("as_of_time", "")
        if (previous.get("purpose") != "live_decision" or datetime.fromisoformat(cached_at) > datetime.fromisoformat(snapshot["as_of_time"])
                or previous.get("feature_coverage", {}).get("daily_bar_cutoff") != cutoff.isoformat()
                or not previous.get("research_universe")):
            continue
        with market_store._connect() as db:
            price_revision = db.execute("SELECT 1 FROM raw_daily_bar_revisions WHERE julianday(available_at)>julianday(?) AND julianday(available_at)<=julianday(?) AND trade_date<=? LIMIT 1", (cached_at, snapshot["as_of_time"], cutoff.isoformat())).fetchone()
            action_revision = db.execute("SELECT 1 FROM corporate_actions WHERE julianday(available_at)>julianday(?) AND julianday(available_at)<=julianday(?) AND ex_date<=? LIMIT 1", (cached_at, snapshot["as_of_time"], cutoff.isoformat())).fetchone()
        if price_revision or action_revision:
            break
        fundamentals = market_store.fundamental_map_as_known(datetime.fromisoformat(snapshot["as_of_time"]).astimezone(ZoneInfo("Asia/Shanghai")).date(), snapshot["as_of_time"])
        securities = {r["symbol"]: r for r in snapshot["securities"]}
        positions = {r["symbol"]: r for r in snapshot["account"].get("positions", [])}
        rows = []
        for old in previous["research_universe"]:
            symbol = old["symbol"]
            if symbol not in securities:
                continue
            rows.append({**old, "security": securities[symbol], "held": symbol in positions,
                         "position": positions.get(symbol),
                         "quote": _quote_with_derived_valuation(snapshot["quotes"].get(symbol, {}), fundamentals.get(symbol)),
                         "fundamentals": _compact_fundamentals(fundamentals.get(symbol))})
        snapshot["research_universe"] = rows
        snapshot["feature_cache"] = {"source": str(path), "source_as_of": cached_at,
                                      "daily_cutoff": cutoff.isoformat(), "later_price_or_action_revisions": False}
        coverage = len(rows) / len(securities) if securities else 0
        breadth = _breadth(rows, len(securities))
        snapshot["market"].update(breadth)
        snapshot["market"].update(regime=_market_regime(breadth, coverage), sector_diffusion=_sector_diffusion(rows))
        snapshot["feature_coverage"] = {"daily_bar_cutoff": cutoff.isoformat(), "coverage_ratio": coverage,
                                        "symbols_with_history": len(rows), "eligible_securities": len(securities)}
        return snapshot
    return FeatureEngine(market_store, chanlun_mode="disabled").enrich(snapshot)


def scan_module(module, settings, audit_store, market_store):
    from .value_strategy import build_growth_candidates
    from .etf_strategy import build_etf_candidates, select_etf_research_candidates
    from .providers.etf_facts import fetch_etf_facts, select_etf_fact_symbols
    store = ModuleStore(settings.runtime_dir)
    progress("读取当次市场行情与交易日历", 1, 4)
    account = store.account(module, audit_store)
    snapshot = DataHub(AkshareProvider()).build_snapshot(datetime.now(ZoneInfo("Asia/Shanghai")), account=account,
                                                       include_growth_boards=module == "value").data
    market_store.apply_etf_attributes(snapshot)
    market_store.upsert_security_master(snapshot["snapshot_id"], snapshot["securities"], snapshot["data_quality"]["available_at"],
                                        deactivate_missing=False)
    progress("读取本地已知日线与财报，不运行全历史补数", 2, 4)
    if module == "etf":
        selected = set(select_etf_fact_symbols(snapshot, limit=30))
        securities = [r for r in snapshot["securities"] if r["symbol"] in selected]
        progress("仅更新ETF候选日线增量及整段复权窗口", 2, 4)
        refresh_etf_candidate_inputs(snapshot, securities, market_store)
    elif module == "value":
        # Company valuation does not need whole-market daily-history calculations.
        stamp = snapshot["as_of_time"]
        day = datetime.fromisoformat(stamp).date()
        fundamentals = market_store.fundamental_map_as_known(day, stamp)
        industries = market_store.primary_industry_map(day, stamp)
        snapshot["research_universe"] = [
            {"symbol": security["symbol"], "security": security,
             "quote": _quote_with_derived_valuation(snapshot["quotes"].get(security["symbol"], {}), fundamentals.get(security["symbol"])),
             "fundamentals": _compact_fundamentals(fundamentals.get(security["symbol"])),
             "industry": industries.get(security["symbol"], {})}
            for security in snapshot["securities"] if security["security_type"] == "stock"]
    else:
        snapshot = light_enrich(snapshot, settings, market_store)
    snapshot["market"]["daily_feature_cutoff"] = _daily_bar_cutoff(snapshot).isoformat()
    snapshot["market"]["daily_feature_usage"] = "breadth/regime与均线、历史收益来自已完成日线；当次市场涨跌分布见all_stock_distribution，不得混为实时指标"
    if module == "etf":
        progress("核验候选ETF实际基金规模与跟踪标的", 3, 4)
        snapshot["market"]["daily_feature_usage"] = "仅候选ETF：收益和波动采用本次整段供应商前复权窗口，成交额来自原始日线；截止日见history_as_of_date，复权或覆盖缺口不生成特征。"
        snapshot["etf_facts"] = fetch_etf_facts([row["symbol"] for row in securities], workers=3,
                                               cache_path=store.directory / "etf" / "facts-cache.json")
        _freeze_after_refresh(snapshot)
        scan = build_etf_candidates(snapshot, market_store, limit=20, policy=store.latest("etf", "policy") or {})
        from .providers.etf_evidence import enrich_etf_evidence
        held = {p["symbol"] for p in account.get("positions", []) if p.get("quantity", 0) > 0}
        discussion = select_etf_research_candidates(scan["candidates"], limit=12, held_symbols=held)
        snapshot = enrich_etf_evidence(snapshot, [row["symbol"] for row in discussion])
        _freeze_after_refresh(snapshot)
        scan = build_etf_candidates(snapshot, market_store, limit=20, policy=store.latest("etf", "policy") or {})
        scan.update(as_of_time=snapshot["as_of_time"], universe_count=scan["screened_count"],
                    quality={"status": "research_only", "warnings": ["规模优先核验活跃及代表ETF；未核验品种不冒充通过。跨境休市/溢价缺口限制执行，不阻止条件研究。"]},
                    methodology=["基金净资产至少10亿元，完整交易日成交额至少1000万元（早盘另核验），规模报告不超过120天", "不配置债券及货币类ETF；优先中国、香港、标普、纳指及不同行业", "同跟踪指数去重；总仓在冻结policy上下限内动态决定，单只不超过35%、单市场不超过60%，获批组合至少覆盖两个明确权益市场", "保留核心仓，只在扣除双边费用及滑点后的价差为正时讨论做T"],
                    sources=[{"title": "上交所ETF交易与T+0/T+1规则", "url": "https://www.sse.com.cn/assortment/fund/etf/question/"}])
        attach_etf_candidate_data(scan, snapshot)
    elif module == "value":
        policy = {"priority_symbols": [],
                  **(store.latest("value", "policy") or {}), "fundamental_valuations_required": True}
        scan = build_growth_candidates(snapshot, market_store, priority_symbols=policy["priority_symbols"])
        selected = {row["symbol"] for row in scan["candidates"]}
        _refresh_candidate_quotes(snapshot, [row for row in snapshot["securities"] if row["symbol"] in selected])
        _freeze_after_refresh(snapshot)
        scan = build_growth_candidates(snapshot, market_store, priority_symbols=policy["priority_symbols"])
        scan["policy"] = policy
        scan["candidate_quote_refresh"] = snapshot["candidate_quote_refresh"]
        snapshot["market"]["daily_feature_usage"] = "本轮公司研究依据财报与市场报价，不计算或使用均线作为价值买入定价依据"
    else:
        scan = build_closing_candidates(snapshot, market_store)
    scan_id = f"{module}-scan-{uuid.uuid4().hex[:12]}"
    directory = store.directory / module
    directory.mkdir(exist_ok=True)
    snapshot_path = directory / f"{scan_id}.snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    scan.update(scan_id=scan_id, snapshot_path=str(snapshot_path), completed_at=now_iso())
    store.save(module, "scan", scan, scan_id)
    store.save_quote_reference(module, snapshot)
    progress("候选扫描已保存，可手动开始Agent讨论", 4, 4)
    return scan


def refresh_etf_candidate_inputs(snapshot, securities, market_store):
    """Retry only explicit candidates; never scan the market or publish a scan."""
    snapshot["candidate_history_refresh"] = refresh_etf_history(snapshot, securities, market_store)
    _refresh_candidate_quotes(snapshot, securities)
    history = snapshot["candidate_history_refresh"]["results"]
    snapshot["research_universe"] = [
        {"symbol": row["symbol"], "name": row["name"], "security_type": "etf", "security": row,
         "quote": snapshot["quotes"].get(row["symbol"], {}),
         "features": history.get(row["symbol"], {}).get("features", {}),
         "history_refresh": {key: value for key, value in history.get(row["symbol"], {}).items() if key != "features"}}
        for row in securities]
    _freeze_after_refresh(snapshot)
    return snapshot


def attach_etf_candidate_data(scan, snapshot):
    contexts = {r["symbol"]: r for r in snapshot.get("research_universe", [])}
    for row in scan["candidates"]:
        row["features"] = contexts.get(row["symbol"], {}).get("features", {})
        row["history_refresh"] = contexts.get(row["symbol"], {}).get("history_refresh", {})
        row["quote"] = snapshot["quotes"].get(row["symbol"], {})
    scan["candidate_history_refresh"] = snapshot["candidate_history_refresh"]
    scan["candidate_quote_refresh"] = snapshot["candidate_quote_refresh"]
    return scan


def _refresh_candidate_quotes(snapshot, securities):
    refreshed = fetch_dated_quotes(securities)
    snapshot["quotes"].update(refreshed["quotes"])
    snapshot["candidate_quote_refresh"] = {"requested_symbols": [row["symbol"] for row in securities],
        "updated_symbols": sorted(refreshed["quotes"]), "errors": refreshed["errors"],
        "completed_at": refreshed["completed_at"], "source": "Tencent dated quote update"}
    snapshot["data_quality"]["quotes_without_source_date"] = sum(not q.get("source_trade_date") for q in snapshot["quotes"].values())


def _freeze_after_refresh(snapshot):
    previous = datetime.fromisoformat(snapshot["as_of_time"])
    current = max(previous, datetime.fromisoformat(now_iso()))
    snapshot.setdefault("market_observed_at", snapshot["as_of_time"])
    snapshot["as_of_time"] = current.isoformat(timespec="seconds")
    market = snapshot["market"]
    if previous.date() != current.date():
        market.update(execution_eligible=False, session="unknown", is_trading_day=None)
        snapshot["data_quality"]["warnings"].append("候选刷新跨日期，行情日历须重新读取后才可执行；保留原完整交易日研究数据")
    else:
        state = session_state(current, market.get("is_trading_day") is True)
        market["execution_eligible"] = bool(market.get("execution_eligible") and state.execution_eligible)
        if market.get("is_trading_day") is not None:
            market["session"] = state.session.value


def refresh_closing(settings, audit_store, market_store):
    store = ModuleStore(settings.runtime_dir)
    scan, analysis = store.latest("closing", "scan"), store.latest("closing", "analysis")
    if not scan:
        raise ValueError("请先扫描尾盘准备池")
    progress("并发读取准备池最新分钟数据", 1, 3)
    candidates = scan["candidates"][:12]
    minutes, errors = fetch_closing_minutes(candidates)
    provider = AkshareProvider()
    rows = provider.fetch_stock_spot()
    risk_rows = provider.fetch_risk_warning()
    suspension_rows = provider.fetch_suspensions(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    calendar = provider.fetch_trading_calendar()
    checked_at = now_iso()
    today = datetime.fromisoformat(checked_at).date()
    quotes = {}
    for r in rows:
        code = str(r.get("代码") or r.get("symbol") or r.get("code") or "").removeprefix("sh").removeprefix("sz").zfill(6)
        quotes[code] = _quote(r, checked_at)
    dates = {str(r.get("trade_date") or r.get("date"))[:10] for r in calendar}
    is_day = today.isoformat() in dates and provider.trading_calendar_complete
    progress("复核当日时窗、量价、原方案与账户版本", 2, 3)
    checks = [minute_check({**c, "quote": quotes.get(c["symbol"], {})}, minutes.get(c["symbol"], []), checked_at, is_day) for c in candidates]
    account = store.account("closing", audit_store)
    approved_positions = ((analysis or {}).get("decision") or {}).get("positions", [])
    fee = float(settings.commission_fixed_per_order or settings.commission_minimum)
    required_cash = sum(p.get("delta_quantity", 0) * p["entry_max"] + fee for p in approved_positions if p.get("delta_quantity", 0) > 0)
    for check in checks:
        if not account.get("cash_confirmed") or required_cash > float(account.get("cash") or 0):
            check["blockers"].append("尾盘独立账户现金未确认或不足以覆盖全部新增买入及费用")
        if not provider.risk_warning_complete or not provider.suspension_complete:
            check["blockers"].append("风险警示或停牌名单尚未完整核验")
        if check["symbol"] in _symbol_set(risk_rows) | _symbol_set(suspension_rows):
            check["blockers"].append("标的属于风险警示或停牌名单")
        plan = next((p for p in (analysis or {}).get("decision", {}).get("positions", []) if p["symbol"] == check["symbol"]), None) if analysis and analysis.get("decision") else None
        if not plan or analysis.get("status") != "completed" or analysis["decision"]["status"] != "approve":
            check["blockers"].append("尚无已批准的本标的Agent方案")
        else:
            quantity = plan.get("delta_quantity", 0)
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0 or quantity % 100:
                check["blockers"].append("批准方案没有可执行的整手新增买入数量")
            price = check["metrics"].get("price", 0)
            if not plan["entry_min"] <= price <= plan["entry_max"]:
                check["blockers"].append("最新价格不在批准买入区间")
            if datetime.fromisoformat(plan["expires_at"]) <= datetime.fromisoformat(checked_at):
                check["blockers"].append("原方案已经过期")
            if account.get("version") != analysis.get("account_version"):
                check["blockers"].append("账户事实已变化，需重新讨论")
            if scan["scan_id"] != analysis.get("scan_id"):
                check["blockers"].append("扫描候选已更新，需对新扫描重新讨论")
            if today.isoformat() != analysis["as_of_time"][:10]:
                check["blockers"].append("隔夜事件研究非当日，请先完成当日讨论")
        check["signal_confirmed"] = not check["blockers"]
    result = {"checked_at": checked_at, "checks": checks, "errors": errors,
              "account_version": account.get("version"), "scan_id": scan["scan_id"],
              "automatic_order_submission": False}
    directory = store.directory / "closing"
    (directory / f"minutes-{uuid.uuid4().hex[:12]}.json").write_text(json.dumps({"observed_at": checked_at, "minutes": minutes, "errors": errors}, ensure_ascii=False, default=str), encoding="utf-8")
    store.save("closing", "refresh", result)
    progress("尾盘复核完成，实际成交仍由券商确认", 3, 3)
    return result


def run_module_action(module, action, settings, audit_store, market_store):
    if action == "experiment":
        if module != "closing":
            from .module_experiments import run_research_experiment
            return run_research_experiment(module, settings, audit_store, market_store)
        from .closing_experiments import run_closing_experiments
        store = ModuleStore(settings.runtime_dir)
        scan = store.latest(module, "scan")
        if not scan:
            raise ValueError("请先手动扫描尾盘准备池")
        snapshot = json.loads(Path(scan["snapshot_path"]).read_text(encoding="utf-8"))
        result = run_closing_experiments(snapshot, scan, store.directory / module / "experiments", market_store=market_store)
        store.save(module, "experiment", result)
        return result
    if action == "scan":
        return scan_module(module, settings, audit_store, market_store)
    if module == "closing" and action == "refresh":
        return refresh_closing(settings, audit_store, market_store)
    if action != "analyze":
        raise ValueError("不支持的模块动作")
    from .module_pipeline import ModulePipeline
    from .runner import CodexRunner
    store = ModuleStore(settings.runtime_dir)
    scan = store.latest(module, "scan")
    if scan is None:
        raise ValueError("请先手动扫描候选")
    snapshot = json.loads(Path(scan["snapshot_path"]).read_text(encoding="utf-8"))
    progress("启动专业Agent研究，再交叉质询、组合决策、风险裁决", 1, 2)
    result = ModulePipeline(settings, store, audit_store, CodexRunner(settings.codex_command, settings.workspace, settings.agent_timeout_seconds)).run(module, scan, snapshot)
    progress("六位Agent讨论与确定性约束校验完成", 2, 2)
    return result
