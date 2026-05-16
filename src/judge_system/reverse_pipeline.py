from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from .config import ModelSpec, _load_model_list
from .drawings import render_four_views, render_3d_preview
from .geometry_metrics import evaluate_geometry
from .providers import OpenRouterChatProvider
from .prompts import SYSTEM_PROMPT
from .report_viewer import build_viewer
from .rubric import expected_component_count_from_plan
from .sandbox import execute_in_sandbox
from .svg_metrics import SvgMetrics, analyze_svg


DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-pro-preview"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_WORKERS = 4
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_OPENROUTER_API_KEY = os.environ.get(DEFAULT_API_KEY_ENV, "")
DEFAULT_DRAWCAD_ROOT = Path(os.environ.get("DRAWCAD_ROOT", Path(__file__).resolve().parents[3] / "external" / "DrawCAD")).resolve()
DEFAULT_VALIDATOR_ROOT = Path(os.environ.get("VALIDATOR_ROOT", Path(__file__).resolve().parents[3] / "external" / "validator" / "validator")).resolve()
DEFAULT_PYTHON_EXECUTABLE = Path(sys.executable).resolve()
DEFAULT_ALIGNMENT_MODEL = "google/gemini-3.1-pro-preview"
DEFAULT_RENDER_ONLY_CASES_PATH = (Path(__file__).resolve().parent / "render_only_cases.txt").resolve()


def _load_render_only_cases(path: Path | str | None = None) -> set[str]:
    """Read a list of case names whose SVG artifacts must be replaced by 3D render PNGs.

    Format: one case name per line; blank lines and lines starting with '#' are ignored.
    Returns an empty set if the file is missing.
    """
    target = Path(path) if path is not None else DEFAULT_RENDER_ONLY_CASES_PATH
    if not target.exists():
        return set()
    out: set[str] = set()
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


# Worker template: read a STEP file, tessellate, render off-screen with VTK,
# write to <out_png>. Mirrors judge_system/drawings.py:render_3d_preview's
# camera/lighting so render-only references look the same as candidate renders.
_STEP_RENDER_WORKER = r"""
import json, sys, tempfile
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
print(json.dumps({{'ok': True, 'bytes': out_png.stat().st_size}}))
"""


def _render_step_to_png(step_path: Path, out_png: Path, timeout_seconds: int = 180) -> Path | None:
    """Spawn an isolated subprocess to render a STEP file to a VTK PNG.

    Used for render-only cases (curved/organic geometry) where DrawCAD's
    SVG projection loses the surface detail. Returns the output path if
    successful, otherwise None.
    """
    if not step_path.exists():
        return None
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if out_png.exists() and out_png.stat().st_mtime >= step_path.stat().st_mtime:
        return out_png  # already up-to-date
    import subprocess as _sp
    code = _STEP_RENDER_WORKER.format(stp=str(step_path), out=str(out_png))
    try:
        proc = _sp.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=timeout_seconds)
    except _sp.TimeoutExpired:
        return None
    if proc.returncode != 0 or not out_png.exists():
        return None
    return out_png


@dataclass(frozen=True)
class ReverseCaseResult:
    case_name: str
    task_markdown: str
    prompt_path: str
    task_path: str
    status: str
    error: str = ""


@dataclass(frozen=True)
class RubricCaseResult:
    case_name: str
    rubric_markdown: str
    prompt_path: str
    rubric_path: str
    status: str
    error: str = ""


@dataclass(frozen=True)
class BenchmarkCaseResult:
    case_name: str
    model_label: str
    sample_index: int
    status: str
    message: str
    code_path: str
    prompt_path: str
    sandbox_ok: bool
    geometry_ok: bool
    code_ok: bool
    result_solid_count: int
    bbox_dx_mm: float
    bbox_dy_mm: float
    bbox_dz_mm: float
    gt_component_count: int
    svg_component_count_estimate: int
    component_count_match: bool
    component_count_delta: int
    svg_path_count: int
    watertight: bool | None
    self_intersection_free: bool | None
    alignment_score: float
    alignment_summary: str
    alignment_error: str
    sandbox_error: str
    geometry_issue_summary: str
    svg_path: str
    png_path: str
    render_png_path: str
    render_mesh_path: str
    render_step_path: str
    geometry_valid: bool
    normal_consistency: bool | None
    volume_valid: bool | None
    bbox_valid: bool | None
    occt_valid: bool | None
    llm_judge_model: str
    llm_judge_breakdown_json: str
    # Split watertight/manifold (added later — defaulted for backwards compat)
    watertight_strict: bool | None = None
    manifold: bool | None = None


def _default_model_specs() -> list[ModelSpec]:
    return [
        # open-source models (runnable now)
        ModelSpec(
            label="oss-qwen-2.5-72b",
            provider="openrouter",
            model="qwen/qwen-2.5-72b-instruct",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="oss-qwen-qwq-32b",
            provider="openrouter",
            model="qwen/qwq-32b",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="oss-qwen-3.5-122b-a10b",
            provider="openrouter",
            model="qwen/qwen3.5-122b-a10b",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="oss-qwen-3.6-35b-a3b",
            provider="openrouter",
            model="qwen/qwen3.6-35b-a3b",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="oss-qwen-3.6-coder-next",
            provider="openrouter",
            model="qwen/qwen3-coder-next",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="oss-llama-3.1-70b",
            provider="openrouter",
            model="meta-llama/llama-3.1-70b-instruct",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="oss-llama-3.1-8b",
            provider="openrouter",
            model="meta-llama/llama-3.1-8b-instruct",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        # closed-source models
        ModelSpec(
            label="closed-gpt-5.5",
            provider="openrouter",
            model="openai/gpt-5.5",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-z-ai-glm-5.1",
            provider="openrouter",
            model="z-ai/glm-5.1",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-z-ai-glm-4.7-flash",
            provider="openrouter",
            model="z-ai/glm-4.7-flash",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-minimax-m2.7",
            provider="openrouter",
            model="minimax/minimax-m2.7",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-minimax-m2.5",
            provider="openrouter",
            model="minimax/minimax-m2.5",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-gpt-4o",
            provider="openrouter",
            model="openai/gpt-4o",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-claude-opus-4.7",
            provider="openrouter",
            model="anthropic/claude-opus-4.7",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-claude-3.7-sonnet",
            provider="openrouter",
            model="anthropic/claude-3.7-sonnet",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
        ModelSpec(
            label="closed-gemini-3.1-pro",
            provider="openrouter",
            model="google/gemini-3.1-pro-preview",
            api_key_env=DEFAULT_API_KEY_ENV,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
        ),
    ]


def _load_model_specs(path: str | None) -> list[ModelSpec]:
    if not path:
        return _default_model_specs()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if not candidate.exists():
        fallback = (Path(__file__).resolve().parents[2] / candidate).resolve()
        if fallback.exists():
            candidate = fallback
    if not candidate.exists():
        raise FileNotFoundError(f"Model list file not found: {path}")
    return _load_model_list(candidate)


def _read_text(path: Path | None, max_chars: int = 12000) -> str:
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if len(text) <= max_chars:
        return text.strip()
    return text[: max_chars - 120].rstrip() + "\n... [truncated] ...\n"


def _safe_text(payload: str, max_chars: int = 12000) -> str:
    content = payload.strip()
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 120].rstrip() + "\n... [truncated] ..."


def _empty_svg_metrics() -> SvgMetrics:
    return SvgMetrics(
        view_labels=[],
        total_path_count=0,
        estimated_component_count=0,
        text_count=0,
        width_mm=0.0,
        height_mm=0.0,
    )


def _extract_python_code(raw: str) -> str:
    text = raw.strip()
    fence = re.search(r"```(?:python)?\s*\n(.*?)\n```", text, flags=re.S | re.I)
    if fence:
        return fence.group(1).strip() + "\n"
    fence = re.search(r"```\s*(.*?)\s*```", text, flags=re.S)
    if fence:
        return fence.group(1).strip() + "\n"
    return text


def _extract_task_component_count(task_text: str) -> int:
    patterns = [
        r"Planned Component Quantity\s*[:\-]?\s*\n?\s*([0-9]+)",
        r"Planned Component Quantity[:\s]+([0-9]+)",
        r"Component Quantity\s*[:\-]?\s*\n?\s*([0-9]+)",
        r"Component Quantity[:\s]+([0-9]+)",
        r"计划装配体数量\s*[：:]\s*([0-9]+)",
        r"组件数量\s*[：:]\s*([0-9]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, task_text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return int(match.group(1))
    if "Component Names" in task_text:
        start = task_text.find("Component Names")
        section = task_text[start:]
        count = len(re.findall(r"^\s*[-*]\s", section, flags=re.MULTILINE))
        if count > 0:
            return count
    if "组件名称" in task_text:
        start = task_text.find("组件名称")
        section = task_text[start:]
        count = len(re.findall(r"^\s*[-*]\s", section, flags=re.MULTILINE))
        if count > 0:
            return count
    fallback = expected_component_count_from_plan(task_text)
    return fallback if fallback > 0 else 0


def _build_cad_query_prompt(task_text: str, rubric_text: str, include_rubric: bool = False) -> str:
    if include_rubric and rubric_text.strip():
        return f"""Task specification:

```markdown
{task_text.strip()}
```

Rubric reference:

```markdown
{rubric_text.strip()}
```

Return only executable Python code.
Use only stdlib/math/cadquery and keep the output deterministic.
"""

    return f"""Task specification:

```markdown
{task_text.strip()}
```

Return only executable Python code.
Use only stdlib/math/cadquery and keep the output deterministic.
"""


def _build_alignment_prompt(task_text: str, rubric_text: str) -> str:
    return f"""<Task_Doc>
{task_text.strip()}
</Task_Doc>

<Reference_SVG>
The gold-standard reference rendering from the dataset.
Provided as Image 1. Use this as the visual anchor for "what correct looks like".
</Reference_SVG>

<Generated_SVG>
The candidate model's CAD artifact, generated from the model under evaluation.
Provided as Image 2. This is the subject you must score.
</Generated_SVG>

<Evaluation_Rubric>
{_safe_text(rubric_text, max_chars=14000)}
</Evaluation_Rubric>

Task instructions:
- Evaluate the candidate from `<Generated_SVG>` against the rubric.
- Use `<Reference_SVG>` as the visual anchor for comparison (do NOT score it).
- Output strict JSON only.
"""


def _load_score_system_prompt() -> str:
    score_prompt_module = _load_prompt_module(Path(__file__).resolve().parents[0] / "prompts" / "generate_score.py")
    system_prompt = str(getattr(score_prompt_module, "generate_score_sp", "")).strip()
    if not system_prompt:
        raise RuntimeError("generate_score_sp is empty. Please check prompts/generate_score.py")
    return system_prompt


def _parse_jsonish(raw: str) -> dict[str, Any]:
    text = raw.strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError(f"Cannot parse JSON response: {text[:200]}")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError(f"Alignment judge payload must be object: {text[:200]}")
    return payload


def _alignment_score_from_payload(payload: dict[str, Any]) -> tuple[float, str]:
    raw = payload.get("overall_score_normalized")
    if raw is None:
        raw = payload.get("overall_score")
    if raw is None:
        return 0.0, str(payload.get("overall_summary", "")).strip()
    score = float(raw)
    if score > 1:
        score = score / 100.0
    score = max(0.0, min(1.0, score))
    return score, str(payload.get("overall_summary", "")).strip()


def _image_part(path: Path) -> dict[str, Any]:
    import base64

    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def _run_alignment_judge(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: int,
    system_prompt: str,
    task_text: str,
    rubric_text: str,
    candidate_svg_png: Path | None,
    reference_png: Path | None,
) -> dict[str, Any]:
    # <Reference_SVG> = gold-standard GT (Image 1); <Generated_SVG> = candidate model output (Image 2).
    # Order matters: reference must be first so its label/index match the prompt.
    if reference_png is None or not reference_png.exists():
        raise RuntimeError("No reference (GT) image for alignment judge.")
    if candidate_svg_png is None or not candidate_svg_png.exists():
        raise RuntimeError("No candidate (generated) image for alignment judge.")

    def _image_label(role: str, idx: int, path: Path) -> dict[str, Any]:
        return {"type": "text", "text": f"{role} (Image {idx}): {path.name}"}

    payload = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.codex.app",
            "X-Title": "judge_system alignment judge",
        },
        json={
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_alignment_prompt(task_text, rubric_text)},
                    _image_label("<Reference_SVG>", 1, reference_png),
                    _image_part(reference_png),
                    _image_label("<Generated_SVG>", 2, candidate_svg_png),
                    _image_part(candidate_svg_png),
                ],
                },
            ],
        },
        timeout=timeout_seconds,
    )
    payload.raise_for_status()
    content = payload.json()["choices"][0]["message"]["content"]
    return _parse_jsonish(content)


def _ensure_reference_svg_png(
    *,
    raw_svg_path: Path,
    output_root: Path,
    case_name: str,
    timeout_seconds: int,
) -> Path | None:
    if not raw_svg_path.exists():
        return None
    if raw_svg_path.suffix.lower() == ".png":
        return raw_svg_path

    from .drawings import create_png_preview

    reference_root = output_root / case_name / "reference"
    reference_root.mkdir(parents=True, exist_ok=True)
    target = reference_root / f"{raw_svg_path.stem}.png"
    if target.exists():
        return target

    ok, error = create_png_preview(raw_svg_path, target, timeout_seconds=min(timeout_seconds, 60))
    if not ok:
        raise RuntimeError(f"Failed to render reference SVG to PNG: {error}")
    return target


def _run_one_benchmark_case(
    *,
    case_name: str,
    sample_index: int,
    raw_case_root: Path,
    task_path: Path,
    rubric_path: Path,
    run_root: Path,
    model: ModelSpec,
    api_key: str,
    timeout_seconds: int,
    alignment_timeout_seconds: int,
    alignment_model: str,
    alignment_base_url: str,
    run_alignment: bool,
    alignment_system_prompt: str,
    include_rubric_in_prompt: bool,
    drawcad_root: Path,
    validator_root: Path,
    python_executable: Path,
    render_only_cases: set[str] | None = None,
) -> BenchmarkCaseResult:
    is_render_only = bool(render_only_cases and case_name in render_only_cases)
    files = _find_case_root_files(raw_case_root)
    py_text = _read_text(files.get("py"), max_chars=18000)
    if not py_text:
        return BenchmarkCaseResult(
            case_name=case_name,
            model_label=model.label,
            sample_index=sample_index,
            status="skipped",
            message="Missing raw .py source",
            code_path="",
            prompt_path="",
            sandbox_ok=False,
            geometry_ok=False,
            code_ok=False,
            result_solid_count=0,
            bbox_dx_mm=0.0,
            bbox_dy_mm=0.0,
            bbox_dz_mm=0.0,
            gt_component_count=0,
            svg_component_count_estimate=0,
            component_count_match=False,
            component_count_delta=0,
            svg_path_count=0,
            watertight=None,
            self_intersection_free=None,
            alignment_score=0.0,
            alignment_summary="",
            alignment_error="Missing raw .py source",
            sandbox_error="Missing raw .py source",
            geometry_issue_summary="",
            svg_path="",
            png_path="",
            render_png_path="",
            render_mesh_path="",
            render_step_path="",
            geometry_valid=False,
            normal_consistency=None,
            volume_valid=None,
            bbox_valid=None,
            occt_valid=None,
            llm_judge_model=alignment_model,
            llm_judge_breakdown_json="{}",
        )

    task_text = _read_text(task_path, max_chars=20000)
    if not task_text:
        return BenchmarkCaseResult(
            case_name=case_name,
            model_label=model.label,
            sample_index=sample_index,
            status="skipped",
            message="Missing task.md",
            code_path="",
            prompt_path="",
            sandbox_ok=False,
            geometry_ok=False,
            code_ok=False,
            result_solid_count=0,
            bbox_dx_mm=0.0,
            bbox_dy_mm=0.0,
            bbox_dz_mm=0.0,
            gt_component_count=0,
            svg_component_count_estimate=0,
            component_count_match=False,
            component_count_delta=0,
            svg_path_count=0,
            watertight=None,
            self_intersection_free=None,
            alignment_score=0.0,
            alignment_summary="",
            alignment_error="Missing task.md",
            sandbox_error="Missing task.md",
            geometry_issue_summary="",
            svg_path="",
            png_path="",
            render_png_path="",
            render_mesh_path="",
            render_step_path="",
            geometry_valid=False,
            normal_consistency=None,
            volume_valid=None,
            bbox_valid=None,
            occt_valid=None,
            llm_judge_model=alignment_model,
            llm_judge_breakdown_json="{}",
        )

    rubric_text = _read_text(rubric_path, max_chars=26000)
    if not rubric_text:
        return BenchmarkCaseResult(
            case_name=case_name,
            model_label=model.label,
            sample_index=sample_index,
            status="skipped",
            message="Missing rubric.md",
            code_path="",
            prompt_path="",
            sandbox_ok=False,
            geometry_ok=False,
            code_ok=False,
            result_solid_count=0,
            bbox_dx_mm=0.0,
            bbox_dy_mm=0.0,
            bbox_dz_mm=0.0,
            gt_component_count=0,
            svg_component_count_estimate=0,
            component_count_match=False,
            component_count_delta=0,
            svg_path_count=0,
            watertight=None,
            self_intersection_free=None,
            alignment_score=0.0,
            alignment_summary="",
            alignment_error="Missing rubric.md",
            sandbox_error="Missing rubric.md",
            geometry_issue_summary="",
            svg_path="",
            png_path="",
            render_png_path="",
            render_mesh_path="",
            render_step_path="",
            geometry_valid=False,
            normal_consistency=None,
            volume_valid=None,
            bbox_valid=None,
            occt_valid=None,
            llm_judge_model=alignment_model,
            llm_judge_breakdown_json="{}",
        )

    gt_component_count = _extract_task_component_count(task_text)

    prompt_root = run_root / "cad_prompts" / case_name
    prompt_root.mkdir(parents=True, exist_ok=True)
    sample_root = run_root / "runs" / case_name / model.label / f"sample_{sample_index}"
    sample_root.mkdir(parents=True, exist_ok=True)

    prompt_text = _build_cad_query_prompt(
        task_text=task_text,
        rubric_text=rubric_text,
        include_rubric=include_rubric_in_prompt,
    )
    prompt_path = prompt_root / f"{model.label}_sample_{sample_index}.json"
    prompt_path.write_text(
        json.dumps(
            {
                "case_name": case_name,
                "sample_index": sample_index,
                "model_label": model.label,
                "model": model.model,
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt": prompt_text,
                "inputs": {
                    "task_path": str(task_path),
                    "rubric_path": str(rubric_path),
                    "raw_py_path": str(files.get("py", "")),
                    "raw_svg_path": str(files.get("svg", "")),
                    "raw_stl_path": str(files.get("stl", "")),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    code_path = sample_root / "code.py"
    try:
        raw = _openrouter_chat(
            api_key=api_key,
            base_url=model.base_url,
            model=model.model,
            temperature=model.temperature,
            timeout_seconds=timeout_seconds,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt_text,
        )
        code = _extract_python_code(raw)
        if not code.strip():
            raise RuntimeError("Model returned empty response")
        code_path.write_text(code, encoding="utf-8")
    except Exception as exc:
        return BenchmarkCaseResult(
            case_name=case_name,
            model_label=model.label,
            sample_index=sample_index,
            status="failed",
            message=str(exc),
            code_path="",
            prompt_path=str(prompt_path),
            sandbox_ok=False,
            geometry_ok=False,
            code_ok=False,
            result_solid_count=0,
            bbox_dx_mm=0.0,
            bbox_dy_mm=0.0,
            bbox_dz_mm=0.0,
            gt_component_count=gt_component_count,
            svg_component_count_estimate=0,
            component_count_match=False,
            component_count_delta=-gt_component_count if gt_component_count else 0,
            svg_path_count=0,
            watertight=None,
            self_intersection_free=None,
            alignment_score=0.0,
            alignment_summary="",
            alignment_error=str(exc),
            sandbox_error=str(exc),
            geometry_issue_summary="",
            svg_path="",
            png_path="",
            render_png_path="",
            render_mesh_path="",
            render_step_path="",
            geometry_valid=False,
            normal_consistency=None,
            volume_valid=None,
            bbox_valid=None,
            occt_valid=None,
            llm_judge_model=alignment_model,
            llm_judge_breakdown_json="{}",
        )

    code = code_path.read_text(encoding="utf-8")
    sandbox = execute_in_sandbox(code, timeout_seconds, python_executable)

    geometry_ok = False
    geometry = None
    geometry_issue_summary = ""
    if sandbox.ok:
        geometry = evaluate_geometry(code, validator_root, python_executable, timeout_seconds)
        geometry_ok = geometry.geometry_valid
        geometry_issue_summary = geometry.issue_summary

    bbox = sandbox.bbox if sandbox.ok else [0.0, 0.0, 0.0]
    result_solid_count = sandbox.solid_count

    svg_path = ""
    png_path = ""
    svg_metrics = _empty_svg_metrics()
    # For render-only (curved/organic) cases, skip the DrawCAD 4-view SVG step
    # entirely — the candidate visual will be the VTK 3D render produced below.
    if sandbox.ok and not is_render_only:
        drawing = render_four_views(
            code_path,
            sample_root / "drawing",
            f"{case_name}_{model.label}_{sample_index}",
            "A4",
            drawcad_root,
            python_executable,
            timeout_seconds,
        )
        if drawing.ok and drawing.svg_path is not None:
            svg_path = str(drawing.svg_path)
            if drawing.png_path is not None:
                png_path = str(drawing.png_path)
            try:
                svg_metrics = analyze_svg(drawing.svg_path)
            except Exception:
                svg_metrics = _empty_svg_metrics()

    render_png_path = ""
    render_mesh_path = ""
    render_step_path = ""
    if sandbox.ok:
        rendered = render_3d_preview(
            code_path,
            sample_root / "render",
            f"{case_name}_{model.label}_{sample_index}",
            python_executable,
            timeout_seconds,
        )
        if rendered.ok:
            if rendered.png_path is not None:
                render_png_path = str(rendered.png_path)
            if rendered.mesh_path is not None:
                render_mesh_path = str(rendered.mesh_path)
            if rendered.step_path is not None:
                render_step_path = str(rendered.step_path)

    alignment_score = 0.0
    alignment_summary = ""
    alignment_error = ""
    llm_payload: dict[str, Any] = {}
    if sandbox.ok and run_alignment:
        # Candidate image:
        #   - render-only case: the VTK 3D render produced above
        #   - normal case:      DrawCAD 4-view SVG -> rsvg-convert PNG
        #                       (fall back to render PNG if drawing failed)
        candidate_svg_png: Path | None
        if is_render_only:
            candidate_svg_png = Path(render_png_path) if render_png_path else None
        else:
            candidate_svg_png = Path(png_path) if png_path else None

        # Reference image:
        #   - render-only case: render the dataset's source STP via VTK
        #                       (out: runs/<case>/reference/<case>_render.png)
        #   - normal case:      rsvg-convert dataset's source SVG -> PNG
        reference_svg_png: Path | None = None
        if is_render_only:
            stp_source = files.get("stp") or files.get("step")
            if stp_source is None:
                # Fall back to globbing for a STEP file in the case dir
                fallbacks = sorted(raw_case_root.glob("*.stp")) + sorted(raw_case_root.glob("*.step"))
                stp_source = fallbacks[0] if fallbacks else None
            if stp_source is not None:
                try:
                    target_dir = run_root / "runs" / case_name / "reference"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    reference_svg_png = _render_step_to_png(
                        stp_source,
                        target_dir / f"{case_name}_render.png",
                        timeout_seconds=alignment_timeout_seconds,
                    )
                except Exception as exc:
                    alignment_error = str(exc)
        elif files.get("svg") is not None:
            try:
                reference_svg_png = _ensure_reference_svg_png(
                    raw_svg_path=files["svg"],
                    output_root=run_root / "runs",
                    case_name=case_name,
                    timeout_seconds=alignment_timeout_seconds,
                )
            except Exception as exc:
                alignment_error = str(exc)

        if candidate_svg_png is None and render_png_path:
            candidate_svg_png = Path(render_png_path)

        if not alignment_error:
            if candidate_svg_png is None:
                alignment_error = "No candidate visual image generated for alignment judge."
            elif reference_svg_png is None:
                alignment_error = (
                    "No reference STP source for alignment judge."
                    if is_render_only
                    else "No reference SVG source for alignment judge."
                )
            else:
                try:
                    llm_payload = _run_alignment_judge(
                        api_key=api_key,
                        base_url=alignment_base_url,
                        model=alignment_model,
                        timeout_seconds=alignment_timeout_seconds,
                        system_prompt=alignment_system_prompt,
                        task_text=task_text,
                        rubric_text=rubric_text,
                        candidate_svg_png=candidate_svg_png,
                        reference_png=reference_svg_png,
                    )
                    alignment_score, alignment_summary = _alignment_score_from_payload(llm_payload)
                except Exception as exc:
                    alignment_error = str(exc)
    elif sandbox.ok and not run_alignment:
        alignment_error = "alignment disabled"

    component_actual = result_solid_count or svg_metrics.estimated_component_count
    component_count_match = sandbox.ok and gt_component_count > 0 and component_actual == gt_component_count
    component_count_delta = component_actual - gt_component_count

    return BenchmarkCaseResult(
        case_name=case_name,
        model_label=model.label,
        sample_index=sample_index,
        status="done" if sandbox.ok else "failed",
        message="ok" if sandbox.ok else sandbox.error or "execution failed",
        code_path=str(code_path),
        prompt_path=str(prompt_path),
        sandbox_ok=bool(sandbox.ok),
        geometry_ok=geometry_ok,
        code_ok=True,
        result_solid_count=result_solid_count,
        bbox_dx_mm=float(bbox[0]),
        bbox_dy_mm=float(bbox[1]),
        bbox_dz_mm=float(bbox[2]),
        gt_component_count=gt_component_count,
        svg_component_count_estimate=svg_metrics.estimated_component_count,
        component_count_match=component_count_match,
        component_count_delta=component_count_delta,
        svg_path_count=svg_metrics.total_path_count,
        watertight=None if geometry is None else geometry.watertight,
        watertight_strict=None if geometry is None else getattr(geometry, "watertight_strict", None),
        manifold=None if geometry is None else getattr(geometry, "manifold", None),
        self_intersection_free=None if geometry is None else geometry.self_intersection_free,
        alignment_score=alignment_score,
        alignment_summary=alignment_summary,
        alignment_error=alignment_error,
        sandbox_error=sandbox.error or "",
        geometry_issue_summary=geometry_issue_summary,
        svg_path=svg_path,
        png_path=png_path,
        render_png_path=render_png_path,
        render_mesh_path=render_mesh_path,
        render_step_path=render_step_path,
        geometry_valid=bool(geometry.geometry_valid) if geometry else False,
        normal_consistency=None if geometry is None else geometry.normal_consistency,
        volume_valid=None if geometry is None else geometry.volume_valid,
        bbox_valid=None if geometry is None else geometry.bbox_valid,
        occt_valid=None if geometry is None else geometry.occt_valid,
        llm_judge_model=alignment_model,
        llm_judge_breakdown_json=json.dumps(llm_payload, ensure_ascii=False),
    )


def _run_benchmark(
    *,
    raw_root: Path,
    task_root: Path,
    rubric_root: Path,
    run_root: Path,
    cases: list[str] | None,
    limit: int | None,
    api_key: str,
    api_key_env: str,
    timeout_seconds: int,
    alignment_timeout_seconds: int,
    drawcad_root: Path,
    validator_root: Path,
    python_executable: Path,
    model_list: str | None,
    alignment_model: str,
    alignment_base_url: str,
    run_alignment: bool,
    alignment_system_prompt: str,
    include_rubric_in_prompt: bool,
    max_workers: int,
    render_only_cases: set[str] | None = None,
) -> tuple[list[BenchmarkCaseResult], list[dict[str, Any]]]:
    model_specs = _load_model_specs(model_list)

    if api_key and not os.environ.get(api_key_env):
        os.environ[api_key_env] = api_key
    if not (key := os.environ.get(api_key_env)):
        raise RuntimeError(f"Missing OpenRouter API key; set {api_key_env} or pass --api-key.")

    for spec in model_specs:
        if spec.api_key_env and not os.environ.get(spec.api_key_env):
            os.environ[spec.api_key_env] = key

    if not raw_root.exists():
        raise FileNotFoundError(f"Raw root does not exist: {raw_root}")
    if not task_root.exists():
        raise FileNotFoundError(f"Task root does not exist: {task_root}")
    if not rubric_root.exists():
        raise FileNotFoundError(f"Rubric root does not exist: {rubric_root}")

    if cases:
        case_names = [name for name in cases if (task_root / name / "task.md").exists() and (rubric_root / name / "rubric.md").exists()]
    else:
        case_names = sorted(
            p.name
            for p in task_root.iterdir()
            if p.is_dir() and (p / "task.md").exists() and (rubric_root / p.name / "rubric.md").exists()
        )

    if not case_names:
        raise RuntimeError("No runnable task/rubric case found for benchmark")
    if limit is not None:
        case_names = case_names[:limit]

    jobs: list[tuple[str, int, ModelSpec, Path, Path]] = []
    for case_name in case_names:
        task_path = task_root / case_name / "task.md"
        rubric_path = rubric_root / case_name / "rubric.md"
        for sample_index, model in enumerate(model_specs, start=1):
            jobs.append((case_name, sample_index, model, task_path, rubric_path))

    records: list[BenchmarkCaseResult] = []
    if max_workers <= 1:
        for case_name, sample_index, model, task_path, rubric_path in jobs:
            records.append(
                _run_one_benchmark_case(
                    case_name=case_name,
                    sample_index=sample_index,
                    raw_case_root=raw_root / case_name,
                    task_path=task_path,
                    rubric_path=rubric_path,
                    run_root=run_root,
                    model=model,
                    api_key=key,
                    timeout_seconds=timeout_seconds,
                    alignment_timeout_seconds=alignment_timeout_seconds,
                    alignment_model=alignment_model,
                    alignment_base_url=alignment_base_url,
                    run_alignment=run_alignment,
                    alignment_system_prompt=alignment_system_prompt,
                    include_rubric_in_prompt=include_rubric_in_prompt,
                    drawcad_root=drawcad_root,
                    validator_root=validator_root,
                    python_executable=python_executable,
                    render_only_cases=render_only_cases,
                )
            )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    _run_one_benchmark_case,
                    case_name=case_name,
                    sample_index=sample_index,
                    raw_case_root=raw_root / case_name,
                    task_path=task_path,
                    rubric_path=rubric_path,
                    run_root=run_root,
                    model=model,
                    api_key=key,
                    timeout_seconds=timeout_seconds,
                    alignment_timeout_seconds=alignment_timeout_seconds,
                    alignment_model=alignment_model,
                    alignment_base_url=alignment_base_url,
                    run_alignment=run_alignment,
                    alignment_system_prompt=alignment_system_prompt,
                    include_rubric_in_prompt=include_rubric_in_prompt,
                    drawcad_root=drawcad_root,
                    validator_root=validator_root,
                    python_executable=python_executable,
                    render_only_cases=render_only_cases,
                ): (case_name, sample_index, model)
                for case_name, sample_index, model, task_path, rubric_path in jobs
            }

            for future in concurrent.futures.as_completed(future_map):
                case_name, sample_index, model = future_map[future]
                try:
                    records.append(future.result())
                except Exception as exc:
                    records.append(
                        BenchmarkCaseResult(
                            case_name=case_name,
                            model_label=model.label,
                            sample_index=sample_index,
                            status="failed",
                            message=str(exc),
                            code_path="",
                            prompt_path="",
                            sandbox_ok=False,
                            geometry_ok=False,
                            code_ok=False,
                            result_solid_count=0,
                            bbox_dx_mm=0.0,
                            bbox_dy_mm=0.0,
                            bbox_dz_mm=0.0,
                            gt_component_count=0,
                            svg_component_count_estimate=0,
                            component_count_match=False,
                            component_count_delta=0,
                            svg_path_count=0,
                            watertight=None,
                            self_intersection_free=None,
                            alignment_score=0.0,
                            alignment_summary="",
                            alignment_error=str(exc),
                            sandbox_error=str(exc),
                            geometry_issue_summary="",
                            svg_path="",
                            png_path="",
                            render_png_path="",
                            render_mesh_path="",
                            render_step_path="",
                            geometry_valid=False,
                            normal_consistency=None,
                            volume_valid=None,
                            bbox_valid=None,
                            occt_valid=None,
                            llm_judge_model=alignment_model,
                            llm_judge_breakdown_json="{}",
                        )
                    )

    rubric_rows: list[dict[str, Any]] = []
    for case_name in case_names:
        task_text = _read_text(task_root / case_name / "task.md")
        rubric_text = _read_text(rubric_root / case_name / "rubric.md")
        if task_text and rubric_text:
            rubric_rows.append(
                {
                    "task_name": case_name,
                    "task_markdown": task_text,
                    "rubric_markdown": rubric_text,
                }
            )

    return records, rubric_rows


def _collect_benchmark_summary(records: list[BenchmarkCaseResult]) -> dict[str, float | int]:
    total = len(records)
    success_rows = [row for row in records if row.sandbox_ok]
    watertight_rows = [row for row in records if row.watertight]
    no_intersection_rows = [row for row in records if row.self_intersection_free]
    with_gt = [row for row in records if row.gt_component_count > 0]
    component_match_rows = [row for row in with_gt if row.component_count_match]
    aligned = [row for row in success_rows if not row.alignment_error]
    alignment = [row.alignment_score for row in aligned]
    return {
        "total": total,
        "survival_success": len(success_rows),
        "survival_rate": (len(success_rows) / total) if total else 0.0,
        "watertight_rate": (len(watertight_rows) / total) if total else 0.0,
        "self_intersection_free_rate": (len(no_intersection_rows) / total) if total else 0.0,
        "component_match_rate": (len(component_match_rows) / len(with_gt)) if with_gt else 0.0,
        "alignment_rate": (sum(alignment) / len(alignment)) if alignment else 0.0,
        "alignment_samples": len(alignment),
    }


def _write_records(
    *,
    run_root: Path,
    records: list[BenchmarkCaseResult],
    rubric_rows: list[dict],
) -> None:
    reports_root = run_root / "reports"
    reports_root.mkdir(parents=True, exist_ok=True)
    record_rows = []
    for row in records:
        record_rows.append(
            {
                "run_id": run_root.name,
                "task_name": row.case_name,
                "model_label": row.model_label,
                "model_name": row.model_label,
                "sample_index": row.sample_index,
                "generation_mode": "openrouter",
                "code_path": row.code_path,
                "svg_path": row.svg_path,
                "png_path": row.png_path,
                "render_png_path": row.render_png_path,
                "render_mesh_path": row.render_mesh_path,
                "render_step_path": row.render_step_path,
                "code_valid": row.code_ok,
                "geometry_valid": row.geometry_ok,
                "sandbox_ok": row.sandbox_ok,
                "sandbox_error": row.sandbox_error,
                "geometry_issue_summary": row.geometry_issue_summary,
                "result_solid_count": row.result_solid_count,
                "bbox_dx_mm": row.bbox_dx_mm,
                "bbox_dy_mm": row.bbox_dy_mm,
                "bbox_dz_mm": row.bbox_dz_mm,
                "gt_component_count": row.gt_component_count,
                "svg_component_count_estimate": row.svg_component_count_estimate,
                "component_count_match": row.component_count_match,
                "component_count_delta": row.component_count_delta,
                "svg_path_count": row.svg_path_count,
                "watertight": row.watertight,
                "watertight_strict": getattr(row, "watertight_strict", None),
                "manifold": getattr(row, "manifold", None),
                "self_intersection_free": row.self_intersection_free,
                "normal_consistency": row.normal_consistency,
                "volume_valid": row.volume_valid,
                "bbox_valid": row.bbox_valid,
                "occt_valid": row.occt_valid,
                "rubric_score": 0.0,
                "rubric_breakdown_json": "[]",
                "rubric_primary_breakdown_json": "{}",
                "rubric_category_breakdown_json": "{}",
                "llm_judge_model": row.llm_judge_model,
                "llm_judge_score": row.alignment_score,
                "llm_judge_summary": row.alignment_summary,
                "llm_judge_breakdown_json": row.llm_judge_breakdown_json,
                "llm_judge_error": row.alignment_error,
            }
        )
    (reports_root / "records.json").write_text(json.dumps(record_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_root / "rubric_catalog.json").write_text(json.dumps(rubric_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_root / "benchmark_summary.json").write_text(
        json.dumps(_collect_benchmark_summary(records), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _load_prompt_module(module_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"judge_task_prompt_{module_path.stem}", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load prompt module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _find_case_root_files(case_dir: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}

    def pick(suffix: str) -> Path | None:
        explicit = case_dir / f"{case_dir.name}{suffix}"
        if explicit.exists():
            return explicit
        candidates = sorted(case_dir.glob(f"*{suffix}"))
        return candidates[0] if candidates else None

    for key, suffix in {
        "py": ".py",
        "svg": ".svg",
        "stl": ".stl",
        "stp": ".stp",
        "step": ".step",
        "task": "task.md",
    }.items():
        file_path = pick(suffix)
        if file_path is not None:
            files[key] = file_path
    return files


def _coerce_design_text(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    block = re.sub(r"^```(?:markdown|md|json)?\s*", "", raw)
    block = re.sub(r"```\s*$", "", block).strip()
    return block


def _extract_json_text(raw: str) -> str:
    # Robustly handle cases where model wraps JSON in markdown fence
    match = re.search(r"\{.*\}", raw, flags=re.S)
    return match.group(0) if match else raw


def _normalize_response(raw: str) -> str:
    text = raw.strip()
    try:
        payload = json.loads(_extract_json_text(text))
    except Exception:
        return _coerce_design_text(text)

    for key in (
        "design_spec_markdown",
        "task_markdown",
        "task_markdown_text",
        "design_spec",
        "design",
        "task",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _coerce_design_text(value)

    # fallback: some models put markdown under `content` fields
    content = payload.get("content")
    if isinstance(content, str):
        return _coerce_design_text(content)

    return _coerce_design_text(text)


def _openrouter_chat(
    *,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    timeout_seconds: int,
    system_prompt: str,
    user_prompt: str,
    images: list[Path] | None = None,
) -> str:
    """Chat completion. If `images` is provided, sends a multimodal user message
    where each image becomes a base64 data-URL `image_url` part following the
    text part. Otherwise sends a plain text user message."""
    if images:
        existing = [p for p in images if p is not None and p.exists()]
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for path in existing:
            user_content.append(_image_part(path))
        user_message: dict[str, Any] = {"role": "user", "content": user_content}
    else:
        user_message = {"role": "user", "content": user_prompt}

    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.codex.app",
            "X-Title": "judge_system reverse task design inference",
        },
        json={
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                user_message,
            ],
        },
        timeout=timeout_seconds,
    )

    response.raise_for_status()
    payload = response.json()
    message = payload["choices"][0]["message"]["content"]
    return message


def _prepare_visual_image_for_case(
    *,
    case_name: str,
    raw_files: dict[str, Path],
    raw_case_root: Path,
    is_render_only: bool,
    output_dir: Path,
    timeout_seconds: int = 180,
) -> tuple[Path | None, str]:
    """Produce a single PNG image to attach to task / rubric inference prompts.

    Returns (path, description). Description is a short string explaining the
    image source (e.g. "STP→3D render" or "SVG→rsvg-convert PNG").
    For render-only cases: render STP via VTK.
    For normal cases: rsvg-convert the dataset SVG.
    Cached at output_dir/<case>_visual.png; reused if up-to-date.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{case_name}_visual.png"

    if is_render_only:
        stp = raw_files.get("stp") or raw_files.get("step")
        if stp is None:
            fallbacks = sorted(raw_case_root.glob("*.stp")) + sorted(raw_case_root.glob("*.step"))
            stp = fallbacks[0] if fallbacks else None
        if stp is None:
            return None, "no STEP source for render-only case"
        # Cache check: regenerate if STEP newer than PNG
        if target.exists() and target.stat().st_mtime >= stp.stat().st_mtime:
            return target, "STP→3D render (cached)"
        result = _render_step_to_png(stp, target, timeout_seconds=timeout_seconds)
        return (result, "STP→3D render") if result else (None, "STEP→PNG render failed")

    # Normal case: SVG → rsvg-convert PNG
    svg = raw_files.get("svg")
    if svg is None or not svg.exists():
        return None, "no SVG source"
    if target.exists() and target.stat().st_mtime >= svg.stat().st_mtime:
        return target, "SVG→PNG (rsvg-convert, cached)"
    from .drawings import create_png_preview
    ok, error = create_png_preview(svg, target, timeout_seconds=min(timeout_seconds, 60))
    if not ok:
        return None, f"SVG→PNG failed: {error}"
    return target, "SVG→PNG (rsvg-convert)"


def _build_task_prompt(
    *,
    system_prompt: str,
    example_text: str,
    case_name: str,
    py_text: str,
    svg_text: str,
    stl_text: str,
    is_render_only: bool = False,
    image_attached: bool = False,
) -> str:
    if image_attached and is_render_only:
        svg_block = (
            "Visual reference: see the attached image — a 3D render of the\n"
            "STEP geometry (used because this case has organic / curved\n"
            "surfaces that don't project well into a 2D technical drawing)."
        )
    elif image_attached:
        svg_block = (
            "Visual reference: see the attached image — a PNG rendered from\n"
            "the source SVG technical drawing (rsvg-convert)."
        )
    elif is_render_only:
        # Fallback when image preparation failed.
        svg_block = (
            "Visual reference (note):\n"
            "Render-only case (organic/curved geometry); image was supposed to\n"
            "be attached but failed to generate. Use the CadQuery source code\n"
            "as the primary structural reference."
        )
    else:
        # Backward-compat fallback: embed the raw SVG XML.
        svg_block = (
            "SVG source:\n```xml\n"
            f"{svg_text}\n"
            "```"
        )
    return f"""{system_prompt}

Reference example:
```markdown
{example_text.strip()}
```

Now generate Design Specification for target case.

Target case: {case_name}

CadQuery source code:
```python
{py_text}
```

{svg_block}

STL source:
```text
{stl_text}
```

Output rule:
- Return markdown design specification text only.
- If some field is unknown, keep it empty instead of hallucinating.
- Use the section structure in the reference example.
    """


def _infer_one_case(
    *,
    case_name: str,
    source_case_root: Path,
    prompt_root: Path,
    task_root: Path,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    timeout_seconds: int,
    overwrite: bool,
    system_prompt: str,
    example_text: str,
    render_only_cases: set[str] | None = None,
) -> ReverseCaseResult:
    files = _find_case_root_files(source_case_root)
    is_render_only = bool(render_only_cases and case_name in render_only_cases)

    py_text = _read_text(files.get("py"), max_chars=20000)
    if not py_text:
        return ReverseCaseResult(
            case_name=case_name,
            task_markdown="",
            prompt_path="",
            task_path="",
            status="skipped",
            error="No .py file found in raw case directory",
        )

    # SVG XML text is no longer embedded; we attach a PNG image instead.
    svg_text = ""
    stl_text = _read_text(files.get("stl"), max_chars=18000)

    # Prepare visual image (SVG→rsvg PNG, or STP→VTK render for render-only).
    visual_dir = prompt_root.parent / "visual_inputs"
    visual_path, visual_desc = _prepare_visual_image_for_case(
        case_name=case_name,
        raw_files=files,
        raw_case_root=source_case_root,
        is_render_only=is_render_only,
        output_dir=visual_dir,
        timeout_seconds=timeout_seconds,
    )
    image_attached = visual_path is not None

    prompt_text = _build_task_prompt(
        system_prompt=system_prompt,
        example_text=example_text,
        case_name=case_name,
        py_text=py_text,
        svg_text=svg_text,
        stl_text=stl_text,
        is_render_only=is_render_only,
        image_attached=image_attached,
    )

    case_prompt_dir = prompt_root / case_name
    case_prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = case_prompt_dir / "task_prompt.json"
    prompt_path.write_text(
        json.dumps(
            {
                "case_name": case_name,
                "system_prompt": system_prompt,
                "user_prompt": prompt_text,
                "inputs": {
                    "py_file": str(files.get("py", "")),
                    "svg_file": str(files.get("svg", "")),
                    "stl_file": str(files.get("stl", "")),
                    "visual_image": str(visual_path) if visual_path else "",
                    "visual_image_source": visual_desc,
                    "is_render_only": is_render_only,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    raw = _openrouter_chat(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        system_prompt=system_prompt,
        user_prompt=prompt_text,
        images=[visual_path] if visual_path else None,
    )
    task_markdown = _normalize_response(raw)

    if not task_markdown.strip():
        return ReverseCaseResult(
            case_name=case_name,
            task_markdown="",
            prompt_path=str(prompt_path),
            task_path="",
            status="failed",
            error="Model returned empty response",
        )

    task_case_dir = task_root / case_name
    if overwrite and task_case_dir.exists():
        # keep a deterministic target dir for reruns
        pass
    task_case_dir.mkdir(parents=True, exist_ok=True)

    target = task_case_dir / "task.md"
    if (not overwrite) and target.exists():
        return ReverseCaseResult(
            case_name=case_name,
            task_markdown=target.read_text(encoding="utf-8"),
            prompt_path=str(prompt_path),
            task_path=str(target),
            status="skipped",
            error="task.md already exists",
        )

    target.write_text(task_markdown, encoding="utf-8")
    return ReverseCaseResult(
        case_name=case_name,
        task_markdown=task_markdown,
        prompt_path=str(prompt_path),
        task_path=str(target),
        status="done",
    )


def _build_rubric_prompt(
    *,
    system_prompt: str,
    case_name: str,
    task_text: str,
    py_text: str,
    svg_text: str,
    stl_text: str,
    is_render_only: bool = False,
    image_attached: bool = False,
) -> str:
    if image_attached and is_render_only:
        svg_block = (
            "<Reference_SVG>\n"
            "See the attached image — a 3D render of the STEP geometry\n"
            "(used because this case has organic / curved surfaces).\n"
        )
    elif image_attached:
        svg_block = (
            "<Reference_SVG>\n"
            "See the attached image — a PNG rendered from the source SVG\n"
            "technical drawing (rsvg-convert).\n"
        )
    elif is_render_only:
        svg_block = (
            "<Reference_SVG>\n"
            "Render-only case (image attachment failed); rely on\n"
            "<Reference_Code> for structural cues.\n"
        )
    else:
        svg_block = (
            "<Reference_SVG>\n"
            "```xml\n"
            f"{svg_text}\n"
            "```"
        )
    return f"""{system_prompt}

Please follow the following placeholder mapping:

<Task_Doc>
```markdown
{task_text.strip()}
```

<Reference_Code>
```python
{py_text}
```

{svg_block}

<STL_Source>
```text
{stl_text}
```

Output rules:
- Return markdown rubric text only.
- If some piece of information is missing, keep the related criterion non-committal instead of hallucinating.
- Preserve the six sub-category structure.
"""


def _infer_one_rubric_case(
    *,
    case_name: str,
    source_case_root: Path,
    prompt_root: Path,
    rubric_root: Path,
    task_root: Path,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    timeout_seconds: int,
    overwrite: bool,
    system_prompt: str,
    render_only_cases: set[str] | None = None,
) -> RubricCaseResult:
    raw_files = _find_case_root_files(source_case_root)
    is_render_only = bool(render_only_cases and case_name in render_only_cases)
    task_path = task_root / case_name / "task.md"
    if not task_path.exists():
        return RubricCaseResult(
            case_name=case_name,
            rubric_markdown="",
            prompt_path="",
            rubric_path="",
            status="skipped",
            error=f"Task markdown not found: {task_path}",
        )

    task_text = _read_text(task_path, max_chars=20000)
    if not task_text:
        return RubricCaseResult(
            case_name=case_name,
            rubric_markdown="",
            prompt_path="",
            rubric_path="",
            status="skipped",
            error=f"Task markdown is empty: {task_path}",
        )

    py_text = _read_text(raw_files.get("py"), max_chars=20000)
    if not py_text:
        return RubricCaseResult(
            case_name=case_name,
            rubric_markdown="",
            prompt_path="",
            rubric_path="",
            status="skipped",
            error="No .py file found in raw case directory",
        )

    svg_text = ""  # SVG XML is no longer embedded; we attach a PNG image instead.
    stl_text = _read_text(raw_files.get("stl"), max_chars=18000)

    visual_dir = prompt_root.parent / "visual_inputs"
    visual_path, visual_desc = _prepare_visual_image_for_case(
        case_name=case_name,
        raw_files=raw_files,
        raw_case_root=source_case_root,
        is_render_only=is_render_only,
        output_dir=visual_dir,
        timeout_seconds=timeout_seconds,
    )
    image_attached = visual_path is not None

    prompt_text = _build_rubric_prompt(
        system_prompt=system_prompt,
        case_name=case_name,
        task_text=task_text,
        is_render_only=is_render_only,
        image_attached=image_attached,
        py_text=py_text,
        svg_text=svg_text,
        stl_text=stl_text,
    )

    case_prompt_dir = prompt_root / case_name
    case_prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = case_prompt_dir / "rubric_prompt.json"
    prompt_path.write_text(
        json.dumps(
            {
                "case_name": case_name,
                "task_path": str(task_path),
                "system_prompt": system_prompt,
                "user_prompt": prompt_text,
                "inputs": {
                    "task_file": str(task_path),
                    "py_file": str(raw_files.get("py", "")),
                    "svg_file": str(raw_files.get("svg", "")),
                    "stl_file": str(raw_files.get("stl", "")),
                    "visual_image": str(visual_path) if visual_path else "",
                    "visual_image_source": visual_desc,
                    "is_render_only": is_render_only,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    raw = _openrouter_chat(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        system_prompt=system_prompt,
        user_prompt=prompt_text,
        images=[visual_path] if visual_path else None,
    )
    rubric_markdown = _normalize_response(raw)

    if not rubric_markdown.strip():
        return RubricCaseResult(
            case_name=case_name,
            rubric_markdown="",
            prompt_path=str(prompt_path),
            rubric_path="",
            status="failed",
            error="Model returned empty response",
        )

    rubric_case_dir = rubric_root / case_name
    if overwrite and rubric_case_dir.exists():
        # keep deterministic target dir for reruns
        pass
    rubric_case_dir.mkdir(parents=True, exist_ok=True)

    target = rubric_case_dir / "rubric.md"
    if (not overwrite) and target.exists():
        return RubricCaseResult(
            case_name=case_name,
            rubric_markdown=target.read_text(encoding="utf-8"),
            prompt_path=str(prompt_path),
            rubric_path=str(target),
            status="skipped",
            error="rubric.md already exists",
        )

    target.write_text(rubric_markdown, encoding="utf-8")
    return RubricCaseResult(
        case_name=case_name,
        rubric_markdown=rubric_markdown,
        prompt_path=str(prompt_path),
        rubric_path=str(target),
        status="done",
    )


def infer_design_books_from_raw(
    *,
    raw_root: Path,
    run_root: Path,
    cases: list[str] | None,
    limit: int | None,
    api_key: str,
    api_key_env: str,
    base_url: str = DEFAULT_OPENROUTER_BASE_URL,
    model: str = DEFAULT_OPENROUTER_MODEL,
    temperature: float = 0.2,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    overwrite: bool = False,
    render_only_cases: set[str] | None = None,
) -> list[ReverseCaseResult]:
    if api_key and not os.environ.get(api_key_env):
        os.environ[api_key_env] = api_key
    if not (key := os.environ.get(api_key_env) or api_key):
        raise RuntimeError(f"Missing OpenRouter API key; set {api_key_env} or pass --api-key.")

    raw_root = raw_root.resolve()
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw root does not exist: {raw_root}")

    prompt_dir = run_root / "task_prompts"
    task_dir = run_root / "task"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(parents=True, exist_ok=True)

    prompt_module = _load_prompt_module(Path(__file__).resolve().parents[0] / "prompts" / "generate_task.py")
    example_module = _load_prompt_module(Path(__file__).resolve().parents[0] / "prompts" / "generate_task_example.py")
    system_prompt = str(getattr(prompt_module, "generate_task_sp", "")).strip()
    example_text = str(getattr(example_module, "example", "")).strip()

    if not system_prompt:
        raise RuntimeError("generate_task_sp is empty. Please check prompts/generate_task.py")

    if cases:
        case_names = [name for name in cases if (raw_root / name).is_dir()]
    else:
        case_names = sorted(
            p.name
            for p in raw_root.iterdir()
            if p.is_dir() and any((p / f"{p.name}{s}").exists() or list(p.glob(f"*{s}")) for s in (".py", ".svg", ".stl"))
        )

    if not case_names:
        raise RuntimeError(f"No usable cases found under raw_root={raw_root}")
    if limit is not None:
        case_names = case_names[:limit]

    results: list[ReverseCaseResult] = []

    if max_workers <= 1 or len(case_names) == 1:
        for case_name in case_names:
            results.append(
                _infer_one_case(
                    case_name=case_name,
                    source_case_root=raw_root / case_name,
                    prompt_root=prompt_dir,
                    task_root=task_dir,
                    api_key=key,
                    base_url=base_url,
                    model=model,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    overwrite=overwrite,
                    system_prompt=system_prompt,
                    example_text=example_text,
                    render_only_cases=render_only_cases,
                )
            )
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _infer_one_case,
                case_name=case_name,
                source_case_root=raw_root / case_name,
                prompt_root=prompt_dir,
                task_root=task_dir,
                api_key=key,
                base_url=base_url,
                model=model,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                overwrite=overwrite,
                system_prompt=system_prompt,
                example_text=example_text,
                render_only_cases=render_only_cases,
            ): case_name
            for case_name in case_names
        }

        for future in concurrent.futures.as_completed(future_map):
            case_name = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    ReverseCaseResult(
                        case_name=case_name,
                        task_markdown="",
                        prompt_path="",
                        task_path="",
                        status="failed",
                        error=str(exc),
                    )
                )

    return results


def infer_rubrics_from_task_and_raw(
    *,
    raw_root: Path,
    task_root: Path,
    run_root: Path,
    rubric_root: Path | None = None,
    cases: list[str] | None = None,
    limit: int | None = None,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    base_url: str = DEFAULT_OPENROUTER_BASE_URL,
    model: str = DEFAULT_OPENROUTER_MODEL,
    temperature: float = 0.2,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    overwrite: bool = False,
    render_only_cases: set[str] | None = None,
) -> list[RubricCaseResult]:
    if api_key and not os.environ.get(api_key_env):
        os.environ[api_key_env] = api_key
    if not (key := os.environ.get(api_key_env) or api_key):
        raise RuntimeError(f"Missing OpenRouter API key; set {api_key_env} or pass --api-key.")

    raw_root = raw_root.resolve()
    task_root = task_root.resolve()
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw root does not exist: {raw_root}")
    if not task_root.exists():
        raise FileNotFoundError(f"Task root does not exist: {task_root}")

    prompt_dir = run_root / "rubric_prompts"
    effective_rubric_root = (rubric_root or (run_root / "rubrics")).resolve()
    prompt_dir.mkdir(parents=True, exist_ok=True)
    effective_rubric_root.mkdir(parents=True, exist_ok=True)

    prompt_module = _load_prompt_module(Path(__file__).resolve().parents[0] / "prompts" / "generate_rubrics_pair.py")
    system_prompt = str(getattr(prompt_module, "generate_rubrics_sp", "")).strip()
    if not system_prompt:
        raise RuntimeError("generate_rubrics_sp is empty. Please check prompts/generate_rubrics_pair.py")

    if cases:
        case_names = [name for name in cases if (task_root / name).is_dir()]
    else:
        case_names = sorted(
            p.name
            for p in task_root.iterdir()
            if p.is_dir() and (p / "task.md").exists()
        )
    if not case_names:
        raise RuntimeError(f"No usable task cases found under task_root={task_root}")
    if limit is not None:
        case_names = case_names[:limit]

    results: list[RubricCaseResult] = []

    if max_workers <= 1 or len(case_names) == 1:
        for case_name in case_names:
            results.append(
                _infer_one_rubric_case(
                    case_name=case_name,
                    source_case_root=raw_root / case_name,
                    prompt_root=prompt_dir,
                    rubric_root=effective_rubric_root,
                    task_root=task_root,
                    api_key=key,
                    base_url=base_url,
                    model=model,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    overwrite=overwrite,
                    system_prompt=system_prompt,
                    render_only_cases=render_only_cases,
                )
            )
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _infer_one_rubric_case,
                case_name=case_name,
                source_case_root=raw_root / case_name,
                prompt_root=prompt_dir,
                rubric_root=effective_rubric_root,
                task_root=task_root,
                api_key=key,
                base_url=base_url,
                model=model,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                overwrite=overwrite,
                system_prompt=system_prompt,
                render_only_cases=render_only_cases,
            ): case_name
            for case_name in case_names
        }

        for future in concurrent.futures.as_completed(future_map):
            case_name = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    RubricCaseResult(
                        case_name=case_name,
                        rubric_markdown="",
                        prompt_path="",
                        rubric_path="",
                        status="failed",
                        error=str(exc),
                    )
                )

    return results


def build_reverse_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reverse pipeline for CAD benchmark")
    parser.add_argument("--raw-root", required=True, help="Root folder containing per-case raw files (.py/.svg/.stl)")
    parser.add_argument("--run-id", default=None, help="Run folder name under results/")
    parser.add_argument("--run-root", default=None, help="Full run root directory; overrides --run-id")
    parser.add_argument("--api-key", default=DEFAULT_OPENROUTER_API_KEY, help="OpenRouter API key")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="Environment variable for API key")
    parser.add_argument("--model", default=DEFAULT_OPENROUTER_MODEL, help="OpenRouter model id")
    parser.add_argument("--base-url", default=DEFAULT_OPENROUTER_BASE_URL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--model-list", default=None, help="Optional path to custom model list (label|provider|model|api_key_env|base_url|temperature)")
    parser.add_argument("--alignment-model", default=DEFAULT_ALIGNMENT_MODEL, help="VLM model for alignment scoring")
    parser.add_argument("--alignment-timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout for VLM alignment scoring")
    parser.add_argument("--alignment-task", action="store_true", help="Deprecated alias: keep alignment judge enabled (default).")
    parser.add_argument("--no-alignment", action="store_true", help="Disable alignment scoring during benchmark")
    parser.add_argument("--case-limit", type=int, default=None)
    parser.add_argument("--cases", nargs="*", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-task", action="store_true", help="Skip task generation and only infer rubrics from existing task folder")
    parser.add_argument("--task-root", default=None, help="Optional existing task root (defaults to <run_root>/task)")
    parser.add_argument("--infer-rubrics", action="store_true", help="Also infer rubrics from raw files + task docs")
    parser.add_argument("--rubric-root", default=None, help="Optional existing rubric root (defaults to <run_root>/rubrics if infer-run) or set separately")
    parser.add_argument("--run-benchmark", action="store_true", help="After task/rubric inference, run code generation + render + scoring")
    parser.add_argument(
        "--include-rubric-in-prompt",
        action="store_true",
        help="Include rubric content in the benchmark CadQuery code prompt",
    )
    parser.add_argument("--no-viewer", action="store_true", help="Skip viewer export")
    parser.add_argument("--drawcad-root", default=str(DEFAULT_DRAWCAD_ROOT), help="DrawCAD converter root")
    parser.add_argument("--validator-root", default=str(DEFAULT_VALIDATOR_ROOT), help="Geometry validator root")
    parser.add_argument("--python-executable", default=str(DEFAULT_PYTHON_EXECUTABLE), help="Python executable for execution/rendering")
    parser.add_argument(
        "--render-only-list",
        default=str(DEFAULT_RENDER_ONLY_CASES_PATH),
        help="Path to a text file listing cases (one per line, '#' comments) for which "
             "every STP->SVG step is replaced by STP->3D render PNG (curved/organic geometry).",
    )
    return parser


def run_reverse_pipeline(
    *,
    raw_root: str,
    run_root: str | None = None,
    run_id: str | None = None,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    model: str = DEFAULT_OPENROUTER_MODEL,
    base_url: str = DEFAULT_OPENROUTER_BASE_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    temperature: float = 0.2,
    max_workers: int = DEFAULT_MAX_WORKERS,
    case_limit: int | None = None,
    cases: list[str] | None = None,
    overwrite: bool = False,
    skip_task: bool = False,
    task_root: str | None = None,
    infer_rubrics: bool = False,
    run_benchmark: bool = False,
    model_list: str | None = None,
    alignment_model: str = DEFAULT_ALIGNMENT_MODEL,
    alignment_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    rubric_root: str | None = None,
    no_viewer: bool = False,
    drawcad_root: str | None = None,
    validator_root: str | None = None,
    python_executable: str | None = None,
    run_alignment: bool = True,
    include_rubric_in_prompt: bool = False,
    render_only_list: str | None = None,
) -> dict[str, Any]:
    workspace = Path(__file__).resolve().parents[2]
    workspace_results = workspace / "results"
    workspace_results.mkdir(parents=True, exist_ok=True)

    def _resolve_optional_path(value: str | None, fallback: Path) -> Path:
        if value is None:
            return fallback
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        for base in (workspace_results, workspace, Path.cwd()):
            candidate_abs = (base / candidate).resolve()
            if candidate_abs.exists():
                return candidate_abs
        return candidate.resolve()

    def _dir_has_case_markdown(target_root: Path, filename: str) -> bool:
        if not target_root.exists():
            return False
        for child in target_root.iterdir():
            if child.is_dir() and (child / filename).exists():
                return True
        return False

    if run_root:
        provided = Path(run_root).expanduser()
        if provided.is_absolute():
            final_run_root = provided.resolve()
        else:
            final_run_root = (workspace_results / provided).resolve()
            if not final_run_root.exists() and (workspace / provided).exists():
                final_run_root = (workspace / provided).resolve()
            elif not final_run_root.exists():
                final_run_root = (Path.cwd() / provided).resolve()
    else:
        run_name = run_id or _now_tag()
        final_run_root = workspace_results / run_name

    task_dir = final_run_root / "task"
    prompt_dir = final_run_root / "task_prompts"
    drawcad_root_path = Path(drawcad_root) if drawcad_root is not None else DEFAULT_DRAWCAD_ROOT
    validator_root_path = Path(validator_root) if validator_root is not None else DEFAULT_VALIDATOR_ROOT
    python_executable_path = Path(python_executable) if python_executable is not None else DEFAULT_PYTHON_EXECUTABLE

    final_run_root.mkdir(parents=True, exist_ok=True)
    alignment_system_prompt = _load_score_system_prompt()
    render_only_cases = _load_render_only_cases(render_only_list)
    if render_only_cases:
        print(f"[render-only] {len(render_only_cases)} case(s) will use STP->3D render instead of SVG: "
              f"{', '.join(sorted(render_only_cases))}")

    benchmark_task_root = _resolve_optional_path(task_root, task_dir)
    benchmark_rubric_root = _resolve_optional_path(rubric_root, final_run_root / "rubrics")

    tasks_results: list[ReverseCaseResult]
    if skip_task or task_root is not None:
        tasks_results = []
    else:
        tasks_results = infer_design_books_from_raw(
            raw_root=Path(raw_root),
            run_root=final_run_root,
            cases=cases,
            limit=case_limit,
            api_key=api_key or "",
            api_key_env=api_key_env,
            base_url=base_url,
            model=model,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            max_workers=max_workers,
            overwrite=overwrite,
            render_only_cases=render_only_cases,
        )

    rubric_results: list[RubricCaseResult] = []
    need_rubric_inference = infer_rubrics or (
        run_benchmark and not _dir_has_case_markdown(benchmark_rubric_root, "rubric.md")
    )
    if need_rubric_inference:
        if not benchmark_task_root.exists():
            raise RuntimeError(f"Cannot infer rubrics: task root does not exist -> {benchmark_task_root}")
        rubric_results = infer_rubrics_from_task_and_raw(
            raw_root=Path(raw_root),
            task_root=benchmark_task_root,
            run_root=final_run_root,
            rubric_root=benchmark_rubric_root,
            cases=cases,
            limit=case_limit,
            api_key=api_key or "",
            api_key_env=api_key_env,
            base_url=base_url,
            model=model,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            max_workers=max_workers,
            overwrite=overwrite,
            render_only_cases=render_only_cases,
        )
        (final_run_root / "rubric_results.json").write_text(
            json.dumps([r.__dict__ for r in rubric_results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        for case_name in sorted(
            p.name
            for p in benchmark_task_root.iterdir()
            if p.is_dir() and (p / "task.md").exists()
        ):
            target = benchmark_rubric_root / case_name / "rubric.md"
            if target.exists():
                rubric_results.append(
                    RubricCaseResult(
                        case_name=case_name,
                        rubric_markdown=target.read_text(encoding="utf-8"),
                        prompt_path=str(benchmark_rubric_root / case_name / "rubric_prompt.json"),
                        rubric_path=str(target),
                        status="done",
                    )
                )

    benchmark_records: list[BenchmarkCaseResult] = []
    benchmark_rubric_rows: list[dict[str, Any]] = []
    if run_benchmark:
        if not benchmark_task_root.exists():
            raise RuntimeError(f"Task root does not exist for benchmark: {benchmark_task_root}")
        if not _dir_has_case_markdown(benchmark_rubric_root, "rubric.md"):
            raise RuntimeError(f"Rubric root missing or empty: {benchmark_rubric_root}")

        benchmark_records, benchmark_rubric_rows = _run_benchmark(
            raw_root=Path(raw_root).resolve(),
            task_root=benchmark_task_root,
            rubric_root=benchmark_rubric_root,
            run_root=final_run_root,
            cases=cases,
            limit=case_limit,
            api_key=api_key or "",
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            alignment_timeout_seconds=alignment_timeout_seconds,
            drawcad_root=drawcad_root_path,
            validator_root=validator_root_path,
            python_executable=python_executable_path,
            model_list=model_list,
            alignment_model=alignment_model,
            alignment_base_url=base_url,
            run_alignment=run_alignment,
            alignment_system_prompt=alignment_system_prompt,
            include_rubric_in_prompt=include_rubric_in_prompt,
            max_workers=max_workers,
            render_only_cases=render_only_cases,
        )

        _write_records(
            run_root=final_run_root,
            records=benchmark_records,
            rubric_rows=benchmark_rubric_rows,
        )
        if not no_viewer:
            build_viewer(
                final_run_root / "reports" / "records.json",
                final_run_root / "reports" / "rubric_catalog.json",
                final_run_root / "reports" / "viewer.html",
            )

    # write audit log
    (final_run_root / "reverse_results.json").write_text(
        json.dumps([r.__dict__ for r in tasks_results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (final_run_root / "benchmark_results.json").write_text(
        json.dumps([r.__dict__ for r in benchmark_records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    done = [r for r in tasks_results if r.status == "done"] + [r for r in rubric_results if r.status == "done"]
    return {
        "run_root": str(final_run_root),
        "status_summary": {
            "total_tasks": len(tasks_results),
            "done_tasks": len([r for r in tasks_results if r.status == "done"]),
            "skipped_tasks": len([r for r in tasks_results if r.status == "skipped"]),
            "failed_tasks": len([r for r in tasks_results if r.status == "failed"]),
            "total_rubrics": len(rubric_results),
            "done_rubrics": len([r for r in rubric_results if r.status == "done"]),
            "skipped_rubrics": len([r for r in rubric_results if r.status == "skipped"]),
            "failed_rubrics": len([r for r in rubric_results if r.status == "failed"]),
            "benchmark": {
                "total": len(benchmark_records),
                "sandbox_ok": len([r for r in benchmark_records if r.sandbox_ok]),
                "failed": len([r for r in benchmark_records if r.status == "failed"]),
                "skipped": len([r for r in benchmark_records if r.status == "skipped"]),
            },
            "done": len(done),
            "failed": len([r for r in tasks_results if r.status == "failed"]) + len([r for r in rubric_results if r.status == "failed"]),
            "skipped": len([r for r in tasks_results if r.status == "skipped"]) + len([r for r in rubric_results if r.status == "skipped"]),
        },
        "task_dir": str(task_dir),
        "prompt_dir": str(prompt_dir),
        "rubric_dir": str(benchmark_rubric_root),
        "rubric_prompt_dir": str(final_run_root / "rubric_prompts"),
        "benchmark_records": str(final_run_root / "reports" / "records.json"),
        "benchmark_summary": str(final_run_root / "reports" / "benchmark_summary.json"),
    }
