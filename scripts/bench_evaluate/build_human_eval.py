"""Build a self-contained human_eval.html for bench_evaluate (gpt-5.5 judge results).

Schema differs from bench_step3_smoke10:
  - 6 flat categories (not primary/secondary): Assembly Readiness / Joint Design /
    Tolerance / Functional Adaptation / Usage Stability / Manufacturability
  - Each scored 0 (Fail) or 1 (Pass) — not 0/1/2
  - overall_score_normalized = sum / 6

Artifacts (code/drawing/render/step/stl) come from bench_step3_smoke10/runs since
bench_evaluate only stores judge scores. Reference images come from
bench_evaluate/runs/_reference_png/<case>.png.

Output: bench_evaluate/reports/human_eval.html (single self-contained file).
"""
from __future__ import annotations
import json, os, re
from collections import defaultdict
from pathlib import Path

# Resolve via env vars so the script is portable. Defaults assume the result tree
# alongside this repo (data layout described in the README).
BENCH_EVALUATE_ROOT = Path(os.environ.get('BENCH_EVALUATE_ROOT', './bench_evaluate')).resolve()
SCORES_ROOT = BENCH_EVALUATE_ROOT / 'runs'
REF_PNG_DIR = SCORES_ROOT / '_reference_png'
PROMPTS_DIR = SCORES_ROOT / '_built_prompts'
SMOKE_ROOT = Path(os.environ.get('BENCH_STEP3_ROOT', './bench_step3_smoke10')).resolve()
SMOKE_RECORDS = SMOKE_ROOT / 'reports' / 'records.json'
TASK_ROOT = Path(os.environ.get('TASK_ROOT', './data/task')).resolve()
RUBRIC_ROOT = Path(os.environ.get('RUBRIC_ROOT', './data/rubrics')).resolve()
OUT = BENCH_EVALUATE_ROOT / 'reports' / 'human_eval.html'

CATEGORIES = [
    'Assembly Readiness',
    'Joint Design',
    'Tolerance',
    'Functional Adaptation',
    'Usage Stability',
    'Manufacturability',
]
ITEM_MAX_SCORE = 1.0  # 0/1 Pass/Fail


def smoke_relpath(absolute: str) -> str:
    """Map absolute path inside bench_step3_smoke10 → relative to bench_evaluate/reports/."""
    if not absolute:
        return ''
    marker = 'bench_step3_smoke10/'
    if marker in absolute:
        return '../../bench_step3_smoke10/' + absolute.split(marker, 1)[1]
    return absolute


def reference_url(case: str) -> str:
    p = REF_PNG_DIR / f'{case}.png'
    if p.exists():
        return f'../runs/_reference_png/{case}.png'
    # fallback: smoke10 reference
    p2 = SMOKE_ROOT / 'runs' / case / 'reference' / f'{case}.png'
    if p2.exists():
        return f'../../bench_step3_smoke10/runs/{case}/reference/{case}.png'
    return ''


def safe_parse(value, fallback):
    if value in (None, '', '[]', '{}'):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


_CRIT_BLOCK_RE = re.compile(
    r'###\s+\d+\.\s+(?P<category>[^\n]+?)\n'
    r'(?P<body>.*?)(?=^###\s+\d+\.\s|^---\s*$|^</Evaluation_Rubric>|\Z)',
    re.DOTALL | re.MULTILINE,
)
_PASS_RE = re.compile(
    r'\*\s+\*\*1\s+Points?\s*\(Pass\)\*\*\s*:\s*(.+?)(?=\*\s+\*\*0\s+Points?|\Z)',
    re.DOTALL,
)
_FAIL_RE = re.compile(
    r'\*\s+\*\*0\s+Points?\s*\(Fail\)\*\*\s*:\s*(.+?)(?=\Z)',
    re.DOTALL,
)


def parse_rubric_criteria_from_prompt(prompt_md: str):
    """Return dict mapping canonical category name → {pass_text, fail_text}.
    Looks for the per-category Pass/Fail blocks inside the <Evaluation_Rubric> section.
    """
    out = {}
    # Restrict to the Evaluation_Rubric section if present
    eval_section = re.search(
        r'<Evaluation_Rubric>(.+?)</Evaluation_Rubric>',
        prompt_md, re.DOTALL,
    )
    body = eval_section.group(1) if eval_section else prompt_md
    for m in _CRIT_BLOCK_RE.finditer(body):
        cat = m.group('category').strip()
        block = m.group('body')
        pass_m = _PASS_RE.search(block)
        fail_m = _FAIL_RE.search(block)
        out[cat] = {
            'pass': pass_m.group(1).strip() if pass_m else '',
            'fail': fail_m.group(1).strip() if fail_m else '',
        }
    return out


def load_case_rubric_criteria(case: str) -> dict:
    """Find any prompt md for this case and parse its rubric criteria."""
    candidates = sorted(PROMPTS_DIR.glob(f'{case}__*.prompt.md'))
    if not candidates:
        return {}
    try:
        return parse_rubric_criteria_from_prompt(candidates[0].read_text(encoding='utf-8'))
    except Exception:
        return {}


def load_score_files():
    """Walk bench_evaluate/runs and return list of (case, model, sample_idx, payload)."""
    out = []
    for case_dir in sorted(SCORES_ROOT.iterdir()):
        if not case_dir.is_dir() or case_dir.name.startswith('_'):
            continue
        for model_dir in sorted(case_dir.iterdir()):
            if not model_dir.is_dir():
                continue
            for sample_dir in sorted(model_dir.iterdir()):
                sj = sample_dir / 'score.json'
                if not sj.exists():
                    continue
                try:
                    payload = json.loads(sj.read_text())
                except Exception:
                    continue
                idx = int(sample_dir.name.split('_')[1])
                out.append({
                    'case': case_dir.name,
                    'model': model_dir.name,
                    'sample': idx,
                    'payload': payload,
                })
    return out


def build_data():
    smoke_rows = json.loads(SMOKE_RECORDS.read_text())
    smoke_by_key = {(r['task_name'], r['model_label'], r['sample_index']): r for r in smoke_rows}
    score_entries = load_score_files()
    print(f'score files loaded: {len(score_entries)}')

    # Smoke10 full-set aggregates per model (independent of bench_evaluate judging)
    smoke_by_model = defaultdict(list)
    for r in smoke_rows:
        smoke_by_model[r.get('model_label') or r.get('model_name') or 'unknown'].append(r)

    def smoke_metric_block(rows):
        n = len(rows) or 1
        total = len(rows)
        sb = sum(1 for r in rows if r.get('sandbox_ok'))
        geom = sum(1 for r in rows if r.get('geometry_valid'))
        # watertight (legacy combined: open-edge AND non-manifold) — fall back to it
        # if the strict/manifold split fields are missing on a row.
        wstrict = sum(1 for r in rows if (r.get('watertight_strict') if r.get('watertight_strict') is not None else r.get('watertight')))
        manif = sum(1 for r in rows if (r.get('manifold') if r.get('manifold') is not None else r.get('watertight')))
        intf = sum(1 for r in rows if r.get('self_intersection_free'))
        comp = sum(1 for r in rows if r.get('component_count_match'))
        return {
            'samples_total': total,
            'sandbox_ok': sb, 'sandbox_ok_rate': sb / n,
            'geometry_valid': geom, 'geometry_valid_rate': geom / n,
            'watertight_strict': wstrict, 'watertight_strict_rate': wstrict / n,
            'manifold': manif, 'manifold_rate': manif / n,
            'intersection_free': intf, 'intersection_free_rate': intf / n,
            'component_match': comp, 'component_match_rate': comp / n,
        }

    smoke_overall = smoke_metric_block(smoke_rows)
    smoke_per_model = {m: smoke_metric_block(rs) for m, rs in smoke_by_model.items()}

    # Build per-case → list of model entries
    by_case = defaultdict(list)
    for s in score_entries:
        by_case[s['case']].append(s)

    # Per-model summary across all judged rows
    per_model_agg = defaultdict(lambda: {
        'scope': '', 'samples': 0, 'judged': 0,
        'overall_sum': 0.0, 'overall_n': 0,
        'cat_scores': {c: {'earned': 0.0, 'judged': 0} for c in CATEGORIES},
    })

    overall_agg = {
        'scope': 'Overall', 'samples': len(score_entries), 'judged': len(score_entries),
        'overall_sum': 0.0, 'overall_n': 0,
        'cat_scores': {c: {'earned': 0.0, 'judged': 0} for c in CATEGORIES},
    }

    for s in score_entries:
        m = s['model']
        per_model_agg[m]['scope'] = m
        per_model_agg[m]['samples'] += 1
        per_model_agg[m]['judged'] += 1
        overall_agg['overall_n'] += 1
        per_model_agg[m]['overall_n'] += 1
        ov = float(s['payload'].get('overall_score_normalized') or 0)
        per_model_agg[m]['overall_sum'] += ov
        overall_agg['overall_sum'] += ov
        for it in s['payload'].get('items', []):
            cat = it.get('category_en')
            if cat in per_model_agg[m]['cat_scores']:
                sc = float(it.get('score') or 0)
                per_model_agg[m]['cat_scores'][cat]['earned'] += sc
                per_model_agg[m]['cat_scores'][cat]['judged'] += 1
                overall_agg['cat_scores'][cat]['earned'] += sc
                overall_agg['cat_scores'][cat]['judged'] += 1

    def finalize_summary(agg, smoke_metrics):
        # Denominator is the FULL smoke10 sample count for this scope (overall: 1696;
        # per-model: 106). Sandbox-failed samples count as 0 — they were never judged
        # by GPT-5.5 but they're real failures, so the model shouldn't get credit.
        denom = (smoke_metrics or {}).get('samples_total') or agg.get('overall_n') or 0
        avg = (agg['overall_sum'] / denom) if denom else 0.0
        out = {
            'scope': agg['scope'],
            'samples': agg['samples'],
            'judged': agg['judged'],
            'avg_overall_score': avg,
            'category_scores': [],
        }
        if smoke_metrics:
            out.update(smoke_metrics)
        for c in CATEGORIES:
            cs = agg['cat_scores'][c]
            ratio = (cs['earned'] / denom) if denom else 0.0
            out['category_scores'].append({
                'key': c, 'category': c,
                'judged_items': cs['judged'],   # judged-only count (kept for reference)
                'denom': denom,                  # total samples (failed sandbox = 0 in numerator)
                'earned_points': cs['earned'],
                'max_points': denom * ITEM_MAX_SCORE,
                'ratio': ratio,
            })
        return out

    overall = finalize_summary(overall_agg, smoke_overall)
    per_model = [finalize_summary(per_model_agg[m], smoke_per_model.get(m)) for m in sorted(per_model_agg.keys())]

    # Build per-case payloads
    tasks = []
    for case in sorted(by_case.keys()):
        # task spec / rubric markdown
        task_md = ''
        rubric_md = ''
        tp = TASK_ROOT / case / 'task.md'
        rp = RUBRIC_ROOT / case / 'rubric.md'
        if tp.exists():
            task_md = tp.read_text(encoding='utf-8')
        if rp.exists():
            rubric_md = rp.read_text(encoding='utf-8')

        models = []
        for s in sorted(by_case[case], key=lambda x: x['model']):
            model = s['model']; idx = s['sample']
            payload = s['payload']
            # Pull artifact paths from smoke10
            smoke = smoke_by_key.get((case, model, idx)) or {}
            base = f'{case}_{model}_{idx}'
            sample_dir = SMOKE_ROOT / 'runs' / case / model / f'sample_{idx}'

            def rel_if(p: Path):
                return f'../../bench_step3_smoke10/runs/{case}/{model}/sample_{idx}/{p.relative_to(sample_dir)}' if p.exists() else ''
            code_url = rel_if(sample_dir / 'code.py')
            svg_url = rel_if(sample_dir / 'drawing' / f'{base}.svg')
            drawing_png_url = rel_if(sample_dir / 'drawing' / f'{base}.png')
            render_png_url = rel_if(sample_dir / 'render' / f'{base}_render.png')
            step_url = rel_if(sample_dir / 'render' / f'{base}.step')
            stl_url = rel_if(sample_dir / 'render' / f'{base}.stl')

            # LLM items keyed by category
            llm_items = {}
            for it in payload.get('items', []):
                cat = it.get('category_en')
                if cat:
                    llm_items[cat] = {
                        'score': it.get('score'),
                        'rationale': it.get('rationale') or '',
                    }

            models.append({
                'model': model,
                'sample_index': idx,
                'code_url': code_url,
                'svg_url': svg_url,
                'drawing_png_url': drawing_png_url,
                'render_png_url': render_png_url,
                'step_url': step_url,
                'stl_url': stl_url,
                'sandbox_ok': smoke.get('sandbox_ok'),
                'geometry_valid': smoke.get('geometry_valid'),
                'watertight': smoke.get('watertight'),
                'watertight_strict': smoke.get('watertight_strict') if smoke.get('watertight_strict') is not None else smoke.get('watertight'),
                'manifold': smoke.get('manifold') if smoke.get('manifold') is not None else smoke.get('watertight'),
                'self_intersection_free': smoke.get('self_intersection_free'),
                'component_count_match': smoke.get('component_count_match'),
                'gt_component_count': smoke.get('gt_component_count'),
                'llm_overall_normalized': payload.get('overall_score_normalized'),
                'llm_overall_summary': payload.get('overall_summary') or '',
                'llm_items': llm_items,
            })

        criteria = load_case_rubric_criteria(case)
        tasks.append({
            'task_name': case,
            'task_markdown': task_md,
            'rubric_markdown': rubric_md,
            'reference_url': reference_url(case),
            'gt_component_count': next((m.get('gt_component_count') for m in models if m.get('gt_component_count') is not None), None),
            'rubric_criteria': criteria,  # {category: {pass, fail}}
            'models': models,
        })

    return {
        'overall': overall,
        'per_model': per_model,
        'tasks': tasks,
        'categories': CATEGORIES,
    }


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Human Eval Viewer (bench_evaluate / GPT-5.5 judge)</title>
  <style>
    :root {
      --bg: #f5f1e8; --panel: #fffaf0; --ink: #1f2937; --muted: #6b7280;
      --line: #d8cdb9; --accent: #0f766e; --good: #166534; --bad: #991b1b; --warn: #92400e;
      --shadow: 0 18px 40px rgba(74, 52, 24, 0.12); --radius: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--ink);
      font-family: "Avenir Next", "PingFang SC", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.14), transparent 28%),
        radial-gradient(circle at top right, rgba(194,65,12,0.12), transparent 24%),
        linear-gradient(180deg, #f7f2e9 0%, #efe7da 100%);
    }
    .shell { width: min(1480px, calc(100vw - 32px)); margin: 20px auto 60px; display: grid; gap: 18px; }
    .panel { background: rgba(255, 250, 240, 0.94); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px; }
    h1 { margin: 0 0 8px; font-size: 22px; }
    h2 { margin: 0 0 10px; font-size: 18px; }
    h3 { margin: 6px 0 8px; font-size: 15px; }
    h4 { margin: 4px 0; font-size: 13px; color: var(--muted); }
    .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .toolbar select, .toolbar button, .toolbar input[type=file] {
      font: inherit; padding: 8px 14px; border-radius: 10px; border: 1px solid var(--line);
      background: #fffdf6; cursor: pointer;
    }
    .toolbar button.primary { background: var(--accent); color: white; border-color: var(--accent); }
    .toolbar button.warn { background: #fff3e0; }
    .toolbar .grow { flex: 1; }
    .progress-pill { padding: 6px 12px; border-radius: 999px; background: #fff3e0; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    table th, table td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; }
    table th:first-child, table td:first-child { text-align: left; }
    table thead th { background: rgba(15,118,110,0.08); font-weight: 600; }
    .num { font-variant-numeric: tabular-nums; }
    .info-line { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .task-grid { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(280px, 1fr); gap: 16px; }
    .task-grid .full { grid-column: 1 / -1; }
    .ref-img { max-width: 100%; max-height: 360px; display: block; margin: 0 auto; border-radius: 10px; border: 1px solid var(--line); }
    .markdown { white-space: pre-wrap; line-height: 1.55; font-size: 13px; max-height: 460px; overflow: auto; padding: 8px 12px; background: #fffdf6; border: 1px solid var(--line); border-radius: 10px; }
    .markdown.compact { max-height: 280px; }
    details { background: #fffdf6; border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
    details + details { margin-top: 10px; }
    summary { cursor: pointer; font-weight: 600; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; max-height: 360px; overflow: auto; background: #fdf8ec; padding: 10px; border-radius: 8px; border: 1px solid var(--line); }
    .model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 16px; }
    .model-card { background: #fffdf6; border: 1px solid var(--line); border-radius: 14px; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
    .model-card.complete { border-color: var(--accent); box-shadow: 0 0 0 1px rgba(15,118,110,0.2) inset; }
    .model-card header { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
    .model-card header .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; background: #f3eadb; }
    .model-card header .badge.ok { background: #dcfce7; color: var(--good); }
    .model-card header .badge.bad { background: #fee2e2; color: var(--bad); }
    .model-card header .badge.warn { background: #fef3c7; color: var(--warn); }
    .model-card img.render { width: 100%; max-height: 260px; object-fit: contain; background: #fdf3df; border-radius: 8px; border: 1px solid var(--line); }
    .links { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; }
    .links a { color: var(--accent); text-decoration: none; padding: 3px 8px; border: 1px solid var(--line); border-radius: 6px; background: #fdf8ec; }
    .links a.disabled { color: var(--muted); pointer-events: none; opacity: 0.5; }
    .view-toggle { display: flex; gap: 6px; flex-wrap: wrap; }
    .view-toggle button { padding: 4px 10px; font-size: 12px; border: 1px solid var(--line); border-radius: 6px; background: #fdf8ec; cursor: pointer; }
    .view-toggle button.active { background: var(--accent); color: white; border-color: var(--accent); }
    .rubric-item { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: #fffdf6; }
    .rubric-item + .rubric-item { margin-top: 8px; }
    .rubric-item .rh { display: flex; gap: 10px; justify-content: space-between; align-items: baseline; flex-wrap: wrap; }
    .rubric-item .tag { font-size: 11px; color: var(--muted); }
    .score-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 6px; }
    .kind-label { font-size: 12px; color: var(--muted); min-width: 110px; font-weight: 600; }
    .cur-tag { font-size: 11px; color: var(--muted); margin-left: 4px; }
    .score-btn { padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line); background: #fdf8ec; cursor: pointer; font-size: 13px; }
    .score-btn.sel { background: var(--accent); color: white; border-color: var(--accent); }
    .score-btn[data-score="1"].sel { background: var(--good); border-color: var(--good); color: white; }
    .score-btn[data-score="0"].sel { background: var(--bad); border-color: var(--bad); color: white; }
    .level-text { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.5; }
    textarea { width: 100%; min-height: 56px; resize: vertical; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font: inherit; background: #fffdf6; }
    .llm-badge { padding: 6px 12px; border-radius: 8px; font-size: 12px; align-self: center; border: 1px dashed var(--line); }
    .llm-badge.llm-1 { background: #dcfce7; color: var(--good); border-style: solid; border-color: var(--good); }
    .llm-badge.llm-0 { background: #fee2e2; color: var(--bad); border-style: solid; border-color: var(--bad); }
    .llm-badge.llm-na { color: var(--muted); }
    .llm-rationale { margin-top: 6px; }
    .llm-rationale summary { font-size: 12px; color: var(--muted); }
    .match-tag { font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; }
    .match-tag.match { background: #dcfce7; color: var(--good); }
    .match-tag.mismatch { background: #fee2e2; color: var(--bad); }
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel">
      <h1>人工 Rubric 评估（LLM judge: GPT-5.5）</h1>
      <div class="info-line">数据来源：<code>bench_evaluate/runs/&lt;case&gt;/&lt;model&gt;/sample_*/score.json</code> + 资源来自 <code>bench_step3_smoke10/runs/</code>。GPT-5.5 评 6 个类目，每个 0/1（Pass/Fail）。</div>
      <div class="toolbar" style="margin-top: 12px;">
        <span class="grow"></span>
        <span class="progress-pill" id="progressPill">已评分 0 / 0</span>
        <input type="file" id="importFile" accept="application/json" style="display:none" />
        <button id="importBtn">导入 JSON</button>
        <button id="exportBtn" class="primary">导出 JSON</button>
        <button id="clearBtn" class="warn">清空当前任务</button>
      </div>
    </section>

    <section class="panel">
      <h2>按模型汇总（GPT-5.5 判分）</h2>
      <div class="info-line">每行覆盖该模型在 smoke10 上的全部样本（per-model = 106；overall = 1696）。Avg LLM 与各类目分的<strong>分母都是全样本数</strong>——sandbox 失败的样本未被 GPT-5.5 评分但仍计入分母（按 0 分计）。</div>
      <div id="summaryWrap" style="overflow:auto"></div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <h2 style="margin-right: auto;">逐任务人工打分</h2>
        <label for="taskSelect">选择任务：</label>
        <select id="taskSelect"></select>
      </div>
    </section>

    <section class="panel" id="taskPanel"></section>
    <section class="panel" id="modelsPanel"></section>
  </div>

  <script type="importmap">
    {
      "imports": {
        "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
        "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
      }
    }
  </script>
  <script type="module">
    import * as THREE from 'three';
    import { STLLoader } from 'three/addons/loaders/STLLoader.js';
    import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
    window.__three__ = { THREE, STLLoader, OrbitControls };
    document.dispatchEvent(new CustomEvent('three-ready'));
  </script>

  <script>
    const DATA = __DATA__;
    const STORAGE_KEY = 'human_eval_eval_scores_v1';
    const byId = (id) => document.getElementById(id);
    const fmtPct = (v) => `${(v * 100).toFixed(1)}%`;
    const fmtRatio = (v) => v.toFixed(3);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    let scores = (() => { try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') || {}; } catch { return {}; } })();
    function saveScores() { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(scores)); } catch {} }
    const scoreKey = (task, model) => `${task}::${model}`;
    function getEntry(task, model) {
      const k = scoreKey(task, model);
      if (!scores[k]) scores[k] = { task, model, items: {}, overall_notes: '', updated_at: null };
      return scores[k];
    }

    function summaryCell(item) {
      if (!item || !item.denom) return '<span style="color:var(--muted)">-</span>';
      // earned / total_samples (sandbox-failed counted as 0). judged_items shown for reference.
      const earned = item.earned_points | 0;
      const judgedHint = (item.judged_items != null && item.judged_items !== item.denom)
        ? ` <span style="color:var(--muted);font-size:10px" title="GPT-5.5 实际判过的样本数（其余 sandbox 失败计 0）">[judged ${item.judged_items}]</span>`
        : '';
      return `${fmtPct(item.ratio)} <span style="color:var(--muted);font-size:11px">(${earned}/${item.denom})</span>${judgedHint}`;
    }

    function renderSummary() {
      const rows = [DATA.overall, ...DATA.per_model];
      const cats = DATA.categories;
      const ratePct = (val, total) => (total ? `${(val/total*100).toFixed(1)}%` : '-');
      const html = ['<table><thead><tr>'];
      html.push('<th style="text-align:left">Scope</th>');
      html.push('<th title="smoke10 全部样本数">Total</th>');
      html.push('<th title="sandbox_ok / total">Sandbox OK</th>');
      html.push('<th title="geometry_valid / total (复合：watertight∧manifold∧intersection-free∧occt-valid∧volume∧bbox)">Geom Valid</th>');
      html.push('<th title="self_intersection_free / total — 模型表面无自相交">Intersection-free</th>');
      html.push('<th title="watertight_strict / total — 无 open edges（边只属于 ≤1 个面），即闭合表面">Watertight</th>');
      html.push('<th title="manifold / total — 无 non-manifold edges（每条边 ≤2 个面共享）">Manifold</th>');
      html.push('<th title="component_count_match / total">Comp Match</th>');
      html.push('<th title="GPT-5.5 判过的样本数（仅 sandbox-OK 样本）">Judged</th>');
      html.push('<th title="overall_score_normalized 之和 / 全部样本（sandbox 失败计 0）">Avg LLM</th>');
      cats.forEach(c => html.push(`<th title="该类目 Pass 数 / 全部样本（sandbox 失败计 0）">${esc(c)}</th>`));
      html.push('</tr></thead><tbody>');
      rows.forEach(r => {
        const tot = r.samples_total ?? r.samples ?? 0;
        html.push('<tr>');
        html.push(`<td style="text-align:left">${esc(r.scope)}</td>`);
        html.push(`<td class="num">${tot}</td>`);
        html.push(`<td class="num">${ratePct(r.sandbox_ok ?? 0, tot)}</td>`);
        html.push(`<td class="num">${ratePct(r.geometry_valid ?? 0, tot)}</td>`);
        html.push(`<td class="num">${ratePct(r.intersection_free ?? 0, tot)}</td>`);
        html.push(`<td class="num">${ratePct(r.watertight_strict ?? 0, tot)}</td>`);
        html.push(`<td class="num">${ratePct(r.manifold ?? 0, tot)}</td>`);
        html.push(`<td class="num">${ratePct(r.component_match ?? 0, tot)}</td>`);
        html.push(`<td class="num">${r.judged}</td>`);
        html.push(`<td class="num">${fmtRatio(r.avg_overall_score || 0)}</td>`);
        cats.forEach(c => {
          const item = (r.category_scores || []).find(x => x.key === c);
          html.push(`<td class="num">${summaryCell(item)}</td>`);
        });
        html.push('</tr>');
      });
      html.push('</tbody></table>');
      byId('summaryWrap').innerHTML = html.join('');
    }

    function renderTaskOptions() {
      const sel = byId('taskSelect');
      sel.innerHTML = DATA.tasks.map((t, i) => `<option value="${i}">${esc(t.task_name)} (${t.models.length} judged)</option>`).join('');
      sel.addEventListener('change', () => renderTask(parseInt(sel.value, 10)));
    }

    function renderTask(idx) {
      const task = DATA.tasks[idx];
      const refImg = task.reference_url
        ? `<img class="ref-img" src="${esc(task.reference_url)}" alt="reference" />`
        : '<div class="info-line">（未找到 reference 图片）</div>';
      const gtTag = task.gt_component_count != null ? ` <span class="tag" style="font-size:13px">gt_components=${task.gt_component_count}</span>` : '';
      byId('taskPanel').innerHTML = `
        <h2>${esc(task.task_name)}${gtTag}</h2>
        <div class="task-grid">
          <div>
            <h3>任务规范 (task.md)</h3>
            <div class="markdown">${esc(task.task_markdown || '(空)')}</div>
          </div>
          <div>
            <h3>Reference 渲染图</h3>
            ${refImg}
          </div>
          <div class="full">
            <details>
              <summary>展开完整 Rubric (rubric.md)</summary>
              <div class="markdown" style="margin-top:8px">${esc(task.rubric_markdown || '(空)')}</div>
            </details>
          </div>
        </div>
      `;
      renderModels(task);
      updateProgress();
    }

    function renderModels(task) {
      const cats = DATA.categories;
      const cards = task.models.map(m => {
        const entry = getEntry(task.task_name, m.model);
        const allHumanScored = cats.every(c => {
          const it = entry.items[c]; if (!it) return false;
          // accept either new dual-score shape or legacy single-score
          if ('svg_score' in it || 'stp_score' in it) return it.svg_score != null && it.stp_score != null;
          return it.score != null;
        });
        const cardClass = allHumanScored ? 'model-card complete' : 'model-card';

        const flagBadges = [];
        if (m.sandbox_ok != null) flagBadges.push(`<span class="badge ${m.sandbox_ok ? 'ok' : 'bad'}">Sandbox ${m.sandbox_ok ? 'OK' : 'FAIL'}</span>`);
        if (m.geometry_valid != null) flagBadges.push(`<span class="badge ${m.geometry_valid ? 'ok' : 'warn'}">Geom ${m.geometry_valid ? 'OK' : 'FAIL'}</span>`);
        if (m.watertight_strict != null) flagBadges.push(`<span class="badge ${m.watertight_strict ? 'ok' : 'warn'}" title="无 open edges">Watertight ${m.watertight_strict ? 'OK' : 'FAIL'}</span>`);
        if (m.manifold != null) flagBadges.push(`<span class="badge ${m.manifold ? 'ok' : 'warn'}" title="无 non-manifold edges">Manifold ${m.manifold ? 'OK' : 'FAIL'}</span>`);
        if (m.self_intersection_free != null) flagBadges.push(`<span class="badge ${m.self_intersection_free ? 'ok' : 'warn'}">Intersect-free ${m.self_intersection_free ? 'OK' : 'FAIL'}</span>`);
        if (m.component_count_match != null) flagBadges.push(`<span class="badge ${m.component_count_match ? 'ok' : 'warn'}">Comp ${m.component_count_match ? '=' : '≠'}</span>`);

        const linkHtml = (label, url, dl) => {
          if (!url) return `<a class="disabled">${label}</a>`;
          const fname = url.split('/').pop() || 'download';
          return dl
            ? `<a href="${esc(url)}" download="${esc(fname)}" type="application/octet-stream">${label}</a>`
            : `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`;
        };

        const cardKey = `${task.task_name}::${m.model}`;
        const hasPng = !!m.render_png_url;
        const hasSvg = !!m.svg_url;
        const hasDrawingPng = !!m.drawing_png_url;
        const has3d = !!m.stl_url;
        const defaultView = hasPng ? 'png' : (hasSvg ? 'svg' : (hasDrawingPng ? 'dpng' : (has3d ? '3d' : null)));
        const visualPanel = (hasPng || hasSvg || hasDrawingPng || has3d) ? `
          <div class="view-toggle" data-card="${esc(cardKey)}">
            ${hasPng ? `<button data-view="png"${defaultView==='png'?' class="active"':''}>3D 渲染图</button>` : ''}
            ${hasDrawingPng ? `<button data-view="dpng"${defaultView==='dpng'?' class="active"':''}>Drawing PNG</button>` : ''}
            ${hasSvg ? `<button data-view="svg"${defaultView==='svg'?' class="active"':''}>4 视图 SVG</button>` : ''}
            ${has3d ? `<button data-view="3d"${defaultView==='3d'?' class="active"':''}>3D 模型 (拖动)</button>` : ''}
          </div>
          ${hasPng ? `<img class="render view-png" data-card="${esc(cardKey)}" src="${esc(m.render_png_url)}"${defaultView==='png'?'':' style="display:none"'} />` : ''}
          ${hasDrawingPng ? `<img class="render view-dpng" data-card="${esc(cardKey)}" src="${esc(m.drawing_png_url)}"${defaultView==='dpng'?'':' style="display:none"'} />` : ''}
          ${hasSvg ? `<img class="render view-svg" data-card="${esc(cardKey)}" src="${esc(m.svg_url)}"${defaultView==='svg'?'':' style="display:none"'} />` : ''}
          ${has3d ? `<div class="stl-viewer view-3d" data-card="${esc(cardKey)}" data-stl="${esc(m.stl_url)}" data-step="${esc(m.step_url || '')}"${defaultView==='3d'?'':' style="display:none;height:300px;background:#1f2937;color:white;padding:14px;font-size:12px"'}><div class="loading">点击切换至 3D 模型时加载…</div></div>` : ''}
        ` : '<div style="padding:24px;text-align:center;color:var(--muted)">无生成可视化</div>';

        const llmOverall = m.llm_overall_normalized != null ? Number(m.llm_overall_normalized).toFixed(2) : '-';
        const judgeBox = m.llm_overall_summary
          ? `<details><summary>GPT-5.5 总体反馈 (overall=${llmOverall})</summary><div class="markdown compact">${esc(m.llm_overall_summary)}</div></details>`
          : '';

        const criteria = task.rubric_criteria || {};
        const itemsHtml = cats.map(cat => {
          const llm = (m.llm_items || {})[cat];
          const llmCls = llm && llm.score != null ? `llm-badge llm-${llm.score}` : 'llm-badge llm-na';
          const llmText = llm && llm.score != null ? `LLM: <strong>${llm.score === 1 ? 'Pass' : 'Fail'}</strong>` : 'LLM: 未判';
          const llmRationale = llm && llm.rationale
            ? `<details class="llm-rationale"><summary>LLM 评分理由</summary><div class="level-text">${esc(llm.rationale)}</div></details>`
            : '';
          const crit = criteria[cat];
          const critHtml = crit
            ? `<details class="llm-rationale" open>
                 <summary style="color:var(--ink);font-weight:600;font-size:12px">评分细则 (Pass/Fail 准则)</summary>
                 <div class="level-text" style="margin-top:6px">
                   ${crit.pass ? `<div style="margin-bottom:6px"><strong style="color:var(--good)">1 Pass：</strong>${esc(crit.pass)}</div>` : ''}
                   ${crit.fail ? `<div><strong style="color:var(--bad)">0 Fail：</strong>${esc(crit.fail)}</div>` : ''}
                 </div>
               </details>`
            : '';
          const cell = entry.items[cat] || { svg_score: null, stp_score: null, note: '' };
          // Backwards-compat: migrate old single-score shape
          if ('score' in cell && !('svg_score' in cell)) {
            cell.svg_score = cell.score; cell.stp_score = null; delete cell.score;
          }
          const makeButtons = (mode, current) => [1, 0].map(sc => {
            const sel = current === sc ? ' sel' : '';
            const lbl = sc === 1 ? '1 (Pass)' : '0 (Fail)';
            return `<button class="score-btn${sel}" data-task="${esc(task.task_name)}" data-model="${esc(m.model)}" data-cat="${esc(cat)}" data-mode="${mode}" data-score="${sc}">${lbl}</button>`;
          }).join('');
          // matchHint: highlight when LLM agrees with at least one human dim
          const matchHint = (llm && llm.score != null && (cell.svg_score != null || cell.stp_score != null))
            ? (() => {
                const dims = [];
                if (cell.svg_score === llm.score) dims.push('SVG');
                if (cell.stp_score === llm.score) dims.push('STP');
                if (dims.length === 2) return '<span class="match-tag match">SVG/STP 与 LLM 一致</span>';
                if (dims.length === 1) return `<span class="match-tag match">${dims[0]} 与 LLM 一致</span>`;
                return '<span class="match-tag mismatch">与 LLM 不一致</span>';
              })()
            : '';
          return `
            <div class="rubric-item">
              <div class="rh">
                <div><strong>${esc(cat)}</strong></div>
                <div class="tag">${matchHint}</div>
              </div>
              ${critHtml}
              <div class="score-row">
                <span class="kind-label">基于 4 视图 SVG：</span>
                ${makeButtons('svg', cell.svg_score)}
              </div>
              <div class="score-row">
                <span class="kind-label">基于 STP / 3D 模型：</span>
                ${makeButtons('stp', cell.stp_score)}
              </div>
              <div class="score-row">
                <span class="${llmCls}">${llmText}</span>
              </div>
              ${llmRationale}
              <textarea data-task="${esc(task.task_name)}" data-model="${esc(m.model)}" data-cat="${esc(cat)}" data-field="note" placeholder="备注 / 证据 ...">${esc(cell.note || '')}</textarea>
            </div>
          `;
        }).join('');

        return `
          <div class="${cardClass}" data-model="${esc(m.model)}">
            <header>
              <div>
                <div><strong>${esc(m.model)}</strong> <span class="tag">sample_${m.sample_index}</span></div>
                <div style="margin-top:4px">${flagBadges.join(' ')}</div>
              </div>
              <div class="links">
                ${linkHtml('STEP ↓', m.step_url, true)}
                ${linkHtml('STL ↓', m.stl_url, true)}
                ${linkHtml('SVG', m.svg_url)}
                ${linkHtml('Render PNG', m.render_png_url)}
                ${linkHtml('code.py ↓', m.code_url, true)}
              </div>
            </header>
            ${visualPanel}
            ${judgeBox}
            <div>${itemsHtml}</div>
            <div>
              <h4>整体备注</h4>
              <textarea data-task="${esc(task.task_name)}" data-model="${esc(m.model)}" data-field="overall_notes" placeholder="整体观察 / 致命问题 / 备注...">${esc(getEntry(task.task_name, m.model).overall_notes || '')}</textarea>
            </div>
          </div>
        `;
      });
      byId('modelsPanel').innerHTML = `<h2>各模型评分（${task.models.length} 个 judged）</h2><div class="model-grid">${cards.join('')}</div>`;
      bindCardEvents();
    }

    function bindCardEvents() {
      document.querySelectorAll('#modelsPanel .score-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const task = btn.dataset.task; const model = btn.dataset.model;
          const cat = btn.dataset.cat; const mode = btn.dataset.mode; const score = parseInt(btn.dataset.score, 10);
          const entry = getEntry(task, model);
          const it = entry.items[cat] || { svg_score: null, stp_score: null, note: '' };
          if ('score' in it && !('svg_score' in it)) { it.svg_score = it.score; it.stp_score = null; delete it.score; }
          const field = `${mode}_score`;
          it[field] = it[field] === score ? null : score;
          entry.items[cat] = it;
          entry.updated_at = new Date().toISOString();
          saveScores();
          renderTask(parseInt(byId('taskSelect').value, 10));
        });
      });
      document.querySelectorAll('#modelsPanel textarea[data-field]').forEach(ta => {
        ta.addEventListener('input', () => {
          const task = ta.dataset.task; const model = ta.dataset.model;
          const entry = getEntry(task, model);
          if (ta.dataset.field === 'overall_notes') entry.overall_notes = ta.value;
          else if (ta.dataset.field === 'note') {
            const cat = ta.dataset.cat;
            entry.items[cat] = entry.items[cat] || { score: null, note: '' };
            entry.items[cat].note = ta.value;
          }
          entry.updated_at = new Date().toISOString();
          saveScores();
          updateProgress();
        });
      });
      document.querySelectorAll('#modelsPanel .view-toggle button').forEach(btn => {
        btn.addEventListener('click', () => {
          const cardKey = btn.parentElement.dataset.card;
          const view = btn.dataset.view;
          btn.parentElement.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
          ['png', 'dpng', 'svg', '3d'].forEach(v => {
            const el = document.querySelector(`.view-${v}[data-card="${CSS.escape(cardKey)}"]`);
            if (el) {
              el.style.display = view === v ? '' : 'none';
              if (view === '3d' && v === '3d' && !el.dataset.loaded) loadStlViewer(el);
            }
          });
        });
      });
      // Force JS-driven download for STEP/STL/code.py links — Chrome ignores the
      // `download` attribute on file:// URLs, so we fetch the blob ourselves.
      document.querySelectorAll('#modelsPanel a[download]').forEach(a => {
        if (a.dataset.dlBound) return;
        a.dataset.dlBound = '1';
        a.addEventListener('click', e => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // honor modifier-clicks
          e.preventDefault();
          jsDownload(a.href, a.getAttribute('download') || '');
        });
      });
    }

    async function jsDownload(url, filename) {
      try {
        const res = await fetch(url);
        if (!res.ok && res.status !== 0) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = filename || (url.split('/').pop() || 'download');
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 2000);
      } catch (e) {
        alert(
          '下载受浏览器 file:// 限制（' + (e && e.message || e) + '）。\n\n' +
          '请用任一方式解决：\n' +
          '  1) 双击同目录下 start_server.command（macOS）\n' +
          '     或运行 python3 -m http.server 8765 后访问 http://localhost:8765/\n' +
          '  2) 在链接上右键 → 链接另存为...\n' +
          '  3) 改用 Safari 打开 index.html（Safari 通常允许 file:// 下载）'
        );
      }
    }

    let threeReady = !!window.__three__;
    document.addEventListener('three-ready', () => { threeReady = true; flushPending(); });
    const pending = [];
    function flushPending() { while (pending.length) loadStlViewer(pending.shift()); }
    function loadStlViewer(container) {
      if (!threeReady) { pending.push(container); return; }
      const url = container.dataset.stl; if (!url) return;
      container.dataset.loaded = '1';
      container.innerHTML = '<div class="loading" style="color:white;padding:14px">加载中…</div>';
      const { THREE, STLLoader, OrbitControls } = window.__three__;
      const scene = new THREE.Scene(); scene.background = new THREE.Color(0x1f2937);
      const w = container.clientWidth || 360, h = container.clientHeight || 300;
      const camera = new THREE.PerspectiveCamera(45, w / h, 0.01, 10000);
      const renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(window.devicePixelRatio); renderer.setSize(w, h);
      scene.add(new THREE.AmbientLight(0xffffff, 0.55));
      const dl1 = new THREE.DirectionalLight(0xffffff, 0.6); dl1.position.set(1, 1, 1); scene.add(dl1);
      const dl2 = new THREE.DirectionalLight(0xffffff, 0.4); dl2.position.set(-1, -0.5, -1); scene.add(dl2);
      new STLLoader().load(url, geometry => {
        geometry.computeVertexNormals(); geometry.center();
        const mat = new THREE.MeshPhongMaterial({ color: 0xfacc15, specular: 0x222222, shininess: 30 });
        const mesh = new THREE.Mesh(geometry, mat); scene.add(mesh);
        geometry.computeBoundingBox(); const bb = geometry.boundingBox;
        const sz = new THREE.Vector3(); bb.getSize(sz);
        const md = Math.max(sz.x, sz.y, sz.z) || 1;
        camera.position.set(md * 1.6, md * 1.2, md * 1.8); camera.lookAt(0, 0, 0);
        camera.near = md / 100; camera.far = md * 100; camera.updateProjectionMatrix();
        container.innerHTML = ''; container.appendChild(renderer.domElement);
        const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true;
        let alive = true;
        function animate() { if (!alive) return; controls.update(); renderer.render(scene, camera); requestAnimationFrame(animate); } animate();
        const mo = new MutationObserver(() => { if (!document.body.contains(container)) { alive = false; renderer.dispose(); geometry.dispose(); mat.dispose(); mo.disconnect(); } });
        mo.observe(document.body, { childList: true, subtree: true });
      }, undefined, err => {
        const stlName = url.split('/').pop() || 'model.stl';
        const stepUrl = (container.dataset.step || '').trim();
        const stepName = stepUrl ? (stepUrl.split('/').pop() || 'model.step') : '';
        const stepBtn = stepUrl
          ? `<button data-dl-url="${esc(stepUrl)}" data-dl-name="${esc(stepName)}" style="padding:8px 14px;background:#0f766e;color:white;border:none;border-radius:6px;cursor:pointer;font-weight:600;margin-left:8px">📐 下载 STEP</button>`
          : '';
        container.innerHTML = `
          <div style="color:#fca5a5;padding:14px;font-size:13px;line-height:1.6">
            <div style="margin-bottom:6px">❌ 3D 模型加载失败</div>
            <small style="color:#fde68a;display:block;margin-bottom:12px;word-break:break-all">${esc((err && err.message) || String(err))}</small>
            <button data-dl-url="${esc(url)}" data-dl-name="${esc(stlName)}" style="padding:8px 14px;background:#fbbf24;color:#1f2937;border:none;border-radius:6px;cursor:pointer;font-weight:600">📥 下载 STL</button>
            ${stepBtn}
            <div style="color:#9ca3af;font-size:11px;margin-top:12px">
              file:// 下浏览器经常拒绝加载 / 下载本地文件。如果上面也下不了，请双击根目录 <code style="color:#fde68a">start_server.command</code>，
              或换 Safari 打开。
            </div>
          </div>
        `;
        container.querySelectorAll('button[data-dl-url]').forEach(btn => {
          btn.addEventListener('click', () => jsDownload(btn.dataset.dlUrl, btn.dataset.dlName));
        });
      });
    }

    function updateProgress() {
      let scored = 0, total = 0;
      DATA.tasks.forEach(t => {
        t.models.forEach(m => {
          DATA.categories.forEach(c => {
            total += 2;  // svg + stp
            const e = scores[scoreKey(t.task_name, m.model)];
            const it = e && e.items && e.items[c];
            if (!it) return;
            if (it.svg_score != null) scored += 1;
            else if ('score' in it && it.score != null) scored += 1;  // legacy
            if (it.stp_score != null) scored += 1;
          });
        });
      });
      byId('progressPill').textContent = `已评分 ${scored} / ${total}`;
    }

    function exportJSON() {
      const out = {
        meta: { dataset: 'bench_evaluate (gpt-5.5 judge)', exported_at: new Date().toISOString() },
        tasks: DATA.tasks.map(t => ({
          task_name: t.task_name,
          models: t.models.map(m => {
            const entry = scores[scoreKey(t.task_name, m.model)] || { items: {}, overall_notes: '' };
            const items = DATA.categories.map(c => {
              const it = entry.items[c] || {};
              const svg_s = it.svg_score != null ? it.svg_score : (it.score != null ? it.score : null);
              const stp_s = it.stp_score != null ? it.stp_score : null;
              return {
                category: c,
                svg_score: svg_s,
                stp_score: stp_s,
                note: it.note || '',
                llm_score: (m.llm_items || {})[c] ? (m.llm_items[c].score) : null,
              };
            });
            return {
              model: m.model, sample_index: m.sample_index,
              items, overall_notes: entry.overall_notes || '',
              llm_overall: m.llm_overall_normalized,
              updated_at: entry.updated_at || null,
            };
          }),
        })),
      };
      const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url;
      a.download = `human_eval_eval_${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    }
    function importJSON(file) {
      const reader = new FileReader();
      reader.onload = e => {
        try {
          const parsed = JSON.parse(e.target.result);
          if (parsed && parsed.tasks) {
            parsed.tasks.forEach(t => {
              t.models.forEach(m => {
                const entry = getEntry(t.task_name, m.model);
                entry.items = {};
                (m.items || []).forEach(it => {
                  // Accept both new (svg_score/stp_score) and legacy (human_score) shapes
                  if ('svg_score' in it || 'stp_score' in it) {
                    entry.items[it.category] = {
                      svg_score: it.svg_score == null ? null : it.svg_score,
                      stp_score: it.stp_score == null ? null : it.stp_score,
                      note: it.note || '',
                    };
                  } else {
                    entry.items[it.category] = {
                      svg_score: (it.human_score == null ? null : it.human_score),
                      stp_score: null,
                      note: it.note || '',
                    };
                  }
                });
                entry.overall_notes = m.overall_notes || '';
              });
            });
          } else if (parsed && typeof parsed === 'object') { scores = parsed; }
          saveScores(); renderTask(parseInt(byId('taskSelect').value, 10));
          alert('导入成功');
        } catch (err) { alert('JSON 解析失败：' + err.message); }
      };
      reader.readAsText(file);
    }
    function clearCurrent() {
      const idx = parseInt(byId('taskSelect').value, 10);
      const task = DATA.tasks[idx];
      if (!confirm(`确定清空任务「${task.task_name}」的所有人工评分？`)) return;
      task.models.forEach(m => { delete scores[scoreKey(task.task_name, m.model)]; });
      saveScores(); renderTask(idx);
    }

    renderSummary();
    renderTaskOptions();
    if (DATA.tasks.length > 0) renderTask(0);
    byId('exportBtn').addEventListener('click', exportJSON);
    byId('importBtn').addEventListener('click', () => byId('importFile').click());
    byId('importFile').addEventListener('change', e => { if (e.target.files[0]) importJSON(e.target.files[0]); });
    byId('clearBtn').addEventListener('click', clearCurrent);
  </script>
</body>
</html>
"""


def main():
    data = build_data()
    payload = json.dumps(data, ensure_ascii=False)
    html = HTML.replace("__DATA__", payload)
    OUT.write_text(html, encoding='utf-8')
    n_models = sum(len(t['models']) for t in data['tasks'])
    print(f'Wrote {OUT} ({len(html):,} bytes)')
    print(f'  cases: {len(data["tasks"])}')
    print(f'  judged (case, model) pairs: {n_models}')
    print(f'  total rubric items: {n_models * len(data["categories"])}')


if __name__ == '__main__':
    main()
