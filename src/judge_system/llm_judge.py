from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import requests


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    return cleaned.strip()


def _image_part(path: Path) -> dict[str, Any]:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def _load_llm_rubric(rubric_path: Path) -> list[dict[str, Any]]:
    gemini_candidate = rubric_path.with_name(f"{rubric_path.stem}_gemini.json")
    if gemini_candidate.exists():
        target = gemini_candidate
        payload = json.loads(target.read_text(encoding="utf-8"))
    elif rubric_path.exists():
        target = rubric_path
        payload = json.loads(target.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError(f"No rubric JSON found for LLM judge: {rubric_path}")

    items: list[dict[str, Any]] = []
    for primary in payload.get("taxonomy_rubric", []):
        primary_en = primary.get("primary_category_en", "")
        for secondary in primary.get("secondary_categories", []):
            if not secondary.get("applicable"):
                continue
            items.append(
                {
                    "primary_category_en": primary_en,
                    "secondary_category_en": secondary.get("secondary_category_en", ""),
                    "evaluation_objective": secondary.get("evaluation_objective", ""),
                    "grading_descriptions": secondary.get("grading_descriptions", {}),
                }
            )
    return items


def build_llm_judge_prompt(task_text: str, plan_text: str, rubric_items: list[dict[str, Any]]) -> str:
    rubric_blob = json.dumps(rubric_items, ensure_ascii=False, indent=2)
    schema = {
        "items": [
            {
                "primary_category_en": "Practicality",
                "secondary_category_en": "Functional Fit",
                "score": 1,
                "rationale": "Short evidence-based explanation.",
            }
        ],
        "overall_score_normalized": 0.2,
        "overall_summary": "Short overall judgement.",
    }
    return f"""You are a careful CAD design judge.

You will evaluate one generated CAD result using:
- the task
- the plan
- rendered images of the generated result
- the rubric items below

Task markdown:
{task_text.strip()}

Plan markdown:
{plan_text.strip()}

Rubric items:
{rubric_blob}

Instructions:
- Score each rubric item with exactly one of: 1, 3, 5.
- Use the grading descriptions exactly as the standard for each item.
- For every item, choose the single closest band from the provided grading descriptions.
- Do not invent intermediate scores, averages, or custom scoring rules.
- Do not use any hidden deduction logic. The score must come only from the grading descriptions.
- Base your judgement on the visible geometry in the provided images together with the task and plan.
- Be strict. A 3 means the design basically works but has clear flaws.
- Return valid JSON only.

Output schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}
"""


def judge_with_vlm(
    *,
    api_key_env: str,
    base_url: str,
    model: str,
    timeout_seconds: int,
    rubric_path: Path,
    task_text: str,
    plan_text: str,
    image_paths: list[Path],
) -> dict[str, Any]:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} is not configured for VLM judging.")

    rubric_items = _load_llm_rubric(rubric_path)
    prompt = build_llm_judge_prompt(task_text, plan_text, rubric_items)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        if path.exists():
            content.append(_image_part(path))

    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.codex.app",
            "X-Title": "judge_system llm judge",
        },
        json={
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": content},
            ],
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(_strip_fences(text))
    scores = [int(item.get("score", 0)) for item in parsed.get("items", []) if str(item.get("score", "")).isdigit()]
    if "overall_score_normalized" not in parsed:
        parsed["overall_score_normalized"] = (sum(scores) / (5 * len(scores))) if scores else 0.0
    return parsed
