from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .config import load_config
from .pipeline import run_inferred_taskplan_pipeline, materialize_prompts, prepare, run_pipeline, smoke_test


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "default.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Judge system CLI")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare-data")
    subparsers.add_parser("smoke-test")
    prompts_parser = subparsers.add_parser("materialize-prompts")
    prompts_parser.add_argument("--run-id", default=None)
    prompts_parser.add_argument("--test-list", default=None)
    prompts_parser.add_argument("--model-list", default=None)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--mode", default="auto", choices=["auto", "manual"])
    run_parser.add_argument("--test-list", default=None)
    run_parser.add_argument("--model-list", default=None)
    run_parser.add_argument("--max-workers", type=int, default=1, help="Workers for benchmark generation/evaluation")

    inferred_parser = subparsers.add_parser("run-from-inferred")
    default_source_root = str((Path(__file__).resolve().parents[2] / "data" / "svg_py_data").resolve())
    inferred_parser.add_argument("--run-id", default=None)
    inferred_parser.add_argument(
        "--source-root",
        default=default_source_root,
        help="Root with CAD code/SVG inputs for reverse inference",
    )
    inferred_parser.add_argument("--example-root", default=None, help="Root with example task/plan markdown for few-shot prompts")
    inferred_parser.add_argument("--case-limit", type=int, default=10)
    inferred_parser.add_argument("--cases", nargs="*", default=None)
    inferred_parser.add_argument("--example-cases", nargs="*", default=None)
    inferred_parser.add_argument("--taskplan-model", default="deepseek/deepseek-chat-v3-0324")
    inferred_parser.add_argument("--taskplan-api-key-env", default="OPENROUTER_API_KEY")
    inferred_parser.add_argument("--taskplan-base-url", default="https://openrouter.ai/api/v1")
    inferred_parser.add_argument("--taskplan-temperature", type=float, default=0.2)
    inferred_parser.add_argument("--taskplan-timeout", type=int, default=240)
    inferred_parser.add_argument("--taskplan-max-workers", type=int, default=4)
    inferred_parser.add_argument("--taskplan-overwrite", action="store_true")
    inferred_parser.add_argument("--skip-step0", action="store_true", help="Disable asset precheck/repair (svg/py/stp)")
    inferred_parser.add_argument("--mode", default="auto", choices=["auto", "manual"])
    inferred_parser.add_argument("--max-workers", type=int, default=8, help="Workers for benchmark pipeline")
    inferred_parser.add_argument("--no-viewer", action="store_true", help="Skip building viewer.html")
    inferred_parser.add_argument("--test-list", default=None)
    inferred_parser.add_argument("--model-list", default=None)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(args.config)
    if getattr(args, "test_list", None):
        config = replace(config, test_list_path=Path(args.test_list))
    if getattr(args, "model_list", None):
        config = replace(config, model_list_path=Path(args.model_list))

    if args.command == "prepare-data":
        prepare(config)
        print(config.cleaned_data_root)
        return

    if args.command == "smoke-test":
        print(smoke_test(config))
        return

    if args.command == "materialize-prompts":
        print(materialize_prompts(config, requested_run_id=args.run_id))
        return

    if args.command == "run":
        print(run_pipeline(config, requested_run_id=args.run_id, generation_mode=args.mode, max_workers=args.max_workers))
        return

    if args.command == "run-from-inferred":
        result = run_inferred_taskplan_pipeline(
            config,
            requested_run_id=args.run_id,
            source_root=Path(args.source_root) if args.source_root else None,
            example_root=Path(args.example_root) if args.example_root else None,
            selected_cases=args.cases,
            case_limit=args.case_limit,
            example_cases=args.example_cases,
            taskplan_model=args.taskplan_model,
            taskplan_api_key_env=args.taskplan_api_key_env,
            taskplan_base_url=args.taskplan_base_url,
            taskplan_temperature=args.taskplan_temperature,
            taskplan_timeout_seconds=args.taskplan_timeout,
            taskplan_max_workers=args.taskplan_max_workers,
            taskplan_overwrite=args.taskplan_overwrite,
            generation_mode=args.mode,
            benchmark_max_workers=args.max_workers,
            build_viewer_html=not args.no_viewer,
            run_step0=not args.skip_step0,
        )
        print(result["run_root"])
        return


if __name__ == "__main__":
    main()
