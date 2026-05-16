from __future__ import annotations

import argparse
import ast
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CLEANED_ROOT = ROOT / "data" / "cleaned"
DEFAULT_RUBRIC_ROOT = ROOT / "data" / "rubrics"
DEFAULT_RUBRIC_ZH_ROOT = ROOT / "data" / "rubrics_zh"
DEFAULT_TAXONOMY_PATH = ROOT / "rubric.md"

PRIMARY_PATTERN = re.compile(r"^##\s+[一二三四五六七八九十]+、(.+)$")
SECONDARY_PATTERN = re.compile(r"^###\s+\d+\.\s+(.+)$")

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "object_type": ("Object type", "对象类型"),
    "dimensions": ("Geometry and dimensions", "几何与尺寸"),
    "material": ("Material", "材料"),
    "manufacturing_method": ("Manufacturing method", "制造方法"),
    "connection_method": ("Connection method", "连接方式"),
    "structural_features": ("Structural features", "结构特征"),
    "special_requirements": ("Special requirements", "特殊要求"),
}

PRIMARY_ZH_TO_EN = {
    "可实用": "Practicality",
    "可组装": "Assembly",
    "可建造": "Constructability",
}

SECONDARY_ZH_TO_EN = {
    "功能适配": "Functional Fit",
    "使用稳定": "Usage Stability",
    "使用安全": "Usage Safety",
    "使用强度": "Usage Strength",
    "部件拆分合理性": "Component Split Rationality",
    "部件关系正确性": "Component Relationship Correctness",
    "功能结构完整性": "Functional Structure Integrity",
    "工艺适配": "Process Fit",
    "局部特征可实现": "Local Feature Realizability",
    "材料与厚度合理": "Material and Thickness Rationality",
    "制造鲁棒性": "Manufacturing Robustness",
}


@dataclass(frozen=True)
class SecondaryTaxonomy:
    primary_zh: str
    secondary_zh: str
    definition: str
    focus_points: list[str]


@dataclass(frozen=True)
class CaseContext:
    case_name: str
    case_dir: Path
    object_type: str
    family: str
    dimensions: str
    material: str
    manufacturing_method: str
    connection_method: str
    structural_features: str
    special_requirements: str
    component_names: list[str]
    component_count_from_plan: int
    param_ranges: dict[str, Any]
    operations: list[str]
    svg_meta: dict[str, Any]
    task_text: str
    plan_text: str
    code_text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate taxonomy-based rubrics from task/plan/svg/py files.")
    parser.add_argument("--cleaned-root", type=Path, default=DEFAULT_CLEANED_ROOT)
    parser.add_argument("--rubric-root", type=Path, default=DEFAULT_RUBRIC_ROOT)
    parser.add_argument("--rubric-zh-root", type=Path, default=DEFAULT_RUBRIC_ZH_ROOT)
    parser.add_argument("--taxonomy-path", type=Path, default=DEFAULT_TAXONOMY_PATH)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def section_value(sections: dict[str, str], key: str) -> str:
    for alias in SECTION_ALIASES.get(key, ()):
        value = sections.get(alias, "").strip()
        if value:
            return value
    return ""


def parse_taxonomy(path: Path) -> list[SecondaryTaxonomy]:
    items: list[SecondaryTaxonomy] = []
    current_primary: str | None = None
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        primary_match = PRIMARY_PATTERN.match(stripped)
        if primary_match:
            current_primary = primary_match.group(1).strip()
            i += 1
            continue
        secondary_match = SECONDARY_PATTERN.match(stripped)
        if secondary_match and current_primary:
            secondary = secondary_match.group(1).strip()
            i += 1
            definition = ""
            focus_points: list[str] = []
            mode: str | None = None
            while i < len(lines):
                current = lines[i].strip()
                if PRIMARY_PATTERN.match(current) or SECONDARY_PATTERN.match(current):
                    break
                if current.startswith("定义："):
                    definition = current.removeprefix("定义：").strip()
                elif current == "关注点：":
                    mode = "focus"
                elif current in {"典型 rubric：", "和其他小类的区别："}:
                    mode = None
                elif mode == "focus" and current.startswith("- "):
                    focus_points.append(current[2:].strip().strip("`"))
                i += 1
            items.append(
                SecondaryTaxonomy(
                    primary_zh=current_primary,
                    secondary_zh=secondary,
                    definition=definition,
                    focus_points=focus_points,
                )
            )
            continue
        i += 1
    return items


def primary_py_path(case_dir: Path) -> Path | None:
    exact = case_dir / f"{case_dir.name}.py"
    if exact.exists():
        return exact
    for candidate in sorted(case_dir.glob("*.py")):
        return candidate
    return None


def primary_svg_path(case_dir: Path) -> Path | None:
    exact = case_dir / f"{case_dir.name}.svg"
    if exact.exists():
        return exact
    for candidate in sorted(case_dir.glob("*.svg")):
        return candidate
    return None


def parse_python_metadata(path: Path | None) -> tuple[list[str], dict[str, Any], list[str], str]:
    if path is None:
        return [], {}, [], ""
    source = read_text(path)
    tree = ast.parse(source)
    component_names: list[str] = []
    param_ranges: dict[str, Any] = {}
    operations = sorted(
        {
            token
            for token in (
                "cut",
                "fuse",
                "fillet",
                "translate",
                "rotate",
                "extrude",
                "loft",
                "revolve",
                "mirror",
                "shell",
                "socket",
                "tenon",
                "slot",
                "hole",
            )
            if token in source.lower()
        }
    )
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            if target.id == "COMPONENT_NAMES" and isinstance(value, list):
                component_names = [str(item) for item in value]
            elif target.id == "PARAM_RANGES" and isinstance(value, dict):
                param_ranges = value
    return component_names, param_ranges, operations, source


def parse_svg_metadata(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"view_box": "", "path_count": 0, "rect_count": 0, "circle_count": 0}
    raw = read_text(path)
    root = ET.fromstring(raw)

    def tag_name(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    counts = {"path_count": 0, "rect_count": 0, "circle_count": 0}
    for element in root.iter():
        name = tag_name(element)
        if name == "path":
            counts["path_count"] += 1
        elif name == "rect":
            counts["rect_count"] += 1
        elif name == "circle":
            counts["circle_count"] += 1
    counts["view_box"] = root.attrib.get("viewBox", "")
    return counts


def component_count_from_plan(plan_text: str) -> int:
    lines = plan_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "## 计划装配体数量":
            for probe in lines[idx + 1 : idx + 4]:
                stripped = probe.strip()
                if stripped.isdigit():
                    return int(stripped)
    return len([line for line in lines if line.startswith("### ")])


def normalize_family(case_name: str, object_type: str) -> str:
    lowered = f"{case_name} {object_type}".lower()
    if "stool" in lowered or "凳" in lowered:
        return "stool"
    if "chair" in lowered or "椅" in lowered:
        return "chair"
    if "table" in lowered or "桌" in lowered:
        return "table"
    if "vase" in lowered or "花瓶" in lowered:
        return "vase"
    if "pen" in lowered or "笔筒" in lowered:
        return "pen_holder"
    if "card holder" in lowered or "名片" in lowered:
        return "card_holder"
    if "pegboard" in lowered or "洞洞板" in lowered:
        return "pegboard"
    if "rack" in lowered or "架" in lowered:
        return "rack"
    return case_name


def load_case_context(case_dir: Path) -> CaseContext:
    task_path = case_dir / "task.md"
    plan_path = case_dir / "plan.md"
    py_path = primary_py_path(case_dir)
    svg_path = primary_svg_path(case_dir)
    task_text = read_text(task_path)
    plan_text = read_text(plan_path)
    task_sections = parse_markdown_sections(task_text)
    component_names, param_ranges, operations, code_text = parse_python_metadata(py_path)
    return CaseContext(
        case_name=case_dir.name,
        case_dir=case_dir,
        object_type=section_value(task_sections, "object_type") or case_dir.name,
        family=normalize_family(case_dir.name, section_value(task_sections, "object_type") or case_dir.name),
        dimensions=section_value(task_sections, "dimensions"),
        material=section_value(task_sections, "material"),
        manufacturing_method=section_value(task_sections, "manufacturing_method"),
        connection_method=section_value(task_sections, "connection_method"),
        structural_features=section_value(task_sections, "structural_features"),
        special_requirements=section_value(task_sections, "special_requirements"),
        component_names=component_names,
        component_count_from_plan=component_count_from_plan(plan_text),
        param_ranges=param_ranges,
        operations=operations,
        svg_meta=parse_svg_metadata(svg_path),
        task_text=task_text,
        plan_text=plan_text,
        code_text=code_text,
    )


def is_load_bearing_furniture(context: CaseContext) -> bool:
    return context.family in {"stool", "chair", "table", "rack"}


def is_multi_part(context: CaseContext) -> bool:
    return max(len(context.component_names), context.component_count_from_plan) > 1


def has_joinery(context: CaseContext) -> bool:
    lowered = " ".join(context.operations).lower() + " " + context.code_text.lower()
    return any(token in lowered for token in ("tenon", "socket", "slot", "mortise", "榫", "槽"))


def has_parametric_ranges(context: CaseContext) -> bool:
    return bool(context.param_ranges)


def applicability_reason(context: CaseContext, secondary_zh: str) -> tuple[bool, str]:
    if secondary_zh == "功能适配":
        return True, "The object has a named use scenario, so functional fit should always be evaluated."
    if secondary_zh == "使用稳定":
        needed = is_load_bearing_furniture(context) or context.family in {"pen_holder", "card_holder", "vase"}
        reason = (
            "The object must remain stable during normal placement or load-bearing use."
            if needed
            else "This object does not primarily depend on freestanding support stability."
        )
        return needed, reason
    if secondary_zh == "使用安全":
        needed = context.family in {"comb"} or "安全" in context.special_requirements
        reason = (
            "Direct hand-contact edge safety is explicitly important in this case."
            if needed
            else "No strong hand-contact edge hazard is expressed by the current task, so this subcategory is not a priority rubric item."
        )
        return needed, reason
    if secondary_zh == "使用强度":
        needed = is_load_bearing_furniture(context) or context.family in {"pegboard", "pen_holder", "card_holder"}
        reason = (
            "The design depends on keeping enough material in load-bearing or frequently handled regions."
            if needed
            else "Daily usage strength is not the main differentiator for this object family."
        )
        return needed, reason
    if secondary_zh == "部件拆分合理性":
        needed = is_multi_part(context)
        reason = (
            "The plan defines multiple independent parts, so the split itself must be graded."
            if needed
            else "The object is effectively a single body, so part split rationality is not needed."
        )
        return needed, reason
    if secondary_zh == "部件关系正确性":
        needed = is_multi_part(context)
        reason = (
            "Multiple parts must preserve their intended relative positions and joinery logic."
            if needed
            else "Without multiple independent parts, part relationship correctness is not needed."
        )
        return needed, reason
    if secondary_zh == "功能结构完整性":
        needed = context.family in {"vase", "pegboard", "chair", "stool", "table", "rack", "pen_holder", "card_holder"} or is_multi_part(context)
        reason = (
            "This design includes key functional substructures that must remain complete."
            if needed
            else "There is no prominent functional substructure that needs a separate integrity rubric."
        )
        return needed, reason
    if secondary_zh == "工艺适配":
        return True, "The output is intended as a real buildable CAD design, so process fit should be evaluated."
    if secondary_zh == "局部特征可实现":
        return True, "Local slots, corners, hollows, or interface features can directly affect whether the design is actually realizable."
    if secondary_zh == "材料与厚度合理":
        needed = is_load_bearing_furniture(context) or has_joinery(context) or context.family in {"vase", "pen_holder", "card_holder", "pegboard"}
        reason = (
            "Thickness, edge margin, and remaining material are critical in this case."
            if needed
            else "Material-retention risk is not a standout concern for this object."
        )
        return needed, reason
    if secondary_zh == "制造鲁棒性":
        needed = has_parametric_ranges(context)
        reason = (
            "The script exposes parameter ranges, so robustness across the allowed range should be checked."
            if needed
            else "No explicit parameter range is defined, so robustness is not a standalone rubric item."
        )
        return needed, reason
    return False, "No rule matched."


def english_title(context: CaseContext, secondary_zh: str) -> str:
    furniture_titles = {
        "功能适配": "Dimensional Fit",
        "使用稳定": "Structural Stability",
        "使用强度": "Structural Strength",
        "部件拆分合理性": "Assembly Equivalence (Component Split)",
        "部件关系正确性": "Joint Relationship Correctness",
        "功能结构完整性": "Functional Structure Integrity",
        "工艺适配": "Manufacturability",
        "局部特征可实现": "Local Feature Realizability",
        "材料与厚度合理": "Material and Thickness Rationality",
        "制造鲁棒性": "Parameter Robustness",
        "使用安全": "Contact Safety",
    }
    if context.family in {"stool", "chair", "table", "rack"}:
        return furniture_titles[secondary_zh]
    fallback = {
        "功能适配": "Functional Fit",
        "使用稳定": "Placement Stability",
        "使用安全": "Usage Safety",
        "使用强度": "Usage Strength",
        "部件拆分合理性": "Component Split Rationality",
        "部件关系正确性": "Component Relationship Correctness",
        "功能结构完整性": "Functional Structure Integrity",
        "工艺适配": "Process Fit",
        "局部特征可实现": "Local Feature Realizability",
        "材料与厚度合理": "Material and Thickness Rationality",
        "制造鲁棒性": "Manufacturing Robustness",
    }
    return fallback[secondary_zh]


def chinese_title(context: CaseContext, secondary_zh: str) -> str:
    furniture_titles = {
        "功能适配": "尺寸适配",
        "使用稳定": "结构稳定性",
        "使用强度": "使用强度",
        "部件拆分合理性": "装配等价（部件拆分）",
        "部件关系正确性": "连接关系正确性",
        "功能结构完整性": "功能结构完整性",
        "工艺适配": "可制造性",
        "局部特征可实现": "局部特征可实现性",
        "材料与厚度合理": "材料与厚度合理性",
        "制造鲁棒性": "参数鲁棒性",
        "使用安全": "接触安全性",
    }
    if context.family in {"stool", "chair", "table", "rack"}:
        return furniture_titles[secondary_zh]
    return secondary_zh


def parameter_summary(context: CaseContext) -> str:
    if not context.param_ranges:
        return "No explicit parameter ranges are declared in the script."
    parts = []
    for name, value in context.param_ranges.items():
        if isinstance(value, tuple | list) and len(value) == 2:
            parts.append(f"{name} {value[0]}~{value[1]} mm")
    return "; ".join(parts[:8]) if parts else "Parameter ranges are declared in the script."


def evidence_lines(context: CaseContext) -> list[str]:
    view_box = context.svg_meta.get("view_box", "")
    return [
        f"Component names in code: {', '.join(context.component_names) if context.component_names else 'not explicitly declared'}.",
        f"Planned component count: {context.component_count_from_plan}.",
        f"Detected CAD operations: {', '.join(context.operations) if context.operations else 'none detected'}.",
        f"SVG evidence: viewBox={view_box or 'n/a'}, path_count={context.svg_meta.get('path_count', 0)}, rect_count={context.svg_meta.get('rect_count', 0)}, circle_count={context.svg_meta.get('circle_count', 0)}.",
        parameter_summary(context),
    ]


def english_object_label(context: CaseContext) -> str:
    label = context.object_type.strip() or context.case_name
    if any("\u4e00" <= ch <= "\u9fff" for ch in label):
        family_map = {
            "stool": "stool",
            "chair": "chair",
            "table": "table",
            "vase": "vase",
            "pen_holder": "pen holder",
            "card_holder": "business card holder",
            "pegboard": "pegboard",
            "rack": "rack",
        }
        return family_map.get(context.family, context.case_name.replace("_", " "))
    return label


def english_requirements(context: CaseContext, secondary_zh: str) -> list[str]:
    label = english_object_label(context)
    component_count = max(len(context.component_names), context.component_count_from_plan)
    if context.family == "stool":
        mapping = {
            "功能适配": [
                f"Assess whether the seat span, seat height, and overall footprint remain plausible for a single-user {label}, rather than drifting toward a side table, step block, or unusable miniature.",
                "Check whether the seat panel still reads as the main sitting surface with enough usable area and a believable seating proportion.",
                "Verify that the overall outer dimensions still match the task and plan intent expressed by the current reference model and SVG projections.",
            ],
            "使用稳定": [
                "Assess whether the four-leg support path is clear from the seat panel down to the floor contact points with no visibly unstable stance.",
                "Check whether the leg placement still creates a stable support polygon instead of clustering too close to the seat center.",
                "Judge whether the seat thickness, leg thickness, and overall height remain balanced enough to avoid a top-heavy or wobbly reading.",
            ],
            "使用强度": [
                "Assess whether the seat panel retains enough material around the socket cuts so it would not crack easily under normal sitting load.",
                "Check whether the leg cross-sections and top tenons remain thick enough to carry load without becoming implausibly fragile.",
                "Judge whether the remaining material around rounded corners, sockets, and tenons still supports repeated use as a stool.",
            ],
            "部件拆分合理性": [
                f"Based on plan.md, the model should remain split into {component_count} independent solid bodies: one seat panel and four independent legs.",
                "Check that the seat panel remains the only receiving part with socket cuts and that each leg remains its own separate body.",
                "Fail this item if the legs are merged together, fused into the seat, or the part count no longer matches the plan.",
            ],
            "部件关系正确性": [
                "Assess whether each leg stays in its intended corner relationship beneath the seat rather than shifting, rotating, or misaligning relative to the socket layout.",
                "Check whether the tenons on the legs still correspond to the seat-panel sockets with sensible contact instead of floating gaps or hard interpenetration.",
                "Judge whether the full assembly still preserves the original stool joinery logic expressed by the CAD script.",
            ],
            "功能结构完整性": [
                "Assess whether the stool still contains the complete functional structure of one seat panel plus four supporting legs.",
                "Check whether any support leg, socket interface, or essential load path is missing, interrupted, or geometrically broken.",
                "Judge whether the final composition still reads as a complete stool rather than a partial frame or decorative abstraction.",
            ],
            "工艺适配": [
                "Assess whether the design remains suitable for the stated woodworking assembly process rather than only existing as a visual CAD shape.",
                "Check whether the use of seat panel, leg solids, tenons, and sockets matches a believable cut-and-assemble fabrication logic.",
                "Judge whether the current part organization and feature style are still appropriate for practical fabrication in wood.",
            ],
            "局部特征可实现": [
                "Assess whether local features such as corner fillets, top tenons, and seat-panel sockets remain clean, separate, and machineable.",
                "Check whether any local cut becomes too thin, overlaps another feature, or breaks through an exterior face in an unrealistic way.",
                "Judge whether the joint interfaces remain detailed enough to build, but not so delicate that they become geometrically implausible.",
            ],
            "材料与厚度合理": [
                "Assess whether the seat thickness, leg thickness, tenon size, and socket size leave enough surrounding material for a durable wooden assembly.",
                "Check whether the edge margins around the socket cuts remain sufficient instead of leaving dangerously thin walls or corners.",
                "Judge whether the current thickness choices still balance manufacturability and structural credibility for a practical stool.",
            ],
            "制造鲁棒性": [
                "Assess whether the model is likely to remain valid when parameters move across the declared ranges in the script.",
                "Check whether extreme combinations of seat size, leg size, tenon size, and socket depth would still preserve non-overlapping, buildable geometry.",
                "Judge whether the joinery logic appears robust, rather than only working at one narrow default parameter setting.",
            ],
        }
        return mapping[secondary_zh]
    common = {
        "功能适配": [
            f"Assess whether the generated design still behaves like the intended {label} rather than only matching a vague silhouette.",
            "Check whether the main usable opening, support zone, or interaction area remains appropriate for the object type.",
            "Judge whether the overall dimensions and proportions remain aligned with the task and plan intent.",
        ],
        "使用稳定": [
            "Assess whether the object still stands or sits stably in its intended use condition.",
            "Check whether the support footprint and center-of-mass logic remain plausible.",
            "Judge whether the proportions create a secure rather than precarious reading.",
        ],
        "使用安全": [
            "Assess whether the main hand-contact or body-contact regions avoid obviously hazardous sharpness or exposure.",
            "Check whether use-facing edges and tips remain controlled and appropriate for the object type.",
            "Judge whether the design introduces avoidable contact risks during normal use.",
        ],
        "使用强度": [
            "Assess whether key load-bearing or frequently handled regions retain enough material for normal use.",
            "Check whether thin sections, perforations, or openings leave overly fragile local geometry.",
            "Judge whether the object still appears durable enough for routine use.",
        ],
        "部件拆分合理性": [
            f"Assess whether the design still preserves the intended split into {component_count} independent parts.",
            "Check whether the decomposition boundaries still match the intended functional pieces.",
            "Judge whether any unreasonable fusion or missing part breaks the original assembly logic.",
        ],
        "部件关系正确性": [
            "Assess whether the relative placement, orientation, and spacing among parts remain correct.",
            "Check whether mating or alignment logic is still preserved instead of drifting or colliding.",
            "Judge whether the assembly still matches the plan and code structure.",
        ],
        "功能结构完整性": [
            "Assess whether the key functional substructure remains complete and continuous.",
            "Check whether any critical cavity, grid, tooth set, or support path has been broken.",
            "Judge whether the object still preserves its defining internal organization.",
        ],
        "工艺适配": [
            "Assess whether the design still matches the intended fabrication process.",
            "Check whether its overall feature language remains believable for the chosen manufacturing route.",
            "Judge whether the object is still practical to fabricate rather than merely renderable.",
        ],
        "局部特征可实现": [
            "Assess whether the local cuts, walls, corners, and joints remain realizable.",
            "Check whether any detail has become too thin, too deep, or self-conflicting.",
            "Judge whether the local feature scale still supports a real build outcome.",
        ],
        "材料与厚度合理": [
            "Assess whether thickness and edge margin remain reasonable throughout the object.",
            "Check whether removed material leaves enough residual strength and fabrication allowance.",
            "Judge whether thickness choices remain consistent with the object type and process.",
        ],
        "制造鲁棒性": [
            "Assess whether the design intent appears stable across the declared parameter ranges.",
            "Check whether feature relationships would likely survive extreme parameter combinations.",
            "Judge whether the model is robust rather than narrowly tuned to one default state.",
        ],
    }
    return common[secondary_zh]


def chinese_requirements(context: CaseContext, secondary_zh: str) -> list[str]:
    object_type = context.object_type
    component_count = max(len(context.component_names), context.component_count_from_plan)
    if context.family == "stool":
        mapping = {
            "功能适配": [
                f"检查该 {object_type} 的坐面尺寸、坐高和整体占地是否仍然符合单人坐具的基本尺度，而不是漂移成边几、脚踏块或不可用的小比例模型。",
                "检查凳面是否仍然明确作为主要受坐平面，且具备合理的可用面积与坐具比例。",
                "检查整体外廓尺寸是否仍与 task、plan 和当前参考 SVG 所表达的设计意图一致。",
            ],
            "使用稳定": [
                "检查四条凳腿到地面的支撑路径是否清晰完整，不应出现明显失衡、偏位或落地不稳的状态。",
                "检查四腿是否仍在凳面下形成稳定支撑面，而不是过度向中心收缩导致容易侧翻。",
                "检查凳面厚度、腿截面和整体高度是否协调，不应显得头重脚轻或明显易晃。",
            ],
            "使用强度": [
                "检查凳面板在开出四个榫窝后，周边剩余材料是否仍足以承受正常坐压而不显得脆弱。",
                "检查凳腿截面与顶部榫头是否仍保有足够厚度，不应细到接近易断的状态。",
                "检查圆角、榫窝和榫头附近的局部保料是否仍支持反复使用场景下的结构可信度。",
            ],
            "部件拆分合理性": [
                f"根据 plan.md，模型应保持为 {component_count} 个独立实体，即 1 个凳面板和 4 个独立凳腿。",
                "检查凳面板是否仍是唯一带榫窝的接收件，且每条凳腿都保持为单独零件。",
                "如果凳腿彼此熔成一体、与凳面并体，或部件数量不再匹配计划，应判为失败。",
            ],
            "部件关系正确性": [
                "检查每条凳腿是否仍位于凳面对应角部的正确装配位置，而不是发生明显平移、旋转或错位。",
                "检查凳腿顶部榫头与凳面榫窝是否仍保持对应关系，不应出现明显悬空、穿透或尺寸逻辑失配。",
                "检查整体装配是否仍保持 CAD 脚本表达的凳面-凳腿连接逻辑。",
            ],
            "功能结构完整性": [
                "检查最终模型是否仍完整包含 1 个凳面和 4 条支撑腿这一核心功能结构。",
                "检查是否有任一凳腿、榫窝界面或关键受力路径缺失、断裂或被破坏。",
                "检查整体是否仍明确读作完整方凳，而不是残缺框架或仅保留造型轮廓的对象。",
            ],
            "工艺适配": [
                "检查该设计是否仍符合任务中“木工装配”的工艺方向，而不是只在 CAD 中视觉上成立。",
                "检查凳面板、凳腿、榫头与榫窝的组织方式是否仍符合可切削、可装配的木作逻辑。",
                "检查整体零件组织和特征表达是否仍适合真实木工制作。",
            ],
            "局部特征可实现": [
                "检查圆角、榫头、榫窝等局部特征是否仍然清晰、独立且具备现实加工可行性。",
                "检查是否出现局部切口过薄、特征互相重叠或直接切穿外表面的不合理情况。",
                "检查连接界面是否既足够明确可做，又没有细弱到接近几何失真的程度。",
            ],
            "材料与厚度合理": [
                "检查凳面厚度、腿厚、榫头尺寸和榫窝尺寸组合后，周围是否仍保留足够木料用于可靠装配。",
                "检查榫窝距边缘的余量是否充足，不应留下危险的薄边、薄角或易裂区域。",
                "检查当前厚度设置是否同时满足制造可行性和坐具结构可信度。",
            ],
            "制造鲁棒性": [
                "检查当脚本参数在声明范围内变化时，该模型是否仍有较大概率保持合法且可制造的几何。",
                "检查凳面尺寸、腿尺寸、榫头尺寸和榫窝深度在极端组合下，是否仍避免重叠、穿边或不可装配。",
                "检查该装配逻辑是否具有参数鲁棒性，而不是只在默认值附近偶然成立。",
            ],
        }
        return mapping[secondary_zh]
    common = {
        "功能适配": [
            f"检查该 {object_type} 是否仍然满足目标用途，而不是只保留外观轮廓。",
            "检查关键可用区域、容纳区域或接触区域是否仍适合该对象类型。",
            "检查整体尺寸与比例是否仍与任务和计划一致。",
        ],
        "使用稳定": [
            "检查对象在正常使用或放置状态下是否仍稳定。",
            "检查支撑面与重心逻辑是否合理。",
            "检查整体比例是否会导致明显倾倒风险。",
        ],
        "使用安全": [
            "检查主要接触区域是否存在明显危险的尖锐边缘或暴露特征。",
            "检查与手或身体接触的边缘、尖端是否仍符合对象类型的安全预期。",
            "检查设计是否引入可避免的正常使用风险。",
        ],
        "使用强度": [
            "检查关键受力或高频使用区域是否仍保留足够材料。",
            "检查薄壁、开孔或细长特征是否导致局部过脆。",
            "检查对象是否仍具备日常使用强度。",
        ],
        "部件拆分合理性": [
            f"检查设计是否仍保持预期的 {component_count} 个独立部件。",
            "检查拆分边界是否仍对应合理的功能件划分。",
            "检查是否存在不合理并体或核心部件缺失。",
        ],
        "部件关系正确性": [
            "检查部件之间的相对位置、朝向和间距是否仍正确。",
            "检查配合与对位逻辑是否仍被保留，而不是出现错位或碰撞。",
            "检查整体装配是否仍与计划和代码结构一致。",
        ],
        "功能结构完整性": [
            "检查关键功能子结构是否仍完整、连续。",
            "检查核心内腔、孔阵列、齿列或支撑路径是否被破坏。",
            "检查对象是否仍保留其定义性的内部组织方式。",
        ],
        "工艺适配": [
            "检查整体设计是否仍符合目标制造工艺。",
            "检查特征表达方式是否仍适合该制造路线。",
            "检查对象是否仍是可落地制造的设计，而不只是可渲染形体。",
        ],
        "局部特征可实现": [
            "检查局部切口、薄壁、圆角、连接位等特征是否仍可实现。",
            "检查是否出现局部过薄、过深或互相冲突的几何。",
            "检查局部特征尺度是否仍支持真实制造。",
        ],
        "材料与厚度合理": [
            "检查整体厚度与边距是否仍合理。",
            "检查切除材料后是否仍保留足够余量与结构能力。",
            "检查厚度选择是否与对象和工艺相匹配。",
        ],
        "制造鲁棒性": [
            "检查设计意图在参数变化下是否仍稳定成立。",
            "检查特征关系在极端参数组合下是否仍能维持。",
            "检查模型是否具有足够的参数鲁棒性。",
        ],
    }
    return common[secondary_zh]


def english_objective(context: CaseContext, secondary_zh: str, title: str) -> str:
    label = english_object_label(context)
    if context.family == "stool" and secondary_zh == "功能适配":
        return (
            f"Assess whether the seat size, seat height, footprint, and overall proportion conform to the intended ergonomic and visual logic of a single-user {label}, "
            "while remaining consistent with the task, plan, SVG, and parametric CAD script."
        )
    if context.family == "stool" and secondary_zh == "使用稳定":
        return (
            "Assess whether the four-leg support path, ground contact logic, and overall center-of-mass distribution remain stable and believable for normal sitting use."
        )
    if context.family == "stool" and secondary_zh == "使用强度":
        return (
            "Assess whether the seat panel, leg sections, and joint regions retain enough remaining material to withstand repeated normal sitting load without becoming implausibly fragile."
        )
    if context.family == "stool" and secondary_zh == "部件拆分合理性":
        return (
            f"Based on plan.md, assess whether the model is correctly split into {max(len(context.component_names), context.component_count_from_plan)} independent solids "
            "with boundaries matching the intended seat-panel and four-leg decomposition."
        )
    if context.family == "stool" and secondary_zh == "部件关系正确性":
        return (
            "Assess whether the leg tenons and seat sockets preserve the intended mating relationship, relative placement, and non-interpenetrating assembly logic."
        )
    if context.family == "stool" and secondary_zh == "功能结构完整性":
        return (
            "Assess whether the complete stool support system remains intact, including one usable seat panel, four effective support legs, and uninterrupted seat-to-floor load transfer."
        )
    if context.family == "stool" and secondary_zh == "工艺适配":
        return (
            "Assess whether the current geometry still follows a believable woodworking assembly process, with features and part organization that could realistically be cut, fitted, and assembled."
        )
    if context.family == "stool" and secondary_zh == "局部特征可实现":
        return (
            "Assess whether local woodworking features such as fillets, sockets, tenons, and edge margins remain clean, separable, and realistically manufacturable."
        )
    if context.family == "stool" and secondary_zh == "材料与厚度合理":
        return (
            "In the context of woodworking assembly, assess whether panel thickness, leg thickness, tenon size, and socket edge margins leave enough material for a durable real-world build."
        )
    if context.family == "stool" and secondary_zh == "制造鲁棒性":
        return (
            "Assess whether the model is likely to keep generating legal, non-colliding, and structurally believable geometry across the declared parameter ranges."
        )
    return (
        f"Assess whether this {label} satisfies the rubric item '{title}' according to the combined evidence in task.md, plan.md, the CAD script, and the SVG projection."
    )


def chinese_objective(context: CaseContext, secondary_zh: str, title: str) -> str:
    if context.family == "stool" and secondary_zh == "功能适配":
        return "评估该凳子的坐面尺寸、坐高、占地和整体比例，是否仍符合单人坐具的基本人体工学与外观逻辑，并与 task、plan、SVG 和参数化脚本保持一致。"
    if context.family == "stool" and secondary_zh == "使用稳定":
        return "评估该凳子的四腿支撑路径、落地逻辑和整体重心分布，是否足以支持正常坐用时的稳定性。"
    if context.family == "stool" and secondary_zh == "使用强度":
        return "评估凳面板、腿部截面和连接区域是否仍保留足够材料，以承受反复正常坐压而不过度脆弱。"
    if context.family == "stool" and secondary_zh == "部件拆分合理性":
        return f"基于 plan.md，评估模型是否仍被正确拆分为 {max(len(context.component_names), context.component_count_from_plan)} 个独立实体，并保持凳面与四腿的预期拆分边界。"
    if context.family == "stool" and secondary_zh == "部件关系正确性":
        return "评估凳腿榫头与凳面榫窝是否仍保持正确的配合关系、相对位置和无不合理穿透的装配逻辑。"
    if context.family == "stool" and secondary_zh == "功能结构完整性":
        return "评估该凳子的完整支撑系统是否仍然成立，包括 1 个可用凳面、4 条有效支撑腿，以及从凳面到地面的连续受力路径。"
    if context.family == "stool" and secondary_zh == "工艺适配":
        return "评估当前几何是否仍符合可信的木工装配工艺，包括零件组织、特征表达和可切削可装配性。"
    if context.family == "stool" and secondary_zh == "局部特征可实现":
        return "评估圆角、榫窝、榫头和边距等局部木作特征，是否仍清晰、互不冲突且具备现实制造可行性。"
    if context.family == "stool" and secondary_zh == "材料与厚度合理":
        return "在木工装配场景下，评估凳面厚度、腿厚、榫头尺寸和榫窝边距是否仍保留足够材料，用于真实可靠的实体制作。"
    if context.family == "stool" and secondary_zh == "制造鲁棒性":
        return "评估该模型在脚本声明的参数范围内变化时，是否仍能稳定生成合法、不碰撞且结构可信的几何。"
    return f"评估该{context.object_type}在“{title}”这一项上，是否符合 task.md、plan.md、CAD 脚本和 SVG 投影共同表达的设计意图。"


def english_grading_descriptions(context: CaseContext, secondary_zh: str) -> dict[str, str]:
    requirements = english_requirements(context, secondary_zh)
    label = english_object_label(context)
    if secondary_zh == "功能适配" and context.family == "stool":
        return {
            "5 Points (Excellent)": f"The bounding-box scale, seat height, seat span, and overall proportion clearly read as a practical single-user {label}. The design fully preserves the intended stool use and does not drift toward another furniture type.",
            "3 Points (Adequate)": "The model is still recognizable and mostly usable as a stool, but one or more dimensional relationships are somewhat off, such as a seat that is too narrow, too high, or proportionally unbalanced relative to the legs.",
            "1 Point (Fail)": "The dimensions or proportions deviate so severely that the object no longer functions credibly as a stool, even if it still resembles one visually.",
        }
    if secondary_zh == "使用稳定" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The four legs are placed in a stable pattern, the load path from seat to floor is clear, and the whole stool reads as firmly grounded with no obvious tipping risk.",
            "3 Points (Adequate)": "The stool still has a workable support layout, but the stance, leg placement, or proportion introduces some visible wobble or balance risk under use.",
            "1 Point (Fail)": "The support logic is clearly broken, such as missing support, heavily offset legs, or a geometry that would not stand or bear load reliably.",
        }
    if secondary_zh == "使用强度" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The seat panel retains solid material around the sockets, the legs remain adequately thick, and the tenon regions read as durable enough for repeated normal use.",
            "3 Points (Adequate)": "The stool is still plausible, but one or more local regions look thinner or weaker than ideal, creating a moderate risk of cracking or long-term weakness.",
            "1 Point (Fail)": "Key load-bearing regions are obviously too thin, too deeply cut, or too fragile to support credible repeated use as a stool.",
        }
    if secondary_zh == "部件拆分合理性" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The model is correctly split into one seat panel and four separate legs, with clean boundaries that match the intended assembly plan exactly.",
            "3 Points (Adequate)": "The overall part count is close or correct, but the decomposition boundaries are not fully clean or logically assigned to the intended functional parts.",
            "1 Point (Fail)": "The part split is fundamentally wrong, such as fused components, missing parts, or a decomposition that no longer matches the plan.",
        }
    if secondary_zh == "部件关系正确性" and context.family == "stool":
        return {
            "5 Points (Excellent)": "Each leg remains correctly aligned to its corresponding seat socket, with believable joinery placement and no obvious hard-body penetration or floating gaps.",
            "3 Points (Adequate)": "The assembly is mostly preserved, but some leg-to-seat interfaces are slightly misaligned, loose, or geometrically inconsistent.",
            "1 Point (Fail)": "The assembly relationship is clearly wrong, with major misplacement, penetration, floating parts, or lost joinery correspondence.",
        }
    if secondary_zh == "功能结构完整性" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The stool keeps its complete functional system: one usable seat, four effective supports, and a continuous load-bearing logic from top to floor.",
            "3 Points (Adequate)": "The main stool structure is still present, but one or more supporting or joint-related substructures appear weakened, incomplete, or not fully convincing.",
            "1 Point (Fail)": "A core functional substructure is missing or broken, so the design no longer reads as a complete, working stool system.",
        }
    if secondary_zh == "工艺适配" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The geometry, part organization, and joinery all align well with a realistic woodworking assembly workflow.",
            "3 Points (Adequate)": "The stool is still broadly manufacturable, but some feature choices feel awkward, overcomplicated, or weakly matched to the stated woodworking process.",
            "1 Point (Fail)": "The design clearly conflicts with the intended woodworking process, even if the shape remains visually recognizable.",
        }
    if secondary_zh == "局部特征可实现" and context.family == "stool":
        return {
            "5 Points (Excellent)": "Local features such as sockets, tenons, fillets, and edge margins are all cleanly resolved and appear realistic to fabricate.",
            "3 Points (Adequate)": "Most local features are workable, but one or more areas show tight clearances, thin remnants, or awkward local geometry.",
            "1 Point (Fail)": "Local features are clearly unrealistic, self-conflicting, or too delicate to be manufactured credibly.",
        }
    if secondary_zh == "材料与厚度合理" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The seat, legs, tenons, and sockets all retain sensible thickness and edge margin for a realistic wood assembly with no obvious local weakness.",
            "3 Points (Adequate)": "The design is still manufacturable, but one or more local areas look thin, fragile, or uncomfortably close to breaking through.",
            "1 Point (Fail)": "Material retention is clearly unreasonable, with dangerously thin remnants, impossible cuts, or geometry that would be physically unreliable to build.",
        }
    if secondary_zh == "制造鲁棒性" and context.family == "stool":
        return {
            "5 Points (Excellent)": "The declared parameters appear to support a wide, stable design space without obvious collisions, broken joinery, or geometric collapse.",
            "3 Points (Adequate)": "The default model is acceptable, but the parameter logic appears narrow enough that some extreme combinations would likely cause local failures.",
            "1 Point (Fail)": "The parameterization appears fragile, with a high chance of generating invalid, colliding, or functionally broken geometry when values move away from the default.",
        }
    title = english_title(context, secondary_zh)
    return {
        "5 Points (Excellent)": f"The {title} intent is clearly satisfied and remains fully consistent with the current {label} design.",
        "3 Points (Adequate)": f"The {title} intent is mostly preserved, but there is some visible drift in proportion, structure, or fabrication logic.",
        "1 Point (Fail)": f"The {title} intent is clearly broken, making the current design no longer credible for this rubric item.",
    }


def chinese_grading_descriptions(context: CaseContext, secondary_zh: str) -> dict[str, str]:
    if secondary_zh == "功能适配" and context.family == "stool":
        return {
            "5分（优秀）": "包围盒尺度、坐高、坐面跨度和整体比例都清晰符合单人方凳的使用逻辑，既可识别也可信用，没有漂移成其他家具类型。",
            "3分（合格）": "整体仍可识别并基本可用，但部分尺寸关系略失衡，例如坐面偏窄、偏高，或腿与凳面比例不够协调。",
            "1分（失败）": "尺寸或比例严重偏离，导致该对象虽然可能还像凳子，但已不再具备可信的方凳功能。",
        }
    if secondary_zh == "使用稳定" and context.family == "stool":
        return {
            "5分（优秀）": "四腿布置稳定，凳面到地面的受力路径清晰，整体落地可信，没有明显侧翻或晃动风险。",
            "3分（合格）": "仍具备可用支撑逻辑，但腿位、支撑面或整体比例带来一定晃动或平衡风险。",
            "1分（失败）": "支撑逻辑明显失效，例如缺失支撑、腿部严重偏位，或整体几何根本无法稳定站立与承重。",
        }
    if secondary_zh == "使用强度" and context.family == "stool":
        return {
            "5分（优秀）": "凳面榫窝周围保料充足，凳腿和榫头厚度合理，整体具备承受反复正常坐用的可信强度。",
            "3分（合格）": "整体仍基本可信，但局部区域偏薄或偏弱，存在一定开裂或长期受力疲劳风险。",
            "1分（失败）": "关键受力区域明显过薄、切削过深或过于脆弱，已不具备可信的重复使用强度。",
        }
    if secondary_zh == "部件拆分合理性" and context.family == "stool":
        return {
            "5分（优秀）": "模型被正确拆分为 1 个凳面和 4 条独立凳腿，边界清晰，完全符合预期装配方案。",
            "3分（合格）": "部件数量接近或正确，但拆分边界不够干净，或部分功能件划分逻辑不够理想。",
            "1分（失败）": "拆分逻辑根本错误，例如并体、缺件，或已不再匹配计划中的装配方式。",
        }
    if secondary_zh == "部件关系正确性" and context.family == "stool":
        return {
            "5分（优秀）": "每条凳腿都与对应榫窝正确对位，连接位置可信，不存在明显硬穿透或悬空间隙。",
            "3分（合格）": "整体装配大致保留，但个别腿与凳面的接口存在轻微错位、偏松或几何逻辑不够严谨的问题。",
            "1分（失败）": "装配关系明显错误，存在严重错位、穿透、悬空，或榫头与榫窝完全失去对应关系。",
        }
    if secondary_zh == "功能结构完整性" and context.family == "stool":
        return {
            "5分（优秀）": "凳子的完整功能系统被保留，包括 1 个可用凳面、4 条有效支撑腿以及连续的承重逻辑。",
            "3分（合格）": "主要结构仍在，但部分支撑或连接相关子结构显得偏弱、不完整，可信度有所下降。",
            "1分（失败）": "核心功能子结构缺失或断裂，模型已不再像一个完整可用的方凳系统。",
        }
    if secondary_zh == "工艺适配" and context.family == "stool":
        return {
            "5分（优秀）": "整体几何、零件组织和连接方式都与真实木工装配流程高度匹配。",
            "3分（合格）": "整体仍可制造，但某些特征选择与木工装配工艺的匹配度一般，显得略别扭或略脆弱。",
            "1分（失败）": "设计与目标木工装配工艺明显冲突，即使外形还可识别，也缺乏真实制作可行性。",
        }
    if secondary_zh == "局部特征可实现" and context.family == "stool":
        return {
            "5分（优秀）": "榫窝、榫头、圆角和边距等局部特征都清晰、互不冲突，且具备现实加工可行性。",
            "3分（合格）": "大部分局部特征仍可实现，但个别区域存在过紧、过薄或局部几何不顺的问题。",
            "1分（失败）": "局部特征明显不现实、互相冲突，或细弱到难以可信制造。",
        }
    if secondary_zh == "材料与厚度合理" and context.family == "stool":
        return {
            "5分（优秀）": "凳面、凳腿、榫头和榫窝都保有合理厚度与边距，适合真实木作装配，没有明显薄弱区域。",
            "3分（合格）": "整体仍可制造，但局部区域偏薄、偏脆，或某些切口距离边缘过近，存在一定风险。",
            "1分（失败）": "保料明显不合理，出现危险薄边、不可实现切口，或制作后在实体上难以可靠存在。",
        }
    if secondary_zh == "制造鲁棒性" and context.family == "stool":
        return {
            "5分（优秀）": "参数空间整体稳定，较宽范围内都不易出现碰撞、装配断裂或几何塌陷。",
            "3分（合格）": "默认参数下基本可行，但参数逻辑偏窄，某些极端组合下很可能产生局部失效。",
            "1分（失败）": "参数化明显脆弱，参数稍有偏离默认值就容易生成非法、碰撞或功能失效的几何。",
        }
    title = chinese_title(context, secondary_zh)
    return {
        "5分（优秀）": f"“{title}”对应的设计意图被清晰满足，并与当前对象的任务要求保持一致。",
        "3分（合格）": f"“{title}”整体仍被保留，但在比例、结构或制造逻辑上存在一定漂移。",
        "1分（失败）": f"“{title}”对应的关键设计意图已经明显失效，当前模型不再可信。",
    }


def build_secondary_entry(context: CaseContext, taxonomy: SecondaryTaxonomy, lang: str) -> dict[str, Any]:
    applicable, reason = applicability_reason(context, taxonomy.secondary_zh)
    base: dict[str, Any] = {
        "secondary_category_zh": taxonomy.secondary_zh,
        "secondary_category_en": SECONDARY_ZH_TO_EN[taxonomy.secondary_zh],
        "applicable": applicable,
        "applicability_reason": reason,
        "taxonomy_definition": taxonomy.definition,
        "focus_points": taxonomy.focus_points,
    }
    if not applicable:
        return base
    if lang == "en":
        title = english_title(context, taxonomy.secondary_zh)
        base.update(
            {
                "rubric_title": title,
                "evaluation_objective": english_objective(context, taxonomy.secondary_zh, title),
                "grading_descriptions": english_grading_descriptions(context, taxonomy.secondary_zh),
                "specific_checks": english_requirements(context, taxonomy.secondary_zh),
            }
        )
    else:
        title = chinese_title(context, taxonomy.secondary_zh)
        base.update(
            {
                "rubric_title": title,
                "evaluation_objective": chinese_objective(context, taxonomy.secondary_zh, title),
                "grading_descriptions": chinese_grading_descriptions(context, taxonomy.secondary_zh),
                "specific_checks": chinese_requirements(context, taxonomy.secondary_zh),
            }
        )
    return base


def build_document(context: CaseContext, taxonomy_items: list[SecondaryTaxonomy], lang: str) -> dict[str, Any]:
    grouped: list[dict[str, Any]] = []
    for primary_zh in ("可实用", "可组装", "可建造"):
        secondaries = [item for item in taxonomy_items if item.primary_zh == primary_zh]
        grouped.append(
            {
                "primary_category_zh": primary_zh,
                "primary_category_en": PRIMARY_ZH_TO_EN[primary_zh],
                "secondary_categories": [build_secondary_entry(context, item, lang) for item in secondaries],
            }
        )
    return {
        "task_name": context.case_name,
        "language": "en" if lang == "en" else "zh",
        "source_bundle": {
            "task_md": str(context.case_dir / "task.md"),
            "plan_md": str(context.case_dir / "plan.md"),
            "cadquery_code": str(primary_py_path(context.case_dir) or ""),
            "svg": str(primary_svg_path(context.case_dir) or ""),
            "rubric_taxonomy_source": str(DEFAULT_TAXONOMY_PATH),
        },
        "design_context": {
            "object_type": english_object_label(context) if lang == "en" else context.object_type,
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
        "evidence_summary": evidence_lines(context),
        "generation_notes": [
            "Applicability is decided per secondary taxonomy before a rubric body is generated.",
            "Each applicable secondary category is written as a case-specific rubric derived from task.md, plan.md, CAD code, SVG evidence, and rubric.md taxonomy.",
        ]
        if lang == "en"
        else [
            "先对每个二级类目判断是否适用，再为适用项生成 rubric。",
            "每个适用的二级类目都基于 task.md、plan.md、CAD 脚本、SVG 证据和 rubric.md taxonomy 生成具体打分说明。",
        ],
    }


def output_paths(case_name: str, rubric_root: Path, rubric_zh_root: Path) -> tuple[Path, Path]:
    return rubric_root / f"{case_name}.json", rubric_zh_root / f"{case_name}_zh.json"


def iter_case_dirs(cleaned_root: Path, only: list[str] | None, limit: int | None) -> list[Path]:
    selected = [path for path in sorted(cleaned_root.iterdir()) if path.is_dir()]
    if only:
        wanted = set(only)
        selected = [path for path in selected if path.name in wanted]
    if limit is not None:
        selected = selected[:limit]
    return selected


def main() -> None:
    args = parse_args()
    args.rubric_root.mkdir(parents=True, exist_ok=True)
    args.rubric_zh_root.mkdir(parents=True, exist_ok=True)
    taxonomy_items = parse_taxonomy(args.taxonomy_path)
    for case_dir in iter_case_dirs(args.cleaned_root, args.only, args.limit):
        if not (case_dir / "task.md").exists() or not (case_dir / "plan.md").exists():
            continue
        context = load_case_context(case_dir)
        rubric_path, rubric_zh_path = output_paths(case_dir.name, args.rubric_root, args.rubric_zh_root)
        if not args.overwrite and (rubric_path.exists() or rubric_zh_path.exists()):
            continue
        rubric_doc = build_document(context, taxonomy_items, lang="en")
        rubric_zh_doc = build_document(context, taxonomy_items, lang="zh")
        rubric_path.write_text(json.dumps(rubric_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rubric_zh_path.write_text(json.dumps(rubric_zh_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"generated {rubric_path}")
        print(f"generated {rubric_zh_path}")


if __name__ == "__main__":
    main()
