"""Locally verified annual exchange schedule; never extrapolate beyond its year."""
from datetime import date, datetime, timedelta


SOURCE_URL = "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml"
VERIFIED_AT = "2026-09-09T13:53:52+08:00"
HOLIDAY_RANGES_2026 = (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
                       ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07"))


def verified_exchange_calendar(as_of: datetime) -> tuple[list[dict], dict]:
    if as_of.year != 2026 or as_of < datetime.fromisoformat(VERIFIED_AT):
        return [], {}
    holidays = set()
    for first, last in HOLIDAY_RANGES_2026:
        day, end = date.fromisoformat("2026-" + first), date.fromisoformat("2026-" + last)
        while day <= end:
            holidays.add(day)
            day += timedelta(days=1)
    rows = []
    day = date(2026, 1, 1)
    while day.year == 2026:
        if day.weekday() < 5 and day not in holidays:
            rows.append({"trade_date": day.isoformat()})
        day += timedelta(days=1)
    return rows, {"source_url": SOURCE_URL, "published_at": "2025-12-22",
                  "available_at": VERIFIED_AT, "coverage_start": "2026-01-01", "coverage_end": "2026-12-31",
                  "basis": "exchange_annual_holidays_and_weekend_closures", "cache_reused": True}
