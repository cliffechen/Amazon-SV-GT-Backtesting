"""Provider-isolated, budgeted collection with recoverable atomic storage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .common import now_iso
from .mcp_client import MCPError, MCPProviderError


class CollectionError(RuntimeError):
    pass


class QuotaError(CollectionError):
    pass


class CollectionStateError(CollectionError):
    pass


class StorageError(CollectionError):
    pass


FINAL_FAILURES = {
    "failed_before_send",
    "failed_unknown_charge",
    "failed_provider",
    "failed_invalid_response",
    "failed_storage",
    "corrupt_file",
}
COUNTED_STATUSES = {
    "reserved", "calling", "received", "staged", "saved",
    "failed_unknown_charge", "failed_provider", "failed_invalid_response",
    "failed_storage", "corrupt_file",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def value_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.")
    if not component or component in {".", ".."}:
        raise ValueError("Invalid path component")
    return component


def provider_cache_path(
    root: str | Path,
    provider: str,
    marketplace: str,
    ingredient_id: str,
    kind: str,
    snapshot_date: str | date,
    request: Mapping[str, Any],
) -> Path:
    provider = _safe_component(provider.casefold())
    if provider not in {"sellersprite", "sif"}:
        raise ValueError(f"Unsupported provider: {provider}")
    day = date.fromisoformat(str(snapshot_date))
    suffix = value_sha256(request)[:12]
    return (
        Path(root)
        / "data" / "raw" / provider / _safe_component(marketplace.upper())
        / _safe_component(ingredient_id) / _safe_component(kind)
        / f"{day.isoformat()}-{suffix}.json"
    )


def ensure_provider_destination(root: str | Path, provider: str, destination: str | Path) -> Path:
    allowed = (Path(root) / "data" / "raw" / _safe_component(provider.casefold())).resolve()
    target = Path(destination)
    if not target.is_absolute():
        target = Path(root) / target
    target = target.resolve()
    try:
        target.relative_to(allowed)
    except ValueError as exc:
        raise StorageError(f"Destination must stay inside {allowed}") from exc
    return target


def invalid_quarantine_path(root: str | Path, provider: str, job_id: str) -> Path:
    """Return the immutable evidence path for one paid invalid response."""
    return ensure_provider_destination(
        root,
        provider,
        Path(root) / "data" / "raw" / provider / "_quarantine" / "invalid"
        / f"{_safe_component(job_id)}.json",
    )


@dataclass(frozen=True)
class StagedFile:
    path: Path
    destination: Path
    sha256: str


def stage_json(destination: str | Path, value: Any, job_id: str) -> StagedFile:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    prefix = f".{destination.name}.{_safe_component(job_id)}."
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".stage", dir=destination.parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A second parse catches encoding/truncation before the file can become visible.
        json.loads(stage.read_text(encoding="utf-8"))
        if file_sha256(stage) != digest:
            raise StorageError("Staged file hash changed after write")
        return StagedFile(stage, destination, digest)
    except Exception:
        try:
            stage.unlink(missing_ok=True)
        finally:
            raise


def commit_stage(staged: StagedFile) -> Path:
    if not staged.path.exists() or file_sha256(staged.path) != staged.sha256:
        raise StorageError("Staged file is missing or has the wrong hash")
    if staged.destination.exists():
        if file_sha256(staged.destination) == staged.sha256:
            staged.path.unlink(missing_ok=True)
            return staged.destination
        raise StorageError(f"Immutable cache conflict at {staged.destination}")
    try:
        # A hard link makes publication atomic and fails rather than overwriting a
        # concurrently-created cache. Both files are always on the same volume.
        os.link(staged.path, staged.destination)
    except FileExistsError:
        if file_sha256(staged.destination) != staged.sha256:
            raise StorageError(f"Immutable cache conflict at {staged.destination}")
    except OSError as exc:
        # Do not fall back to os.replace: on POSIX it can overwrite another run.
        raise StorageError(f"Cannot atomically publish staged cache ({type(exc).__name__})") from exc
    staged.path.unlink(missing_ok=True)
    return staged.destination


@dataclass(frozen=True)
class ProviderQuota:
    provider: str
    period: str
    project_limit: int
    available: int
    account_reserve: int
    per_minute: int

    @property
    def hard_limit(self) -> int:
        return min(self.project_limit, max(0, self.available - self.account_reserve))


class ProviderLedger:
    """SQLite state machine shared by all collection providers.

    Existing SellerSprite rows in the legacy ``reservations`` table count toward
    the same cap, which prevents a migration from accidentally reopening budget.
    """

    def __init__(self, config: str | Path | Mapping[str, Any], database: str | Path) -> None:
        if isinstance(config, Mapping):
            self.config = dict(config)
        else:
            self.config = json.loads(Path(config).read_text(encoding="utf-8-sig"))
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS collection_jobs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    period TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    tool TEXT NOT NULL,
                    request TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    stage_path TEXT,
                    response_sha256 TEXT,
                    error TEXT,
                    dedupe_key TEXT NOT NULL UNIQUE
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS collection_provider_period ON collection_jobs(provider, period)")
            db.execute("CREATE INDEX IF NOT EXISTS collection_run ON collection_jobs(run_id)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _quota(self, provider: str) -> ProviderQuota:
        providers = self.config.get("providers") if isinstance(self.config, dict) else None
        entry = providers.get(provider) if isinstance(providers, dict) else None
        if entry is None and provider == "sellersprite" and "period" in self.config:
            entry = self.config
        if not isinstance(entry, dict):
            raise QuotaError(f"No explicit quota configured for provider {provider}")
        try:
            available = int(entry.get("available", entry.get("screenshot_available")))
            reserve = int(entry.get("reserve", entry.get("account_reserve", 0)))
            quota = ProviderQuota(
                provider=provider,
                period=str(entry["period"]),
                project_limit=int(entry["project_limit"]),
                available=available,
                account_reserve=reserve,
                per_minute=int(entry["per_minute"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QuotaError(f"Invalid quota configuration for {provider}") from exc
        if min(quota.project_limit, quota.available, quota.account_reserve, quota.per_minute) < 0:
            raise QuotaError(f"Quota values for {provider} cannot be negative")
        return quota

    @staticmethod
    def _period(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m")

    @staticmethod
    def _legacy_exists(db: sqlite3.Connection) -> bool:
        return bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'"
        ).fetchone())

    def _spent(self, db: sqlite3.Connection, provider: str, period: str) -> int:
        placeholders = ",".join("?" for _ in COUNTED_STATUSES)
        current = db.execute(
            f"SELECT COALESCE(SUM(units),0) FROM collection_jobs "
            f"WHERE provider=? AND period=? AND status IN ({placeholders})",
            (provider, period, *sorted(COUNTED_STATUSES)),
        ).fetchone()[0]
        legacy = 0
        if self._legacy_exists(db):
            legacy = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM reservations WHERE provider=? AND period=?",
                (provider, period),
            ).fetchone()[0]
        return int(current or 0) + int(legacy or 0)

    def reserve_job(
        self,
        *,
        run_id: str,
        provider: str,
        tool: str,
        request: Mapping[str, Any],
        destination: str | Path,
        units: int = 1,
        now: float | None = None,
    ) -> dict[str, Any]:
        provider = provider.casefold()
        quota = self._quota(provider)
        if type(units) is not int or units < 1:
            raise QuotaError("Units must be a positive integer")
        now = time.time() if now is None else now
        period = self._period(now)
        if period != quota.period:
            raise QuotaError(f"Quota period expired for {provider}")
        request_text = canonical_json(request)
        request_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        destination = str(Path(destination).resolve())
        dedupe = value_sha256([provider, tool, request, destination])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM collection_jobs WHERE dedupe_key=?", (dedupe,)
            ).fetchone()
            reactivate = bool(existing and existing["status"] == "failed_before_send")
            if existing and not reactivate:
                result = dict(existing)
                result["created_new"] = False
                return result
            spent = self._spent(db, provider, period)
            if spent + units > quota.hard_limit:
                raise QuotaError(
                    f"{provider} hard limit reached: {spent}+{units}>{quota.hard_limit}"
                )
            placeholders = ",".join("?" for _ in COUNTED_STATUSES)
            recent = db.execute(
                f"SELECT COUNT(*) FROM collection_jobs WHERE provider=? AND created>? "
                f"AND status IN ({placeholders})",
                (provider, now - 60, *sorted(COUNTED_STATUSES)),
            ).fetchone()[0]
            if provider == "sellersprite" and self._legacy_exists(db):
                recent += db.execute(
                    "SELECT COUNT(*) FROM reservations WHERE provider=? AND created>?",
                    (provider, now - 60),
                ).fetchone()[0]
            if recent >= quota.per_minute:
                raise QuotaError(f"{provider} per-minute limit reached")
            if reactivate:
                job_id = existing["id"]
                db.execute("""
                    UPDATE collection_jobs
                    SET run_id=?,period=?,created=?,updated=?,units=?,status='reserved',error=NULL
                    WHERE id=? AND status='failed_before_send'
                """, (str(run_id), period, now, now, units, job_id))
            else:
                job_id = str(uuid.uuid4())
                db.execute("""
                    INSERT INTO collection_jobs
                    (id,run_id,provider,period,created,updated,tool,request,request_hash,
                     units,status,destination,dedupe_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    job_id, str(run_id), provider, period, now, now, str(tool), request_text,
                    request_hash, units, "reserved", destination, dedupe,
                ))
        return {
            "id": job_id, "run_id": str(run_id), "provider": provider,
            "period": period, "tool": str(tool), "request": request_text,
            "request_hash": request_hash, "units": units, "status": "reserved",
            "destination": destination, "created_new": True,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise CollectionStateError(f"Unknown collection job {job_id}")
        return dict(row)

    def _transition(
        self,
        job_id: str,
        allowed: Iterable[str],
        status: str,
        **fields: Any,
    ) -> dict[str, Any]:
        allowed = tuple(allowed)
        if not allowed:
            raise ValueError("At least one source state is required")
        updates = {"status": status, "updated": time.time(), **fields}
        columns = ",".join(f"{name}=?" for name in updates)
        placeholders = ",".join("?" for _ in allowed)
        values = [updates[name] for name in updates]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                f"UPDATE collection_jobs SET {columns} WHERE id=? AND status IN ({placeholders})",
                (*values, job_id, *allowed),
            )
            if cursor.rowcount != 1:
                current = db.execute("SELECT status FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
                state = current[0] if current else "missing"
                raise CollectionStateError(
                    f"Cannot move collection job from {state} to {status}"
                )
        return self.get_job(job_id)

    def mark_calling(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, ("reserved",), "calling")

    def mark_received(self, job_id: str, response_sha256: str) -> dict[str, Any]:
        return self._transition(
            job_id, ("calling",), "received", response_sha256=response_sha256, error=None
        )

    def mark_staged(self, job_id: str, stage_path: str | Path, response_sha256: str) -> dict[str, Any]:
        return self._transition(
            job_id, ("received",), "staged", stage_path=str(Path(stage_path).resolve()),
            response_sha256=response_sha256, error=None,
        )

    def mark_revalidated_staged(
        self, job_id: str, stage_path: str | Path, response_sha256: str
    ) -> dict[str, Any]:
        """Move quarantined evidence back into the normal recovery path.

        This transition does not reserve a job or change its units. It is only
        valid for a response that was already paid for and rejected by the
        previous validator.
        """
        return self._transition(
            job_id,
            ("failed_invalid_response",),
            "staged",
            stage_path=str(Path(stage_path).resolve()),
            response_sha256=response_sha256,
            error=None,
        )

    def mark_saved(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, ("staged",), "saved", stage_path=None, error=None)

    def mark_failure(self, job_id: str, status: str, error: str) -> dict[str, Any]:
        if status not in FINAL_FAILURES:
            raise ValueError(f"Invalid failure state: {status}")
        allowed = (
            ("reserved",) if status == "failed_before_send"
            else ("calling", "received", "staged", "saved")
        )
        return self._transition(job_id, allowed, status, error=str(error)[:500])

    def status(self, provider: str) -> dict[str, Any]:
        provider = provider.casefold()
        quota = self._quota(provider)
        with self._connect() as db:
            spent = self._spent(db, provider, quota.period)
            rows = db.execute(
                "SELECT status,COUNT(*) count,COALESCE(SUM(units),0) units "
                "FROM collection_jobs WHERE provider=? AND period=? GROUP BY status",
                (provider, quota.period),
            ).fetchall()
        return {
            "provider": provider,
            "period": quota.period,
            "hard_limit": quota.hard_limit,
            "counted_units_including_legacy": spent,
            "remaining": max(0, quota.hard_limit - spent),
            "states": {row["status"]: {"jobs": row["count"], "units": row["units"]} for row in rows},
        }

    def recover(self, *, provider: str | None = None, run_id: str | None = None) -> dict[str, list[str]]:
        clauses, values = ["status IN ('staged','saved')"], []
        if provider:
            clauses.append("provider=?"); values.append(provider.casefold())
        if run_id:
            clauses.append("run_id=?"); values.append(str(run_id))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM collection_jobs WHERE " + " AND ".join(clauses), values
            ).fetchall()
        result: dict[str, list[str]] = {"recovered": [], "verified": [], "corrupt": []}
        for row in rows:
            job = dict(row)
            destination = Path(job["destination"])
            expected = job.get("response_sha256")
            if job["status"] == "saved":
                if destination.exists() and expected and file_sha256(destination) == expected:
                    result["verified"].append(job["id"])
                else:
                    self.mark_failure(job["id"], "corrupt_file", "saved cache missing or hash mismatch")
                    result["corrupt"].append(job["id"])
                continue
            stage = Path(job["stage_path"] or "")
            try:
                if destination.exists() and expected and file_sha256(destination) == expected:
                    self.mark_saved(job["id"])
                    result["recovered"].append(job["id"])
                    continue
                commit_stage(StagedFile(stage, destination, expected or ""))
                self.mark_saved(job["id"])
                result["recovered"].append(job["id"])
            except (OSError, StorageError) as exc:
                result["corrupt"].append(job["id"])
                # Keep staged state so a repaired filesystem can be retried.
                with self._connect() as db:
                    db.execute(
                        "UPDATE collection_jobs SET error=?,updated=? WHERE id=?",
                        (f"{type(exc).__name__}: {exc}"[:500], time.time(), job["id"]),
                    )
        return result


Validator = Callable[[dict[str, Any], Mapping[str, Any]], None]


class CollectionRunner:
    def __init__(self, ledger: ProviderLedger, client: Any, root: str | Path) -> None:
        self.ledger = ledger
        self.client = client
        self.root = Path(root).resolve()

    def collect(
        self,
        jobs: Iterable[Mapping[str, Any]],
        *,
        provider: str,
        run_id: str,
        validator: Validator,
        max_new_units: int,
        min_interval_seconds: float = 0,
    ) -> dict[str, Any]:
        if type(max_new_units) is not int or max_new_units < 0:
            raise ValueError("max_new_units must be a non-negative integer")
        provider = provider.casefold()
        summary: dict[str, Any] = {
            "provider": provider, "run_id": str(run_id), "saved": [],
            "cache_hits": [], "failed": [], "new_units": 0,
        }
        last_call_started: float | None = None
        for specification in jobs:
            request = specification.get("request")
            if not isinstance(request, Mapping):
                raise ValueError("Collection job request must be an object")
            destination = ensure_provider_destination(
                self.root, provider, specification["destination"]
            )
            units = int(specification.get("cost_units", 1))
            job = self.ledger.reserve_job(
                run_id=run_id,
                provider=provider,
                tool=str(specification["tool"]),
                request=request,
                destination=destination,
                units=units,
            )
            if not job["created_new"]:
                if job["status"] == "saved" and destination.exists() and job.get("response_sha256"):
                    if file_sha256(destination) == job["response_sha256"]:
                        summary["cache_hits"].append(job["id"])
                        continue
                if job["status"] == "staged":
                    recovered = self.ledger.recover(provider=provider, run_id=run_id)
                    if job["id"] in recovered["recovered"]:
                        summary["cache_hits"].append(job["id"])
                        continue
                if job["status"] in FINAL_FAILURES:
                    # A paid failure is final evidence, not permission to call the
                    # provider again. Report it and continue so later, previously
                    # unreserved jobs in the same plan can still run.
                    summary["failed"].append({
                        "id": job["id"],
                        "status": job["status"],
                        "error": job.get("error"),
                    })
                    continue
                raise CollectionStateError(
                    f"Existing job {job['id']} is {job['status']}; inspect/recover it without a new call"
                )
            if summary["new_units"] + units > max_new_units:
                self.ledger.mark_failure(
                    job["id"], "failed_before_send", "per-run max_new_units reached"
                )
                raise QuotaError(
                    f"Run cap reached before {specification.get('tool')}: "
                    f"{summary['new_units']}+{units}>{max_new_units}"
                )
            summary["new_units"] += units
            self.ledger.mark_calling(job["id"])
            try:
                if last_call_started is not None and min_interval_seconds > 0:
                    remaining = min_interval_seconds - (time.monotonic() - last_call_started)
                    if remaining > 0:
                        time.sleep(remaining)
                last_call_started = time.monotonic()
                result = self.client.call_tool(str(specification["tool"]), dict(request))
            except MCPProviderError as exc:
                self.ledger.mark_failure(job["id"], "failed_provider", str(exc))
                summary["failed"].append({"id": job["id"], "status": "failed_provider"})
                continue
            except MCPError as exc:
                self.ledger.mark_failure(job["id"], "failed_unknown_charge", str(exc))
                summary["failed"].append({"id": job["id"], "status": "failed_unknown_charge"})
                continue
            except Exception as exc:
                self.ledger.mark_failure(
                    job["id"], "failed_unknown_charge", f"{type(exc).__name__}"
                )
                summary["failed"].append({"id": job["id"], "status": "failed_unknown_charge"})
                continue

            envelope = {
                "source": provider,
                "tool": str(specification["tool"]),
                "request": dict(request),
                "fetched_at": now_iso(),
                "collection_run_id": str(run_id),
                "collection_job_id": job["id"],
                "result": result,
            }
            try:
                response_hash = value_sha256(envelope)
                self.ledger.mark_received(job["id"], response_hash)
                validator(result, specification)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                quarantine: Path | None = None
                try:
                    quarantine = self._quarantine_invalid_response(
                        provider=provider,
                        job_id=job["id"],
                        envelope=envelope,
                        validation_error=error,
                    )
                    reference = str(quarantine.relative_to(self.root)).replace("\\", "/")
                    ledger_error = f"{error}; quarantine={reference}"
                except (OSError, StorageError, TypeError, ValueError) as storage_exc:
                    ledger_error = (
                        f"{error}; quarantine_write_failed="
                        f"{type(storage_exc).__name__}: {storage_exc}"
                    )
                # Serialization can fail before the response reaches `received`.
                # Both calling and received are valid sources for a paid invalid
                # response failure.
                self.ledger.mark_failure(job["id"], "failed_invalid_response", ledger_error)
                failed = {
                    "id": job["id"],
                    "status": "failed_invalid_response",
                    "error": ledger_error,
                }
                if quarantine is not None:
                    failed["quarantine"] = str(quarantine)
                summary["failed"].append(failed)
                continue
            try:
                staged = stage_json(destination, envelope, job["id"])
                # The ledger verifies the actual serialized file, not the compact
                # in-memory JSON hash used above.
                response_hash = staged.sha256
                self.ledger.mark_staged(job["id"], staged.path, response_hash)
                commit_stage(staged)
                self.ledger.mark_saved(job["id"])
                summary["saved"].append({"id": job["id"], "path": str(destination)})
            except (OSError, StorageError, TypeError, ValueError) as exc:
                current = self.ledger.get_job(job["id"])
                if current["status"] == "received":
                    self.ledger.mark_failure(
                        job["id"], "failed_storage", f"{type(exc).__name__}: {exc}"
                    )
                # A staged file remains recoverable and intentionally keeps its state.
                summary["failed"].append({"id": job["id"], "status": self.ledger.get_job(job["id"])["status"]})
        return summary

    def _quarantine_invalid_response(
        self,
        *,
        provider: str,
        job_id: str,
        envelope: Mapping[str, Any],
        validation_error: str,
    ) -> Path:
        """Atomically preserve a complete paid response rejected by validation."""
        destination = invalid_quarantine_path(self.root, provider, job_id)
        evidence = dict(envelope)
        evidence["validation_error"] = validation_error
        staged = stage_json(destination, evidence, f"{job_id}-invalid")
        commit_stage(staged)
        return destination


def recover_quarantined_job(
    root: str | Path,
    ledger: ProviderLedger,
    job_id: str,
    validator: Validator,
) -> dict[str, str]:
    """Revalidate and publish one paid quarantined response without a new call.

    The quarantine envelope is matched to the ledger's original response hash,
    request, tool, provider, and job id before validation. Publication then uses
    the ordinary staged state so an interruption remains recoverable.
    """
    root = Path(root).resolve()
    job = ledger.get_job(job_id)
    if job["status"] != "failed_invalid_response":
        raise CollectionStateError(
            f"Collection job {job_id} is {job['status']}, not failed_invalid_response"
        )
    provider = str(job["provider"]).casefold()
    quarantine = invalid_quarantine_path(root, provider, job_id)
    if not quarantine.exists():
        raise StorageError(f"Quarantine evidence is missing for collection job {job_id}")
    try:
        evidence = json.loads(quarantine.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"Quarantine evidence is unreadable for collection job {job_id}") from exc
    if not isinstance(evidence, dict):
        raise StorageError(f"Quarantine evidence is not an object for collection job {job_id}")

    envelope = dict(evidence)
    envelope.pop("validation_error", None)
    try:
        request = json.loads(str(job["request"]))
    except json.JSONDecodeError as exc:
        raise StorageError(f"Ledger request is invalid for collection job {job_id}") from exc
    if not isinstance(request, dict):
        raise StorageError(f"Ledger request is not an object for collection job {job_id}")
    if (
        envelope.get("source") != provider
        or envelope.get("tool") != job["tool"]
        or envelope.get("collection_job_id") != job_id
        or envelope.get("request") != request
    ):
        raise StorageError(f"Quarantine evidence does not match collection job {job_id}")
    expected = job.get("response_sha256")
    if not expected or value_sha256(envelope) != expected:
        raise StorageError(f"Quarantine evidence hash does not match collection job {job_id}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise StorageError(f"Quarantine evidence has no result for collection job {job_id}")

    destination = ensure_provider_destination(root, provider, job["destination"])
    specification = {
        "tool": job["tool"],
        "request": request,
        "destination": destination,
        "cost_units": int(job["units"]),
    }
    validator(result, specification)
    staged = stage_json(destination, envelope, f"{job_id}-revalidated")
    try:
        ledger.mark_revalidated_staged(job_id, staged.path, staged.sha256)
    except Exception:
        staged.path.unlink(missing_ok=True)
        raise
    commit_stage(staged)
    ledger.mark_saved(job_id)
    return {
        "id": job_id,
        "status": "saved",
        "path": str(destination),
        "quarantine": str(quarantine),
    }


def build_sif_batch_jobs(
    root: str | Path,
    snapshot_date: str | date,
    items: Iterable[Mapping[str, str]],
    *,
    batch_size: int = 5,
) -> list[dict[str, Any]]:
    """Build capped SIF batch jobs (one credit per batch, never per-word fallback)."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    normalized = [
        {"id": str(item["id"]), "keyword": str(item["keyword"])} for item in items
    ]
    if len({item["id"] for item in normalized}) != len(normalized):
        raise ValueError("SIF collection items contain duplicate ingredient ids")
    jobs: list[dict[str, Any]] = []
    for offset in range(0, len(normalized), batch_size):
        batch_items = normalized[offset:offset + batch_size]
        request = {
            "keywords": [item["keyword"] for item in batch_items],
            "country": "US",
            "granularity": "week",
        }
        jobs.append({
            "batch_index": offset // batch_size,
            "items": batch_items,
            "tool": "market_get_keyword_history",
            "request": request,
            "cost_units": 1,
            "destination": provider_cache_path(
                root, "sif", "US", "_batches", "history", snapshot_date, request
            ),
        })
    return jobs


def build_sellersprite_amazon_jobs(
    root: str | Path,
    snapshot_date: str | date,
    items: Iterable[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """Build one SellerSprite Amazon-history job per explicitly mapped item."""
    jobs = []
    seen = set()
    for item in items:
        ingredient_id, keyword = str(item["id"]), str(item["keyword"])
        if ingredient_id in seen:
            raise ValueError(f"Duplicate ingredient id: {ingredient_id}")
        seen.add(ingredient_id)
        request = {"keyword": keyword, "marketplace": "US", "timeGranularity": "W"}
        jobs.append({
            "ingredient_id": ingredient_id,
            "keyword": keyword,
            "tool": "aba_research_trend",
            "request": request,
            "cost_units": 1,
            "destination": provider_cache_path(
                root, "sellersprite", "US", ingredient_id, "amazon", snapshot_date, request
            ),
        })
    return jobs


def mcp_text_body(result: Mapping[str, Any], provider: str) -> dict[str, Any]:
    blocks = result.get("content")
    if not isinstance(blocks, list):
        raise ValueError(f"{provider} result has no content array")
    text = next(
        (block.get("text") for block in blocks
         if isinstance(block, dict) and block.get("type") == "text"
         and isinstance(block.get("text"), str)),
        None,
    )
    if text is None:
        raise ValueError(f"{provider} result has no text content")
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError(f"{provider} text content must decode to an object")
    return body


def validate_sellersprite_history_result(result: dict[str, Any], job: Mapping[str, Any]) -> None:
    """Validate SellerSprite's weekly ABA payload before it reaches immutable storage."""
    body = mcp_text_body(result, "SellerSprite")
    rows = body.get("data")
    if body.get("code") != "OK" or not isinstance(rows, list) or not rows:
        raise ValueError("SellerSprite provider did not return a non-empty OK history")
    labels = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("SellerSprite history contains a non-object row")
        label = str(row.get("label", ""))
        if len(label) != 8 or not label.isdigit():
            raise ValueError("SellerSprite history contains an invalid weekly label")
        if "searches" not in row or "rank" not in row:
            raise ValueError("SellerSprite history row is missing searches or rank")
        labels.append(label)
    if len(labels) != len(set(labels)):
        raise ValueError("SellerSprite history contains duplicate weekly labels")


def sif_history_body(result: Mapping[str, Any]) -> dict[str, Any]:
    body = mcp_text_body(result, "SIF")
    if "code" in body:
        if body.get("code") not in ("OK", 0, 200):
            raise ValueError("SIF provider returned a non-success code")
        body = body.get("data")
    if not isinstance(body, dict):
        raise ValueError("SIF history body is not an object")
    return body


def _normalized_sif_history_entry(
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any], int, int]:
    """Validate one weekly history and remove only proven redundant observations.

    SIF can return the same Sunday twice. That duplication is harmless when both
    measurements agree. One observed provider anomaly also emits a Saturday
    immediately before the equivalent Sunday. That Saturday is removable only
    when the two adjacent rows are one calendar day apart and both measurements
    agree. Every other non-Sunday remains invalid.
    """
    dates, volumes, ranks = entry.get("dates"), entry.get("volumes"), entry.get("ranks")
    if not all(isinstance(values, list) and values for values in (dates, volumes, ranks)):
        raise ValueError("SIF keyword history is empty or missing an array")
    if len(dates) != len(volumes) or len(dates) != len(ranks):
        raise ValueError("SIF keyword history arrays have different lengths")

    parsed_dates: list[date] = []
    previous: date | None = None
    for raw_day in dates:
        if not isinstance(raw_day, str):
            raise ValueError("SIF keyword history contains a non-string date")
        try:
            parsed = date.fromisoformat(raw_day)
        except ValueError as exc:
            raise ValueError("SIF keyword history contains an invalid ISO date") from exc
        if parsed.isoformat() != raw_day:
            raise ValueError("SIF keyword history date is not canonical ISO format")
        if previous is not None and parsed < previous:
            raise ValueError("SIF keyword history dates are not in ascending order")
        parsed_dates.append(parsed)
        previous = parsed

    normalized_dates: list[str] = []
    normalized_volumes: list[Any] = []
    normalized_ranks: list[Any] = []
    observations: dict[str, tuple[Any, Any]] = {}
    same_date_deduplicated_count = 0
    misaligned_duplicate_count = 0
    for index, (raw_day, volume, rank, parsed) in enumerate(
        zip(dates, volumes, ranks, parsed_dates)
    ):
        observation = (volume, rank)
        if parsed.weekday() != 6:
            next_index = index + 1
            is_adjacent_saturday_copy = (
                parsed.weekday() == 5
                and next_index < len(parsed_dates)
                and parsed_dates[next_index] == parsed + timedelta(days=1)
                and parsed_dates[next_index].weekday() == 6
                and (volumes[next_index], ranks[next_index]) == observation
            )
            if not is_adjacent_saturday_copy:
                raise ValueError("SIF keyword history contains an unsupported non-Sunday date")
            misaligned_duplicate_count += 1
            continue

        if raw_day in observations:
            if observations[raw_day] != observation:
                raise ValueError(
                    "SIF keyword history contains conflicting observations for one date"
                )
            same_date_deduplicated_count += 1
            continue

        observations[raw_day] = observation
        normalized_dates.append(raw_day)
        normalized_volumes.append(volume)
        normalized_ranks.append(rank)

    normalized = dict(entry)
    normalized["dates"] = normalized_dates
    normalized["volumes"] = normalized_volumes
    normalized["ranks"] = normalized_ranks
    latest = normalized.get("latest")
    if latest is not None:
        if not isinstance(latest, Mapping):
            raise ValueError("SIF latest value is not an object")
        if (
            normalized_dates[-1] != str(latest.get("date"))
            or normalized_volumes[-1] != latest.get("volume")
            or normalized_ranks[-1] != latest.get("rank")
        ):
            raise ValueError("SIF latest values do not match the history tail")
    return normalized, same_date_deduplicated_count, misaligned_duplicate_count


def validate_sif_batch_result(result: dict[str, Any], job: Mapping[str, Any]) -> None:
    """Require exactly one valid history for every requested keyword."""
    body = sif_history_body(result)
    entries = body.get("keywords")
    if not isinstance(entries, list) or not entries:
        raise ValueError("SIF result has no keyword histories")
    requested = [str(value).casefold() for value in job["request"]["keywords"]]
    returned = [
        str(entry.get("keyword", "")).casefold()
        for entry in entries if isinstance(entry, dict)
    ]
    if len(returned) != len(entries) or len(returned) != len(set(returned)):
        raise ValueError("SIF result contains an invalid or duplicate keyword")
    if set(returned) != set(requested):
        raise ValueError("SIF returned keyword set does not exactly match the request")
    for entry in entries:
        _normalized_sif_history_entry(entry)


def materialize_sif_batch_caches(
    root: str | Path,
    snapshot_date: str | date,
    jobs: Iterable[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Atomically derive one pipeline cache per keyword from full raw batches."""
    root = Path(root).resolve()
    summary: dict[str, list[str]] = {"written": [], "reused": []}
    for job in jobs:
        raw_path = ensure_provider_destination(root, "sif", job["destination"])
        if not raw_path.exists():
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        result = raw.get("result")
        if not isinstance(result, dict):
            raise StorageError(f"Raw SIF batch has no result: {raw_path}")
        validate_sif_batch_result(result, job)
        body = sif_history_body(result)
        returned = {str(entry["keyword"]).casefold(): entry for entry in body["keywords"]}
        for item in job["items"]:
            keyword = str(item["keyword"])
            entry = returned[keyword.casefold()]
            (
                normalized_entry,
                same_date_deduplicated_count,
                misaligned_duplicate_count,
            ) = _normalized_sif_history_entry(entry)
            request = {"keywords": [keyword], "country": "US", "granularity": "week"}
            envelope = {
                "source": "sif",
                "tool": "market_get_keyword_history",
                "request": request,
                "fetched_at": raw.get("fetched_at"),
                "collection_run_id": raw.get("collection_run_id"),
                "collection_job_id": raw.get("collection_job_id"),
                "result": {
                    "country": str(body.get("country") or "US"),
                    "granularity": str(body.get("granularity") or "week"),
                    "keywords": [normalized_entry],
                },
                "provenance": {
                    "raw_batch_path": str(raw_path.relative_to(root)).replace("\\", "/"),
                    "raw_batch_sha256": file_sha256(raw_path),
                    "deduplicated_count": (
                        same_date_deduplicated_count + misaligned_duplicate_count
                    ),
                    "same_date_deduplicated_count": same_date_deduplicated_count,
                    "misaligned_duplicate_count": misaligned_duplicate_count,
                    "transformation": (
                        "one keyword selected from a validated full batch response; "
                        "identical same-date and adjacent Saturday/Sunday duplicate "
                        "observations removed"
                    ),
                },
            }
            destination = provider_cache_path(
                root, "sif", "US", str(item["id"]), "amazon", snapshot_date, request
            )
            if destination.exists():
                if json.loads(destination.read_text(encoding="utf-8")) != envelope:
                    raise StorageError(f"Immutable derived SIF cache conflict at {destination}")
                summary["reused"].append(str(destination))
                continue
            staged = stage_json(
                destination, envelope,
                f"derive-{raw.get('collection_job_id') or job.get('batch_index')}-{item['id']}",
            )
            commit_stage(staged)
            summary["written"].append(str(destination))
    return summary


def load_quota_config(root: str | Path, provider: str, explicit: str | Path | None = None) -> Path:
    """Resolve quota configuration without granting SIF an implicit budget."""
    if explicit:
        return Path(explicit)
    root = Path(root)
    shared = root / "config" / "provider_budgets.json"
    if shared.exists():
        return shared
    if provider.casefold() == "sellersprite":
        legacy = root / "config" / "budget.json"
        if legacy.exists():
            return legacy
    raise QuotaError(
        f"No explicit {provider} quota config. Create config/provider_budgets.json "
        "from the example before any live collection."
    )
