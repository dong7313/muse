# MUSE Benchmark

Text-to-CAD 评测流水线：原始 CAD 资产 → 任务文档 → rubric → 候选模型代码生成 → LLM/VLM 评分。

仓库**只包含代码**。所有数据产物（task / rubric / runs / scores）按下面的 "外部数据目录约定" 单独存放，通过环境变量或 CLI 参数指向。

> 完整背景见 [`docs/experiment_record.md`](docs/experiment_record.md)。本 README 是跑通指引。

---

## 目录结构

```
github_muse/
├── README.md
├── pyproject.toml
├── configs/
│   ├── default.json            # 正向流水线（自有数据集）
│   └── openrouter_eval.json    # OpenRouter 评测配置
├── test_list.md                # benchmark 选用的 case 列表
├── model_list.txt              # 候选模型清单（label|provider|model|api_key_env|base_url|temperature）
├── src/judge_system/           # 流水线核心
│   ├── cli.py                  # 正向 CLI: prepare-data / materialize-prompts / run / run-from-inferred
│   ├── reverse_cli.py          # 反向 CLI: task → rubric → benchmark
│   ├── reverse_pipeline.py     # 反向流水线核心
│   ├── pipeline.py             # 正向流水线核心
│   ├── llm_judge.py            # LLM / VLM judge prompt + 解析
│   ├── drawings.py             # 4 视图 SVG (调用 DrawCAD) + 3D 渲染
│   ├── geometry_metrics.py     # watertight / manifold / bbox / solid count（调用 validator）
│   ├── sandbox.py              # 沙箱执行 CadQuery 代码
│   ├── render_only_cases.txt   # 9 个有机曲面 vase：跳过 SVG，强制走 3D 渲染
│   └── ...
├── scripts/
│   ├── rerun_llm_judge_parallel.py
│   ├── build_hf_dataset.py
│   ├── build_report_viewer.py
│   ├── export_overleaf_tables_from_viewer.py
│   ├── generate_taxonomy_rubrics.py
│   ├── generate_gemini_batch_rubrics.py
│   ├── bench_step3/            # Step 3 (代码生成+渲染) 后处理
│   │   ├── build_viewer.py
│   │   ├── build_human_eval.py            # primary/secondary rubric (0/1/2)
│   │   ├── build_human_eval_claude.py
│   │   ├── build_human_eval_gpt4o.py
│   │   └── agreement_three_judges.py      # 三 judge × human 一致性 (Pearson/Spearman/Kendall)
│   └── bench_evaluate/         # Step 4 (LLM judge) 后处理 & 论文表
│       ├── build_human_eval.py            # 6 flat categories (0/1)
│       ├── generate_latex_tables.py       # paper_tables.tex（早期 GPT-5.5 judge）
│       ├── generate_latex_tables_gemini.py# paper_tables_gemini.tex（论文最终版）
│       └── package_share.py               # 选 20 case 打包对外分发子集
└── docs/
    ├── experiment_record.md    # 各步骤详细记录 + 产物目录
    └── metric_notes.md
```

---

## 外部数据目录约定

代码不再带任何写死的绝对路径。下面这些目录请按需准备好，并通过环境变量指向：

| 用途                                  | 环境变量            | 期望内容 |
|---|---|---|
| Step 0 raw assets (源代码 + STEP + SVG + 3D render PNG) | `MUSE_SOURCE_ROOT`   | `<case>/<case>.py, .step, .svg, _stp_render.png` |
| Step 1 task 文档                      | `TASK_ROOT`         | `<case>/task.md` |
| Step 2 rubric                         | `RUBRIC_ROOT`       | `<case>.md`（注意是 `<case>.md`，不是 `<case>.prompt.md`） |
| Step 3 benchmark runs                 | `BENCH_STEP3_ROOT`  | `runs/<case>/<model>/sample_<n>/{code.py,drawing/,render/}` + `reports/records.json,rubric_catalog.json` |
| Step 4 evaluate runs                  | `BENCH_EVALUATE_ROOT` | `runs/<case>/<model>/sample_<n>/score.json` + `runs/_reference_png/<case>.png` |
| 论文表输出根                          | `RESULTS_ROOT`      | 与 `BENCH_STEP3_ROOT` / `BENCH_EVALUATE_ROOT` 同级的父目录 |

---

## 外部依赖

`drawings.py` 调用 DrawCAD 把 STEP → 4 视图 SVG；`geometry_metrics.py` 直接 `import validator`。仓库不含这两个模块，请自行准备并指向：

```bash
export DRAWCAD_ROOT=/path/to/DrawCAD                      # 含 drawcad_tool/, drawcad_app/...
export DRAWCAD_RUNNER=/path/to/run_drawcad_export.py      # CLI 入口脚本
export VALIDATOR_ROOT=/path/to/validator                  # 含 validator.py (定义 CadQueryValidator)
```

也可改 `configs/*.json` 里的相应字段（支持 `${ENV_VAR:-default}` 写法）。

---

## 安装

需要 Python 3.10+、`cadquery`、`vtk`、`requests`、`numpy`、`scipy`（仅 agreement 脚本用）。`rsvg-convert` 命令行工具用于 SVG→PNG。

```bash
cd github_muse
pip install -e .                       # 装 src/judge_system 包 + 依赖
brew install librsvg                   # macOS：提供 rsvg-convert（Linux: apt-get install librsvg2-bin）
```

环境变量（写到 `.env` 或在 shell 里 export）：

```bash
export OPENROUTER_API_KEY=sk-or-v1-xxx
export OPENAI_API_KEY=sk-...           # 仅 OpenAI provider 用
```

---

## 论文跑过的完整流水线

下面是论文使用的 5 步流水线。所有命令以仓库根目录 (`github_muse/`) 为 CWD。

### Step 0 — Raw assets

`$MUSE_SOURCE_ROOT/<case>/` 是每个 case 的"原料目录"，已经准备好的产物：

| 文件 | 说明 |
|---|---|
| `<case>.py`             | CadQuery 源代码 (ground truth) |
| `<case>.step`           | 由源代码执行得到的 STEP/B-Rep |
| `<case>.svg`            | DrawCAD 生成的 4 视图工程图 SVG |
| `<case>_stp_render.png` | VTK 离屏渲染的 3D 缩略图 |

**视觉输入约定**：默认走 SVG→PNG（rsvg-convert）。9 个有机曲面 vase（见 `src/judge_system/render_only_cases.txt`）4 视图边线投影捕捉不到弯曲表面，改用 3D 渲染 PNG。这套规则对 task 推理 / rubric 推理 / 候选模型评分的"参考图像"全部生效，保证 reference vs candidate 视觉模态一致。

### Step 1 — Task document inference

从 `<case>.py` + `<case>.svg`（或 render-only case 的 STP-PNG）反向推出英文 `task.md`。

```bash
python -m src.judge_system.reverse_cli \
    --raw-root  $MUSE_SOURCE_ROOT \
    --run-root  ./out/reverse_step1 \
    --model     google/gemini-3.1-pro-preview \
    --max-workers 8
```

产物：
- `out/reverse_step1/task/<case>/task.md`
- `out/reverse_step1/task_prompts/<case>.prompt.md`
- `out/reverse_step1/visual_inputs/<case>.png`
- `out/reverse_step1/reverse_results.json`

### Step 2 — Rubric inference（**本仓库实际跑的步骤**）

只跑 rubric 推理、**不重跑 Step 1**，必须传 `--task-root` 指向已有的 task 目录 + `--infer-rubrics`：

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    $MUSE_SOURCE_ROOT \
    --run-root    ./out/reverse_step2_rubric \
    --task-root   $TASK_ROOT \
    --infer-rubrics \
    --no-alignment \
    --max-workers 8
```

产物：
- `out/reverse_step2_rubric/<case>.md`（评测 rubric，6 项 0/1 类别）
- `out/reverse_step2_rubric/_built_prompts/<case>.prompt.md`
- `out/reverse_step2_rubric/_reference_png/<case>.png`

> ⚠️ rubric 文件名是 `<case>.md`，**不是** `<case>.prompt.md`。`.prompt.md` 是 prompt 副产物。

### Step 3 — Candidate code generation (benchmark)

对每个候选模型按 `task.md`（+ rubric，可选）生成 CadQuery 代码 → 沙箱执行 → 渲染候选 SVG/PNG。

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    $MUSE_SOURCE_ROOT \
    --run-root    ./out/bench_step3 \
    --task-root   $TASK_ROOT \
    --rubric-root $RUBRIC_ROOT \
    --infer-rubrics \
    --run-benchmark \
    --model-list  model_list.txt \
    --max-workers 8
```

每个 sample 下面常见的文件：

| 文件 | 说明 |
|---|---|
| `prompt.json`           | 实际发给模型的 chat 消息 |
| `raw_response.txt`      | 模型原始回复 |
| `sample.py`             | 抽取出来的 CadQuery 代码 |
| `sandbox_stdout.log`    | 沙箱执行日志 |
| `sample.step`           | 沙箱执行得到的 STEP |
| `sample.svg`            | DrawCAD 4 视图 SVG (非 render-only case) |
| `sample.png`            | rsvg-convert / VTK 渲染缩略图 |
| `geometry_metrics.json` | 几何硬指标 |

`model_list.txt` 每行一个模型：`label | provider | model | api_key_env | base_url | temperature`。

### Step 4 — Evaluation (LLM / VLM judge)

只读 Step 3 的产物 + rubric，并行重跑 / 补跑 LLM judge。论文里最终用 Gemini-3.1-Pro：

```bash
python scripts/rerun_llm_judge_parallel.py \
    --config       configs/openrouter_eval.json \
    --run-id       bench_step3                # 复用 results/<run-id>/reports/records.json
```

输出在 `results/<run-id>/reports/` 下；同时按需要往 `bench_evaluate*/runs/<case>/<model>/sample_<n>/score.json` 落分。

聚合 + 出论文表：

```bash
RESULTS_ROOT=./out python scripts/bench_evaluate/generate_latex_tables_gemini.py --judge gemini
# → out/bench_evaluate/reports/paper_tables_gemini.tex   ← 论文主表
```

更多脚本：

```bash
# Step 3 records.json → 自包含 viewer.html 做人工 spot-check
BENCH_STEP3_ROOT=./out/bench_step3 python scripts/bench_step3/build_viewer.py

# Step 3 → 6 类 0/1 人工评分页面
BENCH_STEP3_ROOT=./out/bench_step3 \
TASK_ROOT=$TASK_ROOT RUBRIC_ROOT=$RUBRIC_ROOT \
python scripts/bench_step3/build_human_eval.py

# Step 4 → 6 类 0/1 人工评分页面（含 Gemini judge 比对）
BENCH_EVALUATE_ROOT=./out/bench_evaluate BENCH_STEP3_ROOT=./out/bench_step3 \
TASK_ROOT=$TASK_ROOT RUBRIC_ROOT=$RUBRIC_ROOT \
python scripts/bench_evaluate/build_human_eval.py

# 三 judge × human 一致性
BENCH_STEP3_ROOT=./out/bench_step3 HUMAN_EVAL_JSON=human_eval_scores.json \
python scripts/bench_step3/agreement_three_judges.py

# 选 20 case 打包对外分发子集 (share/ + share.tgz)
python scripts/bench_evaluate/package_share.py --seed 42 --n 20
```

---

## 关键脚本速查

| 脚本 | 作用 |
|---|---|
| `src/judge_system/reverse_cli.py`           | 反向流水线入口（task → rubric → benchmark） |
| `src/judge_system/cli.py`                   | 正向流水线入口（prepare-data / materialize-prompts / run） |
| `src/judge_system/reverse_pipeline.py`      | 反向流水线核心 |
| `src/judge_system/llm_judge.py`             | LLM/VLM judge prompt + 解析 |
| `src/judge_system/render_only_cases.txt`    | 9 个 vase（SVG 不可用，强制 3D render） |
| `scripts/rerun_llm_judge_parallel.py`       | 并行重跑 / 补跑 LLM judge |
| `scripts/build_hf_dataset.py`               | 把 Step 0–2 产物组装成 HF dataset 目录 |
| `scripts/bench_evaluate/generate_latex_tables_gemini.py` | 出论文主表 |

---

## HuggingFace 数据集打包

`scripts/build_hf_dataset.py` 把 source / task / rubric 三个目录组装成可上传 HF 的目录布局：

```bash
MUSE_ROOT=. SOURCE_ROOT=$MUSE_SOURCE_ROOT TASK_ROOT=$TASK_ROOT RUBRIC_ROOT=$RUBRIC_ROOT \
python scripts/build_hf_dataset.py
```

输出在 `$HF_OUT_DIR`（默认 `./hf_dataset`），含 `README.md` (dataset card)、`LICENSE` (CC BY 4.0)、`croissant.json`、`metadata.jsonl`、`cases/<case>/{design_description.md, <case>.png, <case>_stp_render.png, evaluation_rubric.md}`。

---

## License

代码：MIT（见 `LICENSE`）。
数据集：CC BY 4.0。
