from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from tempfile import NamedTemporaryFile

from .config import ModelSpec
from .prompts import TASKPLAN_INFERENCE_PROMPT
from .providers import OpenRouterChatProvider
from .drawings import render_four_views, render_3d_preview

MAX_CODE_SNIPPET_CHARS = 12000
MAX_SVG_SNIPPET_CHARS = 12000


@dataclass(frozen=True)
class InferenceResult:
    case_name: str
    task_markdown: str
    plan_markdown: str
    rationale: str


@dataclass(frozen=True)
class InferenceJob:
    case_name: str
    target_root: Path
    overwrite: bool


@dataclass(frozen=True)
class CaseAssetFixReport:
    case_name: str
    py_path: Path
    svg_path: Path | None
    stp_path: Path | None
    generated_svg_paths: tuple[Path, ...]
    generated_stp_paths: tuple[Path, ...]
    errors: tuple[str, ...]


def _trim(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 80] + "\n... [truncated] ...\n"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _safe_json_loads(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        block_match = re.search(r"\{.*\}", text, flags=re.S)
        if not block_match:
            raise
        return json.loads(block_match.group(0))


def _primary_file(case_dir: Path, suffix: str, *alt_suffixes: str) -> Path | None:
    exact = case_dir / f"{case_dir.name}{suffix}"
    if exact.exists():
        return exact
    for candidate in sorted(case_dir.glob(f"*{suffix}")):
        return candidate
    for alt in alt_suffixes:
        exact_alt = case_dir / f"{case_dir.name}{alt}"
        if exact_alt.exists():
            return exact_alt
        for candidate in sorted(case_dir.glob(f"*{alt}")):
            return candidate
    return None


def _build_step_from_stp_script(stp_path: Path, code_path: Path) -> None:
    code_path.write_text(
        f"import cadquery as cq\nresult = cq.importers.importStep({str(stp_path)!r})\n",
        encoding="utf-8",
    )


def ensure_case_assets(
    case_dir: Path,
    case_name: str,
    *,
    drawcad_root: Path,
    python_executable: Path,
    paper_size: str,
    render_timeout_seconds: int,
    overwrite: bool = False,
) -> CaseAssetFixReport:
    py_path = _primary_file(case_dir, ".py")
    if py_path is None:
        raise RuntimeError(f"Case {case_name} missing .py in {case_dir}")

    svg_path = _primary_file(case_dir, ".svg")
    stp_path = _primary_file(case_dir, ".stp", ".step")
    generated_svgs: list[Path] = []
    generated_stps: list[Path] = []
    errors: list[str] = []

    if svg_path is None and stp_path is not None:
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as handle:
            tmp_code_path = Path(handle.name)
            _build_step_from_stp_script(stp_path, tmp_code_path)
        try:
            drawing = render_four_views(
                code_path=tmp_code_path,
                output_dir=case_dir,
                name=case_name,
                paper_size=paper_size,
                drawcad_root=drawcad_root,
                python_executable=python_executable,
                timeout_seconds=render_timeout_seconds,
            )
            if drawing.ok and drawing.svg_path is not None:
                svg_path = drawing.svg_path
                generated_svgs.append(drawing.svg_path)
            else:
                errors.append(f"svg_missing_and_stp_repair_failed:{case_name}:{drawing.error}")
        finally:
            tmp_code_path.unlink(missing_ok=True)

    if stp_path is None:
        rendered = render_3d_preview(
            code_path=py_path,
            output_dir=case_dir,
            name=case_name,
            python_executable=python_executable,
            timeout_seconds=render_timeout_seconds,
        )
        if rendered.ok and rendered.step_path is not None:
            candidate_stp_path = case_dir / f"{case_name}.stp"
            if rendered.step_path.suffix.lower() != ".stp":
                rendered.step_path.replace(candidate_stp_path)
            else:
                rendered.step_path.rename(candidate_stp_path)
            stp_path = candidate_stp_path
            generated_stps.append(candidate_stp_path)
            if svg_path is None:
                drawing = render_four_views(
                    code_path=py_path,
                    output_dir=case_dir,
                    name=case_name,
                    paper_size=paper_size,
                    drawcad_root=drawcad_root,
                    python_executable=python_executable,
                    timeout_seconds=render_timeout_seconds,
                )
                if drawing.ok and drawing.svg_path is not None:
                    svg_path = drawing.svg_path
                    generated_svgs.append(drawing.svg_path)
                else:
                    errors.append(f"stp_missing_and_svg_generation_failed:{case_name}:{drawing.error}")
        else:
            errors.append(f"stp_missing_and_repair_failed:{case_name}:{rendered.error}")

    return CaseAssetFixReport(
        case_name=case_name,
        py_path=py_path,
        svg_path=svg_path,
        stp_path=stp_path,
        generated_svg_paths=tuple(generated_svgs),
        generated_stp_paths=tuple(generated_stps),
        errors=tuple(errors),
    )


def infer_case_bundle(
    *,
    source_root: Path,
    target_root: Path,
    case_names: list[str],
    drawcad_root: Path,
    python_executable: Path,
    paper_size: str,
    render_timeout_seconds: int,
    overwrite: bool = False,
    max_workers: int = 4,
) -> tuple[list[str], list[CaseAssetFixReport]]:
    target_root.mkdir(parents=True, exist_ok=True)
    max_workers = max(1, max_workers)

    def _process_case(case_name: str) -> CaseAssetFixReport:
        source_case = source_root / case_name
        if not source_case.exists() or not source_case.is_dir():
            raise RuntimeError(f"Source case not found: {source_case}")
        copied_case_dir = target_root / case_name
        if copied_case_dir.exists():
            if overwrite:
                shutil.rmtree(copied_case_dir)
            else:
                raise RuntimeError(f"Target case exists and overwrite=False: {copied_case_dir}")
        shutil.copytree(source_case, copied_case_dir)
        return ensure_case_assets(
            copied_case_dir,
            case_name,
            drawcad_root=drawcad_root,
            python_executable=python_executable,
            paper_size=paper_size,
            render_timeout_seconds=render_timeout_seconds,
            overwrite=overwrite,
        )

    if max_workers == 1:
        reports = [_process_case(case_name) for case_name in case_names]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_process_case, case_name): case_name for case_name in case_names}
            reports = [None] * len(case_names)
            for future in as_completed(future_map):
                case_name = future_map[future]
                idx = case_names.index(case_name)
                reports[idx] = future.result()
    return case_names, [r for r in reports if r is not None]


def _load_examples_block(example_root: Path, example_cases: list[str]) -> str:
    if not example_cases:
        return "(no examples provided)"

    sections: list[str] = []
    for name in example_cases:
        case_dir = example_root / name
        task_path = case_dir / "task.md"
        plan_path = case_dir / "plan.md"
        if not task_path.exists() or not plan_path.exists():
            continue

        sections.append(
            "\n".join(
                [
                    f"### Example case: {name}",
                    "Task markdown:",
                    "```markdown",
                    _read_text(task_path).strip(),
                    "```",
                    "Plan markdown:",
                    "```markdown",
                    _read_text(plan_path).strip(),
                    "```",
                ]
            )
        )
    return "\n\n".join(sections) if sections else "(no usable examples found)"


def _build_prompt(case_name: str, source_root: Path, example_root: Path, example_cases: list[str]) -> str:
    case_dir = source_root / case_name
    code_path = _primary_file(case_dir, ".py")
    svg_path = _primary_file(case_dir, ".svg")
    if code_path is None or not code_path.exists():
        raise FileNotFoundError(f"No .py source found for case: {case_name}")

    code_text = _trim(_read_text(code_path), MAX_CODE_SNIPPET_CHARS)
    svg_text = _trim(_read_text(svg_path), MAX_SVG_SNIPPET_CHARS) if svg_path and svg_path.exists() else ""
    examples = _load_examples_block(example_root, example_cases)

    return (
        TASKPLAN_INFERENCE_PROMPT
        .replace("{examples}", examples)
        .replace("{case_name}", case_name)
        .replace("{code_text}", code_text)
        .replace("{svg_text}", svg_text)
    )


def _parse_response(raw: str) -> InferenceResult:
    payload = _safe_json_loads(raw)
    task_markdown = payload.get("task_markdown")
    plan_markdown = payload.get("plan_markdown")
    rationale = payload.get("rationale", "")
    case_name = payload.get("case_name", "")

    if not isinstance(task_markdown, str) or not isinstance(plan_markdown, str):
        raise ValueError("Response JSON must include task_markdown and plan_markdown as strings.")
    if not task_markdown.strip() or not plan_markdown.strip():
        raise ValueError("task_markdown or plan_markdown is empty.")
    return InferenceResult(
        case_name=str(case_name).strip() if case_name else "",
        task_markdown=task_markdown.strip() + "\n",
        plan_markdown=plan_markdown.strip() + "\n",
        rationale=str(rationale).strip(),
    )


def _write_bundle(job: InferenceJob, result: InferenceResult, source_case: Path) -> Path:
    job.target_root.mkdir(parents=True, exist_ok=True)
    if job.case_name and result.case_name and result.case_name != job.case_name:
        result_case_dir = job.target_root / result.case_name
        result_case_dir.mkdir(parents=True, exist_ok=True)
    else:
        result_case_dir = job.target_root

    task_path = result_case_dir / "task.md"
    plan_path = result_case_dir / "plan.md"
    if not job.overwrite and task_path.exists() and plan_path.exists():
        return result_case_dir

    task_path.write_text(result.task_markdown, encoding="utf-8")
    plan_path.write_text(result.plan_markdown, encoding="utf-8")

    notes = {
        "reason": result.rationale,
        "model": "openrouter_task_plan_inference",
        "source_case": str(source_case),
    }
    (result_case_dir / "task_plan_inference.json").write_text(
        json.dumps(notes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for suffix in (".py", ".svg", ".stp"):
        source_path = _primary_file(source_case, suffix)
        if source_path is not None and source_path.exists():
            shutil.copy2(source_path, result_case_dir / source_path.name)

    for thumb in ("thumbnail.png", "render.png"):
        source_thumb = source_case / thumb
        if source_thumb.exists():
            shutil.copy2(source_thumb, result_case_dir / source_thumb.name)

    return result_case_dir


def _infer_one_case(
    case_name: str,
    source_root: Path,
    example_root: Path,
    target_root: Path,
    provider: OpenRouterChatProvider,
    spec: ModelSpec,
    timeout_seconds: int,
    example_cases: list[str],
    overwrite: bool,
) -> Path:
    prompt = _build_prompt(case_name, source_root, example_root, example_cases)
    source_case = source_root / case_name
    if not source_case.exists():
        raise FileNotFoundError(f"Source case not found: {source_case}")

    case_target_root = target_root / case_name
    if not overwrite and (case_target_root / "task.md").exists() and (case_target_root / "plan.md").exists():
        return case_target_root

    response = provider.generate_text(
        system_prompt="Reconstruct CAD task and plan documents from code/svg evidence.",
        user_prompt=prompt,
        spec=spec,
        timeout_seconds=timeout_seconds,
    )
    result = _parse_response(response)
    final_case_name = result.case_name or case_name
    return _write_bundle(
        InferenceJob(case_name=final_case_name, target_root=case_target_root, overwrite=overwrite),
        result,
        source_case,
    )


def infer_task_plan_batch(
    *,
    source_root: Path,
    example_root: Path,
    target_root: Path,
    case_names: list[str],
    example_cases: list[str],
    model: str,
    api_key_env: str,
    base_url: str,
    temperature: float,
    timeout_seconds: int,
    max_workers: int,
    overwrite: bool,
) -> list[str]:
    provider = OpenRouterChatProvider()
    spec = ModelSpec(
        label="task_plan_inference",
        provider="openrouter",
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        temperature=temperature,
    )

    if not source_root.exists():
        raise FileNotFoundError(f"Source root does not exist: {source_root}")
    if not example_root.exists():
        raise FileNotFoundError(f"Example root does not exist: {example_root}")

    jobs = [name for name in case_names if (source_root / name).is_dir()]
    if not jobs:
        raise RuntimeError(f"No usable cases found in source root: {source_root}")

    if max_workers <= 1 or len(jobs) == 1:
        for name in jobs:
            _infer_one_case(
                case_name=name,
                source_root=source_root,
                example_root=example_root,
                target_root=target_root,
                provider=provider,
                spec=spec,
                timeout_seconds=timeout_seconds,
                example_cases=example_cases,
                overwrite=overwrite,
            )
        return jobs

    done: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _infer_one_case,
                name,
                source_root,
                example_root,
                target_root,
                provider,
                spec,
                timeout_seconds,
                example_cases,
                overwrite,
            ): name
            for name in jobs
        }
        for future in as_completed(future_map):
            case_name = future_map[future]
            try:
                future.result()
                done.append(case_name)
            except Exception as exc:
                raise RuntimeError(f"Failed to infer task/plan for {case_name}: {exc}") from exc
    return done
