"""Build a self-contained viewer.html (template = ../rev_gemini31_preview_all/reports/viewer.html)
   embedding bak_test's records.json + rubric_catalog.json data.
"""
import json
import os
import re
from collections import defaultdict
from pathlib import Path

# Point at the bench_step3 run dir (containing reports/records.json + reports/rubric_catalog.json).
ROOT = Path(os.environ.get("BENCH_STEP3_ROOT", "./bench_step3_smoke10")).resolve()
TEMPLATE = Path(os.environ.get("VIEWER_TEMPLATE", ROOT / "reports" / "viewer_template.html"))
RECORDS = ROOT / "reports" / "records.json"
CATALOG = ROOT / "reports" / "rubric_catalog.json"
OUT = ROOT / "reports" / "viewer.html"

ITEM_MAX_SCORE = 2.0


def safe_parse(value, fallback):
    if value in (None, "", "[]", "{}"):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def normalize_model(row):
    return row.get("model_label") or row.get("model_name") or "unknown"


def relativize(path):
    """Map an absolute fs path under .../rev_gemini31_preview_all_bak_test/ to ../<rest>."""
    if not path:
        return ""
    marker = "rev_gemini31_preview_all_bak_test/"
    if marker in path:
        rel = path.split(marker, 1)[1]
        return f"../{rel}"
    return path


def add_status(row):
    if row.get("sandbox_ok") is not True:
        return "sandbox fail"
    if row.get("geometry_valid") is not True:
        return "geometry fail"
    if row.get("watertight") is not True:
        return "watertight fail"
    if row.get("component_count_match") is not True:
        return "component mismatch"
    return "ok"


def aggregate_categories(rows):
    """Build primary_category_scores and secondary_category_scores from llm_judge_breakdown items."""
    prim = defaultdict(lambda: {
        "key": "", "primary_category": "", "secondary_category": "",
        "item_count": 0, "earned_points": 0.0, "max_points": 0.0,
        "judged_row_set": set(),
    })
    sec = defaultdict(lambda: {
        "key": "", "primary_category": "", "secondary_category": "",
        "item_count": 0, "earned_points": 0.0, "max_points": 0.0,
        "judged_row_set": set(),
    })

    for idx, row in enumerate(rows):
        bd = safe_parse(row.get("llm_judge_breakdown_json"), {})
        items = bd.get("items") if isinstance(bd, dict) else None
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            p = (it.get("primary_category_en") or "").strip()
            s = (it.get("secondary_category_en") or "").strip()
            try:
                score = float(it.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            if p:
                rec = prim[p]
                rec["key"] = p
                rec["primary_category"] = p
                rec["secondary_category"] = ""
                rec["item_count"] += 1
                rec["earned_points"] += score
                rec["max_points"] += ITEM_MAX_SCORE
                rec["judged_row_set"].add(idx)
            if p and s:
                k = f"{p} / {s}"
                rec = sec[k]
                rec["key"] = k
                rec["primary_category"] = p
                rec["secondary_category"] = s
                rec["item_count"] += 1
                rec["earned_points"] += score
                rec["max_points"] += ITEM_MAX_SCORE
                rec["judged_row_set"].add(idx)

    def finalize(d):
        out = []
        for v in d.values():
            judged = len(v["judged_row_set"])
            ratio = v["earned_points"] / v["max_points"] if v["max_points"] > 0 else 0.0
            out.append({
                "key": v["key"],
                "primary_category": v["primary_category"],
                "secondary_category": v["secondary_category"],
                "item_count": v["item_count"],
                "earned_points": v["earned_points"],
                "max_points": v["max_points"],
                "weighted_score": ratio,
                "ratio": ratio,
                "judged_rows": judged,
            })
        out.sort(key=lambda r: r["key"])
        return out

    return finalize(prim), finalize(sec)


def build_summary(rows, scope):
    n = len(rows)
    denom = n if n else 1
    sandbox = sum(1 for r in rows if r.get("sandbox_ok") is True)
    geom = sum(1 for r in rows if r.get("geometry_valid") is True)
    water = sum(1 for r in rows if r.get("watertight") is True)
    comp = sum(1 for r in rows if r.get("component_count_match") is True)
    judged = [r for r in rows if not r.get("llm_judge_error") and (r.get("llm_judge_score") or 0) > 0]
    avg_judge = sum(float(r.get("llm_judge_score") or 0) for r in judged) / len(judged) if judged else 0.0
    prim, sec = aggregate_categories(rows)
    return {
        "scope": scope,
        "samples": n,
        "sandbox_samples": sandbox,
        "llm_judge_samples": n,
        "geometry_valid_rate": geom / denom,
        "watertight_rate": water / denom,
        "component_match_rate": comp / denom,
        "sandbox_success_rate": sandbox / denom,
        "avg_llm_judge_score": avg_judge,
        "primary_category_scores": prim,
        "secondary_category_scores": sec,
    }


def enrich_row(row):
    out = dict(row)
    out["task_name"] = row.get("task_name") or "unknown"
    out["model_name"] = normalize_model(row)
    out["status"] = add_status(row)
    out["svg_url"] = relativize(row.get("svg_path") or "")
    out["png_url"] = relativize(row.get("png_path") or "")
    out["render_png_url"] = relativize(row.get("render_png_path") or "")
    out["render_mesh_url"] = relativize(row.get("render_mesh_path") or "")
    out["render_step_url"] = relativize(row.get("render_step_path") or "")
    out["code_url"] = relativize(row.get("code_path") or "")
    out["rubric_breakdown"] = safe_parse(row.get("rubric_breakdown_json"), [])
    out["rubric_primary_breakdown"] = safe_parse(row.get("rubric_primary_breakdown_json"), [])
    out["rubric_category_breakdown"] = safe_parse(row.get("rubric_category_breakdown_json"), [])
    out["llm_judge_breakdown"] = safe_parse(row.get("llm_judge_breakdown_json"), {})
    return out


def build_data():
    rows_raw = json.loads(RECORDS.read_text())
    rows = [enrich_row(r) for r in rows_raw]

    overall = {**build_summary(rows, "Overall")}

    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model_name"]].append(r)
    per_model = [build_summary(arr, model) for model, arr in sorted(by_model.items())]

    rubric_catalog = json.loads(CATALOG.read_text())

    # Same metric_explanations list used by the original viewer.html.
    metric_explanations = [
        {"name": "Geometry Valid", "description": "单个样本是否通过几何有效性检查。它来自 OCCT/CadQuery 几何检查结果，要求样本代码能执行并且几何体本身有效。"},
        {"name": "Geometry Valid Rate", "description": "样本集合中 `geometry_valid = true` 的比例。注意：当前先要求几何被判为有效才会触发后续的 watertight 等指标。"},
        {"name": "Watertight", "description": "几何体是否闭合实体，无开放边界。watertight 一般要求 geometry_valid 为 true。"},
        {"name": "Watertight Rate", "description": "集合中 `watertight = true` 的比例。"},
        {"name": "Component Match", "description": "生成结果的组件数是否和 GT 组件数一致。"},
        {"name": "Component Match Rate", "description": "集合中 `component_count_match = true` 的比例。"},
        {"name": "Result Solid Count", "description": "生成结果在沙箱执行后得到的 solid 数量，0 表示沙箱失败或没有有效几何。"},
        {"name": "GT Component Count", "description": "Ground Truth 中标注的组件数量，来自任务规范。"},
        {"name": "SVG Component Estimate", "description": "基于 SVG path 聚类做的组件数估计，用来辅助分析二维图和三维组件数是否一致。"},
        {"name": "BBox DX/DY/DZ", "description": "包围盒三个方向的边长，单位毫米。不是坐标值，而是尺寸跨度。"},
        {"name": "Rubric Score", "description": "当前 task 的 rubric 加权得分，范围约 0 到 1。它不是全库 rubric 总表，只对应当前样本所属任务。"},
        {"name": "Category Rubric Score", "description": "把 rubric 分数按一级类目和二级类目重新汇总后的得分。可以看到可实用、可组装、可建造，以及各个子类分别丢了哪些分。"},
        {"name": "Sandbox OK", "description": "CadQuery 代码是否能在沙箱里成功执行到拿到 `result`。失败时会显示运行时错误。"},
        {"name": "Normal Consistency / Volume Valid / BBox Valid / OCCT Valid", "description": "这些是几何子指标，用来帮助解释 Geometry Valid 为什么失败或通过。"},
    ]

    return {
        "overall": overall,
        "per_model": per_model,
        "rows": rows,
        "rubric_catalog": rubric_catalog,
        "metric_explanations": metric_explanations,
    }


def main():
    template = TEMPLATE.read_text()
    data = build_data()
    payload = json.dumps(data, ensure_ascii=False)

    pattern = re.compile(r"const DATA = \{.*?\};\s*\n", re.DOTALL)
    if not pattern.search(template):
        raise SystemExit("Could not find inlined DATA block in template")
    replacement = f"const DATA = {payload};\n"
    new_html = pattern.sub(lambda _m: replacement, template, count=1)

    OUT.write_text(new_html)
    print(f"Wrote {OUT} ({len(new_html):,} bytes)")
    print(f"  rows: {len(data['rows'])}")
    print(f"  per_model: {len(data['per_model'])}")
    print(f"  rubric_catalog: {len(data['rubric_catalog'])}")
    print(f"  primary_category_scores: {len(data['overall']['primary_category_scores'])}")
    print(f"  secondary_category_scores: {len(data['overall']['secondary_category_scores'])}")


if __name__ == "__main__":
    main()
