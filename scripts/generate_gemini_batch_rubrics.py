from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from generate_taxonomy_rubrics import (
    DEFAULT_CLEANED_ROOT,
    DEFAULT_TAXONOMY_PATH,
    PRIMARY_ZH_TO_EN,
    SECONDARY_ZH_TO_EN,
    CaseContext,
    load_case_context,
    parse_taxonomy,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_RUBRIC_ROOT = ROOT / "data" / "rubrics"
DEFAULT_MODEL = "google/gemini-2.5-flash"

DEFAULT_CASES = [
    "stool",
    "chair",
    "table",
    "vase_teardrop",
    "pen_holder",
    "business_card_holder",
    "pegboard",
    "handless_comb",
    "cnc_shoe_rack_entry_bench",
    "cnc_tv_stand_gallery",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate English rubric JSON files with one Gemini request per case.")
    parser.add_argument("--cleaned-root", type=Path, default=DEFAULT_CLEANED_ROOT)
    parser.add_argument("--rubric-root", type=Path, default=DEFAULT_RUBRIC_ROOT)
    parser.add_argument("--taxonomy-path", type=Path, default=DEFAULT_TAXONOMY_PATH)
    parser.add_argument("--cases", nargs="*", default=DEFAULT_CASES)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def require_api_key() -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return api_key


def strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    return cleaned.strip()


def compact_range_summary(param_ranges: dict[str, Any]) -> str:
    parts: list[str] = []
    for name, value in param_ranges.items():
        if isinstance(value, (tuple, list)) and len(value) == 2:
            parts.append(f"{name}={value[0]}~{value[1]}")
    return ", ".join(parts[:12])


def build_case_summary(context: CaseContext) -> str:
    svg_meta = context.svg_meta
    return "\n".join(
        [
            f"task_name: {context.case_name}",
            f"object_type: {context.object_type}",
            f"family: {context.family}",
            f"dimensions: {context.dimensions or 'not specified'}",
            f"material: {context.material or 'not specified'}",
            f"manufacturing_method: {context.manufacturing_method or 'not specified'}",
            f"connection_method: {context.connection_method or 'not specified'}",
            f"structural_features: {context.structural_features or 'not specified'}",
            f"special_requirements: {context.special_requirements or 'not specified'}",
            f"planned_component_count: {context.component_count_from_plan}",
            f"component_names: {', '.join(context.component_names) if context.component_names else 'not explicitly declared'}",
            f"operations: {', '.join(context.operations) if context.operations else 'none detected'}",
            f"param_ranges: {compact_range_summary(context.param_ranges) or 'none declared'}",
            f"svg_view_box: {svg_meta.get('view_box') or 'n/a'}",
            f"svg_path_count: {svg_meta.get('path_count', 0)}",
            f"svg_rect_count: {svg_meta.get('rect_count', 0)}",
            f"svg_circle_count: {svg_meta.get('circle_count', 0)}",
        ]
    )


def source_bundle(case_dir: Path) -> dict[str, str]:
    exact_py = case_dir / f"{case_dir.name}.py"
    py_path = exact_py if exact_py.exists() else next(case_dir.glob("*.py"))
    exact_svg = case_dir / f"{case_dir.name}.svg"
    svg_path = exact_svg if exact_svg.exists() else next(case_dir.glob("*.svg"))
    return {
        "task_md": str(case_dir / "task.md"),
        "plan_md": str(case_dir / "plan.md"),
        "cadquery_code": str(py_path),
        "svg": str(svg_path),
        "rubric_taxonomy_source": str(DEFAULT_TAXONOMY_PATH),
    }


def build_case_prompt(context: CaseContext, taxonomy_items: list[Any]) -> str:
    taxonomy_payload = []
    for primary_zh in ("可实用", "可组装", "可建造"):
        taxonomy_payload.append(
            {
                "primary_category_zh": primary_zh,
                "primary_category_en": PRIMARY_ZH_TO_EN[primary_zh],
                "secondary_categories": [
                    {
                        "secondary_category_zh": item.secondary_zh,
                        "secondary_category_en": SECONDARY_ZH_TO_EN[item.secondary_zh],
                        "taxonomy_definition": item.definition or "",
                        "focus_points": item.focus_points,
                    }
                    for item in taxonomy_items
                    if item.primary_zh == primary_zh
                ],
            }
        )

    schema = {
        "task_name": context.case_name,
        "language": "en",
        "source_bundle": {
            "task_md": "absolute path",
            "plan_md": "absolute path",
            "cadquery_code": "absolute path",
            "svg": "absolute path",
            "rubric_taxonomy_source": "absolute path",
        },
        "design_context": {
            "object_type": context.object_type,
            "family": context.family,
            "dimensions": context.dimensions,
            "material": context.material,
            "manufacturing_method": context.manufacturing_method,
            "connection_method": context.connection_method,
            "structural_features": context.structural_features,
            "special_requirements": context.special_requirements,
            "component_names": context.component_names,
            "planned_component_count": context.component_count_from_plan,
        },
        "taxonomy_rubric": [
            {
                "primary_category_zh": "可实用",
                "primary_category_en": "Practicality",
                "secondary_categories": [
                    {
                        "secondary_category_zh": "功能适配",
                        "secondary_category_en": "Functional Fit",
                        "applicable": True,
                        "applicability_reason": "...",
                        "taxonomy_definition": "...",
                        "focus_points": ["...", "..."],
                        "rubric_title": "...",
                        "evaluation_objective": "...",
                        "grading_descriptions": {
                            "5 Points (Excellent)": "...",
                            "3 Points (Adequate)": "...",
                            "1 Point (Fail)": "...",
                        },
                        "specific_checks": ["...", "...", "..."],
                    }
                ],
            }
        ],
        "generation_notes": ["...", "..."],
    }

    return f"""Return exactly one JSON object and nothing else.

You are generating a full English CAD evaluation rubric for one benchmark case.

Case evidence:
{build_case_summary(context)}

Source bundle:
{json.dumps(source_bundle(context.case_dir), ensure_ascii=False, indent=2)}

Taxonomy to use:
{json.dumps(taxonomy_payload, ensure_ascii=False, indent=2)}

Required output schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}

Rules:
- Output must be valid JSON.
- Keep all primary categories and all secondary categories from the taxonomy.
- Decide applicability for each secondary category based on this exact case.
- If a secondary category is not applicable, include only:
  secondary_category_zh, secondary_category_en, applicable, applicability_reason, taxonomy_definition, focus_points.
- If a secondary category is applicable, also include:
  rubric_title, evaluation_objective, grading_descriptions, specific_checks.
- Use exactly 3 specific_checks for every applicable category.
- The 3-point description must mean "basically works but has clear flaws", not "almost perfect".
- Keep the content specific to this case instead of generic product-design advice.
- Do not mention unsupported measurements or fabricated evidence.
"""


def call_openrouter(api_key: str, model: str, prompt: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://local.codex.app",
        "X-Title": "judge_system gemini rubric batch",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 9000,
    }
    response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"]
    return json.loads(strip_fences(text))


def normalize_item(item: dict[str, Any], taxonomy_item: Any) -> dict[str, Any]:
    normalized = {
        "secondary_category_zh": taxonomy_item.secondary_zh,
        "secondary_category_en": SECONDARY_ZH_TO_EN[taxonomy_item.secondary_zh],
        "applicable": bool(item.get("applicable")),
        "applicability_reason": str(item.get("applicability_reason", "")).strip(),
        "taxonomy_definition": str(item.get("taxonomy_definition", taxonomy_item.definition or "")).strip(),
        "focus_points": taxonomy_item.focus_points,
    }
    if not normalized["applicable"]:
        return normalized

    grading = item.get("grading_descriptions") or {}
    checks = [str(value).strip() for value in item.get("specific_checks", []) if str(value).strip()]
    normalized["rubric_title"] = str(item.get("rubric_title", "")).strip()
    normalized["evaluation_objective"] = str(item.get("evaluation_objective", "")).strip()
    normalized["grading_descriptions"] = {
        "5 Points (Excellent)": str(grading.get("5 Points (Excellent)", "")).strip(),
        "3 Points (Adequate)": str(grading.get("3 Points (Adequate)", "")).strip(),
        "1 Point (Fail)": str(grading.get("1 Point (Fail)", "")).strip(),
    }
    normalized["specific_checks"] = checks[:3]
    while len(normalized["specific_checks"]) < 3:
        normalized["specific_checks"].append(
            f"Check case-specific evidence for {SECONDARY_ZH_TO_EN[taxonomy_item.secondary_zh]} against the task, plan, code, and SVG."
        )
    return normalized


def normalize_document(context: CaseContext, taxonomy_items: list[Any], raw_document: dict[str, Any]) -> dict[str, Any]:
    raw_by_secondary: dict[str, dict[str, Any]] = {}
    for primary in raw_document.get("taxonomy_rubric", []):
        for item in primary.get("secondary_categories", []):
            secondary_zh = str(item.get("secondary_category_zh", "")).strip()
            if secondary_zh:
                raw_by_secondary[secondary_zh] = item

    grouped = []
    for primary_zh in ("可实用", "可组装", "可建造"):
        grouped.append(
            {
                "primary_category_zh": primary_zh,
                "primary_category_en": PRIMARY_ZH_TO_EN[primary_zh],
                "secondary_categories": [
                    normalize_item(raw_by_secondary.get(item.secondary_zh, {}), item)
                    for item in taxonomy_items
                    if item.primary_zh == primary_zh
                ],
            }
        )

    return {
        "task_name": context.case_name,
        "language": "en",
        "source_bundle": source_bundle(context.case_dir),
        "design_context": {
            "object_type": context.object_type,
            "family": context.family,
            "dimensions": context.dimensions,
            "material": context.material,
            "manufacturing_method": context.manufacturing_method,
            "connection_method": context.connection_method,
            "structural_features": context.structural_features,
            "special_requirements": context.special_requirements,
            "component_names": context.component_names,
            "planned_component_count": context.component_count_from_plan,
        },
        "taxonomy_rubric": grouped,
        "generation_notes": [
            "Each case was generated with a single Gemini request via OpenRouter.",
            "This file is the Gemini batch variant and intentionally does not include a Chinese translation companion file.",
        ],
    }


def output_path(case_name: str, rubric_root: Path) -> Path:
    return rubric_root / f"{case_name}_gemini.json"


def main() -> None:
    args = parse_args()
    api_key = require_api_key()
    args.rubric_root.mkdir(parents=True, exist_ok=True)
    taxonomy_items = parse_taxonomy(args.taxonomy_path)

    for case_name in args.cases:
        case_dir = args.cleaned_root / case_name
        if not case_dir.exists():
            raise FileNotFoundError(f"Missing case directory: {case_dir}")
        out_path = output_path(case_name, args.rubric_root)
        if out_path.exists() and not args.overwrite:
            print(f"skip {case_name}: {out_path} exists")
            continue

        context = load_case_context(case_dir)
        prompt = build_case_prompt(context, taxonomy_items)
        raw_document = call_openrouter(api_key, args.model, prompt)
        document = normalize_document(context, taxonomy_items, raw_document)
        out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"generated {out_path}")


if __name__ == "__main__":
    main()
