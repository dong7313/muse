"""Pull aggregate data from human_eval.html and emit two LaTeX tables matching
the paper's 3-stage formulation:

  Stage 1 (Code Validity):    Sandbox Success
  Stage 2 (Geometric Validity): Watertight, Self-intersection-free (binary checks
                                from the paper text)
  Stage 3 (Design-Intent Alignment): Assemblability, Functionality, Manufacturability
                                     each = mean of two rubric sub-criteria

Subcriteria mapping (6 → 3 pillars, paper Table:design_intent_criteria):
  Assemblability      ← Assembly Readiness, Joint Design
  Functionality       ← Functional Adaptation, Usage Stability
  Manufacturability   ← Tolerance, Manufacturability

Denominator for every percentage: full smoke10 sample count for that model
(sandbox-failed sample = 0, never excluded).
"""
from __future__ import annotations
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / 'reports' / 'human_eval.html'
OUT = ROOT / 'reports' / 'paper_tables.tex'

# Display-name mapping (drop the closed-/oss- prefix and z-ai-/minimax- noise)
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

PILLAR_SUBCATS = {
    'Assemblability':    ('Assembly Readiness',    'Joint Design'),
    'Functionality':     ('Functional Adaptation', 'Usage Stability'),
    'Manufacturability': ('Tolerance',             'Manufacturability'),
}

# Closed-source: group same series together, within each series biggest→smallest.
# Series order: alphabetical (claude, gemini, glm, gpt, minimax).
CLOSED_ORDER = [
    'claude-opus-4.7', 'claude-3.7-sonnet',
    'gemini-3.1-pro',
    'glm-5.1', 'glm-4.7-flash',
    'gpt-5.5', 'gpt-4o',
    'minimax-m2.7', 'minimax-m2.5',
]
# Open-source: descending total parameter count.
OPEN_ORDER = [
    'qwen-3.5-122b-a10b',  # 122B (A10B MoE)
    'qwen-2.5-72b',        # 72B dense
    'llama-3.1-70b',       # 70B dense
    'qwen-3.6-35b-a3b',    # 35B (A3B MoE)
    'qwen-3.6-coder',      # ~30B (est.)
    'llama-3.1-8b',        # 8B dense
]

# Approximate parameter counts. Closed-source flagships are undisclosed; for
# open-source we report total params (and active params for MoE).
PARAMS = {
    'claude-opus-4.7':     'Undisc.',
    'claude-3.7-sonnet':   'Undisc.',
    'gemini-3.1-pro':      'Undisc.',
    'glm-5.1':             'Undisc.',
    'glm-4.7-flash':       'Undisc.',
    'gpt-5.5':             'Undisc.',
    'gpt-4o':              'Undisc.',
    'minimax-m2.7':        'Undisc.',
    'minimax-m2.5':        'Undisc.',
    'qwen-3.5-122b-a10b':  '122B (A10B)',
    'qwen-2.5-72b':        '72B',
    'llama-3.1-70b':       '70B',
    'qwen-3.6-35b-a3b':    '35B (A3B)',
    'qwen-3.6-coder':      r'$\sim$30B',
    'llama-3.1-8b':        '8B',
}


def load_data():
    html = HTML.read_text(encoding='utf-8')
    m = re.search(r'const DATA = (\{.*?\});\s*\n', html, re.DOTALL)
    if not m:
        raise SystemExit('DATA payload not found in human_eval.html')
    return json.loads(m.group(1))


def cat_lookup(per_model_row):
    return {c['key']: c for c in per_model_row.get('category_scores', [])}


def pct(rate):
    return f'{rate*100:.2f}'


def pillar_overall(catmap, pillar):
    """Mean of the two sub-criterion ratios for this pillar."""
    a, b = PILLAR_SUBCATS[pillar]
    ra = catmap[a]['ratio']; rb = catmap[b]['ratio']
    return (ra + rb) / 2.0


def group_bests(values_by_disp, group_displays, keys):
    """Per-column max within a group, used to decide which cells to bold."""
    out = {}
    for k in keys:
        vals = [values_by_disp[d][k] for d in group_displays if d in values_by_disp]
        if vals:
            out[k] = max(vals)
    return out


def fmt(val, best, eps=1e-6):
    s = pct(val)
    return r'\textbf{' + s + '}' if best is not None and abs(val - best) < eps else s


def zebra(idx):
    """Return rowcolor prefix for every other data row (idx 1-based, alternating)."""
    return r'\rowcolor{gray!7} ' if idx % 2 == 0 else ''


def main():
    data = load_data()
    rows_by_label = {r['scope']: r for r in data['per_model']}
    rows_by_display = {}
    for orig, disp in LABEL_REWRITE.items():
        if orig in rows_by_label:
            rows_by_display[disp] = rows_by_label[orig]

    missing = [d for d in CLOSED_ORDER + OPEN_ORDER if d not in rows_by_display]
    if missing:
        print(f'! warning, no data for: {missing}')

    # ==== Build Table 1: Overall (per-stage) ====
    OVERALL_KEYS = ['sandbox', 'wt', 'intf', 'asm', 'func', 'manu']

    def overall_values(disp):
        r = rows_by_display.get(disp)
        if r is None:
            return None
        cm = cat_lookup(r)
        return {
            'sandbox': r['sandbox_ok_rate'],
            'wt':      r.get('watertight_strict_rate', r.get('watertight_rate', 0)),
            'intf':    r['intersection_free_rate'],
            'asm':     pillar_overall(cm, 'Assemblability'),
            'func':    pillar_overall(cm, 'Functionality'),
            'manu':    pillar_overall(cm, 'Manufacturability'),
        }

    overall_vals = {d: v for d in (CLOSED_ORDER + OPEN_ORDER)
                    if (v := overall_values(d)) is not None}
    closed_best = group_bests(overall_vals, CLOSED_ORDER, OVERALL_KEYS)
    open_best   = group_bests(overall_vals, OPEN_ORDER,   OVERALL_KEYS)

    def overall_row(disp, best, idx):
        v = overall_vals.get(disp)
        if v is None:
            cells = ['-'] * len(OVERALL_KEYS)
        else:
            cells = [fmt(v[k], best.get(k)) for k in OVERALL_KEYS]
        return zebra(idx) + f'{disp} & ' + ' & '.join(cells) + r' \\'

    overall_lines = [
        r'\begin{table}[ht]',
        r'\centering',
        r'\caption{Per-model results across the three evaluation stages. '
        r'Bold marks the best score in each block; sandbox-failed samples count as $0$.}',
        r'\label{tab:overall-metrics}',
        r'% Required preamble:',
        r'%   \usepackage{booktabs}',
        r'%   \usepackage[table]{xcolor}',
        r'%   \usepackage{siunitx}',
        r'%   \sisetup{detect-weight=true, detect-mode=true, table-number-alignment=center, mode=text}',
        r'\renewcommand{\arraystretch}{1.20}',
        r'\setlength{\tabcolsep}{6pt}',
        r'\scalebox{0.78}{',
        r'\begin{tabular}{l *{6}{S[table-format=2.2]}}',
        r'\toprule',
        r' & {\textbf{Stage 1}} & \multicolumn{2}{c}{\textbf{Stage 2 (Geometric Validity)}} '
        r'& \multicolumn{3}{c}{\textbf{Stage 3 (Design-Intent Alignment)}} \\',
        r'\cmidrule(lr){2-2} \cmidrule(lr){3-4} \cmidrule(lr){5-7}',
        r'\textbf{Model} '
        r'& {\footnotesize Sandbox Success} '
        r'& {\footnotesize Watertight} '
        r'& {\footnotesize Self-Intersection Free} '
        r'& {\footnotesize \textbf{Assemblability}} '
        r'& {\footnotesize \textbf{Functionality}} '
        r'& {\footnotesize \textbf{Manufacturability}} \\',
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{7}{c}{\textit{\textbf{Closed-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    overall_lines += [overall_row(d, closed_best, i) for i, d in enumerate(CLOSED_ORDER, start=1)]
    overall_lines += [
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{7}{c}{\textit{\textbf{Open-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    overall_lines += [overall_row(d, open_best, i) for i, d in enumerate(OPEN_ORDER, start=1)]
    overall_lines += [r'\bottomrule', r'\end{tabular}', r'}', r'\end{table}']

    # ==== Build Table 2: Sub-criterion breakdown per pillar ====
    pillar_subhead = {
        'Assemblability':    ('Assembly Readiness',    'Joint Design'),
        'Functionality':     ('Functional Adaptation', 'Usage Stability'),
        'Manufacturability': ('Tolerance',             'Manufacturability'),
    }
    DETAIL_KEYS = []  # 9 column keys: ('Assemblability', 'sub1') ... ('Manufacturability', 'overall')
    for pillar in pillar_subhead:
        DETAIL_KEYS += [(pillar, 'sub1'), (pillar, 'sub2'), (pillar, 'overall')]

    def detail_values(disp):
        r = rows_by_display.get(disp)
        if r is None:
            return None
        cm = cat_lookup(r)
        out = {}
        for pillar, (a, b) in pillar_subhead.items():
            out[(pillar, 'sub1')]    = cm[a]['ratio']
            out[(pillar, 'sub2')]    = cm[b]['ratio']
            out[(pillar, 'overall')] = pillar_overall(cm, pillar)
        return out

    detail_vals = {d: v for d in (CLOSED_ORDER + OPEN_ORDER)
                   if (v := detail_values(d)) is not None}
    closed_d_best = group_bests(detail_vals, CLOSED_ORDER, DETAIL_KEYS)
    open_d_best   = group_bests(detail_vals, OPEN_ORDER,   DETAIL_KEYS)

    def detail_row(disp, best, idx):
        v = detail_vals.get(disp)
        if v is None:
            cells = ['-'] * 9
        else:
            cells = [fmt(v[k], best.get(k)) for k in DETAIL_KEYS]
        return zebra(idx) + f'{disp} & ' + ' & '.join(cells) + r' \\'

    detail_lines = [
        r'',
        r'\begin{table}[ht]',
        r'\centering',
        r'\caption{Stage~3 sub-criterion breakdown; \textit{Overall} is the mean of the two columns to its left. '
        r'Bold marks the best score in each block.}',
        r'\label{tab:rubric-detailed-scores}',
        r'% Required preamble (same as Table~\ref{tab:overall-metrics}):',
        r'%   \usepackage{booktabs}',
        r'%   \usepackage[table]{xcolor}',
        r'%   \usepackage{siunitx}',
        r'%   \sisetup{detect-weight=true, detect-mode=true, table-number-alignment=center, mode=text}',
        r'\renewcommand{\arraystretch}{1.20}',
        r'\setlength{\tabcolsep}{4pt}',
        r'\scalebox{0.74}{',
        r'\begin{tabular}{l *{9}{S[table-format=2.2]}}',
        r'\toprule',
        r' & \multicolumn{3}{c}{\textbf{Assemblability (\%)}} '
        r'& \multicolumn{3}{c}{\textbf{Functionality (\%)}} '
        r'& \multicolumn{3}{c}{\textbf{Manufacturability (\%)}} \\',
        r'\cmidrule(lr){2-4} \cmidrule(lr){5-7} \cmidrule(lr){8-10}',
        r'\textbf{Model} '
        r'& {\footnotesize Assembly Readiness} & {\footnotesize Joint Design} & {\footnotesize \textit{Overall}} '
        r'& {\footnotesize Functional Adaptation} & {\footnotesize Usage Stability} & {\footnotesize \textit{Overall}} '
        r'& {\footnotesize Tolerance} & {\footnotesize Manufacturability} & {\footnotesize \textit{Overall}} \\',
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{10}{c}{\textit{\textbf{Closed-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    detail_lines += [detail_row(d, closed_d_best, i) for i, d in enumerate(CLOSED_ORDER, start=1)]
    detail_lines += [
        r'\midrule',
        r'\addlinespace[2pt]',
        r'\rowcolor{orange!10}',
        r'\multicolumn{10}{c}{\textit{\textbf{Open-Source Models}}} \\',
        r'\addlinespace[1pt]',
    ]
    detail_lines += [detail_row(d, open_d_best, i) for i, d in enumerate(OPEN_ORDER, start=1)]
    detail_lines += [r'\bottomrule', r'\end{tabular}', r'}', r'\end{table}']

    out = '\n'.join(overall_lines + detail_lines) + '\n'
    OUT.write_text(out, encoding='utf-8')
    print(f'wrote {OUT} ({len(out)} bytes)')
    print()
    print(out)


if __name__ == '__main__':
    main()
