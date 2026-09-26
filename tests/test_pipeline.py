import json

from predictor.collection import ProviderLedger
from predictor.common import read_json, write_json
from predictor.pipeline import _provider_budget_status, _publish_run, enrich_analysis


def test_enrichment_preserves_predictions_and_refreshes_context_without_paid_calls(tmp_path):
    write_json(tmp_path / "config/budget.json", {
        "period": "2026-09", "project_limit": 120, "screenshot_available": 1184,
        "account_reserve": 100, "per_minute": 30,
    })
    write_json(tmp_path / "data/processed/audit.json", {"warnings": ["source limitation"]})
    write_json(tmp_path / "data/processed/catalog.json", [])
    write_json(tmp_path / "data/news/verified_seed.json", [])
    write_json(tmp_path / "data/processed/data_quality.json", {
        "ingredients": [{"id": "example", "warnings": ["provisional calendar"]}],
    })
    write_json(tmp_path / "data/processed/competition.json", {
        "example": {"period": "2026.08", "ppc_bid": 2.0},
    })
    path = tmp_path / "analysis.json"
    forecast = {"horizon_weeks": 26, "model": "seasonal", "predicted_mean": 100.0}
    write_json(path, {
        "as_of": "2026-09-12", "warnings": ["original warning"],
        "ingredients": [{"id": "example", "forecasts": [forecast]}],
        "metrics": {"selected_models": {"26": {"sealed_test": {
            "model_metrics": {"wape": 0.5}, "baseline_metrics": {"wape": 0.3},
        }}}},
    })
    first = enrich_analysis(path, tmp_path, "2026-09-23")
    repeat = enrich_analysis(path, tmp_path, "2026-09-23")
    assert set(first["warnings"]) == set(repeat["warnings"])
    assert len(repeat["warnings"]) == len(set(repeat["warnings"]))
    assert repeat["ingredients"][0]["diagnostics"]["warnings"] == ["provisional calendar"]
    later = enrich_analysis(path, tmp_path, "2026-09-24")
    age_warnings = [x for x in later["warnings"] if x.startswith("最新亚马逊数据距报告日期")]
    assert len(age_warnings) == 1 and "12天" in age_warnings[0]
    assert later["budget"]["charged_attempts"] == 0
    assert later["provider_budgets"]["sellersprite"]["unavailable"] is True
    assert later["provider_budgets"]["sif"]["unavailable"] is True
    actual = read_json(path)["ingredients"][0]["forecasts"][0]
    assert all(actual[key] == value for key, value in forecast.items())
    assert actual["has_proven_gain"] is False
    assert later["auxiliary"]["example"]["recent_snapshot"]["ppc_bid"] == 2.0


def test_enrichment_reads_provider_ledger_statuses_from_shared_database(tmp_path):
    legacy_quota = {
        "period": "2026-09", "project_limit": 120, "screenshot_available": 1184,
        "account_reserve": 100, "per_minute": 30,
    }
    provider_quotas = {"providers": {
        "sellersprite": {
            "period": "2026-09", "available": 20, "reserve": 0,
            "project_limit": 20, "per_minute": 30,
        },
        "sif": {
            "period": "2026-09", "available": 5, "reserve": 0,
            "project_limit": 5, "per_minute": 10,
        },
    }}
    write_json(tmp_path / "config/budget.json", legacy_quota)
    write_json(tmp_path / "config/provider_budgets.json", provider_quotas)
    write_json(tmp_path / "data/processed/audit.json", {"warnings": []})
    write_json(tmp_path / "data/processed/catalog.json", [])
    write_json(tmp_path / "data/news/verified_seed.json", [])
    write_json(tmp_path / "data/processed/data_quality.json", {"ingredients": []})
    path = tmp_path / "analysis.json"
    write_json(path, {"as_of": "2026-09-12", "ingredients": [], "warnings": []})

    database = tmp_path / "data/budget.sqlite"
    ledger = ProviderLedger(provider_quotas, database)
    ledger.reserve_job(
        run_id="seller", provider="sellersprite", tool="history", request={"a": 1},
        destination=tmp_path / "seller.json", units=3, now=1790150400.0,
    )
    ledger.reserve_job(
        run_id="sif", provider="sif", tool="history", request={"b": 1},
        destination=tmp_path / "sif.json", units=2, now=1790150400.0,
    )

    enriched = enrich_analysis(path, tmp_path, "2026-09-23")

    assert enriched["budget"]["charged_attempts"] == 0
    assert enriched["provider_budgets"]["sellersprite"] == ledger.status("sellersprite")
    assert enriched["provider_budgets"]["sif"] == ledger.status("sif")
    assert enriched["provider_budgets"]["sellersprite"]["counted_units_including_legacy"] == 3
    assert enriched["provider_budgets"]["sif"]["counted_units_including_legacy"] == 2


def test_provider_budget_status_isolates_bad_provider_without_leaking_values(tmp_path):
    write_json(tmp_path / "config/provider_budgets.json", {"providers": {
        "sellersprite": {
            "period": "2026-09", "available": 20, "reserve": 0,
            "project_limit": 20, "per_minute": 30,
        },
        "sif": {
            "period": "2026-09",
            "source": "https://provider.invalid/mcp?credential=must-not-leak",
        },
    }})

    statuses = _provider_budget_status(tmp_path)

    assert statuses["sellersprite"]["hard_limit"] == 20
    assert statuses["sif"]["unavailable"] is True
    assert "QuotaError" in statuses["sif"]["error"]
    assert "must-not-leak" not in json.dumps(statuses)


def test_publish_run_updates_provider_pointer_and_legacy_sellersprite_copy(tmp_path):
    run = tmp_path / "outputs/runs/sellersprite-2026-09-23-test"
    run.mkdir(parents=True)
    write_json(run / "analysis.json", {"provider": "sellersprite"})
    (run / "report.html").write_text("new report", encoding="utf-8")
    old = tmp_path / "outputs/latest"
    old.mkdir(parents=True)
    (old / "report.html").write_text("old report", encoding="utf-8")

    pointer = _publish_run(tmp_path, run, "sellersprite")

    assert pointer["run_id"] == run.name
    assert read_json(tmp_path / "outputs/latest-sellersprite.json")["path"].endswith(run.name)
    assert (tmp_path / "outputs/latest/report.html").read_text(encoding="utf-8") == "new report"
    assert not list((tmp_path / "outputs").glob(".latest-*"))


def test_publish_sif_does_not_replace_default_sellersprite_report(tmp_path):
    run = tmp_path / "outputs/runs/sif-2026-09-23-test"
    run.mkdir(parents=True)
    write_json(run / "analysis.json", {"provider": "sif"})
    (run / "report.html").write_text("sif report", encoding="utf-8")
    current = tmp_path / "outputs/latest"
    current.mkdir(parents=True)
    (current / "report.html").write_text("seller report", encoding="utf-8")

    _publish_run(tmp_path, run, "sif")

    assert read_json(tmp_path / "outputs/latest-sif.json")["provider"] == "sif"
    assert (current / "report.html").read_text(encoding="utf-8") == "seller report"
