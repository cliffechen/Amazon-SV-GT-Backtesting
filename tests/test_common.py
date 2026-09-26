import json
from pathlib import Path

import pytest

from predictor import common


def test_atomic_write_replaces_file_and_leaves_no_temporary_files(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    common.write_json(target, {"new": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_write_keeps_existing_target_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "state.txt"
    target.write_text("old", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(common.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        common.atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".state.txt.*.tmp")) == []
