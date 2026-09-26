"""Reproducible, past-only ingredient forecasting and rolling evaluation.

The target is the mean estimated Amazon weekly search volume over the next
13/26 weeks. Historical API snapshots are not point-in-time archives.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .common import read_json, sha256


HORIZONS = (13, 26)
MIN_HISTORY = 104
ORIGIN_STEP = 4
TRAIN_STEP = 4
CALENDAR_ANCHOR = pd.Timestamp("1970-01-03")  # Saturday; fixes every 4-week grid globally.
MIN_TRAIN_ROWS = 40
MIN_TRAIN_GROUPS = 4
MIN_TRAIN_ORIGINS = 4
MIN_CALIBRATION = 20
AMAZON_FEATURES = (
    "level_log", "short_momentum", "quarter_momentum",
    "halfyear_momentum", "seasonal_momentum", "volatility",
)
GOOGLE_FEATURES = ("google_level", "google_short", "google_quarter")
ABA_FEATURES = ("rank_log", "rank_momentum")
MODEL_FEATURES = {
    "ridge_amazon": AMAZON_FEATURES,
    "ridge_google": AMAZON_FEATURES + GOOGLE_FEATURES,
    "ridge_aba": AMAZON_FEATURES + ABA_FEATURES,
    "ridge_all": AMAZON_FEATURES + ABA_FEATURES + GOOGLE_FEATURES,
}
MODELS = ("last13mean", "seasonal", *MODEL_FEATURES)
ABLATIONS = {
    "google_ablation_matched_cases": ("ridge_amazon", "ridge_google"),
    "aba_ablation_matched_cases": ("ridge_amazon", "ridge_aba"),
    "google_ablation_with_aba_matched_cases": ("ridge_aba", "ridge_all"),
    "aba_ablation_with_google_matched_cases": ("ridge_google", "ridge_all"),
}
MODEL_VERSION = "ridge-demand-v1.3.0"


def _is_heldout(family_id: str) -> bool:
    """Stable assignment; adding ingredients never moves existing families."""
    return int(hashlib.sha256(str(family_id).encode()).hexdigest()[:8], 16) % 5 == 0


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _find_panel_manifest(panel_path: str | Path, manifest_path: str | Path | None = None):
    panel_path = Path(panel_path)
    candidates = ([Path(manifest_path)] if manifest_path else []) + [
        panel_path.parent / "manifest.json", panel_path.with_suffix(".manifest.json")]
    for candidate in candidates:
        if candidate.exists():
            manifest = read_json(candidate)
            if manifest.get("schema_version") != "2.0":
                raise ValueError("panel manifest must use schema_version 2.0")
            if manifest.get("panel_sha256") != sha256(panel_path):
                raise ValueError("panel SHA256 does not match its manifest")
            return manifest, candidate
    return None, None


def _load_panel(panel_path: str | Path, manifest: dict | None = None) -> pd.DataFrame:
    panel = pd.read_csv(panel_path, dtype={"ingredient_id": str, "family_id": str})
    required = {"ingredient_id", "week_end", "searches"}
    if not required.issubset(panel):
        raise ValueError(f"panel missing columns: {sorted(required - set(panel))}")
    panel["week_end"] = pd.to_datetime(panel["week_end"], errors="raise").dt.normalize()
    if panel["week_end"].isna().any() or panel["ingredient_id"].isna().any():
        raise ValueError("ingredient_id and week_end must be present")
    if (panel["week_end"].dt.dayofweek != 5).any():
        raise ValueError("week_end must be the Saturday ending an Amazon week")
    if panel.duplicated(["ingredient_id", "week_end"]).any():
        raise ValueError("duplicate ingredient/week rows must be resolved before modeling")
    for column in ("searches", "rank", "google_trend"):
        if column not in panel:
            panel[column] = np.nan
        panel[column] = pd.to_numeric(panel[column], errors="coerce")
        panel.loc[~np.isfinite(panel[column]), column] = np.nan
    panel.loc[panel["searches"] < 0, "searches"] = np.nan
    panel.loc[panel["rank"] <= 0, "rank"] = np.nan
    panel.loc[~panel["google_trend"].between(0, 100), "google_trend"] = np.nan
    defaults = {"keyword": "", "name_cn": "", "marketplace": "US"}
    for column, default in defaults.items():
        if column not in panel:
            panel[column] = default
        panel[column] = panel[column].fillna(default).astype(str)
    if "family_id" not in panel:
        panel["family_id"] = panel["ingredient_id"]
    panel["family_id"] = panel["family_id"].fillna(panel["ingredient_id"]).astype(str)
    if panel.groupby("ingredient_id")["family_id"].nunique().gt(1).any():
        raise ValueError("an ingredient cannot belong to multiple families")
    if panel.groupby("ingredient_id")["marketplace"].nunique().gt(1).any():
        raise ValueError("an ingredient_id must identify one marketplace")
    if len(panel) and set(panel["marketplace"]) != {"US"}:
        raise ValueError("v1 shared model requires the US marketplace only")
    if "amazon_provider" in panel and "amazon_source" in panel:
        left = panel["amazon_provider"].fillna("").astype(str).str.casefold()
        right = panel["amazon_source"].fillna("").astype(str).str.casefold()
        if not left.equals(right):
            raise ValueError("amazon_provider and legacy amazon_source disagree")
    if "amazon_provider" not in panel:
        if "amazon_source" in panel:
            panel["amazon_provider"] = panel["amazon_source"]
        elif manifest:
            panel["amazon_provider"] = manifest.get("amazon_provider")
        else:
            panel["amazon_provider"] = "sellersprite"
    panel["amazon_provider"] = panel["amazon_provider"].fillna("").astype(str).str.casefold()
    providers = set(panel["amazon_provider"])
    if not providers <= {"sellersprite", "sif"} or len(providers) != 1:
        raise ValueError("modeling requires exactly one supported Amazon provider per panel")
    if panel.groupby("ingredient_id")["amazon_provider"].nunique().gt(1).any():
        raise ValueError("an ingredient cannot mix Amazon providers")
    if manifest:
        provider = next(iter(providers))
        if manifest.get("amazon_provider") != provider:
            raise ValueError("panel provider does not match its manifest")
        if manifest.get("provider_policy") != "single_amazon_provider":
            raise ValueError("panel manifest does not declare the single-provider policy")
        if manifest.get("rows") != len(panel) or manifest.get("ingredients") != panel["ingredient_id"].nunique():
            raise ValueError("panel row or ingredient counts do not match its manifest")
    return panel.sort_values(["ingredient_id", "week_end"]).reset_index(drop=True)


def _weekly_series(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if "amazon_provider" not in panel:
        panel = panel.copy()
        panel["amazon_provider"] = "sellersprite"
    series = {}
    for ingredient_id, group in panel.groupby("ingredient_id", sort=True):
        frame = group.set_index("week_end").sort_index()
        dates = pd.date_range(frame.index.min(), frame.index.max(), freq="W-SAT")
        frame = frame.reindex(dates)
        for field in ("ingredient_id", "keyword", "name_cn", "marketplace", "family_id", "amazon_provider"):
            frame[field] = group[field].iloc[0]
        series[str(ingredient_id)] = frame
    return series


def _features_at(frame: pd.DataFrame, origin: pd.Timestamp) -> dict | None:
    """Reads only observations <= origin, including deliberately lagged Google."""
    past = frame.loc[:origin]
    if len(past) < MIN_HISTORY or past.index[-1] != origin:
        return None
    s = past["searches"].iloc[-MIN_HISTORY:]
    if s.isna().any():
        return None
    mean13 = float(s.iloc[-13:].mean())
    log13 = np.log1p(mean13)
    row = {
        "baseline": mean13,
        "level_log": log13,
        "short_momentum": np.log1p(s.iloc[-4:].mean()) - log13,
        "quarter_momentum": log13 - np.log1p(s.iloc[-26:-13].mean()),
        "halfyear_momentum": np.log1p(s.iloc[-26:].mean()) - np.log1p(s.iloc[-52:-26].mean()),
        "seasonal_momentum": log13 - np.log1p(s.iloc[-65:-52].mean()),
        "volatility": float(np.log1p(s.iloc[-26:]).std(ddof=0)),
    }
    g = past["google_trend"].iloc[-26:]
    google_ok = (g.iloc[-4:].notna().sum() >= 3 and
                 g.iloc[-13:].notna().sum() >= 10 and
                 g.iloc[:13].notna().sum() >= 10)
    row["google_available"] = bool(google_ok)
    for name in GOOGLE_FEATURES:
        row[name] = np.nan
    if google_ok:
        row.update({
            "google_level": np.log1p(g.iloc[-13:].mean()),
            "google_short": np.log1p(g.iloc[-4:].mean()) - np.log1p(g.iloc[-13:].mean()),
            "google_quarter": np.log1p(g.iloc[-13:].mean()) - np.log1p(g.iloc[:13].mean()),
        })
    rank = past["rank"].iloc[-26:]
    rank = rank.where(np.isfinite(rank) & (rank > 0))
    rank_ok = rank.iloc[-13:].notna().sum() >= 10 and rank.iloc[:13].notna().sum() >= 10
    row["rank_available"] = bool(rank_ok)
    row["rank_log"] = row["rank_momentum"] = np.nan
    if rank_ok:
        strength = -np.log(rank)
        row["rank_log"] = float(strength.iloc[-13:].mean())
        row["rank_momentum"] = float(strength.iloc[-13:].mean() - strength.iloc[:13].mean())
    for horizon in HORIZONS:
        # Same calendar weeks one year earlier: all are observed by origin.
        start = origin - pd.Timedelta(weeks=51)
        end = origin - pd.Timedelta(weeks=52 - horizon)
        seasonal = past.loc[start:end, "searches"]
        row[f"seasonal_{horizon}"] = (
            float(seasonal.mean()) if len(seasonal) == horizon and seasonal.notna().all() else np.nan
        )
    return row


def _build_samples(series: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for ingredient_id, frame in series.items():
        family_id = str(frame["family_id"].iloc[0])
        for origin in frame.index[MIN_HISTORY - 1:]:
            features = _features_at(frame, origin)
            if features is None:
                continue
            row = {"ingredient_id": ingredient_id, "family_id": family_id,
                   "heldout": _is_heldout(family_id), "origin": origin, **features}
            for horizon in HORIZONS:
                end = origin + pd.Timedelta(weeks=horizon)
                future = frame.loc[origin + pd.Timedelta(weeks=1):end, "searches"]
                row[f"target_end_{horizon}"] = end
                row[f"target_{horizon}"] = (
                    float(future.mean()) if len(future) == horizon and future.notna().all() else np.nan
                )
            rows.append(row)
    return pd.DataFrame(rows)


def _calendar_origins(start: pd.Timestamp, end: pd.Timestamp,
                      step_weeks: int = ORIGIN_STEP) -> pd.DatetimeIndex:
    """Return Saturdays on one absolute grid, independent of history start dates."""
    if type(step_weeks) is not int or step_weeks < 1:
        raise ValueError("step_weeks must be a positive integer")
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if end < start:
        return pd.DatetimeIndex([])
    saturdays = pd.date_range(start, end, freq="W-SAT")
    week_number = (saturdays - CALENDAR_ANCHOR).days // 7
    return saturdays[week_number % step_weeks == 0]


def _training_rows(samples: pd.DataFrame, origin: pd.Timestamp, horizon: int,
                   include_heldout: bool = False) -> pd.DataFrame:
    """No label whose last week is after the prediction origin is available."""
    if samples.empty:
        return samples.copy()
    mask = (samples[f"target_end_{horizon}"] <= origin) & samples[f"target_{horizon}"].notna()
    if not include_heldout:
        mask &= ~samples["heldout"]
    # Fixed epoch, independent of future observations or how long a download is.
    week_number = (samples["origin"] - CALENDAR_ANCHOR).dt.days // 7
    mask &= week_number % TRAIN_STEP == 0
    return samples.loc[mask].copy()


def _fit_candidates(samples: pd.DataFrame, origin: pd.Timestamp, horizon: int,
                    include_heldout: bool = False) -> tuple[dict, dict]:
    train = _training_rows(samples, origin, horizon, include_heldout)
    fitted, diagnostics = {}, {}
    for name, columns in MODEL_FEATURES.items():
        usable = train.dropna(subset=list(columns)) if len(train) else train
        diagnostics[name] = {
            "n_rows": len(usable),
            "n_families": int(usable["family_id"].nunique()) if len(usable) else 0,
            "n_origins": int(usable["origin"].nunique()) if len(usable) else 0,
            "max_label_end": usable[f"target_end_{horizon}"].max() if len(usable) else None,
        }
        if (len(usable) < MIN_TRAIN_ROWS or diagnostics[name]["n_families"] < MIN_TRAIN_GROUPS or
                diagnostics[name]["n_origins"] < MIN_TRAIN_ORIGINS):
            continue
        estimator = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
        y = np.log1p(usable[f"target_{horizon}"]) - np.log1p(usable["baseline"])
        estimator.fit(usable.loc[:, list(columns)], y)
        fitted[name] = (estimator, columns)
    return fitted, diagnostics


def _candidate_predictions(row: pd.Series | dict, horizon: int, fitted: dict) -> dict[str, float]:
    result = {"last13mean": float(row["baseline"])}
    if pd.notna(row[f"seasonal_{horizon}"]):
        result["seasonal"] = float(row[f"seasonal_{horizon}"])
    for name, (estimator, columns) in fitted.items():
        values = [row[c] for c in columns]
        if not np.isfinite(values).all():
            continue
        delta = float(estimator.predict(pd.DataFrame([values], columns=columns))[0])
        value = np.expm1(np.clip(np.log1p(row["baseline"]) + delta, -20, 30))
        result[name] = float(max(0, value))
    return result


def _metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "n_origins": 0, "mae": None, "wape": None, "rmse": None,
                "direction_accuracy": None, "interval_coverage": None, "n_intervals": 0}
    rows = sorted(rows, key=lambda r: (str(r.get("ingredient_id", "")), str(r.get("origin", "")), str(r.get("model", ""))))
    a = np.array([r["actual"] for r in rows], dtype=float)
    p = np.array([r["predicted"] for r in rows], dtype=float)
    b = np.array([r["baseline"] for r in rows], dtype=float)
    denom = float(np.abs(a).sum())
    intervals = [r for r in rows if r.get("lower") is not None and r.get("upper") is not None]
    # +/- 10% is a fixed neutral band, not a tuned definition of a "hit".
    actual_dir = np.where(a > 1.1 * b, 1, np.where(a < 0.9 * b, -1, 0))
    predicted_dir = np.where(p > 1.1 * b, 1, np.where(p < 0.9 * b, -1, 0))
    return {
        "n": len(rows), "n_origins": len({str(r.get("origin")) for r in rows}), "mae": float(np.abs(a - p).mean()),
        "wape": float(np.abs(a - p).sum() / denom) if denom else None,
        "rmse": float(np.sqrt(np.square(a - p).mean())),
        "direction_accuracy": float((actual_dir == predicted_dir).mean()),
        "interval_coverage": float(np.mean([r["lower"] <= r["actual"] <= r["upper"] for r in intervals])) if intervals else None,
        "n_intervals": len(intervals),
    }


def _row_key(row: dict) -> tuple:
    return row["ingredient_id"], row["origin"]


def _select_models(backtests: list[dict]) -> tuple[dict, list[dict]]:
    """Select only on earlier validation of non-heldout families."""
    selected, comparisons = {}, []
    for horizon in HORIZONS:
        rows = [r for r in backtests if r["horizon_weeks"] == horizon and r["split"] == "validation"]
        by_model = {m: {_row_key(r): r for r in rows if r["model"] == m} for m in MODELS}
        # Compare all eligible versions against the same matured-training cases.
        keys = set(by_model["last13mean"]) & set(by_model["ridge_amazon"])
        if not keys:
            keys = set(by_model["last13mean"])
        origin_count = len({k[1] for k in keys})
        chosen, best_score = "last13mean", math.inf
        baseline_score = _metrics([by_model["last13mean"][k] for k in keys])["wape"]
        enough = len(keys) >= 20 and origin_count >= 2 and baseline_score is not None
        if enough:
            best_score = baseline_score
        for model in MODELS:
            covered = keys & set(by_model[model])
            metric = _metrics([by_model[model][k] for k in covered])
            complete = len(covered) == len(keys) and bool(keys)
            eligible = enough and complete
            comparisons.append({"horizon_weeks": horizon, "model": model,
                                "split": "validation", "comparison": "selection_common_cases",
                                "eligible_for_selection": eligible, "coverage": len(covered) / len(keys) if keys else 0,
                                "n_origins": origin_count, **metric})
            # At least 5% relative improvement is a predeclared conservative gate.
            if eligible and model != "last13mean" and metric["wape"] is not None:
                if metric["wape"] < best_score and metric["wape"] <= baseline_score * 0.95:
                    chosen, best_score = model, metric["wape"]
        selected[str(horizon)] = {
            "model": chosen, "selection_split": "validation", "n_cases": len(keys),
            "n_origins": origin_count, "baseline_wape": baseline_score,
            "selected_wape": best_score if math.isfinite(best_score) else baseline_score,
            "reason": "validation_improvement" if chosen != "last13mean" else "conservative_baseline",
        }
        # Each feature source is scored on exactly matching ingredient/origin cases.
        for comparison, model_pair in ABLATIONS.items():
            pair_keys = set(by_model[model_pair[0]]) & set(by_model[model_pair[1]])
            for model in model_pair:
                comparisons.append({"horizon_weeks": horizon, "model": model, "split": "validation",
                                    "comparison": comparison,
                                    **_metrics([by_model[model][k] for k in pair_keys])})
    return selected, comparisons


def _calibration(backtests: list[dict]) -> dict:
    calibration = {}
    for horizon in HORIZONS:
        for model in MODELS:
            rows = [r for r in backtests if r["split"] == "validation" and
                    r["horizon_weeks"] == horizon and r["model"] == model]
            residuals = [np.log1p(r["actual"]) - np.log1p(r["predicted"]) for r in rows]
            n_origins = len({r["origin"] for r in rows})
            enough = len(rows) >= MIN_CALIBRATION and n_origins >= 2
            calibration[(horizon, model)] = {
                "n": len(rows), "n_origins": n_origins,
                "low": float(np.quantile(residuals, .1)) if enough else None,
                "high": float(np.quantile(residuals, .9)) if enough else None,
            }
    return calibration


def _interval(prediction: float, calibration: dict) -> tuple[float | None, float | None]:
    if calibration["low"] is None:
        return None, None
    return tuple(float(max(0, np.expm1(np.clip(np.log1p(prediction) + calibration[k], -20, 30))))
                 for k in ("low", "high"))


def _empty_forecast(horizon: int, baseline: float | None = None) -> dict:
    return {"horizon_weeks": horizon, "label": "未来3个月" if horizon == 13 else "未来6个月",
            "baseline": baseline, "predicted_mean": None, "growth_pct": None,
            "lower": None, "upper": None, "model": None, "status": "insufficient_data",
            "n_calibration": 0}


def _recent_baseline(frame: pd.DataFrame, as_of: pd.Timestamp) -> float | None:
    recent = frame.loc[as_of - pd.Timedelta(weeks=12):as_of, "searches"]
    if len(recent) != 13 or recent.index[-1] != as_of or recent.isna().any():
        return None
    return float(recent.mean())


def run_analysis(panel_path: str | Path, output_dir: str | Path,
                 catalog_path: str | Path | None = None,
                 panel_manifest_path: str | Path | None = None) -> dict:
    """Run fixed rolling backtests, select on early validation, forecast at as_of."""
    panel_manifest, resolved_manifest_path = _find_panel_manifest(panel_path, panel_manifest_path)
    panel = _load_panel(panel_path, panel_manifest)
    amazon_provider = str(panel["amazon_provider"].iloc[0])
    provider_inference = "manifest" if panel_manifest else "legacy_default"
    series = _weekly_series(panel)
    samples = _build_samples(series)
    as_of = panel["week_end"].max() if len(panel) else None
    test_start = as_of - pd.Timedelta(weeks=52) if as_of is not None else None
    backtests: list[dict] = []
    training_audit = []
    if not samples.empty:
        earliest = panel["week_end"].min() + pd.Timedelta(weeks=MIN_HISTORY + max(HORIZONS) - 1)
        origins = _calendar_origins(
            earliest, as_of - pd.Timedelta(weeks=min(HORIZONS)), ORIGIN_STEP
        )
        for origin in origins:
            current = samples.loc[samples["origin"] == origin]
            for horizon in HORIZONS:
                target_end = origin + pd.Timedelta(weeks=horizon)
                if target_end > as_of:
                    continue
                if target_end <= test_start:
                    split = "validation"
                elif origin >= test_start:
                    split = "sealed_test"
                else:
                    # Purge label windows straddling the sealed boundary.
                    continue
                fitted, audit = _fit_candidates(samples, origin, horizon)
                training_audit.append({"origin": origin, "horizon_weeks": horizon, **audit})
                for _, row in current.iterrows():
                    actual = row[f"target_{horizon}"]
                    if pd.isna(actual):
                        continue
                    predictions = _candidate_predictions(row, horizon, fitted)
                    for model, prediction in predictions.items():
                        backtests.append({
                            "ingredient_id": row["ingredient_id"], "family_id": row["family_id"],
                            "origin": origin, "target_end": target_end, "horizon_weeks": horizon,
                            "actual": float(actual), "predicted": prediction, "baseline": float(row["baseline"]),
                            "model": model, "split": f"group_{split}" if row["heldout"] else split,
                            "lower": None, "upper": None,
                        })
    selected, comparisons = _select_models(backtests)
    calibration = _calibration(backtests)
    for row in backtests:
        if row["split"] in ("sealed_test", "group_sealed_test"):
            row["lower"], row["upper"] = _interval(row["predicted"], calibration[(row["horizon_weeks"], row["model"])])
    summary = []
    for horizon in HORIZONS:
        for split in ("validation", "sealed_test", "group_validation", "group_sealed_test"):
            for model in MODELS:
                rows = [r for r in backtests if r["horizon_weeks"] == horizon and r["split"] == split and r["model"] == model]
                if rows:
                    summary.append({"horizon_weeks": horizon, "split": split, "model": model, **_metrics(rows)})
            rows = [r for r in backtests if r["horizon_weeks"] == horizon and r["split"] == split]
            by_model = {m: {_row_key(r): r for r in rows if r["model"] == m} for m in MODEL_FEATURES}
            if split != "validation":
                for comparison, model_pair in ABLATIONS.items():
                    keys = set(by_model[model_pair[0]]) & set(by_model[model_pair[1]])
                    for model in model_pair:
                        comparisons.append({"horizon_weeks": horizon, "split": split, "model": model,
                                            "comparison": comparison,
                                            **_metrics([by_model[model][k] for k in keys])})
    deployment_info = {h: _fit_candidates(samples, as_of, h, include_heldout=True) for h in HORIZONS} if as_of is not None and not samples.empty else {}
    deployment = {h: data[0] for h, data in deployment_info.items()}
    ingredients = []
    for ingredient_id, frame in series.items():
        first = frame.iloc[0]
        features = _features_at(frame, as_of) if as_of is not None else None
        forecasts = []
        for horizon in HORIZONS:
            if features is None:
                baseline = _recent_baseline(frame, as_of) if as_of is not None else None
                forecast = _empty_forecast(horizon, baseline)
                if baseline is not None:
                    forecast.update({"predicted_mean": baseline, "growth_pct": 0.0 if baseline > 0 else None,
                                     "model": "last13mean", "status": "baseline_only",
                                     "interval_status": "insufficient_history",
                                     "fallback_reason": "requires_104_complete_weeks_for_model"})
                forecasts.append(forecast)
                continue
            predictions = _candidate_predictions(features, horizon, deployment.get(horizon, {}))
            chosen = selected[str(horizon)]["model"]
            model = chosen if chosen in predictions else "last13mean"
            prediction = predictions[model]
            cal = calibration[(horizon, model)]
            low, high = _interval(prediction, cal)
            baseline = features["baseline"]
            forecasts.append({
                "horizon_weeks": horizon, "label": "未来3个月" if horizon == 13 else "未来6个月",
                "baseline": baseline, "predicted_mean": prediction,
                "growth_pct": (prediction / baseline - 1) * 100 if baseline > 0 else None,
                "lower": low, "upper": high, "model": model,
                "status": "baseline_only" if model == "last13mean" else "ok",
                "n_calibration": cal["n"], "interval_status": "empirical_80pct" if low is not None else "insufficient_calibration",
                "fallback_reason": "selected_model_unavailable" if chosen != model else None,
            })
        rows = [r for r in backtests if r["ingredient_id"] == ingredient_id]
        selected_rows = []
        for horizon in HORIZONS:
            horizon_rows = [r for r in rows if r["horizon_weeks"] == horizon]
            for origin in sorted({r["origin"] for r in horizon_rows}):
                options = {r["model"]: r for r in horizon_rows if r["origin"] == origin}
                chosen = selected[str(horizon)]["model"]
                selected_rows.append(options.get(chosen, options["last13mean"]))
        ingredients.append({
            "id": ingredient_id, "keyword": first["keyword"], "name_cn": first["name_cn"], "marketplace": first["marketplace"],
            "history": [{"date": date, "searches": row["searches"], "rank": row["rank"], "google_trend": row["google_trend"]} for date, row in frame.iterrows()],
            "forecasts": forecasts, "backtests": selected_rows,
            "diagnostics": {"history_weeks": len(frame), "observed_weeks": int(frame["searches"].notna().sum()),
                            "missing_weeks": int(frame["searches"].isna().sum()),
                             "google_weeks": int(frame["google_trend"].notna().sum()),
                             "rank_weeks": int(frame["rank"].notna().sum()),
                             "family_id": first["family_id"], "heldout": _is_heldout(first["family_id"]),
                             "amazon_provider": first["amazon_provider"],
                             "as_of_eligible": features is not None, "latest_week": frame.index.max()},
        })
    catalog = []
    if catalog_path and Path(catalog_path).exists():
        catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    present = set(series)
    for item in catalog:
        if item.get("selected") and str(item.get("id")) not in present:
            ingredients.append({"id": item["id"], "keyword": item.get("keyword", ""), "name_cn": item.get("name_cn", ""),
                                "marketplace": "US", "history": [], "forecasts": [_empty_forecast(h) for h in HORIZONS],
                                "backtests": [], "diagnostics": {"history_weeks": 0, "missing_weeks": 0,
                                "amazon_provider": amazon_provider, "reason": "no_source_data"}})
    provider_label = "卖家精灵" if amazon_provider == "sellersprite" else "SIF"
    warnings = [
        f"预测目标是{provider_label}估计的亚马逊搜索次数，不是销量、独立人数或投资回报。",
        "历史数据及Google指数为本次下载的修订后快照，无法证明历史当时已能取得相同数值；本回测不是完整的实时历史复原。",
        "Google字段按数据契约保守滞后一周；来源周边界映射仍待官方确认。",
        "80%区间来自较早滚动验证的经验残差，样本存在跨词及时间相关性，不保证未来80%覆盖；以后期留出覆盖率检验。",
        "共享模型按family_id整组留出约20%；部署预测会重新使用全部合格成分训练，因此部署模型与留组测试模型不同。",
        "每4周进行一次预测，13/26周目标窗口明显重叠；回测行数和原点数量都不等于独立实验次数。",
        "未来季度/半年均值衡量一段时间的需求，不等于每周持续增长，也不能单独证明产品将成为爆款。",
        "PPC、ABA集中度、TikTok及新闻暂不进入需求预测；ABA排名通过独立消融测试增益，搜索量与排名可能同源相关，不能视为两份独立需求证据。",
        "后期测试不参与自动选模或区间校准；开发中已查看后期结果核验流程，它不是从未查看过的独立盲测。",
    ]
    if amazon_provider == "sif":
        warnings.append("本次模型完全使用SIF亚马逊搜索量口径；其绝对量不能与卖家精灵结果直接相加或比较。")
    if not backtests:
        warnings.append("可用历史不足以完成规定的滚动回测；当前仅能展示历史及保守基线，不能报告预测准确率。")
    for horizon in HORIZONS:
        chosen = selected[str(horizon)]["model"]
        test_rows = [r for r in backtests if r["horizon_weeks"] == horizon and r["split"] == "sealed_test"]
        base_by_key = {_row_key(r): r for r in test_rows if r["model"] == "last13mean"}
        model_by_key = {_row_key(r): r for r in test_rows if r["model"] == chosen}
        keys = set(base_by_key) & set(model_by_key)
        base_test = _metrics([base_by_key[k] for k in keys])
        model_test = _metrics([model_by_key[k] for k in keys])
        selected[str(horizon)]["sealed_test"] = {
            "model_metrics": model_test, "baseline_metrics": base_test,
            "n_matching_cases": len(keys), "used_for_selection": False,
        }
        if chosen != "last13mean" and model_test["wape"] is not None and base_test["wape"] is not None:
            if model_test["wape"] >= base_test["wape"]:
                warnings.append(f"{horizon}周所选{chosen}模型在后期封存测试未超过最近13周均值基准；尚无稳定预测增益证据，不依据该测试结果重新选模。")
    if series and not any(_is_heldout(str(f["family_id"].iloc[0])) for f in series.values()):
        warnings.append("本批成分未落入固定留组桶，尚不能评估新成分泛化能力。")
    result = _json_value({
        "schema_version": "2.0", "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of, "scope": "美国站营养补充剂成分词；未来13/26周平均每周估计搜索量",
        "provenance": {
            "amazon_provider": amazon_provider,
            "provider_counts": {amazon_provider: int(panel["ingredient_id"].nunique())},
            "provider_policy": "single_amazon_provider",
            "provider_inference": provider_inference,
            "panel_sha256": sha256(panel_path),
            "panel_manifest_path": str(resolved_manifest_path.resolve()) if resolved_manifest_path else None,
            "panel_manifest_sha256": sha256(resolved_manifest_path) if resolved_manifest_path else None,
        },
        "methodology": {
            "target": "next_13_or_26_week_mean_estimated_amazon_searches", "baseline": "last13mean",
            "horizons_weeks": list(HORIZONS), "min_history_weeks": MIN_HISTORY,
            "origin_step_weeks": ORIGIN_STEP, "training_step_weeks": TRAIN_STEP,
            "calendar_anchor": CALENDAR_ANCHOR,
            "sealed_test_start": test_start, "selection": "early_validation_only_min_5pct_wape_improvement",
            "group_holdout": "sha256(family_id) first 8 hex modulo 5 equals 0",
            "features": {"amazon": AMAZON_FEATURES, "google": GOOGLE_FEATURES, "aba": ABA_FEATURES},
            "test_usage": "not_used_for_automatic_selection_or_calibration; inspected_during_development",
            "ridge_alpha": 10.0, "target_transform": "log1p(future_mean)-log1p(last13mean)",
            "interval": "central_80pct_empirical_early_validation_log_residuals",
            "training_audit": training_audit,
        },
        "warnings": warnings, "metrics": {"summary": summary, "paired_comparisons": comparisons, "selected_models": selected},
        "backtests": backtests, "ingredients": ingredients,
    })
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    artifacts = []
    fitted_parameters = {}
    for horizon, fitted in deployment.items():
        for name, (estimator, columns) in fitted.items():
            model_dir = destination / "models"
            model_dir.mkdir(exist_ok=True)
            relative_path = Path("models") / f"{name}_{horizon}.joblib"
            artifact_path = destination / relative_path
            joblib.dump({"model_version": MODEL_VERSION, "horizon_weeks": horizon,
                          "model": name, "features": list(columns), "estimator": estimator,
                          "target_transform": "log1p(future_mean)-log1p(last13mean)",
                          "as_of": result["as_of"], "amazon_provider": amazon_provider}, artifact_path)
            artifacts.append({"path": relative_path.as_posix(), "model": name, "horizon_weeks": horizon,
                              "selected": selected[str(horizon)]["model"] == name,
                              "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest()})
            fitted_parameters[f"{name}_{horizon}"] = {
                "features": list(columns), "scaler_mean": estimator[0].mean_.tolist(),
                "scaler_scale": estimator[0].scale_.tolist(), "coefficients": estimator[1].coef_.tolist(),
                "intercept": float(estimator[1].intercept_), "alpha": estimator[1].alpha,
            }
    manifest = _json_value({
        "model_version": MODEL_VERSION, "generated_at": result["generated_at"], "as_of": as_of,
        "source": {"panel_path": str(Path(panel_path).resolve()),
                    "panel_sha256": hashlib.sha256(Path(panel_path).read_bytes()).hexdigest(),
                    "panel_manifest_path": str(resolved_manifest_path.resolve()) if resolved_manifest_path else None,
                    "panel_manifest_sha256": sha256(resolved_manifest_path) if resolved_manifest_path else None,
                    "amazon_provider": amazon_provider,
                    "catalog_sha256": hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest() if catalog_path and Path(catalog_path).exists() else None,
                   "model_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "runtime": {"python": platform.python_version(), **{name: importlib.metadata.version(name)
                    for name in ("numpy", "pandas", "scikit-learn", "joblib")}},
        "methodology": result["methodology"], "selected_models": selected,
        "deployment_training": {str(h): info[1] for h, info in deployment_info.items()},
        "deployment_uses_all_families": True,
        "families": [{"ingredient_id": i, "family_id": str(f["family_id"].iloc[0]),
                      "historical_heldout": _is_heldout(str(f["family_id"].iloc[0]))} for i, f in series.items()],
        "fitted_parameters": fitted_parameters, "model_files": artifacts,
        "calibration": [{"horizon_weeks": h, "model": m, "source_split": "validation", **cal}
                        for (h, m), cal in calibration.items()],
        "reproduction": "Run the same model code with the panel SHA256 and pinned runtime versions; generated_at is the only expected nondeterministic metadata.",
    })
    (destination / "training_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    result["artifacts"] = {"training_manifest": "training_manifest.json", "models": artifacts}
    (destination / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result
