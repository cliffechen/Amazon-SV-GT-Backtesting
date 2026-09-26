import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from predictor.report import (
    _HTML, _prepare_payload, _safe_url, _script_json, _vendor_comparison,
    build_report,
)


def _row(origin, actual, predicted, *, ingredient_id="a", split="sealed_test",
         model="last13mean", target_end=None, baseline=10):
    return {
        "ingredient_id": ingredient_id,
        "horizon_weeks": 13,
        "split": split,
        "origin": origin,
        "target_end": target_end or origin,
        "model": model,
        "actual": actual,
        "predicted": predicted,
        "baseline": baseline,
    }


def _comparison_analysis(provider, rows, *, selected="last13mean", as_of="2026-09-12",
                         ingredient_ids=("a",)):
    return {
        "schema_version": "1.0",
        "as_of": as_of,
        "scope": "Amazon US ingredient demand",
        "provenance": {"amazon_provider": provider},
        "methodology": {
            "target": "future_mean",
            "baseline": "last13mean",
            "horizons_weeks": [13],
            "min_history_weeks": 104,
            "origin_step_weeks": 4,
            "training_step_weeks": 4,
            "sealed_test_start": "2025-09-13",
            "selection": "early_validation_only",
            "group_holdout": "stable_hash",
            "features": {"amazon": ["level"]},
            "ridge_alpha": 10.0,
            "target_transform": "log_growth",
        },
        "metrics": {"selected_models": {"13": {"model": selected}}},
        "ingredients": [{"id": ingredient_id, "keyword": f"kw-{ingredient_id}",
                         "marketplace": "US", "history": [], "forecasts": []}
                        for ingredient_id in ingredient_ids],
        "data_quality": {"ingredients": [
            {"id": ingredient_id, "amazon_provider": provider}
            for ingredient_id in ingredient_ids
        ]},
        "backtests": rows,
    }


def _embedded_payload(path):
    html = Path(path).read_text(encoding="utf-8")
    embedded = re.search(
        r'<script id="analysis-data" type="application/json">(.*?)</script>', html, re.S
    ).group(1)
    return json.loads(embedded)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                                  "file:///etc/passwd", "//example.com", "https:\n//example.com", None])
def test_news_url_rejects_executable_or_local_targets(url):
    assert _safe_url(url) is None


def test_safe_link_and_untrusted_json_round_trip():
    assert _safe_url("https://example.com/story?q=a&b=c") == "https://example.com/story?q=a&b=c"
    malicious = {"title": '</script><script>alert("x")</script>', "name": "<img src=x onerror=alert(1)>", "line": "\u2028\u2029&"}
    serialized = _script_json(malicious)
    assert "<" not in serialized and "&" not in serialized
    assert json.loads(serialized) == malicious


def test_news_window_uses_calendar_months_and_separates_unknown_dates():
    payload = _prepare_payload({"news": {"as_of": "2026-05-31", "items": [
        {"id": "boundary", "published_at": "2026-02-28", "url": "https://example.com"},
        {"id": "older", "published_at": "2026-02-27"},
        {"id": "future", "published_at": "2026-06-01"},
        {"id": "undated", "published_at": None},
    ]}})
    news = payload["news"]
    assert news["window_start"] == "2026-02-28"
    assert [x["id"] for x in news["items"]] == ["boundary"]
    assert {x["id"] for x in news["background_items"]} == {"older", "future", "undated"}


def test_news_module_background_and_country_coverage_are_retained():
    payload = _prepare_payload({"news": {"as_of": "2026-09-23", "items": [],
        "background": [{"id": "background", "published_at": "2025-01-01"}],
        "coverage": {"note": "US 已查；CA/MX 未系统查"}}})
    assert payload["news"]["background_items"][0]["id"] == "background"
    assert payload["news"]["coverage"]["note"] == "US 已查；CA/MX 未系统查"


def test_vendor_comparison_uses_strict_key_intersection_and_keeps_splits_separate(tmp_path):
    primary = _comparison_analysis("sellersprite", [
        _row("2025-09-20", 10, 8),
        _row("2025-10-18", 20, 18),
        _row("2025-11-15", 30, 27, split="group_sealed_test"),
    ])
    secondary = _comparison_analysis("sif", [
        _row("2025-10-18", 40, 32),
        _row("2025-11-15", 50, 45, split="sealed_test"),
        _row("2025-11-15", 60, 48, split="group_sealed_test"),
    ])
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "ok"
    sealed = result["summary"]["13"]["sealed_test"]["last13mean"]
    heldout = result["summary"]["13"]["group_sealed_test"]["last13mean"]
    assert sealed["n_pairs"] == 1
    assert sealed["pairing"] == {"primary_only": 1, "secondary_only": 1}
    assert sealed["primary"]["wape"] == pytest.approx(0.1)
    assert sealed["secondary"]["wape"] == pytest.approx(0.2)
    assert sealed["primary"]["macro_wape"] == pytest.approx(0.1)
    assert heldout["n_pairs"] == 1
    series = result["per_ingredient"]["a"]["13"]["sealed_test"]["last13mean"]
    assert series["origins"] == ["2025-10-18"]
    assert len(series["primary"]["actual"]) == len(series["secondary"]["actual"]) == 1


def test_vendor_comparison_fails_closed_for_incompatible_analyses(tmp_path):
    primary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary = _comparison_analysis("sif", [_row("2025-09-20", 10, 8)], as_of="2026-09-19")
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "incompatible"
    assert "as_of 不一致" in result["reasons"]
    assert result["summary"] == {}
    assert result["per_ingredient"] == {}


def test_vendor_comparison_drops_invalid_and_duplicate_rows_without_crashing(tmp_path):
    duplicate = _row("2025-09-20", 10, 8)
    primary = _comparison_analysis("sellersprite", [
        duplicate, dict(duplicate), _row("2025-10-18", None, 8),
    ])
    secondary = _comparison_analysis("sif", [
        _row("2025-09-20", 12, 9), _row("2025-10-18", 12, 9),
    ])
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "no_pairs"
    assert result["audit"]["duplicate_keys"]["primary"] == 1
    assert result["audit"]["invalid_rows"]["primary"] == 1
    assert result["summary"] == {}


def test_vendor_comparison_only_adds_common_selected_model(tmp_path):
    rows_primary = [
        _row("2025-09-20", 10, 8),
        _row("2025-09-20", 10, 9, model="seasonal"),
    ]
    rows_secondary = [
        _row("2025-09-20", 12, 10),
        _row("2025-09-20", 12, 11, model="ridge_amazon"),
    ]
    primary = _comparison_analysis("sellersprite", rows_primary, selected="seasonal")
    secondary = _comparison_analysis("sif", rows_secondary, selected="ridge_amazon")
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["models_compared"]["13"] == ["last13mean"]
    assert set(result["summary"]["13"]["sealed_test"]) == {"last13mean"}
    assert any("选中的模型不同" in warning for warning in result["warnings"])


def test_vendor_comparison_adds_shared_selected_model_and_macro_averages_ingredients(tmp_path):
    primary = _comparison_analysis("sellersprite", [
        _row("2025-09-20", 100, 50, ingredient_id="a"),
        _row("2025-09-20", 10, 10, ingredient_id="b"),
        _row("2025-09-20", 100, 80, ingredient_id="a", model="seasonal"),
        _row("2025-09-20", 10, 9, ingredient_id="b", model="seasonal"),
    ], selected="seasonal", ingredient_ids=("a", "b"))
    secondary = _comparison_analysis("sif", [
        _row("2025-09-20", 100, 100, ingredient_id="a"),
        _row("2025-09-20", 10, 5, ingredient_id="b"),
        _row("2025-09-20", 100, 90, ingredient_id="a", model="seasonal"),
        _row("2025-09-20", 10, 8, ingredient_id="b", model="seasonal"),
    ], selected="seasonal", ingredient_ids=("a", "b"))
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["models_compared"]["13"] == ["last13mean", "seasonal"]
    baseline = result["summary"]["13"]["sealed_test"]["last13mean"]
    assert baseline["n_pairs"] == 2
    assert baseline["macro_n_ingredients"] == 2
    assert baseline["primary"]["macro_wape"] == pytest.approx(0.25)
    assert baseline["secondary"]["macro_wape"] == pytest.approx(0.25)
    assert baseline["primary"]["wape"] == pytest.approx(50 / 110)
    assert baseline["secondary"]["wape"] == pytest.approx(5 / 110)
    assert "seasonal" in result["summary"]["13"]["sealed_test"]


def test_vendor_comparison_requires_exact_top_level_provider_pair(tmp_path):
    primary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    shared_cache = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(shared_cache), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "incompatible"
    assert result["primary_provider"] == "sellersprite"
    assert result["secondary_provider"] == "sellersprite"
    assert result["providers"] == {"primary": "sellersprite", "secondary": "sellersprite"}
    assert any("恰好分别来自" in reason for reason in result["reasons"])


def test_vendor_comparison_supports_reversed_primary_provider_direction(tmp_path):
    primary = _comparison_analysis("sif", [_row("2025-09-20", 12, 9)])
    secondary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "ok"
    assert result["primary_provider"] == "sif"
    assert result["secondary_provider"] == "sellersprite"
    assert result["providers"] == {"primary": "sif", "secondary": "sellersprite"}
    metrics = result["summary"]["13"]["sealed_test"]["last13mean"]
    assert metrics["primary"]["wape"] == pytest.approx(0.25)
    assert metrics["secondary"]["wape"] == pytest.approx(0.2)


def test_vendor_comparison_accepts_legacy_ingredient_source_with_current_provenance(tmp_path):
    primary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary = _comparison_analysis("sif", [_row("2025-09-20", 12, 9)])
    for analysis in (primary, secondary):
        quality = analysis["data_quality"]["ingredients"][0]
        quality["amazon_source"] = quality.pop("amazon_provider")
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "ok"
    assert result["audit"]["source_mismatch"] == []


@pytest.mark.parametrize("field", ["amazon_provider", "amazon_source"])
def test_vendor_comparison_rejects_ingredient_provider_that_disagrees_with_analysis(
        tmp_path, field):
    primary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary = _comparison_analysis("sif", [_row("2025-09-20", 12, 9)])
    quality = primary["data_quality"]["ingredients"][0]
    quality.pop("amazon_provider")
    quality[field] = "sif"
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "no_pairs"
    assert result["audit"]["source_mismatch"] == ["a"]


def test_vendor_comparison_rejects_missing_top_level_provider_even_with_quality_source(tmp_path):
    primary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary = _comparison_analysis("sif", [_row("2025-09-20", 12, 9)])
    secondary.pop("provenance")
    secondary_path = tmp_path / "secondary.json"
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    result = _vendor_comparison(primary, secondary_path)

    assert result["status"] == "incompatible"
    assert result["secondary_provider"] is None
    assert any("provenance.amazon_provider" in reason for reason in result["reasons"])


def test_build_report_never_auto_discovers_comparison_and_degrades_explicit_failure(tmp_path):
    primary = _comparison_analysis("sellersprite", [_row("2025-09-20", 10, 8)])
    secondary = _comparison_analysis("sif", [_row("2025-09-20", 12, 9)])
    latest = tmp_path / "latest"
    sibling = tmp_path / "sif-caliber"
    latest.mkdir()
    sibling.mkdir()
    primary_path = latest / "analysis.json"
    secondary_path = sibling / "analysis.json"
    primary_path.write_text(json.dumps(primary), encoding="utf-8")
    secondary_path.write_text(json.dumps(secondary), encoding="utf-8")

    without = build_report(primary_path, latest / "without.html")
    assert "vendor_comparison" not in _embedded_payload(without)

    explicit = build_report(primary_path, latest / "explicit.html", comparison_path=secondary_path)
    assert _embedded_payload(explicit)["vendor_comparison"]["status"] == "ok"

    missing = build_report(primary_path, latest / "missing.html",
                           comparison_path=tmp_path / "absent.json")
    unavailable = _embedded_payload(missing)["vendor_comparison"]
    assert unavailable["status"] == "unavailable"
    assert unavailable["reasons"]


def test_vendor_ui_is_separate_safe_and_javascript_compiles():
    assert 'id="vendor-chart"' in _HTML and 'id="vendor-empty"' in _HTML
    assert 'id="vendor-global-details"' in _HTML
    assert "per_ingredient.sif" not in _HTML
    assert '<strong style="color:' not in _HTML
    assert "xaxis2:{" in _HTML and "yaxis2:{" in _HTML
    assert "vc.primary_provider||vc.providers?.primary" in _HTML
    assert "vc.secondary_provider||vc.providers?.secondary" in _HTML
    assert "'卖家精灵 WAPE'" not in _HTML and "'SIF WAPE'" not in _HTML
    assert "name:'卖家精灵 · 真实值'" not in _HTML and "name:'SIF · 真实值'" not in _HTML
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to compile the report application script")
    script = _HTML.rsplit("<script>", 1)[1].rsplit("</script>", 1)[0]
    subprocess.run([node, "--check", "-"], input=script, text=True,
                   encoding="utf-8", capture_output=True, check=True)


def test_budget_ui_prefers_provider_ledgers_and_falls_back_to_legacy_budget():
    assert "DATA.provider_budgets" in _HTML
    assert "counted_units_including_legacy" in _HTML
    assert "SellerSprite（卖家精灵）" in _HTML and "sif:'SIF'" in _HTML
    assert "provider_budgets:DATA.provider_budgets??null" in _HTML
    assert "legacy_budget:DATA.budget??null" in _HTML
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise the budget renderer")
    functions = "\n".join(
        re.search(rf"function {name}\([^)]*\)\{{.*?\n\}}", _HTML, re.S).group(0)
        for name in ("providerBudgetEntries", "budgetProviderLabel", "budgetStatesText",
                     "renderBudget")
    )
    cases = [
        {
            "provider_budgets": {
                "sellersprite": {
                    "provider": "sellersprite", "period": "2026-09", "hard_limit": 160,
                    "counted_units_including_legacy": 144, "remaining": 16,
                    "states": {"saved": {"jobs": 120, "units": 144}},
                },
                "sif": {
                    "provider": "sif", "period": "2026-09", "hard_limit": 11,
                    "counted_units_including_legacy": 11, "remaining": 0,
                    "states": {"failed_provider": {"jobs": 1, "units": 1}},
                },
            },
            "budget": {"project_limit": 160, "charged_attempts": 92,
                       "remaining_project": 68, "estimated_remaining_account": 1092},
        },
        {"budget": {"project_limit": 160, "charged_attempts": 92,
                    "remaining_project": 68, "estimated_remaining_account": 1092}},
    ]
    program = r"""
let DATA={};
function makeNode(tag='',cls='',text=''){return {tag,className:cls,textContent:text,children:[],append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];}};}
const nodes={'budget-grid':makeNode(),'budget-detail':makeNode()};
const $=id=>nodes[id];
const set=(id,value)=>{$(id).textContent=value??'';};
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const fmt=x=>finite(x)?String(x):'—';
const pretty=x=>JSON.stringify(x??{},null,2);
const str=x=>typeof x==='string'?x:pretty(x);
const element=(tag,cls,text)=>makeNode(tag,cls,text);
""" + functions + "\nconst cases=" + json.dumps(cases, ensure_ascii=False) + r""";
const results=cases.map(value=>{DATA=value;nodes['budget-grid']=makeNode();nodes['budget-detail']=makeNode();renderBudget();return JSON.parse(JSON.stringify(nodes));});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run([node, "-e", program], text=True, encoding="utf-8",
                            capture_output=True, check=True)
    provider_view, fallback_view = json.loads(result.stdout)

    provider_grid = json.dumps(provider_view["budget-grid"], ensure_ascii=False)
    assert provider_view["budget-grid"]["children"][0]["className"] == "provider-budget"
    assert "SellerSprite（卖家精灵）" in provider_grid and "SIF" in provider_grid
    assert "144" in provider_grid and "11" in provider_grid
    assert "已保存（saved）" in provider_grid and "供应商失败（failed_provider）" in provider_grid
    assert "已计费尝试" not in provider_grid
    raw = json.loads(provider_view["budget-detail"]["textContent"])
    assert raw["provider_budgets"]["sellersprite"]["counted_units_including_legacy"] == 144
    assert raw["legacy_budget"]["charged_attempts"] == 92

    fallback_grid = json.dumps(fallback_view["budget-grid"], ensure_ascii=False)
    assert "已计费尝试" in fallback_grid and "92" in fallback_grid
    assert "provider-budget" not in fallback_grid


def test_forecast_evidence_label_and_color_follow_each_horizon_not_growth_alone():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise the report's actual decision-rendering function")
    function = re.search(r"function renderForecastDecision\(f,valid\)\{.*?\n\}", _HTML, re.S).group(0)
    cases = [
        {"forecast": {"growth_pct": 161, "has_proven_gain": False,
                      "decision_label": "探索性结果：尚未证实稳定优于简单基准"}, "valid": True},
        {"forecast": {"growth_pct": 20, "has_proven_gain": True,
                      "decision_label": "有后期改善证据，仍需前瞻验证"}, "valid": True},
        {"forecast": {"growth_pct": 50}, "valid": True},
        {"forecast": {"growth_pct": 161, "has_proven_gain": True}, "valid": False},
    ]
    program = """
const nodes = Object.fromEntries(['forecast-decision','growth-value','growth-evidence-note'].map(k=>[k,{}]));
const $ = id => nodes[id];
const set = (id,text) => { nodes[id].textContent = text; };
const finite = x => typeof x === 'number' && Number.isFinite(x);
""" + function + "\nconst cases=" + json.dumps(cases, ensure_ascii=False) + """;
const results=cases.map(c=>{renderForecastDecision(c.forecast,c.valid);return JSON.parse(JSON.stringify(nodes));});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run([node, "-e", program], text=True, encoding="utf-8", capture_output=True, check=True)
    snapshots = json.loads(result.stdout)
    assert snapshots[0]["growth-value"]["className"] == "value fall"
    assert snapshots[0]["forecast-decision"]["className"] == "decision warning"
    assert snapshots[0]["forecast-decision"]["textContent"] == cases[0]["forecast"]["decision_label"]
    assert snapshots[1]["growth-value"]["className"] == "value rise"
    assert snapshots[1]["forecast-decision"]["className"] == "decision supported"
    assert snapshots[1]["forecast-decision"]["textContent"] == cases[1]["forecast"]["decision_label"]
    assert snapshots[2]["forecast-decision"]["className"] == "decision warning"
    assert snapshots[2]["growth-value"]["className"] == "value fall"
    assert snapshots[3]["forecast-decision"]["textContent"] == "数据不足：当前不作增长判断"
    assert snapshots[3]["growth-value"]["className"] == "value muted"


def test_build_offline_report_keeps_missing_values_and_blocks_script_breakout(tmp_path):
    attack = '</script><script id="INJECTED">alert(1)</script>'
    source = tmp_path / "analysis.json"
    source.write_text(json.dumps({"as_of": "2026-09-19", "ingredients": [{"id": "a", "name_cn": attack,
        "history": [{"date": "2026-09-19", "searches": None, "google_trend": float("nan")}],
        "forecasts": [{"horizon_weeks": 13, "status": "insufficient_data", "predicted_mean": None}]}],
        "news": {"as_of": "2026-09-23", "items": [{"title": attack, "published_at": "2026-09-01", "url": "javascript:alert(1)"}]}}), encoding="utf-8")
    output = build_report(source, tmp_path / "report.html")
    html = output.read_text(encoding="utf-8")
    assert '<script id="INJECTED">' not in html
    assert not re.search(r"<script[^>]+src=", html)
    assert "rangeslider:{visible:true" in html
    assert "plotly_relayout" in html
    assert 'id="horizon-13"' in html and 'id="horizon-26"' in html
    assert 'id="news-empty"' in html and 'id="backtest-empty"' in html
    assert 'id="forecast-decision"' in html and 'id="growth-evidence-note"' in html
    embedded = re.search(r'<script id="analysis-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    data = json.loads(embedded)
    assert data["ingredients"][0]["name_cn"] == attack
    assert data["ingredients"][0]["history"][0]["google_trend"] is None
    assert data["news"]["items"][0]["url"] is None
    assert "预测整个窗口的每周均值" in html
