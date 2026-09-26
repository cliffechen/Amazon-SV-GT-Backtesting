"""Build an isolated legacy-SIF dataset manifest without swapping main caches.

The old implementation temporarily replaced SellerSprite files and left the
shared processed panel contaminated. This replacement only writes immutable SIF
derived artifacts plus a versioned manifest. A manifest-aware pipeline can
consume it later without touching the primary dataset.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from predictor.collection import (  # noqa: E402
    commit_stage,
    file_sha256,
    stage_json,
    value_sha256,
)


LEGACY_DIRS = [
    ROOT / "data/raw/US/_sif_batches/entries",
    ROOT / "data/raw/US/_sif_batches/entries2",
]
GRID_PATH = ROOT / "data/raw/US/_sif_batches/grid-dates.json"


def load_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for directory in LEGACY_DIRS:
        for path in sorted(directory.glob("*.json")):
            entry = json.loads(path.read_text(encoding="utf-8"))
            keyword = str(entry.get("keyword", "")).casefold()
            if not keyword:
                raise RuntimeError(f"Missing keyword in {path}")
            if keyword in entries:
                raise RuntimeError(f"Duplicate legacy SIF keyword across inputs: {keyword}")
            entry["_source_path"] = str(path.relative_to(ROOT))
            entries[keyword] = entry
    return entries


def seller_cache(item_id: str, cutoff: date) -> tuple[Path, set[str]] | None:
    directory = ROOT / "data/raw/US" / item_id / "amazon"
    candidates = sorted(
        (path for path in directory.glob("*.json")
         if path.stem[:10] <= cutoff.isoformat()),
        reverse=True,
    )
    for path in candidates:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if envelope.get("source", "sellersprite") != "sellersprite":
            continue
        try:
            body = json.loads(envelope["result"]["content"][0]["text"])
            rows = body["data"]
            weeks = {date.fromisoformat(f"{str(row['label'])[:4]}-{str(row['label'])[4:6]}-{str(row['label'])[6:]}").isoformat()
                     for row in rows}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid SellerSprite cache {path}") from exc
        return path, weeks
    return None


def build_dataset(dataset_id: str, cutoff: date, min_weeks: int) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", dataset_id):
        raise ValueError("dataset_id may contain only letters, numbers, dot, underscore and dash")
    catalog = json.loads((ROOT / "data/processed/catalog.json").read_text(encoding="utf-8"))
    entries = load_entries()
    grid = json.loads(GRID_PATH.read_text(encoding="utf-8"))["dates"]
    built, skipped = [], []

    for item in (row for row in catalog if row.get("selected")):
        ingredient_id, keyword = str(item["id"]), str(item["keyword"])
        entry = entries.get(keyword.casefold())
        seller = seller_cache(ingredient_id, cutoff)
        if entry is None:
            skipped.append({"id": ingredient_id, "reason": "no legacy SIF entry"})
            continue
        if seller is None:
            skipped.append({"id": ingredient_id, "reason": "no valid SellerSprite pairing cache"})
            continue
        seller_path, seller_weeks = seller
        dates = entry.get("dates") or grid
        volumes, ranks = entry.get("volumes"), entry.get("ranks")
        if not all(isinstance(values, list) for values in (dates, volumes, ranks)):
            raise RuntimeError(f"{ingredient_id}: missing SIF arrays")
        if not dates or len(dates) != len(volumes) or len(dates) != len(ranks):
            raise RuntimeError(f"{ingredient_id}: SIF array length mismatch")
        latest = entry.get("latest")
        if latest and not (
            str(dates[-1]) == str(latest.get("date"))
            and volumes[-1] == latest.get("volume")
            and ranks[-1] == latest.get("rank")
        ):
            raise RuntimeError(f"{ingredient_id}: legacy SIF tail mismatch")

        aligned = []
        seen = set()
        for index, raw_day in enumerate(dates):
            sunday = date.fromisoformat(str(raw_day))
            if sunday.weekday() != 6:
                raise RuntimeError(f"{ingredient_id}: non-Sunday SIF label {sunday}")
            week_end = sunday + timedelta(days=6)
            label = week_end.isoformat()
            if week_end <= cutoff and label in seller_weeks and label not in seen:
                seen.add(label)
                aligned.append(index)
        if len(aligned) < min_weeks:
            skipped.append({
                "id": ingredient_id,
                "reason": f"only {len(aligned)} exact overlapping weeks (<{min_weeks})",
            })
            continue

        compact = {
            "keyword": keyword,
            "dates": [str(dates[index]) for index in aligned],
            "volumes": [volumes[index] for index in aligned],
            "ranks": [ranks[index] for index in aligned],
        }
        compact["latest"] = {
            "date": compact["dates"][-1],
            "volume": compact["volumes"][-1],
            "rank": compact["ranks"][-1],
        }
        envelope = {
            "source": "sif",
            "tool": "sif_market_get_keyword_history",
            "request": {"keywords": [keyword], "country": "US", "granularity": "week"},
            "fetched_at": entry.get("fetched_at"),
            "result": {"country": "US", "granularity": "week", "keywords": [compact]},
            "provenance": {
                "evidence_class": (
                    "legacy_programmatic_compact" if entry.get("provenance")
                    else "legacy_transcribed_unverifiable"
                ),
                "original_response_preserved": False,
                "source_entry": entry["_source_path"],
                "seller_pair_sha256": file_sha256(seller_path),
                "exact_week_intersection": True,
                "seller_weeks": len(seller_weeks),
                "overlap_weeks": len(aligned),
                "week_end_cutoff": cutoff.isoformat(),
            },
        }
        digest = value_sha256(envelope)
        destination = (
            ROOT / "data/derived/sif/US" / ingredient_id / "amazon"
            / f"{cutoff.isoformat()}-{digest[:12]}.json"
        )
        if not destination.exists():
            commit_stage(stage_json(
                destination, envelope, f"{dataset_id}-{ingredient_id}-{digest[:12]}"
            ))
        built.append({
            "ingredient_id": ingredient_id,
            "keyword": keyword,
            "sif_path": str(destination.relative_to(ROOT)),
            "sif_sha256": file_sha256(destination),
            "seller_path": str(seller_path.relative_to(ROOT)),
            "seller_sha256": file_sha256(seller_path),
            "overlap_weeks": len(aligned),
            "first_sif_sunday": compact["dates"][0],
            "last_sif_sunday": compact["dates"][-1],
        })

    manifest = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "provider": "sif",
        "marketplace": "US",
        "week_end_cutoff": cutoff.isoformat(),
        "pairing": "exact intersection with each ingredient's SellerSprite week labels",
        "source_class": "legacy compact; original SIF JSON-RPC responses unavailable",
        "entries": built,
        "skipped": skipped,
        "analysis_status": "Requires a manifest-aware pipeline; main caches were not swapped.",
    }
    destination = ROOT / "data/processed/datasets" / dataset_id / "manifest.json"
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if existing != manifest:
            raise RuntimeError(f"Immutable dataset manifest conflict at {destination}")
    else:
        commit_stage(stage_json(destination, manifest, f"manifest-{dataset_id}"))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week-end-cutoff", default="2026-09-12")
    parser.add_argument("--dataset-id")
    parser.add_argument("--min-weeks", type=int, default=104)
    args = parser.parse_args(argv)
    cutoff = date.fromisoformat(args.week_end_cutoff)
    dataset_id = args.dataset_id or f"sif-legacy-{cutoff.isoformat()}"
    manifest = build_dataset(dataset_id, cutoff, args.min_weeks)
    print(json.dumps({
        "manifest": str(ROOT / "data/processed/datasets" / dataset_id / "manifest.json"),
        "built": len(manifest["entries"]),
        "skipped": manifest["skipped"],
        "main_cache_mutations": 0,
        "pipeline_ran": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
