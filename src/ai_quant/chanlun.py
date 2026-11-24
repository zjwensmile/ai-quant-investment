"""Deterministic Chanlun scanning, candidate selection, and acceptance helpers.

The Rust bridge deliberately reports only a marker's anchor bar.  A marker is
*confirmed* on the first end-of-day prefix where the bridge returns it.  This
keeps the boundary between the Rust algorithm and point-in-time policy explicit.
"""

from __future__ import annotations

import json
import math
import subprocess
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


class SignalKind(StrEnum):
    SECOND_BUY = "second_buy"
    SECOND_SELL = "second_sell"
    THIRD_BUY = "third_buy"
    THIRD_SELL = "third_sell"


BUY_KINDS = frozenset({SignalKind.SECOND_BUY, SignalKind.THIRD_BUY})
SELL_KINDS = frozenset({SignalKind.SECOND_SELL, SignalKind.THIRD_SELL})
POLICY_VERSION = "chanlun-daily-confirmed-v2-window20"


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    amount: float | None = None


@dataclass(frozen=True)
class ChanlunSignal:
    symbol: str
    kind: SignalKind
    anchor_date: date
    confirmed_date: date | None = None
    first_visible_date: date | None = None
    price: float | None = None
    chanlun: dict[str, object] = field(default_factory=dict)

    @property
    def signal_id(self) -> str:
        return f"{self.symbol}-1d-{self.kind.value}-{self.anchor_date.isoformat()}"

    def confirmed(self, visible_date: date) -> "ChanlunSignal":
        return replace(self, confirmed_date=visible_date, first_visible_date=visible_date)


@dataclass(frozen=True)
class ChanlunCandidate:
    symbol: str
    signal_id: str
    candidate_source: str
    signal: SignalKind
    anchor_date: date
    confirmed_date: date
    first_visible_date: date
    liquidity: float
    status: str = "active"
    chanlun: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "signal_id": self.signal_id,
            "candidate_source": self.candidate_source,
            "signal": self.signal.value,
            "anchor_date": self.anchor_date.isoformat(),
            "confirmed_date": self.confirmed_date.isoformat(),
            "first_visible_date": self.first_visible_date.isoformat(),
            "liquidity": self.liquidity,
            "status": self.status,
            "chanlun": self.chanlun,
        }


class RustChanlunBridge:
    """One subprocess invocation scans a batch of daily-bar requests.

    The v1 executable receives compact per-request ``{"items": [...]}`` arrays on stdin and
    returns ``{"schema_version": "chanlun_bridge/v1", "results": [...]}``.
    """

    def __init__(self, command: Sequence[str | Path], *, cwd: Path | None = None,
                 timeout_seconds: int = 120) -> None:
        self.command = [str(part) for part in command]
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def scan(self, bars_by_symbol: Mapping[str, Sequence[MarketBar]]) -> dict[str, list[ChanlunSignal]]:
        requests = [ChanlunScanRequest(symbol, symbol, tuple(bars)) for symbol, bars in sorted(bars_by_symbol.items()) if bars]
        if not requests:
            return {}
        scanned = self.scan_requests(requests)
        return {request.symbol: scanned[request.request_id] for request in requests}

    def scan_requests(self, requests: Sequence["ChanlunScanRequest"]) -> dict[str, list[ChanlunSignal]]:
        """Scan independently-addressable histories in one bridge process.

        ``request_id`` allows the caller to send multiple trailing prefixes for
        one symbol and unambiguously match the returned marker sets.
        """
        return self._scan_requests(requests, replay=False)

    def _scan_requests(
        self,
        requests: Sequence["ChanlunScanRequest"],
        *,
        replay: bool,
        recent_sessions: int | None = None,
    ) -> dict[str, list[ChanlunSignal]]:
        if recent_sessions is not None and recent_sessions < 1:
            raise ValueError("recent_sessions must be positive")
        if replay and recent_sessions is not None:
            raise ValueError("replay and recent_sessions are mutually exclusive")
        by_id = {request.request_id: request for request in requests}
        if len(by_id) != len(requests) or any(not request.request_id for request in requests):
            raise ValueError("Chanlun scan request_id values must be non-empty and unique")
        payload_items = [
            {
                "symbol": request.symbol,
                "request_id": request.request_id,
                "dates": [bar.trade_date.isoformat() for bar in request.bars],
                "highs": [bar.high for bar in request.bars],
                "lows": [bar.low for bar in request.bars],
            }
            for request in requests
            if request.bars
        ]
        if not payload_items:
            return {}
        try:
            request_payload: dict[str, object] = {"items": payload_items}
            if replay:
                request_payload["replay"] = True
            if recent_sessions is not None:
                request_payload["recent_sessions"] = recent_sessions
            completed = subprocess.run(
                self.command, input=json.dumps(request_payload, ensure_ascii=False),
                text=True, encoding="utf-8", capture_output=True, cwd=self.cwd,
                timeout=self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Unable to run chanlun bridge: {exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise RuntimeError(f"Chanlun bridge failed: {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Chanlun bridge returned invalid JSON") from exc
        return _parse_bridge_response(payload, by_id)

    def replay(
        self, bars_by_symbol: Mapping[str, Sequence[MarketBar]]
    ) -> dict[str, list[ChanlunSignal]]:
        """Replay all daily prefixes inside one Rust process per supplied batch."""
        requests = [
            ChanlunScanRequest(symbol, symbol, tuple(bars))
            for symbol, bars in sorted(bars_by_symbol.items())
            if bars
        ]
        if not requests:
            return {}
        scanned = self._scan_requests(requests, replay=True)
        return {request.symbol: scanned[request.request_id] for request in requests}

    def replay_batches(
        self,
        bars_by_symbol: Mapping[str, Sequence[MarketBar]],
        *,
        batch_size: int = 100,
    ) -> dict[str, list[ChanlunSignal]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        rows = sorted(bars_by_symbol.items())
        result: dict[str, list[ChanlunSignal]] = {}
        for start in range(0, len(rows), batch_size):
            result.update(self.replay(dict(rows[start:start + batch_size])))
        return result

    def scan_batches(self, bars_by_symbol: Mapping[str, Sequence[MarketBar]], *, batch_size: int = 100) -> dict[str, list[ChanlunSignal]]:
        """Scan bounded symbol batches without starting one process per symbol."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        rows = sorted(bars_by_symbol.items())
        result: dict[str, list[ChanlunSignal]] = {}
        for start in range(0, len(rows), batch_size):
            result.update(self.scan(dict(rows[start:start + batch_size])))
        return result

    def scan_recent_prefixes(self, bars_by_symbol: Mapping[str, Sequence[MarketBar]], as_of_date: date,
                             *, sessions: int = 5) -> dict[tuple[str, date], list[ChanlunSignal]]:
        """Return the baseline plus recent prefix views from one compact Rust scan."""
        if sessions < 1:
            raise ValueError("sessions must be positive")
        dates = sorted({bar.trade_date for bars in bars_by_symbol.values() for bar in bars if bar.trade_date <= as_of_date})
        if as_of_date not in dates:
            raise ValueError("as_of_date must be a trading date in supplied bars")
        first_index = max(0, len(dates) - sessions)
        prefix_dates = dates[max(0, first_index - 1):]
        requests = [
            ChanlunScanRequest(
                symbol,
                symbol,
                tuple(bar for bar in bars if bar.trade_date <= as_of_date),
            )
            for symbol, bars in sorted(bars_by_symbol.items())
            if any(bar.trade_date <= as_of_date for bar in bars)
        ]
        scanned = self._scan_requests(
            requests, replay=False, recent_sessions=sessions
        )
        return {
            (request.symbol, visible_date): [
                replace(signal, confirmed_date=None, first_visible_date=None)
                for signal in scanned[request.request_id]
                if signal.confirmed_date is not None
                and signal.confirmed_date <= visible_date
            ]
            for request in requests for visible_date in prefix_dates
        }


@dataclass(frozen=True)
class ChanlunScanRequest:
    request_id: str
    symbol: str
    bars: tuple[MarketBar, ...]


def _parse_bridge_response(payload: object, requests: Mapping[str, ChanlunScanRequest]) -> dict[str, list[ChanlunSignal]]:
    if not isinstance(payload, dict):
        raise ValueError("Chanlun bridge response must be an object")
    if payload.get("schema_version") != "chanlun_bridge/v1":
        raise ValueError("Chanlun bridge response has an unsupported schema_version")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Chanlun bridge response requires a results list")
    output: dict[str, list[ChanlunSignal]] = {}
    for result in raw_results:
        if not isinstance(result, dict):
            raise ValueError("Chanlun bridge result must be an object")
        symbol = result.get("symbol")
        # v1 returns null for the ordinary one-request-per-symbol path.
        request_id = result.get("request_id") or symbol
        markers = result.get("markers", result.get("signals"))
        request = requests.get(request_id) if isinstance(request_id, str) else None
        if not isinstance(symbol, str) or request is None or request.symbol != symbol:
            raise ValueError("Chanlun bridge result has an unknown symbol or request_id")
        if not isinstance(markers, list):
            raise ValueError(f"Chanlun bridge result for {symbol} requires markers")
        errors = result.get("errors")
        if not isinstance(errors, list):
            raise ValueError(f"Chanlun bridge result for {symbol} requires errors")
        if errors:
            raise RuntimeError(f"Chanlun bridge reported errors for {symbol}: {errors}")
        directions = result.get("directions", {})
        zhongshus = result.get("zhongshus", [])
        if not isinstance(directions, dict) or not isinstance(zhongshus, list):
            raise ValueError(f"Chanlun bridge result for {symbol} has invalid structural metadata")
        chanlun = {"directions": directions, "zhongshus": zhongshus}
        bars = request.bars
        parsed: list[ChanlunSignal] = []
        for marker in markers:
            if not isinstance(marker, dict):
                raise ValueError(f"Chanlun bridge marker for {symbol} must be an object")
            raw_kind = marker.get("kind", marker.get("signal", marker.get("type")))
            try:
                kind = SignalKind(str(raw_kind))
            except ValueError as exc:
                raise ValueError(f"Chanlun bridge marker for {symbol} has invalid kind") from exc
            raw_index = marker.get("bar_index", marker.get("anchor_index"))
            if not isinstance(raw_index, int) or not 0 <= raw_index < len(bars):
                raise ValueError(f"Chanlun bridge marker for {symbol} has invalid bar_index")
            anchor_trade_date = marker.get("anchor_trade_date")
            if anchor_trade_date is not None and anchor_trade_date != bars[raw_index].trade_date.isoformat():
                raise ValueError(f"Chanlun bridge marker for {symbol} has inconsistent anchor_trade_date")
            price = marker.get("price")
            if price is not None and not isinstance(price, (int, float)):
                raise ValueError(f"Chanlun bridge marker for {symbol} has invalid price")
            confirmed_value = marker.get("confirmed_trade_date")
            confirmed_date = None
            if confirmed_value is not None:
                try:
                    confirmed_date = date.fromisoformat(str(confirmed_value))
                except ValueError as exc:
                    raise ValueError(
                        f"Chanlun bridge marker for {symbol} has invalid confirmation date"
                    ) from exc
                available_dates = {bar.trade_date for bar in bars}
                if (
                    confirmed_date not in available_dates
                    or confirmed_date < bars[raw_index].trade_date
                ):
                    raise ValueError(
                        f"Chanlun bridge marker for {symbol} has inconsistent confirmation date"
                    )
            parsed.append(ChanlunSignal(
                symbol,
                kind,
                bars[raw_index].trade_date,
                confirmed_date=confirmed_date,
                first_visible_date=confirmed_date,
                price=float(price) if price is not None else None,
                chanlun=chanlun,
            ))
        output[request_id] = parsed
    missing = set(requests) - set(output)
    if missing:
        raise ValueError(f"Chanlun bridge omitted symbols: {', '.join(sorted(missing))}")
    return output


Scanner = Callable[[Mapping[str, Sequence[MarketBar]]], dict[str, list[ChanlunSignal]]]


def replay_signals(bars_by_symbol: Mapping[str, Sequence[MarketBar]], scanner: Scanner) -> list[ChanlunSignal]:
    """Run every daily prefix and stamp a marker when it first becomes visible."""
    by_date: dict[date, dict[str, list[MarketBar]]] = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        prefix: list[MarketBar] = []
        for bar in sorted(bars, key=lambda item: item.trade_date):
            prefix.append(bar)
            by_date[bar.trade_date][symbol] = list(prefix)
    seen: set[tuple[str, SignalKind, date]] = set()
    result: list[ChanlunSignal] = []
    cumulative: dict[str, list[MarketBar]] = {symbol: [] for symbol in bars_by_symbol}
    for visible_date in sorted(by_date):
        for symbol, prefix in by_date[visible_date].items():
            cumulative[symbol] = prefix
        scan = scanner({symbol: bars for symbol, bars in cumulative.items() if bars})
        for symbol, signals in scan.items():
            for signal in signals:
                if signal.symbol != symbol:
                    raise ValueError("Scanner returned a signal under the wrong symbol")
                key = (symbol, signal.kind, signal.anchor_date)
                if key not in seen:
                    seen.add(key)
                    result.append(signal.confirmed(visible_date))
    return sorted(result, key=lambda item: (item.confirmed_date or item.anchor_date, item.symbol, item.kind.value))


def replay_recent_signals(bars_by_symbol: Mapping[str, Sequence[MarketBar]], scanner: Scanner,
                          as_of_date: date, *, sessions: int = 5) -> list[ChanlunSignal]:
    """Confirm only the trailing daily window used by the live candidate pool.

    The immediately preceding prefix seeds ``seen``.  Markers that already
    existed then are deliberately old, while a marker first returned in the
    trailing window receives that day's confirmation date.  Full prefix replay
    remains available through :func:`replay_signals` for strategy acceptance.
    """
    if sessions < 1:
        raise ValueError("sessions must be positive")
    dates = sorted({bar.trade_date for bars in bars_by_symbol.values() for bar in bars if bar.trade_date <= as_of_date})
    if as_of_date not in dates:
        raise ValueError("as_of_date must be a trading date in supplied bars")
    first_index = max(0, len(dates) - sessions)
    visible_dates = dates[first_index:]

    def prefix(until: date) -> dict[str, list[MarketBar]]:
        return {symbol: [bar for bar in bars if bar.trade_date <= until] for symbol, bars in bars_by_symbol.items() if any(bar.trade_date <= until for bar in bars)}

    seen: set[tuple[str, SignalKind, date]] = set()
    if first_index:
        for symbol, signals in scanner(prefix(dates[first_index - 1])).items():
            seen.update((symbol, item.kind, item.anchor_date) for item in signals)
    result: list[ChanlunSignal] = []
    for visible_date in visible_dates:
        for symbol, signals in scanner(prefix(visible_date)).items():
            for signal in signals:
                key = (symbol, signal.kind, signal.anchor_date)
                if key not in seen:
                    seen.add(key)
                    result.append(signal.confirmed(visible_date))
    return sorted(result, key=lambda item: (item.confirmed_date or item.anchor_date, item.symbol, item.kind.value))


def confirm_recent_scans(scans: Mapping[tuple[str, date], Sequence[ChanlunSignal]], as_of_date: date,
                         *, sessions: int = 5) -> list[ChanlunSignal]:
    """Turn :meth:`RustChanlunBridge.scan_recent_prefixes` output into signals.

    The first (baseline) response supplies old marker identities only.  This is
    the no-lookahead companion to batched live scans and does not rescan data.
    """
    if sessions < 1:
        raise ValueError("sessions must be positive")
    dates = sorted({visible_date for _, visible_date in scans if visible_date <= as_of_date})
    if as_of_date not in dates:
        raise ValueError("as_of_date must occur in scan results")
    first_index = max(0, len(dates) - sessions)
    active_dates = set(dates[first_index:])
    by_symbol: dict[str, list[tuple[date, Sequence[ChanlunSignal]]]] = defaultdict(list)
    for (symbol, visible_date), signals in scans.items():
        if visible_date <= as_of_date:
            by_symbol[symbol].append((visible_date, signals))
    result: list[ChanlunSignal] = []
    for symbol, rows in by_symbol.items():
        seen: set[tuple[str, SignalKind, date]] = set()
        for visible_date, signals in sorted(rows):
            for signal in signals:
                if signal.symbol != symbol:
                    raise ValueError("Scan result contains a signal under the wrong symbol")
                key = (symbol, signal.kind, signal.anchor_date)
                if visible_date not in active_dates:
                    seen.add(key)
                elif key not in seen:
                    seen.add(key)
                    result.append(signal.confirmed(visible_date))
    return sorted(result, key=lambda item: (item.confirmed_date or item.anchor_date, item.symbol, item.kind.value))


def select_candidates(signals: Iterable[ChanlunSignal], bars_by_symbol: Mapping[str, Sequence[MarketBar]],
                      as_of_date: date, *, max_candidates: int = 20,
                      valid_sessions: int = 20,
                      anchor_valid_sessions: int = 20) -> list[ChanlunCandidate]:
    """Select active recent second/third buys; later sell markers invalidate them."""
    all_dates = sorted({bar.trade_date for bars in bars_by_symbol.values() for bar in bars if bar.trade_date <= as_of_date})
    date_positions = {value: index for index, value in enumerate(all_dates)}
    if as_of_date not in date_positions:
        raise ValueError("as_of_date must be a trading date in supplied bars")
    confirmed = [item for item in signals if item.confirmed_date and item.confirmed_date <= as_of_date]
    candidates: list[ChanlunCandidate] = []
    for signal in confirmed:
        if signal.kind not in BUY_KINDS:
            continue
        assert signal.confirmed_date is not None
        age = date_positions[as_of_date] - date_positions.get(signal.confirmed_date, -10_000)
        anchor_age = date_positions[as_of_date] - date_positions.get(
            signal.anchor_date, -10_000
        )
        invalidated = any(
            other.symbol == signal.symbol and other.kind in SELL_KINDS
            and other.confirmed_date is not None and signal.confirmed_date < other.confirmed_date <= as_of_date
            and 0 <= date_positions.get(other.confirmed_date, -10_000)
            - date_positions.get(other.anchor_date, -20_000)
            < anchor_valid_sessions
            for other in confirmed
        )
        if (
            age < 0
            or age >= valid_sessions
            or anchor_age < 0
            or anchor_age >= anchor_valid_sessions
            or invalidated
        ):
            continue
        candidates.append(ChanlunCandidate(
            symbol=signal.symbol, signal_id=signal.signal_id,
            candidate_source=("CHANLUN_SECOND_BUY" if signal.kind is SignalKind.SECOND_BUY else "CHANLUN_THIRD_BUY"),
            signal=signal.kind, anchor_date=signal.anchor_date, confirmed_date=signal.confirmed_date,
            first_visible_date=signal.first_visible_date or signal.confirmed_date,
            liquidity=_liquidity(bars_by_symbol.get(signal.symbol, ()), as_of_date),
            chanlun=signal.chanlun,
        ))
    # Same stock can have several still-active markers; research it once, using the newest.
    newest: dict[str, ChanlunCandidate] = {}
    for candidate in candidates:
        old = newest.get(candidate.symbol)
        if old is None or candidate.confirmed_date > old.confirmed_date:
            newest[candidate.symbol] = candidate
    return sorted(newest.values(), key=lambda item: (-item.confirmed_date.toordinal(), -item.liquidity, item.symbol))[:max_candidates]


def _liquidity(bars: Sequence[MarketBar], as_of_date: date) -> float:
    values = [bar.amount for bar in bars if bar.trade_date <= as_of_date and bar.amount is not None]
    return float(sum(values[-5:])) if values else 0.0


@dataclass(frozen=True)
class ExecutionRules:
    initial_cash: float = 100_000.0
    lot_size: int = 100
    commission_per_order: float = 1.0
    stamp_duty_rate: float = 0.0005
    price_limit_ratio: float = 0.10
    max_candidates: int = 20
    signal_valid_sessions: int = 20
    anchor_valid_sessions: int = 20
    minimum_completed_trades: int = 30
    minimum_history_days: int = 756


@dataclass(frozen=True)
class AcceptanceResult:
    annualized_return: float | None
    maximum_drawdown: float | None
    completed_trades: int
    history_days: int
    sufficient_sample: bool
    passed: bool
    trades: tuple[dict[str, object], ...]
    equity_curve: tuple[tuple[date, float], ...]
    diagnostics: dict[str, object] = field(default_factory=dict)


def run_acceptance(
    bars_by_symbol: Mapping[str, Sequence[MarketBar]],
    scanner: Scanner,
    *,
    execution_bars_by_symbol: Mapping[str, Sequence[MarketBar]] | None = None,
    historical_scanner: Scanner | None = None,
    price_limit_by_symbol: Mapping[str, float] | None = None,
    stamp_duty_rate_by_symbol: Mapping[str, float] | None = None,
    rules: ExecutionRules = ExecutionRules(),
) -> AcceptanceResult:
    """Point-in-time daily simulation: confirmed signal -> next open order.

    Buy orders that are locked at their daily limit are discarded.  A locked
    limit-down exit remains pending for the next session.  Positions are marked
    at close; simultaneous buy signals share available cash equally.
    """
    signals = (
        [
            signal
            for rows in historical_scanner(bars_by_symbol).values()
            for signal in rows
        ]
        if historical_scanner is not None
        else replay_signals(bars_by_symbol, scanner)
    )
    if any(signal.confirmed_date is None for signal in signals):
        raise ValueError("Historical Chanlun replay must stamp every confirmation date")
    execution_bars = execution_bars_by_symbol or bars_by_symbol
    calendar = sorted({bar.trade_date for bars in execution_bars.values() for bar in bars})
    signal_calendar = {
        bar.trade_date for bars in bars_by_symbol.values() for bar in bars
    }
    ordered_signal_calendar = sorted(signal_calendar)
    signal_date_positions = {
        value: index for index, value in enumerate(ordered_signal_calendar)
    }
    liquidity_dates: dict[str, list[date]] = {}
    liquidity_prefix: dict[str, list[float]] = {}
    for symbol, bars in bars_by_symbol.items():
        dated_amounts = [
            (bar.trade_date, float(bar.amount))
            for bar in bars if bar.amount is not None
        ]
        liquidity_dates[symbol] = [item[0] for item in dated_amounts]
        prefix = [0.0]
        for _, amount in dated_amounts:
            prefix.append(prefix[-1] + amount)
        liquidity_prefix[symbol] = prefix

    def liquidity_as_of(symbol: str, current_date: date) -> float:
        dates = liquidity_dates.get(symbol, [])
        end = bisect_right(dates, current_date)
        start = max(0, end - 5)
        prefix = liquidity_prefix.get(symbol, [0.0])
        return prefix[end] - prefix[start]
    by_symbol_date = {symbol: {bar.trade_date: bar for bar in bars} for symbol, bars in execution_bars.items()}
    signals_by_date: dict[date, list[ChanlunSignal]] = defaultdict(list)
    for signal in signals:
        assert signal.confirmed_date is not None
        signals_by_date[signal.confirmed_date].append(signal)
    cash = rules.initial_cash
    positions: dict[str, tuple[int, float, date, ChanlunSignal]] = {}
    pending_buys: list[ChanlunSignal] = []
    pending_sells: set[str] = set()
    recent_buy_signals: list[ChanlunSignal] = []
    latest_sell_confirmation: dict[str, date] = {}
    surfaced_signal_ids: set[str] = set()
    trades: list[dict[str, object]] = []
    equity_curve: list[tuple[date, float]] = []
    previous: dict[str, MarketBar] = {}
    for current_date in calendar:
        todays_buys, pending_buys = pending_buys, []
        # Exit orders run before entry orders.  They are necessarily at least T+1.
        for symbol in sorted(pending_sells.copy()):
            position = positions.get(symbol)
            bar = by_symbol_date.get(symbol, {}).get(current_date)
            if position is None:
                pending_sells.discard(symbol)
                continue
            limit = (price_limit_by_symbol or {}).get(symbol, rules.price_limit_ratio)
            if bar is None or _locked(bar, previous.get(symbol), limit, upward=False):
                continue
            quantity, cost, entry_date, entry_signal = position
            gross = quantity * bar.open
            fee = _fee(
                gross,
                False,
                rules,
                stamp_duty_rate=(stamp_duty_rate_by_symbol or {}).get(symbol),
            )
            cash += gross - fee
            trades.append({
                "symbol": symbol,
                "signal_id": entry_signal.signal_id,
                "signal_kind": entry_signal.kind.value,
                "anchor_date": entry_signal.anchor_date.isoformat(),
                "confirmed_date": (
                    entry_signal.confirmed_date or entry_date
                ).isoformat(),
                "entry_date": entry_date.isoformat(),
                "exit_date": current_date.isoformat(),
                "quantity": quantity,
                "entry_cost": cost,
                "exit_price": bar.open,
                "net_return": (gross - fee) / cost - 1,
            })
            del positions[symbol]
            pending_sells.discard(symbol)
        eligible_buys = [
            signal for signal in todays_buys
            if signal.symbol not in positions and signal.symbol not in pending_sells
            and (bar := by_symbol_date.get(signal.symbol, {}).get(current_date)) is not None
            and not _locked(
                bar,
                previous.get(signal.symbol),
                (price_limit_by_symbol or {}).get(
                    signal.symbol, rules.price_limit_ratio
                ),
                upward=True,
            )
        ]
        for signal in eligible_buys:
            bar = by_symbol_date[signal.symbol][current_date]
            allocation = cash / len(eligible_buys) if eligible_buys else 0.0
            quantity = math.floor(max(allocation - rules.commission_per_order, 0.0) / (bar.open * rules.lot_size)) * rules.lot_size
            gross = quantity * bar.open
            fee = _fee(gross, True, rules) if quantity else 0.0
            if quantity and gross + fee <= cash:
                cash -= gross + fee
                positions[signal.symbol] = (
                    quantity, gross + fee, current_date, signal
                )
        todays_signals = signals_by_date.get(current_date, [])
        current_signal_position = signal_date_positions.get(current_date)
        for signal in todays_signals:
            if (
                current_signal_position is None
                or not 0 <= current_signal_position - signal_date_positions.get(
                    signal.anchor_date, -10_000
                ) < rules.anchor_valid_sessions
            ):
                continue
            if signal.kind in BUY_KINDS:
                recent_buy_signals.append(signal)
            elif signal.kind in SELL_KINDS:
                latest_sell_confirmation[signal.symbol] = current_date
                if signal.symbol in positions:
                    pending_sells.add(signal.symbol)
        if current_date in signal_calendar:
            current_position = signal_date_positions[current_date]
            recent_buy_signals = [
                signal for signal in recent_buy_signals
                if signal.confirmed_date is not None
                and 0 <= current_position - signal_date_positions.get(
                    signal.confirmed_date, -10_000
                ) < rules.signal_valid_sessions
            ]
            active = [
                signal for signal in recent_buy_signals
                if latest_sell_confirmation.get(signal.symbol, date.min)
                <= (signal.confirmed_date or date.min)
                and 0 <= current_position - signal_date_positions.get(
                    signal.anchor_date, -10_000
                ) < rules.anchor_valid_sessions
            ]
            newest: dict[str, ChanlunSignal] = {}
            for signal in active:
                current = newest.get(signal.symbol)
                if (
                    current is None
                    or (signal.confirmed_date or date.min)
                    > (current.confirmed_date or date.min)
                ):
                    newest[signal.symbol] = signal
            selected = sorted(
                newest.values(),
                key=lambda signal: (
                    -(signal.confirmed_date or date.min).toordinal(),
                    -liquidity_as_of(signal.symbol, current_date),
                    signal.symbol,
                ),
            )[:rules.max_candidates]
            for signal in selected:
                if signal.signal_id in surfaced_signal_ids:
                    continue
                surfaced_signal_ids.add(signal.signal_id)
                pending_buys.append(signal)
        marked = cash + sum(
            qty * by_symbol_date[symbol].get(
                current_date, previous[symbol]
            ).close
            for symbol, (qty, _, _, _) in positions.items()
        )
        equity_curve.append((current_date, marked))
        for symbol, by_date in by_symbol_date.items():
            if current_date in by_date:
                previous[symbol] = by_date[current_date]
    if calendar:
        last_date = calendar[-1]
        for symbol, (
            quantity, cost, entry_date, entry_signal
        ) in list(positions.items()):
            bar = by_symbol_date[symbol].get(last_date, previous.get(symbol))
            if bar is None:
                continue
            gross = quantity * bar.close
            fee = _fee(
                gross,
                False,
                rules,
                stamp_duty_rate=(stamp_duty_rate_by_symbol or {}).get(symbol),
            )
            trades.append({
                "symbol": symbol,
                "signal_id": entry_signal.signal_id,
                "signal_kind": entry_signal.kind.value,
                "anchor_date": entry_signal.anchor_date.isoformat(),
                "confirmed_date": (
                    entry_signal.confirmed_date or entry_date
                ).isoformat(),
                "entry_date": entry_date.isoformat(),
                "exit_date": last_date.isoformat(),
                "quantity": quantity,
                "entry_cost": cost,
                "exit_price": bar.close,
                "net_return": (gross - fee) / cost - 1,
                "marked_to_market": True,
            })
    annualized = _annualized(equity_curve, rules.initial_cash)
    drawdown = _maximum_drawdown(equity_curve)
    completed = sum(not trade.get("marked_to_market") for trade in trades)
    sufficient = len(calendar) >= rules.minimum_history_days and completed >= rules.minimum_completed_trades
    diagnostics: dict[str, object] = {
        "confirmed_signal_count": len(signals),
        "signal_counts": dict(sorted(Counter(
            signal.kind.value for signal in signals
        ).items())),
        "surfaced_buy_signal_count": len(surfaced_signal_ids),
        "entered_position_count": len(trades),
        "open_position_symbols": sorted(positions),
    }
    return AcceptanceResult(
        annualized,
        drawdown,
        completed,
        len(calendar),
        sufficient,
        bool(
            sufficient
            and annualized is not None
            and annualized > 0
            and drawdown is not None
            and drawdown >= -0.50
        ),
        tuple(trades),
        tuple(equity_curve),
        diagnostics,
    )


def _fee(
    notional: float,
    buy: bool,
    rules: ExecutionRules,
    *,
    stamp_duty_rate: float | None = None,
) -> float:
    rate = rules.stamp_duty_rate if stamp_duty_rate is None else stamp_duty_rate
    return rules.commission_per_order + (0.0 if buy else notional * rate)


def _locked(bar: MarketBar, previous: MarketBar | None, limit: float, *, upward: bool) -> bool:
    if previous is None:
        return False
    boundary = previous.close * (1 + limit if upward else 1 - limit)
    tolerance = max(boundary * 0.001, 0.001)
    values = (bar.open, bar.high, bar.low, bar.close)
    return all(value >= boundary - tolerance for value in values) if upward else all(value <= boundary + tolerance for value in values)


def _annualized(curve: Sequence[tuple[date, float]], initial_cash: float) -> float | None:
    if len(curve) < 2 or initial_cash <= 0 or curve[-1][1] <= 0:
        return None
    years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1 / 365.25)
    return (curve[-1][1] / initial_cash) ** (1 / years) - 1


def _maximum_drawdown(curve: Sequence[tuple[date, float]]) -> float | None:
    if not curve:
        return None
    peak = curve[0][1]
    drawdown = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return drawdown
