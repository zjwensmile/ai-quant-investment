"""User-triggered broader research batches and reusable, dated company dossiers."""
import json
import uuid
from datetime import datetime
from pathlib import Path

from .closing_strategy import number
from .module_service import ModuleStore, now_iso


def quality_study_candidates(snapshot):
    """Study good reported operations even when today's price is not yet a bargain."""
    rows = []
    cutoff = datetime.fromisoformat(snapshot["as_of_time"])
    for item in snapshot.get("research_universe", []):
        security, f = item.get("security", {}), item.get("fundamentals") or {}
        if security.get("security_type") != "stock" or security.get("risk_warning") is True or "ST" in security.get("name", "").upper():
            continue
        if security.get("trading_status") != "tradable":
            continue
        try:
            observed = [datetime.fromisoformat(str(value)) for value in (f.get("available_at"), item.get("quote", {}).get("available_at"))]
            if cutoff.tzinfo is None or any(t.tzinfo is None or t > cutoff for t in observed):
                continue
            if any(str(f.get(k) or "9999")[:10] > cutoff.date().isoformat() for k in ("announcement_date", "report_period")):
                continue
        except (ValueError, TypeError):
            continue
        eps = number(f.get("earnings_per_share"), 0)
        roe = number(f.get("return_on_equity"), 0)
        growth = number(f.get("revenue_growth"), -999)
        profit_growth = number(f.get("net_profit_growth"), -999)
        cash = number(f.get("operating_cash_flow_per_share"), 0)
        finance = any(word in str(f.get("industry_name", "")) for word in ("银行", "保险", "证券"))
        if eps <= 0 or roe < 8 or growth < 0 or profit_growth < 0 or (not finance and cash < eps * .8):
            continue
        price = number(item.get("quote", {}).get("last"))
        if not price or price <= 0:
            continue
        score = round(min(roe, 30) + min(growth, 30) * .2 + min(profit_growth, 50) * .1 + (0 if finance else min(cash / eps, 2) * 5), 2)
        rows.append({**item, "price": price, "score": score, "eligible": True,
                     "research_source": "quality_watchlist", "reasons": ["最新已披露期ROE至少8%，收入利润同比为正", "先研究经营质量，买入还需独立证明安全边际"],
                     "risks": ["高ROE不等于长期护城河，需排除周期高点及一次性收益"],
                     "evidence_gaps": ["尚未确认现价低估或由大盘下跌导致", "ROE为披露期间值，未当成年化或跨年稳定指标"],
                     "metrics": {"reported_roe_percent": roe, "revenue_growth_percent": growth, "net_profit_growth_percent": profit_growth,
                                 "operating_cash_to_eps": None if finance else cash / eps}})
    return sorted(rows, key=lambda r: (-r["score"], r["symbol"]))


def build_expansion_scan(module, scan, snapshot, library, market_store, batch_size=12):
    from .etf_strategy import build_etf_candidates
    from .value_strategy import build_growth_candidates, build_value_candidates
    known = set(library.get("items", {})) if isinstance(library.get("items"), dict) else {r["symbol"] for r in library.get("items", [])}
    if module == "value":
        if scan.get("strategy") == "quality_growth_research":
            candidates = build_growth_candidates(
                snapshot, market_store, limit=80,
                priority_symbols=(scan.get("policy") or {}).get("priority_symbols", ()),
            )["candidates"]
        else:
            quality = quality_study_candidates(snapshot)
            pressured = build_value_candidates(snapshot, market_store, limit=80)["candidates"]
            candidates = list({r["symbol"]: r for r in [*pressured, *quality]}.values())
            candidates.sort(key=lambda r: (r["symbol"] in known, r.get("research_source") != "quality_watchlist", -r["score"], r["symbol"]))
    else:
        candidates = build_etf_candidates(snapshot, market_store, limit=60, policy=scan.get("policy") or {})["candidates"]
        candidates.sort(key=lambda r: (not r["eligible"], r["symbol"] in known, -r["score"], r["symbol"]))
    new_candidates = [r for r in candidates if r["eligible"] and r["symbol"] not in known][:batch_size]
    new_symbols = [r["symbol"] for r in new_candidates]
    held = {r["symbol"] for r in snapshot.get("account", {}).get("positions", [])
            if number(r.get("quantity"), 0) > 0} if module == "etf" else set()
    held_candidates = [r for r in candidates if r["symbol"] in held and r["symbol"] not in new_symbols]
    candidates = [*new_candidates, *held_candidates]
    new_symbols = [r["symbol"] for r in candidates if r["symbol"] not in known]
    result = {**scan, "scan_id": f"{module}-experiment-scan-{uuid.uuid4().hex[:12]}", "candidates": candidates,
              "new_research_symbols": new_symbols,
              "retained_held_symbols": [r["symbol"] for r in held_candidates if r["symbol"] in known],
              "experiment": "expanded_company_research", "parent_scan_id": scan["scan_id"], "completed_at": now_iso()}
    warning = ("扩展研究包含经营质量观察股；研究资格不等于现价低估。旧研究仅作有日期的背景，本轮仍核验新公告。"
               if module == "value" else "扩展尚未覆盖的沪深上市ETF；产品研究不代表已持仓或已批准调仓。规模、跟踪指数、折溢价和外盘条件仍须本轮核验。")
    result["quality"] = {"status": "research_only", "warnings": [warning]}
    return result


def run_research_experiment(module, settings, audit_store, market_store):
    from .module_library import publish_research_library
    from .module_pipeline import ModulePipeline
    from .runner import CodexRunner
    store = ModuleStore(settings.runtime_dir)
    scan = store.latest(module, "scan")
    if not scan:
        raise ValueError("请先手动扫描此模块")
    snapshot = json.loads(Path(scan["snapshot_path"]).read_text(encoding="utf-8"))
    if module == "etf":
        snapshot["account"] = store.account(module, audit_store)
    library = store.latest(module, "library") or {}
    # Include already finished, actually reviewed symbols even before first library migration.
    latest = store.latest(module, "analysis") or {}
    seen = set(latest.get("analysis_symbols", [])) if latest.get("status") == "completed" else set()
    entries = library.get("items", {})
    if isinstance(entries, list):
        entries = {r["symbol"]: r for r in entries}
    expansion = build_expansion_scan(module, scan, snapshot, {"items": {**entries, **{s: {} for s in seen}}}, market_store)
    experiment_id = f"{module}-experiment-{uuid.uuid4().hex[:12]}"
    report = {"kind": "expanded_company_research", "experiment_id": experiment_id, "status": "running", "as_of_time": scan["as_of_time"],
              "research_symbols": [r["symbol"] for r in expansion["candidates"]],
              "new_research_symbols": expansion["new_research_symbols"],
              "retained_held_symbols": expansion["retained_held_symbols"],
              "metrics": {"new_symbols": len(expansion["new_research_symbols"])},
              "warnings": expansion["quality"]["warnings"], "rounds": []}
    store.save(module, "experiment", report, experiment_id)
    if not expansion["new_research_symbols"]:
        report.update(status="completed", research_symbols=[], summary="已核验范围暂时没有新的合格研究标的；刷新扫描后可继续扩大资料覆盖")
    else:
        store.save(module, "scan", expansion, expansion["scan_id"])
        try:
            result = ModulePipeline(settings, store, audit_store, CodexRunner(settings.codex_command, settings.workspace, settings.agent_timeout_seconds)).run(module, expansion, snapshot)
            library = publish_research_library(store, module, result, expansion)
            retained_note = f"，并保留{len(expansion['retained_held_symbols'])}个现仓的估值与调仓讨论" if module == "etf" and expansion["retained_held_symbols"] else ""
            report.update(status="completed", summary=f"新增完成{len(expansion['new_research_symbols'])}个标的的六角色研究{retained_note}，已保存带来源及时间的研究档案", rounds=[{"run_id": result["run_id"], "symbols": result["analysis_symbols"], "status": result["status"]}])
        except Exception as exc:
            report.update(status="failed", summary=str(exc), warnings=report["warnings"] + ["本轮未通过完整验证的研究不升级为可沿用结论"])
            store.save(module, "experiment", report, experiment_id)
            raise
    report["completed_at"] = now_iso()
    store.save(module, "experiment", report, experiment_id)
    return report
