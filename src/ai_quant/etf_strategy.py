from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math


MODULE = "etf"
MIN_AUM = Decimal("1000000000")
MIN_TURNOVER = Decimal("10000000")
MAX_AUM_AGE_DAYS = 120
CROSS_BORDER = {"hong_kong_equity", "overseas_equity"}
GOVERNMENT_BOND_TERMS = ("国债", "地方债", "地方政府债", "政金债", "政策性金融债", "政策金融债", "政策性银行债", "国开债", "农发债", "口行债", "进出口银行债", "国家开发银行债", "农业发展银行债")


def _identity_text(candidate: dict) -> str:
    """Apply the account mandate to fund identity and underlying, including cached facts."""
    facts = candidate.get("facts", {})
    descriptions = []
    for data in (candidate, facts, candidate.get("source_fields", {}), facts.get("source_fields", {})):
        for key in ("name", "full_name", "fund_name", "index_id", "tracking_index", "asset_group", "fund_type",
                    "基金全称", "基金简称", "跟踪标的", "基金类型"):
            descriptions.append(str(data.get(key, "")))
    return " ".join(descriptions).lower().replace(" ", "")


def etf_buy_excluded(candidate: dict) -> bool:
    """No new debt or cash-like fund exposure, including T repurchases."""
    text = _identity_text(candidate)
    return any(term in text for term in GOVERNMENT_BOND_TERMS +
               ("债", "短融", "融资券", "固收", "货币", "现金管理", "现金收益", "现金增利", "理财", "bond", "treasury",
                "fixed_income", "fixedincome", "money_market", "cash_management")) or any(
                    data.get("asset_group") in {"money", "cash", "cash_equivalent", "bond", "fixed_income"}
                    for data in (candidate, candidate.get("facts", {})))


def government_bond_buy_excluded(candidate: dict) -> bool:
    """Compatibility entry point; the current mandate excludes ALL debt/cash funds."""
    return etf_buy_excluded(candidate)


def etf_market_region(candidate: dict) -> str:
    """Geography supported by captured name/index fields; QDII alone proves no country."""
    text = _identity_text(candidate)
    if etf_buy_excluded(candidate):
        return "excluded"
    if any(term in text for term in ("黄金", "上海金", "gold")):
        return "gold"
    if any(term in text for term in ("中韩", "亚太", "全球", "亚洲")):
        return "mixed"
    if any(term in text for term in ("港股", "恒生", "香港", "hong_kong")):
        return "hong_kong"
    if any(term in text for term in ("纳斯达克", "纳指", "nasdaq", "标普", "s&p", "美国", "道琼斯")):
        return "us"
    if any(term in text for term in ("日本", "日经", "东京", "nikkei")):
        return "japan"
    if any(term in text for term in ("德国", "法国", "英国", "欧洲", "欧元", "dax", "富时100")):
        return "europe"
    if any(term in text for term in ("印度", "nifty", "sensex")):
        return "india"
    if "a_share_equity" in text:
        return "a_share"
    return "unknown"


def etf_research_bucket(candidate: dict) -> str:
    region = etf_market_region(candidate)
    text = _identity_text(candidate)
    if region == "us":
        if any(term in text for term in ("纳斯达克", "纳指", "nasdaq")):
            return "us_nasdaq"
        if any(term in text for term in ("标普500", "s&p500")):
            return "us_sp500"
    return region


def etf_region_excluded(candidate: dict, policy: dict | None = None) -> bool:
    return (candidate.get("region_buy_excluded") is True
            or etf_market_region(candidate) in (policy or {}).get("excluded_equity_regions", []))


def etf_sector(candidate: dict) -> str | None:
    """Name/index evidence for discussion, not a measured holdings overlap."""
    if etf_buy_excluded(candidate):
        return None
    facts = candidate.get("facts", {})
    # Legal full names contain “证券投资基金”, which is not a financial-sector index.
    text = " ".join(str(data.get(key, "")) for data in (candidate, facts,
                    candidate.get("source_fields", {}), facts.get("source_fields", {}))
                    for key in ("index_id", "tracking_index", "跟踪标的"))
    if not text.strip():
        text = " ".join(str(data.get(key, "")) for data in (candidate, facts,
                        candidate.get("source_fields", {}), facts.get("source_fields", {}))
                        for key in ("name", "基金简称"))
    if any(term in text for term in ("期货", "商品", "黄金", "上海金")):
        return None
    if any(term in text.lower() for term in ("科创50", "科创板50", "创业板指", "创业板50", "纳斯达克100", "纳指100", "nasdaq100")):
        return None
    for sector, terms in (
        ("semiconductors", ("半导体", "芯片")),
        ("healthcare", ("医药", "医疗", "创新药", "生物科技")),
        ("financials", ("银行", "证券", "券商", "金融")),
        ("consumer", ("消费", "食品", "饮料", "白酒", "家电")),
        ("energy", ("新能源", "光伏", "电池", "石油", "煤炭", "能源")),
        ("technology", ("互联网", "人工智能", "计算机", "软件", "通信", "科技")),
        ("industrials", ("军工", "机器人", "机械", "制造")),
        ("materials", ("有色", "钢铁", "化工", "稀土")),
        ("utilities", ("电力", "公用事业")),
    ):
        if any(term in text for term in terms):
            return sector
    return None


def select_etf_research_candidates(rows: list[dict], limit: int = 12,
                                   held_symbols=()) -> list[dict]:
    """Reserve market representatives before score truncation; gaps stay visible."""
    ranked = sorted((row for row in rows if not etf_buy_excluded(row) and not etf_region_excluded(row)),
                    key=lambda row: (row.get("eligible") is not True, -float(row.get("score", 0)), row["symbol"]))
    selected = []
    # Both US indices must reach discussion, where their overlap can be assessed.
    for bucket in ("a_share", "hong_kong", "us_sp500", "us_nasdaq"):
        representatives = [row for row in ranked if etf_research_bucket(row) == bucket]
        if bucket in {"a_share", "hong_kong"}:
            broad_terms = (("沪深300", "中证a500", "a500", "中证500", "上证50") if bucket == "a_share"
                           else ("恒生指数", "恒生中国企业"))
            representatives.sort(key=lambda row: (row.get("eligible") is not True,
                0 if any(term in _identity_text(row) for term in broad_terms)
                else 1 if bucket == "hong_kong" and "恒生科技" in _identity_text(row) else 2))
        representative = next(iter(representatives), None)
        if representative is not None and len(selected) < max(0, limit):
            selected.append(representative)
    chosen = {row["symbol"] for row in selected}
    sectors = {etf_sector(row) for row in selected}
    for row in ranked:
        sector = etf_sector(row)
        if (row.get("eligible") is not False and sector and sector not in sectors
                and row["symbol"] not in chosen and len(selected) < max(0, limit)):
            selected.append(row)
            chosen.add(row["symbol"])
            sectors.add(sector)
    for row in ranked:
        if row["symbol"] not in chosen and len(selected) < max(0, limit):
            selected.append(row)
            chosen.add(row["symbol"])
    selected.extend(row for row in rows if row["symbol"] in held_symbols and row["symbol"] not in chosen)
    return selected


def etf_diversification_error(candidates: dict, weights: dict) -> str | None:
    """New mandates require meaningful exposure to two evidenced equity regions."""
    if not candidates.get("policy", {}).get("global_diversification_required"):
        return None
    rows = {row["symbol"]: row for row in candidates.get("candidates", [])}
    equity_regions = {"a_share", "hong_kong", "us", "japan", "europe", "india"}
    totals = {}
    for symbol, value in weights.items():
        weight = _decimal(value)
        if weight is None or weight <= 0:
            continue
        region = etf_market_region(rows.get(symbol, {}))
        if region in equity_regions:
            totals[region] = totals.get(region, Decimal(0)) + weight
    if any(weight > Decimal("0.60") for weight in totals.values()):
        return "equity_region_weight_above_60pct"
    if len(totals) < 2:
        return "global_portfolio_requires_two_verified_equity_regions"
    return None


def _decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _time(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else None
    except ValueError:
        return None


def etf_exposure_bounds(candidates: dict) -> tuple[Decimal, Decimal]:
    policy = candidates.get("policy", {})
    low = _decimal(policy.get("etf_min_weight", "0.90"))
    high = _decimal(policy.get("etf_max_weight", "0.98"))
    if low is None or high is None or not Decimal(0) < low <= high <= Decimal("0.98"):
        raise ValueError("invalid_etf_exposure_policy")
    return low, high


def build_etf_candidates(snapshot: dict, market_store=None, limit: int = 20, *, policy: dict | None = None) -> dict:
    """Auditable data screening only; eligibility is not an investment approval."""
    as_of = snapshot.get("as_of_time", snapshot.get("as_of"))
    clock = _time(as_of)
    if clock is None:
        raise ValueError("ETF 快照必须包含有时区的 as_of_time")
    attributes = market_store.etf_attribute_map(known_at=as_of) if market_store else {}
    facts = snapshot.get("etf_facts", {})
    from .providers.etf_evidence import compact_etf_evidence
    evidence, overlap_evidence = compact_etf_evidence(snapshot)
    min_weight, max_weight = etf_exposure_bounds({"policy": policy or {}})
    target_weight = _decimal((policy or {}).get("etf_target_weight", min(max(Decimal("0.95"), min_weight), max_weight)))
    if target_weight is None or not min_weight <= target_weight <= max_weight:
        raise ValueError("invalid_etf_target_weight")
    sector_target = _decimal((policy or {}).get("sector_target_weight", "0.40"))
    if sector_target is None or not 0 <= sector_target <= max_weight:
        raise ValueError("invalid_etf_sector_target_weight")
    rows = []
    for security in snapshot.get("securities", []):
        symbol = str(security.get("symbol", ""))
        if (security.get("security_type") != "etf" or security.get("exchange") not in {"SSE", "SZSE"}
                or len(symbol) != 6 or not symbol.isdigit()
                or (security["exchange"] == "SSE" and not symbol.startswith("5"))
                or (security["exchange"] == "SZSE" and not symbol.startswith("1"))):
            continue
        fact = facts.get(symbol, {})
        quote = snapshot.get("quotes", {}).get(symbol, {})
        price, aum, turnover = (_decimal(quote.get("last")), _decimal(fact.get("aum_cny")), _decimal(quote.get("turnover")))
        gaps = list(fact.get("evidence_gaps", []))
        reasons, risks, conditions = [], [], []
        buy_excluded = etf_buy_excluded({**security, "facts": fact})
        if buy_excluded:
            reasons.append("bond_or_money_buy_excluded_by_user")
        if price is None or price <= 0:
            gaps.append("valid_price_missing")
        if security.get("trading_status") != "tradable":
            gaps.append("trading_status_unconfirmed")
        available = _time(quote.get("available_at"))
        cutoff = snapshot.get("market", {}).get("latest_completed_trade_date")
        if available is None or available > clock or (cutoff and available.date().isoformat() < cutoff):
            gaps.append("quote_timestamp_unusable")
        aum_known = _time(fact.get("aum_available_at"))
        try:
            aum_date = date.fromisoformat(str(fact.get("aum_as_of")))
            aum_age = (clock.date() - aum_date).days
        except ValueError:
            aum_age = -1
        if (aum is None or aum <= 0 or not fact.get("aum_source_url")
                or fact.get("aum_basis") != "reported_net_assets" or aum_known is None
                or aum_known > clock or not 0 <= aum_age <= MAX_AUM_AGE_DAYS
                or (aum_age >= 0 and aum_known is not None and aum_date > aum_known.date())):
            gaps.append("reported_aum_unverified_or_stale")
        elif aum < MIN_AUM:
            reasons.append("aum_below_1bn_cny")
        liquidity_turnover, liquidity_basis, liquidity_dates = turnover, "snapshot_accumulated_turnover", []
        partial_session = snapshot.get("market", {}).get("session") in {"pre_market", "intraday", "lunch_break"}
        if partial_session and (turnover is None or turnover < MIN_TURNOVER):
            loader = getattr(market_store, "bars_as_known", None)
            bars = loader(symbol, 5, date.fromisoformat(cutoff), as_of) if loader and cutoff and fact else []
            amounts = [_decimal(bar.get("amount")) for bar in bars]
            expected_dates = snapshot.get("market", {}).get("recent_completed_trade_dates", [])[-5:]
            if (len(bars) == len(expected_dates) == 5 and [str(bar.get("trade_date")) for bar in bars] == expected_dates
                    and all(amount is not None and amount >= 0 for amount in amounts)):
                liquidity_turnover = sum(amounts, Decimal(0)) / 5
                liquidity_basis = "five_completed_sessions_mean_amount"
                liquidity_dates = [str(bar["trade_date"]) for bar in bars]
            else:
                liquidity_turnover = None
                risks.append("partial_session_turnover_not_full_day_liquidity")
                conditions.append("verify_live_depth_and_completed_session_liquidity_before_execution")
        if liquidity_turnover is not None and liquidity_turnover < MIN_TURNOVER:
            reasons.append("turnover_below_10m_cny")
        elif liquidity_turnover is None and not partial_session:
            reasons.append("turnover_unverified")
        index_id = fact.get("index_id")
        group = fact.get("asset_group", "unknown")
        region_excluded = etf_region_excluded({**security, "facts": fact}, policy)
        if region_excluded:
            reasons.append("equity_region_excluded_by_user")
        metadata_at = _time(fact.get("metadata_available_at"))
        if (not index_id or group == "unknown" or not fact.get("metadata_source_url")
                or metadata_at is None or metadata_at > clock or fact.get("is_etf") is not True):
            gaps.append("fund_identity_or_underlying_unverified")
        attribute = attributes.get(symbol, {})
        reference = security.get("exchange_reference", {})
        t0 = attribute.get("same_day_turnaround", security.get("same_day_turnaround"))
        rule_source = attribute.get("source_url", reference.get("source_url"))
        rule_at = _time(attribute.get("available_at", reference.get("available_at")))
        if t0 not in (True, False, 0, 1) or t0 is None or not rule_source or rule_at is None or rule_at > clock:
            t0 = None
            gaps.append("exchange_turnaround_unverified")
        premium = _decimal(fact.get("premium_rate"))
        risks.extend(["tracking_error_not_guaranteed", "fund_event_risk_requires_review"])
        if group in CROSS_BORDER:
            risks.extend(["fx_exposure", "foreign_market_hours_mismatch", "cross_border_premium"])
            premium_at = _time(fact.get("premium_available_at"))
            if (premium is None or not fact.get("premium_source_url") or premium_at is None
                    or premium_at > clock or (clock - premium_at).total_seconds() > 1800):
                conditions.append("verify_current_premium_before_execution")
            elif premium > Decimal("0.01"):
                reasons.append("cross_border_premium_above_1pct")
            if fact.get("underlying_market_open") is not True or not fact.get("foreign_calendar_source_url"):
                risks.append("foreign_market_closed_or_unverified")
                conditions.append("reassess_foreign_holiday_nav_and_price_discovery_before_execution")
        if fact.get("event_risk") == "high":
            reasons.append("high_event_risk")
        score = (60 * min(max(math.log10(float(max(aum or Decimal(1), Decimal(1)))) - 9, 0), 3) / 3
                 + 40 * min(max(math.log10(float(max(liquidity_turnover or Decimal(1), Decimal(1)))) - 7, 0), 3) / 3)
        rows.append({"symbol": symbol, "name": security.get("name"), "exchange": security["exchange"],
                     "type": "etf", "asset_group": group, "index_id": index_id,
                     "concentration_group": fact.get("concentration_group", group),
                     "price": str(price) if price is not None else None, "score": round(score, 4),
                     "eligible": not gaps and not reasons, "reasons": reasons, "risks": risks,
                     "buy_excluded": buy_excluded,
                     "region_buy_excluded": region_excluded,
                     "market_region": etf_market_region({**security, "facts": fact}),
                     "research_bucket": etf_research_bucket({**security, "facts": fact}),
                     "sector": etf_sector({**security, "facts": fact}),
                     "lookthrough_evidence": evidence.get(symbol, {}),
                     "overlap_review": ["compare_underlying_holdings_and_index_weights_before_allocation"],
                     "execution_conditions": conditions, "trading_status": security.get("trading_status"),
                     "quote_available_at": quote.get("available_at"),
                     "evidence_gaps": gaps, "same_day_turnaround": bool(t0) if t0 is not None else None,
                     "exchange_reference": {"source_url": rule_source, "available_at": rule_at.isoformat() if rule_at else None},
                     "buy_lot_size": security.get("buy_lot_size", 100), "price_tick": security.get("price_tick", "0.001"),
                     "metrics": {"aum_cny": str(aum) if aum is not None else None,
                                 "turnover_cny": str(turnover) if turnover is not None else None,
                                 "liquidity_turnover_cny": str(liquidity_turnover) if liquidity_turnover is not None else None,
                                 "liquidity_basis": liquidity_basis, "liquidity_trade_dates": liquidity_dates,
                                 "premium_rate": str(premium) if premium is not None else None},
                     "aum_as_of": fact.get("aum_as_of"), "aum_source_url": fact.get("aum_source_url"),
                     "aum_available_at": fact.get("aum_available_at"), "facts": fact})
    rows.sort(key=lambda row: (not row["eligible"], -row["score"], row["symbol"]))
    chosen = set()
    for row in rows:
        if row["eligible"]:
            if row["index_id"] in chosen:
                row["eligible"] = False
                row["reasons"].append("duplicate_tracking_index")
            else:
                chosen.add(row["index_id"])
    rows.sort(key=lambda row: (not row["eligible"], -row["score"], row["symbol"]))
    held = {row.get("symbol") for row in snapshot.get("account", {}).get("positions", [])}
    selected = select_etf_research_candidates(rows, limit)
    # Preserve rejected diagnostics in unused display slots; they never enter research.
    selected.extend(row for row in rows if etf_buy_excluded(row) or etf_region_excluded(row))
    selected = selected[:max(0, limit)]
    selected_symbols = {row["symbol"] for row in selected}
    selected.extend(row for row in rows if row["symbol"] in held and row["symbol"] not in selected_symbols)
    return {"module": MODULE, "as_of": as_of, "candidates": selected, "screened_count": len(rows),
            "overlap_evidence": overlap_evidence,
            "eligible_count": sum(row["eligible"] for row in rows), "execution_authorized": False,
            "policy": {"min_aum_cny": str(MIN_AUM), "min_turnover_cny": str(MIN_TURNOVER),
                       "max_aum_age_days": MAX_AUM_AGE_DAYS, "max_per_index": 1,
                       "government_bond_related_buys_allowed": False,
                       "bond_and_cash_etf_buys_allowed": False,
                       "etf_min_weight": str(min_weight), "etf_max_weight": str(max_weight),
                       "etf_target_weight": str(target_weight),
                       "excluded_equity_regions": list((policy or {}).get("excluded_equity_regions", [])),
                       "priority_research_buckets": [b for b in ["a_share", "hong_kong", "us_sp500", "us_nasdaq"]
                                                     if b not in (policy or {}).get("excluded_equity_regions", [])],
                       "sector_coverage_required_for_research": True,
                       "sector_allocation_objective": "high_prosperity_growth",
                       "sector_target_weight": str(sector_target),
                       "overlap_review": ["sp500_and_nasdaq_may_share_large_cap_holdings",
                                          "broad_and_sector_indices_may_share_holdings",
                                          "different_regions_do_not_prove_sector_diversification"],
                       "global_diversification_required": True}}


def build_etf_allocation(candidates: dict, account: dict, weights: dict | None = None) -> dict:
    """Target holdings for an isolated ETF account; never records funding or fills."""
    result = {"module": MODULE, "status": "no_valid_plan", "execution_authorized": False,
              "funding_confirmed": account.get("funding_confirmed") is True,
              "account_version": account.get("version"), "as_of": candidates.get("as_of"),
              "positions": [], "reasons": []}

    def fail(reason):
        result["positions"] = []
        result["reasons"].append(reason)
        return result

    try:
        min_weight, max_weight = etf_exposure_bounds(candidates)
    except ValueError as exc:
        return fail(str(exc))
    result["exposure_policy"] = {"etf_min_weight": str(min_weight), "etf_max_weight": str(max_weight)}
    capital = _decimal(account.get("planned_capital_cny", account.get("capital_budget")))
    all_rows = {row["symbol"]: row for row in candidates.get("candidates", [])}
    current = {row["symbol"]: row for row in account.get("positions", [])}
    cash = _decimal(account.get("cash_available_cny", account.get("cash")))
    if cash is not None and cash < 0:
        return fail("invalid_negative_cash")
    nav_confirmed = (result["funding_confirmed"] and cash is not None and cash >= 0
                     and account.get("positions_confirmed") is True)
    if nav_confirmed:
        nav = cash
        missing_marks = []
        for symbol, position in current.items():
            if int(position.get("quantity", 0)) == 0:
                continue
            row = all_rows.get(symbol, {})
            mark = _decimal(row.get("price"))
            if mark is None or mark <= 0 or "quote_timestamp_unusable" in row.get("evidence_gaps", []):
                missing_marks.append(symbol)
            else:
                nav += int(position.get("quantity", 0)) * mark
        if missing_marks:
            nav_confirmed = False
            result["nav_unpriced_symbols"] = missing_marks
            result["reasons"].append("existing_holding_market_price_required_for_nav")
        else:
            capital = nav
    if nav_confirmed:
        result.update(capital_basis="confirmed_cash_plus_marked_holdings", current_nav_cny=str(capital))
    else:
        result["capital_basis"] = "planned_funding"
        result["inventory_basis"] = "hypothetical_zero_inventory_for_target_sizing"
        # Unknown inventory cannot generate real rebalance sells or funding proceeds.
        current = {}
    result["valuation_basis"] = "market_quotes" if nav_confirmed else "conditional_buy_limits"
    if capital is None or capital <= 0:
        return fail("positive_etf_capital_required")
    rows = [row for row in all_rows.values()
            if row.get("eligible") is True and not etf_buy_excluded(row)
            and not etf_region_excluded(row, candidates.get("policy"))]
    if weights is None:
        # Fill capacity across verified groups; scoring orders rows, not agent conviction.
        reference = _decimal(candidates.get("policy", {}).get("etf_target_weight",
                             min(max(Decimal("0.95"), min_weight), max_weight)))
        if reference is None or not min_weight <= reference <= max_weight:
            return fail("invalid_etf_target_weight")
        weights, groups, remaining = {}, {}, reference
        for row in rows:
            group = row.get("concentration_group", row["asset_group"])
            weight = min(Decimal("0.35"), Decimal("0.60") - groups.get(group, Decimal(0)), remaining)
            if weight > 0:
                weights[row["symbol"]] = weight
                groups[group] = groups.get(group, Decimal(0)) + weight
                remaining -= weight
            if remaining == 0:
                break
    parsed = {symbol: _decimal(value) for symbol, value in weights.items()}
    eligible = {row["symbol"] for row in rows}
    # A prohibited existing holding may be marked and reduced, never bought back.
    eligible.update(symbol for symbol in current if symbol in all_rows
                    and etf_buy_excluded(all_rows[symbol]))
    if (not parsed or any(value is None or value < 0 for value in parsed.values())
            or any(symbol not in eligible for symbol, value in parsed.items() if value != 0)):
        return fail("no_eligible_weighted_candidates")
    exposure = sum(parsed.values(), Decimal(0))
    if not min_weight <= exposure <= max_weight:
        return fail("investment_target_must_be_90_to_98pct" if (min_weight, max_weight) ==
                    (Decimal("0.90"), Decimal("0.98")) else "investment_target_outside_configured_etf_range")
    groups, indices = {}, set()
    for symbol, weight in parsed.items():
        if weight <= 0:
            continue
        row = all_rows[symbol]
        if weight > Decimal("0.35"):
            return fail("single_etf_weight_above_35pct")
        if row["index_id"] in indices:
            return fail("duplicate_tracking_index")
        indices.add(row["index_id"])
        group = row.get("concentration_group", row["asset_group"])
        groups[group] = groups.get(group, Decimal(0)) + weight
        if groups[group] > Decimal("0.60"):
            return fail("concentration_group_weight_above_60pct")
    diversification_error = etf_diversification_error(candidates, parsed)
    if diversification_error:
        return fail(diversification_error)
    fee = _decimal(account.get("commission_fixed_per_order", "1"))
    if fee is None or fee < 0:
        return fail("invalid_commission")
    result["cost_assumptions"] = {"commission_fixed_per_order": str(fee),
                                  "confirmed": "commission_fixed_per_order" in account,
                                  "slippage_included": False}
    targets = dict(parsed)
    targets.update({symbol: Decimal(0) for symbol in current if symbol not in targets})
    invested = buy_cash = sell_cash = fees = Decimal(0)
    conditions = set()
    if not nav_confirmed:
        conditions.add("confirm_funding_cash_positions_and_recalculate_rebalance_before_execution")
    if result.get("nav_unpriced_symbols"):
        conditions.add("refresh_existing_holding_prices_and_recalculate_nav_before_execution")
    for symbol, weight in targets.items():
        row = all_rows.get(symbol, {})
        price = _decimal(row.get("price"))
        lot = int(row.get("buy_lot_size", 100))
        if price is None or price <= 0 or lot <= 0 or row.get("trading_status") != "tradable":
            return fail("rebalance_price_or_tradability_unverified")
        if "quote_timestamp_unusable" in row.get("evidence_gaps", []):
            return fail("rebalance_quote_timestamp_unusable")
        held = int(current.get(symbol, {}).get("quantity", 0))
        available = int(current.get(symbol, {}).get("available_quantity", 0))
        if held < 0 or not 0 <= available <= held:
            return fail("invalid_existing_inventory")
        quantity = int((capital * weight) // (price * lot)) * lot
        buy_price = _decimal(row.get("buy_limit_price", price))
        sell_price = _decimal(row.get("sell_limit_price", price))
        tick = _decimal(row.get("price_tick", "0.001"))
        if (buy_price is None or buy_price <= 0 or sell_price is None or sell_price <= 0
                or tick is None or tick <= 0 or buy_price % tick or sell_price % tick):
            return fail("invalid_separate_buy_or_sell_limit")
        if not nav_confirmed:
            # A budget-only plan assumes all target units fill at its conditional limit.
            quantity = int((capital * weight) // (buy_price * lot)) * lot
        elif quantity > held:
            sizing_price = max(price, buy_price)
            additional = max(Decimal(0), capital * weight - held * sizing_price)
            quantity = held + int(additional // (sizing_price * lot)) * lot
        delta = quantity - held
        if delta > 0 and etf_buy_excluded(row):
            return fail("bond_or_money_buy_excluded_by_user")
        if delta < 0 and -delta > available:
            return fail("rebalance_exceeds_sellable_inventory")
        valuation_price = price if nav_confirmed else buy_price
        notional = valuation_price * quantity
        invested += notional
        if delta:
            fees += fee
        if delta > 0:
            buy_cash += delta * buy_price + fee
        elif delta < 0:
            sell_cash += -delta * min(price, sell_price) - fee
        conditions.update(row.get("execution_conditions", []))
        result["positions"].append({"symbol": symbol, "name": row.get("name"), "index_id": row.get("index_id"),
                                    "asset_group": row.get("asset_group"), "target_weight": str(weight),
                                    "price": str(price), "limit_price": str(buy_price if delta > 0 else sell_price),
                                    "valuation_price": str(valuation_price),
                                    "side": "buy" if delta > 0 else "sell" if delta < 0 else "hold",
                                    "target_quantity": quantity, "current_quantity": held,
                                    "delta_quantity": delta, "target_notional_cny": str(notional)})
    actual_exposure = invested / capital
    if not min_weight <= actual_exposure <= max_weight or invested + fees > capital:
        return fail("lot_or_fee_constraints_prevent_valid_exposure")
    actual_weights = {row["symbol"]: Decimal(row["target_notional_cny"]) / capital
                      for row in result["positions"]}
    diversification_error = etf_diversification_error(candidates, actual_weights)
    if diversification_error:
        return fail(diversification_error)
    if nav_confirmed and buy_cash > cash + sell_cash:
        return fail("confirmed_cash_insufficient_even_after_planned_sales")
    contingent = nav_confirmed and buy_cash > cash
    if contingent:
        conditions.add("sell_orders_must_fill_before_cash_dependent_buys")
    result.update(status="planned", capital_cny=str(capital), target_investment_ratio=str(exposure),
                  investment_ratio=str(actual_exposure), invested_cny=str(invested), estimated_fees_cny=str(fees),
                  cash_reserve_cny=str((cash if nav_confirmed else capital) + sell_cash - buy_cash),
                  required_buy_cash_cny=str(buy_cash),
                  expected_sell_proceeds_cny=str(sell_cash), cash_contingent_on_sales=contingent,
                  execution_conditions=sorted(conditions))
    if not result["funding_confirmed"]:
        result["reasons"].append("planned_funding_not_deposited")
    if not nav_confirmed:
        observed = {row["symbol"]: row for row in account.get("positions", [])}
        for row in result["positions"]:
            row["current_quantity"] = (int(observed.get(row["symbol"], {}).get("quantity", 0))
                                       if account.get("positions_confirmed") is True else None)
            row["delta_quantity"] = None
            row["side"] = "unknown"
    return result


def build_t_plan(candidate: dict, position: dict | None, account: dict, *, buy_price=None, sell_price=None,
                 direction: str = "sell_then_buy", quantity: int | None = None,
                 core_quantity: int | None = None, slippage_bps="10") -> dict:
    """Conditional round-trip mechanics; both legs require separate actual fills."""
    result = {"module": MODULE, "symbol": candidate.get("symbol"), "status": "conditional",
              "execution_authorized": False, "legs": [], "reasons": []}
    if etf_buy_excluded(candidate):
        result["reasons"].append("bond_or_money_buy_excluded_by_user")
        return result
    if (not position or account.get("positions_confirmed") is not True
            or account.get("funding_confirmed") is not True):
        result["reasons"].append("confirmed_position_and_funding_required")
        return result
    if position.get("symbol") != candidate.get("symbol") or candidate.get("eligible") is not True:
        result["reasons"].append("eligible_matching_etf_required")
        return result
    t0 = candidate.get("same_day_turnaround")
    if type(t0) is not bool or not candidate.get("exchange_reference", {}).get("source_url"):
        result["reasons"].append("exchange_turnaround_unverified")
        return result
    buy, sell = _decimal(buy_price), _decimal(sell_price)
    fee = _decimal(account.get("commission_fixed_per_order"))
    slip = _decimal(slippage_bps)
    cash = _decimal(account.get("cash_available_cny", account.get("cash")))
    tick = _decimal(candidate.get("price_tick", "0.001"))
    if (buy is None or sell is None or buy <= 0 or sell <= buy or fee is None or fee < 0
            or slip is None or slip < 0 or cash is None or cash < 0 or tick is None or tick <= 0
            or buy % tick or sell % tick):
        result["reasons"].append("valid_prices_cash_and_costs_required")
        return result
    held = int(position.get("quantity", 0))
    available = int(position.get("available_quantity", 0))
    lot = int(candidate.get("buy_lot_size", 100))
    core = core_quantity if core_quantity is not None else (held * 8 + 9) // 10
    if lot <= 0 or held <= 0 or not 0 <= available <= held or not 0 < core <= held:
        result["reasons"].append("invalid_inventory_or_core")
        return result
    tactical = (held - core) // lot * lot
    amount = tactical if quantity is None else quantity
    if not isinstance(amount, int) or amount <= 0 or amount % lot or amount > tactical:
        result["reasons"].append("tactical_quantity_exceeds_non_core_lots")
        return result
    if direction not in {"sell_then_buy", "buy_then_sell"}:
        result["reasons"].append("invalid_direction")
        return result
    if (direction == "sell_then_buy" or not t0) and amount > available:
        result["reasons"].append("sellable_inventory_insufficient_t1_new_buys_locked")
        return result
    buy_cost = buy * amount * (1 + slip / 10000) + fee
    sell_proceeds = sell * amount * (1 - slip / 10000) - fee
    if sell_proceeds - buy_cost <= 0:
        result["reasons"].append("spread_does_not_cover_round_trip_costs")
        return result
    if direction == "buy_then_sell" and buy_cost > cash:
        result["reasons"].append("cash_insufficient_for_first_leg")
        return result
    if direction == "sell_then_buy" and cash + sell_proceeds < buy_cost:
        result["reasons"].append("cash_insufficient_for_buyback")
        return result
    buy_leg = {"side": "buy", "quantity": amount, "limit_price": str(buy)}
    sell_leg = {"side": "sell", "quantity": amount, "limit_price": str(sell),
                "inventory_basis": "confirmed_existing_inventory" if not t0 or direction == "sell_then_buy" else "t0_after_buy_fill"}
    result.update(status="conditional_ready", direction=direction, core_quantity=core, quantity=amount,
                  same_day_turnaround=t0, estimated_net_edge_cny=str(sell_proceeds - buy_cost),
                  legs=[sell_leg, buy_leg] if direction == "sell_then_buy" else [buy_leg, sell_leg])
    result["reasons"].extend(["each_leg_requires_price_trigger_and_fill_confirmation", "round_trip_completion_not_guaranteed"])
    result["reasons"].extend(candidate.get("execution_conditions", []))
    return result
