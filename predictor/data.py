from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .common import ROOT, atomic_write_text, now_iso, read_json, sha256, write_json
from .collection import build_sif_batch_jobs, provider_cache_path

SELLERSPRITE = "sellersprite"
SIF = "sif"
AMAZON_PROVIDERS = {SELLERSPRITE, SIF}
TOOLS = {"amazon": "aba_research_trend", "google": "google_trend"}
SIF_AMAZON_TOOL = "market_get_keyword_history"
SIF_AMAZON_TOOL_ALIASES = frozenset({SIF_AMAZON_TOOL, "sif_market_get_keyword_history"})


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


def _query(item, provider):
    queries = item.get("queries") or {}
    return str(queries.get(provider) or item["keyword"])


def _envelope_provider(envelope):
    source = str(envelope.get("source") or "").casefold()
    tool = envelope.get("tool")
    if source:
        return source
    if tool in SIF_AMAZON_TOOL_ALIASES:
        return SIF
    if tool in TOOLS.values():
        return SELLERSPRITE
    return None


def validate_envelope(envelope, item, kind, provider=SELLERSPRITE):
    if provider not in AMAZON_PROVIDERS:
        raise ValueError(f"unsupported Amazon provider: {provider}")
    if _envelope_provider(envelope) != provider:
        raise ValueError(f"{kind} cache provider mismatch")
    if provider == SIF:
        if kind != "amazon":
            raise ValueError("SIF is only supported for Amazon history")
        return _validate_sif_amazon(envelope, item)
    expected = ({"keyword": _query(item, SELLERSPRITE), "marketplace": "US", "timeGranularity": "W"}
                if kind == "amazon" else {"request": {"keyword": _query(item, SELLERSPRITE),
                                                        "marketplace": "US", "googleProp": "web", "monthly": False}})
    if envelope.get("request") != expected or envelope.get("tool") != TOOLS[kind]:
        raise ValueError(f"{kind} cache identity/granularity mismatch")
    payload = unwrap(envelope["result"])
    if kind == "google" and (not isinstance(payload, dict)
            or payload.get("keyword", "").casefold() != _query(item, SELLERSPRITE).casefold()
            or payload.get("marketplace") != "US"):
        raise ValueError("Google identity mismatch")
    return payload


def _validate_sif_amazon(envelope, item):
    """Validate SIF history and convert Sunday starts to Saturday week ends."""
    request = envelope.get("request") or {}
    query = _query(item, SIF)
    keywords = request.get("keywords") or []
    if (_envelope_provider(envelope) != SIF or envelope.get("tool") not in SIF_AMAZON_TOOL_ALIASES
            or request.get("country") != "US"
            or request.get("granularity") != "week"
            or query.casefold() not in {str(k).casefold() for k in keywords}):
        raise ValueError("amazon cache identity/granularity mismatch")
    payload = envelope.get("result")
    if not isinstance(payload, dict) or payload.get("country") != "US" or payload.get("granularity") != "week":
        raise ValueError("amazon cache identity/granularity mismatch")
    matches = [entry for entry in payload.get("keywords", [])
               if str(entry.get("keyword", "")).casefold() == query.casefold()]
    if len(matches) != 1:
        raise ValueError("SIF history must contain exactly one mapped keyword entry")
    entry = matches[0]
    dates = entry.get("dates") or []
    volumes = entry.get("volumes") or []
    ranks = entry.get("ranks") or []
    if not dates or len(dates) != len(volumes) or len(dates) != len(ranks):
        raise ValueError("SIF history arrays must be non-empty and have equal lengths")
    rows, seen, prior = [], {}, None
    for day, searches, rank in zip(dates, volumes, ranks):
        start = pd.Timestamp(str(day))
        if pd.isna(start) or start.weekday() != 6:
            raise ValueError("SIF weekly labels must be Sunday week starts")
        values = (searches, rank)
        if start in seen:
            if seen[start] != values:
                raise ValueError("SIF duplicate week contains conflicting searches or rank")
            continue
        if prior is not None and start <= prior:
            raise ValueError("SIF unique weekly labels must be strictly increasing")
        seen[start] = values
        prior = start
        week_end = start + pd.Timedelta(days=6)
        label = week_end.strftime("%Y%m%d")
        if week_end.weekday() != 5:
            raise ValueError("SIF week mapping produced a duplicate or non-Saturday week")
        rows.append({"label": label, "searches": searches, "rank": rank})
    return rows


def unwrap(result):
    if isinstance(result, dict) and "code" in result:
        payload = result
    elif isinstance(result, dict) and result.get("isError"):
        raise ValueError("MCP returned isError")
    else:
        blocks = result.get("content", []) if isinstance(result, dict) else []
        text = next((x["text"] for x in blocks if x.get("type") == "text"), None)
        if text is None:
            raise ValueError("MCP result has no JSON text content")
        payload = json.loads(text)
    if payload.get("code") != "OK":
        raise ValueError(f"Provider failure: {payload.get('code')}: {payload.get('message')}")
    return payload["data"]


def _cache_snapshot_date(path, envelope):
    """Return the cache snapshot date, including the one-time legacy SIF import."""
    try:
        return date.fromisoformat(path.stem[:10])
    except ValueError:
        pass
    fetched_at = str(envelope.get("fetched_at") or "")[:10]
    try:
        return date.fromisoformat(fetched_at)
    except ValueError:
        pass
    if _envelope_provider(envelope) == SIF:
        entries = (envelope.get("result") or {}).get("keywords") or []
        dates = [str(day) for entry in entries if isinstance(entry, dict)
                 for day in (entry.get("dates") or [])]
        if dates:
            try:
                # The history label is a Sunday start. The earliest honest
                # snapshot boundary for its completed week is the next Sunday.
                return date.fromisoformat(max(dates)) + timedelta(days=7)
            except ValueError:
                return None
    return None


def _cache_candidates(ingredient_id, kind, root=ROOT, as_of=None, provider=SELLERSPRITE):
    root = Path(root)
    sources = [
        (root / "data/raw" / provider / "US" / ingredient_id / kind, "*.json"),
        (root / "data/raw/US" / ingredient_id / kind / provider, "*.json"),
        (root / "data/raw/US" / ingredient_id / kind, "*.json"),
    ]
    if provider == SIF and kind == "amazon":
        # Migration-only location produced before provider-isolated caches
        # existed. Its envelope is still provider-validated below.
        sources.append((root / "data/raw/US/_sif_envelopes_all", f"{ingredient_id}.json"))
    candidates = []
    for priority, (directory, pattern) in enumerate(sources):
        for path in directory.glob(pattern) if directory.exists() else []:
            try:
                envelope = read_json(path)
                if _envelope_provider(envelope) != provider:
                    continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            snapshot = _cache_snapshot_date(path, envelope)
            if snapshot is None or (as_of and snapshot > date.fromisoformat(str(as_of))):
                continue
            candidates.append((snapshot.isoformat(), -priority, path))
    return [row[2] for row in sorted(candidates, reverse=True)]


def latest_cache(ingredient_id, kind, root=ROOT, as_of=None, provider=SELLERSPRITE):
    candidates = _cache_candidates(ingredient_id, kind, root, as_of, provider)
    return candidates[0] if candidates else None


def _latest_valid_cache(item, kind, root, as_of, provider):
    for path in _cache_candidates(item["id"], kind, root, as_of, provider):
        try:
            envelope = read_json(path)
            validate_envelope(envelope, item, kind, provider)
            return path, envelope
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None, None


def collection_plan(root=ROOT, as_of=None, limit=52, cache_days=7, ids=None,
                    amazon_provider=SELLERSPRITE, refresh_amazon=False):
    root = Path(root)
    if amazon_provider not in AMAZON_PROVIDERS:
        raise ValueError(f"unsupported Amazon provider: {amazon_provider}")
    as_of = date.fromisoformat(str(as_of)) if as_of else date.today()
    catalog = read_json(root / "data/processed/catalog.json")
    validate_requested_ids(catalog, ids)
    selected = [x for x in catalog if x["selected"] and (not ids or x["id"] in ids)][:limit]
    jobs, hits, pending_sif = [], [], []
    for item in selected:
        for kind in ("amazon", "google"):
            provider = amazon_provider if kind == "amazon" else SELLERSPRITE
            path, _ = _latest_valid_cache(item, kind, root, as_of, provider)
            force_refresh = bool(refresh_amazon and kind == "amazon")
            snapshot = _cache_snapshot_date(path, read_json(path)) if path else None
            if (not force_refresh and path and snapshot
                    and (as_of - snapshot).days < cache_days):
                hits.append(str(path.relative_to(root)))
                continue
            if provider == SIF:
                pending_sif.append({"id": item["id"], "keyword": _query(item, SIF)})
                continue
            tool = TOOLS[kind]
            request = ({"keyword": _query(item, SELLERSPRITE), "marketplace": "US", "timeGranularity": "W"}
                       if kind == "amazon" else {"request": {"keyword": _query(item, SELLERSPRITE),
                                                              "marketplace": "US", "googleProp": "web", "monthly": False}})
            destination = provider_cache_path(root, provider, "US", item["id"], kind, as_of, request)
            jobs.append({"ingredient_id": item["id"], "keyword": _query(item, provider), "kind": kind,
                         "provider": provider, "tool": tool, "request": request, "cost_units": 1,
                         "cache_path": str(destination.relative_to(root)).replace("\\", "/")})
    for batch in build_sif_batch_jobs(root, as_of, pending_sif, batch_size=5):
        destination = Path(batch.pop("destination"))
        targets = []
        for item in batch["items"]:
            request = {"keywords": [item["keyword"]], "country": "US", "granularity": "week"}
            target = provider_cache_path(root, SIF, "US", item["id"], "amazon", as_of, request)
            targets.append({"ingredient_id": item["id"], "keyword": item["keyword"],
                            "cache_path": str(target.relative_to(root)).replace("\\", "/")})
        cache_path = str(destination.relative_to(root)).replace("\\", "/")
        jobs.append({**batch, "ingredient_ids": [item["id"] for item in batch["items"]],
                     "kind": "amazon", "provider": SIF, "destination": cache_path,
                     "cache_path": cache_path, "cache_targets": targets})
    plan = {"schema_version": "2.0", "created_at": now_iso(), "as_of": str(as_of),
            "amazon_provider": amazon_provider, "refresh_amazon": bool(refresh_amazon),
            "selected_ingredients": len(selected),
            "planned_calls": len(jobs), "cache_hits": hits, "jobs": jobs,
            "note": "缓存按供应商隔离；SIF亚马逊历史每批最多5词并按批计费，成功后须原子拆为逐词缓存；SellerSprite任务执行前必须预约其预算。"}
    write_json(root / "data/collection_plan.json", plan)
    return plan


def _manifest_input(root, ingredient_id, dataset, provider, path, envelope):
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = str(path)
    cache_hash = sha256(path)
    provenance = envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    return {"ingredient_id": ingredient_id, "dataset": dataset, "provider": provider,
            "source": envelope.get("source") or provider,
            "path": relative, "sha256": cache_hash,
            "source_path": provenance.get("raw_batch_path") or relative,
            "source_sha256": provenance.get("raw_batch_sha256") or cache_hash,
            "fetched_at": envelope.get("fetched_at"),
            "tool": envelope.get("tool"), "request": envelope.get("request")}


def assemble_panel(root=ROOT, as_of=None, amazon_provider=SELLERSPRITE):
    root = Path(root)
    if amazon_provider not in AMAZON_PROVIDERS:
        raise ValueError(f"unsupported Amazon provider: {amazon_provider}")
    as_of = date.fromisoformat(str(as_of)) if as_of else date.today()
    catalog_path = root / "data/processed/catalog.json"
    catalog = read_json(catalog_path)
    frames, quality, inputs = [], [], []
    for item in [x for x in catalog if x["selected"]]:
        a_path, envelope = _latest_valid_cache(item, "amazon", root, as_of, amazon_provider)
        g_path, ge = _latest_valid_cache(item, "google", root, as_of, SELLERSPRITE)
        info = {"id": item["id"], "keyword": _query(item, amazon_provider),
                "amazon_provider": amazon_provider, "amazon_cache": str(a_path) if a_path else None,
                "google_provider": SELLERSPRITE if g_path else None,
                "google_cache": str(g_path) if g_path else None, "warnings": []}
        if not a_path:
            info["warnings"].append(f"缺少{amazon_provider}亚马逊历史")
            quality.append(info)
            continue
        try:
            a = validate_envelope(envelope, item, "amazon", amazon_provider)
            if not isinstance(a, list) or not a:
                raise ValueError("No Amazon observations")
            frame = pd.DataFrame(a)
            frame["week_end"] = pd.to_datetime(frame["label"], format="%Y%m%d", errors="raise")
            if frame["week_end"].duplicated().any():
                raise ValueError("Duplicate Amazon weeks")
            if (frame["week_end"].dt.weekday != 5).any():
                raise ValueError("Amazon labels must be Saturday week ends")
            frame = frame.loc[frame["week_end"].dt.date < as_of].copy()
            if frame.empty:
                raise ValueError("No completed Amazon weeks as of the requested date")
            for col in ("searches", "rank"):
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            invalid_search = ~np.isfinite(frame["searches"]) | (frame["searches"] < 0)
            invalid_rank = ~np.isfinite(frame["rank"]) | (frame["rank"] <= 0)
            info["invalid_search_values"] = int(invalid_search.sum())
            info["invalid_rank_values"] = int(invalid_rank.sum())
            if invalid_search.any() or invalid_rank.any():
                info["warnings"].append("非法、非数或无穷数值已标记缺失，不填为零。")
            frame.loc[invalid_search, "searches"] = float("nan")
            frame.loc[invalid_rank, "rank"] = float("nan")
            frame = frame.set_index("week_end")[["searches", "rank"]].sort_index()
            frame = frame.reindex(pd.date_range(frame.index.min(), frame.index.max(), freq="W-SAT"))
            frame.index.name = "week_end"
        except (ValueError, KeyError, TypeError) as exc:
            info["warnings"].append(str(exc))
            quality.append(info)
            continue
        inputs.append(_manifest_input(root, item["id"], "amazon", amazon_provider, a_path, envelope))
        if amazon_provider == SIF:
            info["warnings"].append("SIF来源：搜索量与卖家精灵估计值口径不同，不跨供应商合并绝对量。")
        frame["google_trend"] = float("nan")
        if g_path:
            try:
                gd = validate_envelope(ge, item, "google", SELLERSPRITE)
                g = pd.DataFrame(gd.get("items", []))
                if not g.empty:
                    starts = pd.to_datetime(g["time"], unit="ms", utc=True).dt.tz_localize(None).dt.normalize()
                    if (starts.dt.weekday != 6).any():
                        raise ValueError("Google weekly labels are not Sunday; manual period review required")
                    g["week_end"] = starts + pd.Timedelta(days=6)
                    g = g.loc[g["week_end"].dt.date < as_of].copy()
                    g["value"] = pd.to_numeric(g["value"], errors="coerce")
                    g.loc[(g["value"] < 0) | (g["value"] > 100), "value"] = float("nan")
                    if g["week_end"].duplicated().any():
                        raise ValueError("Duplicate Google weeks")
                    g["week_end"] += pd.Timedelta(days=7)
                    frame["google_trend"] = g.set_index("week_end")["value"].reindex(frame.index)
                info["google_fetched_at"] = ge.get("fetched_at")
                inputs.append(_manifest_input(root, item["id"], "google", SELLERSPRITE, g_path, ge))
            except (ValueError, KeyError, TypeError) as exc:
                info["warnings"].append(str(exc))
        info.update(weeks=len(frame), missing_amazon=int(frame["searches"].isna().sum()),
                    missing_google=int(frame["google_trend"].isna().sum()),
                    amazon_fetched_at=envelope.get("fetched_at"),
                    first_week=str(frame.index.min().date()), last_week=str(frame.index.max().date()))
        info["warnings"].extend([
            "Google周日起始映射待供应商核实，已额外滞后1周；并非准确发布时间复原。",
            "当前下载的历史可能经过修订，回测属于修订历史回放；没有当时快照，不能声称完整实时验证。",
            f"{amazon_provider}搜索量为估计值；历史口径是否追溯修订尚未完全证实。",
        ])
        frame["ingredient_id"] = item["id"]
        frame["family_id"] = item["family_id"]
        frame["keyword"] = _query(item, amazon_provider)
        frame["name_cn"] = item["name_cn"]
        frame["marketplace"] = "US"
        frame["amazon_provider"] = amazon_provider
        frame["amazon_source"] = amazon_provider
        frame["google_provider"] = SELLERSPRITE if g_path else ""
        frames.append(frame.reset_index())
        quality.append(info)
    if not frames:
        raise ValueError(f"No usable {amazon_provider} Amazon histories. Collect or import raw data first.")
    panel = pd.concat(frames, ignore_index=True)
    panel["week_end"] = panel["week_end"].dt.strftime("%Y-%m-%d")

    dataset_dir = root / "data/processed/datasets" / amazon_provider / str(as_of)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    panel_path = dataset_dir / "panel.csv"
    panel_csv = panel.to_csv(index=False, lineterminator="\n")
    atomic_write_text(panel_path, panel_csv)
    quality_payload = {"schema_version": "2.0", "as_of": str(as_of),
                       "amazon_provider": amazon_provider, "ingredients": quality, "google_lag_weeks": 1}
    write_json(dataset_dir / "data_quality.json", quality_payload)
    manifest = {"schema_version": "2.0", "generated_at": now_iso(), "as_of": str(as_of),
                "marketplace": "US", "amazon_provider": amazon_provider,
                "google_provider": SELLERSPRITE, "provider_policy": "single_amazon_provider",
                "week_convention": {"amazon": "W-SAT", "sif_mapping": "Sunday start + 6 days",
                                    "google_lag_weeks": 1},
                "panel_path": str(panel_path.relative_to(root)), "panel_sha256": sha256(panel_path),
                "catalog_path": str(catalog_path.relative_to(root)), "catalog_sha256": sha256(catalog_path),
                "rows": len(panel), "ingredients": int(panel["ingredient_id"].nunique()),
                "first_week": panel["week_end"].min(), "last_week": panel["week_end"].max(),
                "inputs": inputs}
    manifest_path = dataset_dir / "manifest.json"
    write_json(manifest_path, manifest)

    if amazon_provider == SELLERSPRITE:
        legacy_panel = root / "data/processed/panel.csv"
        legacy_panel.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(legacy_panel, panel_csv)
        legacy_manifest = {**manifest, "panel_path": str(legacy_panel.relative_to(root)),
                           "panel_sha256": sha256(legacy_panel),
                           "dataset_manifest_path": str(manifest_path.relative_to(root))}
        write_json(root / "data/processed/panel.manifest.json", legacy_manifest)
        write_json(root / "data/processed/data_quality.json", quality_payload)
    return {"path": str(panel_path), "panel_path": str(panel_path), "manifest_path": str(manifest_path),
            "dataset_id": f"{amazon_provider}/{as_of}", "as_of": str(as_of),
            "amazon_provider": amazon_provider, "rows": len(panel),
            "ingredients": int(panel["ingredient_id"].nunique()),
            "warnings": sum(len(x["warnings"]) for x in quality)}
