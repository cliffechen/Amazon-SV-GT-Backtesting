"""Audit the supplied candidate library without trusting its market claims."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .common import ROOT, now_iso, read_json, sha256, write_json

# Explicitly reviewed query mappings; selection is category coverage, not prior winners.
QUERIES = {
    "urolithin a": "urolithin a", "spermidine": "spermidine", "fisetin": "fisetin",
    "quercetin": "quercetin", "resveratrol": "resveratrol", "pterostilbene": "pterostilbene",
    "nmn": "nmn", "nr": "nicotinamide riboside", "caakg": "ca akg",
    "berberine": "berberine", "glucomannan": "glucomannan", "bitter melon extract": "bitter melon",
    "cinnamon extract": "cinnamon extract", "apple cider vinegar": "apple cider vinegar",
    "green tea extract": "green tea extract", "lions mane": "lions mane", "reishi": "reishi",
    "chaga": "chaga", "cordyceps": "cordyceps", "l-theanine": "l theanine",
    "bacopa monnieri": "bacopa monnieri", "phosphatidylserine": "phosphatidylserine",
    "ginkgo biloba": "ginkgo biloba", "alpha-gpc": "alpha gpc", "ashwagandha": "ashwagandha",
    "rhodiola rosea": "rhodiola rosea", "apigenin": "apigenin", "melatonin": "melatonin",
    "magnesium l-threonate": "magnesium l threonate", "saffron extract": "saffron",
    "elderberry": "elderberry", "vitamin d3": "vitamin d3", "nac": "nac",
    "lactoferrin": "lactoferrin", "akkermansia muciniphila": "akkermansia",
    "milk thistle": "milk thistle", "astaxanthin": "astaxanthin", "creatine": "creatine",
    "coq10": "coq10", "shilajit": "shilajit",
}


def canonical_name(name):
    base = re.sub(r"\s*\([^)]*\)", "", name).strip().lower().replace("’", "'")
    base = base.replace("lion's mane", "lions mane")
    return {"trans-resveratrol": "resveratrol", "creatine monohydrate": "creatine",
            "hyaluronic acid": "hyaluronic acid", "citrus bergamia polyphenols": "bergamot extract"}.get(base, base)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def family(name):
    if name.startswith("magnesium"): return "magnesium"
    if name.startswith("collagen"): return "collagen"
    if name.startswith("omega-3"): return "omega-3"
    if name.startswith("probiotics"): return "probiotics"
    return slug(name)


def build_catalog(source=None, output_dir=None):
    source = Path(source or ROOT / "ingredient_db.json")
    output_dir = Path(output_dir or ROOT / "data/processed")
    raw = read_json(source)
    if not isinstance(raw, dict) or not isinstance(raw.get("categories"), list):
        raise ValueError("Expected categories[].ingredients[] ingredient library")
    entries, names = {}, []
    for cat in raw["categories"]:
        for row in cat.get("ingredients", []):
            if not isinstance(row.get("name"), str) or not row["name"].strip():
                raise ValueError("An ingredient is missing its name")
            names.append(row["name"].lower())
            key = canonical_name(row["name"])
            if cat["id"] == "pet_health":
                key += " (pet)"  # Same substance can have a distinct search audience.
            entry = entries.setdefault(key, {"id": slug(key), "family_id": family(key),
                "name": row["name"], "name_cn": row.get("nameCN", ""), "keyword": QUERIES.get(key),
                "categories": [], "brands": [], "aliases": [], "eligible": True,
                "exclusion_reason": None, "selected": False, "claims_verified": False})
            entry["categories"].append(cat["id"])
            entry["aliases"].append(row["name"])
            entry["brands"].extend(row.get("keyBrands", []))
    for key, item in entries.items():
        for field in ["categories", "aliases", "brands"]:
            item[field] = sorted(set(item[field]))
        excluded = set(item["categories"]) <= {"pet_health", "delivery_innovation"}
        mixture = "+" in key or "/" in key or "complex" in key
        if excluded:
            item.update(eligible=False, exclusion_reason="宠物用途或递送/剂型技术，非首期成人单成分范围")
        elif mixture:
            item.update(eligible=False, exclusion_reason="复合/宽泛概念，需人工拆分核对搜索意图")
        item["selected"] = bool(item["eligible"] and key in QUERIES)
        item["query_status"] = "reviewed_mapping" if item["selected"] else "requires_review"
        if key == "urolithin a": item["brands"] = ["Timeline", "Mitopure", "Amazentis"]
    catalog = sorted(entries.values(), key=lambda x: (not x["selected"], x["id"] != "urolithin-a", x["id"]))
    audit = {"source_file": str(source.resolve()), "source_sha256": sha256(source),
        "categories": len(raw["categories"]), "records": len(names), "unique_names": len(set(names)),
        "canonical_count": len(catalog), "selected_count": sum(x["selected"] for x in catalog),
        "duplicate_names": {n: c for n, c in Counter(names).items() if c > 1},
        "exclusions": [{"id": x["id"], "reason": x["exclusion_reason"]} for x in catalog if not x["eligible"]],
        "generated_at": now_iso(), "source_last_updated": raw.get("meta", {}).get("lastUpdated"),
        "warnings": ["这是候选成分知识库，不是可直接训练的时间序列。", "库中市场、增长、功效和品牌描述未经逐条核实，不用作预测特征。",
            "原库偏向promising/emerging成分，存在候选库选择偏差；结果不能外推所有补充剂。", "ingredientIndex只是辅助索引，存在与正文不一致的分类；以categories正文为准。",
            "首期预测站点US；北美新闻单独涵盖US/CA/MX，不能把US需求外推加拿大或墨西哥。"]}
    write_json(output_dir / "catalog.json", catalog)
    write_json(output_dir / "audit.json", audit)
    return audit
