"""Collect all selected SIF histories in capped batches of at most five.

Each call is saved as a complete immutable raw batch. Only after exact response
keyword validation is it split atomically into provider-isolated per-ingredient
caches. Failed batches are never retried as single-keyword calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from predictor.collection import (  # noqa: E402
    CollectionRunner,
    ProviderLedger,
    build_sif_batch_jobs,
    load_quota_config,
    materialize_sif_batch_caches,
    validate_sif_batch_result,
)
from predictor.mcp_client import MCPClient  # noqa: E402
from predictor.provider_config import load_provider_url  # noqa: E402


BATCH_SIZE = 5


def selected_items(root: Path) -> list[dict[str, str]]:
    catalog = json.loads((root / "data/processed/catalog.json").read_text(encoding="utf-8"))
    return [
        {
            "id": str(item["id"]),
            "keyword": str((item.get("queries") or {}).get("sif") or item["keyword"]),
        }
        for item in catalog
        if item.get("selected")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-date", default=str(date.today()))
    parser.add_argument("--run-id")
    parser.add_argument("--provider-config")
    parser.add_argument("--quota-config")
    parser.add_argument("--ledger", default=str(ROOT / "data/budget.sqlite"))
    parser.add_argument("--max-new-units", type=int, required=True,
                        help="Hard cap for this run; failed batches are not retried")
    args = parser.parse_args(argv)

    date.fromisoformat(args.snapshot_date)
    run_id = args.run_id or f"sif-{args.snapshot_date}-{uuid.uuid4().hex[:8]}"
    jobs = build_sif_batch_jobs(
        ROOT, args.snapshot_date, selected_items(ROOT), batch_size=BATCH_SIZE
    )
    if args.max_new_units < len(jobs):
        raise SystemExit(
            f"Refusing partial implicit collection: {len(jobs)} planned batches exceed "
            f"--max-new-units={args.max_new_units}. Build a smaller explicit plan instead."
        )
    url = load_provider_url("sif", config_path=args.provider_config)
    quota = load_quota_config(ROOT, "sif", args.quota_config)
    ledger = ProviderLedger(quota, args.ledger)
    with MCPClient(url, client_name="amazon-sv-gt-sif-collector") as client:
        summary = CollectionRunner(ledger, client, ROOT).collect(
            jobs,
            provider="sif",
            run_id=run_id,
            validator=validate_sif_batch_result,
            max_new_units=args.max_new_units,
            min_interval_seconds=2.2,
        )
    summary["derived_caches"] = materialize_sif_batch_caches(
        ROOT, args.snapshot_date, jobs
    )
    summary["budget"] = ledger.status("sif")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
