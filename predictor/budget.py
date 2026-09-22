"""Conservative, atomic reservation ledger; does not pretend to know live balance."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from .common import ROOT, read_json


class BudgetError(RuntimeError): pass


class Budget:
    def __init__(self, config=None, database=None):
        self.config = read_json(config or ROOT / "config/budget.json")
        self.database = Path(database or ROOT / "data/budget.sqlite")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, period TEXT, created REAL, provider TEXT, tool TEXT, request TEXT, units INTEGER, status TEXT, cache_path TEXT)")
            if "dedupe_key" not in {r[1] for r in db.execute("PRAGMA table_info(reservations)")}:
                db.execute("ALTER TABLE reservations ADD COLUMN dedupe_key TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS reservation_dedupe ON reservations(dedupe_key)")
            for row in db.execute("SELECT * FROM reservations WHERE dedupe_key IS NULL AND cache_path IS NOT NULL").fetchall():
                key = hashlib.sha256(json.dumps([row["provider"], row["tool"], json.loads(row["request"]), row["cache_path"]], sort_keys=True).encode()).hexdigest()
                if not db.execute("SELECT 1 FROM reservations WHERE dedupe_key=?", (key,)).fetchone():
                    db.execute("UPDATE reservations SET dedupe_key=? WHERE id=?", (key, row["id"]))

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def reserve(self, tool, request, provider="sellersprite", units=1, now=None, cache_key=None):
        now = time.time() if now is None else now
        period = datetime.fromtimestamp(now).strftime("%Y-%m")
        c = self.config
        if provider != "sellersprite": raise BudgetError("This ledger authorizes SellerSprite only; Sorftime needs a separate confirmed credit budget")
        if type(units) is not int or units < 1: raise BudgetError("Units must be a positive integer")
        if period != c["period"]: raise BudgetError("Budget period expired; enter a fresh account balance before calling paid tools")
        limit = min(c["project_limit"], max(0, c["screenshot_available"] - c["account_reserve"]))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dedupe = hashlib.sha256(json.dumps([provider, tool, request, cache_key], sort_keys=True).encode()).hexdigest() if cache_key else None
            if dedupe and db.execute("SELECT 1 FROM reservations WHERE dedupe_key=?", (dedupe,)).fetchone():
                raise BudgetError("This cache job was already reserved; inspect its saved/failed state instead of repeating a paid call")
            spent = db.execute("SELECT COALESCE(SUM(units),0) FROM reservations WHERE period=? AND provider=?", (period, provider)).fetchone()[0]
            if spent + units > limit: raise BudgetError(f"Project hard limit reached: {spent}+{units}>{limit}")
            recent = db.execute("SELECT COUNT(*) FROM reservations WHERE created>? AND provider=?", (now - 60, provider)).fetchone()[0]
            if recent >= c["per_minute"]: raise BudgetError("Rate limit: wait before requesting another reservation")
            token = str(uuid.uuid4())
            db.execute("INSERT INTO reservations (id,period,created,provider,tool,request,units,status,cache_path,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?)", (token, period, now, provider, tool, json.dumps(request, sort_keys=True), units, "reserved", None, dedupe))
        return {"reservation_id": token, "units": units, "remaining_project": limit - spent - units}

    def complete(self, token, cache_path, status="saved"):
        if status not in {"saved", "failed"}: raise ValueError("Invalid reservation status")
        with self._connect() as db:
            cur = db.execute("UPDATE reservations SET status=?,cache_path=? WHERE id=? AND status='reserved'", (status, str(cache_path) if cache_path else None, token))
            if cur.rowcount != 1: raise BudgetError("Unknown or already completed reservation")

    def status(self):
        c = self.config
        with self._connect() as db:
            units = db.execute("SELECT COALESCE(SUM(units),0) FROM reservations WHERE period=?", (c["period"],)).fetchone()[0]
            calls = db.execute("SELECT COUNT(*) FROM reservations WHERE period=?", (c["period"],)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM reservations WHERE period=? AND status='reserved'", (c["period"],)).fetchone()[0]
        limit = min(c["project_limit"], max(0, c["screenshot_available"]-c["account_reserve"]))
        return {**c, "charged_attempts": calls, "reserved_units": units, "pending": pending,
                "remaining_project": max(0, limit-units), "estimated_remaining_account": max(0,c["screenshot_available"]-units),
                "balance_is_live": False, "note": "按所有已预约调用保守计费，包括失败；账户余额仅由截图减本项目调用估算，其他会话用量不可见。"}
