"""Build a self-contained human_eval.html for manual rubric scoring of bak_test results."""
import json
import os
import re
from collections import defaultdict
from pathlib import Path

# Point at the bench_step3 run dir.
ROOT = Path(os.environ.get("BENCH_STEP3_ROOT", "./bench_step3_smoke10")).resolve()
RECORDS = ROOT / "reports" / "records.json"
CATALOG = ROOT / "reports" / "rubric_catalog.json"
RUBRICS_DIR = Path(os.environ.get("RUBRIC_ROOT", ROOT / "rubrics")).resolve()
TASKS_DIR = Path(os.environ.get("TASK_ROOT", ROOT / "task")).resolve()
RUNS_DIR = ROOT / "runs"
OUT = ROOT / "reports" / "human_eval.html"


def relativize(path: str) -> str:
    if not path:
        return ""
    marker = f"{ROOT.name}/"
    if marker in path:
        return "../" + path.split(marker, 1)[1]
    return path


def file_url(p: Path) -> str:
    if not p.exists():
        return ""
    return "../" + str(p.relative_to(ROOT))


# ---------- Rubric parsing ----------

PRIMARY_HEADER = re.compile(r"^###\s+([IVX]+)\.\s+Category:\s+(.+?)\s*$", re.MULTILINE)
SUB_HEADER = re.compile(r"^####\s+(\d+)\.\s+Sub-category:\s+(.+?)\s*$", re.MULTILINE)


def parse_rubric(md: str):
    """Split rubric.md into primary -> [{secondary, body}]."""
    primaries = []
    primary_matches = list(PRIMARY_HEADER.finditer(md))
    for i, m in enumerate(primary_matches):
        start = m.end()
        end = primary_matches[i + 1].start() if i + 1 < len(primary_matches) else len(md)
        primary_block = md[start:end]
        sub_matches = list(SUB_HEADER.finditer(primary_block))
        subs = []
        for j, sm in enumerate(sub_matches):
            sub_start = sm.end()
            sub_end = sub_matches[j + 1].start() if j + 1 < len(sub_matches) else len(primary_block)
            sub_body = primary_block[sub_start:sub_end].strip()
            sub_name = sm.group(2).strip()
            details = parse_sub_details(sub_body)
            subs.append({
                "secondary": sub_name,
                "core_focus": details.get("core_focus", ""),
                "common_errors": details.get("common_errors", ""),
                "levels": details.get("levels", []),
                "raw": sub_body,
            })
        primaries.append({
            "primary": m.group(2).strip(),
            "subs": subs,
        })
    return primaries


def parse_sub_details(body: str):
    out = {"core_focus": "", "common_errors": "", "levels": []}
    cf = re.search(r"\*\s+\*\*Core Focus\*\*:\s*(.+?)(?=\n\s*\*\s+\*\*|$)", body, re.DOTALL)
    if cf:
        out["core_focus"] = cf.group(1).strip()
    ce = re.search(r"\*\s+\*\*Common (?:Visual )?(?:Generation )?Errors[^*]*\*\*:\s*(.+?)(?=\n\s*\*\s+\*\*|$)", body, re.DOTALL)
    if ce:
        out["common_errors"] = ce.group(1).strip()
    levels = []
    for lm in re.finditer(r"\*\s+\*\*(\d) Points?[^*]*\*\*[^:]*:\s*(.+?)(?=\n\s*\*\s+\*\*\d Point|\Z)", body, re.DOTALL):
        levels.append({"score": int(lm.group(1)), "description": lm.group(2).strip()})
    levels.sort(key=lambda x: -x["score"])
    out["levels"] = levels
    return out


# ---------- Path resolution per (task, model) ----------

def resolve_sample_assets(task: str, model: str, sample_idx):
    sample_dir = RUNS_DIR / task / model / f"sample_{sample_idx}"
    render_dir = sample_dir / "render"
    drawing_dir = sample_dir / "drawing"
    base = f"{task}_{model}_{sample_idx}"
    return {
        "code_url": file_url(sample_dir / "code.py"),
        "render_png_url": file_url(render_dir / f"{base}_render.png"),
        "step_url": file_url(render_dir / f"{base}.step"),
        "stl_url": file_url(render_dir / f"{base}.stl"),
        "svg_url": file_url(drawing_dir / f"{base}.svg"),
    }


def reference_url(task: str) -> str:
    return file_url(RUNS_DIR / task / "reference" / f"{task}.png")


# ---------- Summary computation (mirrors build_viewer.py for the table) ----------

ITEM_MAX_SCORE = 2.0


def safe_parse(value, fallback):
    if value in (None, "", "[]", "{}"):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def normalize_secondary(name: str) -> str:
    """Strip trailing parenthetical so 'Functional Adaptation (Size...)' matches 'Functional Adaptation'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip().lower()


def is_assembly_primary(name: str) -> bool:
    """Match Assembly / Assemblability / Assemblable case-insensitively."""
    return (name or "").strip().lower().startswith("assembl")


# Map primary-category aliases to a single canonical name so summary tables
# aggregate consistently across rubric variants ("Assemblability" vs "Assemblable",
# "Functionality" vs "Practical", "Constructability" vs "Manufacturable").
_PRIMARY_ALIAS_GROUPS = {
    "Assemblability": ("assemblability", "assemblable", "assembly"),
    "Functionality": ("functionality", "practical", "functional"),
    "Constructability": ("constructability", "manufacturable", "manufacturability"),
}
_PRIMARY_ALIAS_LOOKUP = {
    alias: canonical
    for canonical, aliases in _PRIMARY_ALIAS_GROUPS.items()
    for alias in aliases
}


def canonicalize_primary(name: str) -> str:
    if not name:
        return name or ""
    return _PRIMARY_ALIAS_LOOKUP.get(name.strip().lower(), name.strip())


def aggregate_categories(rows, canonical_secondaries):
    """
    Aggregate llm_judge breakdown items, using ALL rows in the denominator.
    Failed / unjudged samples count as 0 earned but are still in the expected count.

    canonical_secondaries: list of {"primary": str, "secondary": str} from rubric_struct.
    """
    n = len(rows)
    # Canonicalize primary names so different rubric spellings map to one bucket
    # ("Assemblability" / "Assemblable" / "Assembly", "Functionality" / "Practical", etc.)
    primaries = sorted({canonicalize_primary(c["primary"]) for c in canonical_secondaries})
    items_per_primary = {
        p: sum(1 for c in canonical_secondaries if canonicalize_primary(c["primary"]) == p)
        for p in primaries
    }

    prim = {p: {"key": p, "primary": p, "secondary": "",
                "expected_items": n * items_per_primary.get(p, 0),
                "earned": 0.0, "judged_items": 0, "judged_rows": set()}
            for p in primaries}
    sec = {(c["primary"], c["secondary"]): {"key": f"{c['primary']} / {c['secondary']}",
                                            "primary": c["primary"], "secondary": c["secondary"],
                                            "expected_items": n,
                                            "earned": 0.0, "judged_items": 0, "judged_rows": set()}
           for c in canonical_secondaries}

    norm_sec_lookup = {(canonicalize_primary(c["primary"]).lower(), normalize_secondary(c["secondary"])): (c["primary"], c["secondary"])
                       for c in canonical_secondaries}

    for idx, row in enumerate(rows):
        bd = safe_parse(row.get("llm_judge_breakdown_json"), {})
        items = bd.get("items") if isinstance(bd, dict) else None
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            p_raw = (it.get("primary_category_en") or "").strip()
            p = canonicalize_primary(p_raw)
            s = (it.get("secondary_category_en") or "").strip()
            try:
                score = float(it.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            if p in prim:
                prim[p]["earned"] += score
                prim[p]["judged_items"] += 1
                prim[p]["judged_rows"].add(idx)
            key = (p.lower(), normalize_secondary(s))
            if key in norm_sec_lookup:
                canonical_key = norm_sec_lookup[key]
                bucket = sec[canonical_key]
                bucket["earned"] += score
                bucket["judged_items"] += 1
                bucket["judged_rows"].add(idx)

    def finalize(d):
        out = []
        for v in d.values():
            expected = v["expected_items"]
            max_pts = expected * ITEM_MAX_SCORE
            ratio = (v["earned"] / max_pts) if max_pts else 0.0
            out.append({
                "key": v["key"],
                "primary_category": v["primary"],
                "secondary_category": v["secondary"],
                "item_count": expected,
                "earned_points": v["earned"],
                "max_points": max_pts,
                "weighted_score": ratio,
                "ratio": ratio,
                "judged_items": v["judged_items"],
                "judged_rows": len(v["judged_rows"]),
            })
        out.sort(key=lambda r: r["key"])
        return out

    return finalize(prim), finalize(sec)


def build_summary(rows, scope, canonical_secondaries):
    n = len(rows)
    denom = n if n else 1
    sandbox = sum(1 for r in rows if r.get("sandbox_ok") is True)
    geom = sum(1 for r in rows if r.get("geometry_valid") is True)
    water = sum(1 for r in rows if r.get("watertight") is True)
    comp = sum(1 for r in rows if r.get("component_count_match") is True)
    judged = [r for r in rows if not r.get("llm_judge_error") and (r.get("llm_judge_score") or 0) > 0]
    # avg_llm_judge_score: sum over all rows (failed = 0), denominator includes all
    avg_judge = (sum(float(r.get("llm_judge_score") or 0) for r in rows) / denom)
    prim, sec = aggregate_categories(rows, canonical_secondaries)
    return {
        "scope": scope, "samples": n, "sandbox_samples": sandbox, "llm_judge_samples": n,
        "geometry_valid_rate": geom / denom, "watertight_rate": water / denom,
        "component_match_rate": comp / denom, "sandbox_success_rate": sandbox / denom,
        "avg_llm_judge_score": avg_judge,
        "primary_category_scores": prim, "secondary_category_scores": sec,
    }


# ---------- Build full data payload ----------

def build_data():
    rows_raw = json.loads(RECORDS.read_text())
    catalog = json.loads(CATALOG.read_text())

    # Group rows by task
    rows_by_task = defaultdict(list)
    for r in rows_raw:
        rows_by_task[r["task_name"]].append(r)

    # Build per-task rubric structures first; the canonical taxonomy is shared across tasks.
    parsed_tasks = []
    for entry in catalog:
        task_name = entry["task_name"]
        task_md_path = TASKS_DIR / task_name / "task.md"
        rubric_md_path = RUBRICS_DIR / task_name / "rubric.md"
        task_markdown = task_md_path.read_text() if task_md_path.exists() else entry.get("task_markdown", "")
        rubric_markdown = rubric_md_path.read_text() if rubric_md_path.exists() else entry.get("rubric_markdown", "")
        rubric_struct = parse_rubric(rubric_markdown)
        parsed_tasks.append({"task_name": task_name, "task_markdown": task_markdown,
                             "rubric_markdown": rubric_markdown, "rubric_struct": rubric_struct})

    # Canonical (primary, secondary) list — taken from any task's rubric_struct (they share it).
    canonical_secondaries = []
    for pt in parsed_tasks:
        for primary in pt["rubric_struct"]:
            for sub in primary["subs"]:
                canonical_secondaries.append({"primary": primary["primary"], "secondary": sub["secondary"]})
        if canonical_secondaries:
            break

    # Per-model summary (for the top table)
    by_model = defaultdict(list)
    for r in rows_raw:
        by_model[r.get("model_label") or r.get("model_name") or "unknown"].append(r)
    overall = build_summary(rows_raw, "Overall", canonical_secondaries)
    per_model = [build_summary(arr, m, canonical_secondaries) for m, arr in sorted(by_model.items())]

    # gt_component_count per task — kept for display only; no longer used to filter Assembly.
    gt_comp_by_task = {}
    for r in rows_raw:
        if r["task_name"] not in gt_comp_by_task and r.get("gt_component_count") is not None:
            gt_comp_by_task[r["task_name"]] = r["gt_component_count"]

    # Build per-task payloads
    tasks = []
    for pt in parsed_tasks:
        task_name = pt["task_name"]
        task_markdown = pt["task_markdown"]
        rubric_markdown = pt["rubric_markdown"]
        rubric_struct = pt["rubric_struct"]
        gt_comp = gt_comp_by_task.get(task_name)

        models = []
        for r in sorted(rows_by_task.get(task_name, []), key=lambda x: x.get("model_label") or ""):
            model = r.get("model_label") or r.get("model_name") or "unknown"
            sample_idx = r.get("sample_index")
            assets = resolve_sample_assets(task_name, model, sample_idx)

            # LLM per-rubric-item scores keyed by canonical "<primary>::<normalized_secondary>".
            # Two judge passes: SVG-based (4-view technical drawing) and 3D-based (VTK render).
            def _parse_llm_breakdown(field: str):
                items_by_key: dict[str, dict] = {}
                summary = ""
                normalized = None
                bd_local = safe_parse(r.get(field), {})
                if isinstance(bd_local, dict):
                    summary = bd_local.get("overall_summary") or ""
                    normalized = bd_local.get("overall_score_normalized")
                    for it in (bd_local.get("items") or []):
                        if not isinstance(it, dict):
                            continue
                        p = (it.get("primary_category_en") or "").strip()
                        s = (it.get("secondary_category_en") or "").strip()
                        if not p or not s:
                            continue
                        key = f"{p}::{normalize_secondary(s)}"
                        items_by_key[key] = {
                            "score": it.get("score"),
                            "rationale": it.get("rationale") or "",
                            "primary": p,
                            "secondary": s,
                        }
                return items_by_key, summary, normalized

            llm_items_by_key, llm_overall_summary, llm_overall_normalized = _parse_llm_breakdown("llm_judge_breakdown_json")
            llm_3d_items_by_key, llm_3d_overall_summary, llm_3d_overall_normalized = _parse_llm_breakdown("llm_judge_3d_breakdown_json")

            models.append({
                "model": model,
                "sample_index": sample_idx,
                "run_id": r.get("run_id"),
                "code_url": assets["code_url"],
                "render_png_url": assets["render_png_url"],
                "step_url": assets["step_url"],
                "stl_url": assets["stl_url"],
                "svg_url": assets["svg_url"],
                "geometry_valid": r.get("geometry_valid"),
                "watertight": r.get("watertight"),
                "sandbox_ok": r.get("sandbox_ok"),
                "component_count_match": r.get("component_count_match"),
                "sandbox_error": r.get("sandbox_error") or "",
                "llm_judge_score": r.get("llm_judge_score"),
                "llm_judge_summary": r.get("llm_judge_summary") or "",
                "llm_overall_summary": llm_overall_summary,
                "llm_overall_normalized": llm_overall_normalized,
                "llm_items_by_key": llm_items_by_key,
                # 3D-render-based judge pass (parallel scores)
                "llm_judge_3d_score": r.get("llm_judge_3d_score"),
                "llm_3d_overall_summary": llm_3d_overall_summary,
                "llm_3d_overall_normalized": llm_3d_overall_normalized,
                "llm_3d_items_by_key": llm_3d_items_by_key,
            })

        tasks.append({
            "task_name": task_name,
            "task_markdown": task_markdown,
            "rubric_markdown": rubric_markdown,
            "rubric_struct": rubric_struct,
            "gt_component_count": gt_comp,
            "reference_url": reference_url(task_name),
            "models": models,
        })

    return {"overall": overall, "per_model": per_model, "tasks": tasks}


# ---------- HTML template ----------

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Human Eval Viewer</title>
  <style>
    :root {
      --bg: #f5f1e8; --panel: #fffaf0; --ink: #1f2937; --muted: #6b7280;
      --line: #d8cdb9; --accent: #0f766e; --accent-2: #c2410c;
      --good: #166534; --bad: #991b1b; --warn: #92400e;
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
    .panel {
      background: rgba(255, 250, 240, 0.94); border: 1px solid var(--line);
      border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px;
    }
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

    .task-grid { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(280px, 1fr); gap: 16px; }
    .task-grid .full { grid-column: 1 / -1; }
    .ref-img { max-width: 100%; max-height: 320px; display: block; margin: 0 auto; border-radius: 10px; border: 1px solid var(--line); }
    .markdown { white-space: pre-wrap; line-height: 1.55; font-size: 13px; max-height: 460px; overflow: auto; padding: 8px 12px; background: #fffdf6; border: 1px solid var(--line); border-radius: 10px; }
    .markdown.compact { max-height: 280px; }

    details { background: #fffdf6; border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
    details + details { margin-top: 10px; }
    summary { cursor: pointer; font-weight: 600; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; max-height: 360px; overflow: auto; background: #fdf8ec; padding: 10px; border-radius: 8px; border: 1px solid var(--line); }

    .model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 16px; }
    .model-card { background: #fffdf6; border: 1px solid var(--line); border-radius: 14px; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
    .model-card.complete { border-color: var(--accent); box-shadow: 0 0 0 1px rgba(15,118,110,0.2) inset; }
    .model-card header { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
    .model-card header .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; background: #f3eadb; }
    .model-card header .badge.ok { background: #dcfce7; color: var(--good); }
    .model-card header .badge.bad { background: #fee2e2; color: var(--bad); }
    .model-card header .badge.warn { background: #fef3c7; color: var(--warn); }
    .model-card img.render { width: 100%; max-height: 260px; object-fit: contain; background: #fdf3df; border-radius: 8px; border: 1px solid var(--line); }
    .stl-viewer { width: 100%; height: 320px; border-radius: 8px; border: 1px solid var(--line); background: #1f2937; position: relative; overflow: hidden; }
    .stl-viewer canvas { display: block; }
    .stl-viewer .hint { position: absolute; top: 8px; left: 10px; color: rgba(255,255,255,0.7); font-size: 11px; pointer-events: none; }
    .stl-viewer .err { color: #fca5a5; padding: 14px; font-size: 12px; }
    .stl-viewer .loading { color: rgba(255,255,255,0.7); padding: 14px; font-size: 12px; }
    .view-toggle { display: flex; gap: 6px; }
    .view-toggle button { padding: 4px 10px; font-size: 12px; border: 1px solid var(--line); border-radius: 6px; background: #fdf8ec; cursor: pointer; }
    .view-toggle button.active { background: var(--accent); color: white; border-color: var(--accent); }
    .links { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; }
    .links a { color: var(--accent); text-decoration: none; padding: 3px 8px; border: 1px solid var(--line); border-radius: 6px; background: #fdf8ec; }
    .links a.disabled { color: var(--muted); pointer-events: none; opacity: 0.5; }

    .rubric-item { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: #fffdf6; }
    .rubric-item + .rubric-item { margin-top: 8px; }
    .rubric-item .rh { display: flex; gap: 10px; justify-content: space-between; align-items: baseline; flex-wrap: wrap; }
    .rubric-item .tag { font-size: 11px; color: var(--muted); }
    .score-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; align-items: center; }
    .kind-label { font-size: 12px; color: var(--muted); min-width: 138px; font-weight: 600; }
    .cur-tag { font-size: 11px; color: var(--muted); margin-left: 4px; }
    .llm-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 6px; }
    .score-btn { padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line); background: #fdf8ec; cursor: pointer; font-size: 13px; }
    .score-btn.sel { background: var(--accent); color: white; border-color: var(--accent); }
    .score-btn[data-score="2"].sel { background: var(--good); border-color: var(--good); }
    .score-btn[data-score="1"].sel { background: var(--warn); border-color: var(--warn); color: white; }
    .score-btn[data-score="0"].sel { background: var(--bad); border-color: var(--bad); }
    .level-text { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.5; }
    .llm-badge { padding: 6px 12px; border-radius: 8px; font-size: 12px; align-self: center; border: 1px dashed var(--line); }
    .llm-badge.llm-2 { background: #dcfce7; color: var(--good); border-style: solid; border-color: var(--good); }
    .llm-badge.llm-1 { background: #fef3c7; color: var(--warn); border-style: solid; border-color: var(--warn); }
    .llm-badge.llm-0 { background: #fee2e2; color: var(--bad); border-style: solid; border-color: var(--bad); }
    .llm-badge.llm-na { color: var(--muted); }
    .llm-rationale { margin-top: 6px; }
    .llm-rationale summary { font-size: 12px; color: var(--muted); }
    .match-tag { font-size: 11px; margin-left: 6px; padding: 2px 6px; border-radius: 4px; }
    .match-tag.match { background: #dcfce7; color: var(--good); }
    .match-tag.mismatch { background: #fee2e2; color: var(--bad); }
    textarea { width: 100%; min-height: 56px; resize: vertical; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font: inherit; background: #fffdf6; }

    .rubric-overview { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
    .rubric-overview .item { font-size: 12px; padding: 8px 10px; background: #fdf8ec; border: 1px solid var(--line); border-radius: 8px; }
    .rubric-overview .item .p { color: var(--accent); font-weight: 600; }
    .rubric-overview .item .s { color: var(--ink); }

    .info-line { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .empty { color: var(--muted); font-style: italic; }
    .err-box { font-size: 12px; background: #fef2f2; border: 1px solid #fecaca; color: var(--bad); padding: 6px 10px; border-radius: 8px; }
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel">
      <h1>人工 Rubric 评估</h1>
      <div class="info-line">数据来源：<code>__DATASET__</code>。请按任务逐一为每个模型的输出打分，结果会自动保存到浏览器并可导出 JSON。</div>
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
      <h2>按模型汇总（来自自动判分流水线）</h2>
      <div class="info-line">类目分数 = 该集合内 0/1/2 单项分的平均（0–2）。<strong>分母含全部样本</strong>（沙箱 / Judge 失败的样本计 0），括号 (judged/expected) 显示实际打过分的单项数 / 期望单项数。</div>
      <div id="summaryWrap" style="overflow:auto"></div>
    </section>

    <section class="panel">
      <h2>二级类目总分（按二级类目 × 模型）</h2>
      <div class="info-line">每格为该 (模型, 二级类目) 单项分平均（0–2，分母含全部样本，沙箱 / Judge 失败计 0）。</div>
      <div id="secondaryWrap" style="overflow:auto"></div>
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

    const STORAGE_KEY = 'human_eval_scores_v1::__DATASET__';
    const byId = (id) => document.getElementById(id);
    const fmtPct = (v) => `${(v * 100).toFixed(2)}%`;
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const normSecondary = (s) => String(s || '').replace(/\s*\([^)]*\)\s*$/, '').trim().toLowerCase();
    const llmKey = (primary, secondary) => `${primary}::${normSecondary(secondary)}`;

    let scores = loadScores();

    function loadScores() {
      try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') || {}; } catch { return {}; }
    }
    function saveScores() {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(scores)); } catch (e) { console.warn(e); }
    }
    function scoreKey(task, model) { return `${task}::${model}`; }
    function getEntry(task, model) {
      const k = scoreKey(task, model);
      if (!scores[k]) scores[k] = { task, model, items: {}, overall_notes: '', updated_at: null };
      return scores[k];
    }

    // ---------- Summary tables ----------
    function meanCell(item) {
      // Mean score on 0-2 scale: earned / expected_items, where expected counts ALL samples
      // (failed / unjudged samples count as 0 but are kept in the denominator).
      if (!item || !item.item_count) return '<span style="color:var(--muted)">-</span>';
      const avg = item.earned_points / item.item_count;
      const judged = item.judged_items != null ? item.judged_items : item.item_count;
      const tooltip = `earned ${item.earned_points} / max ${item.max_points}; judged ${judged} of ${item.item_count} expected items`;
      return `<span title="${tooltip}">${avg.toFixed(2)} <span style="color:var(--muted);font-size:11px">(${judged}/${item.item_count})</span></span>`;
    }

    function renderSummary() {
      const rows = [DATA.overall, ...DATA.per_model];
      const colsBase = [
        { label: 'Scope', key: 'scope', fmt: (v) => esc(v), align: 'left' },
        { label: 'Samples', key: 'samples', fmt: (v) => v },
        { label: 'Sandbox OK', key: 'sandbox_success_rate', fmt: fmtPct },
        { label: 'Geometry Valid', key: 'geometry_valid_rate', fmt: fmtPct },
        { label: 'Watertight', key: 'watertight_rate', fmt: fmtPct },
        { label: 'Component Match', key: 'component_match_rate', fmt: fmtPct },
        { label: 'Avg LLM Score', key: 'avg_llm_judge_score', fmt: (v) => v.toFixed(3) },
      ];
      const primaryKeys = (DATA.overall.primary_category_scores || []).map((r) => r.key);
      const html = ['<table><thead><tr>'];
      colsBase.forEach((c) => html.push(`<th${c.align === 'left' ? ' style="text-align:left"' : ''}>${c.label}</th>`));
      primaryKeys.forEach((k) => html.push(`<th>${esc(k)}<br><span style="font-weight:400;color:var(--muted);font-size:11px">avg 0–2</span></th>`));
      html.push('</tr></thead><tbody>');
      rows.forEach((row) => {
        html.push('<tr>');
        colsBase.forEach((c) => {
          const v = row[c.key];
          html.push(`<td class="num"${c.align === 'left' ? ' style="text-align:left"' : ''}>${c.fmt(v == null ? 0 : v)}</td>`);
        });
        primaryKeys.forEach((k) => {
          const item = (row.primary_category_scores || []).find((r) => r.key === k);
          html.push(`<td class="num">${meanCell(item)}</td>`);
        });
        html.push('</tr>');
      });
      html.push('</tbody></table>');
      byId('summaryWrap').innerHTML = html.join('');
    }

    function renderSecondaryTable() {
      // Rows = unique secondary categories from overall; columns = Overall + each model
      const secondaryKeys = (DATA.overall.secondary_category_scores || []).map((r) => r.key);
      if (!secondaryKeys.length) {
        byId('secondaryWrap').innerHTML = '<div class="empty">没有可用的二级类目数据。</div>';
        return;
      }
      const scopes = [DATA.overall, ...DATA.per_model];
      const html = ['<table><thead><tr>'];
      html.push('<th style="text-align:left">二级类目</th>');
      scopes.forEach((s) => html.push(`<th>${esc(s.scope)}</th>`));
      html.push('</tr></thead><tbody>');
      secondaryKeys.forEach((key) => {
        const overallItem = (DATA.overall.secondary_category_scores || []).find((r) => r.key === key);
        const primary = overallItem ? overallItem.primary_category : '';
        const secondary = overallItem ? overallItem.secondary_category : key;
        html.push(`<tr><td style="text-align:left"><span class="tag" style="color:var(--accent)">${esc(primary)}</span> / <strong>${esc(secondary)}</strong></td>`);
        scopes.forEach((s) => {
          const item = (s.secondary_category_scores || []).find((r) => r.key === key);
          html.push(`<td class="num">${meanCell(item)}</td>`);
        });
        html.push('</tr>');
      });
      html.push('</tbody></table>');
      byId('secondaryWrap').innerHTML = html.join('');
    }

    // ---------- Task selector ----------
    function renderTaskOptions() {
      const sel = byId('taskSelect');
      sel.innerHTML = DATA.tasks.map((t, i) => `<option value="${i}">${esc(t.task_name)}</option>`).join('');
      sel.addEventListener('change', () => renderTask(parseInt(sel.value, 10)));
    }

    // ---------- Task panel ----------
    function renderTask(idx) {
      const task = DATA.tasks[idx];
      const refImg = task.reference_url
        ? `<img class="ref-img" src="${esc(task.reference_url)}" alt="reference" />`
        : '<div class="empty">未找到 reference 图片</div>';

      const rubricOverview = task.rubric_struct.flatMap((p) =>
        p.subs.map((s) => `<div class="item"><div class="p">${esc(p.primary)}</div><div class="s">${esc(s.secondary)}</div><div class="tag">满分 2 分</div></div>`)
      ).join('');

      byId('taskPanel').innerHTML = `
        <h2>${esc(task.task_name)} <span class="tag" style="font-size:13px">gt_component_count=${task.gt_component_count ?? '?'}</span></h2>
        <div class="task-grid">
          <div>
            <h3>任务规范 (task.md)</h3>
            <div class="markdown">${esc(task.task_markdown || '(空)')}</div>
          </div>
          <div>
            <h3>Reference 渲染图</h3>
            ${refImg}
            <h3 style="margin-top:14px">Rubric 概览</h3>
            <div class="rubric-overview">${rubricOverview || '<div class="empty">未解析到 rubric 项</div>'}</div>
          </div>
          <div class="full">
            <details>
              <summary>展开完整 Rubric (rubric.md)</summary>
              <div class="markdown" style="margin-top:8px">${esc(task.rubric_markdown || '')}</div>
            </details>
          </div>
        </div>
      `;
      renderModels(task);
      updateProgress();
    }

    // ---------- Model evaluation cards ----------
    function flattenRubric(task) {
      const flat = [];
      task.rubric_struct.forEach((p, pi) => {
        p.subs.forEach((s, si) => {
          flat.push({ id: `${pi}.${si}`, primary: p.primary, secondary: s.secondary, levels: s.levels, core_focus: s.core_focus, common_errors: s.common_errors });
        });
      });
      return flat;
    }

    function renderModels(task) {
      const rubricItems = flattenRubric(task);
      const html = [];
      task.models.forEach((m) => {
        const entry = getEntry(task.task_name, m.model);
        const allScored = rubricItems.every((it) => {
          const c = entry.items[it.id];
          return c && c.svg_score != null && c.stp_score != null;
        });
        const cardClass = allScored ? 'model-card complete' : 'model-card';

        const flagBadges = [];
        flagBadges.push(`<span class="badge ${m.sandbox_ok ? 'ok' : 'bad'}">Sandbox ${m.sandbox_ok ? 'OK' : 'FAIL'}</span>`);
        if (m.geometry_valid != null) flagBadges.push(`<span class="badge ${m.geometry_valid ? 'ok' : 'warn'}">Geom ${m.geometry_valid ? 'OK' : 'FAIL'}</span>`);
        if (m.watertight != null) flagBadges.push(`<span class="badge ${m.watertight ? 'ok' : 'warn'}">Watertight ${m.watertight ? 'OK' : 'FAIL'}</span>`);
        if (m.component_count_match != null) flagBadges.push(`<span class="badge ${m.component_count_match ? 'ok' : 'warn'}">Comp ${m.component_count_match ? '=' : '≠'}</span>`);

        const linkHtml = (label, url) => url
          ? `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`
          : `<a class="disabled">${label}</a>`;

        const cardKey = `${task.task_name}::${m.model}`;
        const hasPng = !!m.render_png_url;
        const hasSvg = !!m.svg_url;
        const has3d = !!m.stl_url;
        const defaultView = hasPng ? 'png' : (hasSvg ? 'svg' : (has3d ? '3d' : null));
        const renderHtml = (hasPng || hasSvg || has3d)
          ? `
            <div class="view-toggle" data-card="${esc(cardKey)}">
              ${hasPng ? `<button data-view="png"${defaultView==='png'?' class="active"':''}>渲染图</button>` : ''}
              ${hasSvg ? `<button data-view="svg"${defaultView==='svg'?' class="active"':''}>技术图 SVG</button>` : ''}
              ${has3d ? `<button data-view="3d"${defaultView==='3d'?' class="active"':''}>3D 模型 (可拖动)</button>` : ''}
            </div>
            ${hasPng ? `<img class="render view-png" data-card="${esc(cardKey)}" src="${esc(m.render_png_url)}" alt="render"${defaultView==='png'?'':' style="display:none"'} />` : ''}
            ${hasSvg ? `<img class="render view-svg" data-card="${esc(cardKey)}" src="${esc(m.svg_url)}" alt="svg"${defaultView==='svg'?'':' style="display:none"'} />` : ''}
            ${has3d ? `<div class="stl-viewer view-3d" data-card="${esc(cardKey)}" data-stl="${esc(m.stl_url)}"${defaultView==='3d'?'':' style="display:none"'}><div class="loading">点击切换至 3D 模型时加载…</div></div>` : ''}
          `
          : '<div class="empty" style="padding:24px;text-align:center">无生成渲染图（沙箱失败）</div>';

        const errBox = (m.sandbox_ok === false && m.sandbox_error)
          ? `<details><summary>沙箱报错 (sandbox_error)</summary><pre>${esc(m.sandbox_error)}</pre></details>`
          : '';

        const svgOverall = m.llm_overall_summary || m.llm_judge_summary;
        const svgNormalized = m.llm_overall_normalized != null ? m.llm_overall_normalized : m.llm_judge_score;
        const v3dOverall = m.llm_3d_overall_summary || '';
        const v3dNormalized = m.llm_3d_overall_normalized != null ? m.llm_3d_overall_normalized : m.llm_judge_3d_score;
        const judgeBox = (svgOverall || v3dOverall)
          ? `
            <details>
              <summary>LLM Judge 总体反馈 — SVG=${svgNormalized == null ? '-' : Number(svgNormalized).toFixed(2)} / 3D=${v3dNormalized == null ? '-' : Number(v3dNormalized).toFixed(2)}</summary>
              ${svgOverall ? `<h4 style="margin-top:8px">基于 SVG (4 视图技术图)</h4><div class="markdown compact">${esc(svgOverall)}</div>` : ''}
              ${v3dOverall ? `<h4 style="margin-top:8px">基于 3D 渲染图</h4><div class="markdown compact">${esc(v3dOverall)}</div>` : ''}
            </details>`
          : '';

        const itemsHtml = rubricItems.map((it) => {
          // Migrate legacy single-score entries into svg_score on first read.
          const raw = entry.items[it.id] || {};
          if (raw.score != null && raw.svg_score == null && raw.stp_score == null) {
            raw.svg_score = raw.score;
            delete raw.score;
            entry.items[it.id] = raw;
          }
          const cur = { svg_score: raw.svg_score ?? null, stp_score: raw.stp_score ?? null, note: raw.note || '' };
          const levelText = it.levels.map((lv) => `<div><strong>${lv.score} 分：</strong>${esc(lv.description)}</div>`).join('');
          const renderRow = (kind, label, currentScore) => {
            const buttons = [2, 1, 0].map((sc) => {
              const sel = currentScore === sc ? ' sel' : '';
              return `<button class="score-btn${sel}" data-task="${esc(task.task_name)}" data-model="${esc(m.model)}" data-item="${it.id}" data-kind="${kind}" data-score="${sc}">${sc} 分</button>`;
            }).join('');
            return `
              <div class="score-row">
                <span class="kind-label">${label}</span>
                ${buttons}
                <span class="cur-tag">当前 ${currentScore == null ? '未评' : currentScore}</span>
              </div>`;
          };
          const llm = (m.llm_items_by_key || {})[llmKey(it.primary, it.secondary)];
          const llmCls = llm && llm.score != null ? `llm-badge llm-${llm.score}` : 'llm-badge llm-na';
          const llmText = llm && llm.score != null ? `LLM: <strong>${llm.score}</strong> 分` : 'LLM: 未判';
          const llmRationale = llm && llm.rationale
            ? `<details class="llm-rationale"><summary>LLM 评分理由 (基于 SVG)</summary><div class="level-text">${esc(llm.rationale)}</div></details>`
            : '';
          const matchTag = (humanScore, kind) => {
            if (humanScore == null || !llm || llm.score == null) return '';
            return humanScore === llm.score
              ? `<span class="match-tag match">${kind} 与 LLM 一致</span>`
              : `<span class="match-tag mismatch">${kind} 与 LLM 差 ${Math.abs(humanScore - llm.score)}</span>`;
          };
          const matchHint = `${matchTag(cur.svg_score, 'SVG')} ${matchTag(cur.stp_score, 'STP')}`.trim();
          return `
            <div class="rubric-item">
              <div class="rh">
                <div><span class="tag">${esc(it.primary)} /</span> <strong>${esc(it.secondary)}</strong></div>
                <div class="tag">满分 2 ${matchHint}</div>
              </div>
              ${it.core_focus ? `<div class="level-text"><em>Core Focus：</em>${esc(it.core_focus)}</div>` : ''}
              ${it.common_errors ? `<div class="level-text"><em>Common Errors：</em>${esc(it.common_errors)}</div>` : ''}
              <div class="llm-row">
                <span class="${llmCls}" title="LLM 判分（基于 SVG，仅参考）">${llmText}</span>
                ${llmRationale}
              </div>
              ${renderRow('svg', '基于 SVG 打分', cur.svg_score)}
              ${renderRow('stp', '基于 STP / 3D 打分', cur.stp_score)}
              <details style="margin-top:8px">
                <summary>查看 0/1/2 评分细则</summary>
                <div class="level-text">${levelText}</div>
              </details>
              <textarea data-task="${esc(task.task_name)}" data-model="${esc(m.model)}" data-item="${it.id}" data-field="note" placeholder="备注 / 证据描述...">${esc(cur.note || '')}</textarea>
            </div>
          `;
        }).join('');

        html.push(`
          <div class="${cardClass}" data-model="${esc(m.model)}">
            <header>
              <div>
                <div><strong>${esc(m.model)}</strong> <span class="tag">sample_${m.sample_index}</span></div>
                <div style="margin-top:4px">${flagBadges.join(' ')}</div>
              </div>
              <div class="links">
                ${linkHtml('STEP', m.step_url)}
                ${linkHtml('STL', m.stl_url)}
                ${linkHtml('SVG', m.svg_url)}
                ${linkHtml('Render PNG', m.render_png_url)}
                ${linkHtml('code.py', m.code_url)}
              </div>
            </header>
            ${renderHtml}
            ${errBox}
            ${judgeBox}
            <div>${itemsHtml}</div>
            <div>
              <h4>整体备注</h4>
              <textarea data-task="${esc(task.task_name)}" data-model="${esc(m.model)}" data-field="overall_notes" placeholder="整体观察 / 致命问题 / 备注...">${esc(entry.overall_notes || '')}</textarea>
            </div>
          </div>
        `);
      });

      byId('modelsPanel').innerHTML = `<h2>各模型逐项评分（${task.models.length} 个模型）</h2><div class="model-grid">${html.join('')}</div>`;
      bindCardEvents();
    }

    // ---------- Event wiring ----------
    function bindCardEvents() {
      document.querySelectorAll('#modelsPanel .score-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          const task = btn.dataset.task;
          const model = btn.dataset.model;
          const item = btn.dataset.item;
          const kind = btn.dataset.kind || 'svg';
          const score = parseInt(btn.dataset.score, 10);
          const entry = getEntry(task, model);
          const cell = entry.items[item] || { svg_score: null, stp_score: null, note: '' };
          // Backwards-compat migration of legacy { score }
          if (cell.score != null && cell.svg_score == null && cell.stp_score == null) {
            cell.svg_score = cell.score;
            delete cell.score;
          }
          const field = kind === 'stp' ? 'stp_score' : 'svg_score';
          cell[field] = cell[field] === score ? null : score;
          entry.items[item] = cell;
          entry.updated_at = new Date().toISOString();
          saveScores();
          renderTask(parseInt(byId('taskSelect').value, 10));
        });
      });
      document.querySelectorAll('#modelsPanel textarea[data-field]').forEach((ta) => {
        ta.addEventListener('input', () => {
          const task = ta.dataset.task;
          const model = ta.dataset.model;
          const entry = getEntry(task, model);
          if (ta.dataset.field === 'overall_notes') {
            entry.overall_notes = ta.value;
          } else if (ta.dataset.field === 'note') {
            const id = ta.dataset.item;
            const cell = entry.items[id] || { svg_score: null, stp_score: null, note: '' };
            cell.note = ta.value;
            entry.items[id] = cell;
          }
          entry.updated_at = new Date().toISOString();
          saveScores();
          updateProgress();
        });
      });
      document.querySelectorAll('#modelsPanel .view-toggle button').forEach((btn) => {
        btn.addEventListener('click', () => {
          const cardKey = btn.parentElement.dataset.card;
          const view = btn.dataset.view;
          btn.parentElement.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
          const png = document.querySelector(`.view-png[data-card="${CSS.escape(cardKey)}"]`);
          const svg = document.querySelector(`.view-svg[data-card="${CSS.escape(cardKey)}"]`);
          const v3d = document.querySelector(`.view-3d[data-card="${CSS.escape(cardKey)}"]`);
          if (png) png.style.display = view === 'png' ? '' : 'none';
          if (svg) svg.style.display = view === 'svg' ? '' : 'none';
          if (v3d) {
            v3d.style.display = view === '3d' ? '' : 'none';
            if (view === '3d' && !v3d.dataset.loaded) {
              loadStlViewer(v3d);
            }
          }
        });
      });
    }

    // ---------- STL Viewer (Three.js) ----------
    let threeReady = !!window.__three__;
    document.addEventListener('three-ready', () => { threeReady = true; flushPendingViewers(); });
    const pendingViewers = [];
    function flushPendingViewers() { while (pendingViewers.length) loadStlViewer(pendingViewers.shift()); }

    function loadStlViewer(container) {
      if (!threeReady) { pendingViewers.push(container); return; }
      const url = container.dataset.stl;
      if (!url) return;
      container.dataset.loaded = '1';
      container.innerHTML = '<div class="loading">加载中…</div>';

      const { THREE, STLLoader, OrbitControls } = window.__three__;
      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x1f2937);

      const w = container.clientWidth || 360;
      const h = container.clientHeight || 320;
      const camera = new THREE.PerspectiveCamera(45, w / h, 0.01, 10000);

      const renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setSize(w, h);

      const ambient = new THREE.AmbientLight(0xffffff, 0.55);
      scene.add(ambient);
      const dir1 = new THREE.DirectionalLight(0xffffff, 0.6);
      dir1.position.set(1, 1, 1);
      scene.add(dir1);
      const dir2 = new THREE.DirectionalLight(0xffffff, 0.4);
      dir2.position.set(-1, -0.5, -1);
      scene.add(dir2);

      new STLLoader().load(url, (geometry) => {
        geometry.computeVertexNormals();
        geometry.center();
        const material = new THREE.MeshPhongMaterial({ color: 0xfacc15, specular: 0x222222, shininess: 30, flatShading: false });
        const mesh = new THREE.Mesh(geometry, material);
        scene.add(mesh);

        // Fit camera to bounding box
        geometry.computeBoundingBox();
        const bb = geometry.boundingBox;
        const size = new THREE.Vector3(); bb.getSize(size);
        const maxDim = Math.max(size.x, size.y, size.z) || 1;
        camera.position.set(maxDim * 1.6, maxDim * 1.2, maxDim * 1.8);
        camera.lookAt(0, 0, 0);
        camera.near = maxDim / 100;
        camera.far = maxDim * 100;
        camera.updateProjectionMatrix();

        container.innerHTML = '';
        container.appendChild(renderer.domElement);
        const hint = document.createElement('div');
        hint.className = 'hint';
        hint.textContent = '左键旋转 · 右键平移 · 滚轮缩放';
        container.appendChild(hint);

        const controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
        controls.dampingFactor = 0.08;

        let alive = true;
        function animate() { if (!alive) return; controls.update(); renderer.render(scene, camera); requestAnimationFrame(animate); }
        animate();

        const ro = new ResizeObserver(() => {
          const nw = container.clientWidth, nh = container.clientHeight;
          if (!nw || !nh) return;
          camera.aspect = nw / nh;
          camera.updateProjectionMatrix();
          renderer.setSize(nw, nh);
        });
        ro.observe(container);

        // teardown when container is removed (task switch re-renders modelsPanel)
        const mo = new MutationObserver(() => {
          if (!document.body.contains(container)) {
            alive = false;
            renderer.dispose();
            geometry.dispose();
            material.dispose();
            ro.disconnect();
            mo.disconnect();
          }
        });
        mo.observe(document.body, { childList: true, subtree: true });
      }, undefined, (err) => {
        container.innerHTML = `<div class="err">加载 STL 失败：${(err && err.message) || err || '未知错误'}<br><a href="${url}" target="_blank" style="color:#fca5a5">直接下载</a></div>`;
      });
    }

    function updateProgress() {
      let svgScored = 0, stpScored = 0, total = 0;
      DATA.tasks.forEach((task) => {
        const flat = flattenRubric(task);
        task.models.forEach((m) => {
          flat.forEach((it) => {
            total += 1;
            const e = scores[scoreKey(task.task_name, m.model)];
            const cell = e && e.items && e.items[it.id];
            if (cell && cell.svg_score != null) svgScored += 1;
            if (cell && cell.stp_score != null) stpScored += 1;
          });
        });
      });
      byId('progressPill').textContent = `SVG ${svgScored}/${total} · STP ${stpScored}/${total}`;
    }

    // ---------- Export / Import ----------
    function exportJSON() {
      const out = {
        meta: {
          dataset: '__DATASET__',
          exported_at: new Date().toISOString(),
        },
        tasks: DATA.tasks.map((t) => ({
          task_name: t.task_name,
          rubric_items: flattenRubric(t).map((it) => ({ id: it.id, primary: it.primary, secondary: it.secondary })),
          models: t.models.map((m) => {
            const entry = scores[scoreKey(t.task_name, m.model)] || { items: {}, overall_notes: '' };
            const items = flattenRubric(t).map((it) => {
              const c = entry.items[it.id] || {};
              // Migrate legacy `score` if encountered.
              const svg = c.svg_score != null ? c.svg_score : (c.score != null ? c.score : null);
              const stp = c.stp_score != null ? c.stp_score : null;
              return {
                id: it.id,
                primary: it.primary,
                secondary: it.secondary,
                svg_score: svg,
                stp_score: stp,
                note: c.note || '',
              };
            });
            const sumStat = (key) => {
              const valid = items.filter((x) => x[key] != null);
              const earned = valid.reduce((a, b) => a + b[key], 0);
              const max = valid.length * 2;
              return { earned_points: earned, max_points: max, ratio: max > 0 ? earned / max : null, judged_items: valid.length };
            };
            return {
              model: m.model,
              sample_index: m.sample_index,
              run_id: m.run_id,
              items,
              overall_notes: entry.overall_notes || '',
              svg: sumStat('svg_score'),
              stp: sumStat('stp_score'),
              updated_at: entry.updated_at || null,
            };
          }),
        })),
      };
      const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `human_eval_scores_${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }

    function importJSON(file) {
      const reader = new FileReader();
      reader.onload = (e) => {
        try {
          const parsed = JSON.parse(e.target.result);
          // Accept both raw scores object or full export
          if (parsed && parsed.tasks && Array.isArray(parsed.tasks)) {
            parsed.tasks.forEach((t) => {
              t.models.forEach((m) => {
                const entry = getEntry(t.task_name, m.model);
                entry.items = {};
                (m.items || []).forEach((it) => {
                  // Accept legacy { score } as svg_score; new exports carry svg_score/stp_score directly.
                  const svg = it.svg_score != null ? it.svg_score : (it.score != null ? it.score : null);
                  const stp = it.stp_score != null ? it.stp_score : null;
                  entry.items[it.id] = { svg_score: svg, stp_score: stp, note: it.note || '' };
                });
                entry.overall_notes = m.overall_notes || '';
                entry.updated_at = m.updated_at || new Date().toISOString();
              });
            });
          } else if (parsed && typeof parsed === 'object') {
            scores = parsed;
          }
          saveScores();
          renderTask(parseInt(byId('taskSelect').value, 10));
          alert('导入成功');
        } catch (err) {
          alert('JSON 解析失败：' + err.message);
        }
      };
      reader.readAsText(file);
    }

    function clearCurrent() {
      const idx = parseInt(byId('taskSelect').value, 10);
      const task = DATA.tasks[idx];
      if (!confirm(`确定要清空任务「${task.task_name}」的所有评分吗？`)) return;
      task.models.forEach((m) => { delete scores[scoreKey(task.task_name, m.model)]; });
      saveScores();
      renderTask(idx);
    }

    // ---------- Boot ----------
    renderSummary();
    renderSecondaryTable();
    renderTaskOptions();
    renderTask(0);
    byId('exportBtn').addEventListener('click', exportJSON);
    byId('importBtn').addEventListener('click', () => byId('importFile').click());
    byId('importFile').addEventListener('change', (e) => { if (e.target.files[0]) importJSON(e.target.files[0]); });
    byId('clearBtn').addEventListener('click', clearCurrent);
  </script>
</body>
</html>
"""


def main():
    data = build_data()
    payload = json.dumps(data, ensure_ascii=False)
    html = HTML.replace("__DATASET__", ROOT.name).replace("__DATA__", payload)
    OUT.write_text(html)
    n_models = sum(len(t["models"]) for t in data["tasks"])
    n_items = sum(len(t["models"]) * sum(len(p["subs"]) for p in t["rubric_struct"]) for t in data["tasks"])
    print(f"Wrote {OUT} ({len(html):,} bytes)")
    print(f"  tasks: {len(data['tasks'])}")
    print(f"  total model evaluations: {n_models}")
    print(f"  total rubric items to score: {n_items}")


if __name__ == "__main__":
    main()
