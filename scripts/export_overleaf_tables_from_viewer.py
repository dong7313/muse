#!/usr/bin/env python3
"""Generate Overleaf LaTeX tables from benchmark `records.json`.

The output matches the user's required layout:
1. Overall Metrics table with grouped Survival + Alignment columns.
2. Rubric Detailed Scores table grouped by primary categories.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

MAX_SCORE_PER_ITEM = 2.0


def _load_json_field(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _safe_percent(numerator: int, denom: int) -> str:
    if denom <= 0:
        return "N/A"
    return f"{100.0 * numerator / denom:.2f}\\%"


def _format_score(raw):
    if raw is None or raw == "":
        return "N/A"
    try:
        v = float(raw)
    except Exception:
        return "N/A"
    return f"{v:.3f}"


def _latex_escape(value: str) -> str:
    if not isinstance(value, str):
        return str(value)
    return (
        value.replace("\\", r"\\textbackslash{}")
        .replace("&", r"\\&")
        .replace("%", r"\\%")
        .replace("$", r"\\$")
        .replace("#", r"\\#")
        .replace("_", r"\\_")
        .replace("{", r"\\{")
        .replace("}", r"\\}")
        .replace("~", r"\\textasciitilde{}")
        .replace("^", r"\\textasciicircum{}")
    )


def _short_stack(value: str) -> str:
    words = value.split()
    if len(words) <= 2:
        return _latex_escape(value)
    return r"\shortstack{" + r"\\ ".join(_latex_escape(" ".join(words[i : i + 2])) for i in range(0, len(words), 2)) + "}"


def _normalize_model_label(model_name: str) -> str:
    for prefix in ("closed-", "oss-"):
        if model_name.startswith(prefix):
            return model_name[len(prefix) :]
    return model_name


def _collect_rubric_keys(rows, level: str):
    keys_primary: set[str] = set()
    keys_secondary: set[tuple[str, str]] = set()
    for row in rows:
        breakdown = _load_json_field(row.get("llm_judge_breakdown_json"))
        items = []
        if isinstance(breakdown, dict) and isinstance(breakdown.get("items"), list):
            items = breakdown["items"]
        else:
            fallback = _load_json_field(row.get("rubric_breakdown_json"))
            if isinstance(fallback, list):
                items = fallback
        if not items:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            primary = item.get("primary_category_en") or item.get("primary_category")
            secondary = item.get("secondary_category_en") or item.get("secondary_category")
            if not primary:
                continue
            keys_primary.add(str(primary))
            if level == "secondary" and secondary:
                keys_secondary.add((str(primary), str(secondary)))
    return keys_primary if level == "primary" else keys_secondary


def _collect_rubric_scores(rows, primary_keys: set[str], secondary_keys: set[tuple[str, str]]):
    primary_earned: dict[str, float] = defaultdict(float)
    primary_max: dict[str, float] = defaultdict(float)
    secondary_earned: dict[tuple[str, str], float] = defaultdict(float)
    secondary_max: dict[tuple[str, str], float] = defaultdict(float)

    for row in rows:
        present_primary = set()
        present_secondary = set()

        breakdown = _load_json_field(row.get("llm_judge_breakdown_json"))
        items = []
        if isinstance(breakdown, dict) and isinstance(breakdown.get("items"), list):
            items = breakdown["items"]
        else:
            fallback = _load_json_field(row.get("rubric_breakdown_json"))
            if isinstance(fallback, list):
                items = fallback

        for item in items:
            if not isinstance(item, dict):
                continue
            primary = item.get("primary_category_en") or item.get("primary_category")
            secondary = item.get("secondary_category_en") or item.get("secondary_category")
            raw_score = item.get("score")
            if primary is None or raw_score is None:
                continue
            try:
                score = float(raw_score)
            except Exception:
                continue

            primary = str(primary)
            primary_earned[primary] += score
            present_primary.add(primary)
            if secondary:
                secondary_key = (primary, str(secondary))
                secondary_earned[secondary_key] += score
                present_secondary.add(secondary_key)

        for key in primary_keys:
            primary_max[key] += MAX_SCORE_PER_ITEM
            if key not in present_primary:
                primary_earned[key] += 0.0

        for key in secondary_keys:
            secondary_max[key] += MAX_SCORE_PER_ITEM
            if key not in present_secondary:
                secondary_earned[key] += 0.0

    primary_scores = {}
    for key in sorted(primary_max.keys()):
        max_v = primary_max[key]
        primary_scores[key] = (primary_earned[key] / max_v) if max_v > 0 else 0.0

    secondary_scores = {}
    for key in sorted(secondary_max.keys(), key=lambda x: (x[0], x[1])):
        max_v = secondary_max[key]
        secondary_scores[key] = (secondary_earned[key] / max_v) if max_v > 0 else 0.0

    return primary_scores, secondary_scores


def _build_overall_table(rows_by_model):
    model_rows: dict[str, dict[str, object]] = {}
    for model_name, rows in rows_by_model.items():
        total = len(rows)
        sandbox_ok = [r for r in rows if bool(r.get("sandbox_ok"))]
        sandbox_ok_count = len(sandbox_ok)

        geom_rate = sum(1 for r in rows if bool(r.get("geometry_valid") if "geometry_valid" in r else r.get("geometry_ok")))
        wat_rate = sum(1 for r in rows if bool(r.get("watertight")))
        comp_rate = sum(1 for r in rows if bool(r.get("component_count_match")))

        primary_keys = set(_collect_rubric_keys(rows, level="primary"))
        secondary_keys = set(_collect_rubric_keys(rows, level="secondary"))
        primary_scores, secondary_scores = _collect_rubric_scores(rows, primary_keys, secondary_keys)


        model_rows[model_name] = {
            "geom": _safe_percent(geom_rate, total),
            "wat": _safe_percent(wat_rate, total),
            "comp": _safe_percent(comp_rate, total),
            "sandbox": _safe_percent(sandbox_ok_count, total),
            "primary_scores": {k: _format_score(v) for k, v in primary_scores.items()},
            "secondary_scores": {k: _format_score(v) for k, v in secondary_scores.items()},
        }

    model_list = sorted(rows_by_model.keys())
    closed_models = [m for m in model_list if m.startswith("closed-")]
    open_models = [m for m in model_list if m.startswith("oss-")]

    # Overall table
    overall_lines = []
    overall_lines.append(r"\begin{table}[ht]")
    overall_lines.append(r"\centering")
    overall_lines.append(r"\caption{Overall Metrics}")
    overall_lines.append(r"\label{tab:overall-metrics}")
    overall_lines.append(r"% 使用 scalebox 调整大小，0.77 表示缩小到 77%。可以根据排版需要更改此数值。")
    overall_lines.append(r"\scalebox{0.77}{ ")
    overall_lines.append(r"\begin{tabular}{lccccccc}")
    overall_lines.append(r"\toprule")
    overall_lines.append(r"& \multicolumn{4}{c}{\textbf{Survival Metrics}} & \multicolumn{3}{c}{\textbf{Alignment Metrics}} \\")
    overall_lines.append(r"\cmidrule(lr){2-5} \cmidrule(lr){6-8}")
    header = (
        r"\textbf{Model} & "
        + r"\shortstack{Geometry \\Valid Rate}"
        + " & "
        + r"\shortstack{Watertight \\Rate}"
        + " & "
        + r"\shortstack{Component \\ Match Rate}"
        + " & "
        + r"\shortstack{Sandbox \\Success Rate}"
        + " & "
        + r"\textbf{Assemblable}"
        + " & "
        + r"\textbf{Manufacturable}"
        + " & "
        + r"\textbf{Practical}"
        + r"\\"
    )
    # Header rows
    overall_lines.append(header)
    overall_lines.append(r"\midrule")

    def add_model_section(models, section_name):
        if not models:
            return
        overall_lines.append(rf"\multicolumn{{8}}{{c}}{{\textbf{{{section_name}}}}} \\")
        overall_lines.append(r"\midrule")
        for model in models:
            data = model_rows[model]
            row = [
                _latex_escape(_normalize_model_label(model)),
                data["geom"],
                data["wat"],
                data["comp"],
                data["sandbox"],
                data["primary_scores"].get("Assemblable", "N/A"),
                data["primary_scores"].get("Manufacturable", "N/A"),
                data["primary_scores"].get("Practical", "N/A"),
            ]
            overall_lines.append(" & ".join(row) + r" \\")

    add_model_section(closed_models, "Closed-Source Models")
    overall_lines.append(r"\midrule")
    add_model_section(open_models, "Open-Source Models")
    overall_lines.append(r"\bottomrule")
    overall_lines.append(r"\end{tabular}")
    overall_lines.append(r"}")
    overall_lines.append(r"\end{table}")

    overall_table = "\n".join(overall_lines)

    # Rubric detailed table with one-row-per-model and grouped columns
    # Primary -> secondary mapping with fixed report schema
    detail_lines = []
    detail_lines.append(r"\begin{table}[ht]")
    detail_lines.append(r"\centering")
    detail_lines.append(r"\caption{Rubric Detailed Scores}")
    detail_lines.append(r"\label{tab:rubric-detailed-scores}")
    detail_lines.append(r"% 使用 scalebox 调整大小，0.85 表示缩小到 85% 以适应页面宽度")
    detail_lines.append(r"\scalebox{0.85}{")
    detail_lines.append(r"\begin{tabular}{l ccc ccc ccc}")
    detail_lines.append(r"\toprule")
    detail_lines.append(
        r"& \multicolumn{3}{c}{\textbf{Assemblable}} & "
        r"\multicolumn{3}{c}{\textbf{Manufacturable}} & "
        r"\multicolumn{3}{c}{\textbf{Practical}} \\")
    detail_lines.append(r"\cmidrule(lr){2-4} \cmidrule(lr){5-7} \cmidrule(lr){8-10}")
    detail_lines.append(
        r"\textbf{Model} & Overall & Correctness & Rationality & "
        + r"Overall & Feasibility & Adaptation & "
        + r"Overall & Sizing & Stability \\")
    detail_lines.append(r"\midrule")

    # Prepare column values for each model
    def get_detail(model_name: str) -> list[str]:
        data = model_rows[model_name]
        p = data["primary_scores"]
        s = data["secondary_scores"]
        return [
            p.get("Assemblable", "N/A"),
            s.get(("Assemblable", "Component Relationship Correctness"), "N/A"),
            s.get(("Assemblable", "Component Split Rationality"), "N/A"),
            p.get("Manufacturable", "N/A"),
            s.get(("Manufacturable", "Manufacturing Feasibility (Local Constraints & Tolerances)"), "N/A"),
            s.get(("Manufacturable", "Process Adaptation"), "N/A"),
            p.get("Practical", "N/A"),
            s.get(("Practical", "Functional Adaptation (Size Matching & Capacity)"), "N/A"),
            s.get(("Practical", "Usage Stability (Structural & Placement Stability)"), "N/A"),
        ]

    def add_rubric_section(models, section_name):
        if not models:
            return
        detail_lines.append(rf"\multicolumn{{10}}{{c}}{{\textbf{{{section_name}}}}} \\")
        detail_lines.append(r"\midrule")
        for model in models:
            row = [_latex_escape(_normalize_model_label(model))] + get_detail(model)
            detail_lines.append(" & ".join(row) + r" \\")

    add_rubric_section(closed_models, "Closed-Source Models")
    detail_lines.append(r"\midrule")
    add_rubric_section(open_models, "Open-Source Models")
    detail_lines.append(r"\bottomrule")
    detail_lines.append(r"\end{tabular}")
    detail_lines.append(r"}")
    detail_lines.append(r"\end{table}")

    detail_table = "\n".join(detail_lines)

    return overall_table, detail_table


def main():
    parser = argparse.ArgumentParser(description="Generate Overleaf tables from benchmark viewer records.")
    parser.add_argument("--records", required=True, help="Path to records.json")
    parser.add_argument("--output", default=None, help="Output .tex path (default: <records_dir>/overleaf_tables.tex)")
    args = parser.parse_args()

    records_path = Path(args.records)
    if not records_path.exists():
        raise FileNotFoundError(f"records.json not found: {records_path}")

    data = json.loads(records_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("records.json should contain a JSON array.")

    rows_by_model = defaultdict(list)
    for row in data:
        if not isinstance(row, dict):
            continue
        model_name = row.get("model_name") or row.get("model") or row.get("model_label") or "unknown"
        rows_by_model[model_name].append(row)

    overall, detail = _build_overall_table(rows_by_model)

    output_path = Path(args.output) if args.output else records_path.with_name("overleaf_tables.tex")
    output_path.write_text("\n\n".join([overall, detail]), encoding="utf-8")
    print(str(output_path))


if __name__ == "__main__":
    main()
