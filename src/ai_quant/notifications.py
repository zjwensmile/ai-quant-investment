from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


_lock = threading.Lock()


def update_notifications(path: Path, decision: dict[str, Any],
                         tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist meaningful result changes; delivery is local to the dashboard."""
    with _lock:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "events": [], "result_signature": None, "failure_signature": None,
        }
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)

        def add(kind, signature, title, body, **extra):
            state["events"].insert(0, {
                "event_id": hashlib.sha256(f"{kind}:{signature}:{datetime.now().isoformat()}".encode()).hexdigest()[:20],
                "kind": kind, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "title": title, "body": body, **extra,
            })

        if decision.get("available") and not decision.get("needs_reanalysis"):
            opportunities = decision.get("opportunities", [])
            # Run ids, generated timestamps and prose summaries change on a rerun
            # without changing the actual recommendation.
            key = {
                "account": decision.get("account_version"),
                "risk": decision.get("risk_decision"),
                "plan": decision.get("order_plan_status"),
                "targets": [{key: row.get(key) for key in (
                    "symbol", "action", "target_weight", "price_min", "price_max",
                    "take_profit_price", "stop_loss_price",
                )} for row in decision.get("targets", [])],
                "rows": [{
                    "symbol": row["symbol"], "source": row["source"],
                    "status": row["status"], "execution": row["execution_status"],
                    "blockers": row["blockers"],
                    "opinions": sorted([
                        (item["agent_id"], item["direction"], item.get("forecast", {}).get("max_action_price"),
                         tuple(item.get("invalidation_conditions", [])))
                        for item in row["opinions"]
                    ], key=lambda item: item[0]),
                } for row in opportunities],
            }
            signature = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if signature != state["result_signature"]:
                approved = sorted({row["symbol"] for row in opportunities if row["status"] == "approved_buy"})
                watching = sorted({row["symbol"] for row in opportunities if row["status"] in {"research_watch", "portfolio_proposed"}})
                body = f"风控批准买入/加仓：{'、'.join(approved) or '无'}；研究观察：{'、'.join(watching) or '无'}。执行状态与条件请查看明细。"
                add("recommendation", signature, "分析结论已更新", body,
                    run_id=decision.get("run_id"), account_version=decision.get("account_version"))
                state["result_signature"] = signature
        latest = tasks[0] if tasks else {}
        if latest.get("status") == "failed":
            signature = json.dumps([latest.get("action"), latest.get("error")], ensure_ascii=False)
            if signature != state["failure_signature"]:
                add("failure", signature, "系统任务失败", str(latest.get("error") or "请检查任务详情"), task_id=latest.get("task_id"))
                state["failure_signature"] = signature
        elif latest.get("status") == "completed":
            state["failure_signature"] = None
        state["events"] = state["events"][:50]
        if not path.exists() or before != json.dumps(state, ensure_ascii=False, sort_keys=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        return [event for event in state["events"] if event["kind"] != "recommendation" or (
            not decision.get("needs_reanalysis") and event.get("account_version") == decision.get("current_account_version")
        )]
