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
    assemble_panel(root, "2026-09-23")
    panel = read_panel(root)
    assert panel.index.tolist() == ["2026-09-05", "2026-09-12", "2026-09-19"]
    assert pd.isna(panel.loc["2026-09-12", "searches"])
    assert pd.isna(panel.loc["2026-09-05", "google_trend"])
    assert panel.loc["2026-09-12", "google_trend"] == 10
    assert panel.loc["2026-09-19", "google_trend"] == 20
    assert 999999 not in panel.searches.values
    assert 99 not in panel.google_trend.values


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
