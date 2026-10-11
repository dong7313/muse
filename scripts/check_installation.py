"""Offline smoke test of the actual MUSE execution/geometry/drawing pipeline."""
from __future__ import annotations

import argparse
import json
import platform
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from judge_system.drawings import render_four_views, render_3d_preview
from judge_system.geometry_metrics import evaluate_geometry
from judge_system.reverse_pipeline import DEFAULT_DRAWCAD_ROOT, DEFAULT_VALIDATOR_ROOT
from judge_system.sandbox import execute_in_sandbox


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "out/installation-check")
    parser.add_argument("--validator-root", type=Path, default=DEFAULT_VALIDATOR_ROOT)
    parser.add_argument("--drawcad-root", type=Path, default=DEFAULT_DRAWCAD_ROOT)
    parser.add_argument("--render-3d", action="store_true", help="Also test VTK; requires a graphics-capable session")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    import cadquery
    import importlib.metadata

    def package_version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "unavailable (possibly installed outside pip)"

    code = 'import cadquery as cq\nresult = cq.Workplane("XY").box(10, 20, 30)\n'
    source = args.output / "box.py"
    source.write_text(code, encoding="utf-8")
    python = Path(sys.executable)
    sandbox = execute_in_sandbox(code, 120, python)
    geometry = evaluate_geometry(code, args.validator_root, python)
    invalid = evaluate_geometry('raise RuntimeError("intentional smoke test")', args.validator_root, python)
    drawing = render_four_views(source, args.output, "box", "A3", args.drawcad_root, python)
    passed = (sandbox.ok and sandbox.solid_count == 1 and geometry.code_valid
              and geometry.geometry_valid and not invalid.code_valid
              and drawing.ok and drawing.svg_path is not None
              and drawing.svg_path.is_file() and drawing.png_path is not None
              and drawing.png_path.is_file())
    report = {
        "python": sys.version, "platform": platform.platform(),
        "cadquery": cadquery.__version__,
        "packages": {name: package_version(name) for name in ("cadquery-ocp", "numpy", "vtk")},
        "validator_root": str(args.validator_root), "drawcad_root": str(args.drawcad_root),
        "sandbox": asdict(sandbox), "geometry": asdict(geometry),
        "invalid_code_rejected": not invalid.code_valid, "drawing": asdict(drawing),
    }
    if args.render_3d:
        preview = render_3d_preview(source, args.output, "box", python)
        report["preview"] = asdict(preview)
        passed = passed and preview.ok
    report["passed"] = bool(passed)
    payload = json.dumps(report, indent=2, default=str)
    (args.output / "report.json").write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
