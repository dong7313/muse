from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class SvgMetrics:
    view_labels: list[str]
    total_path_count: int
    estimated_component_count: int
    text_count: int
    width_mm: float
    height_mm: float


def _parse_dimension(value: Optional[str]) -> float:
    if not value:
        return 0.0
    cleaned = value.replace("mm", "").replace("px", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _path_bbox(path_d: str) -> Optional[Tuple[float, float, float, float]]:
    numbers = [float(item) for item in NUMBER_PATTERN.findall(path_d)]
    if len(numbers) < 4:
        return None
    xs = numbers[0::2]
    ys = numbers[1::2]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], tol: float = 0.5) -> bool:
    return not (
        a[2] < b[0] - tol
        or b[2] < a[0] - tol
        or a[3] < b[1] - tol
        or b[3] < a[1] - tol
    )


def _estimate_components(path_boxes: list[tuple[float, float, float, float]]) -> int:
    if not path_boxes:
        return 0
    groups: list[list[tuple[float, float, float, float]]] = []
    for box in path_boxes:
        matched = False
        for group in groups:
            if any(_boxes_overlap(box, existing) for existing in group):
                group.append(box)
                matched = True
                break
        if not matched:
            groups.append([box])
    return len(groups)


def analyze_svg(svg_path: Path) -> SvgMetrics:
    root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
    ns = {"svg": "http://www.w3.org/2000/svg"}
    texts = root.findall(".//svg:text", ns)
    labels = [("".join(text.itertext())).strip() for text in texts if ("".join(text.itertext())).strip() in {"Isometric", "Top", "Front", "Right"}]

    paths = root.findall(".//svg:path", ns)
    path_boxes = [box for box in (_path_bbox(path.get("d", "")) for path in paths) if box]
    component_count = _estimate_components(path_boxes)

    return SvgMetrics(
        view_labels=labels,
        total_path_count=len(paths),
        estimated_component_count=component_count,
        text_count=len(texts),
        width_mm=_parse_dimension(root.get("width")),
        height_mm=_parse_dimension(root.get("height")),
    )
