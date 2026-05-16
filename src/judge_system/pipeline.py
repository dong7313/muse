from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import AppConfig, ModelSpec
from .data_prep import RubricItem, prepare_dataset
from .drawings import render_3d_preview, render_four_views
from .geometry_metrics import evaluate_geometry
from .llm_judge import judge_with_vlm
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .providers import ManualImportProvider, OpenAIResponsesProvider, OpenRouterChatProvider
from .rubric import (
    category_score_breakdown,
    expected_component_count_from_plan,
    primary_category_score_breakdown,
    score_rubric,
    weighted_rubric_score,
)
from .report_viewer import build_viewer
from .sandbox import execute_in_sandbox
from .svg_metrics import analyze_svg
from .task_plan_inference import infer_case_bundle, infer_task_plan_batch


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_name: str
    model_label: str
    model_name: str
    sample_index: int
    generation_mode: str
    code_path: str
    svg_path: str
    png_path: str
    render_png_path: str
    render_mesh_path: str
    render_step_path: str
    code_valid: bool
    geometry_valid: bool
    sandbox_ok: bool
    sandbox_error: str
    geometry_issue_summary: str
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
    normal_consistency: bool | None
    volume_valid: bool | None
    bbox_valid: bool | None
    occt_valid: bool | None
    rubric_score: float
    rubric_breakdown_json: str
    rubric_primary_breakdown_json: str
    rubric_category_breakdown_json: str
    llm_judge_model: str
    llm_judge_score: float
    llm_judge_summary: str
    llm_judge_breakdown_json: str
    llm_judge_error: str


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_markdown_pair(cleaned_root: Path, task_name: str) -> tuple[str, str]:
    task_text = (cleaned_root / task_name / "task.md").read_text(encoding="utf-8")
    plan_text = (cleaned_root / task_name / "plan.md").read_text(encoding="utf-8")
    return task_text, plan_text


def _load_rubrics(rubric_root: Path, task_name: str) -> list[RubricItem]:
    payload = json.loads((rubric_root / f"{task_name}.json").read_text(encoding="utf-8"))
    if "active_rubrics" in payload:
        items: list[RubricItem] = []
        for rubric in payload["active_rubrics"]:
            specific_case_requirements = rubric.get("specific_case_requirements", {})
            if isinstance(specific_case_requirements, dict):
                requirement_items = list(specific_case_requirements.get("扣分点", []))
            else:
                requirement_items = list(specific_case_requirements)
            deduction_rules = rubric.get("deduction_rules")
            if deduction_rules is None:
                deduction_rules = [
                    {
                        "rule_code": entry.get("rule_code", ""),
                        "trigger": entry.get("rubric", "") or entry.get("problem", ""),
                        "deduction_ratio": (float(entry.get("deduct_score", 0.0) or 0.0) / float(rubric.get("max_points", 0.0) or 1.0)),
                        "evaluation_hint": entry.get("evaluation_hint", ""),
                        "evidence_keys": entry.get("evidence_keys", []),
                    }
                    for entry in requirement_items
                    if isinstance(entry, dict)
                ]
            items.append(
                RubricItem(
                    item_id=rubric.get("rubric_id") or rubric.get("title", ""),
                    title=rubric.get("title", ""),
                    description=rubric.get("objective", ""),
                    raw_weight=float(rubric.get("max_points", 0.0) or 0.0),
                    normalized_weight=float(rubric.get("normalized_weight", 0.0) or 0.0),
                    source="rubric.json",
                    primary_category=rubric.get("primary_category", ""),
                    secondary_category=rubric.get("secondary_category", ""),
                    max_points=float(rubric.get("max_points", 0.0) or 0.0),
                    scoring_method=rubric.get("scoring_method", ""),
                    score_bands=tuple(rubric.get("score_bands", [])),
                    deduction_rules=tuple(deduction_rules),
                )
            )
        return items
    if "taxonomy_rubric" in payload:
        applicable_items: list[dict] = []
        for primary in payload["taxonomy_rubric"]:
            for secondary in primary.get("secondary_categories", []):
                if secondary.get("applicable"):
                    applicable_items.append(
                        {
                            "item_id": secondary.get("secondary_category_en", ""),
                            "title": secondary.get("rubric_title", secondary.get("secondary_category_en", "")),
                            "description": secondary.get("evaluation_objective", ""),
                            "primary_category": primary.get("primary_category_en", ""),
                            "secondary_category": secondary.get("secondary_category_en", ""),
                        }
                    )
        if not applicable_items:
            return []
        weight = 1.0 / len(applicable_items)
        return [
            RubricItem(
                item_id=item["item_id"],
                title=item["title"],
                description=item["description"],
                raw_weight=1.0,
                normalized_weight=weight,
                source="rubric.taxonomy_rubric",
                primary_category=item["primary_category"],
                secondary_category=item["secondary_category"],
                max_points=1.0,
                scoring_method="taxonomy_fallback",
                score_bands=(),
                deduction_rules=(),
            )
            for item in applicable_items
        ]
    return [RubricItem(**item) for item in payload["items"]]


def prepare(
    config: AppConfig,
    *,
    cleaned_root: Path | None = None,
    rubric_root: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, list[RubricItem]]:
    cleaned_root = cleaned_root or config.cleaned_data_root
    rubric_root = rubric_root or config.rubric_root
    return prepare_dataset(source_root or config.source_root, cleaned_root, rubric_root)


def _resolve_case_list(cases_root: Path, selected_cases: list[str] | None, limit: int | None) -> list[str]:
    def has_cad_asset(path: Path) -> bool:
        return path.is_dir() and path.name != "__pycache__" and any(path.glob("*.py")) and any(path.glob("*.svg"))

    all_cases = sorted(path.name for path in cases_root.iterdir() if has_cad_asset(path))
    if selected_cases:
        selected = [case for case in selected_cases if has_cad_asset(cases_root / case)]
        if not selected:
            raise RuntimeError(f"No valid cases found in {cases_root} from provided names: {selected_cases}")
        all_cases = selected
    if limit is not None:
        all_cases = all_cases[:limit]
    return all_cases


def _resolve_case_list_for_step0(cases_root: Path, selected_cases: list[str] | None, limit: int | None) -> list[str]:
    def has_py_asset(path: Path) -> bool:
        return path.is_dir() and path.name != "__pycache__" and any(path.glob("*.py"))

    all_cases = sorted(path.name for path in cases_root.iterdir() if has_py_asset(path))
    if selected_cases:
        selected = [case for case in selected_cases if has_py_asset(cases_root / case)]
        if not selected:
            raise RuntimeError(f"No valid cases found in {cases_root} from provided names: {selected_cases}")
        all_cases = selected
    if limit is not None:
        all_cases = all_cases[:limit]
    return all_cases


def _write_code(output_dir: Path, code: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    code_path = output_dir / "code.py"
    code_path.write_text(code, encoding="utf-8")
    return code_path


def _make_run_root(config: AppConfig, requested_run_id: Optional[str], overwrite: bool = False) -> Path:
    run_id = requested_run_id or _now_tag()
    run_root = config.results_root / run_id
    if not overwrite:
        suffix = 1
        while run_root.exists():
            run_root = config.results_root / f"{run_id}_{suffix:03d}"
            suffix += 1
    (run_root / "runs").mkdir(parents=True, exist_ok=True)
    (run_root / "reports").mkdir(parents=True, exist_ok=True)
    return run_root


def _build_provider_map(config: AppConfig, manual_root: Path) -> dict[str, object]:
    return {
        "openai": OpenAIResponsesProvider(),
        "openrouter": OpenRouterChatProvider(),
        "manual": ManualImportProvider(manual_root),
    }


def materialize_prompts(
    config: AppConfig,
    requested_run_id: Optional[str] = None,
    *,
    cleaned_root: Path | None = None,
    rubric_root: Path | None = None,
    source_root: Path | None = None,
) -> Path:
    effective_cleaned_root = cleaned_root or config.cleaned_data_root
    effective_rubric_root = rubric_root or config.rubric_root
    effective_source_root = source_root or config.source_root
    prepare(
        config,
        cleaned_root=effective_cleaned_root,
        rubric_root=effective_rubric_root,
        source_root=effective_source_root,
    )
    run_root = _make_run_root(config, requested_run_id)
    manual_root = run_root / "manual_inputs"
    provider = ManualImportProvider(manual_root)
    selected_tasks = config.resolve_selected_tasks()
    models = config.resolve_models()

    for task_name in selected_tasks:
        task_text, plan_text = _load_markdown_pair(effective_cleaned_root, task_name)
        rubric_items = _load_rubrics(effective_rubric_root, task_name)
        for model in models:
            for sample_index in range(1, config.samples_per_task + 1):
                prompt_text = build_user_prompt(task_text, plan_text)
                provider.write_prompt_bundle(
                    task_name,
                    model.label,
                    sample_index,
                    SYSTEM_PROMPT,
                    prompt_text,
                )

    return run_root


def _run_one_sample(
    config: AppConfig,
    run_root: Path,
    run_id: str,
    task_name: str,
    task_text: str,
    plan_text: str,
    gt_component_count: int,
    rubric_items: list[RubricItem],
    model: ModelSpec,
    provider: object,
    manual_provider: ManualImportProvider,
    generation_mode: str,
    sample_index: int,
) -> RunRecord:
    sample_root = run_root / "runs" / task_name / model.label / f"sample_{sample_index}"
    prompt_text = build_user_prompt(task_text, plan_text)
    provider_name = model.provider if generation_mode == "auto" else generation_mode

    try:
        if provider_name == "manual":
            code = manual_provider.load_code(task_name, model.label, sample_index)
        elif provider_name in {"openai", "openrouter"} and isinstance(provider, (OpenAIResponsesProvider, OpenRouterChatProvider)):  # type: ignore
            if provider.is_available(model):  # type: ignore[union-attr]
                code = provider.generate_code(  # type: ignore[union-attr]
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prompt_text,
                    spec=model,
                    timeout_seconds=config.generation_timeout_seconds,
                )
            else:
                prompt_path = manual_provider.write_prompt_bundle(  # type: ignore[union-attr]
                    task_name,
                    model.label,
                    sample_index,
                    SYSTEM_PROMPT,
                    prompt_text,
                )
                raise RuntimeError(
                    f"{model.api_key_env} not found. Prompt bundle created at {prompt_path}. "
                    "Use chat/manual mode to fill sample_*.py, then rerun with --mode manual."
                )
        else:
            raise RuntimeError(f"Unsupported generation mode: {provider_name}")

        code = code[: config.max_code_chars]
        code_path = _write_code(sample_root, code)
        sandbox = execute_in_sandbox(code, config.execution_timeout_seconds, config.python_executable)
        geometry = evaluate_geometry(
            code,
            config.validator_root,
            config.python_executable,
            config.execution_timeout_seconds,
        )
        drawing = render_four_views(
            code_path,
            sample_root / "drawing",
            f"{task_name}_{model.label}_{sample_index}",
            config.paper_size,
            config.drawcad_root,
            config.python_executable,
            config.generation_timeout_seconds,
        )
        model_render = render_3d_preview(
            code_path,
            sample_root / "render",
            f"{task_name}_{model.label}_{sample_index}",
            config.python_executable,
            config.generation_timeout_seconds,
        )

        if not drawing.ok or drawing.svg_path is None:
            svg_metrics = type("SvgPlaceholder", (), {"estimated_component_count": 0, "total_path_count": 0})
            svg_path = ""
            png_path = ""
        else:
            svg_metrics = analyze_svg(drawing.svg_path)
            svg_path = str(drawing.svg_path)
            png_path = str(drawing.png_path or "")
        render_png_path = str(model_render.png_path or "") if model_render.ok else ""
        render_mesh_path = str(model_render.mesh_path or "") if model_render.ok else ""
        render_step_path = str(model_render.step_path or "") if model_render.ok else ""
        combined_error = " | ".join(part for part in [sandbox.error, drawing.error, model_render.error] if part)

        rubric_scores = score_rubric(
            rubric_items,
            geometry=geometry,
            sandbox=sandbox,
            svg=svg_metrics,
            task_text=task_text,
            plan_text=plan_text,
        )
        component_actual = sandbox.solid_count or getattr(svg_metrics, "estimated_component_count", 0)
        component_count_delta = component_actual - gt_component_count
        primary_breakdown = primary_category_score_breakdown(rubric_scores)
        category_breakdown = category_score_breakdown(rubric_scores)
        llm_judge_payload: dict | None = None
        llm_judge_error = ""
        if config.llm_judge_enabled:
            try:
                if not drawing.ok or drawing.svg_path is None or drawing.png_path is None:
                    raise RuntimeError("SVG rendering failed; skipping LLM judge.")
                image_paths = [path for path in [drawing.png_path, model_render.png_path] if path is not None]
                if not image_paths:
                    raise RuntimeError("No rendered images available for VLM judging.")
                llm_judge_payload = judge_with_vlm(
                    api_key_env=config.llm_judge_api_key_env,
                    base_url=config.llm_judge_base_url,
                    model=config.llm_judge_model,
                    timeout_seconds=config.llm_judge_timeout_seconds,
                    rubric_path=config.resolve_llm_judge_rubric_root() / f"{task_name}.json",
                    task_text=task_text,
                    plan_text=plan_text,
                    image_paths=image_paths,
                )
            except Exception as exc:
                llm_judge_error = str(exc)
        llm_judge_score = float((llm_judge_payload or {}).get("overall_score_normalized", 0.0) or 0.0)
        llm_judge_summary = str((llm_judge_payload or {}).get("overall_summary", "") or "")
        llm_judge_breakdown_json = json.dumps(llm_judge_payload or {}, ensure_ascii=False)

        return RunRecord(
            run_id=run_id,
            task_name=task_name,
            model_label=model.label,
            model_name=model.model,
            sample_index=sample_index,
            generation_mode=provider_name,
            code_path=str(code_path),
            svg_path=svg_path,
            png_path=png_path,
            render_png_path=render_png_path,
            render_mesh_path=render_mesh_path,
            render_step_path=render_step_path,
            code_valid=geometry.code_valid,
            geometry_valid=geometry.geometry_valid,
            sandbox_ok=sandbox.ok,
            sandbox_error=combined_error,
            geometry_issue_summary=geometry.issue_summary,
            result_solid_count=sandbox.solid_count,
            bbox_dx_mm=sandbox.bbox[0],
            bbox_dy_mm=sandbox.bbox[1],
            bbox_dz_mm=sandbox.bbox[2],
            gt_component_count=gt_component_count,
            svg_component_count_estimate=getattr(svg_metrics, "estimated_component_count", 0),
            component_count_match=(component_actual == gt_component_count),
            component_count_delta=component_count_delta,
            svg_path_count=getattr(svg_metrics, "total_path_count", 0),
            watertight=geometry.watertight,
            self_intersection_free=geometry.self_intersection_free,
            normal_consistency=geometry.normal_consistency,
            volume_valid=geometry.volume_valid,
            bbox_valid=geometry.bbox_valid,
            occt_valid=geometry.occt_valid,
            rubric_score=weighted_rubric_score(rubric_scores),
            rubric_breakdown_json=json.dumps([asdict(item) for item in rubric_scores], ensure_ascii=False),
            rubric_primary_breakdown_json=json.dumps(primary_breakdown, ensure_ascii=False),
            rubric_category_breakdown_json=json.dumps(category_breakdown, ensure_ascii=False),
            llm_judge_model=config.llm_judge_model if config.llm_judge_enabled else "",
            llm_judge_score=llm_judge_score,
            llm_judge_summary=llm_judge_summary,
            llm_judge_breakdown_json=llm_judge_breakdown_json,
            llm_judge_error=llm_judge_error,
        )
    except Exception as exc:
        return RunRecord(
            run_id=run_id,
            task_name=task_name,
            model_label=model.label,
            model_name=model.model,
            sample_index=sample_index,
            generation_mode=provider_name,
            code_path=str(sample_root / "code.py") if (sample_root / "code.py").exists() else "",
            svg_path="",
            png_path="",
            render_png_path="",
            render_mesh_path="",
            render_step_path="",
            code_valid=False,
            geometry_valid=False,
            sandbox_ok=False,
            sandbox_error=str(exc),
            geometry_issue_summary="",
            result_solid_count=0,
            bbox_dx_mm=0.0,
            bbox_dy_mm=0.0,
            bbox_dz_mm=0.0,
            gt_component_count=gt_component_count,
            svg_component_count_estimate=0,
            component_count_match=False,
            component_count_delta=-gt_component_count,
            svg_path_count=0,
            watertight=None,
            self_intersection_free=None,
            normal_consistency=None,
            volume_valid=None,
            bbox_valid=None,
            occt_valid=None,
            rubric_score=0.0,
            rubric_breakdown_json="[]",
            rubric_primary_breakdown_json="[]",
            rubric_category_breakdown_json="[]",
            llm_judge_model=config.llm_judge_model if config.llm_judge_enabled else "",
            llm_judge_score=0.0,
            llm_judge_summary="",
            llm_judge_breakdown_json="{}",
            llm_judge_error="",
        )


def run_pipeline(
    config: AppConfig,
    requested_run_id: Optional[str] = None,
    generation_mode: str = "auto",
    *,
    run_root: Path | None = None,
    selected_tasks: list[str] | None = None,
    cleaned_root: Path | None = None,
    rubric_root: Path | None = None,
    max_workers: int = 1,
    skip_prepare: bool = False,
    build_viewer_html: bool = False,
    overwrite: bool = False,
) -> Path:
    working_cleaned_root = cleaned_root or config.cleaned_data_root
    working_rubric_root = rubric_root or config.rubric_root
    if not skip_prepare:
        prepare(
            config,
            cleaned_root=working_cleaned_root,
            rubric_root=working_rubric_root,
            source_root=working_cleaned_root,
        )

    run_root = run_root or _make_run_root(config, requested_run_id, overwrite=overwrite)
    (run_root / "runs").mkdir(parents=True, exist_ok=True)
    (run_root / "reports").mkdir(parents=True, exist_ok=True)
    run_id = run_root.name
    manual_root = run_root / "manual_inputs"
    providers = _build_provider_map(config, manual_root)
    manual_provider = providers["manual"]
    models = config.resolve_models()
    records: list[RunRecord] = []
    rubric_catalog_rows: list[dict] = []

    task_names = selected_tasks or config.resolve_selected_tasks()
    jobs = []
    for task_name in task_names:
        task_text, plan_text = _load_markdown_pair(working_cleaned_root, task_name)
        rubric_items = _load_rubrics(working_rubric_root, task_name)
        gt_component_count = expected_component_count_from_plan(plan_text)
        rubric_catalog_rows.extend(
            {
                "task_name": task_name,
                "item_id": item.item_id,
                "title": item.title,
                "description": item.description,
                "primary_category": item.primary_category,
                "secondary_category": item.secondary_category,
                "raw_weight": item.raw_weight,
                "normalized_weight": item.normalized_weight,
                "max_points": item.max_points,
                "scoring_method": item.scoring_method,
                "source": item.source,
            }
            for item in rubric_items
        )
        for model in models:
            provider_name = model.provider if generation_mode == "auto" else generation_mode
            provider = providers[provider_name]
            for sample_index in range(1, config.samples_per_task + 1):
                jobs.append(
                    (
                        task_name,
                        task_text,
                        plan_text,
                        gt_component_count,
                        rubric_items,
                        model,
                        provider,
                        provider_name,
                        sample_index,
                    )
                )

    if not jobs:
        raise RuntimeError("No runnable jobs for pipeline.")

    if max_workers <= 1:
        for item in jobs:
            (
                task_name,
                task_text,
                plan_text,
                gt_component_count,
                rubric_items,
                model,
                provider,
                provider_name,
                sample_index,
            ) = item
            records.append(
                _run_one_sample(
                    config=config,
                    run_root=run_root,
                    run_id=run_id,
                    task_name=task_name,
                    task_text=task_text,
                    plan_text=plan_text,
                    gt_component_count=gt_component_count,
                    rubric_items=rubric_items,
                    model=model,
                    provider=provider,
                    manual_provider=manual_provider,
                    generation_mode=generation_mode,
                    sample_index=sample_index,
                )
            )
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _run_one_sample,
                    config=config,
                    run_root=run_root,
                    run_id=run_id,
                    task_name=task_name,
                    task_text=task_text,
                    plan_text=plan_text,
                    gt_component_count=gt_component_count,
                    rubric_items=rubric_items,
                    model=model,
                    provider=provider,
                    manual_provider=manual_provider,
                    generation_mode=generation_mode,
                    sample_index=sample_index,
                ): task_name
                for (
                    task_name,
                    task_text,
                    plan_text,
                    gt_component_count,
                    rubric_items,
                    model,
                    provider,
                    _provider_name,
                    sample_index,
                ) in jobs
            }
            for future in as_completed(futures):
                records.append(future.result())

    _flush_reports(run_root, records, rubric_catalog_rows, config.workspace_root, config.excel_filename)
    if build_viewer_html:
        build_viewer(
            run_root / "reports" / "records.json",
            run_root / "reports" / "rubric_catalog.json",
            run_root / "reports" / "viewer.html",
        )
    return run_root


def run_inferred_taskplan_pipeline(
    config: AppConfig,
    requested_run_id: Optional[str] = None,
    *,
    source_root: Path | None = None,
    example_root: Path | None = None,
    selected_cases: list[str] | None = None,
    case_limit: int = 10,
    example_cases: list[str] | None = None,
    taskplan_model: str = "deepseek/deepseek-chat-v3-0324",
    taskplan_api_key_env: str = "OPENROUTER_API_KEY",
    taskplan_base_url: str = "https://openrouter.ai/api/v1",
    taskplan_temperature: float = 0.2,
    taskplan_timeout_seconds: int = 240,
    taskplan_max_workers: int = 4,
    taskplan_overwrite: bool = False,
    generation_mode: str = "auto",
    benchmark_max_workers: int = 8,
    build_viewer_html: bool = True,
    overwrite: bool = False,
    run_step0: bool = True,
) -> dict[str, object]:
    working_source_root = source_root or config.source_root
    working_example_root = example_root or config.cleaned_data_root
    cases = (
        _resolve_case_list_for_step0(working_source_root, selected_cases, case_limit)
        if run_step0
        else _resolve_case_list(working_source_root, selected_cases, case_limit)
    )
    run_root = _make_run_root(config, requested_run_id, overwrite=overwrite)
    prepared_source_root = working_source_root
    step0_new_svgs: list[str] = []
    step0_new_stps: list[str] = []
    if run_step0:
        prepared_source_root = run_root / "prepared_source"
        _, step0_reports = infer_case_bundle(
            source_root=working_source_root,
            target_root=prepared_source_root,
            case_names=cases,
            drawcad_root=config.drawcad_root,
            python_executable=config.python_executable,
            paper_size=config.paper_size,
            render_timeout_seconds=config.generation_timeout_seconds,
            overwrite=True,
        )
        step0_new_svgs = sorted(
            [str(p) for report in step0_reports for p in report.generated_svg_paths]
        )
        step0_new_stps = sorted(
            [str(p) for report in step0_reports for p in report.generated_stp_paths]
        )
        (run_root / "step0_report.json").write_text(
            json.dumps(
                {
                    "source_root": str(working_source_root),
                    "prepared_source_root": str(prepared_source_root),
                    "case_count": len(cases),
                    "generated_svg_paths": step0_new_svgs,
                    "generated_stp_paths": step0_new_stps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    inferred_root = run_root / "inferred"
    inferred_rubric_root = run_root / "rubrics"
    inferred_rubric_zh_root = run_root / "rubrics_zh"

    inferred_cases = infer_task_plan_batch(
        source_root=prepared_source_root,
        example_root=working_example_root,
        target_root=inferred_root,
        case_names=cases,
        example_cases=example_cases or ["chair_3"],
        model=taskplan_model,
        api_key_env=taskplan_api_key_env,
        base_url=taskplan_base_url,
        temperature=taskplan_temperature,
        timeout_seconds=taskplan_timeout_seconds,
        max_workers=taskplan_max_workers,
        overwrite=taskplan_overwrite,
    )

    if not inferred_cases:
        raise RuntimeError(f"No cases were inferred from source root: {working_source_root}")

    generator = Path(__file__).resolve().parents[2] / "generate_taxonomy_rubrics.py"
    command = [
        sys.executable,
        str(generator),
        "--cleaned-root",
        str(inferred_root),
        "--rubric-root",
        str(inferred_rubric_root),
        "--rubric-zh-root",
        str(inferred_rubric_zh_root),
        "--only",
        *inferred_cases,
    ]
    if taskplan_overwrite:
        command.append("--overwrite")
    subprocess.run(command, check=True)

    benchmark_root = run_pipeline(
        config,
        generation_mode=generation_mode,
        run_root=run_root,
        selected_tasks=inferred_cases,
        cleaned_root=inferred_root,
        rubric_root=inferred_rubric_root,
        max_workers=benchmark_max_workers,
        skip_prepare=True,
        build_viewer_html=build_viewer_html,
        overwrite=True,
    )

    return {
        "run_root": str(benchmark_root),
        "run_id": benchmark_root.name,
        "cases": inferred_cases,
        "prepared_source_root": str(prepared_source_root),
        "step0_new_svgs": step0_new_svgs,
        "step0_new_stps": step0_new_stps,
        "inferred_root": str(inferred_root),
        "rubric_root": str(inferred_rubric_root),
    }


def _write_csv(records: list[RunRecord], csv_path: Path) -> None:
    import csv

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(records[0]).keys()) if records else [])
        if records:
            writer.writeheader()
            for item in records:
                writer.writerow(asdict(item))


def _flush_reports(run_root: Path, records: list[RunRecord], rubric_catalog_rows: list[dict], workspace_root: Path, excel_filename: str) -> None:
    records_path = run_root / "reports" / "records.json"
    records_path.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")
    (run_root / "reports" / "rubric_catalog.json").write_text(
        json.dumps(rubric_catalog_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_csv = run_root / "reports" / "records.csv"
    _write_csv(records, summary_csv)
    if records:
        export_excel_report(workspace_root, run_root, excel_filename)


def export_excel_report(workspace_root: Path, run_root: Path, filename: str) -> Path:
    script_path = workspace_root / "export_excel.mjs"
    output_path = run_root / "reports" / filename
    records_json = run_root / "reports" / "records.json"
    rubric_catalog_json = run_root / "reports" / "rubric_catalog.json"
    subprocess_env = os.environ.copy()
    import shutil
    node_path = os.environ.get("NODE_BIN") or shutil.which("node") or "node"
    subprocess.run(
        [node_path, str(script_path), str(records_json), str(rubric_catalog_json), str(output_path)],
        check=True,
        env=subprocess_env,
    )
    return output_path


def smoke_test(config: AppConfig) -> Path:
    run_root = _make_run_root(config, "smoke_test")
    code = """import cadquery as cq\nresult = cq.Workplane(\"XY\").box(60, 40, 30)\n"""
    code_path = _write_code(run_root / "runs" / "smoke" / "box" / "sample_1", code)
    drawing = render_four_views(
        code_path,
        code_path.parent / "drawing",
        "smoke_box",
        config.paper_size,
        config.drawcad_root,
        config.python_executable,
        config.generation_timeout_seconds,
    )
    if not drawing.ok:
        raise RuntimeError(drawing.error)
    return drawing.svg_path
