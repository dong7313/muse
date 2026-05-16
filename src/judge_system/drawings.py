from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class DrawingArtifacts:
    ok: bool
    svg_path: Optional[Path]
    png_path: Optional[Path]
    error: str


@dataclass(frozen=True)
class RenderArtifacts:
    ok: bool
    png_path: Optional[Path]
    mesh_path: Optional[Path]
    step_path: Optional[Path]
    error: str


def create_png_preview(svg_path: Path, png_path: Path, timeout_seconds: int = 60) -> tuple[bool, str]:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            ["rsvg-convert", str(svg_path), "-o", str(png_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            return False, (completed.stderr or completed.stdout).strip()
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"PNG preview render timed out after {timeout_seconds}s"


DRAWING_TEMPLATE = r"""
import json
import sys
from pathlib import Path

drawcad_root = Path({drawcad_root!r})
sys.path.insert(0, str(drawcad_root))
from cad_to_svg import CadQueryToSVG

source_path = Path({code_path!r})
svg_path = Path({svg_path!r})
converter = CadQueryToSVG(name={name!r}, paper_size={paper_size!r})
converter.load_model_from_file(str(source_path))
converter.render(str(svg_path))
print(json.dumps({{"ok": True, "svg_path": str(svg_path)}}))
"""


RENDER_TEMPLATE = r"""
import json
from pathlib import Path

import cadquery as cq
import vtk

code_path = Path({code_path!r})
png_path = Path({png_path!r})
mesh_path = Path({mesh_path!r})
step_path = Path({step_path!r})

namespace = {{"__builtins__": __builtins__, "cq": cq, "cadquery": cq}}
exec(code_path.read_text(encoding="utf-8"), namespace, namespace)
result = namespace.get("result")
if result is None:
    raise RuntimeError("code did not define result")
export_obj = result
if isinstance(result, cq.Workplane):
    if not result.vals():
        raise RuntimeError("result did not produce any solids")
elif not isinstance(result, cq.Shape):
    raise RuntimeError(f"result must be Workplane or Shape-like, got {{type(result).__name__}}")

cq.exporters.export(export_obj, str(mesh_path), "STL")
cq.exporters.export(export_obj, str(step_path), "STEP")

reader = vtk.vtkSTLReader()
reader.SetFileName(str(mesh_path))
reader.Update()

mapper = vtk.vtkPolyDataMapper()
mapper.SetInputConnection(reader.GetOutputPort())

actor = vtk.vtkActor()
actor.SetMapper(mapper)
actor.GetProperty().SetColor(0.82, 0.72, 0.57)
actor.GetProperty().SetInterpolationToPhong()
actor.GetProperty().SetSpecular(0.18)
actor.GetProperty().SetSpecularPower(24)

renderer = vtk.vtkRenderer()
renderer.AddActor(actor)
renderer.SetBackground(0.972, 0.955, 0.92)
renderer.SetBackground2(0.92, 0.945, 0.985)
renderer.GradientBackgroundOn()

light_key = vtk.vtkLight()
light_key.SetPosition(1.8, -2.2, 2.4)
light_key.SetFocalPoint(0.0, 0.0, 0.0)
light_key.SetIntensity(1.0)
renderer.AddLight(light_key)

light_fill = vtk.vtkLight()
light_fill.SetPosition(-1.5, 1.0, 1.2)
light_fill.SetFocalPoint(0.0, 0.0, 0.0)
light_fill.SetIntensity(0.45)
renderer.AddLight(light_fill)

render_window = vtk.vtkRenderWindow()
render_window.SetOffScreenRendering(1)
render_window.AddRenderer(renderer)
render_window.SetSize(1200, 900)
render_window.SetMultiSamples(0)

renderer.ResetCamera()
bounds = actor.GetBounds()
cx = (bounds[0] + bounds[1]) / 2.0
cy = (bounds[2] + bounds[3]) / 2.0
cz = (bounds[4] + bounds[5]) / 2.0
dx = max(bounds[1] - bounds[0], 1.0)
dy = max(bounds[3] - bounds[2], 1.0)
dz = max(bounds[5] - bounds[4], 1.0)
extent = max(dx, dy, dz)

camera = renderer.GetActiveCamera()
camera.SetFocalPoint(cx, cy, cz)
camera.SetPosition(cx + 2.2 * extent, cy - 2.0 * extent, cz + 1.5 * extent)
camera.SetViewUp(0.0, 0.0, 1.0)
camera.SetClippingRange(0.1, max(10000.0, 10.0 * extent))

render_window.Render()

w2i = vtk.vtkWindowToImageFilter()
w2i.SetInput(render_window)
w2i.SetScale(1)
w2i.SetInputBufferTypeToRGBA()
w2i.ReadFrontBufferOff()
w2i.Update()

writer = vtk.vtkPNGWriter()
writer.SetFileName(str(png_path))
writer.SetInputConnection(w2i.GetOutputPort())
writer.Write()

print(json.dumps({{"ok": True, "png_path": str(png_path), "mesh_path": str(mesh_path), "step_path": str(step_path)}}))
"""


def render_four_views(
    code_path: Path,
    output_dir: Path,
    name: str,
    paper_size: str,
    drawcad_root: Path,
    python_executable: Path,
    timeout_seconds: int = 120,
) -> DrawingArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / f"{name}.svg"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(
            DRAWING_TEMPLATE.format(
                drawcad_root=str(drawcad_root),
                code_path=str(code_path),
                svg_path=str(svg_path),
                name=name,
                paper_size=paper_size,
            )
        )
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
            return DrawingArtifacts(ok=False, svg_path=None, png_path=None, error=(completed.stderr or completed.stdout).strip())
        png_path = svg_path.with_suffix(".png")
        png_ok, png_error = create_png_preview(svg_path, png_path, timeout_seconds=min(timeout_seconds, 60))
        return DrawingArtifacts(
            ok=True,
            svg_path=svg_path,
            png_path=png_path if png_ok else None,
            error="" if png_ok else png_error,
        )
    except subprocess.TimeoutExpired:
        return DrawingArtifacts(ok=False, svg_path=None, png_path=None, error=f"DrawCAD timed out after {timeout_seconds}s")
    finally:
        script_path.unlink(missing_ok=True)


def render_3d_preview(
    code_path: Path,
    output_dir: Path,
    name: str,
    python_executable: Path,
    timeout_seconds: int = 120,
) -> RenderArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    png_path = output_dir / f"{name}_render.png"
    mesh_path = output_dir / f"{name}.stl"
    step_path = output_dir / f"{name}.step"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(
            RENDER_TEMPLATE.format(
                code_path=str(code_path),
                png_path=str(png_path),
                mesh_path=str(mesh_path),
                step_path=str(step_path),
            )
        )
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
            return RenderArtifacts(
                ok=False,
                png_path=None,
                mesh_path=None,
                step_path=None,
                error=(completed.stderr or completed.stdout).strip(),
            )
        return RenderArtifacts(ok=True, png_path=png_path, mesh_path=mesh_path, step_path=step_path, error="")
    except subprocess.TimeoutExpired:
        return RenderArtifacts(
            ok=False,
            png_path=None,
            mesh_path=None,
            step_path=None,
            error=f"3D render timed out after {timeout_seconds}s",
        )
    finally:
        script_path.unlink(missing_ok=True)
