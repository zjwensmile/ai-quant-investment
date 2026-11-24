from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .process_owner import owner_is_alive, process_identity


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _account_model_positions(
    snapshot: dict[str, Any]
) -> tuple[list[dict[str, Any]], float]:
    budget = float(snapshot.get("account", {}).get("capital_budget") or 0)
    quotes = snapshot.get("quotes", {})
    positions = []
    invested = 0.0
    for item in snapshot.get("account", {}).get("positions", []):
        symbol = str(item["symbol"])
        price = float((quotes.get(symbol) or {}).get("last") or 0)
        value = int(item.get("quantity") or 0) * price
        weight = value / budget if budget > 0 else 0.0
        invested += weight
        positions.append({
            "symbol": symbol, "target_weight": weight, "action": "hold",
            "decision_ids": [],
        })
    return positions, max(1.0 - invested, 0.0)


class AuditStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    as_of_time TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    code_version TEXT NOT NULL DEFAULT 'unknown',
                    config_hash TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_executions (
                    execution_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    agent_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output_path TEXT,
                    trace_path TEXT,
                    trace_hash TEXT,
                    output_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS account_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_items (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    execution_id TEXT NOT NULL REFERENCES agent_executions(execution_id),
                    agent_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    stance TEXT NOT NULL,
                    claim TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_time TEXT,
                    published_at TEXT,
                    available_at TEXT,
                    quality TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    raw_content_hash TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, agent_id, evidence_id, stance)
                );

                CREATE TABLE IF NOT EXISTS news_items (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    execution_id TEXT NOT NULL REFERENCES agent_executions(execution_id),
                    agent_id TEXT NOT NULL,
                    news_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    is_primary_source INTEGER NOT NULL,
                    event_time TEXT,
                    published_at TEXT,
                    available_at TEXT NOT NULL,
                    affected_symbols_json TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    novelty REAL NOT NULL,
                    materiality REAL NOT NULL,
                    confidence REAL NOT NULL,
                    priced_in REAL NOT NULL,
                    impact_horizon TEXT NOT NULL,
                    core_reason TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    raw_content_hash TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, agent_id, news_id)
                );

                CREATE INDEX IF NOT EXISTS idx_evidence_source
                ON evidence_items(source, published_at);

                CREATE INDEX IF NOT EXISTS idx_news_published
                ON news_items(published_at, source_name);

                CREATE TABLE IF NOT EXISTS order_plans (
                    plan_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    account_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_state_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    order_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL REFERENCES order_plans(plan_id),
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_order_state_events_order
                ON order_state_events(order_id, sequence);

                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    execution_id TEXT NOT NULL REFERENCES agent_executions(execution_id),
                    agent_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    as_of_time TEXT NOT NULL,
                    price_cutoff_date TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    benchmark TEXT NOT NULL,
                    reference_price REAL,
                    p_up REAL NOT NULL,
                    p_neutral REAL NOT NULL,
                    p_down REAL NOT NULL,
                    return_up REAL,
                    return_neutral REAL,
                    return_down REAL,
                    expires_at TEXT,
                    raw_json TEXT NOT NULL,
                    UNIQUE(run_id, agent_id, decision_id)
                );

                CREATE TABLE IF NOT EXISTS decision_selection_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    decision_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prediction_outcomes (
                    prediction_id TEXT PRIMARY KEY REFERENCES predictions(prediction_id),
                    status TEXT NOT NULL,
                    start_trade_date TEXT,
                    end_trade_date TEXT,
                    start_price REAL,
                    end_price REAL,
                    realized_return REAL,
                    actual_class TEXT,
                    brier_score REAL,
                    evaluated_at TEXT NOT NULL,
                    method_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_portfolios (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    selected_scenario TEXT NOT NULL,
                    risk_decision TEXT NOT NULL,
                    cash_weight REAL NOT NULL,
                    positions_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS counterfactual_scenarios (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    scenario_id TEXT NOT NULL,
                    selected INTEGER NOT NULL,
                    risk_outcome TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, scenario_id)
                );

                CREATE TABLE IF NOT EXISTS system_tasks (
                    task_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    output TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS shadow_fills (
                    order_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    plan_id TEXT NOT NULL REFERENCES order_plans(plan_id),
                    model_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL,
                    fill_trade_date TEXT,
                    fill_price REAL,
                    notional REAL,
                    fees REAL,
                    payload_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shadow_settlements (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    plan_id TEXT NOT NULL REFERENCES order_plans(plan_id),
                    as_of_date TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(agent_executions)")
            }
            for table, additions in {
                "system_tasks": {"owner_json": "TEXT", "child_owner_json": "TEXT", "parent_task_id": "TEXT"},
                "runs": {"owner_json": "TEXT", "system_task_id": "TEXT"},
            }.items():
                existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            if "trace_path" not in columns:
                connection.execute("ALTER TABLE agent_executions ADD COLUMN trace_path TEXT")
            if "trace_hash" not in columns:
                connection.execute("ALTER TABLE agent_executions ADD COLUMN trace_hash TEXT")
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(runs)")
            }
            if "code_version" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN code_version TEXT NOT NULL DEFAULT 'unknown'"
                )
            if "config_hash" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN config_hash TEXT NOT NULL DEFAULT 'unknown'"
                )
            prediction_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(predictions)")
            }
            if "price_cutoff_date" not in prediction_columns:
                connection.execute(
                    "ALTER TABLE predictions ADD COLUMN price_cutoff_date TEXT NOT NULL DEFAULT ''"
                )
            for table in ("evidence_items", "news_items"):
                artifact_columns = {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                additions = {
                    "event_time": "TEXT",
                    "fetched_at": "TEXT NOT NULL DEFAULT ''",
                    "raw_content_hash": "TEXT NOT NULL DEFAULT ''",
                    "parser_version": "TEXT NOT NULL DEFAULT ''",
                }
                for name, definition in additions.items():
                    if name not in artifact_columns:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                        )

    def create_run(
        self,
        run_id: str,
        as_of_time: str,
        snapshot_hash: str,
        code_version: str = "unknown",
        config_hash: str = "unknown",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, as_of_time, snapshot_hash, code_version, "
                "config_hash, status, created_at, owner_json, system_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, as_of_time, snapshot_hash, code_version, config_hash,
                    "running", utc_now(), json.dumps(process_identity()),
                    os.environ.get("AI_QUANT_SYSTEM_TASK_ID"),
                ),
            )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, completed_at = ?, error = ? WHERE run_id = ?",
                (status, utc_now(), error, run_id),
            )

    def start_execution(
        self,
        execution_id: str,
        run_id: str,
        agent_id: str,
        stage: str,
        model: str,
        reasoning_effort: str,
        input_hash: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_executions(
                    execution_id, run_id, agent_id, stage, model, reasoning_effort,
                    status, input_hash, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    run_id,
                    agent_id,
                    stage,
                    model,
                    reasoning_effort,
                    "running",
                    input_hash,
                    utc_now(),
                ),
            )

    def finish_execution(
        self,
        execution_id: str,
        status: str,
        output_path: str | None = None,
        trace_path: str | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        output_json = json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None
        trace_hash = None
        if trace_path and Path(trace_path).exists():
            trace_hash = hashlib.sha256(Path(trace_path).read_bytes()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_executions
                SET status = ?, output_path = ?, trace_path = ?, trace_hash = ?,
                    output_json = ?, completed_at = ?, error = ?
                WHERE execution_id = ?
                """,
                (
                    status, output_path, trace_path, trace_hash, output_json,
                    utc_now(), error, execution_id,
                ),
            )

    def append_account_event(
        self, event_id: str, event_type: str, event_time: str, payload: dict[str, Any]
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO account_events(event_id, event_type, event_time, payload_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    event_time,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )

    def list_account_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM account_events ORDER BY sequence"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(str(item.pop("payload_json")))
            result.append(item)
        return result

    def account_event_exists(self, event_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM account_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row is not None

    def record_agent_artifacts(
        self,
        run_id: str,
        execution_id: str,
        agent_id: str,
        output: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        evidence_rows = []
        decisions = output.get("decisions")
        if isinstance(decisions, list):
            containers = decisions
        elif isinstance(output.get("concepts"), list):
            containers = output["concepts"]
        elif isinstance(output.get("candidates"), list):
            containers = output["candidates"]
        else:
            containers = [output]
        for container in containers:
            for stance, key in (
                ("supporting", "supporting_evidence"),
                ("opposing", "opposing_evidence"),
            ):
                for item in container.get(key, []):
                    raw_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    evidence_rows.append(
                        (
                            run_id,
                            execution_id,
                            agent_id,
                            str(item["evidence_id"]),
                            stance,
                            str(item["claim"]),
                            str(item["source"]),
                            item.get("published_at"),
                            item.get("published_at"),
                            item.get("available_at"),
                            str(item["quality"]),
                            utc_now(),
                            hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                            "agent-evidence-v1",
                            raw_json,
                        )
                    )
        news_rows = []
        for item in output.get("core_news", []):
            raw_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
            news_rows.append(
                (
                    run_id,
                    execution_id,
                    agent_id,
                    str(item["news_id"]),
                    str(item["title"]),
                    str(item["url"]),
                    str(item["source_name"]),
                    int(bool(item["is_primary_source"])),
                    item.get("published_at"),
                    item.get("published_at"),
                    str(item["available_at"]),
                    json.dumps(item.get("affected_symbols", []), ensure_ascii=False),
                    str(item["direction"]),
                    float(item["novelty"]),
                    float(item["materiality"]),
                    float(item["confidence"]),
                    float(item["priced_in"]),
                    str(item["impact_horizon"]),
                    str(item["core_reason"]),
                    utc_now(),
                    hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                    "agent-news-v1",
                    raw_json,
                )
            )
        prediction_rows = []
        if isinstance(decisions, list):
            for item in decisions:
                symbol = item.get("symbol")
                if not symbol:
                    continue
                forecast = item["forecast"]
                prediction_id = f"{run_id}:{agent_id}:{item['decision_id']}"
                price_cutoff = (
                    (snapshot or {}).get("feature_coverage", {}).get("daily_bar_cutoff")
                    or str(output["as_of_time"])[:10]
                )
                prediction_rows.append(
                    (
                        prediction_id, run_id, execution_id, agent_id,
                        str(item["decision_id"]), str(item["subject"]), str(symbol),
                        str(item["strategy"]), str(item["direction"]),
                        str(output["as_of_time"]), str(price_cutoff),
                        int(forecast["horizon_days"]),
                        str(forecast["benchmark"]), forecast.get("reference_price"),
                        float(forecast["p_up"]), float(forecast["p_neutral"]),
                        float(forecast["p_down"]), forecast.get("return_up"),
                        forecast.get("return_neutral"), forecast.get("return_down"),
                        forecast.get("signal_expires_at"),
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                    )
                )
        with self._connect() as connection:
            if evidence_rows:
                connection.executemany(
                    "INSERT OR REPLACE INTO evidence_items("
                    "run_id, execution_id, agent_id, evidence_id, stance, claim, source, "
                    "event_time, published_at, available_at, quality, fetched_at, "
                    "raw_content_hash, parser_version, raw_json) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    evidence_rows,
                )
            if news_rows:
                connection.executemany(
                    "INSERT OR REPLACE INTO news_items("
                    "run_id, execution_id, agent_id, news_id, title, url, source_name, "
                    "is_primary_source, event_time, published_at, available_at, "
                    "affected_symbols_json, direction, novelty, materiality, confidence, "
                    "priced_in, impact_horizon, core_reason, fetched_at, raw_content_hash, "
                    "parser_version, raw_json) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    news_rows,
                )
            if prediction_rows:
                connection.executemany(
                    "INSERT OR REPLACE INTO predictions(" 
                    "prediction_id, run_id, execution_id, agent_id, decision_id, subject, "
                    "symbol, strategy, direction, as_of_time, price_cutoff_date, horizon_days, "
                    "benchmark, reference_price, p_up, p_neutral, p_down, return_up, "
                    "return_neutral, return_down, expires_at, raw_json) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    prediction_rows,
                )
        return {
            "evidence": len(evidence_rows),
            "news": len(news_rows),
            "predictions": len(prediction_rows),
        }

    def record_selection_events(
        self,
        run_id: str,
        portfolio: dict[str, Any],
        risk: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            decisions = [
                str(row[0]) for row in connection.execute(
                    "SELECT decision_id FROM predictions WHERE run_id = ?", (run_id,)
                ).fetchall()
            ]
            selected = {
                str(decision_id): position
                for position in portfolio.get("target_positions", [])
                for decision_id in position.get("decision_ids", [])
            }
            approved_by_symbol = {
                str(item["symbol"]): item for item in risk.get("approved_positions", [])
            }
            risk_decision = str(risk.get("decision") or "unknown")
            rows = []
            for decision_id in decisions:
                position = selected.get(decision_id)
                portfolio_outcome = "selected" if position else "not_selected"
                rows.append((
                    f"{run_id}:portfolio:{decision_id}", run_id, decision_id,
                    "portfolio", portfolio_outcome,
                    json.dumps(position or {}, ensure_ascii=False, sort_keys=True), utc_now(),
                ))
                if position:
                    approved = approved_by_symbol.get(str(position["symbol"]))
                    if risk_decision in {"defer", "veto"}:
                        risk_outcome = risk_decision
                    elif approved is None:
                        risk_outcome = "rejected"
                    elif float(approved["target_weight"]) + 1e-9 < float(position["target_weight"]):
                        risk_outcome = "reduced"
                    else:
                        risk_outcome = "approved"
                    rows.append((
                        f"{run_id}:risk:{decision_id}", run_id, decision_id,
                        "risk", risk_outcome,
                        json.dumps(approved or {}, ensure_ascii=False, sort_keys=True), utc_now(),
                    ))
            connection.executemany(
                "INSERT OR IGNORE INTO decision_selection_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def pending_predictions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.* FROM predictions p LEFT JOIN prediction_outcomes o "
                "ON o.prediction_id = p.prediction_id WHERE o.prediction_id IS NULL "
                "ORDER BY p.as_of_time, p.prediction_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_prediction_outcome(self, outcome: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO prediction_outcomes VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome["prediction_id"], outcome["status"],
                    outcome.get("start_trade_date"), outcome.get("end_trade_date"),
                    outcome.get("start_price"), outcome.get("end_price"),
                    outcome.get("realized_return"), outcome.get("actual_class"),
                    outcome.get("brier_score"), outcome["evaluated_at"],
                    outcome["method_version"],
                    json.dumps(outcome, ensure_ascii=False, sort_keys=True),
                ),
            )

    def evaluation_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.*, o.status outcome_status, o.realized_return, o.actual_class, "
                "o.brier_score, o.start_trade_date, o.end_trade_date, "
                "(SELECT s.outcome FROM decision_selection_events s WHERE s.run_id=p.run_id "
                "AND s.decision_id=p.decision_id AND s.stage='portfolio' LIMIT 1) "
                "portfolio_outcome, "
                "(SELECT s.outcome FROM decision_selection_events s WHERE s.run_id=p.run_id "
                "AND s.decision_id=p.decision_id AND s.stage='risk' LIMIT 1) risk_outcome "
                "FROM predictions p LEFT JOIN prediction_outcomes o "
                "ON o.prediction_id = p.prediction_id ORDER BY p.as_of_time"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_model_ledgers(
        self,
        run_id: str,
        scenarios: list[dict[str, Any]],
        portfolio: dict[str, Any],
        risk: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        selected_id = str(portfolio["selected_scenario"])
        risk_decision = str(risk["decision"])
        approved = risk.get("approved_positions", [])
        account_version = str(
            (snapshot or {}).get("account", {}).get("version") or "unknown"
        )
        with self._connect() as connection:
            actionable = risk_decision not in {"defer", "veto"}
            effective_positions = approved
            effective_cash = float(risk.get("approved_cash_weight", 1.0))
            if not actionable:
                previous = connection.execute(
                    "SELECT positions_json, cash_weight FROM model_portfolios "
                    "WHERE json_extract(payload_json, '$.account_version') = ? "
                    "ORDER BY created_at DESC LIMIT 1"
                    , (account_version,)
                ).fetchone()
                if previous is not None:
                    effective_positions = json.loads(str(previous[0]))
                    effective_cash = float(previous[1])
                else:
                    effective_positions, effective_cash = _account_model_positions(
                        snapshot or {}
                    )
            connection.execute(
                "INSERT OR REPLACE INTO model_portfolios VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, selected_id, risk_decision,
                    effective_cash,
                    json.dumps(effective_positions, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {
                            "account_version": account_version,
                            "portfolio": portfolio, "risk": risk,
                            "actionable": actionable,
                            "effective_positions": effective_positions,
                        },
                        ensure_ascii=False, sort_keys=True,
                    ),
                    utc_now(),
                ),
            )
            rows = []
            for scenario in scenarios:
                selected = str(scenario["scenario_id"]) == selected_id
                outcome = risk_decision if selected else "not_selected"
                rows.append((
                    run_id, str(scenario["scenario_id"]), int(selected), outcome,
                    json.dumps(scenario, ensure_ascii=False, sort_keys=True), utc_now(),
                ))
            connection.executemany(
                "INSERT OR REPLACE INTO counterfactual_scenarios VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def model_portfolio(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_portfolios WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["positions"] = json.loads(str(item.pop("positions_json")))
        return item

    def counterfactual_scenarios(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM counterfactual_scenarios WHERE run_id = ? ORDER BY scenario_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_system_task(
        self, task_id: str, action: str, command: list[str], *, parent_task_id: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_abandoned(connection)
            if parent_task_id:
                parent = connection.execute(
                    "SELECT owner_json FROM system_tasks WHERE task_id = ? AND status = 'running'",
                    (parent_task_id,),
                ).fetchone()
                if not parent or json.loads(parent[0] or "null") != process_identity():
                    raise ValueError("工作流所有者不匹配")
            if connection.execute(
                "SELECT 1 FROM system_tasks WHERE status = 'running' AND task_id != ?",
                (parent_task_id or "",),
            ).fetchone():
                raise ValueError("已有系统任务正在运行；无所有者的旧记录需核实进程后显式恢复")
            connection.execute(
                "INSERT INTO system_tasks(task_id, action, command_json, status, created_at, owner_json, parent_task_id) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (task_id, action, json.dumps(command, ensure_ascii=False), utc_now(),
                 json.dumps(process_identity()), parent_task_id),
            )

    def launch_task_process(self, task_id: str, launch: Any) -> Any:
        # Recovery cannot inspect the task between process launch and identity recording.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            process = launch()
            connection.execute(
                "UPDATE system_tasks SET child_owner_json = ? WHERE task_id = ?",
                (json.dumps(process_identity(process.pid)), task_id),
            )
        return process

    def recover_abandoned_tasks(self, *, recover_legacy: bool = False) -> list[dict[str, str]]:
        """recover_legacy requires prior verification that old unowned jobs have exited."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._recover_abandoned(connection, recover_legacy=recover_legacy)

    def _recover_abandoned(
        self, connection: sqlite3.Connection, *, recover_legacy: bool = False
    ) -> list[dict[str, str]]:
        recovered = []
        for table, key in (("system_tasks", "task_id"), ("runs", "run_id")):
            for row in connection.execute(f"SELECT * FROM {table} WHERE status = 'running'").fetchall():
                owner = json.loads(row["owner_json"] or "null")
                child = json.loads(row["child_owner_json"] or "null") if table == "system_tasks" else None
                if (owner and owner_is_alive(owner)) or (child and owner_is_alive(child)):
                    continue
                if not owner and not recover_legacy:
                    continue
                reason = (
                    "Recovered abandoned task: owner and child processes have exited"
                    if owner else "Recovered legacy running record after operator verified no live process"
                )
                connection.execute(
                    f"UPDATE {table} SET status = 'failed', completed_at = ?, "
                    f"error = CASE WHEN error IS NULL THEN ? ELSE error || char(10) || ? END WHERE {key} = ?",
                    (utc_now(), reason, reason, row[key]),
                )
                if table == "runs":
                    connection.execute(
                        "UPDATE agent_executions SET status = 'failed', completed_at = ?, "
                        "error = CASE WHEN error IS NULL THEN ? ELSE error || char(10) || ? END "
                        "WHERE run_id = ? AND status = 'running'", (utc_now(), reason, reason, row[key]),
                    )
                recovered.append({key: row[key], "reason": reason})
        return recovered

    def finish_system_task(
        self, task_id: str, status: str, output: str, error: str | None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE system_tasks SET status = ?, completed_at = ?, output = ?, error = ? "
                "WHERE task_id = ?",
                (status, utc_now(), output[-20000:], error, task_id),
            )

    def running_system_task(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM system_tasks WHERE status = 'running' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_system_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM system_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_shadow_fill(self, fill: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO shadow_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill["order_id"], fill["run_id"], fill["plan_id"],
                    fill["model_version"], fill["status"], fill["requested_quantity"],
                    fill["filled_quantity"], fill.get("fill_trade_date"),
                    fill.get("fill_price"), fill.get("notional"), fill.get("fees"),
                    json.dumps(fill, ensure_ascii=False, sort_keys=True), utc_now(),
                ),
            )

    def shadow_fills(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_fills WHERE run_id = ? ORDER BY order_id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_shadow_settlement(self, settlement: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO shadow_settlements VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    settlement["run_id"], settlement["plan_id"],
                    settlement["as_of_date"], settlement["model_version"],
                    settlement["status"],
                    json.dumps(settlement, ensure_ascii=False, sort_keys=True), utc_now(),
                ),
            )

    def shadow_settlement(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_settlements WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def completed_shadow_settlements(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_settlements WHERE status = 'completed' "
                "ORDER BY as_of_date, run_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def unsettled_shadow_plans(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT order_plans.payload_json FROM order_plans "
                "LEFT JOIN shadow_settlements USING(run_id) "
                "WHERE shadow_settlements.status IS NULL "
                "OR shadow_settlements.status != 'completed' "
                "ORDER BY order_plans.created_at"
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def list_evidence(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_items WHERE run_id = ? ORDER BY agent_id, evidence_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_news(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM news_items WHERE run_id = ? ORDER BY published_at DESC, news_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_order_plan(self, plan: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO order_plans(
                    plan_id, run_id, account_version, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["plan_id"],
                    plan["run_id"],
                    plan["account_version"],
                    plan["status"],
                    json.dumps(plan, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )

    def get_order_plan(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM order_plans WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_order_plan_by_id(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM order_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def linked_filled_quantity(self, order_id: str) -> int:
        total = 0
        for event in self.list_account_events():
            payload = event["payload"]
            if event["event_type"] == "fill" and payload.get("order_id") == order_id:
                total += int(payload.get("quantity") or 0)
        return total

    def append_linked_fill(
        self,
        account_event_id: str,
        event_time: str,
        fill_payload: dict[str, Any],
        state_event_id: str,
        order_id: str,
        plan_id: str,
        from_state: str,
        to_state: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO account_events(event_id, event_type, event_time, payload_json, recorded_at) "
                "VALUES (?, 'fill', ?, ?, ?)",
                (
                    account_event_id, event_time,
                    json.dumps(fill_payload, ensure_ascii=False, sort_keys=True), utc_now(),
                ),
            )
            connection.execute(
                """
                INSERT INTO order_state_events(
                    event_id, order_id, plan_id, from_state, to_state,
                    event_time, reason, payload_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state_event_id, order_id, plan_id, from_state, to_state,
                    event_time, "券商实际成交已确认",
                    json.dumps({"account_event_id": account_event_id}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def append_order_state_event(
        self,
        event_id: str,
        order_id: str,
        plan_id: str,
        from_state: str | None,
        to_state: str,
        event_time: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO order_state_events(
                    event_id, order_id, plan_id, from_state, to_state,
                    event_time, reason, payload_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, order_id, plan_id, from_state, to_state, event_time,
                    reason, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )

    def current_order_state(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_state_events WHERE order_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def order_state_events(self, plan_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM order_state_events WHERE plan_id = ? ORDER BY sequence",
                (plan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                return None
            executions = connection.execute(
                "SELECT * FROM agent_executions WHERE run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
        result = dict(run)
        result["executions"] = [dict(row) for row in executions]
        return result
