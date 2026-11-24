from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .account import AccountLedger
from .config import Settings
from .market_store import MarketStore
from .pipeline import source_tree_hash, system_config_hash
from .runner import codex_chatgpt_login_status, codex_network_status, terminate_process_tree
from .store import AuditStore, utc_now


FULL_WORKFLOW_STEPS = ("snapshot_raw", "history_current", "fundamentals", "snapshot_for_analysis", "enrich", "analyze")


class TaskController:
    def __init__(
        self, settings: Settings, audit_store: AuditStore, market_store: MarketStore
    ) -> None:
        self.settings = settings
        self.audit_store = audit_store
        self.market_store = market_store
        self._lock = threading.Lock()

    def start(self, action: str) -> dict[str, object]:
        with self._lock:
            command = self._command(action)
            task_id = f"task-{uuid.uuid4().hex}"
            self.audit_store.create_system_task(task_id, action, command)
            thread = threading.Thread(
                target=self._run, args=(task_id, action, command), daemon=True
            )
            try:
                thread.start()
            except Exception as exc:
                self.audit_store.finish_system_task(task_id, "failed", "", str(exc))
                raise
            return {"task_id": task_id, "action": action, "status": "running"}

    def run_sync(self, action: str, *, parent_task_id: str | None = None) -> dict[str, object]:
        with self._lock:
            command = self._command(action)
            task_id = f"task-{uuid.uuid4().hex}"
            self.audit_store.create_system_task(task_id, action, command, parent_task_id=parent_task_id)
        return self._execute(task_id, action, command)

    def _command(self, action: str) -> list[str]:
        base = [sys.executable, "-m", "ai_quant.cli"]
        if action in {f"module_{module}_{step}" for module in ("value", "closing", "etf") for step in ("scan", "analyze", "experiment")} | {"module_closing_refresh"}:
            _, module, step = action.split("_")
            return base + ["module-run", "--module", module, "--action", step]
        if action == "snapshot":
            return base + ["snapshot"]
        if action in {"snapshot_raw", "snapshot_for_analysis"}:
            return base + ["snapshot", "--without-features"]
        if action == "full_workflow":
            # The workflow is orchestrated by this controller. Keep an
            # auditable marker instead of a fake CLI command which could
            # recursively create another TaskController task.
            return ["full_workflow"]
        if action == "enrich":
            snapshot = _latest(self.settings.runtime_dir / "snapshots", "market-*.json")
            if snapshot is None:
                raise ValueError("尚无原始市场快照")
            return base + ["snapshot-enrich", "--snapshot", str(snapshot)]
        if action == "history":
            snapshot = _latest(self.settings.runtime_dir / "snapshots", "market-*.json")
            if snapshot is None:
                raise ValueError("尚无原始市场快照")
            return base + [
                "history-sync", "--snapshot", str(snapshot), "--limit", "10000",
                "--workers", str(self.settings.history_workers),
                "--delay", str(self.settings.history_request_delay_seconds),
            ]
        if action == "fundamentals":
            return base + ["fundamentals-sync"]
        if action == "analyze":
            status = self.market_store.status()
            if float(status.get("history_coverage_ratio") or 0) < 0.8:
                raise ValueError("历史行情覆盖率不足 80%，正式 Agent 分析尚未解锁")
            snapshot = _latest_enriched_snapshot(
                self.settings.runtime_dir / "snapshots"
            )
            if snapshot is None:
                raise ValueError("尚无候选增强快照")
            data = json.loads(snapshot.read_text(encoding="utf-8"))
            feature_coverage = float(
                data.get("feature_coverage", {}).get("coverage_ratio") or 0
            )
            if feature_coverage < 0.8:
                raise ValueError("最新候选快照的有效特征覆盖率不足 80%，请先刷新候选")
            logged_in, _ = codex_chatgpt_login_status(
                self.settings.codex_command, self.settings.workspace
            )
            if not logged_in:
                raise ValueError("Codex CLI 尚未通过 ChatGPT 会员登录")
            reachable, detail = codex_network_status()
            if not reachable:
                raise ValueError(f"Codex 网络不可用: {detail}")
            command = base + ["run", "--snapshot", str(snapshot), "--runner", "codex"]
            current_account = AccountLedger.reduce(self.audit_store.list_account_events())
            snapshot_account = data.get("account", {})
            snapshot_version = (
                str(snapshot_account.get("version"))
                if isinstance(snapshot_account, dict) and snapshot_account.get("version")
                else None
            )
            if current_account is not None and snapshot_version != str(current_account.get("version")):
                command.append("--current-account")
            return command
        if action == "evaluate":
            return base + ["evaluate"]
        if action == "backup":
            return base + ["backup-create"]
        raise ValueError(f"不支持的任务: {action}")

    def _run(self, task_id: str, action: str, command: list[str]) -> None:
        self._execute(task_id, action, command)

    def _execute(
        self, task_id: str, action: str, command: list[str]
    ) -> dict[str, object]:
        try:
            if action == "full_workflow":
                steps = FULL_WORKFLOW_STEPS
                checkpoint = self._load_full_workflow_checkpoint()
                completed_steps = list(checkpoint.get("completed_steps", []))
                outputs = list(checkpoint.get("outputs", []))
                resume_note = "[断点续跑] 已跳过: " + ", ".join(completed_steps) if completed_steps else ""
                for step in steps[len(completed_steps):]:
                    completed = (self._refresh_history(task_id) if step == "history_current"
                                 else self._run_workflow_command(self._command(step), task_id))
                    outputs.append(
                        f"[{step}]\n{completed.stdout}\n{completed.stderr}".strip()
                    )
                    if completed.returncode != 0:
                        output = "\n\n".join(item for item in [resume_note, *outputs] if item)
                        detail = (completed.stderr or completed.stdout or "无错误详情").strip()
                        error = (
                            f"一键完整流程在 {step} 步骤失败 "
                            f"(exit_code={completed.returncode}): {detail[-500:]}"
                        )
                        self._save_full_workflow_checkpoint(
                            completed_steps, outputs[:len(completed_steps)]
                        )
                        self.audit_store.finish_system_task(
                            task_id, "failed", output, error
                        )
                        return {
                            "task_id": task_id, "action": action,
                            "status": "failed", "output": output, "error": error,
                        }
                    completed_steps.append(step)
                    if step == "snapshot_for_analysis":
                        self._assert_history_visible()
                    self._save_full_workflow_checkpoint(completed_steps, outputs)
                output = "\n\n".join(item for item in [resume_note, *outputs] if item)
                self._clear_full_workflow_checkpoint()
                self.audit_store.finish_system_task(task_id, "completed", output, None)
                return {
                    "task_id": task_id, "action": action, "status": "completed",
                    "output": output, "error": None,
                }
            completed = self._run_command(
                command,
                task_id=task_id,
                cwd=self.settings.workspace,
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "AI_QUANT_SYSTEM_TASK_ID": task_id,
                    "AI_QUANT_TASK_PROGRESS_FILE": str(
                        self._progress_path(task_id)
                    ),
                },
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=max(self.settings.agent_timeout_seconds * 12, 3600),
                check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()
            self.audit_store.finish_system_task(
                task_id,
                "completed" if completed.returncode == 0 else "failed",
                output,
                None if completed.returncode == 0 else self._failure_detail(completed),
            )
            return {
                "task_id": task_id, "action": action,
                "status": "completed" if completed.returncode == 0 else "failed",
                "output": output,
                "error": None if completed.returncode == 0 else self._failure_detail(completed),
            }
        except Exception as exc:
            self.audit_store.finish_system_task(task_id, "failed", "", str(exc))
            return {
                "task_id": task_id, "action": action, "status": "failed",
                "output": "", "error": str(exc),
            }

    def _run_workflow_command(self, command: list[str], task_id: str) -> subprocess.CompletedProcess:
        return self._run_command(
            command, task_id=task_id, cwd=self.settings.workspace,
            env={**os.environ, "PYTHONUTF8": "1", "AI_QUANT_SYSTEM_TASK_ID": task_id,
                 "AI_QUANT_TASK_PROGRESS_FILE": str(self._progress_path(task_id))},
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=max(self.settings.agent_timeout_seconds * 12, 3600), check=False,
        )

    def _history_plan(self) -> tuple[Path, dict[str, object], dict[str, object]]:
        path = _latest(self.settings.runtime_dir / "snapshots", "market-*.json")
        if path is None:
            raise ValueError("历史增量检查前缺少新市场快照")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        market = snapshot.get("market", {})
        dates = market.get("recent_completed_trade_dates") or []
        if not dates or market.get("latest_completed_trade_date") != dates[-1]:
            raise ValueError("权威交易日历不足，无法确认最近已完成交易日；请刷新行情后重试")
        plan = self.market_store.history_refresh_plan(
            {str(item["symbol"]) for item in snapshot.get("securities", [])}, dates,
        )
        return path, snapshot, plan

    def _refresh_history(self, task_id: str) -> subprocess.CompletedProcess:
        _, snapshot, plan = self._history_plan()
        commands = []
        details = []
        # New securities bootstrap separately, so one IPO cannot make every stale
        # existing security download two years instead of its recent missing days.
        for bootstrap in (False, True):
            needed = [item for item in plan["needed"] if item["bootstrap"] is bootstrap]
            if not needed:
                continue
            symbols = {item["symbol"] for item in needed}
            subset = {**snapshot, "securities": [item for item in snapshot["securities"] if item["symbol"] in symbols]}
            directory = self.settings.runtime_dir / "task_progress"
            directory.mkdir(parents=True, exist_ok=True)
            subset_path = directory / f"{task_id}-history-{int(bootstrap)}.json"
            subset_path.write_text(json.dumps(subset, ensure_ascii=False), encoding="utf-8")
            command = [sys.executable, "-m", "ai_quant.cli", "history-sync", "--snapshot", str(subset_path),
                       "--start", min(item["start"] for item in needed), "--end", str(plan["through"]),
                       "--limit", "0", "--raw-only", "--incremental-window", "--refresh-existing",
                       "--workers", str(self.settings.history_workers),
                       "--delay", str(self.settings.history_request_delay_seconds)]
            commands.append(command)
            completed = self._run_workflow_command(command, task_id)
            details.append((completed.stdout + "\n" + completed.stderr).strip())
            if completed.returncode != 0:
                return subprocess.CompletedProcess(commands, completed.returncode, "\n".join(details), "历史缺口补齐失败，停止正式研究")
        _, _, remaining = self._history_plan()
        report_path = self._history_report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {**remaining, "requested_symbols": len(plan["needed"]), "status": "incomplete" if remaining["needed"] or remaining["unavailable"] else "current"}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {"through": plan["through"], "requested_symbols": len(plan["needed"]),
                   "status": report["status"], "unavailable_symbols": len(remaining["unavailable"]),
                   "report_path": str(report_path),
                   "corporate_action_scope": "随本轮缺口股票同步分红；未对无日线缺口的全池重新拉取公司行动",
                   "message": "仅补齐缺失原始日线" if commands else "当前截止日已检查，无需再次下载历史"}
        return subprocess.CompletedProcess(commands, 0, json.dumps(summary, ensure_ascii=False), "")

    def _history_report_path(self) -> Path:
        return self.settings.runtime_dir / "task_checkpoints" / "history_refresh.json"

    def _assert_history_visible(self) -> None:
        path, snapshot, plan = self._history_plan()
        if plan["needed"]:
            self._clear_full_workflow_checkpoint()
            raise ValueError("新快照对应交易日仍有未检查日线缺口，请重新启动完整流程补齐")
        latest = plan.get("latest_available_at")
        if latest and datetime.fromisoformat(snapshot["as_of_time"]) < datetime.fromisoformat(str(latest)):
            self._clear_full_workflow_checkpoint()
            raise ValueError("新快照早于历史行情入库时间，禁止使用尚不可见的数据，请重新抓取行情")
        quality = snapshot.setdefault("data_quality", {})
        quality["history_status"] = "incomplete" if plan["unavailable"] else "current"
        quality["history_refresh"] = {"through": plan["through"], "unavailable": plan["unavailable"]}
        if plan["unavailable"]:
            quality.setdefault("warnings", []).append(
                f"历史行情仍有 {len(plan['unavailable'])} 个证券缺口；同一截止日已向数据源请求，详情见 history_refresh"
            )
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _failure_detail(completed: subprocess.CompletedProcess) -> str:
        detail = (completed.stderr or completed.stdout or "无错误详情").strip()
        return f"exit_code={completed.returncode}: {detail[-1000:]}"

    def _run_command(self, command: list[str], *, task_id: str, **kwargs) -> subprocess.CompletedProcess:
        timeout = kwargs.pop("timeout")
        kwargs.pop("check")
        kwargs.pop("capture_output")
        process = self.audit_store.launch_task_process(
            task_id,
            lambda: subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs),
        )
        with process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                process.communicate()
                raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _progress_path(self, task_id: str) -> Path:
        return self.settings.runtime_dir / "task_progress" / f"{task_id}.json"

    def _full_workflow_checkpoint_path(self) -> Path:
        return self.settings.runtime_dir / "task_checkpoints" / "full_workflow.json"

    def _checkpoint_signature(self) -> dict[str, str]:
        return {
            "source_tree_hash": source_tree_hash(self.settings.workspace),
            "system_config_hash": system_config_hash(self.settings.workspace),
            "local_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        }

    def _load_full_workflow_checkpoint(self) -> dict[str, object]:
        path = self._full_workflow_checkpoint_path()
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(checkpoint, dict) or checkpoint.get("signature") != self._checkpoint_signature():
            return {}
        steps = checkpoint.get("completed_steps")
        outputs = checkpoint.get("outputs")
        allowed = FULL_WORKFLOW_STEPS
        if (
            not isinstance(steps, list)
            or not isinstance(outputs, list)
            or len(outputs) != len(steps)
            or steps != list(allowed[:len(steps)])
            or not all(isinstance(item, str) for item in outputs)
        ):
            return {}
        # Every resume republishes a fresh point-in-time snapshot and features.
        return {"completed_steps": steps[:3], "outputs": outputs[:3]}

    def _save_full_workflow_checkpoint(
        self, completed_steps: list[str], outputs: list[str]
    ) -> None:
        path = self._full_workflow_checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "full-workflow-checkpoint-v1",
            "signature": self._checkpoint_signature(),
            "completed_steps": completed_steps,
            "outputs": outputs,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _clear_full_workflow_checkpoint(self) -> None:
        path = self._full_workflow_checkpoint_path()
        if path.exists():
            path.unlink()


def _latest(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime)
    return files[-1].resolve() if files else None


def _latest_enriched_snapshot(directory: Path) -> Path | None:
    """Return the newest snapshot that has actually completed feature enrichment."""
    candidates = []
    for pattern in ("enriched-*.json", "analysis-account-*.json", "market-*.json"):
        for item in directory.glob(pattern):
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
                if "feature_coverage" in data:
                    candidates.append(item)
            except (OSError, json.JSONDecodeError):
                continue
    return max(candidates, key=lambda item: item.stat().st_mtime).resolve() if candidates else None
