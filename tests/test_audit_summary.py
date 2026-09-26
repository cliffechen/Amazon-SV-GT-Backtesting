from predictor.audit_summary import build_audit_summary
from predictor.common import read_json, write_json


def test_audit_summary_is_small_and_contains_no_raw_history(tmp_path):
    analysis = tmp_path / "analysis.json"
    write_json(analysis, {
        "schema_version": "2.0", "as_of": "2026-09-19", "scope": {"marketplace": "US"},
        "provenance": {"amazon_provider": "sellersprite", "provider_counts": {"sellersprite": 1}},
        "ingredients": [{"id": "a", "history": [
            {"date": "2026-09-12", "searches": 10}, {"date": "2026-09-19", "searches": 20},
        ]}],
        "backtests": [{"horizon_weeks": 13, "split": "sealed_test", "model": "last13mean"}],
        "metrics": {"selected_models": {"13": {"model": "last13mean"}}, "paired_comparisons": []},
    })
    output = tmp_path / "summary.json"

    result = build_audit_summary(analysis, output)

    assert result["primary"]["source_counts"] == {"sellersprite": 1}
    assert result["primary"]["history"]["minimum_weeks"] == 2
    assert result["primary"]["backtest_counts"][0]["n"] == 1
    assert "searches" not in output.read_text(encoding="utf-8")
    assert read_json(output)["primary"]["analysis_sha256"]


def test_audit_summary_uses_repo_relative_paths_for_shareable_artifacts(tmp_path, monkeypatch):
    import predictor.audit_summary as module

    project = tmp_path / "project"
    analysis = project / "outputs" / "run" / "analysis.json"
    write_json(analysis, {
        "schema_version": "2.0", "as_of": "2026-09-19", "scope": {},
        "provenance": {
            "amazon_provider": "sellersprite",
            "panel_manifest_path": str(project / "data/processed/manifest.json"),
        },
        "ingredients": [], "backtests": [], "metrics": {},
    })
    monkeypatch.setattr(module, "ROOT", project)

    result = module.build_audit_summary(analysis)

    assert result["primary"]["analysis_path"] == "outputs/run/analysis.json"
    assert result["primary"]["provenance"]["panel_manifest_path"] == "data/processed/manifest.json"
