"""Convert legacy compact SIF entries into isolated, immutable derived files.

No file below the legacy SellerSprite-compatible ``data/raw/US`` tree is ever
written. These inputs lack complete original JSON-RPC responses, so their
provenance is explicitly marked as legacy compact/transcribed evidence.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from predictor.collection import (  # noqa: E402
    commit_stage,
    provider_cache_path,
    stage_json,
    value_sha256,
)


BATCHES = [
    ["probiotics", "magnesium", "vitamin c"],
    ["collagen peptides", "curcumin", "zinc"],
    ["biotin", "omega 3", "glutathione"],
    ["tongkat ali", "saw palmetto", "vitamin b12"],
]
BATCH_TAG = "reviewed_batch2_2026-09-23"
WEEK_END_CUTOFF = date(2026, 9, 12)


def main() -> int:
    batch_dir = ROOT / "data/raw/US/_sif_batches"
    grid = json.loads((batch_dir / "grid-dates.json").read_text(encoding="utf-8"))["dates"]
    catalog = json.loads((ROOT / "data/processed/catalog.json").read_text(encoding="utf-8"))
    by_keyword = {
        item["keyword"]: item for item in catalog
        if item.get("query_status") == BATCH_TAG and item.get("selected")
    }
    expected = {keyword for batch in BATCHES for keyword in batch}
    if set(by_keyword) != expected:
        raise RuntimeError(f"Catalog/batch mismatch: {sorted(set(by_keyword) ^ expected)}")

    entries = {}
    for path in sorted((batch_dir / "entries").glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if path.stem.endswith("-crosscheck"):
            continue
        keyword = str(entry.get("keyword", ""))
        if keyword in entries:
            raise RuntimeError(f"Duplicate compact SIF entry: {keyword}")
        entries[keyword] = entry
    if set(entries) != expected:
        raise RuntimeError(f"Entry/batch mismatch: {sorted(set(entries) ^ expected)}")

    cutoff_start = WEEK_END_CUTOFF - timedelta(days=6)
    written, reused = [], []
    for keyword in sorted(expected):
        entry = entries[keyword]
        dates = entry.get("dates") or grid
        volumes, ranks = entry.get("volumes"), entry.get("ranks")
        if not all(isinstance(values, list) for values in (dates, volumes, ranks)):
            raise RuntimeError(f"{keyword}: missing history arrays")
        if not dates or len(dates) != len(volumes) or len(dates) != len(ranks):
            raise RuntimeError(f"{keyword}: history array length mismatch")
        latest = entry.get("latest")
        if latest and not (
            str(dates[-1]) == str(latest.get("date"))
            and volumes[-1] == latest.get("volume")
            and ranks[-1] == latest.get("rank")
        ):
            raise RuntimeError(f"{keyword}: tail does not match compact latest field")

        keep = []
        seen = set()
        for index, value in enumerate(dates):
            day = date.fromisoformat(str(value))
            if day.weekday() != 6:
                raise RuntimeError(f"{keyword}: non-Sunday SIF label {day}")
            if day <= cutoff_start and day not in seen:
                seen.add(day)
                keep.append(index)
        if not keep:
            raise RuntimeError(f"{keyword}: no completed weeks through {WEEK_END_CUTOFF}")
        kept_dates = [str(dates[index]) for index in keep]
        kept_volumes = [volumes[index] for index in keep]
        kept_ranks = [ranks[index] for index in keep]
        compact = {
            "keyword": keyword,
            "dates": kept_dates,
            "volumes": kept_volumes,
            "ranks": kept_ranks,
            "latest": {
                "date": kept_dates[-1],
                "volume": kept_volumes[-1],
                "rank": kept_ranks[-1],
            },
        }
        envelope = {
            "source": "sif",
            "tool": "sif_market_get_keyword_history",
            "request": {"keywords": [keyword], "country": "US", "granularity": "week"},
            "fetched_at": entry.get("fetched_at"),
            "result": {"country": "US", "granularity": "week", "keywords": [compact]},
            "provenance": {
                "evidence_class": (
                    "legacy_programmatic_compact" if entry.get("fetched_at")
                    else "legacy_transcribed_unverifiable"
                ),
                "original_response_preserved": False,
                "source_entry": str((batch_dir / "entries" / f"{keyword.replace(' ', '-')}.json").relative_to(ROOT)),
                "transformation": "SIF Sunday labels retained; later ingestion maps week end to label+6 days",
                "week_end_cutoff": WEEK_END_CUTOFF.isoformat(),
                "raw_latest_before_cutoff": latest,
                "limitations": "Only array lengths and tail values are verifiable; exact MCP call count and middle-value transcription are not independently auditable.",
            },
        }
        digest = value_sha256(envelope)
        item = by_keyword[keyword]
        destination = provider_cache_path(
            ROOT, "sif", "US", item["id"], "amazon", "2026-09-23",
            envelope["request"],
        )
        if destination.exists():
            reused.append(str(destination))
            continue
        staged = stage_json(destination, envelope, f"legacy-sif-{item['id']}-{digest[:12]}")
        commit_stage(staged)
        written.append(str(destination))

    print(json.dumps({"written": written, "reused": reused, "total": len(expected)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
