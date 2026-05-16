from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeometryMetrics:
    code_valid: bool
    geometry_valid: bool
    watertight: bool | None        # NOTE: True iff no open edges AND no non-manifold edges (legacy combined)
    watertight_strict: bool | None # True iff no open edges (Watertightness errors); IGNORES manifold check
    manifold: bool | None          # True iff no non-manifold edges (every edge has ≤2 faces)
    self_intersection_free: bool | None
    normal_consistency: bool | None
    volume_valid: bool | None
    bbox_valid: bool | None
    occt_valid: bool | None
    watertight_error_count: int
    self_intersection_error_count: int
    non_manifold_error_count: int
    volume_error_count: int
    bbox_error_count: int
    issue_summary: str


VALIDATOR_TEMPLATE = r"""
import json
import sys
sys.path.insert(0, {validator_root!r})
from validator import CadQueryValidator

validator = CadQueryValidator()
report = validator.get_detailed_report({code!r})
issues = []
for issue in report.geometry_issues:
    issues.append({{
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "description": issue.description,
    }})
print(json.dumps({{
    "code_valid": report.code_valid,
    "geometry_valid": report.geometry_valid,
    "issues": issues,
}}))
"""


def evaluate_geometry(code: str, validator_root: Path, python_executable: Path, timeout_seconds: int = 120) -> GeometryMetrics:
    def _resolve_validator_root(root: Path) -> Path:
        candidates = [
            root,
            root / "validator",
            root.parent / "validator",
            root.parent / "validator" / "validator",
        ]
        for candidate in candidates:
            candidate = candidate.resolve()
            if not candidate.exists() or not candidate.is_dir():
                continue
            module_path = candidate / "validator.py"
            package_path = candidate / "__init__.py"
            if module_path.exists() or package_path.exists():
                return candidate
        return root

    validator_root = _resolve_validator_root(validator_root)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(VALIDATOR_TEMPLATE.format(validator_root=str(validator_root), code=code))
        script_path = Path(handle.name)

    try:
        completed = subprocess.run(
            [str(python_executable), str(script_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            return GeometryMetrics(
                code_valid=False,
                geometry_valid=False,
                watertight=None,
                watertight_strict=None,
                manifold=None,
                self_intersection_free=None,
                normal_consistency=None,
                volume_valid=None,
                bbox_valid=None,
                occt_valid=None,
                watertight_error_count=0,
                self_intersection_error_count=0,
                non_manifold_error_count=0,
                volume_error_count=0,
                bbox_error_count=0,
                issue_summary=(completed.stderr or completed.stdout).strip(),
            )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        issues = payload.get("issues", [])

        def count(name: str) -> int:
            return sum(1 for issue in issues if issue["issue_type"] == name and issue["severity"] == "error")

        def no_error(*names: str) -> bool | None:
            if not bool(payload["code_valid"]):
                return None
            related = [issue for issue in issues if issue["issue_type"] in names]
            if not related:
                return True
            return not any(issue["severity"] == "error" for issue in related)

        return GeometryMetrics(
            code_valid=bool(payload["code_valid"]),
            geometry_valid=bool(payload["geometry_valid"]),
            watertight=no_error("Watertightness", "NonManifoldEdge"),
            watertight_strict=no_error("Watertightness"),
            manifold=no_error("NonManifoldEdge"),
            self_intersection_free=no_error("SelfIntersection"),
            normal_consistency=no_error("NormalConsistency"),
            volume_valid=no_error("ZeroVolume", "NegativeVolume"),
            bbox_valid=no_error("DegenerateBBox", "InfiniteBBox", "BoundingBox"),
            occt_valid=no_error("OCCTValidity"),
            watertight_error_count=count("Watertightness"),
            self_intersection_error_count=count("SelfIntersection"),
            non_manifold_error_count=count("NonManifoldEdge"),
            volume_error_count=count("ZeroVolume") + count("NegativeVolume"),
            bbox_error_count=count("BoundingBox"),
            issue_summary=" | ".join(
                f"[{issue['severity'].upper()}] {issue['issue_type']}: {issue['description']}"
                for issue in issues
            )[:1000],
        )
    except subprocess.TimeoutExpired:
        return GeometryMetrics(
            code_valid=False,
            geometry_valid=False,
            watertight=None,
            watertight_strict=None,
            manifold=None,
            self_intersection_free=None,
            normal_consistency=None,
            volume_valid=None,
            bbox_valid=None,
            occt_valid=None,
            watertight_error_count=0,
            self_intersection_error_count=0,
            non_manifold_error_count=0,
            volume_error_count=0,
            bbox_error_count=0,
            issue_summary=f"Geometry validator timed out after {timeout_seconds}s",
        )
    finally:
        script_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Interpenetration ("穿模") check.
# Loads a STEP file via cadquery, decomposes into solids, and pairwise tests
# whether any two solids overlap in volume by more than `rel_threshold` of the
# smaller solid's volume. Single-solid models are reported as "free" (nothing
# to interpenetrate). Runs in a subprocess so a malformed STEP can't crash the
# parent.
INTERPEN_TEMPLATE = r"""
import json, sys, traceback

step_path = sys.argv[1]
rel_threshold = float(sys.argv[2])

try:
    import cadquery as cq
    shape = cq.importers.importStep(step_path)
    solids = list(shape.solids().vals())
    n = len(solids)

    if n <= 1:
        print(json.dumps({
            "interpenetration_free": True,
            "n_solids": n,
            "max_overlap_ratio": 0.0,
            "interpenetrating_pairs": 0,
            "pairs_checked": 0,
            "note": "single-solid model (no interpenetration possible)",
        }))
        sys.exit(0)

    bbs, vols = [], []
    for s in solids:
        bb = s.BoundingBox()
        bbs.append((bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax))
        try:
            vols.append(abs(s.Volume()))
        except Exception:
            vols.append(0.0)

    EPS = 1e-6
    def bbox_overlap(b1, b2):
        return (b1[0] < b2[3] - EPS and b2[0] < b1[3] - EPS and
                b1[1] < b2[4] - EPS and b2[1] < b1[4] - EPS and
                b1[2] < b2[5] - EPS and b2[2] < b1[5] - EPS)

    pairs_checked = 0
    pairs_flagged = 0
    max_ratio = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            if not bbox_overlap(bbs[i], bbs[j]):
                continue
            try:
                common = solids[i].intersect(solids[j])
                cv = abs(common.Volume()) if common is not None else 0.0
            except Exception:
                continue
            pairs_checked += 1
            denom = min(vols[i], vols[j]) or 1.0
            ratio = cv / denom
            if ratio > max_ratio:
                max_ratio = ratio
            if ratio > rel_threshold:
                pairs_flagged += 1

    print(json.dumps({
        "interpenetration_free": pairs_flagged == 0,
        "n_solids": n,
        "max_overlap_ratio": max_ratio,
        "interpenetrating_pairs": pairs_flagged,
        "pairs_checked": pairs_checked,
    }))
except Exception as e:
    print(json.dumps({
        "interpenetration_free": None,
        "error": "{}: {}".format(type(e).__name__, e),
        "traceback": traceback.format_exc(),
    }))
    sys.exit(1)
"""


def evaluate_interpenetration(
    step_path: Path,
    python_executable: Path,
    timeout_seconds: int = 120,
    rel_threshold: float = 0.01,
) -> dict:
    """Detect interpenetration ('穿模') in a STEP file.

    Returns a dict with at least the keys:
      interpenetration_free  : bool | None  (None on parse / runtime failure)
      n_solids               : int
      max_overlap_ratio      : float        (overlap / volume of the smaller solid)
      interpenetrating_pairs : int          (#pairs above rel_threshold)
      pairs_checked          : int          (#pairs that actually ran a Boolean op)
      error / note           : str (optional)
    """
    if not step_path.exists():
        return {"interpenetration_free": None, "error": f"STEP missing: {step_path}"}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(INTERPEN_TEMPLATE)
        script_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [str(python_executable), str(script_path), str(step_path), str(rel_threshold)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0 and not completed.stdout.strip():
            return {"interpenetration_free": None, "error": (completed.stderr or "subprocess failed").strip()[:500]}
        last_line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        try:
            return json.loads(last_line)
        except Exception as e:
            return {"interpenetration_free": None, "error": f"parse: {e}: {last_line[:300]}"}
    except subprocess.TimeoutExpired:
        return {"interpenetration_free": None, "error": f"timed out after {timeout_seconds}s"}
    finally:
        script_path.unlink(missing_ok=True)
