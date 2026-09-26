"""Small, shareable summaries derived from complete local analysis artifacts."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .common import ROOT, now_iso, read_json, sha256, write_json
from .report import _vendor_comparison


def _source_counts(analysis):
    counts = Counter()
    provenance = analysis.get("provenance") or {}
    provider_counts = provenance.get("provider_counts")
    if isinstance(provider_counts, dict):
        for provider, count in provider_counts.items():
            if isinstance(count, int):
                counts[str(provider)] += count
    if not counts:
        for row in (analysis.get("data_quality") or {}).get("ingredients", []):
            provider = row.get("amazon_source") or row.get("amazon_provider")
            if provider:
                counts[str(provider)] += 1
    return dict(sorted(counts.items()))


def _history_audit(analysis):
    lengths, starts, ends = [], [], []
    for ingredient in analysis.get("ingredients", []):
        history = ingredient.get("history") or []
        lengths.append(len(history))
        dates = [str(row.get("date")) for row in history if row.get("date")]
        if dates:
            starts.append(min(dates)); ends.append(max(dates))
    return {
        "minimum_weeks": min(lengths) if lengths else 0,
        "maximum_weeks": max(lengths) if lengths else 0,
        "earliest_week": min(starts) if starts else None,
        "latest_week": max(ends) if ends else None,
    }


def _backtest_counts(analysis):
    counts = Counter()
    for row in analysis.get("backtests", []):
        key = (str(row.get("horizon_weeks")), str(row.get("split")), str(row.get("model")))
        counts[key] += 1
    return [
        {"horizon_weeks": int(horizon), "split": split, "model": model, "n": count}
        for (horizon, split, model), count in sorted(counts.items())
    ]


def _display_path(path):
    """Keep shareable summaries portable when an artifact is inside the repo."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _provenance_summary(analysis):
    provenance = analysis.get("provenance") or {}
    result = {
        key: provenance.get(key)
        for key in (
            "amazon_provider", "provider_counts", "provider_policy",
            "provider_inference", "panel_sha256", "panel_manifest_sha256",
        )
        if provenance.get(key) is not None
    }
    if provenance.get("panel_manifest_path"):
        result["panel_manifest_path"] = _display_path(provenance["panel_manifest_path"])
    return result


def build_audit_summary(analysis_path, output_path=None, comparison_path=None):
    analysis_path = Path(analysis_path)
    analysis = read_json(analysis_path)
    result = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "primary": {
            "analysis_path": _display_path(analysis_path),
            "analysis_sha256": sha256(analysis_path),
            "analysis_schema_version": analysis.get("schema_version"),
            "as_of": analysis.get("as_of"),
            "scope": analysis.get("scope"),
            "provenance": _provenance_summary(analysis),
            "source_counts": _source_counts(analysis),
            "ingredient_count": len(analysis.get("ingredients", [])),
            "history": _history_audit(analysis),
            "backtest_counts": _backtest_counts(analysis),
            "selected_models": (analysis.get("metrics") or {}).get("selected_models", {}),
            "paired_feature_comparisons": (analysis.get("metrics") or {}).get("paired_comparisons", []),
        },
    }
    if comparison_path:
        comparison_path = Path(comparison_path)
        comparison = _vendor_comparison(analysis, comparison_path)
        result["comparison"] = {
            "analysis_path": _display_path(comparison_path),
            "analysis_sha256": sha256(comparison_path) if comparison_path.exists() else None,
            "status": comparison.get("status"),
            "reasons": comparison.get("reasons", []),
            "primary_provider": comparison.get("primary_provider"),
            "secondary_provider": comparison.get("secondary_provider"),
            "dual_source_ingredients": comparison.get("dual_source_ingredients", []),
            "summary": comparison.get("summary", {}),
            "audit": comparison.get("audit", {}),
        }
    if output_path:
        write_json(output_path, result)
    return result
