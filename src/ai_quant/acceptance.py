from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .account import AccountLedger
from .backup import BackupService
from .config import Settings
from .market_store import MarketStore
from .registry import all_agents
from .scheduler import WindowsTaskScheduler, load_configured_schedule
from .store import AuditStore


class AcceptanceAuditor:
    def __init__(
        self, settings: Settings, audit_store: AuditStore, market_store: MarketStore
    ) -> None:
        self.settings = settings
        self.audit_store = audit_store
        self.market_store = market_store

    def report(self) -> dict[str, Any]:
        status = self.market_store.status()
        account = AccountLedger.reduce(self.audit_store.list_account_events())
        completed_run = self._latest_formal_run()
        backup = self._latest_backup()
        restore_test = self._latest_restore_test()
        config = _system_config(self.settings)
        required_shadow_days = config.get("shadow_acceptance_days")
        selection_model_activated_on = config.get("selection_model_activated_on")
        schedule = load_configured_schedule(self.settings)
        schedule_status = WindowsTaskScheduler(self.settings).status()
        shadow_days = self._shadow_days(selection_model_activated_on)
        backtest = self._latest_source_backtest()
        feature_coverage, snapshot_path = self._latest_feature_coverage()
        checks = {
            "history_coverage_80pct": float(status["history_coverage_ratio"]) >= 0.8,
            "latest_feature_coverage_80pct": feature_coverage >= 0.8,
            "industry_queue_complete": int(status["industry_target_status"].get("queued", 0)) == 0,
            "account_ledger_initialized": account is not None,
            "broker_cash_confirmed": account is not None and account.get("cash") is not None,
            "verified_backup_exists": bool(
                backup
                and backup.get("valid")
                and backup.get("manifest_version") == "backup-manifest-v2"
            ),
            "verified_restore_test_exists": bool(
                restore_test and restore_test.get("valid")
            ),
            "formal_daily_agent_run_completed": completed_run is not None,
            "web_research_traces_present": self._web_traces_present(completed_run),
            "shadow_acceptance_days_configured": isinstance(required_shadow_days, int)
            and required_shadow_days > 0,
            "shadow_acceptance_days_met": isinstance(required_shadow_days, int)
            and shadow_days >= required_shadow_days,
            "selection_model_activation_configured": bool(selection_model_activated_on),
            "diversified_source_backtest_completed": bool(
                backtest and backtest.get("status") == "completed"
            ),
            "daily_schedule_configured": bool(schedule and schedule.enabled),
            "daily_schedule_installed": bool(schedule_status.get("installed")),
            "daily_schedule_enabled": schedule_status.get("live_query_available") is True
            and schedule_status.get("enabled") is True,
            "real_broker_api_disabled": self.settings.real_trading_enabled is False,
        }
        research_ready_keys = {
            "history_coverage_80pct", "industry_queue_complete",
            "latest_feature_coverage_80pct",
            "account_ledger_initialized", "verified_backup_exists",
            "verified_restore_test_exists",
            "real_broker_api_disabled",
        }
        trial_ready_keys = set(checks)
        optional_checks = ({
            "daily_schedule_configured", "daily_schedule_installed", "daily_schedule_enabled",
        } if schedule is not None and not schedule.enabled else set())
        trial_ready_keys -= optional_checks
        blockers = [name for name, passed in checks.items() if not passed and name not in optional_checks]
        return {
            "version": "acceptance-audit-v1",
            "research_ready": all(checks[key] for key in research_ready_keys),
            "manual_real_funds_trial_ready": all(checks[key] for key in trial_ready_keys),
            "checks": checks,
            "operating_mode": "manual" if optional_checks else "scheduled" if schedule else "unconfigured",
            "optional_checks": sorted(optional_checks),
            "blockers": blockers,
            "history_coverage_ratio": status["history_coverage_ratio"],
            "latest_feature_coverage_ratio": feature_coverage,
            "latest_enriched_snapshot": str(snapshot_path) if snapshot_path else None,
            "formal_run_id": completed_run,
            "shadow_days": shadow_days,
            "required_shadow_days": required_shadow_days,
            "selection_model_activated_on": selection_model_activated_on,
            "latest_source_backtest": backtest,
            "schedule": schedule.to_dict() if schedule else None,
            "schedule_status": schedule_status,
            "latest_backup": backup,
            "latest_restore_test": restore_test,
            "note": "自动券商交易始终不属于本验收范围。",
        }

    def _latest_formal_run(self) -> str | None:
        for run in self.audit_store.list_runs(100):
            if run["status"] != "completed":
                continue
            detail = self.audit_store.get_run(str(run["run_id"]))
            executions = detail.get("executions", []) if detail else []
            if len(executions) < 11:
                continue
            outputs = [str(item.get("output_json") or "") for item in executions]
            if outputs and all("example_only" not in item for item in outputs):
                return str(run["run_id"])
        return None

    def _latest_backup(self) -> dict[str, Any] | None:
        directory = self.settings.runtime_dir / "backups"
        backups = sorted(
            (item for item in directory.glob("*") if item.is_dir()),
            key=lambda item: item.name,
        )
        if not backups:
            return None
        try:
            return BackupService.verification_status(backups[-1])
        except Exception as exc:
            return {"valid": False, "findings": [str(exc)], "directory": str(backups[-1])}

    def _latest_restore_test(self) -> dict[str, Any] | None:
        directory = self.settings.runtime_dir / "restore-tests"
        tests = sorted(
            (item for item in directory.glob("*") if item.is_dir()),
            key=lambda item: item.name,
        )
        if not tests:
            return None
        try:
            return BackupService.restore_status(tests[-1])
        except Exception as exc:
            return {
                "valid": False, "findings": [str(exc)],
                "destination": str(tests[-1]),
            }

    def _web_traces_present(self, run_id: str | None) -> bool:
        if run_id is None:
            return False
        detail = self.audit_store.get_run(run_id)
        by_agent = {agent.agent_id: agent for agent in all_agents()}
        for execution in (detail or {}).get("executions", []):
            agent = by_agent.get(str(execution.get("agent_id") or ""))
            if agent is None or not agent.requires_web_research:
                continue
            trace = execution.get("trace_path")
            if not trace or not Path(str(trace)).exists() or Path(str(trace)).stat().st_size == 0:
                return False
        return True

    def _shadow_days(self, activated_on: str | None = None) -> int:
        return len({
            str(row["as_of_date"])
            for row in self.audit_store.completed_shadow_settlements()
            if not activated_on or str(row["as_of_date"]) >= activated_on
        })

    def _latest_source_backtest(self) -> dict[str, Any] | None:
        directory = self.settings.runtime_dir / "backtests"
        files = sorted(directory.glob("source-backtest-*.json"))
        if not files:
            return None
        try:
            data = json.loads(files[-1].read_text(encoding="utf-8"))
            return {
                "path": str(files[-1]), "status": data.get("status"),
                "version": data.get("version"), "start": data.get("start"),
                "end": data.get("end"),
            }
        except Exception as exc:
            return {"path": str(files[-1]), "status": "invalid", "error": str(exc)}

    def _latest_feature_coverage(self) -> tuple[float, Path | None]:
        directory = self.settings.runtime_dir / "snapshots"
        files = sorted(
            [
                item for pattern in ("market-*.json", "enriched-*.json")
                for item in directory.glob(pattern)
            ],
            key=lambda item: item.stat().st_mtime,
        )
        if not files:
            return 0.0, None
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        return float(data.get("feature_coverage", {}).get("coverage_ratio") or 0), files[-1]


def _system_config(settings: Settings) -> dict[str, Any]:
    path = settings.workspace / "config" / "system.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
