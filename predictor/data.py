from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np

from .common import ROOT, now_iso, read_json, write_json

TOOLS = {"amazon": "aba_research_trend", "google": "google_trend"}


def validate_requested_ids(catalog, ids):
    """An explicit selection must not silently become an empty or full batch."""
    if ids is None:
        return
    guidance = ("先在 data/processed/catalog.json 明确核对 keyword/family_id 并设 selected=true，"
                "或选择当前已映射的 ID。")
    if not ids:
        raise ValueError("--ids 后必须提供至少一个成分 ID；未执行任何任务。" + guidance)
    known = {item["id"]: item for item in catalog}
    unknown = sorted(set(ids) - known.keys())
    unselected = sorted({iid for iid in ids if iid in known and not known[iid].get("selected")})
    problems = []
    if unknown:
        problems.append("未知成分 ID：" + "、".join(unknown))
    if unselected:
        problems.append("尚未纳入已核对映射（selected=false）的成分 ID：" + "、".join(unselected))
    if problems:
        raise ValueError("；".join(problems) + "。未执行任何任务。" + guidance)


def validate_envelope(envelope, item, kind):
    expected = ({"keyword": item["keyword"], "marketplace": "US", "timeGranularity": "W"}
                if kind == "amazon" else {"request": {"keyword": item["keyword"], "marketplace": "US", "googleProp": "web", "monthly": False}})
    if envelope.get("request") != expected or envelope.get("tool") != TOOLS[kind]:
        raise ValueError(f"{kind} cache identity/granularity mismatch")
    payload = unwrap(envelope["result"])
    if kind == "google" and (not isinstance(payload, dict) or payload.get("keyword", "").casefold() != item["keyword"].casefold() or payload.get("marketplace") != "US"):
        raise ValueError("Google identity mismatch")
    return payload


def unwrap(result):
    if isinstance(result, dict) and "code" in result:
        payload = result
    elif isinstance(result, dict) and result.get("isError"):
        raise ValueError("MCP returned isError")
    else:
        blocks = result.get("content", []) if isinstance(result, dict) else []
        text = next((x["text"] for x in blocks if x.get("type") == "text"), None)
        if text is None: raise ValueError("MCP result has no JSON text content")
        payload = json.loads(text)
    if payload.get("code") != "OK": raise ValueError(f"Provider failure: {payload.get('code')}: {payload.get('message')}")
    return payload["data"]


def latest_cache(ingredient_id, kind, root=ROOT, as_of=None):
    candidates = sorted((Path(root) / "data/raw/US" / ingredient_id / kind).glob("*.json"))
    if as_of:
        candidates = [p for p in candidates if p.stem <= str(as_of)]
    return candidates[-1] if candidates else None


def collection_plan(root=ROOT, as_of=None, limit=40, cache_days=7, ids=None):
    root = Path(root)
    as_of = date.fromisoformat(str(as_of)) if as_of else date.today()
    catalog = read_json(root / "data/processed/catalog.json")
    validate_requested_ids(catalog, ids)
    selected = [x for x in catalog if x["selected"] and (not ids or x["id"] in ids)][:limit]
    jobs, hits = [], []
    for item in selected:
        for kind, tool in TOOLS.items():
            path = latest_cache(item["id"], kind, root, as_of)
            if path and (as_of - date.fromisoformat(path.stem)).days < cache_days:
                try:
                    validate_envelope(read_json(path), item, kind)
                    hits.append(str(path.relative_to(root)))
                    continue
                except (ValueError, KeyError, TypeError): pass
            request = ({"keyword": item["keyword"], "marketplace": "US", "timeGranularity": "W"}
                       if kind == "amazon" else {"request": {"keyword": item["keyword"], "marketplace": "US", "googleProp": "web", "monthly": False}})
            jobs.append({"ingredient_id": item["id"], "keyword": item["keyword"], "kind": kind,
                "tool": tool, "request": request, "cost_units": 1,
                "cache_path": f"data/raw/US/{item['id']}/{kind}/{as_of}.json"})
    plan = {"created_at": now_iso(), "as_of": str(as_of), "selected_ingredients": len(selected),
            "planned_calls": len(jobs), "cache_hits": hits, "jobs": jobs,
            "note": "每个完整历史接口按1次调用保守登记；当前账户真实扣费仍以供应商账单为准。执行每项前必须预约预算。"}
    write_json(root / "data/collection_plan.json", plan)
    return plan


def assemble_panel(root=ROOT, as_of=None):
    root = Path(root)
    as_of = date.fromisoformat(str(as_of)) if as_of else date.today()
    catalog = read_json(root / "data/processed/catalog.json")
    frames, quality = [], []
    for item in [x for x in catalog if x["selected"]]:
        a_path = latest_cache(item["id"], "amazon", root, as_of)
        g_path = latest_cache(item["id"], "google", root, as_of)
        info = {"id": item["id"], "keyword": item["keyword"], "amazon_cache": str(a_path) if a_path else None,
                "google_cache": str(g_path) if g_path else None, "warnings": []}
        if not a_path:
            info["warnings"].append("缺少亚马逊历史"); quality.append(info); continue
        try:
            envelope = read_json(a_path)
            a = validate_envelope(envelope, item, "amazon")
            if not isinstance(a, list) or not a: raise ValueError("No Amazon observations")
            frame = pd.DataFrame(a)
            frame["week_end"] = pd.to_datetime(frame["label"], format="%Y%m%d", errors="raise")
            if frame["week_end"].duplicated().any(): raise ValueError("Duplicate Amazon weeks")
            if (frame["week_end"].dt.weekday != 5).any(): raise ValueError("Amazon labels must be Saturday week ends")
            frame = frame.loc[frame["week_end"].dt.date < as_of].copy()
            if frame.empty: raise ValueError("No completed Amazon weeks as of the requested date")
            for col in ["searches", "rank"]: frame[col] = pd.to_numeric(frame[col], errors="coerce")
            invalid_search = ~np.isfinite(frame["searches"]) | (frame["searches"] < 0)
            invalid_rank = ~np.isfinite(frame["rank"]) | (frame["rank"] <= 0)
            info["invalid_search_values"] = int(invalid_search.sum())
            info["invalid_rank_values"] = int(invalid_rank.sum())
            if invalid_search.any() or invalid_rank.any(): info["warnings"].append("非法、非数或无穷数值已标记缺失，不填为零。")
            frame.loc[invalid_search, "searches"] = float("nan")
            frame.loc[invalid_rank, "rank"] = float("nan")
            frame.loc[frame["searches"] < 0, "searches"] = float("nan")
            frame.loc[frame["rank"] <= 0, "rank"] = float("nan")
            frame = frame.set_index("week_end")[["searches", "rank"]].sort_index()
            frame = frame.reindex(pd.date_range(frame.index.min(), frame.index.max(), freq="W-SAT"))
            frame.index.name = "week_end"
        except (ValueError, KeyError, TypeError) as exc:
            info["warnings"].append(str(exc)); quality.append(info); continue
        frame["google_trend"] = float("nan")
        if g_path:
            try:
                ge = read_json(g_path)
                gd = validate_envelope(ge, item, "google")
                g = pd.DataFrame(gd.get("items", []))
                if not g.empty:
                    starts = pd.to_datetime(g["time"], unit="ms", utc=True).dt.tz_localize(None).dt.normalize()
                    if (starts.dt.weekday != 6).any(): raise ValueError("Google weekly labels are not Sunday; manual period review required")
                    g["week_end"] = starts + pd.Timedelta(days=6)
                    g = g.loc[g["week_end"].dt.date < as_of].copy()
                    g["value"] = pd.to_numeric(g["value"], errors="coerce")
                    g.loc[(g["value"] < 0) | (g["value"] > 100), "value"] = float("nan")
                    if g["week_end"].duplicated().any(): raise ValueError("Duplicate Google weeks")
                    # Explicit conservative lag: mapping remains provisional until vendor confirmation.
                    g["week_end"] += pd.Timedelta(days=7)
                    frame["google_trend"] = g.set_index("week_end")["value"].reindex(frame.index)
                info["google_fetched_at"] = ge.get("fetched_at")
            except (ValueError, KeyError, TypeError) as exc: info["warnings"].append(str(exc))
        info.update(weeks=len(frame), missing_amazon=int(frame["searches"].isna().sum()),
            missing_google=int(frame["google_trend"].isna().sum()), amazon_fetched_at=envelope.get("fetched_at"),
            first_week=str(frame.index.min().date()), last_week=str(frame.index.max().date()))
        info["warnings"].extend(["Google周日起始映射待供应商核实，已额外滞后1周；并非准确发布时间复原。",
            "当前下载的历史可能经过修订，回测属于修订历史回放；没有当时快照，不能声称完整实时验证。",
            "卖家精灵搜索量为估计值；2023-05统计口径变化是否追溯修订尚未证实。"])
        frame["ingredient_id"] = item["id"]
        frame["family_id"] = item["family_id"]
        frame["keyword"] = item["keyword"]
        frame["name_cn"] = item["name_cn"]
        frame["marketplace"] = "US"
        frames.append(frame.reset_index())
        quality.append(info)
    if not frames: raise ValueError("No usable Amazon histories. Collect or import raw data first.")
    panel = pd.concat(frames, ignore_index=True)
    panel["week_end"] = panel["week_end"].dt.strftime("%Y-%m-%d")
    path = root / "data/processed/panel.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(path, index=False, encoding="utf-8")
    write_json(root / "data/processed/data_quality.json", {"as_of": str(as_of), "ingredients": quality, "google_lag_weeks": 1})
    return {"path": str(path), "rows": len(panel), "ingredients": panel["ingredient_id"].nunique(), "warnings": sum(len(x["warnings"]) for x in quality)}
