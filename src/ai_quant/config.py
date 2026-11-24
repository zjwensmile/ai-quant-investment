from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    runtime_dir: Path
    database_path: Path
    market_database_path: Path | None = None
    codex_command: str = "codex"
    max_parallel_agents: int = 2
    history_workers: int = 4
    history_request_delay_seconds: float = 0.25
    real_trading_enabled: bool = False
    agent_timeout_seconds: int = 900
    commission_minimum: Decimal = Decimal("0")
    commission_rate: Decimal | None = None
    commission_fixed_per_order: Decimal | None = None
    stock_sell_stamp_duty_rate: Decimal = Decimal("0.0005")
    risk_warning_permission: bool | None = None
    exclude_risk_warning_securities: bool = True
    simulation_all_or_none: bool = True
    simulation_buy_at_limit_up: bool = False
    simulation_sell_at_limit_down: bool = False
    market_data_max_age_seconds: int = 300
    analysis_market_data_max_age_seconds: int = 1800
    chanlun_bridge_path: Path | None = None
    chanlun_mode: str = "shadow"

    @classmethod
    def load(cls, workspace: Path | None = None) -> "Settings":
        root = (workspace or Path.cwd()).resolve()
        values: dict[str, object] = {}
        config_path = root / "config" / "system.json"
        if config_path.exists():
            values = json.loads(config_path.read_text(encoding="utf-8"))

        runtime_dir = root / "runtime"
        raw_bridge_path = os.getenv(
            "AI_QUANT_CHANLUN_BRIDGE_PATH",
            str(values.get(
                "chanlun_bridge_path",
                "rust/chanlun_bridge/target/release/chanlun_bridge.exe",
            )),
        )
        bridge_path = Path(raw_bridge_path)
        if not bridge_path.is_absolute():
            bridge_path = root / bridge_path
        chanlun_mode = str(
            os.getenv("AI_QUANT_CHANLUN_MODE", values.get("chanlun_mode", "shadow"))
        ).lower()
        if chanlun_mode not in {"shadow", "active", "disabled"}:
            raise ValueError("chanlun_mode must be shadow, active, or disabled")
        history_workers = int(values.get("history_workers", 4))
        history_delay = float(values.get("history_request_delay_seconds", 0.25))
        if not 1 <= history_workers <= 4:
            raise ValueError("history_workers must be within 1..4")
        if not math.isfinite(history_delay) or history_delay < 0:
            raise ValueError("history_request_delay_seconds must be finite and nonnegative")
        return cls(
            workspace=root,
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "ai_quant_v2.sqlite3",
            market_database_path=runtime_dir / "market.sqlite3",
            codex_command=os.getenv(
                "AI_QUANT_CODEX_COMMAND", str(values.get("codex_command", "codex"))
            ),
            max_parallel_agents=int(values.get("max_parallel_agents", 2)),
            history_workers=history_workers,
            history_request_delay_seconds=history_delay,
            real_trading_enabled=bool(values.get("real_trading_enabled", False)),
            agent_timeout_seconds=int(values.get("agent_timeout_seconds", 900)),
            commission_minimum=Decimal(str(values.get("commission_minimum", 0))),
            commission_rate=(
                Decimal(str(values["commission_rate"]))
                if values.get("commission_rate") is not None
                else None
            ),
            commission_fixed_per_order=(
                Decimal(str(values["commission_fixed_per_order"]))
                if values.get("commission_fixed_per_order") is not None
                else None
            ),
            stock_sell_stamp_duty_rate=Decimal(
                str(values.get("stock_sell_stamp_duty_rate", "0.0005"))
            ),
            risk_warning_permission=(
                bool(values["risk_warning_permission"])
                if values.get("risk_warning_permission") is not None
                else None
            ),
            exclude_risk_warning_securities=bool(
                values.get("exclude_risk_warning_securities", True)
            ),
            simulation_all_or_none=bool(values.get("simulation_all_or_none", True)),
            simulation_buy_at_limit_up=bool(
                values.get("simulation_buy_at_limit_up", False)
            ),
            simulation_sell_at_limit_down=bool(
                values.get("simulation_sell_at_limit_down", False)
            ),
            market_data_max_age_seconds=int(values.get("market_data_max_age_seconds", 300)),
            analysis_market_data_max_age_seconds=int(
                values.get("analysis_market_data_max_age_seconds", 1800)
            ),
            chanlun_bridge_path=bridge_path,
            chanlun_mode=chanlun_mode,
        )

    def ensure_directories(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "runs").mkdir(parents=True, exist_ok=True)

    @property
    def resolved_market_database_path(self) -> Path:
        return self.market_database_path or self.runtime_dir / "market.sqlite3"
