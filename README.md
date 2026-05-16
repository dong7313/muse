# MUSE Benchmark

Text-to-CAD benchmark: reverse-infer task description and evaluation rubric from raw CAD assets, generate candidate CadQuery code from multiple LLMs, then judge with LLM/VLM scorers.

## Local Setup
```
conda create -n muse python=3.10
conda activate muse
pip install -e .
brew install librsvg          # Linux: apt-get install librsvg2-bin
export OPENROUTER_API_KEY=...  # required for all LLM calls
```

External modules (DrawCAD + validator) must be placed under `external/` (or pointed at via `DRAWCAD_ROOT` / `VALIDATOR_ROOT` env vars).

## Data preparation
Each case under `$RAW_ROOT/<case>/` should contain:
* `<case>.py` — ground-truth CadQuery source
* `<case>.step` — STEP from executing the source
* `<case>.svg` — 4-view drawing rendered by DrawCAD
* `<case>_stp_render.png` — VTK 3D render thumbnail

The 9 organic-vase cases listed in `src/judge_system/render_only_cases.txt` use the 3D render PNG instead of the 4-view SVG everywhere (their curved surfaces are not captured by edge projection).

## Step 1 — Task inference (`reverse_cli.py`)
Reverse-infer English `task.md` design specs from `<case>.py` + `<case>.svg` (or `<case>_stp_render.png` for render-only cases).
```ruby
python -m src.judge_system.reverse_cli \
    --raw-root  results/raw_new_step0/prepared_source \
    --run-root  results/reverse_step1 \
    --model     google/gemini-3.1-pro-preview \
    --max-workers 8
```
Output: `results/reverse_step1/task/<case>/task.md`

## Step 2 — Rubric inference (`reverse_cli.py --infer-rubrics`)
Reuse the Step 1 tasks and infer 6-category evaluation rubrics. `--task-root` makes Step 1 short-circuit (no regeneration).
```ruby
python -m src.judge_system.reverse_cli \
    --raw-root    results/raw_new_step0/prepared_source \
    --run-root    results/reverse_step2_rubric \
    --task-root   results/reverse_step1/task \
    --infer-rubrics \
    --no-alignment \
    --max-workers 8
```
Output: `results/reverse_step2_rubric/<case>.md` (the rubric file is `<case>.md`, **not** `<case>.prompt.md` — the `.prompt.md` is the prompt artifact.)

## Step 3 — Candidate generation (`reverse_cli.py --run-benchmark`)
For every model in `model_list.txt`, generate CadQuery code, sandbox-execute → STEP, then render candidate SVG/PNG.
```ruby
python -m src.judge_system.reverse_cli \
    --raw-root    results/raw_new_step0/prepared_source \
    --run-root    results/bench_step3 \
    --task-root   results/reverse_step1/task \
    --rubric-root results/reverse_step2_rubric \
    --run-benchmark \
    --model-list  model_list.txt \
    --max-workers 8
```
* `model_list.txt` format per line: `label | provider | model | api_key_env | base_url | temperature`

Output per sample under `results/bench_step3/runs/<case>/<model>/sample_<n>/`:
* `code.py`, `sample.step`, `drawing/sample.svg`, `render/*.png`, `geometry_metrics.json`
* `results/bench_step3/reports/records.json` — aggregated rows for every (case, model, sample)

## Step 4 — LLM/VLM judge (`rerun_llm_judge_parallel.py`)
Read Step 3 artifacts + Step 2 rubrics, score with an LLM judge (Gemini 3.1 Pro in the paper). No sandbox runs here.
```ruby
python scripts/rerun_llm_judge_parallel.py \
    --config  configs/openrouter_eval.json \
    --run-id  bench_step3 \
    --max-workers 8
```
Output: `score.json` next to each `sample_<n>/`, plus updated `reports/records.json`.

## Aggregation & paper tables
```ruby
RESULTS_ROOT=./results python scripts/bench_evaluate/generate_latex_tables_gemini.py --judge gemini
# → results/bench_evaluate/reports/paper_tables_gemini.tex
```

Optional viewers for spot-checking:
```ruby
BENCH_STEP3_ROOT=./results/bench_step3 python scripts/bench_step3/build_viewer.py
BENCH_STEP3_ROOT=./results/bench_step3 \
TASK_ROOT=./results/reverse_step1/task RUBRIC_ROOT=./results/reverse_step2_rubric \
python scripts/bench_step3/build_human_eval.py
```

Three-judge × human agreement (Pearson / Spearman / Kendall):
```ruby
BENCH_STEP3_ROOT=./results/bench_step3 HUMAN_EVAL_JSON=human_eval_scores.json \
python scripts/bench_step3/agreement_three_judges.py
```

## License
Code: MIT. Dataset (released separately): CC BY 4.0.
