from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .market_store import MarketStore
from .simulation import MODEL_VERSION, ShadowExecutionEngine
from .store import AuditStore
from .tasks import TaskController


class DailyWorkflow:
    """Audited local workflow used by the optional Windows scheduler."""

    def __init__(
        self,
        settings: Settings,
        audit_store: AuditStore,
        market_store: MarketStore,
        controller: TaskController | None = None,
    ) -> None:
        self.settings = settings
        self.audit_store = audit_store
        self.market_store = market_store
        self.controller = controller or TaskController(settings, audit_store, market_store)

    def run(self, mode: str, through: date | None = None) -> dict[str, Any]:
        if mode not in {"refresh", "analyze", "close"}:
            raise ValueError(f"未知每日工作流: {mode}")
        task_id = f"task-{uuid.uuid4().hex}"
        self.audit_store.create_system_task(task_id, f"daily_{mode}", ["daily-workflow", mode])
        try:
            result = self._run_steps(mode, through, task_id)
            error = next((str(item.get("error") or "工作流步骤失败") for item in result["steps"]
                          if item["status"] != "completed"), None)
            self.audit_store.finish_system_task(
                task_id, result["status"], json.dumps(result, ensure_ascii=False), error,
            )
            return {**result, "task_id": task_id}
        except Exception as exc:
            self.audit_store.finish_system_task(task_id, "failed", "", str(exc))
            raise

    def _run_steps(self, mode: str, through: date | None, parent_task_id: str) -> dict[str, Any]:
        target_date = through or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        results: list[dict[str, Any]] = []
        snapshot_result = self.controller.run_sync(
            "snapshot" if mode == "refresh" else "snapshot_raw", parent_task_id=parent_task_id
        )
        results.append(snapshot_result)
        failed = snapshot_result["status"] != "completed"

        if mode == "analyze" and not failed:
            snapshot = self._latest_snapshot()
            if bool(snapshot.get("market", {}).get("is_trading_day")):
                for action in ("fundamentals", "enrich", "analyze"):
                    result = self.controller.run_sync(action, parent_task_id=parent_task_id)
                    results.append(result)
                    if result["status"] != "completed":
                        failed = True
                        break

        if mode == "close" and not failed:
            snapshot = self._latest_snapshot()
            is_trading_day = bool(snapshot.get("market", {}).get("is_trading_day"))
            actions = (
                ("fundamentals", "enrich", "analyze", "evaluate")
                if is_trading_day else ("evaluate",)
            )
            for action in actions:
                result = self.controller.run_sync(action, parent_task_id=parent_task_id)
                results.append(result)
                if result["status"] != "completed":
                    failed = True
                    break
            if not failed:
                settlement = self._settle_pending(target_date, parent_task_id)
                results.append(settlement)
                failed = settlement["status"] != "completed"

            backup = self.controller.run_sync("backup", parent_task_id=parent_task_id)
            results.append(backup)
            failed = failed or backup["status"] != "completed"

        return {
            "version": "daily-workflow-v2-diversified-sources",
            "mode": mode,
            "through": target_date.isoformat(),
            "status": "failed" if failed else "completed",
            "steps": results,
        }

    def _latest_snapshot(self) -> dict[str, Any]:
        files = sorted(
            (self.settings.runtime_dir / "snapshots").glob("market-*.json"),
            key=lambda item: item.stat().st_mtime,
        )
        if not files:
            raise ValueError("快照任务完成后仍找不到市场快照")
        return json.loads(files[-1].read_text(encoding="utf-8"))

    def _settle_pending(self, through: date, parent_task_id: str) -> dict[str, Any]:
        task_id = f"task-{uuid.uuid4().hex}"
        action = "shadow_settle_pending"
        command = ["internal", action, "--through", through.isoformat()]
        self.audit_store.create_system_task(task_id, action, command, parent_task_id=parent_task_id)
        try:
            engine = ShadowExecutionEngine(self.audit_store, self.market_store)
            settlements = []
            for plan in self.audit_store.unsettled_shadow_plans():
                settlements.append({
                    "run_id": plan["run_id"],
                    **engine.settle(plan, through),
                })
            output = json.dumps(
                {"model_version": MODEL_VERSION, "settlements": settlements},
                ensure_ascii=False,
            )
            self.audit_store.finish_system_task(task_id, "completed", output, None)
            return {
                "task_id": task_id, "action": action, "status": "completed",
                "output": output, "error": None,
            }
        except Exception as exc:
            self.audit_store.finish_system_task(task_id, "failed", "", str(exc))
            return {
                "task_id": task_id, "action": action, "status": "failed",
                "output": "", "error": str(exc),
            }
