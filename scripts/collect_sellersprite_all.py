"""Collect Amazon histories for every selected SellerSprite mapping.

Only ``aba_research_trend`` is called; this command does not collect Google
trends. The explicit per-run cap must equal the current selected cohort size so
an accidental partial full-cohort run cannot be mistaken for complete data.
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
    build_sellersprite_amazon_jobs,
    load_quota_config,
    validate_sellersprite_history_result,
)
from predictor.mcp_client import MCPClient  # noqa: E402
from predictor.provider_config import load_provider_url  # noqa: E402


def selected_items(root: Path) -> list[dict[str, str]]:
    catalog = json.loads((root / "data/processed/catalog.json").read_text(encoding="utf-8"))
    return [
        {
            "id": str(item["id"]),
            "keyword": str((item.get("queries") or {}).get("sellersprite") or item["keyword"]),
        }
        for item in catalog if item.get("selected")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-date", default=str(date.today()))
    parser.add_argument("--run-id")
    parser.add_argument("--provider-config")
    parser.add_argument("--quota-config")
    parser.add_argument("--ledger", default=str(ROOT / "data/budget.sqlite"))
    parser.add_argument("--max-new-units", type=int, required=True)
    args = parser.parse_args(argv)

    date.fromisoformat(args.snapshot_date)
    items = selected_items(ROOT)
    jobs = build_sellersprite_amazon_jobs(ROOT, args.snapshot_date, items)
    if args.max_new_units != len(jobs):
        raise SystemExit(
            f"Full selected cohort contains {len(jobs)} jobs; pass "
            f"--max-new-units {len(jobs)} explicitly. No network call was made."
        )
    run_id = args.run_id or f"sellersprite-all-{args.snapshot_date}-{uuid.uuid4().hex[:8]}"
    url = load_provider_url("sellersprite", config_path=args.provider_config)
    quota = load_quota_config(ROOT, "sellersprite", args.quota_config)
    ledger = ProviderLedger(quota, args.ledger)
    with MCPClient(url, client_name="amazon-sv-gt-sellersprite-all") as client:
        summary = CollectionRunner(ledger, client, ROOT).collect(
            jobs,
            provider="sellersprite",
            run_id=run_id,
            validator=validate_sellersprite_history_result,
            max_new_units=args.max_new_units,
            min_interval_seconds=2.2,
        )
    summary["selected_ingredients"] = len(items)
    summary["google_calls"] = 0
    summary["budget"] = ledger.status("sellersprite")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
