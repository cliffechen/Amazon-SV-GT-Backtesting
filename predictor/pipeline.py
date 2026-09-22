from __future__ import annotations

from datetime import date
from pathlib import Path

from .common import ROOT, now_iso, read_json, write_json


def enrich_analysis(path, root=ROOT, as_of=None):
    """Attach contemporaneous context after modeling; news never touches training."""
    from .budget import Budget
    from .news import build_news_digest
    root = Path(root); path = Path(path)
    as_of = str(as_of or date.today())
    result = read_json(path)
    result["report_as_of"] = as_of
    data_date = date.fromisoformat(str(result["as_of"])[:10])
    result["data_age_days"] = (date.fromisoformat(as_of) - data_date).days
    result["warnings"] = [w for w in result.get("warnings", []) if not w.startswith("最新亚马逊数据距报告日期")]
    if result["data_age_days"] > 7:
        result.setdefault("warnings", []).append(f"最新亚马逊数据距报告日期{result['data_age_days']}天；13/26周预测从数据截止周开始，包含尚未发布数据的近期周，不是从今天重新起算。")
    result["catalog_audit"] = read_json(root / "data/processed/audit.json")
    result["budget"] = Budget(root / "config/budget.json", root / "data/budget.sqlite").status()
    quality = read_json(root / "data/processed/data_quality.json")
    result["data_quality"] = quality
    quality_by_id = {x["id"]: x for x in quality["ingredients"]}
    for ingredient in result["ingredients"]:
        q = quality_by_id.get(ingredient["id"], {})
        ingredient.setdefault("diagnostics", {})["source_audit"] = q
        ingredient["diagnostics"].setdefault("warnings", []).extend(q.get("warnings", []))
        ingredient["diagnostics"]["warnings"] = list(dict.fromkeys(ingredient["diagnostics"]["warnings"]))
        for forecast in ingredient.get("forecasts", []):
            selected = result.get("metrics", {}).get("selected_models", {}).get(str(forecast["horizon_weeks"]), {})
            test = selected.get("sealed_test", {})
            model_error = test.get("model_metrics", {}).get("wape")
            baseline_error = test.get("baseline_metrics", {}).get("wape")
            forecast["has_proven_gain"] = bool(model_error is not None and baseline_error is not None and model_error < baseline_error and forecast.get("model") != "last13mean")
            forecast["decision_label"] = "有后期改善证据，仍需前瞻验证" if forecast["has_proven_gain"] else "探索性结果：尚未证实稳定优于简单基准"
    result.setdefault("warnings", []).extend(result["catalog_audit"]["warnings"])
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["auxiliary"] = {}
    for file in (root / "data/auxiliary").glob("*.json"):
        obj = read_json(file)
        if "ingredient_id" in obj:
            result["auxiliary"][obj["ingredient_id"]] = obj
    snapshot_path = root / "data/processed/competition.json"
    if snapshot_path.exists():
        for iid, snapshot in read_json(snapshot_path).items():
            result["auxiliary"].setdefault(iid, {})["recent_snapshot"] = snapshot
    seed = root / "data/news/verified_seed.json"
    result["news"] = build_news_digest(seed, as_of, root / "data/processed/catalog.json")
    write_json(root / "data/news/digest.json", result["news"])
    result["report_generated_at"] = now_iso()
    write_json(path, result)
    return result


def run_pipeline(root=ROOT, output=None, as_of=None):
    from .catalog import build_catalog
    from .data import assemble_panel
    from .model import run_analysis
    from .report import build_report
    root = Path(root); output = Path(output or root / "outputs/latest")
    if not (root / "data/processed/catalog.json").exists():
        build_catalog(root / "ingredient_db.json", root / "data/processed")
    assemble_panel(root, as_of)
    run_analysis(root / "data/processed/panel.csv", output, root / "data/processed/catalog.json")
    enrich_analysis(output / "analysis.json", root, as_of)
    report = build_report(output / "analysis.json", output / "report.html")
    return {"report": str(report.resolve()), "analysis": str((output / "analysis.json").resolve()),
            "note": "从已缓存数据复算；没有调用付费工具，也没有执行实时新闻搜索。"}
