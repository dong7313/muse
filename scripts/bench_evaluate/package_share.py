"""Build a shareable, self-contained subset of the human_eval viewer:
  - randomly picks 20 of the ~106 cases (deterministic via --seed)
  - strips all GPT-5.5 LLM scores / rationales / overall summaries
  - rewrites artifact URLs so everything is local under share/assets/
  - copies just the artifacts those 20 cases × 16 models need
  - tars the whole share/ tree into share.tgz

Output:
  bench_evaluate/share/         (self-contained directory)
  bench_evaluate/share/index.html
  bench_evaluate/share/assets/runs/<case>/<model>/sample_X/...
  bench_evaluate/share/assets/reference_png/<case>.png
  bench_evaluate/share.tgz
"""
from __future__ import annotations
import argparse, json, random, shutil, sys, tarfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from build_human_eval import build_data, HTML, CATEGORIES, SMOKE_ROOT  # type: ignore

SHARE_DIR = ROOT / 'share'
ASSETS_DIR = SHARE_DIR / 'assets'
TARBALL = ROOT / 'share.tgz'


def remap_url(url: str) -> str:
    if not url:
        return ''
    # smoke10 runs → assets/runs/...
    marker = '../../bench_step3_smoke10/runs/'
    if marker in url:
        return 'assets/runs/' + url.split(marker, 1)[1]
    # bench_evaluate reference_png → assets/reference_png/...
    if url.startswith('../runs/_reference_png/'):
        return 'assets/reference_png/' + url.split('../runs/_reference_png/', 1)[1]
    return url


def resolve_source(url: str) -> Path | None:
    """Reverse a remapped (assets/...) URL back to the original on-disk path."""
    if not url:
        return None
    if url.startswith('assets/runs/'):
        return SMOKE_ROOT / 'runs' / url[len('assets/runs/'):]
    if url.startswith('assets/reference_png/'):
        return ROOT / 'runs' / '_reference_png' / url[len('assets/reference_png/'):]
    return None


def copy_artifact(remapped_url: str, copied: set[Path]) -> None:
    if not remapped_url:
        return
    src = resolve_source(remapped_url)
    if src is None or not src.exists():
        return
    dest = SHARE_DIR / remapped_url
    if dest in copied:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    copied.add(dest)


def patch_html_remove_llm(html: str) -> str:
    """Remove GPT-5.5 / LLM-score affordances from the HTML template."""
    repls: list[tuple[str, str]] = [
        # Title
        (
            "<title>Human Eval Viewer (bench_evaluate / GPT-5.5 judge)</title>",
            "<title>CAD Human Eval Viewer</title>",
        ),
        # H1
        (
            "<h1>人工 Rubric 评估（LLM judge: GPT-5.5）</h1>",
            "<h1>人工 Rubric 评估（CAD 设计）</h1>",
        ),
        # Top info-line
        (
            '<div class="info-line">数据来源：<code>bench_evaluate/runs/&lt;case&gt;/&lt;model&gt;/sample_*/score.json</code> + 资源来自 <code>bench_step3_smoke10/runs/</code>。GPT-5.5 评 6 个类目，每个 0/1（Pass/Fail）。</div>',
            '<div class="info-line">每个任务下展示多个模型生成的 CAD 结果。每个类目分别基于 <strong>SVG 4 视图</strong> 和 <strong>STP / 3D 模型</strong>各给一个 Pass/Fail 分；技术指标（Sandbox/Watertight/Manifold/Intersection-free）由 OCCT 验证器计算。</div>\n      <div class="info-line" style="background:#fef3c7;border:1px solid #d4a373;padding:8px 12px;border-radius:8px;margin-top:8px;color:#7c2d12">⚠️ 如果点 <strong>STEP/STL/code.py</strong> 打开成文本而不是下载文件，请双击同目录下的 <code>start_server.command</code>（macOS）或运行 <code>python3 -m http.server 8765</code> 然后访问 <code>http://localhost:8765/</code>。这是浏览器对 <code>file://</code> 的限制。</div>',
        ),
        # Summary section heading
        (
            "<h2>按模型汇总（GPT-5.5 判分）</h2>",
            "<h2>按模型汇总（技术指标）</h2>",
        ),
        # Summary section subtitle
        (
            '每行覆盖该模型在 smoke10 上的全部样本（per-model = 106；overall = 1696）。Avg LLM 与各类目分的<strong>分母都是全样本数</strong>——sandbox 失败的样本未被 GPT-5.5 评分但仍计入分母（按 0 分计）。',
            '每行为该模型在选定任务集上的技术指标统计。',
        ),
        # Summary table: drop "Judged"+"Avg LLM"+per-category columns
        (
            "html.push('<th title=\"GPT-5.5 判过的样本数（仅 sandbox-OK 样本）\">Judged</th>');\n      html.push('<th title=\"overall_score_normalized 之和 / 全部样本（sandbox 失败计 0）\">Avg LLM</th>');\n      cats.forEach(c => html.push(`<th title=\"该类目 Pass 数 / 全部样本（sandbox 失败计 0）\">${esc(c)}</th>`));",
            "// LLM columns hidden",
        ),
        # Summary table: drop the matching cells
        (
            "html.push(`<td class=\"num\">${r.judged}</td>`);\n        html.push(`<td class=\"num\">${fmtRatio(r.avg_overall_score || 0)}</td>`);\n        cats.forEach(c => {\n          const item = (r.category_scores || []).find(x => x.key === c);\n          html.push(`<td class=\"num\">${summaryCell(item)}</td>`);\n        });",
            "// LLM cells hidden",
        ),
        # Per-card "GPT-5.5 总体反馈" details
        (
            "const llmOverall = m.llm_overall_normalized != null ? Number(m.llm_overall_normalized).toFixed(2) : '-';\n        const judgeBox = m.llm_overall_summary\n          ? `<details><summary>GPT-5.5 总体反馈 (overall=${llmOverall})</summary><div class=\"markdown compact\">${esc(m.llm_overall_summary)}</div></details>`\n          : '';",
            "const judgeBox = '';",
        ),
        # Per-rubric-item LLM badge / rationale prep
        (
            "const llm = (m.llm_items || {})[cat];\n          const llmCls = llm && llm.score != null ? `llm-badge llm-${llm.score}` : 'llm-badge llm-na';\n          const llmText = llm && llm.score != null ? `LLM: <strong>${llm.score === 1 ? 'Pass' : 'Fail'}</strong>` : 'LLM: 未判';\n          const llmRationale = llm && llm.rationale\n            ? `<details class=\"llm-rationale\"><summary>LLM 评分理由</summary><div class=\"level-text\">${esc(llm.rationale)}</div></details>`\n            : '';",
            "const llm = null; const llmCls = ''; const llmText = ''; const llmRationale = '';",
        ),
        # Score-row: remove standalone LLM badge row (the SVG + STP rows stay)
        (
            '<div class="score-row">\n                <span class="${llmCls}">${llmText}</span>\n              </div>',
            '',
        ),
        # matchHint (与 LLM 一致 / 不一致 / 部分一致 IIFE)
        (
            "const matchHint = (llm && llm.score != null && (cell.svg_score != null || cell.stp_score != null))\n            ? (() => {\n                const dims = [];\n                if (cell.svg_score === llm.score) dims.push('SVG');\n                if (cell.stp_score === llm.score) dims.push('STP');\n                if (dims.length === 2) return '<span class=\"match-tag match\">SVG/STP 与 LLM 一致</span>';\n                if (dims.length === 1) return `<span class=\"match-tag match\">${dims[0]} 与 LLM 一致</span>`;\n                return '<span class=\"match-tag mismatch\">与 LLM 不一致</span>';\n              })()\n            : '';",
            "const matchHint = '';",
        ),
        # Task option label "judged" -> "models"
        (
            "${t.models.length} judged",
            "${t.models.length} models",
        ),
    ]
    for old, new in repls:
        if old not in html:
            print(f'  ! patch did not match (skipping): {old[:80]!r}...')
            continue
        html = html.replace(old, new)
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', type=int, default=20)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--no-tar', action='store_true')
    args = ap.parse_args()

    print('Building source data...')
    data = build_data()
    all_cases = [t['task_name'] for t in data['tasks']]
    print(f'all cases: {len(all_cases)}')

    rng = random.Random(args.seed)
    chosen = sorted(rng.sample(all_cases, k=min(args.cases, len(all_cases))))
    print(f'chosen {len(chosen)} cases (seed={args.seed}):')
    for c in chosen:
        print(f'  - {c}')

    # Reset share dir
    if SHARE_DIR.exists():
        shutil.rmtree(SHARE_DIR)
    ASSETS_DIR.mkdir(parents=True)

    # Filter tasks; remap URLs; strip LLM data; copy artifacts.
    # IMPORTANT: build_data() only includes models that GPT-5.5 judged (score.json exists).
    # We want ALL 16 models per case, sourced from smoke10 records, so the friend can
    # score every model variant — not only the gpt-5.5-judged subset.
    chosen_set = set(chosen)
    smoke_rows = json.loads((SMOKE_ROOT / 'reports' / 'records.json').read_text())
    tasks_by_case = {t['task_name']: t for t in data['tasks']}
    judged_models_by_key = {
        (t['task_name'], m['model'], m['sample_index']): m
        for t in data['tasks'] for m in t['models']
    }

    copied: set[Path] = set()
    tasks = []
    for case in chosen:
        base = tasks_by_case.get(case) or {
            'task_name': case,
            'task_markdown': '',
            'rubric_markdown': '',
            'reference_url': '',
            'gt_component_count': None,
            'rubric_criteria': {},
            'models': [],
        }
        base = dict(base)  # don't mutate cached
        base['reference_url'] = remap_url(base.get('reference_url', ''))
        copy_artifact(base['reference_url'], copied)

        # Pull every smoke10 row for this case (one per model_label)
        case_rows = [r for r in smoke_rows if r.get('task_name') == case]
        case_rows.sort(key=lambda r: (r.get('model_label') or '', r.get('sample_index') or 0))

        models = []
        for r in case_rows:
            model = r.get('model_label') or r.get('model_name') or 'unknown'
            idx = r.get('sample_index')
            existing = judged_models_by_key.get((case, model, idx))
            if existing is not None:
                m = dict(existing)
            else:
                # No score.json — synthesize artifact URLs directly from smoke10 layout.
                base_str = f'{case}_{model}_{idx}'
                sample_dir = SMOKE_ROOT / 'runs' / case / model / f'sample_{idx}'

                def rel_if(p: Path) -> str:
                    return (
                        f'../../bench_step3_smoke10/runs/{case}/{model}/sample_{idx}/{p.relative_to(sample_dir)}'
                        if p.exists() else ''
                    )

                m = {
                    'model': model,
                    'sample_index': idx,
                    'code_url': rel_if(sample_dir / 'code.py'),
                    'svg_url': rel_if(sample_dir / 'drawing' / f'{base_str}.svg'),
                    'drawing_png_url': rel_if(sample_dir / 'drawing' / f'{base_str}.png'),
                    'render_png_url': rel_if(sample_dir / 'render' / f'{base_str}_render.png'),
                    'step_url': rel_if(sample_dir / 'render' / f'{base_str}.step'),
                    'stl_url': rel_if(sample_dir / 'render' / f'{base_str}.stl'),
                    'sandbox_ok': r.get('sandbox_ok'),
                    'geometry_valid': r.get('geometry_valid'),
                    'watertight': r.get('watertight'),
                    'watertight_strict': r.get('watertight_strict') if r.get('watertight_strict') is not None else r.get('watertight'),
                    'manifold': r.get('manifold') if r.get('manifold') is not None else r.get('watertight'),
                    'self_intersection_free': r.get('self_intersection_free'),
                    'component_count_match': r.get('component_count_match'),
                    'gt_component_count': r.get('gt_component_count'),
                }
            # Strip LLM fields (in case existing entry had them)
            m.pop('llm_overall_normalized', None)
            m.pop('llm_overall_summary', None)
            m['llm_items'] = {}
            # Remap URLs and copy artifacts
            for k in ('code_url', 'svg_url', 'drawing_png_url', 'render_png_url', 'step_url', 'stl_url'):
                m[k] = remap_url(m.get(k, ''))
                copy_artifact(m[k], copied)
            models.append(m)

        models.sort(key=lambda x: x['model'])
        base['models'] = models
        tasks.append(base)

    # Recompute summary metrics over the 20-case subset only.
    sub_smoke = [r for r in smoke_rows if r.get('task_name') in chosen_set]

    by_model: dict[str, list] = defaultdict(list)
    for r in sub_smoke:
        by_model[r.get('model_label') or r.get('model_name') or 'unknown'].append(r)

    def block(rows):
        n = len(rows) or 1
        sb = sum(1 for r in rows if r.get('sandbox_ok'))
        geom = sum(1 for r in rows if r.get('geometry_valid'))
        wstrict = sum(1 for r in rows if (r.get('watertight_strict') if r.get('watertight_strict') is not None else r.get('watertight')))
        manif = sum(1 for r in rows if (r.get('manifold') if r.get('manifold') is not None else r.get('watertight')))
        intf = sum(1 for r in rows if r.get('self_intersection_free'))
        comp = sum(1 for r in rows if r.get('component_count_match'))
        return {
            'samples_total': len(rows),
            'sandbox_ok': sb, 'sandbox_ok_rate': sb / n,
            'geometry_valid': geom, 'geometry_valid_rate': geom / n,
            'watertight_strict': wstrict, 'watertight_strict_rate': wstrict / n,
            'manifold': manif, 'manifold_rate': manif / n,
            'intersection_free': intf, 'intersection_free_rate': intf / n,
            'component_match': comp, 'component_match_rate': comp / n,
        }

    overall = {
        'scope': 'Overall',
        'samples': len(sub_smoke), 'judged': len(sub_smoke),
        'avg_overall_score': 0.0, 'category_scores': [],
        **block(sub_smoke),
    }
    per_model = []
    for m, rs in sorted(by_model.items()):
        per_model.append({
            'scope': m,
            'samples': len(rs), 'judged': len(rs),
            'avg_overall_score': 0.0, 'category_scores': [],
            **block(rs),
        })

    payload = {
        'overall': overall,
        'per_model': per_model,
        'tasks': tasks,
        'categories': CATEGORIES,
    }

    print('Patching HTML to hide LLM scoring...')
    html = patch_html_remove_llm(HTML)
    html = html.replace('__DATA__', json.dumps(payload, ensure_ascii=False))

    out_html = SHARE_DIR / 'index.html'
    out_html.write_text(html, encoding='utf-8')

    # Local-server launcher (some browsers refuse download / 3D loading on file://)
    launcher = SHARE_DIR / 'start_server.command'
    launcher.write_text(
        '#!/bin/bash\n'
        '# Double-click on macOS to start a local web server and open the viewer.\n'
        'cd "$(dirname "$0")"\n'
        'PORT=8765\n'
        'echo "Serving $(pwd) at http://localhost:$PORT/"\n'
        'echo "Press Ctrl+C in this window to stop."\n'
        '( sleep 1 && open "http://localhost:$PORT/index.html" ) &\n'
        'python3 -m http.server "$PORT"\n',
        encoding='utf-8',
    )
    launcher.chmod(0o755)

    readme = SHARE_DIR / 'README.txt'
    readme.write_text(
        'CAD Human Eval — 使用说明\n'
        '================================\n\n'
        '直接打开:\n'
        '  双击 index.html，浏览器即可使用。\n\n'
        '⚠️ 如果点 STEP / STL / code.py 链接打开成文本而不是下载:\n'
        '  这是浏览器对 file:// URL 的限制。请改用本地服务器:\n\n'
        '  方法一 (macOS): 双击 start_server.command\n'
        '  方法二 (任意系统): 在本目录运行 `python3 -m http.server 8765`\n'
        '    再用浏览器打开 http://localhost:8765/index.html\n\n'
        '操作:\n'
        '  - 每个任务下展示 16 个模型生成的 CAD 结果\n'
        '  - 每个类目按 SVG (4 视图) 和 STP / 3D 模型分别打 Pass/Fail (1/0)\n'
        '  - 完成后点页面顶部 "导出 JSON" 保存评分\n'
        '  - 关闭浏览器不会丢分: 数据存在 localStorage 里\n',
        encoding='utf-8',
    )

    # Quick stats
    n_files = sum(1 for _ in SHARE_DIR.rglob('*') if _.is_file())
    total_bytes = sum(p.stat().st_size for p in SHARE_DIR.rglob('*') if p.is_file())
    print(f'\nshare dir: {SHARE_DIR}')
    print(f'  cases: {len(tasks)}')
    print(f'  (case, model) cards: {sum(len(t["models"]) for t in tasks)}')
    print(f'  files: {n_files}  ({total_bytes / 1e6:.1f} MB uncompressed)')

    if not args.no_tar:
        if TARBALL.exists():
            TARBALL.unlink()
        print(f'\nWriting {TARBALL}...')
        with tarfile.open(TARBALL, 'w:gz') as tar:
            tar.add(SHARE_DIR, arcname='cad_human_eval_share')
        print(f'tarball: {TARBALL}  ({TARBALL.stat().st_size / 1e6:.1f} MB compressed)')


if __name__ == '__main__':
    main()
