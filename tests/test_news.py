import json
from datetime import date
from pathlib import Path

import pytest

from predictor.news import (build_news_digest, build_search_queries, calendar_window,
                            canonical_url, deduplicate, import_news, normalize_item,
                            refresh_news)


def record(**changes):
    value = {
        "id": "a", "ingredient_ids": ["urolithin-a"], "ingredient_keywords": ["urolithin a"],
        "title": "A checked source", "url": "https://example.com/a", "source": "Publisher",
        "published_at": "2026-07-01", "summary": "A factual summary", "region": "US",
        "category": "Company announcement", "brands": ["Timeline"], "evidence": "verified_primary",
        "source_kind": "brand_announcement", "retrieved_at": "2026-09-23",
        "relevance_note": "Direct ingredient mention", "date_evidence": "Date on article",
        "region_evidence": "Explicit US launch", "verification_note": "Opened original article",
    }
    return {**value, **changes}


def write_records(path, records):
    path.write_text(json.dumps({"items": records}), encoding="utf-8")
    return path


def test_calendar_months_not_ninety_days_and_month_ends():
    assert calendar_window("2026-09-23") == (date(2026, 6, 23), date(2026, 9, 23))
    assert (calendar_window("2026-09-23")[1] - calendar_window("2026-09-23")[0]).days == 92
    assert calendar_window("2024-05-31")[0] == date(2024, 2, 29)
    assert calendar_window("2025-05-31")[0] == date(2025, 2, 28)


def test_window_boundary_future_and_background_are_separate(tmp_path):
    records = [record(id=str(i), url=f"https://example.com/{i}", published_at=value)
               for i, value in enumerate(["2026-06-23", "2026-09-23", "2026-06-22", None, "2026-09-24"])]
    result = build_news_digest(write_records(tmp_path / "news.json", records), "2026-09-23")
    assert {item["id"] for item in result["items"]} == {"0", "1"}
    assert {item["background_reason"] for item in result["background"]} == {"undated", "outside_window"}
    assert result["excluded"] == [{"id": "4", "reason": "future_publication"}]
    assert not result["model_feature"]


def test_region_and_provenance_filters(tmp_path):
    records = [record(id=region, region=region, url=f"https://example.com/{region}")
               for region in ["US", "CA", "MX", "GB", "unknown"]]
    records += [record(id="unverified", evidence="unverified", url="https://example.com/u")]
    result = build_news_digest(write_records(tmp_path / "news.json", records), "2026-09-23")
    assert {item["region"] for item in result["items"]} == {"US", "CA", "MX"}
    assert len(result["excluded"]) == 3
    with pytest.raises(ValueError, match="region_evidence"):
        normalize_item(record(region_evidence=""))


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///x", "https://u:p@example.com", "data:text/html,x", "https://example.com/\n"])
def test_non_web_urls_and_control_chars_rejected(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_malformed_region_and_related_links_cannot_bypass_validation(tmp_path):
    rows = [record(region=["US"]), record(related_sources=[{"url": "javascript:alert(1)"}])]
    result = build_news_digest(write_records(tmp_path / "news.json", rows), "2026-09-23")
    assert not result["items"]
    assert len(result["excluded"]) == 2


def test_tracking_duplicate_and_event_dedup_preserve_related_sources():
    first = normalize_item(record(url="https://example.com/a?utm_source=test#top", event_id="launch"))
    article = normalize_item(record(id="b", url="https://publisher.com/story", event_id="launch", evidence="verified_secondary", source_kind="independent_media"))
    copy = normalize_item(record(id="c", url="https://example.com/a"))
    values = deduplicate([copy, first, article])
    assert len(values) == 1
    assert values[0]["id"] == "b"
    assert len(values[0]["related_sources"]) == 1
    assert canonical_url("https://EXAMPLE.com/a?utm_source=x&id=3&fbclid=y") == "https://example.com/a?id=3"


def test_explicit_catalog_matching_does_not_match_brands_alone(tmp_path):
    catalog = [{"id": "ua", "keyword": "urolithin a", "brands": ["Timeline"]},
               {"id": "other", "keyword": "other ingredient", "brands": ["Timeline"]}]
    result = build_news_digest(write_records(tmp_path / "news.json", [record()]), "2026-09-23", catalog)
    assert result["items"][0]["ingredient_ids"] == ["ua"]


def test_import_refresh_is_idempotent_and_invalid_import_does_not_clobber(tmp_path):
    incoming = write_records(tmp_path / "incoming.json", [record()])
    store = tmp_path / "store.json"
    import_news(incoming, store, as_of="2026-09-23")
    import_news(incoming, store, as_of="2026-09-23")
    assert len(json.loads(store.read_text())["items"]) == 1
    before = store.read_bytes()
    write_records(incoming, [record(published_at="July 1")])
    with pytest.raises(ValueError):
        import_news(incoming, store, as_of="2026-09-23")
    assert store.read_bytes() == before
    output = tmp_path / "digest.json"
    result = refresh_news(store, output, "2027-09-23")
    assert result["items"] == []
    assert result["coverage"]["status"] == "no_verified_news_in_window"
    assert len(result["background"]) == 1


def test_generic_queries_cover_each_country_brand_and_optional_language():
    rows = [{"id": "new", "keyword": "new ingredient", "brands": ["Brand X", "Brand X"]}]
    queries = build_search_queries(rows, "2026-09-23", languages=("en", "es"))
    assert len(queries) == 12
    assert {q["region"] for q in queries} == {"US", "CA", "MX"}
    assert all("after:2026-06-22 before:2026-09-24" in q["query"] for q in queries)
    assert any('"new ingredient" "Brand X"' in q["query"] for q in queries)


def test_real_seed_has_verifiable_current_timeline_coverage_and_old_background():
    path = Path(__file__).resolve().parents[1] / "data" / "news" / "verified_seed.json"
    result = build_news_digest(path, "2026-09-23")
    assert len(result["items"]) == 7
    assert len(result["background"]) == 1
    assert not result["excluded"]
    timeline = [item for item in result["items"] if "urolithin-a" in item["ingredient_ids"]]
    assert len(timeline) == 4
    assert {item["source_kind"] for item in timeline} == {"independent_media", "brand_announcement", "brand_education"}
    assert all(item["date_evidence"] and item["region_evidence"] for item in result["items"])
    assert all(item["model_feature"] is False for item in result["items"])
