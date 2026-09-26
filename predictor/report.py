"""Build the auditable, offline report. External strings are never HTML."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


def _clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_json(v) for v in value]
    return value


def _safe_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() in {"http", "https"} and parts.netloc:
            return value.strip()
    except ValueError:
        pass
    return None


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _prepare_payload(analysis):
    """Keep dated current news separate from background; do not invent dates."""
    payload = _clean_json(analysis)
    news = payload.get("news")
    if not isinstance(news, dict):
        news = {}
    end = (_day(news.get("as_of")) or _day(news.get("window_end"))
           or _day(payload.get("generated_at")) or datetime.now(timezone.utc).date())
    serial = end.year * 12 + end.month - 1 - 3
    year, month0 = divmod(serial, 12)
    start = date(year, month0 + 1, min(end.day, calendar.monthrange(year, month0 + 1)[1]))
    current, background = [], []
    records = (list(news.get("items") or []) + list(news.get("background_items") or [])
               + list(news.get("background") or []))
    for raw in records:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["url"] = _safe_url(item.get("url"))
        item["ingredient_ids"] = item.get("ingredient_ids") or []
        item["brands"] = item.get("brands") or []
        published = _day(item.get("published_at"))
        if published and start <= published <= end:
            current.append(item)
        else:
            item["background_reason"] = "未提供发布日期" if not published else "不在最近三个日历月内"
            background.append(item)
    current.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    news.update(as_of=end.isoformat(), window_start=start.isoformat(), window_end=end.isoformat(),
                items=current, background_items=background)
    payload["news"] = news
    payload.setdefault("ingredients", [])
    return payload


def _script_json(value):
    # Escaping '<' blocks </script> even inside an application/json element.
    return (json.dumps(value, ensure_ascii=False, allow_nan=False)
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


_COMPARISON_SPLITS = ("sealed_test", "group_sealed_test")
_METHOD_COMPATIBILITY_KEYS = (
    "target", "baseline", "horizons_weeks", "min_history_weeks",
    "origin_step_weeks", "training_step_weeks", "sealed_test_start",
    "selection", "group_holdout", "features", "ridge_alpha",
    "target_transform",
)


def _finite_nonnegative(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _comparison_shell(status, reasons=None):
    return {
        "status": status,
        "reasons": list(reasons or []),
        "warnings": [],
        "primary_provider": None,
        "secondary_provider": None,
        "providers": {"primary": None, "secondary": None},
        "selected_models": {},
        "models_compared": {},
        "dual_source_ingredients": [],
        "summary": {},
        "per_ingredient": {},
        "audit": {
            "invalid_rows": {"primary": 0, "secondary": 0},
            "duplicate_keys": {"primary": 0, "secondary": 0},
            "source_mismatch": [],
            "metadata_mismatch": [],
        },
    }


def _normalize_provider(value):
    if not isinstance(value, str):
        return None
    provider = value.strip().lower()
    return provider if provider in {"sellersprite", "sif"} else None


def _analysis_provider(analysis):
    provenance = analysis.get("provenance")
    if not isinstance(provenance, dict):
        return None
    return _normalize_provider(provenance.get("amazon_provider"))


def _quality_provider(record):
    """Read the current quality field, with a fail-closed legacy fallback."""
    if not isinstance(record, dict):
        return None
    current_raw = record.get("amazon_provider")
    legacy_raw = record.get("amazon_source")
    current_present = current_raw is not None and str(current_raw).strip() != ""
    legacy_present = legacy_raw is not None and str(legacy_raw).strip() != ""
    current = _normalize_provider(current_raw) if current_present else None
    legacy = _normalize_provider(legacy_raw) if legacy_present else None
    if ((current_present and current is None) or (legacy_present and legacy is None)
            or (current and legacy and current != legacy)):
        return None
    return current or legacy


def _analysis_compatibility(primary, secondary):
    reasons = []
    for key in ("schema_version", "as_of", "scope"):
        if primary.get(key) is None or secondary.get(key) is None:
            reasons.append(f"两份分析都必须提供 {key}")
        elif primary.get(key) != secondary.get(key):
            reasons.append(f"{key} 不一致")
    primary_method = primary.get("methodology")
    secondary_method = secondary.get("methodology")
    if not isinstance(primary_method, dict) or not isinstance(secondary_method, dict):
        reasons.append("两份分析都必须提供 methodology")
    else:
        for key in _METHOD_COMPATIBILITY_KEYS:
            if key not in primary_method or key not in secondary_method:
                reasons.append(f"methodology.{key} 缺失")
            elif primary_method.get(key) != secondary_method.get(key):
                reasons.append(f"methodology.{key} 不一致")
    return reasons


def _ingredient_maps(analysis):
    ingredients = {}
    for item in analysis.get("ingredients") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            ingredients[str(item["id"])] = item
    quality = {}
    block = analysis.get("data_quality")
    if isinstance(block, dict):
        for item in block.get("ingredients") or []:
            if isinstance(item, dict) and item.get("id") is not None:
                quality[str(item["id"])] = item
    return ingredients, quality


def _selected_model(analysis, horizon):
    selected = (analysis.get("metrics") or {}).get("selected_models") or {}
    record = selected.get(str(horizon)) or selected.get(horizon) or {}
    model = record.get("model") if isinstance(record, dict) else None
    return str(model) if model else None


def _index_backtests(analysis, eligible, models_by_horizon):
    index, duplicates = {}, set()
    invalid = 0
    for row in analysis.get("backtests") or []:
        if not isinstance(row, dict):
            invalid += 1
            continue
        ingredient_id = str(row.get("ingredient_id"))
        try:
            horizon = int(row.get("horizon_weeks"))
        except (TypeError, ValueError):
            invalid += 1
            continue
        split, model = row.get("split"), row.get("model")
        if (ingredient_id not in eligible or split not in _COMPARISON_SPLITS
                or model not in models_by_horizon.get(horizon, ())):
            continue
        origin, target_end = _day(row.get("origin")), _day(row.get("target_end"))
        values = (row.get("actual"), row.get("predicted"), row.get("baseline"))
        if not origin or not target_end or not all(_finite_nonnegative(v) for v in values):
            invalid += 1
            continue
        key = (ingredient_id, horizon, split, origin.isoformat(), target_end.isoformat(), str(model))
        clean = {
            "ingredient_id": ingredient_id,
            "horizon_weeks": horizon,
            "split": split,
            "origin": origin.isoformat(),
            "target_end": target_end.isoformat(),
            "model": str(model),
            "actual": float(row["actual"]),
            "predicted": float(row["predicted"]),
            "baseline": float(row["baseline"]),
        }
        if key in index:
            duplicates.add(key)
        else:
            index[key] = clean
    for key in duplicates:
        index.pop(key, None)
    return index, invalid, len(duplicates)


def _wape(rows):
    denominator = sum(abs(row["actual"]) for row in rows)
    if not denominator:
        return None
    return sum(abs(row["actual"] - row["predicted"]) for row in rows) / denominator


def _paired_metrics(keys, primary_index, secondary_index):
    primary_rows = [primary_index[key] for key in keys]
    secondary_rows = [secondary_index[key] for key in keys]
    ingredient_ids = sorted({key[0] for key in keys})
    primary_by_ingredient, secondary_by_ingredient = [], []
    macro_ids = []
    for ingredient_id in ingredient_ids:
        ingredient_keys = [key for key in keys if key[0] == ingredient_id]
        primary_value = _wape([primary_index[key] for key in ingredient_keys])
        secondary_value = _wape([secondary_index[key] for key in ingredient_keys])
        if primary_value is not None and secondary_value is not None:
            primary_by_ingredient.append(primary_value)
            secondary_by_ingredient.append(secondary_value)
            macro_ids.append(ingredient_id)
    return {
        "n_pairs": len(keys),
        "n_ingredients": len(ingredient_ids),
        "n_origins": len({key[3] for key in keys}),
        "macro_n_ingredients": len(macro_ids),
        "primary": {
            "wape": _wape(primary_rows),
            "macro_wape": (sum(primary_by_ingredient) / len(primary_by_ingredient)
                           if primary_by_ingredient else None),
        },
        "secondary": {
            "wape": _wape(secondary_rows),
            "macro_wape": (sum(secondary_by_ingredient) / len(secondary_by_ingredient)
                           if secondary_by_ingredient else None),
        },
    }


def _vendor_comparison(primary, comparison_path):
    """Build a fail-closed, strictly paired two-provider comparison."""
    result = _comparison_shell("unavailable")
    path = Path(comparison_path)
    try:
        secondary = _clean_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        result["reasons"] = [f"比较分析无法读取：{type(exc).__name__}"]
        return result
    if not isinstance(secondary, dict):
        result["reasons"] = ["比较分析的顶层必须是对象"]
        return result

    primary_provider = _analysis_provider(primary)
    secondary_provider = _analysis_provider(secondary)
    result["primary_provider"] = primary_provider
    result["secondary_provider"] = secondary_provider
    result["providers"] = {
        "primary": primary_provider, "secondary": secondary_provider,
    }
    provider_reasons = []
    if primary_provider is None:
        provider_reasons.append("主分析必须提供有效的 provenance.amazon_provider")
    if secondary_provider is None:
        provider_reasons.append("比较分析必须提供有效的 provenance.amazon_provider")
    if (primary_provider is not None and secondary_provider is not None
            and {primary_provider, secondary_provider} != {"sellersprite", "sif"}):
        provider_reasons.append("两份分析必须恰好分别来自 sellersprite 和 sif")

    compatibility = provider_reasons + _analysis_compatibility(primary, secondary)
    if compatibility:
        result["status"] = "incompatible"
        result["reasons"] = compatibility
        return result

    primary_ingredients, primary_quality = _ingredient_maps(primary)
    secondary_ingredients, secondary_quality = _ingredient_maps(secondary)
    shared_ids = sorted(set(primary_ingredients) & set(secondary_ingredients))
    eligible = set()
    for ingredient_id in shared_ids:
        primary_source = _quality_provider(primary_quality.get(ingredient_id))
        secondary_source = _quality_provider(secondary_quality.get(ingredient_id))
        if primary_source != primary_provider or secondary_source != secondary_provider:
            result["audit"]["source_mismatch"].append(ingredient_id)
            continue
        primary_item, secondary_item = primary_ingredients[ingredient_id], secondary_ingredients[ingredient_id]
        identity = ("keyword", "marketplace")
        identities = [(str(primary_item.get(key) or "").strip().lower(),
                       str(secondary_item.get(key) or "").strip().lower()) for key in identity]
        if any(not primary_value or not secondary_value or primary_value != secondary_value
               for primary_value, secondary_value in identities):
            result["audit"]["metadata_mismatch"].append(ingredient_id)
            continue
        eligible.add(ingredient_id)
    if not eligible:
        result["status"] = "no_pairs"
        result["reasons"] = ["没有来源与成分身份均可核对的双数据源成分"]
        return result

    horizons = []
    for value in (primary.get("methodology") or {}).get("horizons_weeks") or []:
        try:
            horizon = int(value)
        except (TypeError, ValueError):
            continue
        if horizon not in horizons:
            horizons.append(horizon)
    models_by_horizon = {}
    for horizon in horizons:
        primary_model = _selected_model(primary, horizon)
        secondary_model = _selected_model(secondary, horizon)
        result["selected_models"][str(horizon)] = {
            "primary": primary_model, "secondary": secondary_model,
        }
        models = ["last13mean"]
        if primary_model and primary_model == secondary_model and primary_model != "last13mean":
            models.append(primary_model)
        elif primary_model != secondary_model:
            result["warnings"].append(
                f"{horizon} 周两份分析选中的模型不同；只比较 last13mean 基线")
        models_by_horizon[horizon] = tuple(models)
        result["models_compared"][str(horizon)] = models

    primary_index, invalid_primary, duplicate_primary = _index_backtests(
        primary, eligible, models_by_horizon)
    secondary_index, invalid_secondary, duplicate_secondary = _index_backtests(
        secondary, eligible, models_by_horizon)
    result["audit"]["invalid_rows"] = {
        "primary": invalid_primary, "secondary": invalid_secondary,
    }
    result["audit"]["duplicate_keys"] = {
        "primary": duplicate_primary, "secondary": duplicate_secondary,
    }

    paired_ingredients = set()
    for horizon in horizons:
        horizon_key = str(horizon)
        result["summary"][horizon_key] = {}
        for split in _COMPARISON_SPLITS:
            split_summary = {}
            for model in models_by_horizon[horizon]:
                primary_keys = {key for key in primary_index
                                if key[1] == horizon and key[2] == split and key[5] == model}
                secondary_keys = {key for key in secondary_index
                                  if key[1] == horizon and key[2] == split and key[5] == model}
                keys = sorted(primary_keys & secondary_keys)
                if not keys:
                    continue
                metrics = _paired_metrics(keys, primary_index, secondary_index)
                metrics["pairing"] = {
                    "primary_only": len(primary_keys - secondary_keys),
                    "secondary_only": len(secondary_keys - primary_keys),
                }
                split_summary[model] = metrics
                for ingredient_id in sorted({key[0] for key in keys}):
                    ingredient_keys = [key for key in keys if key[0] == ingredient_id]
                    primary_rows = [primary_index[key] for key in ingredient_keys]
                    secondary_rows = [secondary_index[key] for key in ingredient_keys]
                    series = {
                        "origins": [row["origin"] for row in primary_rows],
                        "target_ends": [row["target_end"] for row in primary_rows],
                        "primary": {
                            "actual": [row["actual"] for row in primary_rows],
                            "predicted": [row["predicted"] for row in primary_rows],
                            "wape": _wape(primary_rows),
                        },
                        "secondary": {
                            "actual": [row["actual"] for row in secondary_rows],
                            "predicted": [row["predicted"] for row in secondary_rows],
                            "wape": _wape(secondary_rows),
                        },
                    }
                    (result["per_ingredient"].setdefault(ingredient_id, {})
                     .setdefault(horizon_key, {}).setdefault(split, {})[model]) = series
                    paired_ingredients.add(ingredient_id)
            if split_summary:
                result["summary"][horizon_key][split] = split_summary
        if not result["summary"][horizon_key]:
            result["summary"].pop(horizon_key)

    result["dual_source_ingredients"] = sorted(paired_ingredients)
    if paired_ingredients:
        result["status"] = "ok"
        result["reasons"] = []
    else:
        result["status"] = "no_pairs"
        result["reasons"] = ["两份分析没有通过严格键匹配的封存回测窗口"]
    return result


def build_report(analysis_path, output_path, comparison_path=None) -> Path:
    """Create a self-contained report; no remote scripts, fonts, or chart assets."""
    from plotly.offline import get_plotlyjs

    analysis = json.loads(Path(analysis_path).read_text(encoding="utf-8"))
    payload = _prepare_payload(analysis)
    if comparison_path is not None:
        payload["vendor_comparison"] = _vendor_comparison(payload, comparison_path)
    document = _HTML.replace("__PLOTLY_JS__", get_plotlyjs()).replace(
        "__ANALYSIS_JSON__", _script_json(payload))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>成分趋势研究台 · Amazon US</title>
<style>
:root{--bg:#fff;--ink:#111214;--muted:#73767d;--line:#e9e9ed;--accent:#1938f5;--pale:#f1f3ff;--orange:#986018;--radius:0;--rail:56px}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:22px}
body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.7 "Segoe UI","Microsoft YaHei",Arial,sans-serif;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit}button{cursor:pointer}button,a,select,input{outline-offset:4px;accent-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent)}a{color:var(--accent)}button{transition:background .15s,color .15s}button:disabled{cursor:default}
.rail{position:fixed;inset:0 auto 0 0;width:var(--rail);border-right:1px solid var(--line);background:#fff;z-index:5;display:flex;flex-direction:column;align-items:stretch}
.rail-mark{height:76px;display:grid;place-items:center;border-bottom:1px solid var(--line)}.rail-mark svg{width:22px;height:22px}
.rail-links{display:grid;gap:10px;padding-top:27px}.rail a{height:44px;display:grid;place-items:center;color:#7c7e86;position:relative;text-decoration:none}
.rail a:hover{background:var(--pale);color:var(--accent)}.rail a:first-child{background:var(--accent);color:#fff}.rail a svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round}
.rail-bottom{margin-top:auto;text-align:center;color:#8d8f98;font-size:9px;letter-spacing:.12em;padding:20px 0;writing-mode:vertical-rl;align-self:center}
.top{height:77px;margin-left:var(--rail);border-bottom:1px solid var(--line);padding:0 34px;display:flex;gap:24px;align-items:center;justify-content:space-between;background:#fff}
.identity{display:flex;gap:11px;align-items:center}.logo{width:9px;height:9px;flex-shrink:0;background:var(--accent);font-size:0;box-shadow:5px 5px 0 #dce2ff}
.brand{font-weight:600;font-size:16px;letter-spacing:.025em}.sub{color:var(--muted);font-size:10px;letter-spacing:.02em}.identity .sub{font-size:8px;letter-spacing:.13em;margin-top:2px}
.top nav{display:flex;height:100%;gap:28px}.top nav a{color:#666871;text-decoration:none;font-size:11px;display:flex;align-items:center;border-bottom:2px solid transparent;padding-top:2px;white-space:nowrap}
.top nav a:first-child{border-color:#17191f;color:#17191f}.top nav a:hover{color:var(--accent);border-color:var(--accent)}
.scope{font-size:10px;color:#73767f;white-space:nowrap;display:flex;align-items:center;gap:8px}.scope:before{content:"";width:5px;height:5px;background:var(--accent)}
.layout{margin-left:var(--rail);display:grid;grid-template-columns:208px minmax(0,1fr);gap:36px;padding:32px 34px 0 24px;max-width:1760px}
.sidebar{position:sticky;top:22px;align-self:start;border-right:1px solid var(--line);padding:0 20px 18px 0;min-width:0}.sidebar h2{font-size:12px;font-weight:500;letter-spacing:.04em;margin:0 0 17px}.sidebar h2:before{content:"/";color:var(--accent);margin-right:8px}
.sidebar label{font-size:10px;display:block;margin-bottom:8px}.search{width:100%;border:1px solid var(--line);padding:9px 10px;background:#fff;color:var(--ink);border-radius:0;font-size:11px}.search::placeholder{color:#a0a1a8}.search:focus{border-color:var(--accent)}
.catalog-count{font-size:10px;color:#858891;margin:15px 0 17px;letter-spacing:.025em}.ingredient-list{max-height:calc(100vh - 265px);overflow:auto;display:grid;gap:3px;scrollbar-width:thin;scrollbar-color:#dce0e8 transparent}
.ingredient{border:0;border-left:2px solid transparent;background:transparent;padding:11px 10px;text-align:left;color:#43464d;min-width:0;line-height:1.55;border-radius:0}
.ingredient:hover{background:#f7f8fb}.ingredient[aria-current=true]{border-left-color:#1938f5;background:var(--pale);color:var(--accent)}.ingredient strong{font-size:12px;font-weight:500}.ingredient strong,.ingredient span{display:block;overflow-wrap:anywhere}.ingredient span{font-size:10px;color:#8c8f98;margin-top:3px}.ingredient[aria-current=true] span{color:#6576cf}
main{min-width:0;counter-reset:section}.intro{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin:0 0 24px}.eyebrow{font-size:9px;font-weight:500;letter-spacing:.17em;color:#7e818b}.eyebrow:before{content:"←";color:#1a1c22;font-size:14px;letter-spacing:0;margin-right:12px}
.intro h1{font-size:32px;font-weight:450;line-height:1.4;letter-spacing:-.04em;margin:18px 0 4px}.intro p{font-size:11px;color:var(--muted);margin:0}
.segment{display:flex;gap:20px;white-space:nowrap;border-bottom:1px solid var(--line);align-self:flex-end}.segment button{border:0;border-bottom:2px solid transparent;padding:8px 0;background:transparent;color:#858790;font-size:11px;border-radius:0}.segment button[aria-pressed=true]{color:#17191d;border-bottom-color:#17191d}.segment button:hover{color:var(--accent)}
.notice{font-size:11px;color:#716f69;border-left:2px solid #c7bda9;background:#faf9f6;padding:8px 12px;margin:0 0 27px;line-height:1.8}
.cards{display:grid;grid-template-columns:1.15fr .85fr 1.1fr;gap:0;margin:0 0 19px}.card{min-width:0;padding:16px 22px;border:0;border-left:1px solid var(--line);background:#fff;border-radius:0}.card:first-child{border-left:0;padding-left:0}
.card .label{font-size:11px;color:#73767f;letter-spacing:.02em}.value{font-size:46px;font-weight:350;line-height:1.25;letter-spacing:-.06em;margin:16px 0 14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.small{font-size:12px;line-height:1.8;color:var(--muted)}.primary .value{font-size:64px;color:#08090c;font-weight:350;margin-top:15px;margin-bottom:16px}.primary .small{font-size:11px}.primary{padding-top:16px}
main>.cards{min-height:227px}main>.cards>.card:nth-child(2){padding-top:16px}main>.cards>.card:nth-child(2) .value{font-size:42px;margin-top:25px}
main>.cards>.card:last-child{background:var(--accent);color:#fff;padding:24px 25px;border:0;position:relative;overflow:hidden;margin-left:14px}
main>.cards>.card:last-child:before{content:"";display:block;width:7px;height:7px;background:#fff;box-shadow:7px 7px 0 #ffffff38;margin-bottom:24px}
main>.cards>.card:last-child .label{color:#d0d7ff;font-size:11px}main>.cards>.card:last-child .value{font-size:32px;font-weight:350;letter-spacing:-.04em;margin:13px 0 19px;color:#fff}main>.cards>.card:last-child .small{color:#d1d8ff;font-size:11px}
.rise{color:var(--accent)}.fall{color:var(--orange)}.muted{color:var(--muted)}.decision{font-size:11px;font-weight:500;line-height:1.8;padding:6px 8px;margin:0 0 10px;border-left:2px solid #b98636;max-width:300px}.decision.warning{color:#896026;background:#faf5e9}.decision.supported{color:#263cc4;background:#f0f2ff;border-color:var(--accent)}
.window{font-size:11px;color:#757983;border-top:1px solid var(--line);padding:12px 0;margin-bottom:3px;line-height:1.85}.diagnostics{display:flex;gap:16px;flex-wrap:wrap}.pill{display:inline-block;font-size:10px;color:#777a84;background:#f5f6f8;padding:3px 7px;line-height:1.8}.pill.warning{color:#896026;background:#faf5e9}
.panel{border:0;border-top:1px solid var(--line);background:#fff;border-radius:0;padding:26px 0 24px;margin:29px 0 0;scroll-margin-top:22px;counter-increment:section}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.section-head h2{font-size:18px;font-weight:450;letter-spacing:-.035em;margin:0 0 5px}.section-head h2:before{content:counter(section,decimal-leading-zero);font-size:9px;color:var(--accent);font-weight:500;letter-spacing:.08em;vertical-align:middle;display:inline-block;width:27px}.section-head p{font-size:11px;color:var(--muted);margin:0;line-height:1.9}
.controls{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.controls button{border:1px solid var(--line);background:#fff;padding:5px 9px;font-size:10px;color:#656873;border-radius:0}.controls button:hover,.controls button:focus-visible{border-color:var(--accent);color:var(--accent)}.controls label{font-size:10px;white-space:nowrap;color:#60636d;margin-left:6px}.controls input{vertical-align:-2px}
.chart{width:100%;height:330px}.google-chart{height:155px;border-top:1px solid #f0f0f3;margin-top:8px}.vendor-block{border-top:1px solid var(--line);margin-top:22px;padding-top:20px}.vendor-block h3{font-size:14px;font-weight:500;margin:0 0 4px}.vendor-chart{height:500px;margin-top:12px}.vendor-table{margin-top:12px}.legend-note{font-size:11px;color:#757983;line-height:1.8;margin-top:8px}.empty{padding:34px 20px;border:1px dashed #dfe1e8;color:var(--muted);text-align:center;background:#fafafb;font-size:11px}
#competition-values{background:#fafafb;margin-top:25px;padding:20px 0}#competition-values .card{background:transparent;padding:0 20px}#competition-values .value{font-size:36px;color:#14161d;margin:13px 0}#competition-values .card .label{font-size:10px}
#competition-panel .section-head .pill,#backtest-panel .section-head .pill,#news-panel .section-head .pill,#audit-panel .section-head .pill{background:transparent;border:1px solid var(--line);font-size:9px}
#performance-cards{margin-top:22px;border-left:2px solid var(--accent);padding:1px 0 1px 17px}#performance-cards .diagnostics .pill{background:#f3f5ff;color:#3148b7;padding:6px 10px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:28px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:11px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);background:#fafafb;font-weight:500}
.news-filters{display:flex;gap:10px;flex-wrap:wrap;margin:21px 0}.news-filters select{border:1px solid #e0e2e9;background:#fff;border-radius:0;padding:8px 25px 8px 10px;font-size:11px;color:#555964;max-width:100%}
.news-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 24px}.news-card{border:0;border-top:1px solid #dfe2e9;border-radius:0;padding:20px 0;display:flex;flex-direction:column}.news-card:before{content:"";width:5px;height:5px;background:var(--accent);box-shadow:5px 5px 0 #e3e7ff;margin:0 0 15px}.news-card h3{font-size:14px;font-weight:500;line-height:1.7;margin:12px 0 10px;overflow-wrap:anywhere}.news-card h3 a{color:#1e2027;text-decoration:none}.news-card h3 a:hover{color:var(--accent);text-decoration:underline}.news-card p{font-size:12px;color:#696e78;margin:0 0 12px;line-height:1.9}.news-meta{display:flex;gap:7px;flex-wrap:wrap;align-items:center;color:#7b7e88;font-size:9px}.news-meta .pill{font-size:9px;padding:1px 5px}.evidence{padding:1px 5px;background:#f0f2ff;color:#435ac8;font-size:9px}.evidence.unverified{background:#faf5e9;color:#896026}.news-footer{margin-top:auto;font-size:10px;color:#9699a2}
details{border-top:1px solid var(--line);padding-top:15px;margin-top:18px}summary{cursor:pointer;color:#767a85;font-size:11px}summary:hover{color:var(--accent)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:10px/1.7 ui-monospace,monospace;background:#f7f8fb;padding:15px;max-height:320px;overflow:auto}.list{padding-left:17px;font-size:12px;color:#696e78;line-height:1.9}.list li{margin-bottom:9px}.grid2 h3{font-weight:500}
.budget-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;border-bottom:1px solid var(--line);padding:13px 0 24px;margin-bottom:20px}.budget-grid .small{font-size:10px}.budget-grid strong{display:block;font-size:32px;font-weight:350;color:#16181f;letter-spacing:-.05em;margin-top:7px}.provider-budget{border:1px solid var(--line);padding:15px 16px;background:#fafafb}.provider-budget-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}.provider-budget-head h3{font-size:13px;font-weight:550;margin:0;color:#171920}.provider-budget-head span{font-size:9px;color:var(--muted);text-align:right}.provider-budget-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.provider-budget-stats strong{font-size:25px;margin-top:5px}.budget-states{font-size:9px;color:#747985;line-height:1.75;border-top:1px solid #e5e6eb;margin:13px 0 0;padding-top:10px}.footer{border-top:1px solid var(--line);font-size:10px;color:#9699a2;padding:20px 0 30px;margin-top:12px}[hidden]{display:none!important}
@media(min-width:1700px){.layout{padding-right:60px;grid-template-columns:220px minmax(0,1fr);gap:50px}.primary .value{font-size:76px}main>.cards>.card:last-child .value{font-size:40px}.intro h1{font-size:38px}}
@media(max-width:1200px){.layout{grid-template-columns:177px minmax(0,1fr);gap:25px;padding-right:25px;padding-left:19px}.top{padding:0 25px}.top nav{gap:20px}.scope{display:none}.sidebar{padding-right:15px}.card{padding:14px 17px}.primary .value{font-size:52px}main>.cards>.card:nth-child(2) .value{font-size:35px}main>.cards>.card:last-child{padding:21px 20px;margin-left:8px}main>.cards>.card:last-child .value{font-size:28px}}
@media(max-width:1000px){.layout{grid-template-columns:155px minmax(0,1fr);gap:21px;padding-right:20px}.top nav{gap:15px}.top nav a{font-size:10px}.brand{font-size:14px}.intro h1{font-size:27px}.cards{grid-template-columns:1.1fr .9fr}.card:nth-child(2){border-left:1px solid var(--line)}main>.cards>.card:last-child{grid-column:1/-1;margin:20px 0 0;display:grid;grid-template-columns:1fr 1.4fr;gap:4px 20px;padding:21px 24px}main>.cards>.card:last-child:before{position:absolute;left:24px;top:22px;width:5px;height:5px;margin:0}main>.cards>.card:last-child .label{padding-left:18px;align-self:center}main>.cards>.card:last-child .value{grid-column:2;grid-row:1/3;margin:0;align-self:center;font-size:30px}main>.cards>.card:last-child .small{grid-column:1;font-size:9px;margin-top:5px}.news-grid{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}.intro{align-items:flex-start;flex-direction:column;gap:15px}.segment{align-self:flex-start}.section-head{flex-wrap:wrap}#competition-values{grid-template-columns:repeat(3,minmax(0,1fr))}#competition-values .card{padding:0 14px}#competition-values .value{font-size:29px}.budget-grid{gap:25px}}
@media(max-width:720px){:root{--rail:42px}.rail-mark{height:68px}.rail a{height:40px}.rail a svg{width:15px;height:15px}.top{height:69px;padding:0 17px;gap:10px}.top nav{display:none}.scope{display:flex;font-size:8px}.brand{font-size:14px}.identity .sub{font-size:7px}.layout{display:block;padding:22px 17px 0}.sidebar{position:static;border-right:0;border-bottom:1px solid var(--line);padding:0 0 17px;margin-bottom:26px}.sidebar h2{margin-bottom:10px}.sidebar label{display:none}.search{padding:9px 10px}.catalog-count{margin:9px 0}.ingredient-list{max-height:150px;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 8px}.ingredient{padding:8px}.ingredient strong{font-size:10px}.ingredient span{font-size:9px}.intro h1{font-size:29px;margin-top:13px}.intro{gap:15px}.notice{font-size:9px;margin-bottom:19px}.primary .value{font-size:46px}main>.cards>.card:nth-child(2) .value{font-size:34px}.card{padding:12px}.card .label{font-size:9px}.small{font-size:10px}.primary .small{font-size:9px}.decision{font-size:9px}.value{letter-spacing:-.05em}main>.cards>.card:last-child{padding:20px;grid-template-columns:1fr}main>.cards>.card:last-child:before{left:20px}main>.cards>.card:last-child .value{grid-row:auto;grid-column:1;margin:10px 0 8px;font-size:30px}main>.cards>.card:last-child .small{font-size:10px}.panel{margin-top:24px;padding-top:23px}.section-head h2{font-size:17px}.section-head p{font-size:9px}.controls{gap:6px}.controls button{font-size:9px;padding:4px 7px}.chart{height:300px}.google-chart{height:155px}.news-grid{grid-template-columns:1fr}#competition-values{grid-template-columns:1fr;padding:0 15px}#competition-values .card{border-left:0;border-top:1px solid var(--line);padding:16px 0}#competition-values .card:first-child{border-top:0}.grid2{gap:12px}.budget-grid{grid-template-columns:1fr;gap:14px}.budget-grid strong{font-size:30px}.provider-budget-stats strong{font-size:23px}.news-filters{gap:7px}.news-filters select{font-size:10px;padding:7px}.footer{font-size:9px}.rail-bottom{font-size:8px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}button{transition:none}}
</style></head><body>
<nav class="rail" aria-label="快捷导航"><div class="rail-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M4 17V7h5v10M15 17V7h5v10" stroke="#1938f5" stroke-width="2.5"/><path d="M9 12h6" stroke="#1938f5" stroke-width="2.5"/></svg></div><div class="rail-links"><a href="#trend-panel" aria-label="需求趋势" title="需求趋势"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v16h16M7 14l4-5 4 3 5-7"/></svg></a><a href="#backtest-panel" aria-label="历史回测" title="历史回测"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h12v16H6zM9 8h6M9 12h6M9 16h3"/></svg></a><a href="#news-panel" aria-label="市场资讯" title="市场资讯"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM8 9h3v4H8zM14 9h3M14 13h3M8 16h9"/></svg></a><a href="#audit-panel" aria-label="数据审计与预算" title="数据审计与预算"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 4v6c0 4-8 8-8 8s-8-4-8-8V7zM8 12l3 3 5-6"/></svg></a></div><div class="rail-bottom" aria-hidden="true">DEMAND RESEARCH / US</div></nav>
<header class="top"><div class="identity"><div class="logo" aria-hidden="true">↗</div><div><div class="brand">成分趋势研究台</div><div class="sub">INGREDIENT DEMAND · RESEARCH PREVIEW</div></div></div><nav aria-label="报告导航"><a href="#trend-panel">趋势与预测</a><a href="#backtest-panel">历史验证</a><a href="#news-panel">市场动态</a><a href="#audit-panel">数据审计</a></nav><div class="scope">Amazon US · 离线报告</div></header>
<div class="layout"><aside class="sidebar"><h2>成分观察名单</h2><label for="ingredient-search" class="sub">搜索中文名称或核心关键词</label><input id="ingredient-search" class="search" type="search" placeholder="例如：尿石素 / urolithin" autocomplete="off"><div id="catalog-count" class="catalog-count"></div><div id="ingredient-list" class="ingredient-list" aria-label="成分选择"></div></aside>
<main><div class="intro"><div><div class="eyebrow">DEMAND OUTLOOK</div><h1 id="ingredient-title">正在加载报告</h1><p id="ingredient-subtitle"></p></div><div class="segment" role="group" aria-label="预测时长"><button id="horizon-13" data-horizon="13" aria-pressed="true">未来 13 周</button><button id="horizon-26" data-horizon="26" aria-pressed="false">未来 26 周</button></div></div>
<div class="notice">预测对象是美国站关键词的搜索需求，不等于产品销量或盈利。搜索量包含供应商估计；历史下载可能被修订，回测并非完整的当时可见数据重演。</div>
<div class="cards"><div class="card primary"><div class="label" id="forecast-label">未来窗口平均每周搜索量</div><div id="forecast-value" class="value">—</div><div id="forecast-decision" class="decision warning" aria-live="polite"></div><div id="forecast-model" class="small"></div></div><div class="card"><div class="label">相对最近 13 周均值</div><div id="growth-value" class="value">—</div><div id="baseline-value" class="small"></div><div id="growth-evidence-note" class="small"></div></div><div class="card"><div class="label">预测区间与证据</div><div id="interval-value" class="value">—</div><div id="confidence-note" class="small"></div></div></div>
<div id="forecast-window" class="window"></div><div id="diagnostics" class="diagnostics"></div>
<section id="trend-panel" class="panel"><div class="section-head"><div><h2>需求历史</h2><p>拖动图下时间条两端，可调整观察范围；Google 使用独立刻度。</p></div><div class="controls"><button id="range-6m">近 6 个月</button><button id="range-1y">近 1 年</button><button id="range-all">全部</button><label><input id="rank-toggle" type="checkbox"> ABA 排名</label></div></div><div id="history-chart" class="chart" aria-label="亚马逊搜索量历史与可拖动时间范围"></div><div id="google-chart" class="chart google-chart" aria-label="滞后一周的Google热度历史"></div><div class="legend-note">Google 为 0–100 相对热度，面板中已滞后一周；不代表搜索次数。ABA 数字越小排名越高。空缺保留为空缺。</div></section>
<section id="competition-panel" class="panel"><div class="section-head"><div><h2>进入市场的竞争参考</h2><p id="competition-period"></p></div><span class="pill">辅助决策 · 月份快照</span></div><div id="competition-values" class="cards"><div class="card"><div class="label">PPC 建议竞价</div><div id="ppc-value" class="value">—</div><div id="ppc-range" class="small"></div></div><div class="card"><div class="label">点击前三商品 · 点击总占比</div><div id="click-share-value" class="value">—</div><div class="small">衡量搜索点击有多集中</div></div><div class="card"><div class="label">同一组前三商品 · 转化总占比</div><div id="conversion-share-value" class="value">—</div><div class="small">这是购买份额，不是整体转化率</div></div></div><div id="competition-empty" class="empty" hidden>暂无已核对的 PPC 和 ABA 集中度快照。</div><p id="competition-note" class="small"></p><div class="legend-note">PPC 为广告建议出价，不是实际扣费；这里不展示完整周趋势。集中度变化也可能来自头部商品更替。需求增长与能否盈利需要分别评估。</div></section>
<section id="backtest-panel" class="panel"><div class="section-head"><div><h2>过去预测得怎么样</h2><p id="backtest-description">每个点是一处历史预测起点，对比随后整个窗口的平均搜索量。</p></div><span class="pill">按时间回测</span></div><div id="backtest-chart" class="chart" aria-label="历史预测与真实窗口平均值"></div><div id="backtest-empty" class="empty" hidden>这个成分尚无符合条件的完整回测窗口。</div><div id="backtest-summary" class="small"></div><div id="vendor-details" class="vendor-block" hidden><h3>双数据源严格配对回测</h3><p id="vendor-description" class="small"></p><div id="vendor-chart" class="chart vendor-chart" aria-label="两个供应商严格配对的封存回测"></div><div id="vendor-empty" class="empty" hidden></div><div id="vendor-table" class="table-wrap vendor-table"></div><p class="legend-note" id="vendor-note"></p></div><details><summary>查看同一时长的模型评估与比较</summary><div id="metrics-table" class="table-wrap"></div><pre id="metrics-detail"></pre></details></section>
<section id="news-panel" class="panel"><div class="section-head"><div><h2>北美市场动态</h2><p id="news-window"></p></div><span class="pill">新闻范围 ≠ 预测站点</span></div><div class="small">新闻用于追查可能的事件与品牌动作，不作为已证实的增长原因。来源核验不等于产品功效验证。</div><p id="news-coverage" class="small"></p><div class="news-filters"><select id="news-scope" aria-label="新闻成分范围"><option value="ingredient">当前成分</option><option value="all">全部成分</option></select><select id="news-brand" aria-label="新闻品牌筛选"><option value="">全部品牌</option></select><select id="news-evidence" aria-label="新闻证据筛选"><option value="">全部核验状态</option><option value="verified_primary">已核验一手来源</option><option value="verified_secondary">已核验媒体来源</option><option value="unverified">待核验</option></select></div><div id="news-items" class="news-grid"></div><div id="news-empty" class="empty" hidden>此筛选下没有已收集的近三个月北美新闻。没有新闻记录不代表市场没有活动。</div><details id="background-section"><summary id="background-summary">历史背景与无日期资料</summary><div id="background-items" class="news-grid"></div></details></section>
<section id="audit-panel" class="panel"><div class="section-head"><div><h2>数据质量与使用预算</h2><p id="report-asof"></p></div><span class="pill">可追溯</span></div><div id="budget-grid" class="budget-grid"></div><div class="grid2"><div><h3 style="font-size:14px">当前成分的数据说明</h3><ul id="ingredient-warnings" class="list"></ul><details><summary>辅助指标原始快照与稀疏历史审计</summary><pre id="auxiliary-detail"></pre></details></div><div><h3 style="font-size:14px">全局方法与限制</h3><ul id="global-warnings" class="list"></ul></div></div><details id="vendor-global-details" hidden><summary>双数据源配对汇总与审计</summary><div id="vendor-global-table" class="table-wrap"></div><pre id="vendor-audit-detail"></pre></details><details><summary>来源、原始目录与口径审计</summary><pre id="catalog-audit"></pre></details><details><summary>预算原始记录</summary><pre id="budget-detail"></pre></details><details><summary>完整模型方法</summary><pre id="methodology-detail"></pre></details></section><div class="footer">本报告已包含图表程序和数据，可断网打开。访问新闻原文需要联网。预测有不确定性，适合作为选品调研的下一步线索。</div></main></div>
<script>__PLOTLY_JS__</script><script id="analysis-data" type="application/json">__ANALYSIS_JSON__</script>
<script>
'use strict';
const DATA=JSON.parse(document.getElementById('analysis-data').textContent);
const INGREDIENTS=Array.isArray(DATA.ingredients)?DATA.ingredients:[];
const $=id=>document.getElementById(id);
const set=(id,value)=>{$(id).textContent=value??'';};
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const fmt=x=>finite(x)?new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(x):'—';
const pct=x=>finite(x)?(x>0?'+':'')+x.toFixed(1)+'%':'—';
const pretty=x=>JSON.stringify(x??{},null,2);
const reportTimestamp=value=>{if(!value)return '未提供';const d=new Date(value);if(Number.isNaN(d.getTime()))return String(value);return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(d)+'（北京时间，UTC+8）';};
const arr=x=>Array.isArray(x)?x:[];
const str=x=>typeof x==='string'?x:pretty(x);
const modelName=x=>({'baseline':'简单基线','seasonal_naive':'季节性基线','recent_mean':'近期均值基线','last13mean':'最近 13 周均值基线','seasonal':'去年同期基线','ridge_amazon':'Amazon 搜索量模型','ridge_google':'Amazon 搜索量 + Google 模型','ridge_aba':'Amazon 搜索量 + ABA 排名模型','ridge_all':'Amazon 搜索量 + ABA 排名 + Google 模型','ridge':'正则化模型','elastic_net':'弹性网模型'}[x]||x||'模型未提供');
const sharePercent=x=>finite(x)&&x>=0&&x<=1?(x*100).toFixed(1)+'%':'—';
const money=(x,currency='USD')=>{if(!finite(x)||x<0)return '—';const symbol={USD:'$',usd:'$','$':'$',CAD:'CA$',MXN:'MX$'}[currency]||String(currency||'USD')+' ';return symbol+x.toFixed(2);};
let current=INGREDIENTS[0]||null,horizon=13,range=null,chartReady=false;
const chartConfig={responsive:true,displaylogo:false,modeBarButtonsToRemove:['lasso2d','select2d'],toImageButtonOptions:{format:'png',scale:2}};
const baseLayout={font:{family:'system-ui, Microsoft YaHei, sans-serif',size:11,color:'#80848e'},paper_bgcolor:'#fff',plot_bgcolor:'#fff',margin:{l:60,r:28,t:20,b:45},hovermode:'x unified',legend:{orientation:'h',y:1.16,x:0},yaxis:{gridcolor:'#eeeff3',zeroline:false},xaxis:{gridcolor:'#f4f4f7',type:'date'}};
function element(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function safeLink(url){try{const u=new URL(url);return ['http:','https:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function addDays(iso,days){if(!iso)return null;const d=new Date(iso.slice(0,10)+'T00:00:00Z');if(Number.isNaN(d.getTime()))return null;d.setUTCDate(d.getUTCDate()+days);return d.toISOString().slice(0,10);}
function history(){return arr(current?.history).filter(x=>x&&x.date).slice().sort((a,b)=>a.date.localeCompare(b.date));}
function forecast(){return arr(current?.forecasts).find(x=>Number(x.horizon_weeks)===horizon)||{};}
function renderCatalog(){const q=$('ingredient-search').value.trim().toLowerCase();const list=$('ingredient-list');list.replaceChildren();const selected=INGREDIENTS.filter(x=>[x.name_cn,x.keyword,x.id].some(v=>String(v||'').toLowerCase().includes(q)));set('catalog-count',`${selected.length} / ${INGREDIENTS.length} 个成分 · 美国站`);for(const item of selected){const b=element('button','ingredient');b.type='button';b.setAttribute('aria-current',String(item.id===current?.id));b.append(element('strong','',item.name_cn||item.keyword||item.id),element('span','',item.keyword||''));b.addEventListener('click',()=>{current=item;range=null;renderCatalog();renderCurrent();});list.append(b);}if(!selected.length)list.append(element('div','empty','没有匹配的成分'));}
function pill(text,warn){return element('span','pill'+(warn?' warning':''),text);}
function renderForecastDecision(f,valid){
 const proven=valid&&f.has_proven_gain===true;
 const label=valid?(f.decision_label||'探索性结果：尚未证实稳定优于简单基准'):'数据不足：当前不作增长判断';
 set('forecast-decision',label);
 $('forecast-decision').className='decision '+(proven?'supported':'warning');
 $('growth-value').className='value '+(!valid?'muted':proven&&finite(f.growth_pct)&&f.growth_pct>=0?'rise':'fall');
 set('growth-evidence-note',valid&&!proven?'增长幅度为探索性估计，尚无稳定增益证据。':valid?'后期比较有改善，仍需真实前瞻验证。':'');
}
function renderForecast(){const f=forecast(),d=current?.diagnostics||{},h=history(),valid=f.status!=='insufficient_data'&&finite(f.predicted_mean);set('ingredient-title',current?.name_cn||current?.keyword||'暂无成分数据');set('ingredient-subtitle',current?`${current.keyword||''} · Amazon ${current.marketplace||'US'} · 需求研究`:'请先完成数据采集与分析。');set('forecast-label',`未来 ${horizon} 周平均每周搜索量`);set('forecast-value',valid?fmt(f.predicted_mean):'数据不足');set('forecast-model',valid?`预测模型：${modelName(f.model)}`:'当前证据不足，未提供数值预测');set('growth-value',valid?pct(f.growth_pct):'—');renderForecastDecision(f,valid);set('baseline-value',`最近 13 周均值：${fmt(f.baseline)}`);const interval=valid&&finite(f.lower)&&finite(f.upper);set('interval-value',interval?`${fmt(f.lower)} – ${fmt(f.upper)}`:'区间暂不可用');set('confidence-note',interval?`校准样本 ${fmt(f.n_calibration)} 个；区间覆盖率需由回测检查，不代表“会火”的概率。`:'没有足够的历史残差或预测数据；不输出虚构置信度。');const cutoff=DATA.as_of||h.at(-1)?.date;set('forecast-window',valid?`目标窗口：${addDays(cutoff,7)||'—'} 至 ${addDays(cutoff,horizon*7)||'—'}。这里预测整个窗口的每周均值，不是逐周路径。`:`未来 ${horizon} 周：${f.status||'尚无可用预测'}。短历史、缺失或回测样本不足时，请先补充数据。`);const diag=$('diagnostics');diag.replaceChildren();diag.append(pill(`历史 ${fmt(d.history_weeks??h.length)} 周`),pill(`缺失 ${fmt(d.missing_weeks)} 周`,finite(d.missing_weeks)&&d.missing_weeks>0),pill(`校准 ${fmt(f.n_calibration)} 个窗口`,!finite(f.n_calibration)||f.n_calibration<20));if(f.status&&f.status!=='ok')diag.append(pill(f.status,true));}
function emptyChart(id,message){Plotly.purge($(id));$(id).replaceChildren(element('div','empty',message));}
function renderHistory(){const h=history();chartReady=false;if(!h.length){emptyChart('history-chart','没有可用的亚马逊历史数据。');emptyChart('google-chart','没有可用的 Google 趋势数据。');return;}const x=h.map(r=>r.date),traces=[{x,y:h.map(r=>finite(r.searches)?r.searches:null),name:'Amazon 周搜索量',type:'scatter',mode:'lines',line:{color:'#1c2029',width:1.4},fill:'tozeroy',fillcolor:'rgba(25,56,245,.025)',connectgaps:false,hovertemplate:'%{y:,.0f} 次<extra>Amazon 周搜索量</extra>'}];if($('rank-toggle').checked)traces.push({x,y:h.map(r=>finite(r.rank)?r.rank:null),name:'ABA 排名（越小越高）',type:'scatter',mode:'lines',yaxis:'y2',line:{color:'#1938f5',width:1.5,dash:'dot'},connectgaps:false,hovertemplate:'排名 %{y:,.0f}<extra>ABA</extra>'});const layout={...baseLayout,margin:{l:60,r:$('rank-toggle').checked?65:25,t:35,b:25},xaxis:{...baseLayout.xaxis,rangeslider:{visible:true,thickness:.15,bgcolor:'#f6f7fb',bordercolor:'#e3e6ef',borderwidth:1},...(range?{range}: {})},yaxis:{...baseLayout.yaxis,title:{text:'每周搜索次数',font:{size:10}}},yaxis2:{overlaying:'y',side:'right',autorange:'reversed',showgrid:false,title:{text:'ABA 排名',font:{size:10}}},legend:{...baseLayout.legend,y:1.15}};Plotly.react($('history-chart'),traces,layout,chartConfig).then(()=>{chartReady=true;$('history-chart').removeAllListeners?.('plotly_relayout');$('history-chart').on('plotly_relayout',ev=>{if(ev['xaxis.range'])range=ev['xaxis.range'];else if(ev['xaxis.range[0]'])range=[ev['xaxis.range[0]'],ev['xaxis.range[1]']];else if(ev['xaxis.autorange'])range=null;else return;if(h.some(r=>finite(r.google_trend)))Plotly.relayout($('google-chart'),range?{'xaxis.range':range}:{'xaxis.autorange':true});});});const google=h.some(r=>finite(r.google_trend));if(google)Plotly.react($('google-chart'),[{x,y:h.map(r=>finite(r.google_trend)?r.google_trend:null),name:'Google 热度（滞后 1 周）',type:'scatter',mode:'lines',line:{color:'#1938f5',width:1.7},connectgaps:false,hovertemplate:'热度 %{y:.1f}<extra>Google · 滞后 1 周</extra>'}],{...baseLayout,margin:{l:60,r:25,t:32,b:32},xaxis:{...baseLayout.xaxis,...(range?{range}:{})},yaxis:{...baseLayout.yaxis,range:[0,100],title:{text:'Google 0–100',font:{size:10}}},legend:{orientation:'h',y:1.45}},chartConfig);else emptyChart('google-chart','该成分尚无可用的 Google 周热度，模型不应把缺失当作零。');}
function setRange(days){const h=history();if(!h.length||!chartReady)return;range=days?[addDays(h.at(-1).date,-days),h.at(-1).date]:null;Plotly.relayout($('history-chart'),range?{'xaxis.range':range}:{'xaxis.autorange':true});if(h.some(r=>finite(r.google_trend)))Plotly.relayout($('google-chart'),range?{'xaxis.range':range}:{'xaxis.autorange':true});}
function backtests(){const local=arr(current?.backtests);return (local.length?local:arr(DATA.backtests).filter(r=>r.ingredient_id===current?.id||r.id===current?.id)).filter(r=>Number(r.horizon_weeks)===horizon&&finite(r.actual)&&finite(r.predicted)).sort((a,b)=>String(a.origin).localeCompare(String(b.origin)));}
function renderBacktest(){const rows=backtests();$('backtest-chart').hidden=!rows.length;$('backtest-empty').hidden=!!rows.length;set('backtest-description',`每个点以历史预测起点为横轴，比较随后 ${horizon} 周的平均周搜索量；不是某一周销量。`);if(rows.length){const groups=new Map();for(const row of rows){const key=row.model||'model';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);}const uniq=[...new Map(rows.map(r=>[r.origin,r])).values()];const traces=[{x:uniq.map(r=>r.origin),y:uniq.map(r=>r.actual),name:'随后窗口的真实均值',mode:'lines+markers',line:{color:'#181b23',width:2},marker:{size:5},hovertemplate:'%{y:,.0f}<extra>真实窗口均值</extra>'}];if(!groups.has('last13mean')&&uniq.some(r=>finite(r.baseline)))traces.push({x:uniq.map(r=>r.origin),y:uniq.map(r=>r.baseline),name:'最近 13 周均值基线',mode:'lines',line:{color:'#b5b9c4',width:1.4,dash:'dash'},hovertemplate:'%{y:,.0f}<extra>近期均值基线</extra>'});let i=0;for(const [name,values]of groups){traces.push({x:values.map(r=>r.origin),y:values.map(r=>r.predicted),name:modelName(name),mode:'lines+markers',line:{color:['#1938f5','#8592c6','#9b8467'][i++%3],width:1.8,dash:'dot'},marker:{size:4},hovertemplate:'%{y:,.0f}<extra>'+modelName(name).replace(/[<>&]/g,'')+'</extra>'});}Plotly.react($('backtest-chart'),traces,{...baseLayout,margin:{l:65,r:20,t:48,b:52},xaxis:{...baseLayout.xaxis,title:'历史预测起点'},yaxis:{...baseLayout.yaxis,title:'后续窗口的平均周搜索量'},legend:{orientation:'h',y:1.25}},chartConfig);set('backtest-summary',`${uniq.length} 个历史起点，${groups.size} 种模型记录。相邻窗口可能重叠，不能将其视作完全独立样本。`);}else{Plotly.purge($('backtest-chart'));set('backtest-summary','暂无已成熟的历史预测标签，暂不能判断预测准确度。');}renderMetrics();}
function renderMetrics(){
 const metrics=DATA.metrics||{},rows=arr(metrics.summary).filter(r=>!r.horizon_weeks||Number(r.horizon_weeks)===horizon),f=forecast();
 const splits={validation:'较早验证集',sealed_test:'后期测试集',group_validation:'未见成分 · 验证',group_sealed_test:'未见成分 · 测试'};
 const labels={horizon_weeks:'跨度（周）',split:'数据集',model:'模型',n:'样本窗口',n_origins:'历史起点',mae:'平均绝对误差',wape:'总量加权误差',rmse:'均方根误差',direction_accuracy:'方向判断正确率',interval_coverage:'区间覆盖率',n_intervals:'有效区间数'};
 const ratioKeys=new Set(['wape','direction_accuracy','interval_coverage']);
 const percent=x=>finite(x)?(x*100).toFixed(1)+'%':'—';
 const host=$('metrics-table');host.replaceChildren();
 if(rows.length){const keys=Object.keys(labels).filter(k=>rows.some(r=>r[k]!==undefined));const table=element('table'),thead=element('thead'),tr=element('tr');for(const k of keys)tr.append(element('th','',labels[k]));thead.append(tr);const tbody=element('tbody');for(const r of rows){const row=element('tr');for(const k of keys){const value=k==='model'?modelName(r[k]):k==='split'?(splits[r[k]]||r[k]):ratioKeys.has(k)?percent(r[k]):finite(r[k])?fmt(r[k]):str(r[k]??'—');row.append(element('td','',value));}tbody.append(row);}table.append(thead,tbody);host.append(table);}else host.append(element('div','small','暂无汇总评估指标。'));
 let score=$('performance-cards');if(!score){score=element('div','');score.id='performance-cards';$('backtest-summary').after(score);}score.replaceChildren();
 const split=current?.diagnostics?.heldout?'group_sealed_test':'sealed_test';
 const chosen=rows.find(r=>r.model===f.model&&r.split===split);
 if(chosen){const label=element('p','small',`本批成分的${splits[split]} · ${modelName(f.model)} · ${fmt(chosen.n)} 个窗口（不是当前成分单独的准确率）`);score.append(label);const grid=element('div','diagnostics');for(const [label,value]of [['总量加权误差',percent(chosen.wape)],['方向判断正确率',percent(chosen.direction_accuracy)],['经验区间覆盖率',percent(chosen.interval_coverage)]])grid.append(pill(`${label} ${value}`));score.append(grid,element('p','small','误差越低越好；方向分为上升 / 持平 / 下降（±10% 为持平），不能解释为爆款概率。'));}
 else score.append(element('p','small','当前模型尚无对应后期测试集结果，不展示准确率承诺。'));
 set('metrics-detail',pretty({paired_comparisons:metrics.paired_comparisons,selected_models:metrics.selected_models,...(!metrics.summary?{metrics}: {})}));
}
function newsList(background=false){const news=DATA.news||{};return arr(background?news.background_items:news.items).filter(item=>{const ids=arr(item.ingredient_ids);return $('news-scope').value==='all'||(current&&ids.includes(current.id));});}
function newsCard(item,background=false){const card=element('article','news-card'),meta=element('div','news-meta');meta.append(element('span','',item.published_at||'日期未提供'),element('span','',item.region||'地区未注明'));const evidence={verified_primary:'已核验一手来源',verified_secondary:'已核验媒体来源',unverified:'待核验'};meta.append(element('span','evidence'+(!evidence[item.evidence]||item.evidence==='unverified'?' unverified':''),evidence[item.evidence]||'待核验'));const kinds={brand_announcement:'品牌公告 · 含商业推广',brand_education:'品牌科普 · 含商业推广',independent_media:'独立媒体',research:'研究来源',official:'官方机构'};const kind=element('span','pill'+(['brand_announcement','brand_education'].includes(item.source_kind)?' warning':''),kinds[item.source_kind]||item.category||'来源类型未标注');meta.append(kind);if(item.category&&item.category!==kinds[item.source_kind])meta.append(element('span','',item.category));const title=element('h3');const url=safeLink(item.url);if(url){const a=element('a','',item.title||'未命名资料');a.href=url;a.target='_blank';a.rel='noopener noreferrer';title.append(a);}else title.textContent=item.title||'未命名资料';card.append(meta,title,element('p','',item.summary||'未提供摘要'));if(item.relevance_note)card.append(element('p','',item.relevance_note));if(background)card.append(element('p','',item.background_reason||'背景资料'));card.append(element('div','news-footer',`${item.source||'来源未注明'}${arr(item.brands).length?' · '+item.brands.join(' / '):''}${url?'':' · 未提供可安全访问的来源链接'}`));return card;}
function renderNews(resetBrands=false){const news=DATA.news||{};set('news-window',`${news.window_start||'—'} 至 ${news.window_end||'—'} · 美国 / 加拿大 / 墨西哥及有明确北美关联的事件`);set('news-coverage',news.coverage?.note||'具体国家覆盖情况未提供；美国站预测不代表整个北美市场。');const list=newsList();if(resetBrands){const old=$('news-brand').value;$('news-brand').replaceChildren(element('option','','全部品牌'));$('news-brand').firstChild.value='';const brands=[...new Set(list.flatMap(x=>arr(x.brands)))].sort();for(const b of brands){const o=element('option','',b);o.value=b;$('news-brand').append(o);}if(brands.includes(old))$('news-brand').value=old;}const brand=$('news-brand').value,evidence=$('news-evidence').value;const visible=list.filter(x=>(!brand||arr(x.brands).includes(brand))&&(!evidence||(x.evidence||'unverified')===evidence));$('news-items').replaceChildren(...visible.map(x=>newsCard(x)));$('news-empty').hidden=!!visible.length;const bg=newsList(true);$('background-items').replaceChildren(...bg.map(x=>newsCard(x,true)));set('background-summary',`历史背景与无日期资料（${bg.length} 条，不计入近期新闻）`);$('background-section').hidden=!bg.length;}
function warningStrings(value){if(Array.isArray(value))return value.map(str);if(value&&typeof value==='object')return Object.entries(value).map(([k,v])=>`${k}：${str(v)}`);return value?[str(value)]:[];}
function providerBudgetEntries(value){
 const nested=value&&typeof value==='object'&&!Array.isArray(value)&&value.providers;
 const source=nested&&typeof nested==='object'?nested:value;
 let pairs=[];
 if(Array.isArray(source))pairs=source.map((record,index)=>[record?.provider||String(index),record]);
 else if(source&&typeof source==='object')pairs=source.provider&&Object.prototype.hasOwnProperty.call(source,'hard_limit')?[[source.provider,source]]:Object.entries(source);
 const order={sellersprite:0,sif:1};
 return pairs.filter(([,record])=>record&&typeof record==='object'&&!Array.isArray(record)).map(([key,record])=>({key:String(record.provider||key).toLowerCase(),record})).sort((a,b)=>(order[a.key]??99)-(order[b.key]??99)||a.key.localeCompare(b.key));
}
function budgetProviderLabel(value){return ({sellersprite:'SellerSprite（卖家精灵）',sif:'SIF'}[String(value||'').toLowerCase()]||String(value||'未知供应商'));}
function budgetStatesText(states){
 if(!states||typeof states!=='object'||Array.isArray(states)||!Object.keys(states).length)return '状态：暂无记录';
 const labels={reserved:'已预留',calling:'调用中',received:'已接收',staged:'已暂存',saved:'已保存',failed_before_send:'发送前失败',failed_provider:'供应商失败',failed_validation:'校验失败',failed_storage:'存储失败'};
 return '状态：'+Object.entries(states).map(([state,value])=>{const prefix=`${labels[state]||state}（${state}）`;return value&&typeof value==='object'?`${prefix} ${fmt(value.units)} 单位 / ${fmt(value.jobs)} 项`:`${prefix} ${finite(value)?fmt(value):str(value)}`;}).join('；');
}
function renderBudget(){
 const grid=$('budget-grid'),hasProviderBudgets=Object.prototype.hasOwnProperty.call(DATA,'provider_budgets')&&DATA.provider_budgets!==null;
 const raw={provider_budgets:DATA.provider_budgets??null,legacy_budget:DATA.budget??null};
 set('budget-detail',pretty(raw));grid.replaceChildren();
 if(hasProviderBudgets){
  const rows=providerBudgetEntries(DATA.provider_budgets);
  for(const {key,record} of rows){
   const card=element('section','provider-budget'),head=element('div','provider-budget-head');
   head.append(element('h3','',budgetProviderLabel(key)),element('span','',`${key.toUpperCase()}${record.period?' · '+record.period:''}`));
   const stats=element('div','provider-budget-stats');
   for(const [field,label]of [['hard_limit','硬上限'],['counted_units_including_legacy','已计费用量（含旧账本）'],['remaining','剩余']]){const item=element('div','small',label);item.append(element('strong','',fmt(record[field])));stats.append(item);}
   card.append(head,stats,element('p','budget-states',budgetStatesText(record.states)));grid.append(card);
  }
  if(!rows.length)grid.append(element('div','small','provider_budgets 已提供，但没有可显示的供应商预算记录。'));
  return;
 }
 const b=DATA.budget||{},fields=[['project_limit','本项目调用上限'],['charged_attempts','已计费尝试'],['remaining_project','项目剩余额度'],['estimated_remaining_account','账户剩余（估计）']];
 for(const [key,label]of fields)if(b[key]!==undefined){const div=element('div','small',label);div.append(element('strong','',fmt(b[key])));grid.append(div);}
 if(!grid.children.length)grid.append(element('div','small','暂无预算统计。'));
}
function renderAudit(){const d=current?.diagnostics||{};const warnings=[...warningStrings(d.warnings),...warningStrings(current?.warnings)];if(finite(d.missing_weeks)&&d.missing_weeks>0)warnings.unshift(`缺少 ${d.missing_weeks} 周记录；搜索量缺失不填为 0。`);if(!history().some(r=>finite(r.google_trend)))warnings.push('Google 趋势缺失；不可宣称已利用 Google 信号。');if(!warnings.length)warnings.push('本次未报告额外缺失警示；仍受供应商估计、修订历史和模型限制影响。');$('ingredient-warnings').replaceChildren(...warnings.map(x=>element('li','',x)));const aux=DATA.auxiliary?.[current?.id]||DATA.auxiliary?.[current?.keyword];set('auxiliary-detail',aux?pretty(aux):'ABA 集中度 / PPC / TikTok：本成分暂无已整合的辅助证据。');set('report-asof',`模型数据截止 ${DATA.as_of||'未提供'} · 报告日期 ${DATA.report_as_of||'未提供'} · 报告生成 ${reportTimestamp(DATA.report_generated_at||DATA.generated_at)}`);$('global-warnings').replaceChildren(...warningStrings(DATA.warnings).map(x=>element('li','',x)));if(!$('global-warnings').children.length)$('global-warnings').append(element('li','','未提供全局警示详情，请查看完整模型方法。'));set('catalog-audit',pretty(DATA.catalog_audit||{message:'目录来源审计尚未提供'}));renderBudget();set('methodology-detail',pretty(DATA.methodology||{message:'方法详情尚未提供'}));}
function auxiliary(){return DATA.auxiliary?.[current?.id]||DATA.auxiliary?.[current?.keyword]||{};}
function renderCompetition(){
 const aux=auxiliary(),s=aux.recent_snapshot;
 const valid=s&&typeof s==='object';
 $('competition-values').hidden=!valid;$('competition-empty').hidden=!!valid;
 set('competition-period',valid?`数据月份 ${s.period||'未标注'} · 采集 ${s.observed_at||'未标注'} · Amazon ${current?.marketplace||'US'}`:'当前成分尚无已核对的竞争指标快照。');
 if(valid){set('ppc-value',money(s.ppc_bid,s.currency));set('ppc-range',`建议范围 ${money(s.ppc_min,s.currency)} – ${money(s.ppc_max,s.currency)}`);set('click-share-value',sharePercent(s.click_share));set('conversion-share-value',sharePercent(s.conversion_share));}
 set('competition-note',valid?(s.note||'历史月份的指标快照；字段具体采样时间以供应商说明为准。'):'这些辅助数据缺失时，需求预测仍可运行；不能将缺失视为低竞争。');
}
function renderVendorSummary(){
 const vc=DATA.vendor_comparison;
 const block=$('vendor-details'),chart=$('vendor-chart'),empty=$('vendor-empty'),host=$('vendor-table');
 const globalDetails=$('vendor-global-details'),globalHost=$('vendor-global-table');
 Plotly.purge(chart);host.replaceChildren();globalHost.replaceChildren();
 chart.hidden=true;empty.hidden=true;block.hidden=!vc;globalDetails.hidden=!vc;
 set('vendor-description','');set('vendor-note','');
 if(!vc)return;
 const providerLabel=value=>({sellersprite:'卖家精灵',sif:'SIF'})[String(value||'').toLowerCase()]||String(value||'未知数据源');
 const primaryProvider=vc.primary_provider||vc.providers?.primary;
 const secondaryProvider=vc.secondary_provider||vc.providers?.secondary;
 const primaryLabel=providerLabel(primaryProvider),secondaryLabel=providerLabel(secondaryProvider);
 set('vendor-audit-detail',pretty({status:vc.status,reasons:vc.reasons,warnings:vc.warnings,providers:vc.providers,selected_models:vc.selected_models,models_compared:vc.models_compared,audit:vc.audit}));
 const percent2=x=>finite(x)?(x*100).toFixed(2)+'%':'—';
 const splitName=x=>({sealed_test:'后期测试集',group_sealed_test:'未见成分测试集'}[x]||x);
 const appendTable=(target,headers,rows)=>{const table=element('table'),thead=element('thead'),head=element('tr');for(const label of headers)head.append(element('th','',label));thead.append(head);const tbody=element('tbody');for(const values of rows){const tr=element('tr');for(const value of values)tr.append(element('td','',value));tbody.append(tr);}table.append(thead,tbody);target.append(table);};
 if(vc.status!=='ok'){
  chart.hidden=true;empty.hidden=false;set('vendor-empty',`双数据源比较不可用：${arr(vc.reasons).join('；')||vc.status}`);
  globalHost.append(element('div','small',`比较状态：${vc.status}。${arr(vc.reasons).join('；')}`));
  return;
 }
 const currentData=vc.per_ingredient?.[current?.id]?.[String(horizon)]||{};
 const entries=[];
 for(const [split,models]of Object.entries(currentData))for(const [model,series]of Object.entries(models||{}))entries.push({split,model,series});
 if(!entries.length){
  empty.hidden=false;set('vendor-empty',`当前成分在 ${horizon} 周跨度下没有严格配对的双数据源封存窗口。`);
 }else{
  const preferred=vc.selected_models?.[String(horizon)]?.primary;
  const chosen=entries.find(x=>x.model===preferred)||entries.find(x=>x.model==='last13mean')||entries[0];
  const s=chosen.series,x=arr(s.origins),safeModel=modelName(chosen.model).replace(/[<>&]/g,'');
  const traces=[
   {x,y:arr(s.primary?.actual),name:`${primaryLabel} · 真实值`,mode:'lines+markers',line:{color:'#1938f5',width:2},marker:{size:5,symbol:'circle'},hovertemplate:`%{y:,.0f}<extra>${primaryLabel} · 真实值</extra>`},
   {x,y:arr(s.primary?.predicted),name:`${primaryLabel} · ${safeModel}`,mode:'lines+markers',line:{color:'#1938f5',width:1.7,dash:'dash'},marker:{size:4,symbol:'circle-open'},hovertemplate:`%{y:,.0f}<extra>${primaryLabel} · 预测</extra>`},
   {x,y:arr(s.secondary?.actual),xaxis:'x2',yaxis:'y2',name:`${secondaryLabel} · 真实值`,mode:'lines+markers',line:{color:'#e07f1a',width:2},marker:{size:5,symbol:'square'},hovertemplate:`%{y:,.0f}<extra>${secondaryLabel} · 真实值</extra>`},
   {x,y:arr(s.secondary?.predicted),xaxis:'x2',yaxis:'y2',name:`${secondaryLabel} · ${safeModel}`,mode:'lines+markers',line:{color:'#e07f1a',width:1.7,dash:'dash'},marker:{size:4,symbol:'square-open'},hovertemplate:`%{y:,.0f}<extra>${secondaryLabel} · 预测</extra>`}
  ];
  chart.hidden=false;
  Plotly.react(chart,traces,{...baseLayout,margin:{l:70,r:24,t:58,b:50},hovermode:'x unified',xaxis:{...baseLayout.xaxis,domain:[0,1],anchor:'y',showticklabels:false},yaxis:{...baseLayout.yaxis,domain:[.57,1],title:{text:`${primaryLabel} 搜索量`,font:{size:10}}},xaxis2:{...baseLayout.xaxis,domain:[0,1],anchor:'y2',matches:'x',title:'严格配对的历史预测起点'},yaxis2:{...baseLayout.yaxis,domain:[0,.43],title:{text:`${secondaryLabel} 搜索量`,font:{size:10}}},legend:{orientation:'h',y:1.14}},chartConfig);
  set('vendor-description',`${primaryLabel}（上）与 ${secondaryLabel}（下）· ${splitName(chosen.split)} · ${safeModel} · ${x.length} 个严格配对窗口；上下图共享相同预测起点、使用独立刻度。`);
  appendTable(host,['评估集','模型','配对窗口',`${primaryLabel} WAPE`,`${secondaryLabel} WAPE`],entries.map(({split,model,series})=>[splitName(split),modelName(model),fmt(series.origins?.length),percent2(series.primary?.wape),percent2(series.secondary?.wape)]));
  set('vendor-note','只使用两侧都存在、键完全一致的预测起点。上下图的绝对搜索量不可互相换算；WAPE 较低只表示该估算序列在这个模型下更易预测，不代表供应商更接近真实市场。');
 }
 const summary=vc.summary?.[String(horizon)]||{},globalRows=[];
 for(const [split,models]of Object.entries(summary))for(const [model,metric]of Object.entries(models||{}))globalRows.push([splitName(split),modelName(model),fmt(metric.n_pairs),fmt(metric.macro_n_ingredients),percent2(metric.primary?.macro_wape),percent2(metric.secondary?.macro_wape),percent2(metric.primary?.wape),percent2(metric.secondary?.wape)]);
 if(globalRows.length)appendTable(globalHost,['评估集','模型','配对窗口','宏平均成分数',`${primaryLabel} 宏平均 WAPE`,`${secondaryLabel} 宏平均 WAPE`,`${primaryLabel} pooled WAPE`,`${secondaryLabel} pooled WAPE`],globalRows);
 else globalHost.append(element('div','small',`未来 ${horizon} 周没有严格配对的全体成分汇总。`));
}
function renderCurrent(){renderForecast();renderCompetition();renderHistory();renderBacktest();renderVendorSummary();renderNews(true);renderAudit();}
$('ingredient-search').addEventListener('input',renderCatalog);
document.querySelectorAll('[data-horizon]').forEach(b=>b.addEventListener('click',()=>{horizon=Number(b.dataset.horizon);document.querySelectorAll('[data-horizon]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));renderForecast();renderBacktest();renderVendorSummary();}));
$('rank-toggle').addEventListener('change',renderHistory);$('range-6m').addEventListener('click',()=>setRange(183));$('range-1y').addEventListener('click',()=>setRange(365));$('range-all').addEventListener('click',()=>setRange(null));$('news-scope').addEventListener('change',()=>renderNews(true));$('news-brand').addEventListener('change',()=>renderNews());$('news-evidence').addEventListener('change',()=>renderNews());renderCatalog();renderCurrent();
</script></body></html>'''
