# MUSE Benchmark — 实验记录

记录从原始 CAD 资产 → 任务文档 → 评测 rubric → 候选模型代码生成 → LLM/VLM 评分的全流程,以及每个产物落地的目录。所有命令以 `judge_system/` 为当前工作目录运行。

```
judge_system/
├── src/judge_system/         # 流水线代码 (cli.py / reverse_cli.py / reverse_pipeline.py …)
├── configs/                  # default.json / openrouter_eval.json
├── data/                     # cleaned/ 与 rubrics/ 派生数据
└── results/                  # 每一步的输出都落到这里
```

CLI 入口:
- `python -m src.judge_system.cli …`           ← 数据清洗、materialize-prompts、benchmark
- `python -m src.judge_system.reverse_cli …`   ← 反向流水线 (task / rubric inference + benchmark)

---

## Step 0 — Raw assets prep (已经完成)

`results/raw_new_step0_highconcurrency/prepared_source/<case>/` 是每个 case 的"原料目录",已经准备好的产物:

| 文件 | 说明 |
|---|---|
| `<case>.py`             | CadQuery 源代码 (ground truth) |
| `<case>.step`           | 由源代码执行得到的 STEP/B-Rep |
| `<case>.svg`            | DrawCAD 生成的 4 视图工程图 SVG |
| `<case>_stp_render.png` | VTK 离屏渲染的 3D 缩略图 (PNG) |
| `thumbnail.png`         | 上游流水线产生的可选缩略图 |
| `task_zh-CN.md` / `plan_zh-CN.md` / `review_zh-CN.md` | 中文中间产物 (非必需) |

### 视觉输入的统一约定 (重要)

下游所有 prompt / VLM judge 用到"参考图像"时,遵循:

1. **默认 SVG → PNG**:对 `<case>.svg` 跑 `rsvg-convert` 生成 PNG,作为参考图。
2. **9 个有机曲面 vase 例外**:DrawCAD 4 视图边线投影捕捉不到弯曲表面,直接用 `<case>_stp_render.png` 顶替 SVG-PNG。case 列表写在 `src/judge_system/render_only_cases.txt`:
   ```
   vase_wave_blossom
   vase_wave_dune
   vase_wave_fluted
   vase_wave_petal
   vase_wave_ripple
   vase_wave_scallop
   vase_wave_shell
   vase_wave_twist
   wave_vase
   ```
   对这些 case,task / rubric 推理 prompt 里的 SVG 文本也跳过,只附 3D 渲染图。
3. 这套规则同时影响**候选模型输出**:render-only case 的候选 CadQuery 代码不再走 DrawCAD 4 视图,而是再次 VTK 3D 渲染后送给 VLM judge,保证 reference / candidate 视觉模态一致。

---

## Step 1 — Task document inference

从 `<case>.py` + `<case>.svg`(或 render-only case 的 STP-PNG)反向推出英文 `task.md` 设计说明。

**输出目录:** `results/reverse_step1_retry/task/<case>/task.md`

```bash
python -m src.judge_system.reverse_cli \
    --raw-root  results/raw_new_step0_highconcurrency/prepared_source \
    --run-root  results/reverse_step1_retry \
    --model     <openrouter model id> \
    --max-workers 8
```

副产物:
- `results/reverse_step1_retry/task_prompts/<case>.prompt.md` — 提交给 LLM 的完整 prompt
- `results/reverse_step1_retry/visual_inputs/<case>.png`        — 实际送入 VLM 的参考图
- `results/reverse_step1_retry/reverse_results.json`            — 每个 case 的成功/失败 + token 统计

---

## Step 2 — Rubric inference (本次实际使用)

只跑 rubric 推理、**不重跑 Step 1**,所以必须传 `--task-root` 指向已有的 task 目录,并传 `--infer-rubrics`:

**输出目录:** `results/reverse_step2_rubric_new/<case>.md`
(注意是 `<case>.md`,不是 `<case>.prompt.md`;`.prompt.md` 是 prompt 副产物)

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    results/raw_new_step0_highconcurrency/prepared_source \
    --run-root    results/reverse_step2_rubric_new \
    --task-root   results/reverse_step1_retry/task \
    --infer-rubrics \
    --no-alignment \
    --max-workers 8
```

副产物:
- `results/reverse_step2_rubric_new/_built_prompts/<case>.prompt.md` — rubric 推理 prompt
- `results/reverse_step2_rubric_new/_reference_png/<case>.png`        — 实际送入 VLM 的参考图(SVG→PNG 或 STP-render)

> 关键点:`--task-root` 指向**已有目录**就会跳过 Step 1,只重建 rubric。

---

## Step 3 — Candidate code generation (benchmark)

对每个候选模型,根据 `task.md`(+ rubric,可选)生成 CadQuery 代码,在沙箱执行得到 STEP,再渲染候选 SVG / PNG。

**输出目录:** `results/bench_step3_smoke10/runs/<case>/<model_label>/sample_<n>/`

每个 sample 下面常见的文件:
| 文件 | 说明 |
|---|---|
| `prompt.json`           | 实际发给模型的 chat 消息 |
| `raw_response.txt`      | 模型原始回复 |
| `sample.py`             | 抽取出来的 CadQuery 代码 |
| `sandbox_stdout.log`    | 沙箱执行日志 |
| `sample.step`           | 沙箱执行得到的 STEP |
| `sample.svg`            | DrawCAD 4 视图 SVG (非 render-only case) |
| `sample.png`            | rsvg-convert / VTK 渲染得到的候选缩略图 |
| `geometry_metrics.json` | 几何硬指标 (watertight / manifold / bbox / solid count …) |

启动命令(在 Step 2 完成的基础上接着跑):

```bash
python -m src.judge_system.reverse_cli \
    --raw-root    results/raw_new_step0_highconcurrency/prepared_source \
    --run-root    results/bench_step3_smoke10 \
    --task-root   results/reverse_step1_retry/task \
    --rubric-root results/reverse_step2_rubric_new \
    --infer-rubrics \
    --run-benchmark \
    --model-list  model_list.txt \
    --max-workers 8
```

副产物:
- `results/bench_step3_smoke10/benchmark_results.json` — 每 (case, model, sample) 的全字段记录
- `results/bench_step3_smoke10/reports/`                — viewer.html / 汇总表

> 模型清单格式: `label|provider|model|api_key_env|base_url|temperature`,每行一个。

---

## Step 4 — Evaluation (LLM / VLM judge)

对 Step 3 的候选 SVG/PNG + 候选代码 + rubric 跑 LLM-as-judge 评分。这一步只读 Step 3 的产物,不再调用沙箱。

**早期版本:** `results/bench_evaluate/runs/<case>/<model_label>/sample_<n>/`
**论文最终版 (Gemini-3.1-Pro):** `results/bench_evaluate_gemini/runs/<case>/<model_label>/sample_<n>/`
**仅供参考 (GPT-4o judge):** `results/bench_evaluate_4o/runs/`

主要文件:
| 文件 | 说明 |
|---|---|
| `alignment.json`     | VLM 对参考 vs 候选图的语义对齐分 |
| `rubric_scores.json` | LLM 按 rubric 逐条 0/1 打分 + 解释 |
| `judge_prompt.txt`   | 实际送 judge 的 prompt |

入口脚本(并行重跑/补跑 judge):
```bash
python rerun_llm_judge_parallel.py \
    --runs-root  results/bench_step3_smoke10/runs \
    --out-root   results/bench_evaluate/runs \
    --rubric-root results/reverse_step2_rubric_new \
    --judge-config configs/openrouter_eval.json \
    --max-workers 8
```

聚合产物:
- `results/bench_evaluate/reports/`                   — 每模型 pass-rate / 每 rubric 项命中率(早期版本)
- `results/bench_evaluate_gemini/reports/paper_tables.tex` — **论文里实际使用的 Gemini-3.1-Pro 主表**
- `results/bench_evaluate/share/`                     — 准备好打包对外分发的子集
- `results/bench_evaluate/share.tgz`                  — 压缩包

生成最终表格的脚本:
- `results/generate_latex_tables_gemini.py` — 从 `bench_evaluate_gemini/runs/` 聚合并导出 `reports/paper_tables.tex`

---

## HuggingFace 数据集打包 (一次性)

`build_hf_dataset.py` 从上面 Step 0–2 的产物组装成 `hf_dataset/`:

```
hf_dataset/
├── README.md                       # dataset card + YAML frontmatter (license: cc-by-4.0)
├── LICENSE                         # CC BY 4.0 全文
├── croissant.json                  # Croissant 1.0 + RAI 元数据
├── metadata.jsonl                  # 每行 1 个 case 的索引
└── cases/<case>/
    ├── design_description.md       # ← results/reverse_step1_retry/task/<case>/task.md
    ├── <case>.png                  # ← rsvg-convert(prepared_source/<case>.svg)
    ├── <case>_stp_render.png       # ← prepared_source/<case>_stp_render.png
    │                                #   (9 个 vase case: 用 svg→png 复制顶替)
    └── evaluation_rubric.md        # ← results/reverse_step2_rubric_new/<case>.md
```

发布到: (URL withheld for double-blind review)

---

## 文件 / 脚本速查

| 脚本 | 作用 |
|---|---|
| `src/judge_system/cli.py`                  | 正向流水线 (prepare-data / materialize-prompts / run) |
| `src/judge_system/reverse_cli.py`          | 反向流水线 (task → rubric → benchmark) |
| `src/judge_system/reverse_pipeline.py`     | 反向流水线核心实现 |
| `src/judge_system/pipeline.py`             | 正向流水线核心实现 |
| `src/judge_system/llm_judge.py`            | LLM-as-judge prompt/解析 |
| `src/judge_system/drawings.py`             | DrawCAD 4 视图 SVG 渲染封装 |
| `src/judge_system/render_only_cases.txt`   | 9 个使用 STP-render 顶替 SVG 的 case 列表 |
| `build_hf_dataset.py`                      | 把 Step 0–2 产物组装成 HF dataset 目录 |
| `rerun_llm_judge_parallel.py`              | 并行重跑 / 补跑 LLM judge |
| `build_report_viewer.py`                   | 生成 viewer.html 做人工 spot-check |
| `export_overleaf_tables_from_viewer.py`    | 从 viewer 数据导出 LaTeX 主表 |

## 关键目录速查

| 用途 | 路径 |
|---|---|
| 原始 CAD 资产                   | `results/raw_new_step0_highconcurrency/prepared_source/` |
| Step 1 task 文档                | `results/reverse_step1_retry/task/<case>/task.md` |
| Step 2 评测 rubric              | `results/reverse_step2_rubric_new/<case>.md` |
| Step 3 候选代码生成 / 沙箱执行  | `results/bench_step3_smoke10/runs/` |
| Step 4 LLM judge 分数 (早期)    | `results/bench_evaluate/runs/` |
| Step 4 LLM judge 分数 (4o)      | `results/bench_evaluate_4o/runs/` |
| **Step 4 LLM judge 分数 (最终,Gemini-3.1-Pro)** | **`results/bench_evaluate_gemini/runs/`** |
| **Step 4 论文主表 (LaTeX)**      | **`results/bench_evaluate_gemini/reports/paper_tables.tex`** |
| Step 4 聚合报告 (早期)          | `results/bench_evaluate/reports/` |
| HF 上传目录                     | `hf_dataset/` |
