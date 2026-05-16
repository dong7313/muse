from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .data_prep import RubricItem
from .geometry_metrics import GeometryMetrics
from .sandbox import SandboxResult
from .svg_metrics import SvgMetrics


@dataclass(frozen=True)
class RubricScore:
    item_id: str
    title: str
    primary_category: str
    secondary_category: str
    weight: float
    max_points: float
    score: float
    points: float
    rationale: str
    deductions: list[dict[str, Any]] = field(default_factory=list)


def _expected_component_count_from_plan(plan_text: str) -> int:
    for line in plan_text.splitlines():
        if line.startswith("## 计划装配体数量"):
            continue
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    headings = [line for line in plan_text.splitlines() if line.startswith("### ")]
    return len(headings)


def expected_component_count_from_plan(plan_text: str) -> int:
    return _expected_component_count_from_plan(plan_text)


def _bbox_missing_or_collapsed(sandbox: SandboxResult) -> bool:
    positive = [value for value in sandbox.bbox if value and value > 0]
    return len(positive) < 2


def _component_actual(sandbox: SandboxResult, svg: SvgMetrics) -> int:
    return sandbox.solid_count or getattr(svg, "estimated_component_count", 0)


def _should_apply_rule(
    rule_code: str,
    *,
    item: RubricItem,
    geometry: GeometryMetrics,
    sandbox: SandboxResult,
    svg: SvgMetrics,
    expected_components: int,
) -> tuple[bool, str]:
    actual_components = _component_actual(sandbox, svg)
    if rule_code == "code_or_result_missing":
        triggered = (not geometry.code_valid) or (not sandbox.ok) or actual_components <= 0
        return triggered, f"code_valid={geometry.code_valid}, sandbox_ok={sandbox.ok}, actual_components={actual_components}"
    if rule_code == "global_geometry_invalid":
        triggered = not geometry.geometry_valid or geometry.occt_valid is False
        return triggered, f"geometry_valid={geometry.geometry_valid}, occt_valid={geometry.occt_valid}"
    if rule_code == "bbox_missing_or_collapsed":
        triggered = _bbox_missing_or_collapsed(sandbox)
        return triggered, f"bbox={sandbox.bbox}"
    if rule_code == "functional_support_or_access_risk":
        triggered = (not geometry.geometry_valid) or actual_components <= 0 or getattr(svg, "total_path_count", 0) <= 0
        return triggered, f"geometry_valid={geometry.geometry_valid}, actual_components={actual_components}, svg_path_count={getattr(svg, 'total_path_count', 0)}"
    if rule_code == "contact_safety_risk":
        triggered = (not geometry.geometry_valid) or geometry.bbox_valid is False
        return triggered, f"geometry_valid={geometry.geometry_valid}, bbox_valid={geometry.bbox_valid}"
    if rule_code == "structural_strength_risk":
        triggered = (not geometry.geometry_valid) or geometry.volume_valid is False or geometry.bbox_valid is False
        return triggered, f"geometry_valid={geometry.geometry_valid}, volume_valid={geometry.volume_valid}, bbox_valid={geometry.bbox_valid}"
    if rule_code == "component_count_mismatch":
        triggered = actual_components != expected_components
        return triggered, f"gt_component_count={expected_components}, actual_component_count={actual_components}"
    if rule_code == "assembly_relationship_risk":
        delta = abs(actual_components - expected_components)
        triggered = delta >= 1 or (item.secondary_category == "部件关系正确性" and geometry.geometry_valid is False)
        return triggered, f"component_delta={delta}, geometry_valid={geometry.geometry_valid}"
    if rule_code == "functional_structure_broken":
        triggered = (not geometry.geometry_valid) or geometry.watertight is False
        return triggered, f"geometry_valid={geometry.geometry_valid}, watertight={geometry.watertight}"
    if rule_code == "local_continuity_risk":
        triggered = geometry.self_intersection_free is False or geometry.normal_consistency is False or geometry.volume_valid is False
        return triggered, (
            f"self_intersection_free={geometry.self_intersection_free}, "
            f"normal_consistency={geometry.normal_consistency}, volume_valid={geometry.volume_valid}"
        )
    if rule_code == "process_fit_risk":
        triggered = (
            geometry.self_intersection_free is False
            or geometry.normal_consistency is False
            or geometry.volume_valid is False
            or geometry.bbox_valid is False
        )
        return triggered, (
            f"self_intersection_free={geometry.self_intersection_free}, "
            f"normal_consistency={geometry.normal_consistency}, volume_valid={geometry.volume_valid}, bbox_valid={geometry.bbox_valid}"
        )
    if rule_code == "local_feature_manufacturing_risk":
        triggered = (not geometry.geometry_valid) or geometry.bbox_valid is False or getattr(svg, "total_path_count", 0) <= 0
        return triggered, f"geometry_valid={geometry.geometry_valid}, bbox_valid={geometry.bbox_valid}, svg_path_count={getattr(svg, 'total_path_count', 0)}"
    if rule_code == "parameter_range_fragility":
        triggered = (not geometry.geometry_valid) or geometry.volume_valid is False or geometry.bbox_valid is False
        return triggered, f"geometry_valid={geometry.geometry_valid}, volume_valid={geometry.volume_valid}, bbox_valid={geometry.bbox_valid}"
    if rule_code == "narrow_safe_range":
        triggered = _bbox_missing_or_collapsed(sandbox) or geometry.geometry_valid is False
        return triggered, f"bbox={sandbox.bbox}, geometry_valid={geometry.geometry_valid}"
    if rule_code == "wave_continuity_or_opening_risk":
        triggered = (not geometry.geometry_valid) or geometry.watertight is False or getattr(svg, "total_path_count", 0) <= 0
        return triggered, f"geometry_valid={geometry.geometry_valid}, watertight={geometry.watertight}, svg_path_count={getattr(svg, 'total_path_count', 0)}"
    if rule_code == "pegboard_grid_fit_risk":
        triggered = (not geometry.geometry_valid) or getattr(svg, "estimated_component_count", 0) <= 0
        return triggered, f"geometry_valid={geometry.geometry_valid}, svg_component_estimate={getattr(svg, 'estimated_component_count', 0)}"
    return False, "rule_not_triggered"


def _score_from_rubric_rules(
    item: RubricItem,
    *,
    geometry: GeometryMetrics,
    sandbox: SandboxResult,
    svg: SvgMetrics,
    expected_components: int,
) -> tuple[float, list[dict[str, Any]], str]:
    score = 1.0
    deductions: list[dict[str, Any]] = []
    for rule in item.deduction_rules:
        rule_code = str(rule.get("rule_code", "")).strip()
        if not rule_code:
            continue
        triggered, evidence = _should_apply_rule(
            rule_code,
            item=item,
            geometry=geometry,
            sandbox=sandbox,
            svg=svg,
            expected_components=expected_components,
        )
        if not triggered:
            continue
        deduction_ratio = float(rule.get("deduction_ratio", 0.0) or 0.0)
        score -= deduction_ratio
        deductions.append(
            {
                "rule_code": rule_code,
                "trigger": rule.get("trigger", ""),
                "deduction_ratio": deduction_ratio,
                "evidence": evidence,
            }
        )
    score = max(0.0, min(1.0, score))
    rationale = "No deductions triggered; rubric evidence remained consistent."
    if deductions:
        rationale = " | ".join(
            f"{item['rule_code']}: -{item['deduction_ratio']:.2f} ({item['evidence']})"
            for item in deductions
        )
    return score, deductions, rationale


def score_rubric(
    rubric_items: Iterable[RubricItem],
    *,
    geometry: GeometryMetrics,
    sandbox: SandboxResult,
    svg: SvgMetrics,
    task_text: str,
    plan_text: str,
) -> list[RubricScore]:
    del task_text
    expected_components = _expected_component_count_from_plan(plan_text)
    scores: list[RubricScore] = []
    for item in rubric_items:
        score_ratio, deductions, rationale = _score_from_rubric_rules(
            item,
            geometry=geometry,
            sandbox=sandbox,
            svg=svg,
            expected_components=expected_components,
        )
        points = item.max_points * score_ratio
        scores.append(
            RubricScore(
                item_id=item.item_id,
                title=item.title,
                primary_category=item.primary_category,
                secondary_category=item.secondary_category,
                weight=item.normalized_weight,
                max_points=item.max_points,
                score=score_ratio,
                points=points,
                rationale=rationale,
                deductions=deductions,
            )
        )
    return scores


def weighted_rubric_score(scores: Iterable[RubricScore]) -> float:
    return sum(item.weight * item.score for item in scores)


def category_score_breakdown(scores: Iterable[RubricScore]) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for item in scores:
        key = f"{item.primary_category}/{item.secondary_category}"
        bucket = summary.setdefault(
            key,
            {
                "primary_category": item.primary_category,
                "secondary_category": item.secondary_category,
                "item_count": 0,
                "max_points": 0.0,
                "earned_points": 0.0,
                "weighted_score": 0.0,
            },
        )
        bucket["item_count"] = int(bucket["item_count"]) + 1
        bucket["max_points"] = float(bucket["max_points"]) + item.max_points
        bucket["earned_points"] = float(bucket["earned_points"]) + item.points
        bucket["weighted_score"] = float(bucket["weighted_score"]) + (item.weight * item.score)
    for bucket in summary.values():
        max_points = float(bucket["max_points"])
        bucket["ratio"] = 0.0 if max_points <= 0 else float(bucket["earned_points"]) / max_points
    return summary


def primary_category_score_breakdown(scores: Iterable[RubricScore]) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for item in scores:
        bucket = summary.setdefault(
            item.primary_category,
            {
                "primary_category": item.primary_category,
                "item_count": 0,
                "max_points": 0.0,
                "earned_points": 0.0,
                "weighted_score": 0.0,
            },
        )
        bucket["item_count"] = int(bucket["item_count"]) + 1
        bucket["max_points"] = float(bucket["max_points"]) + item.max_points
        bucket["earned_points"] = float(bucket["earned_points"]) + item.points
        bucket["weighted_score"] = float(bucket["weighted_score"]) + (item.weight * item.score)
    for bucket in summary.values():
        max_points = float(bucket["max_points"])
        bucket["ratio"] = 0.0 if max_points <= 0 else float(bucket["earned_points"]) / max_points
    return summary
