import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from predictor.report import _HTML, _prepare_payload, _safe_url, _script_json, build_report


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
