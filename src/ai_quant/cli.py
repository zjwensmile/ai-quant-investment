from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time as time_module
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .account import AccountLedger, build_fill_payload, record_confirmed_fill
from .acceptance import AcceptanceAuditor
from .backup import BackupService
from .chanlun import (
    POLICY_VERSION, ExecutionRules, MarketBar, RustChanlunBridge,
    confirm_recent_scans, run_acceptance,
    select_candidates,
)
from .backtest import SourceBacktester
from .config import Settings
from .datahub import BuiltSnapshot, DataHub, SnapshotBuildError
from .dashboard import serve
from .exchange_reference import ExchangeReferenceProvider
from .evaluation import EvaluationEngine, evaluation_report
from .features import FeatureEngine, _chanlun_bars
from .fundamentals import (
    AkshareFinancialReportProvider, FundamentalSyncService, recent_completed_periods,
)
from .history import HistorySyncService
from .market_store import DailyBar, MarketStore, StoredEtfTradeAttribute
from .order_state import OrderState, validate_transition
from .order_plans import validate_order_plan_account_version
from .pipeline import Pipeline
from .providers import AkshareProvider, ProviderError
from .reference_data import IndustrySyncService
from .registry import all_agents
from .rules import RuleBook, Side
from .securities import Exchange, SecurityType, normalize_symbol
from .simulation import MODEL_VERSION, ShadowExecutionEngine
from .scheduler import (
    SchedulePlan, WindowsTaskScheduler, load_configured_schedule,
)
from .runner import (
    CodexRunner,
    FakeRunner,
    codex_chatgpt_login_status,
    codex_network_status,
)
from .store import AuditStore, utc_now
from .tasks import TaskController
from .workflows import DailyWorkflow


def _settings() -> Settings:
    return Settings.load(Path.cwd())


def _store(settings: Settings) -> AuditStore:
    settings.ensure_directories()
    store = AuditStore(settings.database_path)
    store.initialize()
    return store


def _market_store(settings: Settings) -> MarketStore:
    settings.ensure_directories()
    store = MarketStore(settings.resolved_market_database_path)
    store.initialize()
    return store


def _feature_engine(settings: Settings, market_store: MarketStore) -> FeatureEngine:
    """Keep Chanlun routing explicit at every local feature-enrichment entrypoint."""
    path = settings.chanlun_bridge_path
    bridge = (
        RustChanlunBridge([path], cwd=settings.workspace)
        if path is not None and path.is_file() else None
    )
    return FeatureEngine(
        market_store,
        chanlun_bridge=bridge,
        chanlun_mode=settings.chanlun_mode,
        chanlun_reference_passed=_chanlun_reference_passed(
            settings, market_store
        ),
    )


def _chanlun_acceptance_path(settings: Settings) -> Path:
    return settings.runtime_dir / "chanlun" / "acceptance.json"


def _active_chanlun_symbols(market_store: MarketStore) -> list[str]:
    return sorted({
        str(item.get("symbol") or "")
        for item in market_store.security_master_rows(active_only=True)
        if item.get("symbol")
    })


def _symbol_signature(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(symbols)).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chanlun_policy_sha256(settings: Settings) -> str | None:
    paths = [
        settings.workspace / "src/ai_quant/chanlun.py",
        settings.workspace / "src/ai_quant/features.py",
    ]
    if any(not path.is_file() for path in paths):
        return None
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _chanlun_reference_passed(
    settings: Settings, market_store: MarketStore
) -> bool:
    try:
        receipt = json.loads(
            _chanlun_acceptance_path(settings).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    symbols = _active_chanlun_symbols(market_store)
    policy_hash = _chanlun_policy_sha256(settings)
    return bool(
        receipt.get("schema_version") == "chanlun-acceptance/v1"
        and receipt.get("reference_criteria_met") is True
        and receipt.get("result", {}).get("passed") is True
        and receipt.get("result", {}).get("sufficient_sample") is True
        and receipt.get("universe", {}).get("kind") == "active_security_master"
        and receipt.get("universe", {}).get("symbol_signature")
        == _symbol_signature(symbols)
        and receipt.get("algorithm", {}).get("bridge_sha256")
        == _file_sha256(settings.chanlun_bridge_path or Path())
        and receipt.get("algorithm", {}).get("policy_version") == POLICY_VERSION
        and policy_hash is not None
        and receipt.get("algorithm", {}).get("policy_sha256") == policy_hash
    )


def _print_json(value: object) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    encoding = sys.stdout.encoding or "utf-8"
    print(rendered.encode(encoding, errors="replace").decode(encoding))


def command_init(args: argparse.Namespace) -> int:
    settings = _settings()
    _store(settings)
    _market_store(settings)
    target = settings.workspace / "config" / "system.json"
    source = settings.workspace / "config" / "system.example.json"
    if not target.exists():
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created {target}")
    print(f"Database ready: {settings.database_path}")
    print(f"Market database ready: {settings.resolved_market_database_path}")
    return 0


def command_agents(args: argparse.Namespace) -> int:
    print(json.dumps([agent.to_dict() for agent in all_agents()], ensure_ascii=False, indent=2))
    return 0


def command_module_run(args: argparse.Namespace) -> int:
    from .module_data import run_module_action
    settings = _settings()
    result = run_module_action(args.module, args.action, settings, _store(settings), _market_store(settings))
    _print_json({key: result.get(key) for key in ("module", "scan_id", "run_id", "status", "as_of_time", "checked_at")})
    return 0


def command_plan(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    snapshot_path = Path(args.snapshot).resolve()
    pipeline = Pipeline(settings, store, FakeRunner("unused"))
    manifest = pipeline.plan(snapshot_path, include_monthly=args.monthly)
    print(manifest)
    return 0


def command_run(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    snapshot_path = Path(args.snapshot).resolve()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if args.current_account:
        snapshot_path, snapshot = _analysis_snapshot_with_current_account(
            settings, store, _market_store(settings), snapshot_path
        )
    if args.runner == "fake":
        runner = FakeRunner(
            snapshot["as_of_time"], str(snapshot.get("account", {}).get("version") or "unknown")
        )
    else:
        logged_in, login_detail = codex_chatgpt_login_status(
            settings.codex_command, settings.workspace
        )
        if not logged_in:
            print(f"Codex 会员登录不可用: {login_detail}", file=sys.stderr)
            return 2
        reachable, network_detail = codex_network_status()
        if not reachable:
            print(f"Codex 网络不可用: {network_detail}", file=sys.stderr)
            return 2
        runner = CodexRunner(
            command=settings.codex_command,
            workspace=settings.workspace,
            timeout_seconds=settings.agent_timeout_seconds,
        )
    run_id = Pipeline(settings, store, runner).run(snapshot_path, include_monthly=args.monthly)
    print(run_id)
    return 0


def _analysis_snapshot_with_current_account(
    settings: Settings,
    store: AuditStore,
    market_store: MarketStore,
    source: Path,
) -> tuple[Path, dict[str, object]]:
    data = json.loads(source.read_text(encoding="utf-8"))
    account = AccountLedger.reduce(store.list_account_events())
    if account is None:
        return source, data
    data["account"] = account
    enriched = _feature_engine(settings, market_store).enrich(data)
    target = (
        settings.runtime_dir / "snapshots"
        / f"analysis-account-{uuid.uuid4().hex}.json"
    )
    BuiltSnapshot(enriched).write(target)
    return target.resolve(), enriched


def command_runs(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    print(json.dumps(store.list_runs(args.limit), ensure_ascii=False, indent=2))
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    settings = _settings()
    settings.ensure_directories()
    as_of = datetime.now(ZoneInfo("Asia/Shanghai"))
    target = Path(args.output).resolve() if args.output else None
    try:
        account = _load_account(settings, args.account)
        total_stages = 4 if args.without_features else 5
        _report_task_progress(
            "正在抓取全市场证券主数据与实时行情", 1, total_stages
        )
        snapshot = DataHub(AkshareProvider()).build_snapshot(as_of, account=account)
        if args.account is None:
            current_account = _load_account(settings, None)
            if current_account is not None:
                snapshot.data["account"] = current_account
        market_store = _market_store(settings)
        _report_task_progress("正在校验并写入证券主数据", 2, total_stages)
        market_store.apply_etf_attributes(snapshot.data)
        market_store.upsert_security_master(
            snapshot.data["snapshot_id"],
            snapshot.data["securities"],
            snapshot.data["data_quality"]["available_at"],
        )
        _report_task_progress("正在同步盘后原始日线", 3, total_stages)
        _persist_post_market_bars(market_store, snapshot.data)
        if not args.without_features:
            _report_task_progress("正在计算特征与日线缠论候选", 4, total_stages)
            snapshot = BuiltSnapshot(
                _feature_engine(settings, market_store).enrich(snapshot.data)
            )
        if target is None:
            target = settings.runtime_dir / "snapshots" / f"{snapshot.data['snapshot_id']}.json"
        _report_task_progress("正在写入可审计市场快照", total_stages, total_stages)
        snapshot.write(target)
    except (ProviderError, SnapshotBuildError, ValueError) as exc:
        print(f"快照生成失败，未产生可交易快照: {exc}", file=sys.stderr)
        return 2
    print(target)
    quality = snapshot.data["data_quality"]
    print(f"数据状态: {quality['status']}")
    print(f"证券数量: {len(snapshot.data['securities'])}")
    for warning in quality.get("warnings", []):
        print(f"警告: {warning}")
    return 0


def _report_task_progress(phase: str, current_stage: int, total_stages: int) -> None:
    """Publish coarse, truthful stage progress when snapshot is task-controlled."""
    raw_path = os.environ.get("AI_QUANT_TASK_PROGRESS_FILE")
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase,
        "current_stage": current_stage,
        "total_stages": total_stages,
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _persist_post_market_bars(
    market_store: MarketStore, snapshot: dict[str, object]
) -> int:
    market = snapshot.get("market", {})
    if not isinstance(market, dict) or not market.get("is_trading_day"):
        return 0
    if market.get("session") != "post_market":
        return 0
    trade_date = as_local_date(str(snapshot["as_of_time"]))
    bars = []
    for symbol, quote in dict(snapshot.get("quotes", {})).items():
        if not isinstance(quote, dict):
            continue
        try:
            open_price = float(quote["open"])
            high = float(quote["high"])
            low = float(quote["low"])
            close = float(quote["last"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(open_price, high, low, close) <= 0:
            continue
        if high < max(open_price, close) or low > min(open_price, close):
            continue
        bars.append(DailyBar(
            symbol=normalize_symbol(str(symbol)), trade_date=trade_date,
            open=open_price, high=high, low=low, close=close,
            volume=_optional_float(quote.get("volume")),
            amount=_optional_float(quote.get("turnover")),
            turnover=_optional_float(quote.get("turnover_rate")),
        ))
    available_at = str(
        dict(snapshot.get("data_quality", {})).get("available_at")
        or snapshot["as_of_time"]
    )
    return market_store.upsert_bars(
        bars, "raw", "post_market_snapshot", available_at
    )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def command_history_sync(args: argparse.Namespace) -> int:
    settings = _settings()
    args.workers = settings.history_workers if args.workers is None else args.workers
    args.delay = settings.history_request_delay_seconds if args.delay is None else args.delay
    if not 1 <= args.workers <= 4 or not math.isfinite(args.delay) or args.delay < 0:
        print("历史并发需为1至4，等待秒数需为非负有限值", file=sys.stderr)
        return 2
    market_store = _market_store(settings)
    snapshot = json.loads(Path(args.snapshot).resolve().read_text(encoding="utf-8"))
    securities = snapshot.get("securities", [])
    if not securities:
        print("快照中没有证券主数据", file=sys.stderr)
        return 2
    requested = {
        normalize_symbol(item)
        for value in args.symbols
        for item in value.split(",")
        if item.strip()
    }
    held = {
        normalize_symbol(item["symbol"])
        for item in snapshot.get("account", {}).get("positions", [])
        if item.get("symbol")
    }
    available_symbols = {item["symbol"] for item in securities}
    missing = requested - available_symbols
    if missing:
        print(f"快照中找不到证券: {', '.join(sorted(missing))}", file=sys.stderr)
        return 2

    end_date = date.fromisoformat(args.end) if args.end else as_local_date(snapshot["as_of_time"])
    requested_start = (
        date.fromisoformat(args.start) if args.start else end_date - timedelta(days=730)
    )
    if requested_start > end_date:
        print("历史同步开始日期不能晚于结束日期", file=sys.stderr)
        return 2
    market_store.upsert_security_master(
        snapshot["snapshot_id"],
        securities,
        snapshot["data_quality"]["available_at"],
        deactivate_missing=not args.incremental_window,
    )
    market_store.seed_history_targets(
        securities,
        held,
        snapshot.get("quotes", {}),
        requested_start,
        end_date,
    )
    if args.stale_after_minutes < 0:
        print("中断恢复阈值不能为负数", file=sys.stderr)
        return 2
    recovered = market_store.recover_interrupted_history_syncs(
        args.stale_after_minutes
    )
    if requested:
        market_store.queue_history_targets(requested)
    elif args.refresh_existing:
        market_store.queue_history_targets(available_symbols)
    selected = market_store.pending_history_targets(
        0 if requested else args.limit,
        args.max_attempts,
        requested or available_symbols,
    )
    if recovered:
        print(f"已恢复 {recovered} 个上次中断的同步目标")
    if not selected:
        print("没有待同步目标")
        print(json.dumps(market_store.status(), ensure_ascii=False, indent=2))
        return 0

    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    if args.workers == 1:
        completed = (
            (item, _sync_history_target(
                market_store, item, end_date, not args.raw_only,
                not args.no_dividends, args.delay,
                requested_start if args.incremental_window else None,
            ))
            for item in selected
        )
    else:
        executor = ProcessPoolExecutor(max_workers=args.workers)
        futures = {
            executor.submit(
                _sync_history_target, market_store, item, end_date,
                not args.raw_only, not args.no_dividends, args.delay,
                requested_start if args.incremental_window else None,
            ): item
            for item in selected
        }
        completed = ((futures[future], future.result()) for future in as_completed(futures))
    try:
        for index, (item, (result, error)) in enumerate(completed, start=1):
            if error is None:
                results.append(result)
                print(f"[{index}/{len(selected)}] 完成 {item['symbol']} {item['name']}")
            else:
                failures.append({"symbol": str(item["symbol"]), "error": error})
                print(f"[{index}/{len(selected)}] 失败 {item['symbol']}: {error}", file=sys.stderr)
    finally:
        if args.workers > 1:
            executor.shutdown(wait=True)
    print(json.dumps({"completed": results, "failed": failures}, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


def _sync_history_target(
    market_store: MarketStore,
    item: dict[str, object],
    end_date: date,
    include_qfq: bool,
    include_dividends: bool,
    delay: float,
    incremental_start: date | None = None,
) -> tuple[dict[str, object], str | None]:
    symbol = str(item["symbol"])
    sync_id = market_store.begin_history_sync(symbol)
    try:
        latest = market_store.latest_date(symbol)
        earliest = market_store.earliest_date(symbol)
        target_start = date.fromisoformat(str(item["requested_start"]))
        start_date = incremental_start or (
            target_start
            if item["status"] == "failed"
            or (item.get("last_error") or "").startswith("上次同步进程中断")
            or (earliest is not None and earliest > target_start)
            else latest + timedelta(days=1) if latest else target_start
        )
        if start_date > end_date:
            result: dict[str, object] = {
                "symbol": symbol, "raw_bars": 0, "qfq_bars": 0,
                "corporate_actions": 0,
                "available_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
                    timespec="seconds"
                ),
            }
        else:
            result = HistorySyncService(AkshareProvider(), market_store).sync_symbol(
                symbol, Exchange(str(item["exchange"])),
                SecurityType(str(item["security_type"])), start_date, end_date,
                include_qfq=include_qfq, include_dividends=include_dividends,
            )
        latest_after = market_store.latest_date(symbol)
        if latest_after is not None and latest_after < end_date:
            warning = (
                f"行情源最新日线为 {latest_after.isoformat()}，"
                f"落后请求截止日 {end_date.isoformat()}"
            )
            result.setdefault("warnings", [])
            if warning not in result["warnings"]:
                result["warnings"].append(warning)
        market_store.finish_history_sync(sync_id, symbol, result)
        return result, None
    except Exception as exc:
        market_store.fail_history_sync(sync_id, symbol, str(exc))
        return {"symbol": symbol}, str(exc)
    finally:
        if delay > 0:
            time_module.sleep(delay)


def command_history_show(args: argparse.Namespace) -> int:
    rows = _market_store(_settings()).bars(
        normalize_symbol(args.symbol), args.adjustment, args.limit
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _chanlun_symbols(values: list[str]) -> list[str]:
    symbols = {
        normalize_symbol(symbol)
        for value in values
        for symbol in value.split(",")
        if symbol.strip()
    }
    if not symbols:
        raise ValueError("至少提供一个 --symbols 证券代码")
    return sorted(symbols)


def _local_chanlun_bars(
    market_store: MarketStore,
    symbols: list[str],
    *,
    limit: int,
    end: date | None = None,
    start: date | None = None,
) -> dict[str, list[MarketBar]]:
    result: dict[str, list[MarketBar]] = {}
    for symbol in symbols:
        rows = market_store.bars(symbol, "raw", limit, end)
        bars = [
            MarketBar(
                symbol=symbol, trade_date=date.fromisoformat(str(row["trade_date"])),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                amount=_optional_float(row.get("amount")),
            )
            for row in rows
            if start is None or date.fromisoformat(str(row["trade_date"])) >= start
        ]
        if bars:
            result[symbol] = bars
    if not result:
        raise ValueError("本地原始日线不足；请先运行 history-sync")
    return result


def _chanlun_bridge(settings: Settings) -> RustChanlunBridge:
    path = settings.chanlun_bridge_path or (
        settings.workspace / "rust/chanlun_bridge/target/release/chanlun_bridge.exe"
    )
    return RustChanlunBridge([path], cwd=settings.workspace)


def _chanlun_signal_bars(
    market_store: MarketStore,
    execution_bars: dict[str, list[MarketBar]],
    known_by: date,
) -> dict[str, list[MarketBar]]:
    """Use action-adjusted OHLC for structure while retaining raw execution bars."""
    result: dict[str, list[MarketBar]] = {}
    action_reader = getattr(market_store, "corporate_actions", None)
    for symbol, bars in execution_bars.items():
        actions = action_reader(symbol, known_by) if action_reader is not None else []
        raw_rows = [
            {
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "amount": bar.amount,
            }
            for bar in bars
        ]
        result[symbol] = _chanlun_bars(symbol, raw_rows, actions)
    return result


def _chanlun_price_limits(
    market_store: MarketStore, symbols: set[str]
) -> dict[str, float]:
    reader = getattr(market_store, "security_master_rows", None)
    if reader is None:
        return {}
    limits: dict[str, float] = {}
    for item in reader(active_only=False):
        symbol = str(item.get("symbol") or "")
        value = item.get("price_limit_ratio")
        if symbol not in symbols or value is None:
            continue
        ratio = float(value)
        if 0 < ratio < 1:
            limits[symbol] = ratio
    return limits


def _chanlun_stamp_duty_rates(
    market_store: MarketStore, symbols: set[str], stock_rate: float
) -> dict[str, float]:
    reader = getattr(market_store, "security_master_rows", None)
    if reader is None:
        return {}
    return {
        str(item.get("symbol")): (
            stock_rate if item.get("security_type") == "stock" else 0.0
        )
        for item in reader(active_only=False)
        if str(item.get("symbol") or "") in symbols
    }


def command_chanlun_scan(args: argparse.Namespace) -> int:
    """Scan locally stored raw daily bars without starting Agents or orders."""
    settings = _settings()
    try:
        market_store = _market_store(settings)
        execution_bars = _local_chanlun_bars(
            market_store, _chanlun_symbols(args.symbols), limit=args.limit,
            end=date.fromisoformat(args.as_of) if args.as_of else None,
        )
        as_of = date.fromisoformat(args.as_of) if args.as_of else max(
            bar.trade_date for bars in execution_bars.values() for bar in bars
        )
        bars_by_symbol = _chanlun_signal_bars(
            market_store, execution_bars, as_of
        )
        bridge = _chanlun_bridge(settings)
        recent = bridge.scan_recent_prefixes(
            bars_by_symbol, as_of, sessions=args.valid_sessions
        )
        signals = confirm_recent_scans(recent, as_of, sessions=args.valid_sessions)
        candidates = select_candidates(
            signals, bars_by_symbol, as_of, max_candidates=args.max_candidates,
            valid_sessions=args.valid_sessions,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"缠论本地扫描失败: {exc}", file=sys.stderr)
        return 2
    _print_json({
        "local_only": True,
        "as_of_date": as_of.isoformat(),
        "symbols": sorted(bars_by_symbol),
        "candidates": [candidate.as_dict() for candidate in candidates],
        "note": "仅使用本地原始日线和 Rust 缠论桥接；未调用 Agent，未生成订单。",
    })
    return 0


def command_chanlun_validate(args: argparse.Namespace) -> int:
    """Run the fixed local Chanlun acceptance check; this never calls Agents."""
    settings = _settings()
    try:
        market_store = _market_store(settings)
        all_symbols = bool(getattr(args, "all", False))
        symbols = (
            _active_chanlun_symbols(market_store)
            if all_symbols else _chanlun_symbols(args.symbols)
        )
        execution_bars = _local_chanlun_bars(
            market_store, symbols, limit=args.limit,
            start=date.fromisoformat(args.start) if args.start else None,
            end=date.fromisoformat(args.end) if args.end else None,
        )
        missing_symbols = set(symbols) - set(execution_bars)
        excluded_short_history = {
            symbol for symbol, bars in execution_bars.items() if len(bars) < 250
        } if all_symbols else set()
        if all_symbols:
            execution_bars = {
                symbol: bars for symbol, bars in execution_bars.items()
                if symbol not in excluded_short_history
            }
        if not execution_bars:
            raise ValueError("没有满足日线缠论最低 250 根历史要求的证券")
        known_by = date.fromisoformat(args.end) if args.end else max(
            bar.trade_date for bars in execution_bars.values() for bar in bars
        )
        signal_bars = _chanlun_signal_bars(market_store, execution_bars, known_by)
        bridge = _chanlun_bridge(settings)
        result = run_acceptance(
            signal_bars,
            bridge.scan,
            execution_bars_by_symbol=execution_bars,
            historical_scanner=bridge.replay_batches,
            price_limit_by_symbol=_chanlun_price_limits(
                market_store, set(execution_bars)
            ),
            stamp_duty_rate_by_symbol=_chanlun_stamp_duty_rates(
                market_store,
                set(execution_bars),
                float(settings.stock_sell_stamp_duty_rate),
            ),
            rules=ExecutionRules(
                commission_per_order=float(
                    settings.commission_fixed_per_order
                    if settings.commission_fixed_per_order is not None
                    else settings.commission_minimum
                ),
                stamp_duty_rate=float(settings.stock_sell_stamp_duty_rate),
            ),
        )
    except (RuntimeError, ValueError) as exc:
        print(f"缠论本地验收失败: {exc}", file=sys.stderr)
        return 2
    status = "passed" if result.passed else (
        "insufficient_sample" if not result.sufficient_sample else "failed"
    )
    open_positions_at_end = sum(
        bool(trade.get("marked_to_market")) for trade in result.trades
    )
    reference_criteria_met = bool(
        result.passed and all_symbols and not missing_symbols
    )
    receipt = {
        "schema_version": "chanlun-acceptance/v1",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        ),
        "algorithm": {
            "schema_version": "chanlun_bridge/v1",
            "crate": "chanlun_lean_lib",
            "feature": "guidao",
            "marker_filter": "tdx_dll mark 37: apply_sup_retreat_filter",
            "policy_version": POLICY_VERSION,
            "policy_sha256": _chanlun_policy_sha256(settings),
            "bridge_sha256": _file_sha256(
                settings.chanlun_bridge_path or Path()
            ),
        },
        "universe": {
            "kind": "active_security_master" if all_symbols else "explicit_symbols",
            "symbol_count": len(symbols),
            "symbol_signature": _symbol_signature(symbols),
            "eligible_symbol_count": len(execution_bars),
            "excluded_short_history_count": len(excluded_short_history),
            "missing_history_count": len(missing_symbols),
        },
        "period": {
            "start": min(
                bar.trade_date for bars in execution_bars.values() for bar in bars
            ).isoformat(),
            "end": max(
                bar.trade_date for bars in execution_bars.values() for bar in bars
            ).isoformat(),
        },
        "result": {
            "annualized_return": result.annualized_return,
            "maximum_drawdown": result.maximum_drawdown,
            "completed_trades": result.completed_trades,
            "open_positions_at_end": open_positions_at_end,
            "history_days": result.history_days,
            "sufficient_sample": result.sufficient_sample,
            "passed": result.passed,
            "diagnostics": result.diagnostics,
            "trade_sample": list(result.trades[:100]),
        },
        "criteria": {
            "annualized_return": "> 0",
            "maximum_drawdown": ">= -0.50",
            "minimum_history_days": 756,
            "minimum_completed_trades": 30,
        },
        "reference_only": True,
        "reference_criteria_met": reference_criteria_met,
    }
    receipt_path = _chanlun_acceptance_path(settings)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_json({
        "local_only": True,
        "status": status,
        "annualized_return": result.annualized_return,
        "maximum_drawdown": result.maximum_drawdown,
        "completed_trades": result.completed_trades,
        "open_positions_at_end": open_positions_at_end,
        "history_days": result.history_days,
        "sufficient_sample": result.sufficient_sample,
        "passed": result.passed,
        "diagnostics": result.diagnostics,
        "reference_only": True,
        "reference_criteria_met": reference_criteria_met,
        "acceptance_receipt": str(receipt_path),
        "universe": receipt["universe"],
        "criteria": {
            "annualized_return": "> 0",
            "maximum_drawdown": ">= -0.50",
            "minimum_history_days": 756,
            "minimum_completed_trades": 30,
        },
        "note": (
            "样本不足：至少需要 756 个交易日和 30 笔完成交易。"
            if not result.sufficient_sample else "仅使用本地原始日线和 Rust 缠论桥接；未调用 Agent，未生成订单。"
        ),
    })
    return 0


def command_snapshot_enrich(args: argparse.Namespace) -> int:
    settings = _settings()
    source = Path(args.snapshot).resolve()
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    current_account = _load_account(settings, None)
    if current_account is not None:
        snapshot["account"] = current_account
    market_store = _market_store(settings)
    market_store.apply_etf_attributes(snapshot)
    target = (
        Path(args.output).resolve()
        if args.output
        else settings.runtime_dir
        / "snapshots"
        / f"enriched-{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S')}.json"
    )
    market_store.upsert_security_master(
        snapshot["snapshot_id"],
        snapshot["securities"],
        snapshot["data_quality"]["available_at"],
    )
    enriched = BuiltSnapshot(_feature_engine(settings, market_store).enrich(snapshot))
    enriched.write(target)
    coverage = enriched.data["feature_coverage"]
    candidates = enriched.data["candidates"]
    daily_candidates = enriched.data.get("chanlun_daily_candidates", {})
    print(target)
    print(f"历史特征覆盖: {coverage['symbols_with_history']}/{coverage['eligible_securities']}")
    print(
        "候选数量: "
        f"HOLDINGS={len(candidates.get('HOLDINGS', []))}, "
        f"CHANLUN_SECOND_BUY={len(daily_candidates.get('CHANLUN_SECOND_BUY', []))}, "
        f"CHANLUN_THIRD_BUY={len(daily_candidates.get('CHANLUN_THIRD_BUY', []))}; "
        "缠论候选保持全量，随后由 Agent 最多选择 20 只进入研究"
    )
    return 0


def command_fundamentals_sync(args: argparse.Namespace) -> int:
    settings = _settings()
    periods = (
        [date.fromisoformat(value) for value in args.period]
        if args.period
        else recent_completed_periods(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    )
    result = FundamentalSyncService(
        AkshareFinancialReportProvider(), _market_store(settings)
    ).sync(periods)
    _print_json(result)
    return 0


def command_history_status(args: argparse.Namespace) -> int:
    print(json.dumps(_market_store(_settings()).status(), ensure_ascii=False, indent=2))
    return 0


def command_industry_sync(args: argparse.Namespace) -> int:
    settings = _settings()
    market_store = _market_store(settings)
    snapshot = json.loads(Path(args.snapshot).resolve().read_text(encoding="utf-8"))
    stocks = [
        item for item in snapshot.get("securities", []) if item.get("security_type") == "stock"
    ]
    market_store.seed_industry_targets(stocks)
    recovered = market_store.recover_interrupted_industry_syncs()
    requested = {
        normalize_symbol(item)
        for value in args.symbols
        for item in value.split(",")
        if item.strip()
    }
    available = {item["symbol"] for item in stocks}
    missing = requested - available
    if missing:
        print(f"股票主数据中找不到: {', '.join(sorted(missing))}", file=sys.stderr)
        return 2
    if requested:
        market_store.queue_industry_targets(requested)
    selected = market_store.pending_industry_targets(
        0 if requested else args.limit, args.max_attempts, requested or None
    )
    if recovered:
        print(f"已恢复 {recovered} 个中断的行业同步目标")
    if not selected:
        print("没有待同步的行业目标")
        return 0
    start_date = date.fromisoformat(args.start)
    end_date = (
        date.fromisoformat(args.end) if args.end else as_local_date(snapshot["as_of_time"])
    )
    service = IndustrySyncService(AkshareProvider(), market_store)
    completed: list[dict[str, object]] = []
    failed: list[dict[str, str]] = []
    for index, item in enumerate(selected, start=1):
        symbol = str(item["symbol"])
        print(f"[{index}/{len(selected)}] 同步行业 {symbol} {item['name']}")
        sync_id = market_store.begin_industry_sync(symbol)
        try:
            result = service.sync_symbol(symbol, start_date, end_date)
            market_store.finish_industry_sync(sync_id, symbol, int(result["memberships"]))
            completed.append(result)
        except Exception as exc:
            market_store.fail_industry_sync(sync_id, symbol, str(exc))
            failed.append({"symbol": symbol, "error": str(exc)})
            print(f"失败: {exc}", file=sys.stderr)
        if index < len(selected) and args.delay > 0:
            time_module.sleep(args.delay)
    print(json.dumps({"completed": completed, "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def command_etf_reference_sync(args: argparse.Namespace) -> int:
    settings = _settings()
    market_store = _market_store(settings)
    snapshot = json.loads(Path(args.snapshot).resolve().read_text(encoding="utf-8"))
    etfs = {
        str(item["symbol"]): str(item["exchange"])
        for item in snapshot.get("securities", [])
        if item.get("security_type") == "etf"
    }
    if not etfs:
        print("快照中没有 ETF", file=sys.stderr)
        return 2
    provider = ExchangeReferenceProvider()
    requested = {args.exchange} if args.exchange != "all" else {"sse", "szse"}
    fetched = []
    failures: list[dict[str, str]] = []
    for exchange in ("sse", "szse"):
        if exchange not in requested:
            continue
        try:
            rows = (
                provider.fetch_sse_etf_attributes()
                if exchange == "sse"
                else provider.fetch_szse_etf_list()
            )
            fetched.extend(
                item
                for item in rows
                if item.symbol in etfs and etfs[item.symbol].lower() == exchange
            )
        except Exception as exc:
            failures.append({"exchange": exchange.upper(), "error": str(exc)})
    stored = [
        StoredEtfTradeAttribute(
            symbol=item.symbol,
            exchange=item.exchange.value,
            same_day_turnaround=item.same_day_turnaround,
            price_limit_ratio=(
                str(item.price_limit_ratio) if item.price_limit_ratio is not None else None
            ),
            listing_date=item.listing_date,
            fund_category=item.fund_category,
            source_url=item.source_url,
            available_at=item.available_at,
            raw=item.raw,
        )
        for item in fetched
    ]
    market_store.upsert_etf_trade_attributes(stored)
    coverage = market_store.status()
    print(
        json.dumps(
            {
                "stored": len(stored),
                "snapshot_etfs": len(etfs),
                "same_day_turnaround_known": coverage["etf_same_day_turnaround_known"],
                "price_limit_known": coverage["etf_price_limit_known"],
                "failed": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 2


def _load_account(settings: Settings, value: str | None) -> dict[str, object] | None:
    path = Path(value).resolve() if value else settings.workspace / "config" / "account.json"
    if value is None:
        state = AccountLedger.reduce(_store(settings).list_account_events())
        if state is not None:
            return state
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def command_account_import(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    source = Path(args.file).resolve() if args.file else settings.workspace / "config" / "account.json"
    account = json.loads(source.read_text(encoding="utf-8"))
    event_id = args.event_id or f"account-init-{account['version']}"
    event_time = args.time or utc_now()
    if not store.account_event_exists(event_id):
        store.append_account_event(event_id, "account_initialized", event_time, account)
    print(json.dumps(AccountLedger.reduce(store.list_account_events()), ensure_ascii=False, indent=2))
    return 0


def command_account_show(args: argparse.Namespace) -> int:
    settings = _settings()
    state = AccountLedger.reduce(_store(settings).list_account_events())
    if state is None:
        state = _load_account(settings, args.file)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def command_account_cash(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    store.append_account_event(
        args.event_id or f"cash-{uuid.uuid4().hex}",
        "cash_reconciled",
        args.time or utc_now(),
        {"cash": args.cash, "source": args.source},
    )
    print(json.dumps(AccountLedger.reduce(store.list_account_events()), ensure_ascii=False, indent=2))
    return 0


def command_account_fill(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    payload = build_fill_payload(
        symbol=args.symbol,
        name=args.name or normalize_symbol(args.symbol),
        security_type=SecurityType(args.security_type),
        side=Side(args.side),
        quantity=args.quantity,
        price=Decimal(args.price),
        same_day_turnaround=args.same_day_turnaround,
    )
    event_id = args.event_id or f"fill-{uuid.uuid4().hex}"
    event_time = args.time or utc_now()
    state = record_confirmed_fill(
        store, account_event_id=event_id,
        state_event_id=f"state-{uuid.uuid4().hex}", event_time=event_time,
        payload=payload, order_id=args.order_id,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def command_account_settle(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    event_id = args.event_id or f"settlement-{uuid.uuid4().hex}"
    event_time = args.time or utc_now()
    payload = {
        "symbol": normalize_symbol(args.symbol),
        "available_quantity": args.available_quantity,
    }
    candidate = store.list_account_events() + [{
        "event_id": event_id, "event_type": "settlement",
        "event_time": event_time, "payload": payload,
    }]
    state = AccountLedger.reduce(candidate)
    store.append_account_event(event_id, "settlement", event_time, payload)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def command_account_position(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store(settings)
    event_id = args.event_id or f"position-{uuid.uuid4().hex}"
    event_time = args.time or utc_now()
    payload = {
        "symbol": normalize_symbol(args.symbol), "name": args.name,
        "security_type": args.security_type, "quantity": args.quantity,
        "available_quantity": args.available_quantity,
        "frozen_quantity": args.frozen_quantity, "cost_price": args.cost_price,
        "source": args.source,
    }
    candidate = store.list_account_events() + [{
        "event_id": event_id, "event_type": "position_reconciled",
        "event_time": event_time, "payload": payload,
    }]
    state = AccountLedger.reduce(candidate)
    store.append_account_event(event_id, "position_reconciled", event_time, payload)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def command_order_transition(args: argparse.Namespace) -> int:
    store = _store(_settings())
    current = store.current_order_state(args.order_id)
    if current is None:
        print(f"找不到订单: {args.order_id}", file=sys.stderr)
        return 2
    plan = store.get_order_plan_by_id(str(current["plan_id"]))
    if plan is None:
        print(f"找不到订单计划: {current['plan_id']}", file=sys.stderr)
        return 2
    try:
        validate_order_plan_account_version(
            plan, AccountLedger.reduce(store.list_account_events())
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    before, after = validate_transition(str(current["to_state"]), args.to)
    event_time = args.time or utc_now()
    store.append_order_state_event(
        args.event_id or f"state-{uuid.uuid4().hex}",
        args.order_id,
        str(current["plan_id"]),
        before.value,
        after.value,
        event_time,
        args.reason,
        {},
    )
    print(json.dumps(store.current_order_state(args.order_id), ensure_ascii=False, indent=2))
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    settings = _settings()
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date()
    store = _store(settings)
    result = EvaluationEngine(store, _market_store(settings)).evaluate_due(as_of)
    result["report"] = evaluation_report(store.evaluation_rows())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_evaluation_report(args: argparse.Namespace) -> int:
    store = _store(_settings())
    print(json.dumps(evaluation_report(store.evaluation_rows()), ensure_ascii=False, indent=2))
    return 0


def command_source_backtest(args: argparse.Namespace) -> int:
    settings = _settings()
    result = SourceBacktester(
        _market_store(settings),
        initial_cash=float(args.initial_cash),
        rebalance_sessions=args.rebalance_sessions,
        maximum_positions=args.maximum_positions,
        slippage_bps=args.slippage_bps,
        commission_fixed_per_order=(
            settings.commission_fixed_per_order
            if settings.commission_fixed_per_order is not None
            else settings.commission_minimum
        ),
        stock_sell_stamp_duty_rate=settings.stock_sell_stamp_duty_rate,
    ).run(date.fromisoformat(args.start), date.fromisoformat(args.end))
    directory = settings.runtime_dir / "backtests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"source-backtest-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["artifact"] = str(target)
    _print_json(result)
    return 0 if result["status"] == "completed" else 2


def command_backup_create(args: argparse.Namespace) -> int:
    settings = _settings()
    _store(settings)
    _market_store(settings)
    target = BackupService(settings).create()
    result = BackupService.verify_and_record(target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


def command_backup_verify(args: argparse.Namespace) -> int:
    result = BackupService.verify_and_record(Path(args.directory).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


def command_backup_restore_test(args: argparse.Namespace) -> int:
    result = BackupService.restore(
        Path(args.directory).resolve(), Path(args.destination).resolve()
    )
    summary = {
        "version": result["version"],
        "source_backup": result["source_backup"],
        "destination": result["destination"],
        "restored_at": result["restored_at"],
        "valid": result["valid"],
        "file_count": len(result["files"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def command_shadow_settle(args: argparse.Namespace) -> int:
    if args.model != MODEL_VERSION:
        raise ValueError("未知影子成交模型")
    settings = _settings()
    store = _store(settings)
    plan = store.get_order_plan(args.run_id)
    if plan is None:
        print(f"找不到运行的订单计划: {args.run_id}", file=sys.stderr)
        return 2
    through = date.fromisoformat(args.through) if args.through else datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date()
    result = ShadowExecutionEngine(store, _market_store(settings)).settle(plan, through)
    result["model_version"] = MODEL_VERSION
    result["fills"] = store.shadow_fills(args.run_id)
    result["settlement"] = store.shadow_settlement(args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_acceptance_status(args: argparse.Namespace) -> int:
    settings = _settings()
    report = AcceptanceAuditor(
        settings, _store(settings), _market_store(settings)
    ).report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["manual_real_funds_trial_ready"] else 2


def command_task_run(args: argparse.Namespace) -> int:
    settings = _settings()
    result = TaskController(
        settings, _store(settings), _market_store(settings)
    ).run_sync(args.action)
    _print_json(result)
    return 0 if result["status"] == "completed" else 2


def command_daily_workflow(args: argparse.Namespace) -> int:
    settings = _settings()
    result = DailyWorkflow(
        settings, _store(settings), _market_store(settings)
    ).run(
        args.mode,
        date.fromisoformat(args.through) if args.through else None,
    )
    _print_json(result)
    return 0 if result["status"] == "completed" else 2


def command_schedule_preview(args: argparse.Namespace) -> int:
    settings = _settings()
    configured = load_configured_schedule(settings)
    plan = configured or SchedulePlan.recommended()
    print(json.dumps(WindowsTaskScheduler(settings).preview(plan), ensure_ascii=False, indent=2))
    return 0


def command_schedule_install(args: argparse.Namespace) -> int:
    settings = _settings()
    plan = load_configured_schedule(settings)
    if plan is None:
        raise ValueError("system.json 尚未配置 schedule")
    result = WindowsTaskScheduler(settings).install(plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_schedule_status(args: argparse.Namespace) -> int:
    result = WindowsTaskScheduler(_settings()).status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["installed"] else 2


def command_schedule_remove(args: argparse.Namespace) -> int:
    result = WindowsTaskScheduler(_settings()).remove()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def as_local_date(value: str) -> date:
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai")).date()


def command_rules(args: argparse.Namespace) -> int:
    versions = [
        {
            "version": item.version,
            "effective_from": item.effective_from.isoformat(),
            "effective_to": item.effective_to.isoformat() if item.effective_to else None,
            "risk_warning_price_limit": str(item.risk_warning_price_limit),
        }
        for item in RuleBook.versions
    ]
    print(json.dumps(versions, ensure_ascii=False, indent=2))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    settings = _settings()
    checks: dict[str, object] = {
        "python": sys.version.split()[0],
        "workspace": str(settings.workspace),
        "real_trading_enabled": settings.real_trading_enabled,
        "codex_command": settings.codex_command,
        "codex_found": shutil.which(settings.codex_command) is not None,
        "commission_minimum": str(settings.commission_minimum),
        "commission_rate": (
            str(settings.commission_rate) if settings.commission_rate is not None else None
        ),
        "commission_fixed_per_order": (
            str(settings.commission_fixed_per_order)
            if settings.commission_fixed_per_order is not None
            else None
        ),
        "stock_sell_stamp_duty_rate": str(settings.stock_sell_stamp_duty_rate),
        "risk_warning_permission": settings.risk_warning_permission,
        "exclude_risk_warning_securities": settings.exclude_risk_warning_securities,
        "simulation_all_or_none": settings.simulation_all_or_none,
        "simulation_buy_at_limit_up": settings.simulation_buy_at_limit_up,
        "simulation_sell_at_limit_down": settings.simulation_sell_at_limit_down,
    }
    if checks["codex_found"]:
        try:
            result = subprocess.run(
                [settings.codex_command, "--version"],
                cwd=settings.workspace,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            checks["codex_runnable"] = result.returncode == 0
            checks["codex_version"] = (result.stdout or result.stderr).strip()
            logged_in, login_detail = codex_chatgpt_login_status(
                settings.codex_command, settings.workspace
            )
            checks["codex_chatgpt_logged_in"] = logged_in
            checks["codex_login_status"] = login_detail
            reachable, network_detail = codex_network_status()
            checks["codex_network_reachable"] = reachable
            checks["codex_network_status"] = network_detail
        except (OSError, subprocess.SubprocessError) as exc:
            checks["codex_runnable"] = False
            checks["codex_error"] = str(exc)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if (
        checks.get("codex_runnable", False)
        and checks.get("codex_chatgpt_logged_in", False)
        and checks.get("codex_network_reachable", False)
    ) else 2


def command_serve(args: argparse.Namespace) -> int:
    settings = _settings()
    serve(_store(settings), _market_store(settings), settings, args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-quant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize local config and database")
    init_parser.set_defaults(func=command_init)

    agents_parser = subparsers.add_parser("agents", help="List configured agents")
    agents_parser.set_defaults(func=command_agents)

    plan_parser = subparsers.add_parser("plan", help="Create a dry-run pipeline manifest")
    plan_parser.add_argument("--snapshot", required=True)
    plan_parser.add_argument("--monthly", action="store_true")
    plan_parser.set_defaults(func=command_plan)

    run_parser = subparsers.add_parser("run", help="Run the fixed agent pipeline")
    run_parser.add_argument("--snapshot", required=True)
    run_parser.add_argument("--runner", choices=("fake", "codex"), default="fake")
    run_parser.add_argument("--monthly", action="store_true")
    run_parser.add_argument(
        "--current-account", action="store_true",
        help="Rebind the latest account ledger and recompute candidates before Agents run",
    )
    run_parser.set_defaults(func=command_run)

    runs_parser = subparsers.add_parser("runs", help="List recent runs")
    runs_parser.add_argument("--limit", type=int, default=20)
    runs_parser.set_defaults(func=command_runs)

    snapshot_parser = subparsers.add_parser("snapshot", help="Build a fail-closed market snapshot")
    snapshot_parser.add_argument("--output")
    snapshot_parser.add_argument("--account", help="Account JSON; defaults to config/account.json")
    snapshot_parser.add_argument(
        "--without-features", action="store_true",
        help="Persist the raw market snapshot without running local feature enrichment",
    )
    snapshot_parser.set_defaults(func=command_snapshot)

    snapshot_enrich_parser = subparsers.add_parser(
        "snapshot-enrich", help="Recompute local features and candidates without refetching quotes"
    )
    snapshot_enrich_parser.add_argument("--snapshot", required=True)
    snapshot_enrich_parser.add_argument("--output")
    snapshot_enrich_parser.set_defaults(func=command_snapshot_enrich)

    fundamentals_parser = subparsers.add_parser(
        "fundamentals-sync",
        help="Sync point-in-time bulk financial reports for quality/value research",
    )
    fundamentals_parser.add_argument(
        "--period", action="append", default=[], help="YYYY-MM-DD quarter end; repeatable"
    )
    fundamentals_parser.set_defaults(func=command_fundamentals_sync)

    history_sync_parser = subparsers.add_parser(
        "history-sync", help="Incrementally sync raw history, qfq display bars, and dividends"
    )
    history_sync_parser.add_argument("--snapshot", required=True)
    history_sync_parser.add_argument("--symbols", action="append", default=[])
    history_sync_parser.add_argument("--start", help="YYYY-MM-DD; defaults to two years ago")
    history_sync_parser.add_argument("--end", help="YYYY-MM-DD; defaults to snapshot date")
    history_sync_parser.add_argument("--limit", type=int, default=10, help="0 means all")
    history_sync_parser.add_argument("--delay", type=float, default=None,
                                     help="Seconds between symbols per worker; defaults to history_request_delay_seconds")
    history_sync_parser.add_argument(
        "--workers", type=int, choices=(1, 2, 3, 4), default=None,
        help="Separate history processes; defaults to history_workers (independent of Agent concurrency)",
    )
    history_sync_parser.add_argument("--max-attempts", type=int, default=3)
    history_sync_parser.add_argument(
        "--stale-after-minutes", type=int, default=60,
        help="Requeue running targets only after this age; use 0 after confirming no sync is active",
    )
    history_sync_parser.add_argument("--raw-only", action="store_true")
    history_sync_parser.add_argument("--no-dividends", action="store_true")
    history_sync_parser.add_argument(
        "--incremental-window", action="store_true",
        help="Sync exactly the requested --start/--end gap without reopening older target boundaries",
    )
    history_sync_parser.add_argument(
        "--refresh-existing", action="store_true", help="Incrementally update existing symbols"
    )
    history_sync_parser.set_defaults(func=command_history_sync)

    history_show_parser = subparsers.add_parser("history-show", help="Show locally stored bars")
    history_show_parser.add_argument("--symbol", required=True)
    history_show_parser.add_argument("--adjustment", choices=("raw", "qfq"), default="qfq")
    history_show_parser.add_argument("--limit", type=int, default=20)
    history_show_parser.set_defaults(func=command_history_show)

    chanlun_scan_parser = subparsers.add_parser(
        "chanlun-scan", help="Scan locally stored daily bars; never starts Agents or orders"
    )
    chanlun_scan_parser.add_argument("--symbols", action="append", required=True,
                                    help="Comma-separated stock or ETF symbols; repeatable")
    chanlun_scan_parser.add_argument("--as-of", help="YYYY-MM-DD; defaults to latest local bar")
    chanlun_scan_parser.add_argument("--limit", type=int, default=2500)
    chanlun_scan_parser.add_argument("--valid-sessions", type=int, default=20)
    chanlun_scan_parser.add_argument("--max-candidates", type=int, default=5000)
    chanlun_scan_parser.set_defaults(func=command_chanlun_scan)

    chanlun_validate_parser = subparsers.add_parser(
        "chanlun-validate", help="Validate Chanlun locally; never starts Agents or orders"
    )
    chanlun_validate_scope = chanlun_validate_parser.add_mutually_exclusive_group(
        required=True
    )
    chanlun_validate_scope.add_argument(
        "--symbols", action="append",
        help="Comma-separated stock or ETF symbols; repeatable",
    )
    chanlun_validate_scope.add_argument(
        "--all", action="store_true",
        help="Use the complete active security master for a reference validation",
    )
    chanlun_validate_parser.add_argument("--start", help="YYYY-MM-DD")
    chanlun_validate_parser.add_argument("--end", help="YYYY-MM-DD")
    chanlun_validate_parser.add_argument("--limit", type=int, default=2500)
    chanlun_validate_parser.set_defaults(func=command_chanlun_validate)

    history_status_parser = subparsers.add_parser(
        "history-status", help="Show local historical warehouse coverage"
    )
    history_status_parser.set_defaults(func=command_history_status)

    industry_sync_parser = subparsers.add_parser(
        "industry-sync", help="Sync point-in-time stock industry memberships from CNInfo"
    )
    industry_sync_parser.add_argument("--snapshot", required=True)
    industry_sync_parser.add_argument("--symbols", action="append", default=[])
    industry_sync_parser.add_argument("--start", default="1990-01-01")
    industry_sync_parser.add_argument("--end")
    industry_sync_parser.add_argument("--limit", type=int, default=10)
    industry_sync_parser.add_argument("--delay", type=float, default=0.5)
    industry_sync_parser.add_argument("--max-attempts", type=int, default=3)
    industry_sync_parser.set_defaults(func=command_industry_sync)

    etf_reference_parser = subparsers.add_parser(
        "etf-reference-sync", help="Sync official exchange ETF trading attributes"
    )
    etf_reference_parser.add_argument("--snapshot", required=True)
    etf_reference_parser.add_argument(
        "--exchange", choices=("all", "sse", "szse"), default="all"
    )
    etf_reference_parser.set_defaults(func=command_etf_reference_sync)

    account_import_parser = subparsers.add_parser(
        "account-import", help="Initialize the append-only account ledger from account JSON"
    )
    account_import_parser.add_argument("--file")
    account_import_parser.add_argument("--event-id")
    account_import_parser.add_argument("--time")
    account_import_parser.set_defaults(func=command_account_import)

    account_show_parser = subparsers.add_parser("account-show", help="Show reduced account state")
    account_show_parser.add_argument("--file")
    account_show_parser.set_defaults(func=command_account_show)

    account_cash_parser = subparsers.add_parser(
        "account-cash", help="Append a broker cash reconciliation"
    )
    account_cash_parser.add_argument("--cash", type=float, required=True)
    account_cash_parser.add_argument("--source", default="manual_broker_reconciliation")
    account_cash_parser.add_argument("--event-id")
    account_cash_parser.add_argument("--time")
    account_cash_parser.set_defaults(func=command_account_cash)

    account_fill_parser = subparsers.add_parser(
        "account-fill", help="Append a manually confirmed broker fill"
    )
    account_fill_parser.add_argument("--symbol", required=True)
    account_fill_parser.add_argument("--name")
    account_fill_parser.add_argument("--security-type", choices=("stock", "etf"), required=True)
    account_fill_parser.add_argument("--side", choices=("buy", "sell"), required=True)
    account_fill_parser.add_argument("--quantity", type=int, required=True)
    account_fill_parser.add_argument("--price", required=True)
    account_fill_parser.add_argument("--same-day-turnaround", action="store_true")
    account_fill_parser.add_argument("--order-id")
    account_fill_parser.add_argument("--event-id")
    account_fill_parser.add_argument("--time")
    account_fill_parser.set_defaults(func=command_account_fill)

    account_settle_parser = subparsers.add_parser(
        "account-settle", help="Append a T+1/T+0 available-quantity settlement"
    )
    account_settle_parser.add_argument("--symbol", required=True)
    account_settle_parser.add_argument("--available-quantity", type=int, required=True)
    account_settle_parser.add_argument("--event-id")
    account_settle_parser.add_argument("--time")
    account_settle_parser.set_defaults(func=command_account_settle)

    account_position_parser = subparsers.add_parser(
        "account-position", help="Append a broker position reconciliation"
    )
    account_position_parser.add_argument("--symbol", required=True)
    account_position_parser.add_argument("--name", required=True)
    account_position_parser.add_argument("--security-type", choices=("stock", "etf"), required=True)
    account_position_parser.add_argument("--quantity", type=int, required=True)
    account_position_parser.add_argument("--available-quantity", type=int, required=True)
    account_position_parser.add_argument("--frozen-quantity", type=int, default=0)
    account_position_parser.add_argument("--cost-price", type=float, required=True)
    account_position_parser.add_argument("--source", default="manual_broker_reconciliation")
    account_position_parser.add_argument("--event-id")
    account_position_parser.add_argument("--time")
    account_position_parser.set_defaults(func=command_account_position)

    order_transition_parser = subparsers.add_parser(
        "order-transition", help="Append a validated manual order-state transition"
    )
    order_transition_parser.add_argument("--order-id", required=True)
    order_transition_parser.add_argument(
        "--to", choices=tuple(item.value for item in OrderState), required=True
    )
    order_transition_parser.add_argument("--reason", required=True)
    order_transition_parser.add_argument("--event-id")
    order_transition_parser.add_argument("--time")
    order_transition_parser.set_defaults(func=command_order_transition)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="Settle due prediction outcomes from point-in-time local bars"
    )
    evaluate_parser.add_argument("--as-of", help="YYYY-MM-DD; defaults to today")
    evaluate_parser.set_defaults(func=command_evaluate)

    evaluation_report_parser = subparsers.add_parser(
        "evaluation-report", help="Show prediction calibration and attribution summary"
    )
    evaluation_report_parser.set_defaults(func=command_evaluation_report)

    source_backtest_parser = subparsers.add_parser(
        "source-backtest",
        help="Compare diversified candidate sources with point-in-time data and trading friction",
    )
    source_backtest_parser.add_argument("--start", required=True)
    source_backtest_parser.add_argument("--end", required=True)
    source_backtest_parser.add_argument("--initial-cash", type=float, default=50000)
    source_backtest_parser.add_argument("--rebalance-sessions", type=int, default=20)
    source_backtest_parser.add_argument("--maximum-positions", type=int, default=5)
    source_backtest_parser.add_argument("--slippage-bps", type=float, default=5)
    source_backtest_parser.set_defaults(func=command_source_backtest)

    backup_create_parser = subparsers.add_parser(
        "backup-create", help="Create and verify a consistent local database backup"
    )
    backup_create_parser.set_defaults(func=command_backup_create)

    backup_verify_parser = subparsers.add_parser(
        "backup-verify", help="Verify backup hashes and SQLite integrity"
    )
    backup_verify_parser.add_argument("--directory", required=True)
    backup_verify_parser.set_defaults(func=command_backup_verify)

    backup_restore_parser = subparsers.add_parser(
        "backup-restore-test",
        help="Restore a verified backup into a new isolated directory",
    )
    backup_restore_parser.add_argument("--directory", required=True)
    backup_restore_parser.add_argument("--destination", required=True)
    backup_restore_parser.set_defaults(func=command_backup_restore_test)

    shadow_parser = subparsers.add_parser(
        "shadow-settle", help="Settle an order plan with the provisional next-open model"
    )
    shadow_parser.add_argument("--run-id", required=True)
    shadow_parser.add_argument("--through", help="YYYY-MM-DD; defaults to today")
    shadow_parser.add_argument("--model", default=MODEL_VERSION, choices=(MODEL_VERSION,))
    shadow_parser.set_defaults(func=command_shadow_settle)

    acceptance_parser = subparsers.add_parser(
        "acceptance-status", help="Audit research and manual real-funds trial readiness"
    )
    acceptance_parser.set_defaults(func=command_acceptance_status)

    task_run_parser = subparsers.add_parser(
        "task-run", help="Run one audited dashboard task synchronously"
    )
    task_run_parser.add_argument(
        "--action",
        choices=(
            "snapshot", "snapshot_raw", "history", "fundamentals", "enrich", "analyze",
            "full_workflow",
            "evaluate", "backup",
            "module_value_scan", "module_value_analyze", "module_closing_scan",
            "module_closing_analyze", "module_closing_refresh", "module_etf_scan", "module_etf_analyze",
            "module_value_experiment", "module_closing_experiment", "module_etf_experiment",
        ),
        required=True,
    )
    task_run_parser.set_defaults(func=command_task_run)

    module_parser = subparsers.add_parser("module-run", help="Run one independent manual investment module")
    module_parser.add_argument("--module", choices=("value", "closing", "etf"), required=True)
    module_parser.add_argument("--action", choices=("scan", "analyze", "refresh", "experiment"), required=True)
    module_parser.set_defaults(func=command_module_run)

    workflow_parser = subparsers.add_parser(
        "daily-workflow", help="Run an audited refresh or post-market workflow"
    )
    workflow_parser.add_argument(
        "--mode", choices=("refresh", "analyze", "close"), required=True
    )
    workflow_parser.add_argument("--through", help="YYYY-MM-DD; defaults to today")
    workflow_parser.set_defaults(func=command_daily_workflow)

    schedule_preview_parser = subparsers.add_parser(
        "schedule-preview", help="Preview configured or recommended Windows schedule"
    )
    schedule_preview_parser.set_defaults(func=command_schedule_preview)

    schedule_install_parser = subparsers.add_parser(
        "schedule-install", help="Install the user-confirmed Windows schedule"
    )
    schedule_install_parser.set_defaults(func=command_schedule_install)

    schedule_status_parser = subparsers.add_parser(
        "schedule-status", help="Query installed Windows schedule tasks"
    )
    schedule_status_parser.set_defaults(func=command_schedule_status)

    schedule_remove_parser = subparsers.add_parser(
        "schedule-remove", help="Remove only tasks from this system's install manifest"
    )
    schedule_remove_parser.set_defaults(func=command_schedule_remove)

    rules_parser = subparsers.add_parser("rules", help="List versioned exchange rules")
    rules_parser.set_defaults(func=command_rules)

    doctor_parser = subparsers.add_parser("doctor", help="Check local runtime dependencies")
    doctor_parser.set_defaults(func=command_doctor)

    serve_parser = subparsers.add_parser("serve", help="Serve the local audit dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.set_defaults(func=command_serve)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
