from __future__ import annotations

from .reverse_pipeline import build_reverse_parser, run_reverse_pipeline


def main() -> None:
    parser = build_reverse_parser()
    args = parser.parse_args()

    result = run_reverse_pipeline(
        raw_root=args.raw_root,
        run_root=args.run_root,
        run_id=args.run_id,
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        model=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout,
        temperature=args.temperature,
        max_workers=args.max_workers,
        case_limit=args.case_limit,
        cases=args.cases,
        overwrite=args.overwrite,
        skip_task=args.skip_task,
        task_root=args.task_root,
        infer_rubrics=args.infer_rubrics,
        run_benchmark=args.run_benchmark,
        model_list=args.model_list,
        alignment_model=args.alignment_model,
        alignment_timeout_seconds=args.alignment_timeout,
        rubric_root=args.rubric_root,
        include_rubric_in_prompt=args.include_rubric_in_prompt,
        no_viewer=args.no_viewer,
        drawcad_root=args.drawcad_root,
        validator_root=args.validator_root,
        python_executable=args.python_executable,
        run_alignment=(not args.no_alignment) or args.alignment_task,
        render_only_list=args.render_only_list,
    )
    print(result)


if __name__ == "__main__":
    main()
