# MUSE Benchmark

**MUSE** — *Manufacturable, Functional, and Assemblable* Text-to-CAD benchmark.
Given a natural-language design specification, an LLM must generate CadQuery
code that produces a watertight, manifold solid that is also functionally
correct and assemblable. Outputs are judged by Gemini-3.1-Pro against
per-case rubrics through a three-stage funnel: **code execution → geometric
validity → design-intent alignment**.

| | |
|---|---|
| 🏠 Project page | https://dong7313.github.io/muse-benchmark/ |
| 🏆 Leaderboard | https://dong7313.github.io/muse-benchmark/leaderboard.html |
| 🤗 Dataset | https://huggingface.co/datasets/dongxiaoyu/MUSE |
| 📑 Paper | NeurIPS 2026 Datasets & Benchmarks (coming soon) |

---

## Quickstart — evaluate your LLM on MUSE

Two scripts get you from *"I have a new LLM endpoint"* to *"I have its leaderboard scores"*.
The pipeline is: **(a) point the LLM at each design spec → generate CadQuery**, then
**(b) sandbox-execute + geometric-check + judge with Gemini-3.1-Pro**.

### 1. Install

```bash
conda create -n muse python=3.10
conda activate muse
pip install -e .
brew install librsvg          # Linux: apt-get install librsvg2-bin

cp .env.example .env          # then fill OPENROUTER_API_KEY
```

External modules `DrawCAD` (4-view drawing) and `validator` (OCCT checks) must
live under `external/` or be pointed at via `DRAWCAD_ROOT` / `VALIDATOR_ROOT`.

### 2. Pull the MUSE data

```bash
pip install -U huggingface_hub
huggingface-cli download dongxiaoyu/MUSE --repo-type=dataset --local-dir ./data/muse
```

Each case under `data/muse/cases/<case>/` ships with:

| File | Used as |
|---|---|
| `design_description.md` | Prompt fed to the candidate LLM (Step 3 task) |
| `evaluation_rubric.md`  | Rubric the VLM judge scores against (Step 4) |
| `<case>.png`            | Reference 4-view engineering drawing |
| `<case>_stp_render.png` | Reference 3D render (used for the 9 organic-vase cases) |

### 3. Register your model

Add one line to `model_list.txt`:

```
my-model | openrouter | provider-org/model-id | OPENROUTER_API_KEY | https://openrouter.ai/api/v1 | 0.2
```

Format: `label | provider | model_id | api_key_env | base_url | temperature`.
Comment out the existing rows if you only want to evaluate yours.

### 4. Generate CadQuery code (= Step 3 of the build pipeline)

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    data/muse/cases \
    --run-root    out/my_eval \
    --task-root   data/muse/cases \
    --rubric-root data/muse/cases \
    --run-benchmark \
    --model-list  model_list.txt \
    --max-workers 8
```

For each (case, model, sample) this produces under
`out/my_eval/runs/<case>/<model>/sample_<n>/`:

- `code.py` — extracted CadQuery
- `sample.step` — solid produced by the sandbox
- `drawing/sample.svg` + `render/*.png` — candidate's 4-view and 3D render
- `geometry_metrics.json` — sandbox / OCCT results (Stage 1 + Stage 2)

…plus `out/my_eval/reports/records.json` aggregating every sample row.

### 5. Judge with Gemini-3.1-Pro (= Step 4)

```bash
python scripts/rerun_llm_judge_parallel.py \
    --config  configs/openrouter_eval.json \
    --run-id  my_eval \
    --max-workers 8
```

Writes a `score.json` next to each sample and updates `reports/records.json`
with the Stage 3 columns: `functionality`, `manufacturability`,
`assemblability`, and `final`.

### 6. Read the results

```bash
python scripts/bench_evaluate/package_share.py --run-id my_eval
```

…or load `reports/records.json` directly. The same aggregation logic that
produces the public leaderboard lives in `results/generate_latex_tables_gemini.py`
in the build repo — copy it if you want the paper-style table.

---

## How MUSE was built

`Quickstart` runs Steps 3 + 4 on the published dataset.
The full pipeline that *created* the dataset is below — re-run only if you
want to author additional cases or rebuild rubrics from scratch.

> **Upstream: raw asset collection.** Each case starts as a CadQuery script
> hand-authored (or LLM-augmented and human-reviewed) by a designer. Running
> the script produces a STEP file; DrawCAD renders a 4-view SVG; VTK produces
> a 3D render. The result is `<case>.py` / `<case>.step` / `<case>.svg` /
> `<case>_stp_render.png` per case, in `prepared_source/<case>/`. This is the
> only stage with no LLM in the loop.

### Step 1 — Task inference (`reverse_cli.py`)

Reverse-infer an English `task.md` design spec from `<case>.py` + `<case>.svg`
(or the 3D render PNG for the 9 organic-vase cases listed in
`src/judge_system/render_only_cases.txt`).

```bash
python -m src.judge_system.reverse_cli \
    --raw-root  data/prepared_source \
    --run-root  out/reverse_step1 \
    --model     google/gemini-3.1-pro-preview \
    --max-workers 8
```

Output: `out/reverse_step1/task/<case>/task.md`.

### Step 2 — Rubric inference (`reverse_cli.py --infer-rubrics`)

Reuse the Step 1 tasks and infer per-case evaluation rubrics across six
sub-criteria (Functional · Robust · Well-toleranced · Manufacturable ·
Assembly-ready · Connectable). Passing `--task-root` to an existing dir makes
Step 1 short-circuit so it's not regenerated.

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    data/prepared_source \
    --run-root    out/reverse_step2_rubric \
    --task-root   out/reverse_step1/task \
    --infer-rubrics \
    --no-alignment \
    --max-workers 8
```

Output: `out/reverse_step2_rubric/<case>.md` (the rubric file is `<case>.md`,
**not** `<case>.prompt.md` — that suffix is the prompt artifact).

### Step 3 — Candidate generation (`reverse_cli.py --run-benchmark`)

For every model in `model_list.txt`, prompt with the task, capture CadQuery
code, run in sandbox, render 4-view + 3D. Same command as Quickstart Step 4,
just pointed at the full 106-case authoring layout instead of the HF mirror.

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    data/prepared_source \
    --run-root    out/bench_step3 \
    --task-root   out/reverse_step1/task \
    --rubric-root out/reverse_step2_rubric \
    --run-benchmark \
    --model-list  model_list.txt \
    --max-workers 8
```

### Step 4 — LLM/VLM judge (`rerun_llm_judge_parallel.py`)

Read Step 3 artifacts + Step 2 rubrics, score with a VLM judge
(Gemini-3.1-Pro in the paper, configurable via `configs/openrouter_eval.json`).
A sample is forced to `0` on every Stage 3 sub-criterion if it failed any
Stage 1 / Stage 2 check — this exposes *where* each model breaks down rather
than averaging the failures away.

```bash
python scripts/rerun_llm_judge_parallel.py \
    --config  configs/openrouter_eval.json \
    --run-id  bench_step3 \
    --max-workers 8
```

---

## Repository layout

```
muse/
├── src/judge_system/         # pipeline (cli, reverse_cli, sandbox, geometry_metrics, llm_judge, …)
│   ├── reverse_cli.py        # Steps 1/2/3 driver
│   ├── reverse_pipeline.py   # core implementation
│   ├── llm_judge.py          # prompt/response handling for Step 4
│   ├── sandbox.py            # CadQuery sandbox execution
│   ├── geometry_metrics.py   # OCCT checks
│   ├── drawings.py           # DrawCAD wrapper (4-view SVG)
│   └── render_only_cases.txt # 9 organic-vase exception list
├── scripts/
│   ├── rerun_llm_judge_parallel.py  # Step 4 — parallel judge re-run / fill-in
│   ├── bench_step3/                 # post-processing for Step 3 (agreement, …)
│   ├── bench_evaluate/              # packaging & sharing helpers
│   ├── build_hf_dataset.py          # bundles Steps 0–2 outputs into the HF dataset
│   └── build_report_viewer.py       # generates the inspection viewer.html
├── configs/
│   ├── default.json            # used by Step 3 (candidate generation)
│   └── openrouter_eval.json    # used by Step 4 (judge)
├── model_list.txt              # one line per LLM endpoint
└── pyproject.toml
```

## Citation

```bibtex
@inproceedings{muse2026,
  title     = {MUSE: Benchmarking Manufacturable, Functional, and Assemblable Text-to-CAD Generation},
  author    = {Anonymous},
  booktitle = {NeurIPS Datasets and Benchmarks Track},
  year      = {2026},
  url       = {https://dong7313.github.io/muse-benchmark/}
}
```

## License

- **Code**: MIT (see `LICENSE`)
- **Dataset** ([HF](https://huggingface.co/datasets/dongxiaoyu/MUSE)): CC BY 4.0
