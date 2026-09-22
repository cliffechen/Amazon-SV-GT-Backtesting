from predictor.common import read_json, write_json
from predictor.pipeline import enrich_analysis


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
    actual = read_json(path)["ingredients"][0]["forecasts"][0]
    assert all(actual[key] == value for key, value in forecast.items())
    assert actual["has_proven_gain"] is False
    assert later["auxiliary"]["example"]["recent_snapshot"]["ppc_bid"] == 2.0
