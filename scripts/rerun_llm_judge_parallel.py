from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from src.judge_system.config import load_config
from src.judge_system.llm_judge import judge_with_vlm
from src.judge_system.pipeline import export_excel_report
from src.judge_system.report_viewer import build_viewer


def _load_markdown_pair(cleaned_root: Path, task_name: str) -> tuple[str, str]:
    task_text = (cleaned_root / task_name / "task.md").read_text(encoding="utf-8")
    plan_text = (cleaned_root / task_name / "plan.md").read_text(encoding="utf-8")
    return task_text, plan_text


def _write_csv_dicts(rows: list[dict], csv_path: Path) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            for row in rows:
                writer.writerow(row)


def _judge_one(record: dict, *, cleaned_root: Path, rubric_root: Path, model: str, api_key_env: str, base_url: str, timeout_seconds: int) -> dict:
    updated = dict(record)
    svg_ok = bool(updated.get("svg_path")) and bool(updated.get("png_path"))
    if not svg_ok:
        updated["llm_judge_score"] = 0.0
        updated["llm_judge_summary"] = ""
        updated["llm_judge_breakdown_json"] = "{}"
        updated["llm_judge_error"] = "SVG rendering failed; skipping LLM judge."
        updated["llm_judge_model"] = model
        return updated

    task_text, plan_text = _load_markdown_pair(cleaned_root, updated["task_name"])
    image_paths = [Path(updated["png_path"])]
    render_png_path = updated.get("render_png_path") or ""
    if render_png_path:
        image_paths.append(Path(render_png_path))

    payload = judge_with_vlm(
        api_key_env=api_key_env,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        rubric_path=rubric_root / f"{updated['task_name']}.json",
        task_text=task_text,
        plan_text=plan_text,
        image_paths=image_paths,
    )
    updated["llm_judge_model"] = model
    updated["llm_judge_score"] = float(payload.get("overall_score_normalized", 0.0) or 0.0)
    updated["llm_judge_summary"] = str(payload.get("overall_summary", "") or "")
    updated["llm_judge_breakdown_json"] = json.dumps(payload, ensure_ascii=False)
    updated["llm_judge_error"] = ""
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Rerun VLM judge in parallel for an existing run.")
    parser.add_argument("--config", required=True, help="Path to config json.")
    parser.add_argument("--run-id", required=True, help="Run id under results/.")
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel worker count for VLM requests.")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    run_root = config.results_root / args.run_id
    report_dir = run_root / "reports"
    records_path = report_dir / "records.json"
    rubric_catalog_path = report_dir / "rubric_catalog.json"
    if not records_path.exists():
        raise FileNotFoundError(f"Missing records.json: {records_path}")

    backup_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = report_dir / f"records.before_llm_rerun_{backup_tag}.json"
    shutil.copy2(records_path, backup_path)

    rows = json.loads(records_path.read_text(encoding="utf-8"))
    updated_rows: list[dict] = [None] * len(rows)  # type: ignore[assignment]

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_map = {
            executor.submit(
                _judge_one,
                row,
                cleaned_root=config.cleaned_data_root,
                rubric_root=config.resolve_llm_judge_rubric_root(),
                model=config.llm_judge_model,
                api_key_env=config.llm_judge_api_key_env,
                base_url=config.llm_judge_base_url,
                timeout_seconds=config.llm_judge_timeout_seconds,
            ): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                updated_rows[index] = future.result()
            except Exception as exc:
                row = dict(rows[index])
                row["llm_judge_model"] = config.llm_judge_model
                row["llm_judge_score"] = 0.0
                row["llm_judge_summary"] = ""
                row["llm_judge_breakdown_json"] = "{}"
                row["llm_judge_error"] = str(exc)
                updated_rows[index] = row

    records_path.write_text(json.dumps(updated_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv_dicts(updated_rows, report_dir / "records.csv")
    export_excel_report(config.workspace_root, run_root, config.excel_filename)
    build_viewer(records_path, rubric_catalog_path, report_dir / "viewer.html")

    success_count = sum(1 for row in updated_rows if not row.get("llm_judge_error") and float(row.get("llm_judge_score", 0.0) or 0.0) > 0)
    skipped_count = sum(1 for row in updated_rows if row.get("llm_judge_error") == "SVG rendering failed; skipping LLM judge.")
    print(f"Updated {len(updated_rows)} rows.")
    print(f"Backup: {backup_path}")
    print(f"LLM judge success count: {success_count}")
    print(f"LLM judge skipped count: {skipped_count}")
    print(report_dir / "viewer.html")


if __name__ == "__main__":
    main()
