"""Budgeted SellerSprite batch-2 collector using immutable provider storage.

This script never writes the legacy ``data/raw/US`` tree. Credentials come
from ASVGT_SELLERSPRITE_MCP_URL or an explicitly selected user config file.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

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


KEYWORDS = {
    "probiotics": "probiotics",
    "magnesium": "magnesium",
    "vitamin c": "vitamin-c",
    "collagen peptides": "collagen-peptides",
    "curcumin": "curcumin",
    "zinc": "zinc",
    "biotin": "biotin",
    "omega 3": "omega-3",
    "glutathione": "glutathione",
    "tongkat ali": "tongkat-ali",
    "saw palmetto": "saw-palmetto",
    "vitamin b12": "vitamin-b12",
    "berberine": "berberine-control",
}


def build_jobs(root: Path, snapshot_date: str) -> list[dict[str, Any]]:
    return build_sellersprite_amazon_jobs(
        root, snapshot_date,
        [{"id": ingredient_id, "keyword": keyword}
         for keyword, ingredient_id in KEYWORDS.items()],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-date", default=str(date.today()))
    parser.add_argument("--run-id")
    parser.add_argument("--provider-config")
    parser.add_argument("--quota-config")
    parser.add_argument("--ledger", default=str(ROOT / "data/budget.sqlite"))
    parser.add_argument("--max-new-units", type=int, default=len(KEYWORDS))
    args = parser.parse_args(argv)

    date.fromisoformat(args.snapshot_date)
    run_id = args.run_id or f"sellersprite-{args.snapshot_date}-{uuid.uuid4().hex[:8]}"
    url = load_provider_url("sellersprite", config_path=args.provider_config)
    quota = load_quota_config(ROOT, "sellersprite", args.quota_config)
    ledger = ProviderLedger(quota, args.ledger)
    jobs = build_jobs(ROOT, args.snapshot_date)
    with MCPClient(url, client_name="amazon-sv-gt-sellersprite-collector") as client:
        summary = CollectionRunner(ledger, client, ROOT).collect(
            jobs,
            provider="sellersprite",
            run_id=run_id,
            validator=validate_sellersprite_history_result,
            max_new_units=args.max_new_units,
            min_interval_seconds=2.2,
        )
    summary["budget"] = ledger.status("sellersprite")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
