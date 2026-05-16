from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


WEIGHT_SECTION_PATTERNS = (
    r"^##\s*功能需求与评价权重\s*$",
    r"^##\s*功能需求及评分权重\s*$",
)


@dataclass(frozen=True)
class RubricItem:
    item_id: str
    title: str
    description: str
    raw_weight: float
    normalized_weight: float
    source: str
    primary_category: str = ""
    secondary_category: str = ""
    max_points: float = 0.0
    scoring_method: str = ""
    score_bands: tuple = ()
    deduction_rules: tuple = ()


def _split_weight_section(markdown_text: str) -> tuple[str, str]:
    lines = markdown_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if any(re.match(pattern, line.strip()) for pattern in WEIGHT_SECTION_PATTERNS):
            start = idx
            break
    if start is None:
        return markdown_text.strip() + "\n", ""

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break

    cleaned_lines = lines[:start] + lines[end:]
    section_lines = lines[start:end]
    cleaned = "\n".join(cleaned_lines).strip() + "\n"
    section = "\n".join(section_lines).strip() + "\n"
    return cleaned, section


def _extract_rubric_items(section_text: str, source: str) -> list[RubricItem]:
    items: list[RubricItem] = []
    bullet_pattern = re.compile(
        r"^\s*-\s*#?(?P<id>\d+)\s+(?P<title>[^:：]+)[:：]\s*(?P<desc>.+?)"
        r"(?:（权重系数[:：]\s*(?P<coef>\d+(?:\.\d+)?)）|\(权重[:：]\s*(?P<score>\d+)(?:/\d+)?\))\s*$"
    )

    for line in section_text.splitlines():
        match = bullet_pattern.match(line.strip())
        if not match:
            continue
        raw_weight = float(match.group("coef") or match.group("score") or 0)
        items.append(
            RubricItem(
                item_id=match.group("id"),
                title=match.group("title").strip(),
                description=match.group("desc").strip(),
                raw_weight=raw_weight,
                normalized_weight=0.0,
                source=source,
            )
        )

    if not items:
        return []

    deduped: dict[tuple[str, str], RubricItem] = {}
    for item in items:
        key = (item.title, item.description)
        current = deduped.get(key)
        if current is None or item.raw_weight > current.raw_weight:
            deduped[key] = item
    items = list(deduped.values())
    positive_items = [item for item in items if item.raw_weight > 0]
    if positive_items:
        items = positive_items
    total = sum(item.raw_weight for item in items)

    if total <= 0:
        total = float(len(items))

    return [
        RubricItem(
            item_id=item.item_id,
            title=item.title,
            description=item.description,
            raw_weight=item.raw_weight,
            normalized_weight=item.raw_weight / total,
            source=item.source,
        )
        for item in items
    ]


def prepare_dataset(source_root: Path, cleaned_root: Path, rubric_root: Path) -> dict[str, list[RubricItem]]:
    cleaned_root.mkdir(parents=True, exist_ok=True)
    rubric_root.mkdir(parents=True, exist_ok=True)

    rubric_map: dict[str, list[RubricItem]] = {}

    for task_dir in sorted(path for path in source_root.iterdir() if path.is_dir()):
        task_file = task_dir / "task.md"
        plan_file = task_dir / "plan.md"
        if not task_file.exists() or not plan_file.exists():
            continue
        target_dir = cleaned_root / task_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)

        task_text = task_file.read_text(encoding="utf-8")
        plan_text = plan_file.read_text(encoding="utf-8")

        cleaned_task, task_section = _split_weight_section(task_text)
        cleaned_plan, plan_section = _split_weight_section(plan_text)

        (target_dir / "task.md").write_text(cleaned_task, encoding="utf-8")
        (target_dir / "plan.md").write_text(cleaned_plan, encoding="utf-8")

        rubric_items = _extract_rubric_items(plan_section, "plan.md") or _extract_rubric_items(task_section, "task.md")
        rubric_map[task_dir.name] = rubric_items

        target_rubric_path = rubric_root / f"{task_dir.name}.json"
        if target_rubric_path.exists():
            try:
                existing_payload = json.loads(target_rubric_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing_payload = None
            if isinstance(existing_payload, dict) and "active_rubrics" in existing_payload:
                continue

        target_rubric_path.write_text(
            json.dumps(
                {
                    "task_name": task_dir.name,
                    "items": [
                        {
                            "item_id": item.item_id,
                            "title": item.title,
                            "description": item.description,
                            "raw_weight": item.raw_weight,
                            "normalized_weight": item.normalized_weight,
                            "source": item.source,
                        }
                        for item in rubric_items
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return rubric_map
