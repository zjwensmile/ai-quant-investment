"""Manual module persistence and isolated ETF account reconciliation."""
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from .account import AccountLedger

MODULES = {
    "value": {"id": "value", "title": "优质成长与价值研究", "subtitle": "财报估值与安全边际 · 核验周期位置及现金质量", "budget": 50000, "target_exposure": None, "account_mode": "shared_stock"},
    "closing": {"id": "closing", "title": "尾盘与隔夜", "subtitle": "独立10万元账户 · 尾盘复核 · 次日分情景退出", "budget": 100000, "target_exposure": .10, "account_mode": "independent_stock"},
    "etf": {"id": "etf", "title": "ETF动态仓位账户", "subtitle": "高景气行业主动配置 · 按允许市场配置核心仓 · 动态定仓 · 不买债券或货币类", "budget": 100000, "target_exposure": None, "account_mode": "planning"},
}


def now_iso():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


class ModuleStore:
    def __init__(self, runtime_dir):
        self.directory = runtime_dir / "modules"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "modules.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY,module TEXT,kind TEXT,created_at TEXT,payload TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS module_kind ON records(module,kind,created_at)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, module, kind, payload, record_id=None):
        if module not in MODULES:
            raise ValueError("未知投资模块")
        record_id = record_id or f"{module}-{kind}-{uuid.uuid4().hex[:12]}"
        with self.connect() as db:
            db.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (record_id, module, kind, now_iso(), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return record_id

    def latest(self, module, kind):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM records WHERE module=? AND kind=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (module, kind)).fetchone()
        return json.loads(row[0]) if row else None

    def save_quote_reference(self, module, snapshot):
        """Display-only captured quotes; never update frozen research or execution state."""
        sources = snapshot.get("data_quality", {}).get("source_details", {})
        securities = {s["symbol"]: s for s in snapshot.get("securities", [])}
        quotes = {symbol: {"last": q.get("last"), "available_at": q.get("available_at"),
                           **{key: q[key] for key in ("source_quote_at", "source_trade_date", "quote_time_status") if key in q},
                           "source": q.get("source") or sources.get("etf_spot" if securities.get(symbol, {}).get("security_type") == "etf" else "stock_spot", "未标注")}
                  for symbol, q in snapshot.get("quotes", {}).items()}
        self.save(module, "quote_reference", {"quotes": quotes, "observed_at": snapshot["as_of_time"],
                                             "snapshot_id": snapshot.get("snapshot_id")})

    def price_comparison(self, analysis):
        observed_at = now_iso()
        clock = datetime.fromisoformat(observed_at)
        with self.connect() as db:
            row = db.execute("SELECT payload FROM records WHERE id=? AND kind='scan'", (analysis.get("scan_id"),)).fetchone()
        original = json.loads(row[0]) if row else {}
        candidates = {r["symbol"]: r for r in original.get("candidates", [])}
        references = [(self.latest(module, "quote_reference") or {}).get("quotes", {}) for module in MODULES]
        rows = []
        decision = analysis.get("decision") or {}
        plans = {p["symbol"]: p for p in decision.get("valuations", [])}
        plans.update({p["symbol"]: p for p in decision.get("positions", [])})
        for p in plans.values():
            candidate = candidates.get(p["symbol"], {})
            research_quote = candidate.get("quote", {})
            research_price = research_quote.get("last", candidate.get("price"))
            best, best_rank = {}, None
            for quotes in references:
                q = quotes.get(p["symbol"], {})
                try:
                    stamp = datetime.fromisoformat(q.get("available_at", ""))
                    value = q.get("last")
                    valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                    source_at = datetime.fromisoformat(q["source_quote_at"]) if q.get("source_quote_at") else None
                    if source_at and (not source_at.tzinfo or source_at > clock):
                        continue
                    rank = (source_at is not None, source_at or stamp, stamp)
                    if valid and stamp.tzinfo and stamp <= clock and (best_rank is None or rank > best_rank):
                        best, best_rank = q, rank
                except (ValueError, TypeError):
                    continue
            reference = best.get("last")
            lower, upper = p.get("entry_min"), p.get("entry_max")
            relation = "unknown" if reference is None or lower is None or upper is None else "above_entry" if reference > upper else "below_entry" if reference < lower else "within_entry"
            dated = any(key in best for key in ("quote_time_status", "source_quote_at", "source_trade_date"))
            reference_at = (best.get("source_quote_at") or best.get("source_trade_date")) if dated else best.get("available_at")
            stale = True
            try:
                stamp = datetime.fromisoformat(reference_at or "")
                stale = not stamp.tzinfo or not 0 <= (clock - stamp).total_seconds() <= 300
            except (ValueError, TypeError):
                pass
            rows.append({"symbol": p["symbol"], "research_price": research_price,
                         "research_at": (research_quote.get("source_quote_at") or research_quote.get("source_trade_date")) if any(key in research_quote for key in ("quote_time_status", "source_quote_at", "source_trade_date")) else research_quote.get("available_at", analysis.get("as_of_time")),
                         "reference_price": reference, "reference_at": reference_at,
                         "reference_fetched_at": best.get("available_at"),
                         "reference_source": best.get("source"),
                         "reference_stale": stale,
                         "entry_min": lower, "entry_max": upper,
                         "gap_to_entry_max": upper / reference - 1 if reference and upper is not None else None,
                         "relation": relation})
        return {"rows": rows, "observed_at": observed_at}

    def account(self, module, audit_store):
        if module == "closing":
            return self.latest("closing", "account") or {
                "budget": 100000, "capital_budget": 100000, "planned_capital_cny": 100000,
                "cash": None, "cash_confirmed": False, "positions_confirmed": False,
                "positions": [], "mode": "independent_stock", "version": "closing-planning-v1",
                "shared_modules": []}
        if module == "value":
            state = AccountLedger.reduce(audit_store.list_account_events()) or {}
            cash = state.get("cash")
            confirmed = not isinstance(cash, bool) and isinstance(cash, (int, float)) and math.isfinite(cash) and cash >= 0 and state.get("cash_status") == "confirmed"
            return {**state, "budget": state.get("capital_budget", MODULES["value"]["budget"]), "cash_confirmed": confirmed, "mode": "shared_stock",
                    "shared_modules": ["value"]}
        return self.latest("etf", "account") or {
            "budget": 100000, "planned_capital_cny": 100000, "cash": None,
            "cash_available_cny": None, "cash_confirmed": False, "funding_confirmed": False,
            "positions_confirmed": False, "positions": [], "mode": "planning", "version": "etf-planning-v1"}

    def reconcile_etf(self, body, market_store):
        if body.get("confirmed") is not True:
            raise ValueError("请明确确认券商现金与持仓事实")
        if isinstance(body["cash"], bool):
            raise ValueError("现金必须是金额，不能是布尔值")
        cash = float(body["cash"])
        if not math.isfinite(cash) or cash < 0:
            raise ValueError("现金必须是有限非负金额")
        known = {r["symbol"]: r for r in market_store.security_master_rows(active_only=True)}
        positions, seen = [], set()
        for row in body.get("positions", []):
            symbol = str(row["symbol"])
            if symbol in seen or known.get(symbol, {}).get("security_type") != "etf":
                raise ValueError(f"{symbol}: 需为不重复的沪深上市ETF证券代码")
            seen.add(symbol)
            quantity, available = row["quantity"], row["available_quantity"]
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (quantity, available)) or not 0 <= available <= quantity:
                raise ValueError("持仓数量必须为整数，且可卖数量不得超过持仓")
            if isinstance(row["cost_price"], bool):
                raise ValueError("持仓成本必须是价格，不能是布尔值")
            cost = float(row["cost_price"])
            if not math.isfinite(cost) or cost < 0 or (quantity and cost == 0):
                raise ValueError("非空持仓成本必须为正数")
            positions.append({"symbol": symbol, "name": known[symbol]["name"], "quantity": quantity,
                              "available_quantity": available, "cost_price": cost, "security_type": "etf"})
        state = {"budget": 100000, "planned_capital_cny": 100000, "cash": cash,
                 "cash_available_cny": cash, "cash_confirmed": True, "funding_confirmed": True,
                 "positions_confirmed": True, "positions": positions, "mode": "confirmed",
                 "version": f"etf-{uuid.uuid4().hex}", "confirmed_at": now_iso()}
        self.save("etf", "account", state)
        return state

    def view(self, module, audit_store):
        if module not in MODULES:
            raise ValueError("未知投资模块")
        tasks = [t for t in audit_store.list_system_tasks() if str(t.get("action", "")).startswith(f"module_{module}_")]
        task = tasks[0] if tasks else None
        analysis = self.latest(module, "analysis")
        scan = self.latest(module, "scan")
        research_policy = self.latest(module, "policy") or {}
        account = self.account(module, audit_store)
        if analysis and analysis.get("account_version") != account.get("version"):
            analysis["execution"] = {"status": "account_changed", "blockers": ["账户已更新，请重新讨论或复核，旧方案不能直接执行"]}
        if analysis:
            analysis["price_comparison"] = self.price_comparison(analysis)
            execution = analysis.get("execution") or {}
            blockers = list(execution.get("blockers", []))
            if module == "etf":
                from .etf_strategy import etf_buy_excluded
                if any(research_policy.get(key) is not None and research_policy[key] != (scan or {}).get("policy", {}).get(key)
                       for key in ("sector_allocation_objective", "sector_target_weight", "excluded_equity_regions")):
                    blockers.append("ETF行业配置目标或市场范围已更新，请先重新扫描再讨论；旧方案未按新目标重审")
                if any(etf_buy_excluded(p) for p in (analysis.get("decision") or {}).get("positions", [])):
                    blockers.append("ETF投资范围已更新：旧方案含债券或货币类禁品，需重新扫描和讨论")
            if scan and analysis.get("scan_id") != scan.get("scan_id"):
                blockers.append("候选扫描已更新，请重新讨论；旧方案不能作为新扫描的执行结论")
            expires = [p["expires_at"] for p in (analysis.get("decision") or {}).get("positions", [])]
            if execution.get("quote_valid_until"):
                expires.append(execution["quote_valid_until"])
            if any(datetime.fromisoformat(value) <= datetime.fromisoformat(now_iso()) for value in expires):
                blockers.append("方案或执行报价已过期，需手动重新讨论或刷新复核")
            if blockers != execution.get("blockers", []):
                analysis["execution"] = {**execution, "status": "conditional", "blockers": blockers}
        from .module_library import library_summary
        return {"module": {**MODULES[module], "budget": account.get("budget", MODULES[module]["budget"]), "account_mode": account["mode"]}, "account": account,
                "scan": scan, "analysis": analysis, "task": task, "research_policy": research_policy,
                "refresh": self.latest(module, "refresh"), "experiment": self.latest(module, "experiment"),
                "library": library_summary(self, module, now_iso())}

    def overview(self, audit_store):
        result = []
        for module in MODULES:
            view = self.view(module, audit_store)
            result.append({**view["module"], "latest_scan_at": (view["scan"] or {}).get("as_of_time"),
                           "latest_run_at": (view["analysis"] or {}).get("as_of_time"),
                           "status": (view["task"] or {}).get("status", "idle")})
        return {"modules": result, "account": self.account("value", audit_store)}
