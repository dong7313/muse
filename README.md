# MUSE: A CAD Design Benchmark with Multi-modal Ground Truth and Rubric-based Evaluation

> Code for the NeurIPS 2026 Datasets & Benchmarks track submission
> *MUSE: A CAD Design Benchmark with Multi-modal Ground Truth and Rubric-based Evaluation*.
>
> Authorship is intentionally omitted for double-blind review.

MUSE is a benchmark of **106 engineering CAD design cases**. Each case pairs a
natural-language design specification with two complementary ground-truth
artefacts (a 4-view dimensioned engineering drawing and a 3D STP rendered
image) and a hand-crafted, rubric-style evaluation guide covering assembly
readiness, joint design, tolerance, and functional adaptation.

This repository contains the **code** used to (1) build the benchmark,
(2) run candidate models, (3) score model outputs against the per-case
rubrics, and (4) reproduce the figures and tables in the paper.

The **dataset itself** is hosted on Hugging Face:

> 🤗  https://huggingface.co/datasets/dongxiaoyu/MUSE
>
> *(The Hugging Face account name is incidental and does not identify the
>  authors of this submission. The dataset card was produced by an
>  automated upload tool that uses the uploader's account handle.)*

A Croissant 1.0 metadata file with full Responsible AI fields ships at
[`croissant.json`](https://huggingface.co/datasets/dongxiaoyu/MUSE/resolve/main/croissant.json)
in the dataset repository.

---

## Repository layout

```
muse-benchmark/
├── README.md                    # this file
├── LICENSE                      # MIT (code)
├── pyproject.toml               # Python package + tool config
├── requirements.txt             # pinned runtime dependencies
├── .gitignore
│
├── src/muse/                    # library code
│   ├── data/                    # dataset loader, HF integration
│   ├── pipeline/                # benchmark pipeline (reverse, rubric, judge stages)
│   ├── drawings/                # CadQuery → 4-view SVG rendering
│   ├── render/                  # VTK off-screen STP rendering
│   ├── judge/                   # rubric judge prompts + scoring
│   ├── metrics/                 # geometry validation (watertight, manifold, bbox)
│   └── cli.py                   # `muse` command-line entry point
│
├── scripts/                     # one-off scripts (dataset assembly, figure prep)
│   ├── build_hf_dataset.py
│   ├── upload_to_hf.py
│   ├── make_compare_html.py
│   └── export_overleaf_tables.py
│
├── configs/                     # YAML configs for the pipeline
│   ├── models/                  # per-model API/runtime configs
│   ├── pipelines/               # benchmark-stage configs
│   └── judges/                  # judge model + prompt configs
│
├── prompts/                     # canonical prompts (judge, rubric authoring, etc.)
│
├── tests/                       # unit tests
│
└── examples/                    # one or two tiny illustrative cases (optional)
```

The actual `data/`, `results/`, and `hf_dataset/` directories are deliberately
**not** versioned in this repo — they are pulled from the Hugging Face mirror
at runtime. See `src/muse/data/__init__.py` for the loader.

## Installation

```bash
git clone <this repo>
cd muse-benchmark
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

System-level dependencies (used by the SVG and STP renderers — only required
if you want to *rebuild* the dataset rather than consume it):

```bash
# macOS
brew install librsvg
# Ubuntu / Debian
sudo apt install librsvg2-bin
```

CadQuery and VTK are installed via the Python `pip install -e .` step.

## Quick start

### 1. Load the benchmark from Hugging Face

```python
from muse import load_benchmark

bench = load_benchmark()                 # downloads from HF on first call
print(len(bench))                        # 106 cases
case = bench["bookshelf"]
print(case.design_description[:200])
print(case.svg_png_path)                 # local cached path
print(case.stp_render_path)
print(case.evaluation_rubric)
```

### 2. Run a model on the benchmark

```bash
muse run \
    --config configs/models/example_openai.yaml \
    --output results/run_example/
```

This writes one `case_<id>/output.svg` (or `.py`, depending on modality) per
case, plus a `manifest.jsonl` summarizing each invocation.

### 3. Judge the model outputs

```bash
muse judge \
    --candidate-dir results/run_example/ \
    --judge-config configs/judges/llm_judge.yaml \
    --out         results/run_example/scores.jsonl
```

Each line of `scores.jsonl` records, per case, the per-criterion 0/1 verdicts
produced by the judge model and the aggregated mean.

### 4. Reproduce the paper tables

```bash
muse report \
    --runs results/run_example/scores.jsonl results/run_other_model/scores.jsonl \
    --out  paper/tables/main_table.tex
```

## Reproducing the dataset (optional)

> Most users do **not** need this — the published Hugging Face dataset is the
> canonical version. These steps are documented for transparency and to
> support extending the benchmark with new cases.

```bash
muse build-dataset \
    --tasks-dir   path/to/reverse_step1_retry/task \
    --sources-dir path/to/raw_new_step0_highconcurrency/prepared_source \
    --rubrics-dir path/to/reverse_step2_rubric_new \
    --out-dir     hf_dataset/
muse upload-hf hf_dataset/ --repo dongxiaoyu/MUSE
```

The `build-dataset` step:
1. Copies `task.md` → `design_description.md` per case.
2. Materialises a 4-view engineering-drawing PNG (from the SVG, via
   `rsvg-convert`) and a 3D STP-rendered PNG (via VTK off-screen rendering)
   for each case.
3. For 9 organic / curved-vase cases the SVG projection is reused as the STP
   render PNG (the SVG silhouette is the same image as the 3D render for
   curved geometry).
4. Copies the per-case rubric markdown into `evaluation_rubric.md`.
5. Emits a `metadata.jsonl` index.

## Evaluation protocol (recommended)

For each case:

1. Feed `design_description.md` to the model under test as the user prompt.
2. Collect the model's output (e.g. CAD code, SVG, mesh, or rendered image,
   depending on the modality being evaluated).
3. Compare the model output against the ground-truth pair
   (`<case>.png`, `<case>_stp_render.png`).
4. Apply each criterion in `evaluation_rubric.md` as an independent 0/1 score.
5. Report per-rubric pass-rates and the overall mean across all 106 cases.

A reference judge prompt and a breakdown JSON schema for the LLM judge are in
`prompts/judge_prompt.md` and `src/muse/judge/schema.py`.

## Citation

```bibtex
@inproceedings{muse_2026,
  title     = {MUSE: A CAD Design Benchmark with Multi-modal Ground Truth and Rubric-based Evaluation},
  author    = {Anonymous},
  booktitle = {Advances in Neural Information Processing Systems (NeurIPS) Datasets and Benchmarks Track},
  year      = {2026}
}
```

## License

- **Code** in this repository: [MIT](LICENSE).
- **Dataset** on Hugging Face: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
