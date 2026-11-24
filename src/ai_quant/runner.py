from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .domain import AgentSpec, SchemaKind
from .schemas import schema_for


@dataclass(frozen=True)
class RunnerResult:
    output: dict[str, Any]
    output_path: Path
    trace_path: Path | None = None


class AgentRunner(Protocol):
    accepts_test_snapshots: bool

    def run(self, agent: AgentSpec, prompt: str, run_dir: Path) -> RunnerResult: ...


def terminate_process_tree(process: subprocess.Popen) -> None:
    """Terminate only the live process owned by this Popen and its Windows children."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Kill the tree before its root exits; otherwise .cmd/Node descendants
        # outlive the root and retain the captured output pipe handles.
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, timeout=15, check=False,
        )
        if result.returncode != 0 and process.poll() is None:
            raise RuntimeError(f"Unable to terminate owned process tree {process.pid}")
    else:
        process.kill()
    process.wait(timeout=5)


class CodexRunner:
    accepts_test_snapshots = False
    publishes_dashboard = True
    run_kind = "formal"

    def __init__(self, command: str, workspace: Path, timeout_seconds: int = 900):
        self.command = command
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds

    def _environment(self) -> dict[str, str]:
        return codex_environment()

    def run(self, agent: AgentSpec, prompt: str, run_dir: Path) -> RunnerResult:
        schema_dir = run_dir / "schemas"
        output_dir = run_dir / "outputs"
        trace_dir = run_dir / "traces"
        schema_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        trace_dir.mkdir(parents=True, exist_ok=True)
        schema_path = schema_dir / f"{agent.agent_id}.{agent.schema_kind.value}.schema.json"
        output_path = output_dir / f"{agent.agent_id}.json"
        attempt_output_path = output_dir / f"{agent.agent_id}.{uuid.uuid4().hex}.pending.json"
        trace_path = trace_dir / f"{agent.agent_id}.jsonl"
        schema_path.write_text(
            json.dumps(schema_for(agent.schema_kind), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # A retry must produce its own result; an earlier output is not proof that
        # a new process completed successfully. Keep partial files for diagnosis.
        command = self._command(agent, schema_path, attempt_output_path)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=self.workspace,
                env=self._environment(),
            )
            try:
                stdout, stderr = process.communicate(prompt, timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                try:
                    terminate_process_tree(process)
                    stdout, stderr = process.communicate(timeout=5)
                except (OSError, subprocess.SubprocessError, RuntimeError) as cleanup_error:
                    self._record_trace(
                        trace_path, exc.stdout, exc.stderr,
                        {"type": "runner_cleanup_error", "error": str(cleanup_error)},
                    )
                    raise exc from cleanup_error
                # communicate returns the complete captured streams after cleanup.
                exc.stdout, exc.stderr = stdout, stderr
                raise
        except (OSError, subprocess.SubprocessError) as exc:
            self._record_trace(
                trace_path, getattr(exc, "stdout", None), getattr(exc, "stderr", None),
                {"type": "runner_error", "error_type": type(exc).__name__, "error": str(exc)},
            )
            raise RuntimeError(f"Unable to run Codex for {agent.agent_id}: {exc}") from exc

        self._record_trace(trace_path, stdout, stderr)
        if process.returncode != 0:
            detail = _failure_detail(stderr, stdout)
            raise RuntimeError(f"Codex failed for {agent.agent_id}: {detail}")
        if not attempt_output_path.exists():
            raise RuntimeError(f"Codex produced no output file for {agent.agent_id}")
        try:
            output = json.loads(attempt_output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codex output is not valid JSON for {agent.agent_id}") from exc
        attempt_output_path.replace(output_path)
        return RunnerResult(output=output, output_path=output_path, trace_path=trace_path)

    @staticmethod
    def _record_trace(
        path: Path, stdout: str | bytes | None, stderr: str | bytes | None,
        failure: dict[str, str] | None = None,
    ) -> None:
        # Append so the semantic correction attempt cannot erase the first trace.
        with path.open("a", encoding="utf-8") as stream:
            for value in (stdout, stderr):
                if value:
                    stream.write(value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value)
                    stream.write("\n")
            if failure is not None:
                stream.write(json.dumps(failure, ensure_ascii=False) + "\n")

    def _command(self, agent: AgentSpec, schema_path: Path, output_path: Path) -> list[str]:
        command = [
            self.command,
            "exec",
            "--ephemeral",
            "--json",
            "--model",
            agent.model,
            "--config",
            f'model_reasoning_effort="{agent.reasoning_effort}"',
            "--config",
            'forced_login_method="chatgpt"',
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "Read the supplied task context and return only JSON that satisfies the output schema.",
        ]
        if agent.requires_web_research:
            command[2:2] = ["--enable", "browser_use"]
        return command


def codex_chatgpt_login_status(command: str, workspace: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [command, "login", "status"], cwd=workspace, text=True,
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode == 0:
        if "chatgpt" in detail.lower():
            return True, detail
        return False, detail or "Codex CLI is not logged in with ChatGPT"
    return False, detail


def codex_network_status(timeout_seconds: int = 5) -> tuple[bool, str]:
    request = Request("https://api.openai.com/v1/models", method="HEAD")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return True, f"HTTP {response.status}"
    except HTTPError as exc:
        if exc.code in {400, 401, 403, 404, 405}:
            return True, f"HTTP {exc.code}"
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, f"Network check failed: {type(exc.reason).__name__}"
    except OSError as exc:
        return False, f"Network check failed: {type(exc).__name__}"


def codex_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODEX_API_KEY", None)
    if not environment.get("SSL_CERT_FILE"):
        try:
            import certifi
        except ImportError:
            pass
        else:
            environment["SSL_CERT_FILE"] = certifi.where()
    return environment


def _failure_detail(stderr: str, stdout: str) -> str:
    detail = (stderr or stdout or "unknown error").strip()
    lower = detail.lower()
    if "not logged in" in lower or "authentication" in lower:
        return "Codex CLI is not logged in with ChatGPT"
    if "usage limit" in lower:
        return "Codex membership usage is temporarily unavailable"
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    return (lines[-1] if lines else "unknown error")[:500]


class FakeRunner:
    """Deterministic runner for pipeline and storage verification."""

    accepts_test_snapshots = True
    publishes_dashboard = False
    run_kind = "verification"

    def __init__(
        self, as_of_time: str, account_version: str = "example-account-v1"
    ):
        self.as_of_time = as_of_time
        self.account_version = account_version

    def run(self, agent: AgentSpec, prompt: str, run_dir: Path) -> RunnerResult:
        output_dir = run_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{agent.agent_id}.json"
        output = self._output_for(agent)
        context = json.loads(prompt.split("DATA:\n", 1)[1].split(
            "\n\nSEMANTIC CORRECTION RETRY", 1
        )[0])
        candidates = [
            {**candidate, "candidate_source": source}
            for source, rows in context["snapshot"].get("candidates", {}).items()
            for candidate in rows
        ]
        if agent.schema_kind is SchemaKind.CHANLUN_SELECTION:
            output["candidates"] = [{
                "candidate_source": item["candidate_source"], "symbol": item["symbol"],
                "signal_id": item["signal_id"], "priority": 0,
                **{key: "Verification only; no investment assessment." for key in (
                    "market_position", "industry_prosperity", "fundamental_assessment",
                    "liquidity_assessment", "signal_freshness", "selection_rationale",
                )},
                "supporting_evidence": [],
            } for item in candidates[:20]]
            output["excluded_candidates"] = [{
                "candidate_source": item["candidate_source"], "symbol": item["symbol"],
                "signal_id": item["signal_id"], "reason": "Verification capacity limit.",
            } for item in candidates[20:]]
        elif agent.schema_kind is SchemaKind.RESEARCH and not output["decisions"]:
            output["decisions"] = [{
                "decision_id": f"{agent.agent_id}-{index}", "subject": item["symbol"],
                "symbol": item["symbol"], "strategy": item["candidate_source"],
                "signal_id": item.get("signal_id"), "direction": "neutral",
                "forecast": {
                    "horizon_days": 5, "benchmark": "verification", "reference_price": None,
                    "max_action_price": None, "p_up": 0.2, "p_neutral": 0.6, "p_down": 0.2,
                    "return_up": None, "return_neutral": None, "return_down": None,
                    "expected_return_net": None, "signal_expires_at": None,
                },
                "supporting_evidence": [], "opposing_evidence": [],
                "invalidation_conditions": [], "data_quality": "example_only",
                "uncertainty_sources": ["No real research performed."],
                "summary": "Verification only; no investment conclusion.",
            } for index, item in enumerate(candidates)]
        elif agent.schema_kind is SchemaKind.NEWS:
            output["candidate_coverage"] = [{
                "symbol": item["symbol"], "signal_id": item.get("signal_id"),
                "conclusion": "no_material_news", "summary": "Verification only.", "risks": [],
            } for item in candidates]
        elif agent.schema_kind is SchemaKind.RISK:
            output["hard_rule_findings"] = context["upstream"].get("hard_rule_findings", [])
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return RunnerResult(output=output, output_path=output_path)

    def _output_for(self, agent: AgentSpec) -> dict[str, Any]:
        if agent.schema_kind is SchemaKind.CONCEPTS:
            return {
                "agent_id": agent.agent_id, "as_of_time": self.as_of_time,
                "month": self.as_of_time[:7],
                "concepts": [
                    {"concept_id": f"c{i}", "name": f"概念{i}", "thesis": "测试",
                     "market_environment": "测试", "selection_criteria": [],
                     "risks": [], "supporting_evidence": []}
                    for i in range(1, 4)
                ],
                "summary": "Fake monthly concepts.",
            }
        if agent.schema_kind is SchemaKind.CANDIDATE_POOL:
            return {
                "agent_id": agent.agent_id, "as_of_time": self.as_of_time,
                "concept_month": self.as_of_time[:7], "candidates": [],
                "rejected_ideas": [], "summary": "Fake candidate pool.",
            }
        if agent.schema_kind is SchemaKind.CHANLUN_SELECTION:
            return {
                "agent_id": agent.agent_id, "as_of_time": self.as_of_time,
                "candidates": [], "excluded_candidates": [],
                "summary": "Verification runner covers the supplied research universe.",
            }
        if agent.schema_kind is SchemaKind.NEWS:
            return {
                "agent_id": agent.agent_id,
                "as_of_time": self.as_of_time,
                "search_queries": [],
                "core_news": [],
                "candidate_coverage": [],
                "rejected_noise": [],
                "data_quality": "example_only",
                "summary": "Fake output. No web research was performed.",
            }
        if agent.schema_kind is SchemaKind.RESEARCH:
            return {
                "agent_id": agent.agent_id,
                "as_of_time": self.as_of_time,
                "decisions": [],
                "rejected_candidates": [],
                "overall_data_quality": "example_only",
                "overall_summary": "Fake output. No real investment conclusion.",
            }
        if agent.schema_kind is SchemaKind.CHALLENGE:
            return {
                "agent_id": agent.agent_id,
                "as_of_time": self.as_of_time,
                "challenges": [],
                "shared_source_risks": [],
                "summary": "No real candidates were supplied.",
            }
        if agent.schema_kind is SchemaKind.PORTFOLIO:
            return {
                "agent_id": agent.agent_id,
                "as_of_time": self.as_of_time,
                "account_version": self.account_version,
                "selected_scenario": "cash_only",
                "cash_weight": 1.0,
                "target_positions": [],
                "worst_case": "No real analysis was performed.",
                "alternative": "Remain in cash.",
                "rationale": "Fake runner always selects cash.",
            }
        if agent.schema_kind is SchemaKind.RISK:
            return {
                "agent_id": agent.agent_id,
                "as_of_time": self.as_of_time,
                "decision": "defer",
                "approved_cash_weight": 1.0,
                "approved_positions": [],
                "monitored_positions": [],
                "hard_rule_findings": [
                    "Current market session or execution-safety data is not execution eligible.",
                    "Real trading is disabled until P0 parameters and acceptance tests are complete.",
                ],
                "scenario_risks": [],
                "required_changes": ["Complete P0 configuration before real trading."],
                "rationale": "Fake runner cannot approve a real trade.",
            }
        return {
            "agent_id": agent.agent_id,
            "period": "example",
            "strategy_attribution": [],
            "portfolio_findings": [],
            "risk_findings": [],
            "execution_findings": [],
            "proposed_changes": [],
            "insufficient_evidence": ["No live history."],
            "summary": "Fake monthly review.",
        }
