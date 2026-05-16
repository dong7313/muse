"""Aggregate VLM-judge results (bench_evaluate{,_gemini,_4o}/runs/) + smoke10
sandbox/geometry metrics, then emit two LaTeX tables (S columns, zebra rows,
orange section dividers, bold-best per group, 3-stage layout, 6→3 pillar
mapping).

Run as:
  python generate_latex_tables_gemini.py --judge gemini   # default
  python generate_latex_tables_gemini.py --judge gpt5.5
  python generate_latex_tables_gemini.py --judge 4o
"""
from __future__ import annotations
import argparse, json, os
from collections import defaultdict
from pathlib import Path

ROOT          = Path(os.environ.get('RESULTS_ROOT', './results')).resolve()
SMOKE_RECORDS = ROOT / 'bench_step3_smoke10' / 'reports' / 'records.json'

# Map judge → (runs root, output filename, caption display name)
JUDGE_CFG = {
    'gpt5.5': (ROOT / 'bench_evaluate' / 'runs',
               ROOT / 'bench_evaluate' / 'reports' / 'paper_tables.tex',
               'GPT-5.5'),
    'gemini': (ROOT / 'bench_evaluate_gemini' / 'runs',
               ROOT / 'bench_evaluate' / 'reports' / 'paper_tables_gemini.tex',
               'Gemini-3.1-Pro'),
    '4o':     (ROOT / 'bench_evaluate_4o' / 'runs',
               ROOT / 'bench_evaluate' / 'reports' / 'paper_tables_4o.tex',
               'GPT-4o'),
}

CATEGORIES = [
    'Assembly Readiness', 'Joint Design', 'Tolerance',
    'Functional Adaptation', 'Usage Stability', 'Manufacturability',
]

PILLAR_SUBCATS = {
    # Order matches the figure: Functionality / Manufacturability / Assemblability
    'Functionality':     ('Functional Adaptation', 'Usage Stability'),
    'Manufacturability': ('Tolerance',             'Manufacturability'),
    'Assemblability':    ('Assembly Readiness',    'Joint Design'),
}

# Paper-style display names for the 6 rubric sub-criteria
CAT_DISPLAY = {
    'Functional Adaptation': 'Functional',
    'Usage Stability':       'Robust',
    'Tolerance':             'Well-toleranced',
    'Manufacturability':     'Manufacturable',
    'Assembly Readiness':    'Assembly-ready',
    'Joint Design':          'Connectable',
}

LABEL_REWRITE = {
    'closed-claude-3.7-sonnet':     'claude-3.7-sonnet',
    'closed-claude-opus-4.7':       'claude-opus-4.7',
    'closed-gemini-3.1-pro':        'gemini-3.1-pro',
    'closed-gpt-4o':                'gpt-4o',
    'closed-gpt-5.5':               'gpt-5.5',
    'closed-minimax-m2.5':          'minimax-m2.5',
    'closed-minimax-m2.7':          'minimax-m2.7',
    'closed-z-ai-glm-4.7-flash':    'glm-4.7-flash',
    'closed-z-ai-glm-5.1':          'glm-5.1',
    'oss-llama-3.1-70b':            'llama-3.1-70b',
    'oss-llama-3.1-8b':             'llama-3.1-8b',
    'oss-qwen-2.5-72b':             'qwen-2.5-72b',
    'oss-qwen-3.5-122b-a10b':       'qwen-3.5-122b-a10b',
    'oss-qwen-3.6-35b-a3b':         'qwen-3.6-35b-a3b',
    'oss-qwen-3.6-coder-next':      'qwen-3.6-coder',
}

CLOSED_ORDER = [
    'claude-opus-4.7', 'claude-3.7-sonnet',
    'gemini-3.1-pro',
    'glm-5.1', 'glm-4.7-flash',
    'gpt-5.5', 'gpt-4o',
    'minimax-m2.7', 'minimax-m2.5',
]
OPEN_ORDER = [
    'qwen-3.5-122b-a10b', 'qwen-2.5-72b', 'llama-3.1-70b',
    'qwen-3.6-35b-a3b', 'qwen-3.6-coder', 'llama-3.1-8b',
]


def pct(rate): return f'{rate*100:.2f}'


def fmt(val, best, eps=1e-6):
    s = pct(val)
    return r'\textbf{' + s + '}' if best is not None and abs(val - best) < eps else s


def zebra(idx):
    return r'\rowcolor{gray!7} ' if idx % 2 == 0 else ''


def group_bests(values_by_disp, group_displays, keys):
    out = {}
    for k in keys:
        vals = [values_by_disp[d][k] for d in group_displays if d in values_by_disp]
        if vals:
            out[k] = max(vals)
    return out


# ---------- aggregate ----------
# Stage 3 penalty rule (per user, "Option A"): a sample's Stage 3 (VLM-judged)
# scores count only if the sample passed EVERY upstream check — sandbox, the
# three OCCT geometry checks (watertight, manifold, self-intersection-free),
# and the interpenetration check. Any failure on Stage 1 or Stage 2 forces
# every Stage 3 sub-criterion to 0 for that sample. This matches the paper
# text: "a STEP file passes this stage only if all checks receive 1".
def _passes_upstream(row) -> bool:
    if not row.get('sandbox_ok'):
        return False
    if row.get('interpenetration_free') is False:
        return False
    wt = row.get('watertight_strict') if row.get('watertight_strict') is not None else row.get('watertight')
    if wt is False:
        return False
    manif = row.get('manifold') if row.get('manifold') is not None else row.get('watertight')
    if manif is False:
        return False
    if row.get('self_intersection_free') is False:
        return False
    return True


def aggregate(judge_runs: Path):
    smoke_rows = json.loads(SMOKE_RECORDS.read_text())
    smoke_by_model = defaultdict(list)
    for r in smoke_rows:
        smoke_by_model[r['model_label']].append(r)

    # (case, model, sample_idx) → True iff sample passed Stage 1 + Stage 2 fully
    upstream_pass = {
        (r['task_name'], r['model_label'], r['sample_index']): _passes_upstream(r)
        for r in smoke_rows
    }

    judge_per_model = defaultdict(lambda: {
        'judged': 0,
        'overall_sum': 0.0,
        'cat_earned': {c: 0.0 for c in CATEGORIES},
        'cat_judged': {c: 0   for c in CATEGORIES},
    })
    for case_dir in sorted(judge_runs.iterdir()):
        if not case_dir.is_dir() or case_dir.name.startswith('_'):
            continue
        for model_dir in sorted(case_dir.iterdir()):
            if not model_dir.is_dir():
                continue
            model = model_dir.name
            for sample_dir in sorted(model_dir.iterdir()):
                sj = sample_dir / 'score.json'
                if not sj.exists():
                    continue
                try:
                    payload = json.loads(sj.read_text())
                except Exception:
                    continue
                sample_idx = int(sample_dir.name.split('_', 1)[1])
                passed = upstream_pass.get((case_dir.name, model, sample_idx), False)
                agg = judge_per_model[model]
                agg['judged'] += 1
                if not passed:
                    # Stage 3 penalty: any upstream failure (sandbox / wt / manif /
                    # sif / interpenetration) forces every category score to 0.
                    for cat in CATEGORIES:
                        agg['cat_judged'][cat] += 1
                    # overall_sum gets 0 — no-op
                else:
                    agg['overall_sum'] += float(payload.get('overall_score_normalized') or 0)
                    for it in payload.get('items', []):
                        cat = it.get('category_en')
                        if cat in agg['cat_earned']:
                            agg['cat_earned'][cat] += float(it.get('score') or 0)
                            agg['cat_judged'][cat] += 1

    by_disp = {}
    for raw, disp in LABEL_REWRITE.items():
        smoke_rows_m = smoke_by_model.get(raw, [])
        denom = len(smoke_rows_m) or 0
        if denom == 0:
            continue

        # Stage 1/2 are RAW objective measurements — interpenetration penalty does
        # NOT apply here. The point of these columns is to expose the gap between
        # "code runs" (Sandbox) and "produces assemblable geometry" (Interpen.Free).
        sandbox = sum(1 for r in smoke_rows_m if r.get('sandbox_ok')) / denom
        wt_count = sum(1 for r in smoke_rows_m
                       if (r.get('watertight_strict') if r.get('watertight_strict') is not None
                           else r.get('watertight')) is True)
        wt   = wt_count / denom
        manif_count = sum(1 for r in smoke_rows_m
                          if (r.get('manifold') if r.get('manifold') is not None
                              else r.get('watertight')) is True)
        manif = manif_count / denom
        intf  = sum(1 for r in smoke_rows_m if r.get('self_intersection_free') is True) / denom
        ipf   = sum(1 for r in smoke_rows_m if r.get('interpenetration_free') is True) / denom

        # Stage 2 "Overall" = sample passed sandbox AND every binary OCCT check.
        # Equivalent to the Stage 3 gate.
        s2_overall = sum(1 for r in smoke_rows_m if _passes_upstream(r)) / denom

        agg = judge_per_model.get(raw, judge_per_model[raw])
        cat_ratio = {
            c: (agg['cat_earned'][c] / denom) for c in CATEGORIES
        }
        # Pillar overall = mean of two sub-criteria
        def pillar(p):
            a, b = PILLAR_SUBCATS[p]
            return (cat_ratio[a] + cat_ratio[b]) / 2.0

        s3_func = pillar('Functionality')
        s3_manu = pillar('Manufacturability')
        s3_asm  = pillar('Assemblability')
        # Stage 3 final score: simple mean of the three pillar overall scores.
        s3_final = (s3_func + s3_manu + s3_asm) / 3.0

        by_disp[disp] = {
            'sandbox':    sandbox,
            'wt':         wt,
            'manif':      manif,
            'intf':       intf,
            'ipf':        ipf,
            's2_overall': s2_overall,
            'func':       s3_func,
            'manu':       s3_manu,
            'asm':        s3_asm,
            's3_final':   s3_final,
            'cat':        cat_ratio,
            'denom':      denom,
            'judged':     agg['judged'],
        }
    return by_disp


# ---------- LaTeX emission ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--judge', choices=list(JUDGE_CFG), default='gemini',
                    help='Which judge results to summarise (default: gemini)')
    args = ap.parse_args()

    judge_runs, OUT, judge_display = JUDGE_CFG[args.judge]
    label_suffix = '' if args.judge == 'gpt5.5' else f'-{args.judge}'

    OUT.parent.mkdir(parents=True, exist_ok=True)
    by_disp = aggregate(judge_runs)

    # Coverage stats for caption
    cov = {d: by_disp[d]['judged'] / by_disp[d]['denom'] for d in by_disp}
    avg_cov = sum(cov.values()) / len(cov) * 100 if cov else 0.0

    # ===== Table 1 =====
    # Stage layout (matches paper figure):
    #   Stage 1 (Code Check)               : Sandbox Success
    #   Stage 2 (Geometry Check)           : Watertight | Manifold | Self-Int. Free | Overlap Free | Overall
    #   Stage 3 (Design Intent Alignment)  : Functionality | Manufacturability | Assemblability | Final Score
    KEYS_T1 = ['sandbox', 'wt', 'manif', 'intf', 'ipf', 's2_overall',
               'func', 'manu', 'asm', 's3_final']
    closed_best = group_bests(by_disp, CLOSED_ORDER, KEYS_T1)
    open_best   = group_bests(by_disp, OPEN_ORDER,   KEYS_T1)

    def row1(disp, best, idx):
        v = by_disp.get(disp)
        cells = [fmt(v[k], best.get(k)) for k in KEYS_T1] if v else ['-'] * len(KEYS_T1)
        return zebra(idx) + f'{disp} & ' + ' & '.join(cells) + r' \\'

    def avg_row1(disps):
        cells = []
        for k in KEYS_T1:
            vals = [by_disp[d][k] for d in disps if d in by_disp]
            cells.append(pct(sum(vals) / len(vals)) if vals else '-')
        return r'\rowcolor{gray!18} \textit{\textbf{Average}} & ' + ' & '.join(cells) + r' \\'

    overall = [
        r'\begin{table}[ht]',
        r'\centering',
        r'\caption{Per-model results across the three evaluation stages, judged by '
        r'\textit{' + judge_display + r'}. The table reports code execution, geometric '
        r'validity, and design-intent alignment scores under our funnel-style evaluation '
        r'protocol. Bold marks the best score per block.}',
        r'\label{tab:overall-metrics' + label_suffix + r'}',
        r'% Required preamble:',
        r'%   \usepackage{booktabs}',
        r'%   \usepackage[table]{xcolor}',
        r'%   \usepackage{siunitx}',
        r'%   \sisetup{detect-weight=true, detect-mode=true, table-number-alignment=center, mode=text}',
        r'\renewcommand{\arraystretch}{1.20}',
        r'\setlength{\tabcolsep}{3pt}',
        r'\scalebox{0.66}{',
        r'\begin{tabular}{l *{10}{S[table-format=2.2]}}',
        r'\toprule',
        r' & {\textbf{Code Check (\%)}} & \multicolumn{5}{c}{\textbf{Geometry Check (\%)}} '
        r'& \multicolumn{4}{c}{\textbf{Design Intent Alignment (\%)}} \\',
        r'\cmidrule(lr){2-2} \cmidrule(lr){3-7} \cmidrule(lr){8-11}',
        r'\textbf{Model} '
        r'& {\footnotesize Sandbox Success} '
        r'& {\footnotesize Watertight} '
        r'& {\footnotesize Manifold} '
        r'& {\footnotesize Self-Int.\ Free} '
        r'& {\footnotesize Overlap Free} '
        r'& {\footnotesize \textit{Geom.\ Valid}} '
        r'& {\footnotesize \textbf{Functionality}} '
        r'& {\footnotesize \textbf{Manufacturability}} '
        r'& {\footnotesize \textbf{Assemblability}} '
        r'& {\footnotesize \textit{Final Score}} \\',
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{11}{c}{\textit{\textbf{Closed-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    overall += [row1(d, closed_best, i) for i, d in enumerate(CLOSED_ORDER, start=1)]
    overall += [r'\addlinespace[1pt]', avg_row1(CLOSED_ORDER)]
    overall += [
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{11}{c}{\textit{\textbf{Open-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    overall += [row1(d, open_best, i) for i, d in enumerate(OPEN_ORDER, start=1)]
    overall += [r'\addlinespace[1pt]', avg_row1(OPEN_ORDER)]
    overall += [r'\bottomrule', r'\end{tabular}', r'}', r'\end{table}']

    # ===== Table 2 =====
    KEYS_T2 = []
    for pillar in PILLAR_SUBCATS:
        KEYS_T2 += [(pillar, 'sub1'), (pillar, 'sub2'), (pillar, 'overall')]

    detail_vals = {}
    for d, v in by_disp.items():
        out = {}
        for pillar, (a, b) in PILLAR_SUBCATS.items():
            out[(pillar, 'sub1')]    = v['cat'][a]
            out[(pillar, 'sub2')]    = v['cat'][b]
            out[(pillar, 'overall')] = (v['cat'][a] + v['cat'][b]) / 2.0
        detail_vals[d] = out

    closed_d_best = group_bests(detail_vals, CLOSED_ORDER, KEYS_T2)
    open_d_best   = group_bests(detail_vals, OPEN_ORDER,   KEYS_T2)

    def row2(disp, best, idx):
        v = detail_vals.get(disp)
        cells = [fmt(v[k], best.get(k)) for k in KEYS_T2] if v else ['-'] * 9
        return zebra(idx) + f'{disp} & ' + ' & '.join(cells) + r' \\'

    def avg_row2(disps):
        cells = []
        for k in KEYS_T2:
            vals = [detail_vals[d][k] for d in disps if d in detail_vals]
            cells.append(pct(sum(vals) / len(vals)) if vals else '-')
        return r'\rowcolor{gray!18} \textit{\textbf{Average}} & ' + ' & '.join(cells) + r' \\'

    detail = [
        r'',
        r'\begin{table}[ht]',
        r'\centering',
        r'\caption{Stage~3 sub-criterion breakdown judged by \textit{' + judge_display + r'}; '
        r'\textit{Average} is the mean of the two sub-criterion columns to its left. '
        r'Bold marks the best score per block.}',
        r'\label{tab:rubric-detailed-scores' + label_suffix + r'}',
        r'% Required preamble (same as Table~\ref{tab:overall-metrics' + label_suffix + r'}).',
        r'\renewcommand{\arraystretch}{1.20}',
        r'\setlength{\tabcolsep}{4pt}',
        r'\scalebox{0.8}{',
        r'\begin{tabular}{l *{9}{S[table-format=2.2]}}',
        r'\toprule',
        r' & \multicolumn{3}{c}{\textbf{Functionality (\%)}} '
        r'& \multicolumn{3}{c}{\textbf{Manufacturability (\%)}} '
        r'& \multicolumn{3}{c}{\textbf{Assemblability (\%)}} \\',
        r'\cmidrule(lr){2-4} \cmidrule(lr){5-7} \cmidrule(lr){8-10}',
        r'\textbf{Model} '
        r'& {\footnotesize Functional} & {\footnotesize Robust} & {\footnotesize \textit{Average}} '
        r'& {\footnotesize Well-toleranced} & {\footnotesize Manufacturable} & {\footnotesize \textit{Average}} '
        r'& {\footnotesize Assembly-ready} & {\footnotesize Connectable} & {\footnotesize \textit{Average}} \\',
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{10}{c}{\textit{\textbf{Closed-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    detail += [row2(d, closed_d_best, i) for i, d in enumerate(CLOSED_ORDER, start=1)]
    detail += [r'\addlinespace[1pt]', avg_row2(CLOSED_ORDER)]
    detail += [
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{10}{c}{\textit{\textbf{Open-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    detail += [row2(d, open_d_best, i) for i, d in enumerate(OPEN_ORDER, start=1)]
    detail += [r'\addlinespace[1pt]', avg_row2(OPEN_ORDER)]
    detail += [r'\bottomrule', r'\end{tabular}', r'}', r'\end{table}']

    out = '\n'.join(overall + detail) + '\n'
    OUT.write_text(out, encoding='utf-8')
    print(f'wrote {OUT} ({len(out)} bytes)')
    print(f'judge coverage (judged/denom) over {len(by_disp)} models, avg {avg_cov:.1f}%')
    print()
    print(out)


if __name__ == '__main__':
    main()
