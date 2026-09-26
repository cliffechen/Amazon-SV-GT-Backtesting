import json
import sqlite3
from pathlib import Path

import pytest

from predictor.collection import (
    CollectionRunner,
    ProviderLedger,
    QuotaError,
    StagedFile,
    StorageError,
    build_sif_batch_jobs,
    build_sellersprite_amazon_jobs,
    commit_stage,
    file_sha256,
    materialize_sif_batch_caches,
    provider_cache_path,
    recover_quarantined_job,
    stage_json,
    validate_sif_batch_result,
    validate_sellersprite_history_result,
)
from predictor.mcp_client import MCPProviderError


NOW = 1790150400.0  # 2026-09 UTC


def quotas(seller=10, sif=2):
    return {"providers": {
        "sellersprite": {"period": "2026-09", "available": seller,
                         "reserve": 0, "project_limit": seller, "per_minute": 100},
        "sif": {"period": "2026-09", "available": sif,
                "reserve": 0, "project_limit": sif, "per_minute": 100},
    }}


class FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def call_tool(self, tool, request):
        self.calls.append((tool, request))
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def job(root, provider="sif", keyword="a"):
    request = {"keywords": [keyword], "country": "US", "granularity": "week"}
    return {
        "tool": "market_get_keyword_history",
        "request": request,
        "cost_units": 1,
        "destination": provider_cache_path(
            root, provider, "US", "batches", "history", "2026-09-23", request
        ),
    }


def test_provider_caps_are_independent_and_sif_has_hard_limit(tmp_path):
    ledger = ProviderLedger(quotas(), tmp_path / "ledger.sqlite")
    for index in range(2):
        ledger.reserve_job(run_id="sif-run", provider="sif", tool="history",
                           request={"i": index}, destination=tmp_path / f"sif-{index}", now=NOW)
    with pytest.raises(QuotaError, match="hard limit"):
        ledger.reserve_job(run_id="sif-run", provider="sif", tool="history",
                           request={"i": 3}, destination=tmp_path / "sif-3", now=NOW)
    seller = ledger.reserve_job(run_id="seller-run", provider="sellersprite", tool="history",
                                request={"i": 1}, destination=tmp_path / "seller", now=NOW)
    assert seller["created_new"] is True
    assert ledger.status("sif")["remaining"] == 0
    assert ledger.status("sellersprite")["remaining"] == 9


def test_legacy_sellersprite_reservations_still_consume_new_ledger_cap(tmp_path):
    database = tmp_path / "ledger.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE reservations (id TEXT PRIMARY KEY, period TEXT, created REAL, "
                   "provider TEXT, tool TEXT, request TEXT, units INTEGER, status TEXT, cache_path TEXT)")
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?)",
                   ("legacy", "2026-09", NOW, "sellersprite", "history", "{}", 9, "saved", "x"))
    ledger = ProviderLedger(quotas(seller=10), database)
    ledger.reserve_job(run_id="r", provider="sellersprite", tool="history",
                       request={"i": 1}, destination=tmp_path / "one", now=NOW)
    with pytest.raises(QuotaError):
        ledger.reserve_job(run_id="r", provider="sellersprite", tool="history",
                           request={"i": 2}, destination=tmp_path / "two", now=NOW)


def test_runner_is_idempotent_and_never_calls_provider_for_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(), tmp_path / "ledger.sqlite")
    client = FakeClient([{"content": []}])
    runner = CollectionRunner(ledger, client, tmp_path)
    specification = job(tmp_path)
    first = runner.collect([specification], provider="sif", run_id="run",
                           validator=lambda result, spec: None, max_new_units=1)
    assert len(first["saved"]) == 1
    destination = specification["destination"]
    original = destination.read_bytes()
    second = runner.collect([specification], provider="sif", run_id="run",
                            validator=lambda result, spec: None, max_new_units=1)
    assert len(second["cache_hits"]) == 1
    assert len(client.calls) == 1
    assert destination.read_bytes() == original


def test_provider_failure_is_counted_once_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(), tmp_path / "ledger.sqlite")
    client = FakeClient([MCPProviderError("provider failed")])
    specification = job(tmp_path)
    summary = CollectionRunner(ledger, client, tmp_path).collect(
        [specification], provider="sif", run_id="run",
        validator=lambda result, spec: None, max_new_units=1,
    )
    assert len(client.calls) == 1
    assert summary["failed"][0]["status"] == "failed_provider"
    assert not specification["destination"].exists()
    assert ledger.status("sif")["counted_units_including_legacy"] == 1


def test_run_cap_stops_before_network_and_releases_unissued_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(sif=10), tmp_path / "ledger.sqlite")
    client = FakeClient([{"content": []}])
    with pytest.raises(QuotaError, match="Run cap"):
        CollectionRunner(ledger, client, tmp_path).collect(
            [job(tmp_path)], provider="sif", run_id="run",
            validator=lambda result, spec: None, max_new_units=0,
        )
    assert client.calls == []
    assert ledger.status("sif")["counted_units_including_legacy"] == 0
    assert ledger.status("sif")["states"]["failed_before_send"]["jobs"] == 1


def test_atomic_publish_refuses_to_overwrite_existing_cache(tmp_path):
    destination = tmp_path / "cache.json"
    destination.write_text('{"original":true}\n', encoding="utf-8")
    original = destination.read_bytes()
    staged = stage_json(destination, {"replacement": True}, "job-1")
    with pytest.raises(StorageError, match="conflict"):
        commit_stage(staged)
    assert destination.read_bytes() == original
    assert staged.path.exists()


def test_recover_commits_staged_file_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(), tmp_path / "ledger.sqlite")
    specification = job(tmp_path)
    registered = ledger.reserve_job(
        run_id="recover", provider="sif", tool=specification["tool"],
        request=specification["request"], destination=specification["destination"], now=NOW,
    )
    ledger.mark_calling(registered["id"])
    ledger.mark_received(registered["id"], "temporary")
    staged = stage_json(specification["destination"], {"ok": True}, registered["id"])
    ledger.mark_staged(registered["id"], staged.path, staged.sha256)
    result = ledger.recover(provider="sif", run_id="recover")
    assert result["recovered"] == [registered["id"]]
    assert specification["destination"].exists()
    assert file_sha256(specification["destination"]) == staged.sha256
    assert ledger.get_job(registered["id"])["status"] == "saved"


def test_destination_must_be_inside_provider_tree(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(), tmp_path / "ledger.sqlite")
    client = FakeClient([{"content": []}])
    outside = job(tmp_path)
    outside["destination"] = tmp_path / "data/raw/sellersprite/US/wrong.json"
    with pytest.raises(StorageError, match="inside"):
        CollectionRunner(ledger, client, tmp_path).collect(
            [outside], provider="sif", run_id="run",
            validator=lambda result, spec: None, max_new_units=1,
        )
    assert not client.calls


def sif_result(keywords):
    entries = [{
        "keyword": keyword,
        "dates": ["2026-09-06", "2026-09-13"],
        "volumes": [10, 20],
        "ranks": [100, 90],
        "latest": {"date": "2026-09-13", "volume": 20, "rank": 90},
    } for keyword in keywords]
    body = {"country": "US", "granularity": "week", "keywords": entries}
    return {"content": [{"type": "text", "text": json.dumps(body)}]}


def test_sif_52_items_are_eleven_batches_of_at_most_five(tmp_path):
    items = [{"id": f"ingredient-{index}", "keyword": f"keyword {index}"} for index in range(52)]
    jobs = build_sif_batch_jobs(tmp_path, "2026-09-23", items)
    assert len(jobs) == 11
    assert all(len(job["request"]["keywords"]) <= 5 for job in jobs)
    assert sum(len(job["items"]) for job in jobs) == 52
    assert {job["tool"] for job in jobs} == {"market_get_keyword_history"}
    assert all("data\\raw\\sif" in str(job["destination"]) or "data/raw/sif" in str(job["destination"])
               for job in jobs)


def test_sif_batch_requires_exact_returned_keyword_set(tmp_path):
    job_spec = build_sif_batch_jobs(
        tmp_path, "2026-09-23",
        [{"id": "a", "keyword": "alpha"}, {"id": "b", "keyword": "beta"}],
    )[0]
    validate_sif_batch_result(sif_result(["beta", "alpha"]), job_spec)
    with pytest.raises(ValueError, match="exactly"):
        validate_sif_batch_result(sif_result(["alpha"]), job_spec)
    with pytest.raises(ValueError, match="exactly"):
        validate_sif_batch_result(sif_result(["alpha", "gamma"]), job_spec)


def test_sif_allows_identical_duplicate_sundays_but_rejects_conflicts_and_bad_order(tmp_path):
    job_spec = build_sif_batch_jobs(
        tmp_path, "2026-09-23", [{"id": "a", "keyword": "alpha"}],
    )[0]
    duplicate = sif_result(["alpha"])
    entry = json.loads(duplicate["content"][0]["text"])["keywords"][0]
    entry["dates"] = ["2026-09-06", "2026-09-06", "2026-09-13"]
    entry["volumes"] = [10, 10, 20]
    entry["ranks"] = [100, 100, 90]
    duplicate["content"][0]["text"] = json.dumps({
        "country": "US", "granularity": "week", "keywords": [entry],
    })
    validate_sif_batch_result(duplicate, job_spec)

    conflicting = json.loads(json.dumps(duplicate))
    conflicting_entry = json.loads(conflicting["content"][0]["text"])["keywords"][0]
    conflicting_entry["volumes"][1] = 11
    conflicting["content"][0]["text"] = json.dumps({
        "country": "US", "granularity": "week", "keywords": [conflicting_entry],
    })
    with pytest.raises(ValueError, match="conflicting"):
        validate_sif_batch_result(conflicting, job_spec)

    reversed_result = sif_result(["alpha"])
    reversed_entry = json.loads(reversed_result["content"][0]["text"])["keywords"][0]
    for field in ("dates", "volumes", "ranks"):
        reversed_entry[field].reverse()
    reversed_entry["latest"] = {
        "date": reversed_entry["dates"][-1],
        "volume": reversed_entry["volumes"][-1],
        "rank": reversed_entry["ranks"][-1],
    }
    reversed_result["content"][0]["text"] = json.dumps({
        "country": "US", "granularity": "week", "keywords": [reversed_entry],
    })
    with pytest.raises(ValueError, match="ascending"):
        validate_sif_batch_result(reversed_result, job_spec)

    weekday_result = sif_result(["alpha"])
    weekday_entry = json.loads(weekday_result["content"][0]["text"])["keywords"][0]
    weekday_entry["dates"][0] = "2026-09-07"
    weekday_result["content"][0]["text"] = json.dumps({
        "country": "US", "granularity": "week", "keywords": [weekday_entry],
    })
    with pytest.raises(ValueError, match="Sunday"):
        validate_sif_batch_result(weekday_result, job_spec)


def test_full_sif_batch_is_atomically_materialized_to_per_ingredient_caches(tmp_path):
    items = [{"id": "a", "keyword": "alpha"}, {"id": "b", "keyword": "beta"}]
    jobs = build_sif_batch_jobs(tmp_path, "2026-09-23", items)
    raw_result = sif_result(["alpha", "beta"])
    raw_envelope = {
        "source": "sif", "tool": "market_get_keyword_history",
        "request": jobs[0]["request"], "fetched_at": "2026-09-23T00:00:00Z",
        "collection_run_id": "run", "collection_job_id": "job", "result": raw_result,
    }
    commit_stage(stage_json(jobs[0]["destination"], raw_envelope, "raw-job"))
    first = materialize_sif_batch_caches(tmp_path, "2026-09-23", jobs)
    assert len(first["written"]) == 2
    for item in items:
        request = {"keywords": [item["keyword"]], "country": "US", "granularity": "week"}
        path = provider_cache_path(
            tmp_path, "sif", "US", item["id"], "amazon", "2026-09-23", request
        )
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["source"] == "sif"
        assert envelope["tool"] == "market_get_keyword_history"
        assert envelope["result"]["keywords"][0]["keyword"] == item["keyword"]
        assert envelope["provenance"]["raw_batch_sha256"] == file_sha256(jobs[0]["destination"])
    second = materialize_sif_batch_caches(tmp_path, "2026-09-23", jobs)
    assert len(second["reused"]) == 2


def test_sif_materialization_deduplicates_identical_rows_and_records_count(tmp_path):
    jobs = build_sif_batch_jobs(
        tmp_path, "2026-09-23", [{"id": "a", "keyword": "alpha"}],
    )
    raw_result = sif_result(["alpha"])
    body = json.loads(raw_result["content"][0]["text"])
    entry = body["keywords"][0]
    entry["dates"] = ["2026-09-06", "2026-09-06", "2026-09-13"]
    entry["volumes"] = [10, 10, 20]
    entry["ranks"] = [100, 100, 90]
    raw_result["content"][0]["text"] = json.dumps(body)
    raw_envelope = {
        "source": "sif", "tool": "market_get_keyword_history",
        "request": jobs[0]["request"], "fetched_at": "2026-09-23T00:00:00Z",
        "collection_run_id": "run", "collection_job_id": "job", "result": raw_result,
    }
    commit_stage(stage_json(jobs[0]["destination"], raw_envelope, "raw-job"))

    materialize_sif_batch_caches(tmp_path, "2026-09-23", jobs)
    request = {"keywords": ["alpha"], "country": "US", "granularity": "week"}
    path = provider_cache_path(tmp_path, "sif", "US", "a", "amazon", "2026-09-23", request)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    normalized = envelope["result"]["keywords"][0]
    assert normalized["dates"] == ["2026-09-06", "2026-09-13"]
    assert normalized["volumes"] == [10, 20]
    assert normalized["ranks"] == [100, 90]
    assert envelope["provenance"]["deduplicated_count"] == 1
    assert envelope["provenance"]["same_date_deduplicated_count"] == 1
    assert envelope["provenance"]["misaligned_duplicate_count"] == 0


def test_sif_only_allows_identical_adjacent_saturday_sunday_copy(tmp_path):
    jobs = build_sif_batch_jobs(
        tmp_path, "2026-09-23", [{"id": "a", "keyword": "alpha"}],
    )
    result = sif_result(["alpha"])
    body = json.loads(result["content"][0]["text"])
    entry = body["keywords"][0]
    entry["dates"] = ["2021-05-01", "2021-05-02", "2026-09-06", "2026-09-13"]
    entry["volumes"] = [7, 7, 10, 20]
    entry["ranks"] = [70, 70, 100, 90]
    result["content"][0]["text"] = json.dumps(body)
    validate_sif_batch_result(result, jobs[0])

    raw_envelope = {
        "source": "sif", "tool": "market_get_keyword_history",
        "request": jobs[0]["request"], "fetched_at": "2026-09-23T00:00:00Z",
        "collection_run_id": "run", "collection_job_id": "job", "result": result,
    }
    commit_stage(stage_json(jobs[0]["destination"], raw_envelope, "raw-job"))
    materialize_sif_batch_caches(tmp_path, "2026-09-23", jobs)
    request = {"keywords": ["alpha"], "country": "US", "granularity": "week"}
    path = provider_cache_path(tmp_path, "sif", "US", "a", "amazon", "2026-09-23", request)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    normalized = envelope["result"]["keywords"][0]
    assert normalized["dates"] == ["2021-05-02", "2026-09-06", "2026-09-13"]
    assert envelope["provenance"]["same_date_deduplicated_count"] == 0
    assert envelope["provenance"]["misaligned_duplicate_count"] == 1
    assert envelope["provenance"]["deduplicated_count"] == 1

    mismatched = json.loads(json.dumps(result))
    mismatched_body = json.loads(mismatched["content"][0]["text"])
    mismatched_body["keywords"][0]["volumes"][0] = 8
    mismatched["content"][0]["text"] = json.dumps(mismatched_body)
    with pytest.raises(ValueError, match="unsupported non-Sunday"):
        validate_sif_batch_result(mismatched, jobs[0])

    friday = json.loads(json.dumps(result))
    friday_body = json.loads(friday["content"][0]["text"])
    friday_body["keywords"][0]["dates"][0] = "2021-04-30"
    friday["content"][0]["text"] = json.dumps(friday_body)
    with pytest.raises(ValueError, match="unsupported non-Sunday"):
        validate_sif_batch_result(friday, jobs[0])


def test_invalid_paid_response_is_atomically_quarantined_and_referenced(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(sif=3), tmp_path / "ledger.sqlite")
    specification = job(tmp_path, keyword="alpha")
    invalid = sif_result(["wrong-keyword"])
    summary = CollectionRunner(ledger, FakeClient([invalid]), tmp_path).collect(
        [specification], provider="sif", run_id="run",
        validator=validate_sif_batch_result, max_new_units=1,
    )

    failed = summary["failed"][0]
    assert failed["status"] == "failed_invalid_response"
    quarantine = list((tmp_path / "data/raw/sif/_quarantine/invalid").glob("*.json"))
    assert len(quarantine) == 1
    evidence = json.loads(quarantine[0].read_text(encoding="utf-8"))
    assert evidence["result"] == invalid
    assert evidence["request"] == specification["request"]
    ledger_job = ledger.get_job(failed["id"])
    assert "quarantine=data/raw/sif/_quarantine/invalid/" in ledger_job["error"]
    assert not specification["destination"].exists()


def test_existing_final_failure_is_reported_without_retry_and_later_job_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(sif=3), tmp_path / "ledger.sqlite")
    first = job(tmp_path, keyword="alpha")
    second = job(tmp_path, keyword="beta")
    CollectionRunner(ledger, FakeClient([sif_result(["wrong-keyword"])]), tmp_path).collect(
        [first], provider="sif", run_id="run",
        validator=validate_sif_batch_result, max_new_units=1,
    )

    client = FakeClient([sif_result(["beta"])])
    summary = CollectionRunner(ledger, client, tmp_path).collect(
        [first, second], provider="sif", run_id="run",
        validator=validate_sif_batch_result, max_new_units=1,
    )
    assert len(client.calls) == 1
    assert client.calls[0][1]["keywords"] == ["beta"]
    assert summary["failed"][0]["status"] == "failed_invalid_response"
    assert len(summary["saved"]) == 1
    assert second["destination"].exists()


def test_quarantined_job_can_be_revalidated_and_published_without_new_charge(tmp_path, monkeypatch):
    monkeypatch.setattr("predictor.collection.time.time", lambda: NOW)
    ledger = ProviderLedger(quotas(sif=3), tmp_path / "ledger.sqlite")
    specification = job(tmp_path, keyword="alpha")
    result = sif_result(["alpha"])
    body = json.loads(result["content"][0]["text"])
    entry = body["keywords"][0]
    entry["dates"] = ["2021-05-01", "2021-05-02", "2026-09-06", "2026-09-13"]
    entry["volumes"] = [7, 7, 10, 20]
    entry["ranks"] = [70, 70, 100, 90]
    result["content"][0]["text"] = json.dumps(body)
    client = FakeClient([result])
    first = CollectionRunner(ledger, client, tmp_path).collect(
        [specification], provider="sif", run_id="run",
        validator=lambda response, spec: (_ for _ in ()).throw(ValueError("old validator")),
        max_new_units=1,
    )
    job_id = first["failed"][0]["id"]
    spent_before = ledger.status("sif")["counted_units_including_legacy"]

    recovered = recover_quarantined_job(
        tmp_path, ledger, job_id, validate_sif_batch_result,
    )

    assert recovered["status"] == "saved"
    assert ledger.get_job(job_id)["status"] == "saved"
    assert ledger.status("sif")["counted_units_including_legacy"] == spent_before == 1
    assert len(client.calls) == 1
    assert specification["destination"].exists()
    published = json.loads(specification["destination"].read_text(encoding="utf-8"))
    assert "validation_error" not in published
    assert published["result"] == result
    assert Path(recovered["quarantine"]).exists()


def test_sellersprite_full_cohort_builder_only_creates_amazon_jobs(tmp_path):
    items = [{"id": f"ingredient-{index}", "keyword": f"keyword {index}"} for index in range(52)]
    jobs = build_sellersprite_amazon_jobs(tmp_path, "2026-09-23", items)
    assert len(jobs) == 52
    assert {job["tool"] for job in jobs} == {"aba_research_trend"}
    assert all("/amazon/" in str(job["destination"]).replace("\\", "/") for job in jobs)
    assert all(job["request"]["timeGranularity"] == "W" for job in jobs)


def test_sellersprite_validator_rejects_bad_or_duplicate_rows():
    def result(rows, code="OK"):
        return {"content": [{"type": "text", "text": json.dumps({"code": code, "data": rows})}]}

    good = [{"label": "20260912", "searches": 100, "rank": 20}]
    validate_sellersprite_history_result(result(good), {})
    with pytest.raises(ValueError, match="duplicate"):
        validate_sellersprite_history_result(result(good + good), {})
    with pytest.raises(ValueError, match="non-empty OK"):
        validate_sellersprite_history_result(result(good, code="ERROR"), {})
