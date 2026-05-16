#!/usr/bin/env python3
"""Assemble the MUSE benchmark dataset for HuggingFace upload."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("MUSE_ROOT", Path(__file__).resolve().parents[1])).resolve()
TASK_DIR = Path(os.environ.get("TASK_ROOT", ROOT / "data" / "task"))
SOURCE_DIR = Path(os.environ.get("SOURCE_ROOT", ROOT / "data" / "source"))
RUBRIC_DIR = Path(os.environ.get("RUBRIC_ROOT", ROOT / "data" / "rubrics"))

OUT_DIR = Path(os.environ.get("HF_OUT_DIR", ROOT / "hf_dataset"))
CASES_DIR = OUT_DIR / "cases"

SVG_AS_RENDER = {
    "vase_wave_blossom",
    "vase_wave_dune",
    "vase_wave_fluted",
    "vase_wave_petal",
    "vase_wave_ripple",
    "vase_wave_scallop",
    "vase_wave_shell",
    "vase_wave_twist",
    "wave_vase",
}

STEP_RENDER_WORKER = r"""
import json, tempfile
from pathlib import Path
import cadquery as cq
from cadquery import exporters
import vtk

stp = Path({stp!r})
out_png = Path({out!r})
out_png.parent.mkdir(parents=True, exist_ok=True)

shape = cq.importers.importStep(str(stp))
tmp_stl = Path(tempfile.mkstemp(suffix='.stl')[1])
exporters.export(shape, str(tmp_stl), exportType=exporters.ExportTypes.STL)

reader = vtk.vtkSTLReader(); reader.SetFileName(str(tmp_stl)); reader.Update()
mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(reader.GetOutputPort())
actor = vtk.vtkActor(); actor.SetMapper(mapper)
prop = actor.GetProperty()
prop.SetColor(0.82, 0.72, 0.57); prop.SetInterpolationToPhong()
prop.SetSpecular(0.18); prop.SetSpecularPower(24)

renderer = vtk.vtkRenderer(); renderer.AddActor(actor)
renderer.SetBackground(0.972, 0.955, 0.92)
renderer.SetBackground2(0.92, 0.945, 0.985); renderer.GradientBackgroundOn()

light_key = vtk.vtkLight(); light_key.SetPosition(1.8, -2.2, 2.4)
light_key.SetFocalPoint(0.0, 0.0, 0.0); light_key.SetIntensity(1.0)
renderer.AddLight(light_key)
light_fill = vtk.vtkLight(); light_fill.SetPosition(-1.5, 1.0, 1.2)
light_fill.SetFocalPoint(0.0, 0.0, 0.0); light_fill.SetIntensity(0.45)
renderer.AddLight(light_fill)

rw = vtk.vtkRenderWindow(); rw.SetOffScreenRendering(1)
rw.AddRenderer(renderer); rw.SetSize(1200, 900); rw.SetMultiSamples(0)

renderer.ResetCamera()
b = actor.GetBounds()
cx, cy, cz = (b[0]+b[1])/2.0, (b[2]+b[3])/2.0, (b[4]+b[5])/2.0
extent = max(max(b[1]-b[0], 1.0), max(b[3]-b[2], 1.0), max(b[5]-b[4], 1.0))
cam = renderer.GetActiveCamera()
cam.SetFocalPoint(cx, cy, cz)
cam.SetPosition(cx + 2.2*extent, cy - 2.0*extent, cz + 1.5*extent)
cam.SetViewUp(0.0, 0.0, 1.0)
cam.SetClippingRange(0.1, max(10000.0, 10.0*extent))
rw.Render()

w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(rw); w2i.SetScale(1)
w2i.SetInputBufferTypeToRGBA(); w2i.ReadFrontBufferOff(); w2i.Update()
writer = vtk.vtkPNGWriter(); writer.SetFileName(str(out_png))
writer.SetInputConnection(w2i.GetOutputPort()); writer.Write()
tmp_stl.unlink(missing_ok=True)
print(json.dumps({{'ok': True}}))
"""


def svg_to_png(svg_path: Path, png_path: Path) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["rsvg-convert", str(svg_path), "-o", str(png_path)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"rsvg-convert failed for {svg_path}: {proc.stderr}")


def step_to_png(stp_path: Path, png_path: Path) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    code = STEP_RENDER_WORKER.format(stp=str(stp_path), out=str(png_path))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"step render failed for {stp_path}: {proc.stderr}")


def assemble_case(case: str) -> dict:
    src_task = TASK_DIR / case / "task.md"
    src_dir = SOURCE_DIR / case
    src_svg = src_dir / f"{case}.svg"
    src_svg_png = src_dir / f"{case}.png"
    src_thumbnail = src_dir / "thumbnail.png"
    src_stp = src_dir / f"{case}.stp"
    src_rubric = RUBRIC_DIR / f"{case}.md"

    for required in (src_task, src_rubric):
        if not required.exists():
            raise FileNotFoundError(f"missing {required}")

    out_case = CASES_DIR / case
    out_case.mkdir(parents=True, exist_ok=True)
    out_svg_png = out_case / f"{case}.png"
    out_stp_render = out_case / f"{case}_stp_render.png"

    shutil.copyfile(src_task, out_case / "design_description.md")

    # SVG-to-PNG: prefer existing prebuilt png, otherwise convert from svg.
    if src_svg_png.exists():
        shutil.copyfile(src_svg_png, out_svg_png)
        svg_png_source = "prebuilt"
    elif src_svg.exists():
        svg_to_png(src_svg, out_svg_png)
        svg_png_source = "rsvg-convert"
    else:
        raise FileNotFoundError(f"no svg or png for {case}")

    # STP render.
    if case in SVG_AS_RENDER:
        shutil.copyfile(out_svg_png, out_stp_render)
        stp_render_source = "svg_png_duplicate"
    elif src_thumbnail.exists():
        shutil.copyfile(src_thumbnail, out_stp_render)
        stp_render_source = "thumbnail"
    elif src_stp.exists():
        step_to_png(src_stp, out_stp_render)
        stp_render_source = "vtk_render"
    else:
        raise FileNotFoundError(f"no stp render available for {case}")

    shutil.copyfile(src_rubric, out_case / "evaluation_rubric.md")

    return {
        "case_id": case,
        "design_description": f"cases/{case}/design_description.md",
        "svg_png": f"cases/{case}/{case}.png",
        "stp_render": f"cases/{case}/{case}_stp_render.png",
        "evaluation_rubric": f"cases/{case}/evaluation_rubric.md",
        "svg_png_source": svg_png_source,
        "stp_render_source": stp_render_source,
    }


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    CASES_DIR.mkdir(parents=True)

    cases = sorted(p.name for p in TASK_DIR.iterdir() if p.is_dir())
    metadata = []
    for c in cases:
        print(f"  -> {c}", flush=True)
        metadata.append(assemble_case(c))

    (OUT_DIR / "metadata.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in metadata) + "\n",
        encoding="utf-8",
    )
    print(f"assembled {len(metadata)} cases at {OUT_DIR}")


if __name__ == "__main__":
    main()
