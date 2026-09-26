from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from uuid import uuid4

from .common import ROOT, now_iso, read_json, sha256, write_json


def _provider_budget_status(root):
    """Read provider-aware ledger totals without making offline analysis fragile."""
    from .collection import ProviderLedger

    root = Path(root)
    providers = ("sellersprite", "sif")
    config = root / "config/provider_budgets.json"

    def unavailable(reason):
        # Do not include exception text: configuration errors can otherwise echo
        # user-controlled values such as credential-bearing endpoints.
        return {"unavailable": True, "error": reason}

    if not config.exists():
        return {
            provider: unavailable("provider quota configuration is unavailable")
            for provider in providers
        }
    try:
        ledger = ProviderLedger(config, root / "data/budget.sqlite")
    except Exception as exc:
        reason = f"provider budget ledger is unavailable ({type(exc).__name__})"
        return {provider: unavailable(reason) for provider in providers}

    statuses = {}
    for provider in providers:
        try:
            statuses[provider] = ledger.status(provider)
        except Exception as exc:
            statuses[provider] = unavailable(
                f"provider budget status is unavailable ({type(exc).__name__})"
            )
    return statuses


def enrich_analysis(path, root=ROOT, as_of=None, dataset_dir=None):
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
    result["provider_budgets"] = _provider_budget_status(root)
    dataset_dir = Path(dataset_dir) if dataset_dir else None
    quality_path = dataset_dir / "data_quality.json" if dataset_dir else root / "data/processed/data_quality.json"
    quality = read_json(quality_path)
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


def _default_run_dir(root, provider, as_of):
    stamp = now_iso().replace(":", "").replace("-", "").split(".")[0].replace("+0000", "Z")
    return Path(root) / "outputs/runs" / f"{provider}-{as_of}-{stamp}-{uuid4().hex[:8]}"


def _publish_run(root, run_dir, provider):
    """Publish a completed immutable run and retain the legacy SellerSprite URL."""
    root, run_dir = Path(root), Path(run_dir).resolve()
    outputs = root / "outputs"
    relative = run_dir.relative_to(root.resolve()).as_posix()
    pointer = {
        "schema_version": 1,
        "provider": provider,
        "run_id": run_dir.name,
        "path": relative,
        "analysis_sha256": sha256(run_dir / "analysis.json"),
        "report_sha256": sha256(run_dir / "report.html"),
        "published_at": now_iso(),
    }
    write_json(outputs / f"latest-{provider}.json", pointer)
    if provider != "sellersprite":
        return pointer

    latest = outputs / "latest"
    staging = outputs / f".latest-staging-{uuid4().hex}"
    backup = outputs / f".latest-backup-{uuid4().hex}"
    shutil.copytree(run_dir, staging)
    moved_old = False
    try:
        if latest.exists():
            latest.replace(backup)
            moved_old = True
        staging.replace(latest)
    except BaseException:
        if moved_old and backup.exists() and not latest.exists():
            backup.replace(latest)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists():
            shutil.rmtree(backup)
    return pointer


def run_pipeline(root=ROOT, output=None, as_of=None, amazon_provider="sellersprite",
                 comparison_analysis=None, publish=None):
    from .catalog import build_catalog
    from .data import assemble_panel
    from .model import run_analysis
    from .report import build_report
    root = Path(root)
    catalog_path = root / "data/processed/catalog.json"
    audit_path = root / "data/processed/audit.json"
    mappings_path = root / "config/catalog_mappings.json"
    rebuild_catalog = not catalog_path.exists() or not audit_path.exists()
    if not rebuild_catalog and mappings_path.exists():
        try:
            rebuild_catalog = read_json(audit_path).get("mapping_sha256") != sha256(mappings_path)
        except (OSError, ValueError, KeyError):
            rebuild_catalog = True
    if rebuild_catalog:
        build_catalog(root / "ingredient_db.json", root / "data/processed",
                      mappings_path=mappings_path)
    assembled = assemble_panel(root, as_of, amazon_provider=amazon_provider)
    resolved_as_of = assembled.get("as_of") or as_of or str(date.today())
    publish = output is None if publish is None else bool(publish)
    output = Path(output) if output else _default_run_dir(root, amazon_provider, resolved_as_of)
    output.mkdir(parents=True, exist_ok=False) if not output.exists() else None
    run_analysis(assembled["panel_path"], output, root / "data/processed/catalog.json",
                 panel_manifest_path=assembled["manifest_path"])
    enrich_analysis(output / "analysis.json", root, as_of,
                    dataset_dir=Path(assembled["panel_path"]).parent)
    report = build_report(output / "analysis.json", output / "report.html",
                          comparison_path=comparison_analysis)
    pointer = _publish_run(root, output, amazon_provider) if publish else None
    return {"report": str(report.resolve()), "analysis": str((output / "analysis.json").resolve()),
            "dataset": assembled.get("dataset_id"), "provider": amazon_provider,
            "published": pointer,
            "note": "从已缓存数据复算；没有调用付费工具，也没有执行实时新闻搜索。"}
