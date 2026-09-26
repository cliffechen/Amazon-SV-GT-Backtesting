from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import ROOT, read_json, write_json


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="成分需求预测：数据审计、预算、回测和离线HTML")
    parser.add_argument("--root", default=str(ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("audit"); p.add_argument("--source"); p.add_argument("--mappings")
    p = sub.add_parser("plan"); p.add_argument("--as-of"); p.add_argument("--limit", type=int, default=52); p.add_argument("--ids", nargs="*"); p.add_argument("--amazon-provider", choices=("sellersprite", "sif"), default="sellersprite"); p.add_argument("--refresh-amazon", action="store_true")
    sub.add_parser("budget")
    p = sub.add_parser("collect-status"); p.add_argument("--provider", choices=("sellersprite", "sif"), required=True); p.add_argument("--quota-config"); p.add_argument("--ledger", default="data/budget.sqlite")
    p = sub.add_parser("collect-recover"); p.add_argument("--provider", choices=("sellersprite", "sif")); p.add_argument("--run-id"); p.add_argument("--quota-config"); p.add_argument("--ledger", default="data/budget.sqlite")
    p = sub.add_parser("reserve"); p.add_argument("--plan"); p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("complete"); p.add_argument("--token", required=True); p.add_argument("--path"); p.add_argument("--failed", action="store_true")
    p = sub.add_parser("prepare"); p.add_argument("--as-of"); p.add_argument("--amazon-provider", choices=("sellersprite", "sif"), default="sellersprite")
    p = sub.add_parser("analyze"); p.add_argument("--output", default="outputs/latest"); p.add_argument("--dataset-id"); p.add_argument("--panel"); p.add_argument("--manifest")
    p = sub.add_parser("report"); p.add_argument("--analysis", default="outputs/latest/analysis.json"); p.add_argument("--output", default="outputs/latest/report.html"); p.add_argument("--comparison-analysis")
    p = sub.add_parser("audit-summary"); p.add_argument("--analysis", default="outputs/latest/analysis.json"); p.add_argument("--comparison-analysis"); p.add_argument("--output", required=True)
    p = sub.add_parser("run"); p.add_argument("--as-of"); p.add_argument("--output"); p.add_argument("--amazon-provider", choices=("sellersprite", "sif"), default="sellersprite"); p.add_argument("--comparison-analysis"); p.add_argument("--no-publish", action="store_true")
    p = sub.add_parser("enrich"); p.add_argument("--as-of"); p.add_argument("--analysis", default="outputs/latest/analysis.json")
    p = sub.add_parser("news-queries"); p.add_argument("--as-of"); p.add_argument("--ids", nargs="*")
    p = sub.add_parser("news-import"); p.add_argument("--input", required=True); p.add_argument("--as-of")
    args = parser.parse_args(argv); root = Path(args.root)
    if args.command == "audit":
        from .catalog import build_catalog
        result = build_catalog(args.source or root/"ingredient_db.json", root/"data/processed",
                               mappings_path=args.mappings or root/"config/catalog_mappings.json")
    elif args.command == "plan":
        from .data import collection_plan
        try:
            result = collection_plan(root, args.as_of, args.limit, ids=args.ids,
                                     amazon_provider=args.amazon_provider,
                                     refresh_amazon=args.refresh_amazon)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command in {"budget", "reserve", "complete"}:
        from .budget import Budget
        budget = Budget(root/"config/budget.json", root/"data/budget.sqlite")
        if args.command == "budget": result = budget.status()
        elif args.command == "reserve":
            plan = read_json(args.plan or root/"data/collection_plan.json")
            job = plan["jobs"][args.index]
            result = {**budget.reserve(job["tool"], job["request"], units=job.get("cost_units", 1), cache_key=job["cache_path"]), "job": job}
        else:
            budget.complete(args.token, args.path, "failed" if args.failed else "saved"); result = budget.status()
    elif args.command in {"collect-status", "collect-recover"}:
        from .collection import ProviderLedger, load_quota_config
        provider = args.provider or "sellersprite"
        quota = load_quota_config(root, provider, args.quota_config)
        ledger = ProviderLedger(quota, root / args.ledger)
        result = (ledger.status(provider) if args.command == "collect-status"
                  else ledger.recover(provider=args.provider, run_id=args.run_id))
    elif args.command == "prepare":
        from .data import assemble_panel
        result = assemble_panel(root, args.as_of, amazon_provider=args.amazon_provider)
    elif args.command == "analyze":
        from .model import run_analysis
        if args.dataset_id:
            dataset_dir = root / "data/processed/datasets" / args.dataset_id
            panel_path, manifest_path = dataset_dir / "panel.csv", dataset_dir / "manifest.json"
        else:
            panel_path = root / (args.panel or "data/processed/panel.csv")
            manifest_path = root / args.manifest if args.manifest else None
        result = run_analysis(panel_path, root/args.output, root/"data/processed/catalog.json",
                              panel_manifest_path=manifest_path)
        result = {"output": str(root/args.output/"analysis.json"), "ingredients": len(result.get("ingredients", []))}
    elif args.command == "report":
        from .report import build_report
        comparison = root / args.comparison_analysis if args.comparison_analysis else None
        result = {"path": str(build_report(root/args.analysis, root/args.output, comparison_path=comparison))}
    elif args.command == "audit-summary":
        from .audit_summary import build_audit_summary
        comparison = root / args.comparison_analysis if args.comparison_analysis else None
        result = build_audit_summary(root/args.analysis, root/args.output, comparison_path=comparison)
    elif args.command == "run":
        from .pipeline import run_pipeline
        output = root / args.output if args.output else None
        comparison = root / args.comparison_analysis if args.comparison_analysis else None
        result = run_pipeline(root, output, args.as_of, amazon_provider=args.amazon_provider,
                              comparison_analysis=comparison,
                              publish=False if args.no_publish else None)
    elif args.command == "enrich":
        from .pipeline import enrich_analysis
        enrich_analysis(root/args.analysis, root, args.as_of)
        result = {"path": str(root/args.analysis)}
    elif args.command in {"news-queries", "news-import"}:
        from datetime import date
        from .news import build_search_queries, import_news
        as_of = args.as_of or str(date.today())
        if args.command == "news-queries":
            from .data import validate_requested_ids
            catalog = read_json(root/"data/processed/catalog.json")
            try:
                validate_requested_ids(catalog, args.ids)
            except ValueError as exc:
                parser.error(str(exc))
            selected = [x for x in catalog if x["selected"] and (not args.ids or x["id"] in args.ids)]
            result = build_search_queries(selected, as_of)
            write_json(root/"data/news/search_queries.json", result)
        else:
            result = import_news(args.input, root/"data/news/verified_seed.json", as_of=as_of, catalog_path=root/"data/processed/catalog.json")
            write_json(root/"data/news/digest.json", result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__": main()
