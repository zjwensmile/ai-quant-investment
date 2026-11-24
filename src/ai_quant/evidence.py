from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


EVIDENCE_QUALITIES = {
    "official_primary",
    "company_primary",
    "major_media",
    "secondary",
    "community",
    "unknown",
}


def filter_future_agent_evidence(
    output: dict[str, Any], as_of_time: str
) -> tuple[dict[str, Any], int]:
    """Remove decisions/news that rely on information unavailable at the cutoff.

    A research decision, discovery concept, or scout candidate is discarded as a
    unit when any of its evidence is from after the snapshot. Retaining it after
    merely removing the citation could still let a future fact influence portfolio
    construction. News items are independent, so only the affected item is removed.
    """
    cutoff = datetime.fromisoformat(as_of_time)
    if cutoff.tzinfo is None:
        raise ValueError("Snapshot as_of_time must include a timezone")

    filtered = deepcopy(output)
    dropped = _filter_future_evidence_container(
        filtered, "decisions", "rejected_candidates", cutoff
    )
    dropped += _filter_future_evidence_container(
        filtered, "concepts", None, cutoff
    )
    dropped += _filter_future_evidence_container(
        filtered, "candidates", "rejected_ideas", cutoff
    )

    if isinstance(filtered.get("core_news"), list):
        kept_news = []
        rejected_noise = filtered.get("rejected_noise")
        for item in filtered["core_news"]:
            if not _item_is_future(item, cutoff):
                kept_news.append(item)
                continue
            dropped += 1
            if isinstance(rejected_noise, list):
                rejected_noise.append(
                    f"{item.get('title') or item.get('news_id', 'unknown')}: "
                    "timestamp is after snapshot as_of_time"
                )
        filtered["core_news"] = kept_news

    if dropped:
        quality_key = (
            "overall_data_quality" if "overall_data_quality" in filtered else "data_quality"
        )
        if quality_key in filtered:
            filtered[quality_key] = "insufficient_evidence"
        summary_key = "overall_summary" if "overall_summary" in filtered else "summary"
        if summary_key in filtered:
            suffix = (
                f" Safety filter removed {dropped} evidence/news item(s) dated after "
                "snapshot.as_of_time; affected conclusions were not used."
            )
            filtered[summary_key] = str(filtered.get(summary_key) or "").rstrip() + suffix
    return filtered, dropped


def _filter_future_evidence_container(
    output: dict[str, Any], collection_key: str, rejection_key: str | None,
    cutoff: datetime,
) -> int:
    """Discard one discovery/research item when it relies on future evidence.

    Concepts and scout candidates are just as capable of leaking future knowledge as
    a conventional research decision.  Their output shape uses different labels,
    so they need the same point-in-time filter before being persisted or routed.
    """
    items = output.get(collection_key)
    if not isinstance(items, list):
        return 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    rejected = output.get(rejection_key) if rejection_key else None
    for item in items:
        future_evidence = [
            evidence
            for key in ("supporting_evidence", "opposing_evidence")
            for evidence in item.get(key, [])
            if _item_is_future(evidence, cutoff)
        ]
        if not future_evidence:
            kept.append(item)
            continue
        dropped += len(future_evidence)
        if isinstance(rejected, list):
            rejected.append(
                f"{_item_label(item)}: evidence timestamp is after snapshot as_of_time"
            )
    output[collection_key] = kept
    return dropped


def _item_label(item: dict[str, Any]) -> str:
    return str(
        item.get("symbol")
        or item.get("name")
        or item.get("subject")
        or item.get("concept_id")
        or item.get("decision_id")
        or "unknown"
    )


def normalize_duplicate_evidence_ids(
    output: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Make repeated model-generated evidence IDs unique without dropping evidence.

    Agents sometimes reuse a short ID when the same source supports more than one
    decision.  The evidence itself remains auditable, so a deterministic suffix is
    safer than failing the entire multi-Agent run.
    """
    normalized = deepcopy(output)
    used: set[str] = set()
    renamed = 0
    containers = [
        *normalized.get("decisions", []),
        *normalized.get("concepts", []),
        *normalized.get("candidates", []),
    ]
    for decision in containers:
        for key in ("supporting_evidence", "opposing_evidence"):
            for item in decision.get(key, []):
                base = str(item.get("evidence_id") or "").strip()
                if not base or base not in used:
                    if base:
                        used.add(base)
                    continue
                suffix = 2
                candidate = f"{base}-{suffix}"
                while candidate in used:
                    suffix += 1
                    candidate = f"{base}-{suffix}"
                item["evidence_id"] = candidate
                used.add(candidate)
                renamed += 1
    return normalized, renamed


def validate_agent_evidence(output: dict[str, Any], as_of_time: str) -> None:
    if output.get("as_of_time") != as_of_time:
        raise ValueError("Agent as_of_time must exactly match the market snapshot")
    cutoff = datetime.fromisoformat(as_of_time)
    if cutoff.tzinfo is None:
        raise ValueError("Snapshot as_of_time must include a timezone")
    evidence_ids: set[str] = set()
    containers = [
        *output.get("decisions", []),
        *output.get("concepts", []),
        *output.get("candidates", []),
    ]
    for decision in containers:
        for key in ("supporting_evidence", "opposing_evidence"):
            for item in decision.get(key, []):
                evidence_id = str(item["evidence_id"])
                if evidence_id in evidence_ids:
                    raise ValueError("Evidence IDs must be unique per Agent output")
                evidence_ids.add(evidence_id)
                _validate_url(str(item["source"]))
                if item.get("quality") not in EVIDENCE_QUALITIES:
                    raise ValueError(f"Unknown evidence quality: {item.get('quality')}")
                _not_future(item.get("published_at"), cutoff, "published_at")
                _not_future(item.get("available_at"), cutoff, "available_at")
    news_ids: set[str] = set()
    for item in output.get("core_news", []):
        news_id = str(item["news_id"])
        if news_id in news_ids:
            raise ValueError("News IDs must be unique per Agent output")
        news_ids.add(news_id)
        _validate_url(str(item["url"]))
        _not_future(item.get("published_at"), cutoff, "published_at")
        _not_future(item.get("available_at"), cutoff, "available_at")


def _validate_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Evidence source must be an exact HTTP(S) URL: {value}")


def _not_future(value: object, cutoff: datetime, field: str) -> None:
    if _is_future(value, cutoff):
        raise ValueError(f"Evidence {field} is after snapshot as_of_time")


def _item_is_future(item: dict[str, Any], cutoff: datetime) -> bool:
    return any(
        _is_future(item.get(field), cutoff)
        for field in ("published_at", "available_at")
    )


def _is_future(value: object, cutoff: datetime) -> bool:
    if value is None:
        return False
    observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if observed.tzinfo is None:
        return observed.date() > cutoff.date()
    return observed.astimezone(cutoff.tzinfo) > cutoff
