"""Explicit adjusted-series fallback; an unadjusted `day` response is not qfq."""
import requests


def fetch_etf_qfq(symbol, prefix, start_date, end_date, *, get=requests.get):
    code = prefix + symbol
    response = get("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                   params={"param": f"{code},day,{start_date.isoformat()},{end_date.isoformat()},320,qfq"}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    rows = (payload.get("data") or {}).get(code, {}).get("qfqday")
    if payload.get("code", 0) != 0 or not rows:
        raise ValueError("Tencent未返回该代码明确标注的qfqday序列，不能用day替代")
    return [{"date": row[0], "open": row[1], "close": row[2], "high": row[3], "low": row[4],
             "volume": float(row[5]) * 100, "amount": None}
            for row in rows if len(row) >= 6 and start_date.isoformat() <= row[0] <= end_date.isoformat()]
