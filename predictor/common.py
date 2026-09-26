from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_write_bytes(path, value):
    """Durably replace one file without exposing a partially-written target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(path, value, encoding="utf-8"):
    return atomic_write_bytes(path, value.encode(encoding))


def write_json(path, value):
    path = Path(path)
    return atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
