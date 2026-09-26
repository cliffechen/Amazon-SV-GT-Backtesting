"""No paid calls: exercise boundaries against real ingestion and SQLite ledger."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from predictor.budget import Budget, BudgetError
from predictor.catalog import build_catalog
from predictor.collection import provider_cache_path
from predictor.data import assemble_panel, collection_plan


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def budget(tmp_path, limit=3, reserve=0, available=100):
    config = tmp_path / "budget.json"
    save(config, {"period": "2026-09", "project_limit": limit,
                  "screenshot_available": available, "account_reserve": reserve, "per_minute": 100})
    return Budget(config, tmp_path / "ledger.sqlite")


NOW = datetime(2026, 9, 23, 12).timestamp()


def test_budget_concurrent_workers_cannot_exceed_account_or_project_limit(tmp_path):
    ledger = budget(tmp_path, limit=7, reserve=98)

    def attempt(index):
        try:
            return ledger.reserve("aba_research_trend", {"keyword": str(index)}, now=NOW)
        except BudgetError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(x is not None for x in results) == 2
    assert ledger.status()["reserved_units"] == 2
    assert ledger.status()["remaining_project"] == 0


def test_failed_calls_still_count_and_completion_cannot_be_reused(tmp_path):
    ledger = budget(tmp_path, limit=1)
    reservation = ledger.reserve("google_trend", {"keyword": "a"}, now=NOW)
    ledger.complete(reservation["reservation_id"], None, "failed")
    with pytest.raises(BudgetError):
        ledger.complete(reservation["reservation_id"], "pretend-cache.json")
    with pytest.raises(BudgetError):
        ledger.reserve("google_trend", {"keyword": "b"}, now=NOW)
    assert ledger.status()["remaining_project"] == 0


def test_budget_requires_new_month_balance_and_rejects_wrong_provider(tmp_path):
    ledger = budget(tmp_path)
    with pytest.raises(BudgetError, match="period"):
        ledger.reserve("google_trend", {}, now=datetime(2026, 10, 1).timestamp())
    with pytest.raises(BudgetError, match="Sorftime"):
        ledger.reserve("tiktok", {}, provider="sorftime", now=NOW)
    assert ledger.status()["charged_attempts"] == 0


def test_legacy_ledger_migration_preserves_already_charged_units(tmp_path):
    with sqlite3.connect(tmp_path / "ledger.sqlite") as db:
        db.execute("CREATE TABLE reservations (id TEXT PRIMARY KEY, period TEXT, created REAL, provider TEXT, tool TEXT, request TEXT, units INTEGER, status TEXT, cache_path TEXT)")
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?)",
                   ("old", "2026-09", NOW - 90, "sellersprite", "google_trend", "{}", 2, "failed", None))
    ledger = budget(tmp_path, limit=3)
    assert ledger.status()["reserved_units"] == 2
    assert ledger.status()["remaining_project"] == 1
    reservation = ledger.reserve("google_trend", {"keyword": "new"}, cache_key="new-job", now=NOW)
    assert reservation["remaining_project"] == 0


def test_same_job_concurrent_reservations_allow_only_one_paid_attempt(tmp_path):
    ledger = budget(tmp_path, limit=20)

    def attempt(_):
        try:
            return ledger.reserve("google_trend", {"keyword": "a"},
                                  cache_key="data/raw/a/google/2026-09-23.json", now=NOW)
        except BudgetError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(x is not None for x in results) == 1
    assert ledger.status()["charged_attempts"] == 1


@pytest.mark.parametrize("status", ["saved", "failed"])
def test_completed_job_key_cannot_be_reissued(tmp_path, status):
    ledger = budget(tmp_path, limit=20)
    first = ledger.reserve("google_trend", {"keyword": "a"}, cache_key="same-job", now=NOW)
    ledger.complete(first["reservation_id"], "cache.json" if status == "saved" else None, status)
    with pytest.raises(BudgetError):
        ledger.reserve("google_trend", {"keyword": "a"}, cache_key="same-job", now=NOW)
    assert ledger.status()["charged_attempts"] == 1


def make_root(tmp_path, amazon_request=None, google_request=None, amazon_rows=None, google_values=None):
    root = tmp_path
    save(root / "data/processed/catalog.json", [{"id": "ingredient-a", "family_id": "family-a",
         "keyword": "ingredient a", "name_cn": "成分甲", "selected": True}])
    a_request = amazon_request or {"keyword": "ingredient a", "marketplace": "US", "timeGranularity": "W"}
    rows = amazon_rows if amazon_rows is not None else [
        {"label": "20260905", "searches": 100, "rank": 40},
        {"label": "20260919", "searches": 300, "rank": 20},
        {"label": "20260926", "searches": 999999, "rank": 1},
    ]
    save(root / "data/raw/US/ingredient-a/amazon/2026-09-23.json", {
        "fetched_at": "2026-09-23T00:00:00Z", "request": a_request, "tool": "aba_research_trend",
        "result": {"code": "OK", "data": rows}})
    g_request = google_request or {"request": {"keyword": "ingredient a", "marketplace": "US", "monthly": False, "googleProp": "web"}}
    values = google_values or [("2026-08-30", 10), ("2026-09-06", 20), ("2026-09-13", 99)]
    items = [{"time": int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000), "value": value}
             for day, value in values]
    save(root / "data/raw/US/ingredient-a/google/2026-09-23.json", {
        "fetched_at": "2026-09-23T00:00:00Z", "request": g_request, "tool": "google_trend",
        "result": {"code": "OK", "data": {"keyword": "ingredient a", "marketplace": "US", "items": items}}})
    return root


def read_panel(root):
    return pd.read_csv(root / "data/processed/panel.csv").set_index("week_end")


def test_missing_target_stays_missing_google_is_lagged_and_future_rows_excluded(tmp_path):
    root = make_root(tmp_path)
    result = assemble_panel(root, "2026-09-23")
    panel = read_panel(root)
    assert panel.index.tolist() == ["2026-09-05", "2026-09-12", "2026-09-19"]
    assert pd.isna(panel.loc["2026-09-12", "searches"])
    assert pd.isna(panel.loc["2026-09-05", "google_trend"])
    assert panel.loc["2026-09-12", "google_trend"] == 10
    assert panel.loc["2026-09-19", "google_trend"] == 20
    assert 999999 not in panel.searches.values
    assert 99 not in panel.google_trend.values
    assert (root / "data/processed/datasets/sellersprite/2026-09-23/panel.csv").exists()
    alias_manifest = json.loads((root / "data/processed/panel.manifest.json").read_text(encoding="utf-8"))
    dataset_manifest = json.loads((root / result["manifest_path"]).read_text(encoding="utf-8"))
    assert alias_manifest["panel_sha256"] == dataset_manifest["panel_sha256"]
    assert alias_manifest["amazon_provider"] == "sellersprite"
    assert all(row["provider"] and row["source"] and row["source_sha256"]
               for row in dataset_manifest["inputs"])


def test_wrong_amazon_keyword_cache_is_not_a_reusable_hit(tmp_path):
    root = make_root(tmp_path, amazon_request={"keyword": "different ingredient", "marketplace": "US", "timeGranularity": "W"})
    plan = collection_plan(root, "2026-09-23")
    assert any(job["kind"] == "amazon" for job in plan["jobs"])
    with pytest.raises(ValueError, match="No usable"):
        assemble_panel(root, "2026-09-23")


@pytest.mark.parametrize("wrong", [{"monthly": True}, {"googleProp": "youtube"}, {"keyword": "different ingredient"}])
def test_wrong_google_query_cannot_mix_into_feature_or_suppress_refetch(tmp_path, wrong):
    request = {"keyword": "ingredient a", "marketplace": "US", "monthly": False, "googleProp": "web", **wrong}
    root = make_root(tmp_path, google_request={"request": request})
    plan = collection_plan(root, "2026-09-23")
    assert any(job["kind"] == "google" for job in plan["jobs"])
    assemble_panel(root, "2026-09-23")
    assert read_panel(root).google_trend.isna().all()
    quality = json.loads((root / "data/processed/data_quality.json").read_text(encoding="utf-8"))
    assert quality["ingredients"][0]["warnings"]


def test_nonfinite_or_invalid_amazon_values_are_missing_in_panel(tmp_path):
    rows = [{"label": "20260905", "searches": "Infinity", "rank": "Infinity"},
            {"label": "20260912", "searches": -2, "rank": 0},
            {"label": "20260919", "searches": "bad", "rank": None}]
    root = make_root(tmp_path, amazon_rows=rows)
    assemble_panel(root, "2026-09-23")
    panel = read_panel(root)
    assert panel.searches.isna().all()
    assert panel["rank"].isna().all()
    assert not np.isinf(panel.select_dtypes(include="number").to_numpy()).any()


def test_non_saturday_amazon_period_is_rejected(tmp_path):
    root = make_root(tmp_path, amazon_rows=[{"label": "20260918", "searches": 100, "rank": 1}])
    with pytest.raises(ValueError, match="No usable"):
        assemble_panel(root, "2026-09-23")


def make_sif_root(tmp_path, dates, volumes, ranks, keywords=("ingredient a",), country="US",
                  granularity="week", tool="market_get_keyword_history", source="sif"):
    root = tmp_path
    save(root / "data/processed/catalog.json", [{"id": "ingredient-a", "family_id": "family-a",
         "keyword": "ingredient a", "name_cn": "成分甲", "selected": True}])
    save(root / "data/raw/US/ingredient-a/amazon/2026-09-23.json", {
        "fetched_at": "2026-09-23T00:00:00Z", "source": source, "tool": tool,
        "request": {"keywords": list(keywords), "country": country, "granularity": granularity},
        "result": {"country": "US", "granularity": "week", "keywords": [
            {"keyword": "ingredient a", "dates": dates, "volumes": volumes, "ranks": ranks}]}})
    return root


def test_sif_sunday_start_labels_map_to_saturday_and_future_week_is_excluded(tmp_path):
    root = make_sif_root(tmp_path,
                         dates=["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"],
                         volumes=[10, 20, 30, 999], ranks=[9, 8, 7, 1])
    result = assemble_panel(root, "2026-09-23", amazon_provider="sif")
    panel = pd.read_csv(result["panel_path"]).set_index("week_end")
    assert panel.index.tolist() == ["2026-09-05", "2026-09-12", "2026-09-19"]
    assert panel.loc["2026-09-05", "searches"] == 10
    assert panel.loc["2026-09-12", "searches"] == 20
    assert panel.loc["2026-09-12", "rank"] == 8
    assert panel.loc["2026-09-19", "searches"] == 30
    assert 999 not in panel.searches.values
    assert panel["amazon_provider"].eq("sif").all()
    assert not (root / "data/processed/panel.csv").exists()
    manifest = json.loads((root / "data/processed/datasets/sif/2026-09-23/manifest.json").read_text(encoding="utf-8"))
    assert manifest["amazon_provider"] == "sif" and manifest["panel_sha256"]
    quality = json.loads((root / "data/processed/datasets/sif/2026-09-23/data_quality.json").read_text(encoding="utf-8"))
    assert any("SIF来源" in w for w in quality["ingredients"][0]["warnings"])


def test_legacy_sif_tool_alias_remains_readable_during_migration(tmp_path):
    root = make_sif_root(tmp_path, dates=["2026-09-13"], volumes=[1], ranks=[2],
                         tool="sif_market_get_keyword_history")
    result = assemble_panel(root, "2026-09-23", amazon_provider="sif")
    assert result["ingredients"] == 1


def test_sif_tool_cannot_override_conflicting_explicit_source(tmp_path):
    root = make_sif_root(tmp_path, dates=["2026-09-13"], volumes=[1], ranks=[2],
                         source="sellersprite")
    with pytest.raises(ValueError, match="No usable"):
        assemble_panel(root, "2026-09-23", amazon_provider="sif")


def test_legacy_sif_import_directory_is_read_only_migration_source(tmp_path):
    root = tmp_path
    save(root / "data/processed/catalog.json", [{"id": "ingredient-a", "family_id": "family-a",
         "keyword": "ingredient a", "name_cn": "成分甲", "selected": True}])
    save(root / "data/raw/US/_sif_envelopes_all/ingredient-a.json", {
        "fetched_at": "", "source": "sif", "tool": "sif_market_get_keyword_history",
        "request": {"keywords": ["ingredient a"], "country": "US", "granularity": "week"},
        "result": {"country": "US", "granularity": "week", "keywords": [{
            "keyword": "ingredient a", "dates": ["2026-09-13"],
            "volumes": [4], "ranks": [3]}]}})
    result = assemble_panel(root, "2026-09-23", amazon_provider="sif")
    panel = pd.read_csv(result["panel_path"])
    assert panel.loc[0, "amazon_provider"] == "sif"
    assert panel.loc[0, "week_end"] == "2026-09-19"
    manifest = json.loads((root / result["manifest_path"]).read_text(encoding="utf-8"))
    assert "_sif_envelopes_all" in manifest["inputs"][0]["path"]


@pytest.mark.parametrize("wrong", [{"keywords": ("other term",)}, {"country": "DE"}, {"granularity": "month"}])
def test_sif_cache_with_wrong_identity_is_rejected(tmp_path, wrong):
    root = make_sif_root(tmp_path, dates=["2026-09-13"], volumes=[1], ranks=[2], **wrong)
    with pytest.raises(ValueError, match="No usable"):
        assemble_panel(root, "2026-09-23", amazon_provider="sif")


@pytest.mark.parametrize(
    "dates,volumes,ranks",
    [(["2026-09-13", "2026-09-20"], [1], [2, 1]),
     (["2026-09-13", "2026-09-13"], [1, 2], [2, 1]),
     (["2026-09-12"], [1], [2])],
)
def test_sif_malformed_arrays_duplicate_or_non_sunday_are_rejected(tmp_path, dates, volumes, ranks):
    root = make_sif_root(tmp_path, dates=dates, volumes=volumes, ranks=ranks)
    with pytest.raises(ValueError, match="No usable"):
        assemble_panel(root, "2026-09-23", amazon_provider="sif")


def test_sif_identical_duplicate_week_is_deduplicated(tmp_path):
    root = make_sif_root(
        tmp_path,
        dates=["2026-09-13", "2026-09-13", "2026-09-20"],
        volumes=[10, 10, 20],
        ranks=[3, 3, 2],
    )
    result = assemble_panel(root, "2026-09-30", amazon_provider="sif")
    panel = pd.read_csv(result["panel_path"])
    assert panel[["week_end", "searches", "rank"]].to_dict("records") == [
        {"week_end": "2026-09-19", "searches": 10, "rank": 3},
        {"week_end": "2026-09-26", "searches": 20, "rank": 2},
    ]


def test_sif_conflicting_duplicate_week_is_rejected(tmp_path):
    root = make_sif_root(
        tmp_path,
        dates=["2026-09-13", "2026-09-13"],
        volumes=[10, 11],
        ranks=[3, 3],
    )
    with pytest.raises(ValueError, match="No usable"):
        assemble_panel(root, "2026-09-30", amazon_provider="sif")


def test_sif_cache_never_suppresses_sellersprite_plan(tmp_path):
    root = make_sif_root(tmp_path, dates=["2026-09-13", "2026-09-20"], volumes=[1, 2], ranks=[5, 4])
    seller_plan = collection_plan(root, "2026-09-23")
    assert any(job["kind"] == "amazon" and job["provider"] == "sellersprite"
               for job in seller_plan["jobs"])
    sif_plan = collection_plan(root, "2026-09-23", amazon_provider="sif")
    assert not any(job["kind"] == "amazon" for job in sif_plan["jobs"])
    assert any("amazon" in hit for hit in sif_plan["cache_hits"])
    assert all(job["cache_path"].startswith("data/raw/sellersprite/US/") for job in seller_plan["jobs"])


def test_provider_specific_caches_can_coexist_without_cross_hits(tmp_path):
    root = make_root(tmp_path)
    request = {"keywords": ["ingredient a"], "country": "US", "granularity": "week"}
    sif_path = provider_cache_path(root, "sif", "US", "ingredient-a", "amazon", "2026-09-23", request)
    save(sif_path, {
        "fetched_at": "2026-09-23T00:00:00Z", "source": "sif", "tool": "sif_market_get_keyword_history",
        "request": request,
        "result": {"country": "US", "granularity": "week", "keywords": [{
            "keyword": "ingredient a", "dates": ["2026-09-13"], "volumes": [7], "ranks": [3]}]}})
    seller_plan = collection_plan(root, "2026-09-23")
    sif_plan = collection_plan(root, "2026-09-23", amazon_provider="sif")
    assert not any(job["kind"] == "amazon" for job in seller_plan["jobs"])
    assert not any(job["kind"] == "amazon" for job in sif_plan["jobs"])
    assert any("/amazon/2026-09-23.json" in hit.replace("\\", "/") for hit in seller_plan["cache_hits"])
    assert any(hit.replace("\\", "/").startswith("data/raw/sif/US/ingredient-a/amazon/2026-09-23-")
               for hit in sif_plan["cache_hits"])


def test_sif_plan_batches_at_most_five_keywords_and_exposes_derived_targets(tmp_path):
    catalog = [
        {"id": f"ingredient-{index}", "family_id": f"family-{index}",
         "keyword": f"ingredient {index}", "name_cn": str(index), "selected": True}
        for index in range(12)
    ]
    save(tmp_path / "data/processed/catalog.json", catalog)
    plan = collection_plan(tmp_path, "2026-09-23", amazon_provider="sif")
    amazon_jobs = [job for job in plan["jobs"] if job["kind"] == "amazon"]
    assert len(amazon_jobs) == 3
    assert [len(job["request"]["keywords"]) for job in amazon_jobs] == [5, 5, 2]
    assert all(job["tool"] == "market_get_keyword_history" for job in amazon_jobs)
    assert all("/sif/US/_batches/history/" in f"/{job['cache_path']}" for job in amazon_jobs)
    assert all(len(job["cache_targets"]) == len(job["items"]) for job in amazon_jobs)
    assert plan["planned_calls"] == 15  # 3 SIF batches + 12 SellerSprite Google calls.


def test_refresh_amazon_forces_only_amazon_job_and_keeps_fresh_google_hit(tmp_path):
    root = make_root(tmp_path)
    normal = collection_plan(root, "2026-09-23")
    assert normal["jobs"] == []
    refreshed = collection_plan(root, "2026-09-23", refresh_amazon=True)
    assert refreshed["refresh_amazon"] is True
    assert [job["kind"] for job in refreshed["jobs"]] == ["amazon"]
    assert any("/google/" in hit.replace("\\", "/") for hit in refreshed["cache_hits"])


def test_reviewed_queries_keep_generic_and_detailed_alias_provenance(tmp_path):
    source = tmp_path / "ingredients.json"
    save(source, {"categories": [{"id": "adult", "ingredients": [
        {"name": "Creatine", "nameCN": "肌酸", "keyBrands": ["A"]},
        {"name": "Creatine Monohydrate", "nameCN": "一水肌酸", "keyBrands": ["B"]},
        {"name": "Vitamin D3 + K2", "nameCN": "复合维生素"}]}]})
    build_catalog(source, tmp_path / "processed")
    rows = json.loads((tmp_path / "processed/catalog.json").read_text(encoding="utf-8"))
    creatine = next(x for x in rows if x["id"] == "creatine")
    assert creatine["aliases"] == ["Creatine", "Creatine Monohydrate"]
    assert creatine["selected"] is True
    assert creatine["claims_verified"] is False
    assert next(x for x in rows if "+" in x["name"])["eligible"] is False


def test_git_tracked_mappings_rebuild_all_52_reviewed_queries(tmp_path):
    build_catalog(output_dir=tmp_path / "processed")
    rows = json.loads((tmp_path / "processed/catalog.json").read_text(encoding="utf-8"))
    selected = {row["id"]: row for row in rows if row["selected"]}
    assert len(selected) == 52
    for ingredient_id in ("probiotics", "magnesium", "vitamin-c", "collagen-peptides",
                          "curcumin", "zinc", "biotin", "omega-3", "glutathione",
                          "tongkat-ali", "saw-palmetto", "vitamin-b12"):
        assert selected[ingredient_id]["query_status"] == "reviewed_batch2_2026-09-23"
        assert set(selected[ingredient_id]["queries"]) == {"sellersprite", "sif"}
