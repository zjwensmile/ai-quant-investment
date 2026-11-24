from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings


TASK_PREFIX = "AIQuantV2_"


@dataclass(frozen=True)
class ScheduleEntry:
    name: str
    time: str
    workflow: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ScheduleEntry":
        name = str(value.get("name") or "")
        time = str(value.get("time") or "")
        workflow = str(value.get("workflow") or "")
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError(f"非法调度名称: {name}")
        try:
            datetime.strptime(time, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"非法调度时间: {time}") from exc
        if workflow not in {"refresh", "analyze", "close"}:
            raise ValueError(f"非法调度工作流: {workflow}")
        return cls(name=name, time=time, workflow=workflow)


@dataclass(frozen=True)
class SchedulePlan:
    enabled: bool
    timezone: str
    entries: tuple[ScheduleEntry, ...]

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> "SchedulePlan":
        timezone = str(value.get("timezone") or "")
        if timezone != "Asia/Shanghai":
            raise ValueError("调度时区必须为 Asia/Shanghai")
        entries = tuple(ScheduleEntry.from_dict(item) for item in value.get("tasks", []))
        if not entries:
            raise ValueError("调度任务不能为空")
        names = [item.name for item in entries]
        if len(names) != len(set(names)):
            raise ValueError("调度任务名称不能重复")
        close_count = sum(item.workflow == "close" for item in entries)
        if close_count != 1:
            raise ValueError("每天必须且只能配置一个盘后完整工作流")
        return cls(bool(value.get("enabled")), timezone, entries)

    @classmethod
    def recommended(cls) -> "SchedulePlan":
        return cls(
            enabled=False,
            timezone="Asia/Shanghai",
            entries=(
                ScheduleEntry("premarket", "08:45", "analyze"),
                ScheduleEntry("intraday_1000", "10:00", "analyze"),
                ScheduleEntry("closing_watch", "14:45", "analyze"),
                ScheduleEntry("postmarket", "15:20", "close"),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "timezone": self.timezone,
            "tasks": [asdict(item) for item in self.entries],
        }


def load_configured_schedule(settings: Settings) -> SchedulePlan | None:
    path = settings.workspace / "config" / "system.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    schedule = values.get("schedule")
    return SchedulePlan.from_value(schedule) if isinstance(schedule, dict) else None


class WindowsTaskScheduler:
    def __init__(
        self,
        settings: Settings,
        runner: Callable[..., Any] = subprocess.run,
        platform: str = os.name,
        python_command: str | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.platform = platform
        self.python_command = python_command or sys.executable
        self.directory = settings.runtime_dir / "scheduler"
        self.manifest_path = self.directory / "install-manifest.json"

    def preview(self, plan: SchedulePlan) -> dict[str, Any]:
        return {
            "configured": load_configured_schedule(self.settings) is not None,
            "enabled": plan.enabled,
            "timezone": plan.timezone,
            "tasks": [self._task_description(entry) for entry in plan.entries],
            "installed": self.manifest_path.exists(),
        }

    def install(self, plan: SchedulePlan) -> dict[str, Any]:
        if self.platform != "nt":
            raise RuntimeError("本地调度安装仅支持 Windows")
        if not plan.enabled:
            raise ValueError("schedule.enabled 必须由用户确认后设为 true")
        preflight = self.runner(
            ["schtasks.exe", "/Query", "/FO", "LIST"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        if preflight.returncode != 0:
            raise RuntimeError(
                "当前进程无法访问 Windows 任务计划程序；请在本机用户终端运行 "
                "ai-quant schedule-install"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        installed = []
        for entry in plan.entries:
            description = self._task_description(entry)
            script = Path(description["script"])
            script.write_text(self._script(entry), encoding="utf-8-sig")
            command = [
                "schtasks.exe", "/Create", "/TN", description["task_name"],
                "/TR", description["task_command"], "/SC", "WEEKLY",
                "/D", "MON,TUE,WED,THU,FRI", "/ST", entry.time, "/F",
            ]
            completed = self.runner(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"安装调度任务失败 {description['task_name']}: "
                    f"{(completed.stderr or completed.stdout).strip()}"
                )
            installed.append(description)
        manifest = {
            "version": "windows-schedule-manifest-v1",
            "installed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
                timespec="seconds"
            ),
            "plan": plan.to_dict(),
            "tasks": installed,
        }
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    def status(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8")) if self.manifest_path.exists() else {}
        plan = load_configured_schedule(self.settings)
        expected = ([self._task_description(entry) for entry in plan.entries]
                    if plan else manifest.get("tasks", []))
        if not expected:
            return {"installed": False, "enabled": False, "tasks": []}
        script = (
            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
            "@(Get-ScheduledTask | Where-Object {$_.TaskName -like 'AIQuantV2_*'} | ForEach-Object {"
            "$task=$_; $info=$task | Get-ScheduledTaskInfo; "
            "[pscustomobject]@{task_name=$task.TaskName; present=$true; enabled=[bool]$task.Settings.Enabled; "
            "state=[string]$task.State; last_run=if($info.LastRunTime.Year -gt 2000){$info.LastRunTime.ToString('o')}else{$null}; "
            "next_run=if($info.NextRunTime.Year -gt 2000){$info.NextRunTime.ToString('o')}else{$null}; "
            "last_result=[long]$info.LastTaskResult; missed_runs=[int]$info.NumberOfMissedRuns}"
            "}) | ConvertTo-Json -Compress"
        )
        try:
            completed = self.runner(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=20,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout).strip())
            live = json.loads(completed.stdout.strip() or "[]")
            live = [live] if isinstance(live, dict) else live
            by_name = {item["task_name"]: item for item in live}
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            return {
                "installed": None, "enabled": None, "configured_enabled": plan.enabled if plan else None,
                "verification": "unavailable", "live_query_available": False,
                "installed_at": manifest.get("installed_at"),
                "tasks": [{**item, "present": None, "enabled": None} for item in expected],
                "warning": f"无法实时核实 Windows 调度状态: {exc}",
            }
        tasks = []
        for item in expected:
            task = {**item, **by_name.get(item["task_name"], {"present": False, "enabled": False})}
            result_path = self.directory / f"{item['name']}-result.json"
            try:
                result = json.loads(result_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                result = {}
            task["last_success"] = (task.get("last_run") if task.get("last_result") == 0
                                    else result.get("last_success"))
            task["log_path"] = str(self.directory / f"{item['name']}-latest.log")
            code = task.get("last_result")
            task["last_result_text"] = (
                "尚未运行" if not task.get("last_run") else
                "成功" if code == 0 else "正在运行" if code == 267009 else
                f"Windows 任务结果: {code} (0x{int(code):08X})" if code is not None else "未知"
            )
            task["last_error"] = ((result.get("error") or task["last_result_text"])
                                  if task.get("last_run") and code not in {None, 0, 267009} else None)
            tasks.append(task)
        return {
            "installed": bool(tasks) and all(item["present"] for item in tasks),
            "enabled": bool(tasks) and all(item["enabled"] for item in tasks),
            "configured_enabled": plan.enabled if plan else None,
            "verification": "live_query",
            "live_query_available": True,
            "tasks": tasks,
        }

    def remove(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"removed": [], "not_installed": True}
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        removed = []
        for item in manifest.get("tasks", []):
            task_name = str(item["task_name"])
            if not task_name.startswith(TASK_PREFIX):
                raise ValueError(f"拒绝删除非本系统任务: {task_name}")
            completed = self.runner(
                ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False,
            )
            if completed.returncode not in {0, 1}:
                raise RuntimeError(f"删除调度任务失败: {task_name}")
            removed.append(task_name)
        self.manifest_path.unlink()
        return {"removed": removed, "not_installed": False}

    def _task_description(self, entry: ScheduleEntry) -> dict[str, str]:
        script = (self.directory / f"{entry.name}.ps1").resolve()
        return {
            "name": entry.name,
            "time": entry.time,
            "workflow": entry.workflow,
            "task_name": f"{TASK_PREFIX}{entry.name}",
            "script": str(script),
            "task_command": (
                "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden "
                f'-ExecutionPolicy Bypass -File "{script}"'
            ),
        }

    def _script(self, entry: ScheduleEntry) -> str:
        workspace = str(self.settings.workspace).replace("'", "''")
        python = str(self.python_command).replace("'", "''")
        result_path = str(self.directory / f"{entry.name}-result.json").replace("'", "''")
        log_path = str(self.directory / f"{entry.name}-latest.log").replace("'", "''")
        return (
            "$ErrorActionPreference = 'Stop'\n"
            "$env:PYTHONUTF8 = '1'\n"
            f"$resultPath = '{result_path}'\n"
            "$lastSuccess = $null\n"
            "if (Test-Path -LiteralPath $resultPath) {\n"
            "  try { $lastSuccess = (Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json).last_success } catch {}\n"
            "}\n"
            "$startedAt = (Get-Date).ToString('o')\n"
            "$taskExitCode = 1\n"
            "$taskError = $null\n"
            "try {\n"
            f"  Set-Location -LiteralPath '{workspace}'\n"
            "  $LASTEXITCODE = 1\n"
            "  $ErrorActionPreference = 'Continue'\n"
            f"  & '{python}' -m ai_quant.cli daily-workflow --mode '{entry.workflow}' > '{log_path}' 2>&1\n"
            "  $taskExitCode = $LASTEXITCODE\n"
            "  $ErrorActionPreference = 'Stop'\n"
            "  if ($taskExitCode -ne 0) { $taskError = '工作流失败；详细原因见日志与系统任务审计' }\n"
            "} catch { $taskError = $_.Exception.Message }\n"
            "if ($taskExitCode -eq 0) { $lastSuccess = (Get-Date).ToString('o') }\n"
            "@{started_at=$startedAt; completed_at=(Get-Date).ToString('o'); exit_code=$taskExitCode; "
            "error=$taskError; last_success=$lastSuccess} | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8\n"
            "exit $taskExitCode\n"
        )
