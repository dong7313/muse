"""Pearson / Spearman / Kendall agreement at item / cell / system level
for three LLM judges (Gemini 3.1 Pro, GPT-4o, Claude) vs human SVG scores."""
import json, os
from collections import defaultdict
import numpy as np
from scipy.stats import pearsonr, spearmanr, kendalltau

BASE = os.environ.get('BENCH_STEP3_ROOT')
if not BASE:
    raise SystemExit("Set BENCH_STEP3_ROOT to bench_step3_smoke10/ (containing reports/records.json and the human-eval score json).")
HUMAN_NAME = os.environ.get('HUMAN_EVAL_JSON', 'human_eval_scores.json')
HUMAN = os.path.join(BASE, HUMAN_NAME)
REC   = os.path.join(BASE, 'reports/records.json')

with open(HUMAN) as f: human = json.load(f)
with open(REC) as f: records = json.load(f)

def parse_bd(s):
    if not s or s == '{}': return None
    try: return json.loads(s)
    except Exception: return None

def norm_sec(s):
    """Strip parenthetical descriptions and normalize whitespace/case
    so 'Integrity(Component Split Rationality)' and 'Integrity' match."""
    if s is None: return ''
    s = str(s).split('(')[0]
    return ''.join(s.lower().split())

def keyify(primary, secondary):
    return (norm_sec(primary), norm_sec(secondary))

JUDGES = {
    'Gemini 3.1 Pro': ('llm_judge_breakdown_json',         'llm_judge_score'),
    'GPT-4o'        : ('llm_judge_4o_breakdown_json',      'llm_judge_4o_score'),
    'Claude'        : ('llm_judge_claude_breakdown_json',  'llm_judge_claude_score'),
}

# index llm scores by (task, model, sample) for each judge
llm_idx = {jname: {} for jname in JUDGES}
for r in records:
    key = (r['task_name'], r['model_label'], r['sample_index'])
    for jname, (bd_field, ov_field) in JUDGES.items():
        bd = parse_bd(r.get(bd_field))
        items = {}
        if bd and 'items' in bd:
            for it in bd['items']:
                k = keyify(it.get('primary_category_en'), it.get('secondary_category_en'))
                items[k] = it.get('score')
        llm_idx[jname][key] = {'overall': r.get(ov_field), 'items': items}

# build paired arrays per judge (SVG channel only — STP not judged)
def build_pairs(jname):
    item_pairs, cell_pairs = [], []
    for t in human['tasks']:
        for m in t['models']:
            if m['updated_at'] is None:
                continue
            key = (t['task_name'], m['model'], m['sample_index'])
            llm = llm_idx[jname].get(key)
            if llm is None:
                continue
            for it in m['items']:
                k = keyify(it['primary'], it['secondary'])
                hs = it.get('svg_score')
                ls = llm['items'].get(k)
                if hs is not None and ls is not None:
                    item_pairs.append((int(hs), int(ls)))
            if m['svg']['ratio'] is not None and llm['overall'] is not None:
                cell_pairs.append((m['svg']['ratio'], llm['overall'], m['model']))
    return item_pairs, cell_pairs

def safe_corr(stat):
    def f(h, l):
        if np.std(h) == 0 or np.std(l) == 0:
            return float('nan')
        return float(stat(h, l).statistic)
    return f

P = safe_corr(pearsonr); S = safe_corr(spearmanr); K = safe_corr(kendalltau)

def boot_ci(fn, h, l, n=10000, seed=42):
    rng = np.random.default_rng(seed); vals = []
    for _ in range(n):
        idx = rng.integers(0, len(h), len(h))
        v = fn(h[idx], l[idx])
        if v is not None and np.isfinite(v): vals.append(v)
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))

def report(jname):
    item_pairs, cell_pairs = build_pairs(jname)
    print(f'\n############### {jname} ###############')
    print(f'item pairs={len(item_pairs)}  cell pairs={len(cell_pairs)}')
    if not item_pairs:
        print('  (no overlapping pairs)'); return None

    H = np.array([p[0] for p in item_pairs]); L = np.array([p[1] for p in item_pairs])
    Hc = np.array([p[0] for p in cell_pairs]); Lc = np.array([p[1] for p in cell_pairs])
    by_h, by_l = defaultdict(list), defaultdict(list)
    for h, l, mdl in cell_pairs:
        by_h[mdl].append(h); by_l[mdl].append(l)
    models = sorted(by_h.keys())
    Hs = np.array([np.mean(by_h[m]) for m in models])
    Ls = np.array([np.mean(by_l[m]) for m in models])

    out = {}
    for level, h, l in [('Item', H, L), ('Cell', Hc, Lc), ('System', Hs, Ls)]:
        n = len(h)
        row = {'n': n}
        for fn, fname in [(P, 'Pearson'), (S, 'Spearman'), (K, 'Kendall')]:
            pt = fn(h, l)
            if level == 'Item' and n >= 30:
                lo, hi = boot_ci(fn, h, l)
                row[fname] = (pt, lo, hi)
            else:
                row[fname] = (pt, None, None)
        out[level] = row
        print(f'\n  --- {level}-level (n={n}) ---')
        print(f'  Mean human={np.mean(h):.3f}  Mean LLM={np.mean(l):.3f}  Bias={np.mean(l)-np.mean(h):+.3f}')
        for fname in ['Pearson', 'Spearman', 'Kendall']:
            pt, lo, hi = row[fname]
            if lo is None:
                print(f'  {fname:<9} = {pt:.3f}')
            else:
                print(f'  {fname:<9} = {pt:.3f}   [{lo:.3f}, {hi:.3f}]')
    return out

results = {jname: report(jname) for jname in JUDGES}

# combined comparison table
print('\n\n============= SUMMARY (point estimates only) =============')
print(f'{"Judge":<18}{"Lvl":<8}{"n":>4}  {"Pearson":>9}  {"Spearman":>9}  {"Kendall":>9}')
for jname in JUDGES:
    if results[jname] is None: continue
    for level in ['Item', 'Cell', 'System']:
        row = results[jname][level]
        print(f'{jname:<18}{level:<8}{row["n"]:>4}  '
              f'{row["Pearson"][0]:>9.3f}  {row["Spearman"][0]:>9.3f}  {row["Kendall"][0]:>9.3f}')
