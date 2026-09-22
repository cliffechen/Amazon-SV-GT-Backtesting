"""Explicit IDs must not cause silent omissions or unintended batch work."""
import json

import pytest

from predictor.cli import main
from predictor.data import collection_plan


@pytest.fixture
def catalog_root(tmp_path):
    path = tmp_path / "data/processed/catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([
        {"id": "urolithin-a", "keyword": "urolithin a", "family_id": "urolithin-a", "selected": True, "brands": ["Timeline"]},
        {"id": "creatine", "keyword": "creatine", "family_id": "creatine", "selected": True, "brands": []},
        {"id": "5-htp", "keyword": None, "family_id": "5-htp", "selected": False, "brands": []},
    ]), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("ids, expected", [(["does-not-exist"], "未知成分 ID"), (["5-htp"], "selected=false"), ([], "至少一个成分 ID"), (["urolithin-a", "does-not-exist"], "未知成分 ID")])
def test_bad_plan_ids_fail_before_overwriting_an_existing_plan(catalog_root, ids, expected):
    previous = catalog_root / "data/collection_plan.json"
    previous.write_text("existing plan", encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        collection_plan(catalog_root, "2026-09-23", ids=ids)
    assert previous.read_text(encoding="utf-8") == "existing plan"


def test_omitted_ids_keep_default_batch_and_explicit_ids_stay_narrow(catalog_root):
    default = collection_plan(catalog_root, "2026-09-23")
    assert default["selected_ingredients"] == 2
    assert default["planned_calls"] == 4
    single = collection_plan(catalog_root, "2026-09-23", ids=["urolithin-a"])
    assert single["selected_ingredients"] == 1
    assert {job["ingredient_id"] for job in single["jobs"]} == {"urolithin-a"}


@pytest.mark.parametrize("command", ["plan", "news-queries"])
@pytest.mark.parametrize("ids", [["unknown"], ["5-htp"], []])
def test_cli_invalid_ids_exit_with_actionable_chinese_error(catalog_root, capsys, command, ids):
    with pytest.raises(SystemExit) as exit_info:
        main(["--root", str(catalog_root), command, "--as-of", "2026-09-23", "--ids", *ids])
    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "keyword/family_id" in output.err
    assert "selected=true" in output.err
    assert "未执行任何任务" in output.err
    assert output.out == ""
    assert not (catalog_root / "data/news/search_queries.json").exists()
    assert not (catalog_root / "data/collection_plan.json").exists()


def test_news_queries_without_ids_still_covers_only_selected_catalog(catalog_root, capsys):
    main(["--root", str(catalog_root), "news-queries", "--as-of", "2026-09-23"])
    rows = json.loads(capsys.readouterr().out)
    assert {row["ingredient_id"] for row in rows} == {"urolithin-a", "creatine"}
