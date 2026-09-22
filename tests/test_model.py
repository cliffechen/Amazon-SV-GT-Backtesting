import copy
import json

import numpy as np
import pandas as pd
import pytest

from predictor.model import (
    AMAZON_FEATURES, HORIZONS, MODEL_FEATURES, _build_samples, _candidate_predictions,
    _features_at, _fit_candidates, _is_heldout, _select_models,
    _training_rows, _weekly_series, run_analysis,
)


def synthetic_panel(n_ingredients=10, n_weeks=240, google=True):
    dates = pd.date_range("2021-01-02", periods=n_weeks, freq="W-SAT")
    rows = []
    for i in range(n_ingredients):
        t = np.arange(n_weeks)
        searches = (500 + i * 100) * np.exp(.002 * t) * (1 + .2 * np.sin(2 * np.pi * t / 52 + i / 5))
        for j, date in enumerate(dates):
            rows.append({"ingredient_id": f"ingredient-{i}", "family_id": f"family{i}",
                         "keyword": f"ingredient {i}", "name_cn": f"成分{i}", "marketplace": "US",
                         "week_end": date, "searches": searches[j], "rank": 10000 / (i + 1),
                         "google_trend": 30 + 10 * np.sin(2 * np.pi * j / 52) if google else np.nan})
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def prepared():
    panel = synthetic_panel()
    series = _weekly_series(panel)
    return panel, series, _build_samples(series)


def test_future_values_cannot_change_past_features_or_predictions(prepared):
    panel, series, samples = prepared
    origin = sorted(panel.week_end.unique())[180]
    changed = panel.copy()
    changed.loc[changed.week_end > origin, ["searches", "google_trend", "rank"]] = [9e8, 99, 1]
    altered_series = _weekly_series(changed)
    altered_samples = _build_samples(altered_series)
    for horizon in HORIZONS:
        fitted, _ = _fit_candidates(samples, origin, horizon)
        altered_fitted, _ = _fit_candidates(altered_samples, origin, horizon)
        assert "ridge_amazon" in fitted
        assert "ridge_aba" in fitted
        assert "ridge_all" in fitted
        for ingredient_id in series:
            row = _features_at(series[ingredient_id], origin)
            altered_row = _features_at(altered_series[ingredient_id], origin)
            assert row == altered_row
            before = _candidate_predictions(row, horizon, fitted)
            after = _candidate_predictions(altered_row, horizon, altered_fitted)
            assert before == pytest.approx(after, rel=1e-12)


def test_labels_are_mature_and_families_are_held_out(prepared):
    _, _, samples = prepared
    assert samples.heldout.any()
    origin = pd.Timestamp("2024-06-15")
    for horizon in HORIZONS:
        train = _training_rows(samples, origin, horizon)
        assert len(train) > 0
        assert train[f"target_end_{horizon}"].max() <= origin
        assert not train.heldout.any()
        assert not any(_is_heldout(x) for x in train.family_id)
    # An alias/formulation in the same family must receive the same assignment.
    same_family = pd.concat([synthetic_panel(1, 130), synthetic_panel(1, 130)], ignore_index=True)
    same_family.loc[same_family.index >= 130, "ingredient_id"] = "second-formulation"
    same_family["family_id"] = "family2"
    group_samples = _build_samples(_weekly_series(same_family))
    assert group_samples.groupby("ingredient_id").heldout.first().tolist() == [True, True]


def test_scaler_fits_training_rows_only(prepared):
    _, _, samples = prepared
    origin = pd.Timestamp("2024-06-15")
    fitted, _ = _fit_candidates(samples, origin, 26)
    train = _training_rows(samples, origin, 26)
    estimator, columns = fitted["ridge_amazon"]
    np.testing.assert_allclose(estimator[0].mean_, train[list(columns)].mean().to_numpy())
    assert tuple(columns) == AMAZON_FEATURES


def test_sealed_and_group_tests_never_select_model():
    rows = []
    for h in HORIZONS:
        for origin in ("2023-01-07", "2023-04-08"):
            for i in range(12):
                for model, prediction in (("last13mean", 100), ("ridge_amazon", 120), ("ridge_google", 105)):
                    rows.append({"ingredient_id": str(i), "origin": origin, "horizon_weeks": h,
                                 "model": model, "split": "validation", "actual": 120,
                                 "baseline": 100, "predicted": prediction})
    expected, _ = _select_models(rows)
    assert expected["13"]["model"] == "ridge_amazon"
    changed = copy.deepcopy(rows)
    for split in ("sealed_test", "group_validation", "group_sealed_test"):
        for row in rows:
            extra = dict(row, split=split, actual=1000000, predicted=0)
            changed.append(extra)
    actual, _ = _select_models(changed)
    assert expected == actual


def test_empty_google_and_short_history_have_honest_fallback(tmp_path):
    panel = synthetic_panel(1, 80, google=False)
    path = tmp_path / "panel.csv"
    panel.to_csv(path, index=False)
    result = run_analysis(path, tmp_path / "out")
    forecasts = result["ingredients"][0]["forecasts"]
    assert result["backtests"] == []
    assert all(f["status"] == "baseline_only" and f["model"] == "last13mean" for f in forecasts)
    assert all(f["lower"] is None and f["upper"] is None for f in forecasts)
    assert json.loads((tmp_path / "out" / "analysis.json").read_text(encoding="utf-8"))["schema_version"] == "1.0"
    assert (tmp_path / "out" / "training_manifest.json").exists()


def test_missing_targets_never_filled_and_missing_google_not_fabricated():
    panel = synthetic_panel(1, 160, google=False)
    origin = panel.week_end.iloc[110]
    panel.loc[panel.index == 120, "searches"] = np.nan
    samples = _build_samples(_weekly_series(panel))
    row = samples.loc[samples.origin == origin].iloc[0]
    assert pd.isna(row["target_13"])
    assert pd.isna(row["target_26"])
    assert not row["google_available"]
    assert samples.google_level.isna().all()


def test_missing_or_invalid_rank_falls_back_without_disabling_amazon_google(prepared):
    _, series, samples = prepared
    origin = pd.Timestamp("2024-06-15")
    fitted, _ = _fit_candidates(samples, origin, 13)
    frame = next(iter(series.values())).copy()
    frame.loc[origin - pd.Timedelta(weeks=25):origin, "rank"] = 0
    row = _features_at(frame, origin)
    assert not row["rank_available"]
    predictions = _candidate_predictions(row, 13, fitted)
    assert {"last13mean", "ridge_amazon", "ridge_google"}.issubset(predictions)
    assert "ridge_aba" not in predictions and "ridge_all" not in predictions
    no_rank_samples = samples.copy()
    no_rank_samples[["rank_log", "rank_momentum"]] = np.nan
    no_rank_models, _ = _fit_candidates(no_rank_samples, origin, 13)
    assert "ridge_amazon" in no_rank_models and "ridge_google" in no_rank_models
    assert "ridge_aba" not in no_rank_models and "ridge_all" not in no_rank_models


def test_rank_strength_and_momentum_use_past_positive_ranks(prepared):
    _, series, _ = prepared
    frame = next(iter(series.values())).copy()
    origin = pd.Timestamp("2024-06-15")
    frame.loc[:origin, "rank"] = 100
    frame.loc[origin - pd.Timedelta(weeks=12):origin, "rank"] = 10
    row = _features_at(frame, origin)
    assert row["rank_log"] == pytest.approx(-np.log(10))
    assert row["rank_momentum"] == pytest.approx(np.log(10))


def test_end_to_end_backtest_intervals_are_only_later_test(tmp_path):
    panel = synthetic_panel(10, 240, google=False)
    path = tmp_path / "panel.csv"
    panel.to_csv(path, index=False)
    result = run_analysis(path, tmp_path / "out")
    rows = result["backtests"]
    assert rows
    assert not any(r["model"] == "ridge_google" for r in rows)
    assert not any(r["model"] == "ridge_all" for r in rows)
    assert any(r["model"] == "ridge_amazon" for r in rows)
    assert any(r["model"] == "ridge_aba" for r in rows)
    assert {r["split"] for r in rows} == {"validation", "sealed_test", "group_validation", "group_sealed_test"}
    boundary = result["methodology"]["sealed_test_start"]
    for row in rows:
        if row["split"].endswith("validation"):
            assert row["target_end"] <= boundary
            assert row["lower"] is None
        else:
            assert row["origin"] >= boundary
    for audit in result["methodology"]["training_audit"]:
        for model in MODEL_FEATURES:
            label_end = audit[model]["max_label_end"]
            assert label_end is None or label_end <= audit["origin"]
    assert all(f["n_calibration"] >= 0 for i in result["ingredients"] for f in i["forecasts"])
    manifest = json.loads((tmp_path / "out" / "training_manifest.json").read_text(encoding="utf-8"))
    assert manifest["deployment_uses_all_families"]
    assert manifest["model_files"]
    assert all(c["source_split"] == "validation" for c in manifest["calibration"])
    for artifact in manifest["model_files"]:
        assert (tmp_path / "out" / artifact["path"]).exists()
    paired = result["metrics"]["paired_comparisons"]
    assert any(p["comparison"] == "aba_ablation_matched_cases" and p["n"] > 0 for p in paired)
