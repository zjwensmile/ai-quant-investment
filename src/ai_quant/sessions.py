from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum


class MarketSession(StrEnum):
    PRE_MARKET = "pre_market"
    INTRADAY = "intraday"
    LUNCH_BREAK = "lunch_break"
    POST_MARKET = "post_market"
    CLOSED_NON_TRADING_DAY = "closed_non_trading_day"


@dataclass(frozen=True)
class SessionState:
    session: MarketSession
    quote_max_age_seconds: int | None
    execution_eligible: bool


def session_state(as_of: datetime, is_trading_day: bool) -> SessionState:
    if not is_trading_day:
        return SessionState(MarketSession.CLOSED_NON_TRADING_DAY, None, False)
    value = as_of.timetz().replace(tzinfo=None)
    if value < time(9, 30):
        return SessionState(MarketSession.PRE_MARKET, None, False)
    if time(9, 30) <= value <= time(11, 30) or time(13, 0) <= value <= time(15, 0):
        return SessionState(MarketSession.INTRADAY, 300, True)
    if time(11, 30) < value < time(13, 0):
        return SessionState(MarketSession.LUNCH_BREAK, 1800, False)
    return SessionState(MarketSession.POST_MARKET, None, False)
