"""Durable, dated module research; archived price plans never authorize execution."""

from copy import deepcopy
from datetime import datetime, timedelta

from .evidence import validate_agent_evidence
from .module_agents import follow_up_agent, module_agents
from .module_service import now_iso


def _time(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Research-library timestamps require a timezone")
    return result


def _formal_provenance(provenance):
    return (provenance.get("run_kind") == "formal"
            and provenance.get("snapshot_purpose") == "live_decision"
            and provenance.get("accepts_test_snapshots") is False)


def _formal(analysis, module, scan):
    provenance = analysis.get("provenance", {})
    if (analysis.get("status") != "completed" or analysis.get("module") != module
            or not _formal_provenance(provenance)):
        raise ValueError("Only completed real formal module research may enter the library")
    if not analysis.get("run_id") or analysis.get("as_of_time") != scan.get("as_of_time"):
        raise ValueError("Research run and scan identity/time must agree")
    if not scan.get("scan_id") or scan.get("module", module) != module or analysis.get("scan_id") != scan.get("scan_id"):
        raise ValueError("Research library cannot mix modules or source scans")
    as_of = _time(analysis["as_of_time"])
    if _time(analysis.get("completed_at")) < as_of:
        raise ValueError("Research cannot complete before its source cutoff")
    specifications = {agent.agent_id: agent for agent in module_agents(module)}
    expected = set(specifications)
    outputs = analysis.get("research", [])
    if len(outputs) != len(expected) or {item.get("agent_id") for item in outputs} != expected:
        raise ValueError("A formal library review requires all six module roles")
    symbols = analysis.get("analysis_symbols", [])
    candidates = {item["symbol"] for item in scan.get("candidates", [])}
    if len(symbols) != len(set(symbols)) or not set(symbols) <= candidates:
        raise ValueError("Research symbols must be unique members of the source scan")
    for item in outputs:
        if item.get("status") != "completed" or item.get("model") not in {"gpt-6-astra", "gpt-5.6-sol"}:
            raise ValueError("Incomplete or unsupported-model output cannot enter the library")
        if item.get("as_of_time") != analysis["as_of_time"]:
            raise ValueError("Each role must preserve the original research cutoff")
        if item.get("stage") != specifications[item["agent_id"]].stage.value:
            raise ValueError("Stored role stage must match its actual module role")
        if _time(item.get("completed_at") or analysis["completed_at"]) < as_of:
            raise ValueError("Role completion cannot precede its research cutoff")
        if item.get("stage") in {"research", "challenge"}:
            findings = item.get("findings", [])
            covered = [f["symbol"] for f in findings if f.get("symbol") is not None]
            if len(covered) != len(symbols) or set(covered) != set(symbols):
                raise ValueError("Each research role must cover the complete reviewed batch")
            validate_agent_evidence({"as_of_time": analysis["as_of_time"], "decisions": findings}, analysis["as_of_time"])
    _validate_supplemental(analysis, module, symbols)
    return symbols


def _validate_supplemental(analysis, module, symbols):
    challenge = next(item for item in analysis["research"] if item["stage"] == "challenge")
    requests = challenge.get("follow_up_requests", [])
    outputs = analysis.get("supplemental_research", [])
    if len(requests) > 3:
        raise ValueError("A review may ask at most three targeted evidence questions")
    expected = {}
    for question in requests:
        spec = follow_up_agent(module, question["researcher_id"])
        if question["symbol"] not in symbols:
            raise ValueError("Supplemental question is outside the reviewed batch")
        expected.setdefault(spec.agent_id, []).append(question)
    if len(outputs) != len(expected) or {item.get("agent_id") for item in outputs} != set(expected):
        raise ValueError("Every assigned supplemental review must be completed")
    for item in outputs:
        questions = expected[item["agent_id"]]
        parent = questions[0]["researcher_id"]
        spec = follow_up_agent(module, parent)
        requested = list(dict.fromkeys(q["symbol"] for q in questions))
        covered = [f.get("symbol") for f in item.get("findings", []) if f.get("symbol") is not None]
        if (item.get("status") != "completed" or item.get("model") != spec.model
                or item.get("stage") != "research" or item.get("parent_agent_id") != parent
                or item.get("assigned_questions") != questions or item.get("requested_symbols") != requested
                or len(covered) != len(requested) or set(covered) != set(requested)
                or item.get("follow_up_requests") or not item.get("request_sha256")
                or not item.get("output_path") or not item.get("trace_path")):
            raise ValueError("Supplemental evidence identity, scope or provenance is invalid")
        if (item.get("as_of_time") != analysis["as_of_time"]
                or _time(item.get("completed_at")) < _time(analysis["as_of_time"])):
            raise ValueError("Supplemental evidence must preserve the original cutoff")
        validate_agent_evidence({"as_of_time": analysis["as_of_time"],
                                "decisions": item["findings"]}, analysis["as_of_time"])


def publish_research_library(module_store, module, analysis, scan):
    """Merge a completed batch into the module-wide library and return the library.

    Provenance is supplied by the actual runner at run creation, never inferred
    from output prose. Legacy runs need a separate audited migration. Callers
    serialize publication per module, as they serialize its manual tasks.
    """
    symbols = _formal(analysis, module, scan)
    library = deepcopy(module_store.latest(module, "library") or {
        "module": module, "schema_version": 1, "items": {},
        "execution_reusable": False,
    })
    if library.get("module") != module:
        raise ValueError("Stored research library belongs to another module")
    candidates = {row["symbol"]: row for row in scan["candidates"]}
    changed = 0
    for symbol in symbols:
        entry = library["items"].setdefault(symbol, {
            "symbol": symbol, "name": candidates[symbol].get("name", symbol), "reviews": [],
        })
        if any(review["source_run_id"] == analysis["run_id"] for review in entry["reviews"]):
            continue
        roles = []
        for output in [*analysis["research"], *analysis.get("supplemental_research", [])]:
            if output.get("stage") not in {"research", "challenge"}:
                continue
            finding = next((item for item in output["findings"] if item.get("symbol") == symbol), None)
            if finding is None:
                continue
            roles.append({
                "agent_id": output["agent_id"], "display_name": output.get("display_name"),
                "stage": output["stage"], "model": output["model"],
                "source_run_id": analysis["run_id"], "source_as_of_time": analysis["as_of_time"],
                "origin_run_id": output.get("reused_from_run_id") or analysis["run_id"],
                "reviewed_at": output.get("completed_at") or analysis["completed_at"],
                "output_path": output.get("output_path"), "trace_path": output.get("trace_path"),
                "business_thesis": finding.get("summary"), "buy_condition": finding.get("buy_condition"),
                "invalidation": finding.get("invalidation"), "finding": deepcopy(finding),
                "role_summary": output.get("summary"), "disagreements": deepcopy(output.get("disagreements", [])),
                "parent_agent_id": output.get("parent_agent_id"),
                "assigned_questions": deepcopy(output.get("assigned_questions", [])),
                "request_sha256": output.get("request_sha256"),
                "market_findings": [deepcopy(item) for item in output["findings"] if item.get("symbol") is None],
            })
        archive = []
        for output in analysis["research"]:
            if output.get("stage") not in {"portfolio", "risk"}:
                continue
            archive.append({"agent_id": output["agent_id"], "model": output["model"],
                "stage": output["stage"], "source_run_id": analysis["run_id"],
                "origin_run_id": output.get("reused_from_run_id") or analysis["run_id"],
                "source_as_of_time": analysis["as_of_time"], "reviewed_at": output.get("completed_at") or analysis["completed_at"],
                "positions": [deepcopy(p) for p in output.get("positions", []) if p["symbol"] == symbol],
                "valuations": [deepcopy(p) for p in output.get("valuations", []) if p["symbol"] == symbol],
                "t_suggestions": [deepcopy(p) for p in output.get("t_suggestions", []) if isinstance(p, dict) and p.get("symbol") == symbol],
                "summary": output.get("summary"), "discussion_resolution": deepcopy(output.get("discussion_resolution", [])),
                "exit_plan": deepcopy(output.get("exit_plan")), "risks": deepcopy(output.get("risks", [])),
                "execution_reusable": False})
        review = {"source_run_id": analysis["run_id"], "source_scan_id": scan["scan_id"],
            "name": candidates[symbol].get("name", symbol),
            "source_as_of_time": analysis["as_of_time"], "reviewed_at": analysis["completed_at"],
            "provenance": deepcopy(analysis["provenance"]), "source_hash": analysis.get("source_hash"),
            "roles": roles, "price_plan_archive": archive, "execution_reusable": False}
        entry["reviews"].append(review)
        entry["reviews"].sort(key=lambda r: (_time(r["source_as_of_time"]), _time(r["reviewed_at"])))
        latest = entry["reviews"][-1]
        entry.update(name=latest.get("name", entry.get("name", symbol)),
                     latest_review_at=latest["reviewed_at"], latest_source_run_id=latest["source_run_id"],
                     latest_source_as_of_time=latest["source_as_of_time"])
        changed += 1
    library["updated_at"] = now_iso()
    library["coverage"] = {"covered_count": len(library["items"]), "batch_count": len(symbols),
        "updated_count": changed, "excluded_count": len(candidates) - len(symbols),
        "review_count": sum(len(entry["reviews"]) for entry in library["items"].values())}
    if changed:
        module_store.save(module, "library", library)
    return library


def _freshness(module, role, as_of):
    original = _time(role["source_as_of_time"]).astimezone(as_of.tzinfo)
    key = (role.get("parent_agent_id") or role["agent_id"]).removeprefix(module + "_")
    if module == "value" and key in {"business", "accounts"} or module == "etf" and key == "products":
        stale = as_of - original >= timedelta(days=30)
        return ("stale" if stale else "reference"), "经营/产品研究自原截止起最多30日参考；新公告、财报或产品变化仍须本轮核验"
    if module == "value":
        return "update_required", "历史估值与质询只供背景；本轮重估价格、安全边际与失效条件"
    stale = as_of.date() != original.date()
    return ("stale" if stale else "reference"), "宏观、配置与隔夜条件跨日须更新；当日也需检查新增事件"


def _safe_review(review, as_of):
    try:
        if not _formal_provenance(review.get("provenance", {})) or not review.get("roles"):
            return False
        cutoff = review["source_as_of_time"]
        if _time(cutoff) > as_of:
            return False
        for role in review["roles"]:
            if role["source_as_of_time"] != cutoff:
                return False
            validate_agent_evidence({"as_of_time": cutoff,
                "decisions": [role["finding"], *role.get("market_findings", [])]}, cutoff)
        return True
    except (ValueError, TypeError, KeyError):
        return False


def load_research_context(module_store, module, symbols, as_of_time):
    """Return dated reference findings, freshness counts and mandatory rechecks.

    Completion/review times are computational timestamps, not market evidence.
    Original source cutoffs and evidence times determine point-in-time safety and
    age. Price-plan archives are deliberately absent from the returned context.
    """
    if module not in {"value", "closing", "etf"}:
        raise ValueError("Unknown research module")
    as_of = _time(as_of_time)
    requested = list(dict.fromkeys(symbols))
    library = module_store.latest(module, "library") or {"items": {}}
    entries, missing, excluded = [], [], []
    for symbol in requested:
        stored = library["items"].get(symbol)
        if stored is None:
            missing.append(symbol)
            continue
        valid = [review for review in stored["reviews"] if _safe_review(review, as_of)]
        if not valid:
            excluded.append(symbol)
            continue
        review = max(valid, key=lambda r: (_time(r["source_as_of_time"]), _time(r["reviewed_at"])))
        roles = []
        for saved in review["roles"]:
            role = deepcopy(saved)
            status, reason = _freshness(module, role, as_of)
            role.update(freshness=status, refresh_reason=reason, execution_reusable=False,
                        verified_in_current_run=False)
            roles.append(role)
        entries.append({"symbol": symbol, "name": review.get("name", stored.get("name", symbol)),
            "source_run_id": review["source_run_id"], "source_as_of_time": review["source_as_of_time"],
            "latest_review_at": review["reviewed_at"], "roles": roles,
            "stale": any(role["freshness"] == "stale" for role in roles),
            "execution_reusable": False, "verified_in_current_run": False,
            "price_plan_archive_available": bool(review.get("price_plan_archive"))})
    return {"module": module, "as_of_time": as_of_time, "entries": entries,
        "missing_symbols": missing, "excluded_symbols": excluded,
        "coverage": {"requested_count": len(requested), "covered_count": len(entries),
            "missing_count": len(missing), "excluded_count": len(excluded),
            "stale_count": sum(entry["stale"] for entry in entries),
            "reference_role_count": sum(role["freshness"] == "reference" for e in entries for role in e["roles"]),
            "update_required_role_count": sum(role["freshness"] != "reference" for e in entries for role in e["roles"])},
        "execution_reusable": False,
        "mandatory_rechecks": ["这些是原日期研究，尚未在本轮重新验证；逐只检查新公告、财报及原失效条件",
            "陈旧观点仅供历史背景，不得直接支撑当前买入；冲突时以新的一手证据更新并解释变化",
            "全部买入价格、止盈止损、有效期、仓位与做T计划必须用当前行情和账户重新讨论，档案不授权执行"]}


def library_summary(module_store, module, as_of_time):
    """Compact read-only UI view; mixed fresh/stale roles remain distinguishable."""
    library = module_store.latest(module, "library") or {"items": {}}
    context = load_research_context(module_store, module, list(library["items"]), as_of_time)
    items = []
    for entry in context["entries"]:
        usable = any(role["freshness"] == "reference" for role in entry["roles"])
        items.append({"symbol": entry["symbol"], "name": entry["name"],
            "last_reviewed_at": entry["latest_review_at"], "source_as_of_time": entry["source_as_of_time"],
            "status": "partial_update" if usable and entry["stale"] else "reference" if usable else "stale",
            "summary": entry["roles"][0].get("business_thesis"), "usable": usable,
            "stale": entry["stale"], "execution_reusable": False})
    return {"module": module, "as_of_time": as_of_time, "coverage_count": len(library["items"]),
        "usable_count": sum(item["usable"] for item in items),
        "stale_count": sum(item["stale"] for item in items),
        "excluded_count": context["coverage"]["excluded_count"], "items": items,
        "execution_reusable": False}
