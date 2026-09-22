"""Verified, offline-first news context. Nothing here is a model feature.

Search results are candidates, never evidence. A researcher must open a source,
check its own publication date and North American relevance, and import JSON.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REGIONS = {"US": "United States", "CA": "Canada", "MX": "Mexico"}
EVIDENCE = {"verified_primary", "verified_secondary", "unverified"}
SOURCE_KINDS = {"brand_announcement", "brand_education", "independent_media", "research", "official"}
DISCLAIMER = "站外新闻仅供当前选品背景参考，不进入历史模型、回测或预测权重；来源核验不等于为来源中的宣传或功效主张背书。"


def _date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected ISO calendar date YYYY-MM-DD")
    return date.fromisoformat(value)


def calendar_window(as_of: str | date, months: int = 3) -> tuple[date, date]:
    """Inclusive trailing calendar-month window, clipping month-end dates."""
    if not isinstance(months, int) or months < 1:
        raise ValueError("months must be a positive integer")
    end = _date(as_of)
    year, month0 = divmod(end.year * 12 + end.month - 1 - months, 12)
    start = date(year, month0 + 1, min(end.day, calendar.monthrange(year, month0 + 1)[1]))
    return start, end


def canonical_url(value: str) -> str:
    """Allow web links only; retain semantic query parameters, discard tracking."""
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid news URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("News URL must be an http(s) URL without credentials")
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return list(dict.fromkeys(v.strip() for v in value))


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate imported provenance without guessing dates or ingredients."""
    item = dict(raw)
    for field in ("title", "source", "summary", "category", "relevance_note"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"Missing {field}")
        item[field] = item[field].strip()
    item["url"] = canonical_url(item.get("url"))
    item["ingredient_ids"] = _strings(item.get("ingredient_ids", []), "ingredient_ids")
    item["ingredient_keywords"] = _strings(item.get("ingredient_keywords", []), "ingredient_keywords")
    if not item["ingredient_ids"] and not item["ingredient_keywords"]:
        raise ValueError("An explicit ingredient association is required")
    item["brands"] = _strings(item.get("brands", []), "brands")
    item["region"] = item.get("region", "unknown")
    if not isinstance(item["region"], str):
        raise ValueError("region must be an explicit country code")
    item["evidence"] = item.get("evidence", "unverified")
    if item["evidence"] not in EVIDENCE:
        raise ValueError("Unknown evidence status")
    item["source_kind"] = item.get("source_kind", "independent_media" if item["evidence"] == "verified_secondary" else "brand_announcement")
    if item["source_kind"] not in SOURCE_KINDS:
        raise ValueError("Unknown source_kind")
    item["published_at"] = _date(item["published_at"]).isoformat() if item.get("published_at") else None
    if item.get("event_at"):
        item["event_at"] = _date(item["event_at"]).isoformat()
    retrieved = item.get("retrieved_at")
    if not isinstance(retrieved, str):
        raise ValueError("retrieved_at is required")
    try:
        datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid retrieved_at") from exc
    if item["evidence"].startswith("verified"):
        for field in ("date_evidence", "region_evidence", "verification_note"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"Verified items require {field}")
    item.setdefault("id", "news-" + hashlib.sha256(item["url"].encode()).hexdigest()[:16])
    if not isinstance(item["id"], str) or not item["id"].strip():
        raise ValueError("id must be a non-empty string")
    if item.get("event_id") is not None and not isinstance(item["event_id"], str):
        raise ValueError("event_id must be a string")
    related = item.get("related_sources", [])
    if not isinstance(related, list):
        raise ValueError("related_sources must be a list")
    item["related_sources"] = []
    for source in related:
        if not isinstance(source, dict):
            raise ValueError("Invalid related source")
        cleaned = dict(source)
        cleaned["url"] = canonical_url(cleaned.get("url"))
        if cleaned.get("published_at"):
            cleaned["published_at"] = _date(cleaned["published_at"]).isoformat()
        item["related_sources"].append(cleaned)
    item["model_feature"] = False
    return item


def _load(path: str | Path) -> tuple[list[dict], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return payload, {}
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"], {k: v for k, v in payload.items() if k != "items"}
    raise ValueError("News import must be a list or an object with items")


def _catalog(path_or_records: str | Path | list[dict] | None) -> list[dict]:
    if path_or_records is None:
        return []
    if isinstance(path_or_records, list):
        return path_or_records
    records = json.loads(Path(path_or_records).read_text(encoding="utf-8-sig"))
    if not isinstance(records, list):
        raise ValueError("Catalog must be a list")
    return records


def _associate(item: dict, catalog: list[dict]) -> dict:
    if not catalog:
        return item
    known = {row["id"] for row in catalog}
    ids = [value for value in item["ingredient_ids"] if value in known]
    terms = {v.casefold().strip() for v in item["ingredient_keywords"]}
    # Deliberate explicit keyword matching: a broad brand does not establish an
    # ingredient relationship. The importer must record that relationship.
    for row in catalog:
        names = [row.get("keyword", ""), row.get("name", ""), *row.get("aliases", [])]
        if terms.intersection(v.casefold().strip() for v in names if isinstance(v, str) and v.strip()):
            ids.append(row["id"])
    return {**item, "ingredient_ids": list(dict.fromkeys(ids))}


def deduplicate(items: Iterable[dict]) -> list[dict]:
    """Coalesce exact URLs and manually verified event IDs, never fuzzy topics.

    An independent article is the lead over brand marketing of the same event;
    otherwise retain the earliest dated verified item. Preserve other sources.
    """
    def quality(item: dict) -> tuple:
        return (item["evidence"] == "unverified", item["source_kind"] != "independent_media",
                item.get("published_at") or "9999", item["url"])

    groups: list[list[dict]] = []
    for item in sorted(items, key=quality):
        matched = [group for group in groups if any(
            item["url"] == other["url"] or (item.get("event_id") and item.get("event_id") == other.get("event_id"))
            for other in group)]
        if matched:
            merged = [item, *(entry for group in matched for entry in group)]
            groups = [group for group in groups if group not in matched]
            groups.append(sorted(merged, key=quality))
        else:
            groups.append([item])
    result = []
    for group in groups:
        lead = dict(group[0])
        lead["ingredient_ids"] = list(dict.fromkeys(v for item in group for v in item["ingredient_ids"]))
        lead["brands"] = list(dict.fromkeys(v for item in group for v in item["brands"]))
        related = list(lead.get("related_sources", []))
        for item in group[1:]:
            related.append({key: item.get(key) for key in ("title", "url", "source", "published_at", "evidence", "source_kind")})
        lead["related_sources"] = list({entry["url"]: entry for entry in related if entry.get("url") != lead["url"]}.values())
        result.append(lead)
    return sorted(result, key=lambda item: (item.get("published_at") or "", item["id"]), reverse=True)


def build_news_digest(seed_path: str | Path, as_of: str | date, catalog_path: str | Path | list[dict] | None = None,
                      *, regions: Iterable[str] = ("US", "CA", "MX"), months: int = 3) -> dict:
    """Refresh the display window from saved, verified source records, offline."""
    start, end = calendar_window(as_of, months)
    allowed = set(regions)
    if not allowed or not allowed.issubset(REGIONS):
        raise ValueError("regions must contain US, CA and/or MX")
    raw_items, metadata = _load(seed_path)
    catalog = _catalog(catalog_path)
    current, background, excluded = [], [], []
    for raw in raw_items:
        try:
            item = _associate(normalize_item(raw), catalog)
        except (ValueError, TypeError, AttributeError) as exc:
            excluded.append({"id": raw.get("id") if isinstance(raw, dict) else None, "reason": str(exc)})
            continue
        reason = None
        if item["region"] not in allowed:
            reason = "outside_region_or_region_unverified"
        elif catalog and not item["ingredient_ids"]:
            reason = "ingredient_not_in_catalog"
        elif item["evidence"] == "unverified":
            reason = "source_unverified"
        elif item["published_at"] and _date(item["published_at"]) > end:
            reason = "future_publication"
        if reason:
            excluded.append({"id": item["id"], "reason": reason})
        elif not item["published_at"] or _date(item["published_at"]) < start:
            background.append({**item, "background_reason": "undated" if not item["published_at"] else "outside_window"})
        else:
            current.append(item)
    items = deduplicate(current)
    return {
        "schema_version": "1.0", "as_of": end.isoformat(), "window_start": start.isoformat(),
        "window_end": end.isoformat(), "window_months": months, "regions": sorted(allowed),
        "items": items, "background": deduplicate(background), "excluded": excluded,
        "disclaimer": DISCLAIMER, "model_feature": False,
        "coverage": {"status": "verified_items_available" if items else "no_verified_news_in_window",
                     "items": len(items), "raw_records": len(raw_items), "duplicates_removed": len(current) - len(items),
                     "ingredient_ids": sorted({i for item in items for i in item["ingredient_ids"]}),
                     "search_as_of": metadata.get("searched_at"),
                     "note": metadata.get("coverage_note", "未收录不等于没有新闻；离线刷新不会联网检索新文章。")},
    }


def import_news(import_path: str | Path, store_path: str | Path, *, as_of: str | date,
                catalog_path: str | Path | list[dict] | None = None) -> dict:
    """Validate then merge research results. Invalid imports never replace store."""
    _date(as_of)
    catalog = _catalog(catalog_path)
    incoming, incoming_meta = _load(import_path)
    validated = [normalize_item(item) for item in incoming]
    destination = Path(store_path)
    existing, old_meta = _load(destination) if destination.exists() else ([], {})
    by_url = {normalize_item(item)["url"]: normalize_item(item) for item in existing}
    by_url.update({item["url"]: item for item in validated})
    payload = {**old_meta, **incoming_meta, "schema_version": "1.0", "items": list(by_url.values())}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return build_news_digest(destination, as_of, catalog)


def build_search_queries(catalog_path: str | Path | list[dict], as_of: str | date,
                         *, regions: Iterable[str] = ("US", "CA", "MX"), languages: Iterable[str] = ("en",)) -> list[dict]:
    """Build a Codex web-search worklist; dates must still be checked on pages."""
    start, end = calendar_window(as_of)
    regions, languages = tuple(regions), tuple(languages)
    if not set(regions).issubset(REGIONS) or not regions or not languages or not set(languages).issubset({"en", "fr", "es"}):
        raise ValueError("Use US/CA/MX regions and en/fr/es languages")
    news_words = {"en": "news launch research announcement", "fr": "actualités lancement recherche communiqué", "es": "noticias lanzamiento investigación comunicado"}
    queries = []
    for row in _catalog(catalog_path):
        keyword = str(row.get("keyword") or row.get("name") or "").strip().replace('"', "")
        if not keyword:
            continue
        brands = list(dict.fromkeys(str(b).strip().replace('"', "") for b in row.get("brands", []) if str(b).strip()))
        for region in regions:
            for language in languages:
                for brand in [None, *brands]:
                    terms = f'"{keyword}"' + (f' "{brand}"' if brand else "")
                    queries.append({"ingredient_id": row["id"], "keyword": keyword, "brand": brand, "region": region,
                                    "language": language, "window_start": start.isoformat(), "window_end": end.isoformat(),
                                    "query": f'{terms} {REGIONS[region]} {news_words[language]} after:{(start - timedelta(days=1)).isoformat()} before:{(end + timedelta(days=1)).isoformat()}'})
    return queries


def refresh_news(seed_path: str | Path, output_path: str | Path, as_of: str | date,
                 catalog_path: str | Path | list[dict] | None = None) -> dict:
    digest = build_news_digest(seed_path, as_of, catalog_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(digest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return digest
