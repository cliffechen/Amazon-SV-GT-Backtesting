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
    p = sub.add_parser("audit"); p.add_argument("--source")
    p = sub.add_parser("plan"); p.add_argument("--as-of"); p.add_argument("--limit", type=int, default=40); p.add_argument("--ids", nargs="*")
    sub.add_parser("budget")
    p = sub.add_parser("reserve"); p.add_argument("--plan"); p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("complete"); p.add_argument("--token", required=True); p.add_argument("--path"); p.add_argument("--failed", action="store_true")
    p = sub.add_parser("prepare"); p.add_argument("--as-of")
    p = sub.add_parser("analyze"); p.add_argument("--output", default="outputs/latest")
    p = sub.add_parser("report"); p.add_argument("--analysis", default="outputs/latest/analysis.json"); p.add_argument("--output", default="outputs/latest/report.html")
    p = sub.add_parser("run"); p.add_argument("--as-of"); p.add_argument("--output", default="outputs/latest")
    p = sub.add_parser("enrich"); p.add_argument("--as-of"); p.add_argument("--analysis", default="outputs/latest/analysis.json")
    p = sub.add_parser("news-queries"); p.add_argument("--as-of"); p.add_argument("--ids", nargs="*")
    p = sub.add_parser("news-import"); p.add_argument("--input", required=True); p.add_argument("--as-of")
    args = parser.parse_args(argv); root = Path(args.root)
    if args.command == "audit":
        from .catalog import build_catalog
        result = build_catalog(args.source or root/"ingredient_db.json", root/"data/processed")
    elif args.command == "plan":
        from .data import collection_plan
        try:
            result = collection_plan(root, args.as_of, args.limit, ids=args.ids)
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
    elif args.command == "prepare":
        from .data import assemble_panel
        result = assemble_panel(root, args.as_of)
    elif args.command == "analyze":
        from .model import run_analysis
        result = run_analysis(root/"data/processed/panel.csv", root/args.output, root/"data/processed/catalog.json")
        result = {"output": str(root/args.output/"analysis.json"), "ingredients": len(result.get("ingredients", []))}
    elif args.command == "report":
        from .report import build_report
        result = {"path": str(build_report(root/args.analysis, root/args.output))}
    elif args.command == "run":
        from .pipeline import run_pipeline
        result = run_pipeline(root, root/args.output, args.as_of)
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
