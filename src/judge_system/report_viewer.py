from __future__ import annotations

import html
import json
import os
from pathlib import Path
from urllib.parse import quote


METRIC_EXPLANATIONS = [
    {
        "name": "Geometry Valid",
        "description": "单个样本是否通过几何有效性检查。它来自 OCCT/CadQuery 几何检查结果，要求样本代码能执行并且几何体本身有效。",
    },
    {
        "name": "Geometry Valid Rate",
        "description": "一组样本里 `geometry_valid = true` 的比例。这里按你 benchmark 里的定义统计。",
    },
    {
        "name": "Watertight",
        "description": "几何体是否保持闭合实体，没有开放边。对执行失败的样本记为未评估，不再误记为通过。",
    },
    {
        "name": "Watertight Rate",
        "description": "一组样本里 `watertight = true` 的比例。",
    },
    {
        "name": "Component Match",
        "description": "生成结果的组件数是否和 GT 组件数一致。GT 组件数来自 `plan.md` 中的计划装配体数量。",
    },
    {
        "name": "Component Match Rate",
        "description": "一组样本里 `component_count_match = true` 的比例。",
    },
    {
        "name": "Result Solid Count",
        "description": "执行 CadQuery 后，从 `result` 中识别出的 solid 数量。",
    },
    {
        "name": "GT Component Count",
        "description": "从 `plan.md` 解析出的目标组件数，用来和生成结果做装配等价比较。",
    },
    {
        "name": "SVG Component Estimate",
        "description": "基于 SVG path 聚类做的组件数估计，用来辅助分析二维图和三维组件数是否一致。",
    },
    {
        "name": "BBox DX/DY/DZ",
        "description": "包围盒三个方向的边长，单位毫米。不是坐标值，而是尺寸跨度。",
    },
    {
        "name": "Rubric Score",
        "description": "当前 task 的 rubric 加权得分，范围约 0 到 1。它不是全库 rubric 总表，只对应当前样本所属任务。",
    },
    {
        "name": "Category Rubric Score",
        "description": "把 rubric 分数按一级类目和二级类目重新汇总后的得分。可以看到可实用、可组装、可建造，以及各个子类分别丢了哪些分。",
    },
    {
        "name": "Sandbox OK",
        "description": "CadQuery 代码是否能在沙箱里成功执行到拿到 `result`。失败时会显示运行时错误。",
    },
    {
        "name": "Normal Consistency / Volume Valid / BBox Valid / OCCT Valid",
        "description": "这些是几何子指标，用来帮助解释 Geometry Valid 为什么失败或通过。",
    },
]


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _merge_category_breakdowns(rows: list[dict], field_name: str) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in rows:
        payload = json.loads(row.get(field_name) or "{}")
        for key, item in payload.items():
            bucket = merged.setdefault(
                key,
                {
                    "key": key,
                    "primary_category": item.get("primary_category", ""),
                    "secondary_category": item.get("secondary_category", ""),
                    "item_count": 0,
                    "max_points": 0.0,
                    "earned_points": 0.0,
                    "weighted_score": 0.0,
                },
            )
            bucket["item_count"] += int(item.get("item_count", 0))
            bucket["max_points"] += float(item.get("max_points", 0.0))
            bucket["earned_points"] += float(item.get("earned_points", 0.0))
            bucket["weighted_score"] += float(item.get("weighted_score", 0.0))
    result = []
    for bucket in merged.values():
        max_points = float(bucket["max_points"])
        bucket["ratio"] = 0.0 if max_points == 0 else float(bucket["earned_points"]) / max_points
        result.append(bucket)
    result.sort(key=lambda item: item["key"])
    return result


def _summarize_scope(scope: str, rows: list[dict]) -> dict:
    total_count = len(rows)
    sandbox_rows = [row for row in rows if row["sandbox_ok"]]
    sandbox_count = len(sandbox_rows)
    llm_scores = [0.0] * len(rows)
    for idx, row in enumerate(rows):
        if row.get("sandbox_ok"):
            if not row.get("llm_judge_error"):
                llm_scores[idx] = float(row.get("llm_judge_score", 0.0) or 0.0)
    primary_rubric_keys = _collect_rubric_keys(rows, level="primary")
    secondary_rubric_keys = _collect_rubric_keys(rows, level="secondary")
    return {
        "scope": scope,
        "samples": len(rows),
        "sandbox_samples": sandbox_count,
        "llm_judge_samples": len(rows),
        "geometry_valid_rate": _rate(sum(1 for row in rows if row["geometry_valid"]), total_count),
        "watertight_rate": _rate(sum(1 for row in rows if row["watertight"] is True), total_count),
        "component_match_rate": _rate(sum(1 for row in rows if row["component_count_match"]), total_count),
        "sandbox_success_rate": _rate(sum(1 for row in rows if row["sandbox_ok"]), len(rows)),
        "avg_llm_judge_score": _rate(sum(llm_scores), len(rows)) if rows else 0.0,
        "primary_category_scores": _llm_category_scores(rows, "primary", primary_rubric_keys),
        "secondary_category_scores": _llm_category_scores(rows, "secondary", secondary_rubric_keys),
    }


def _collect_rubric_keys(rows: list[dict], level: str | None = "primary") -> set[str]:
    rubric_keys: set[str] = set()
    for row in rows:
        payload = json.loads(row.get("llm_judge_breakdown_json") or "{}")
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not items:
            continue
        for item in items:
            primary = item.get("primary_category_en", "")
            secondary = item.get("secondary_category_en", "")
            if level == "secondary":
                key = f"{primary} / {secondary}" if primary or secondary else ""
            else:
                key = primary
            if key:
                rubric_keys.add(key)
    return rubric_keys


def _build_summary(rows: list[dict]) -> tuple[dict, list[dict]]:
    overall = _summarize_scope("Overall", rows)

    per_model = []
    for model_label in sorted({row["model_label"] for row in rows}):
        subset = [row for row in rows if row["model_label"] == model_label]
        per_model.append(_summarize_scope(model_label, subset))
    return overall, per_model


def _to_viewer_url(output_path: Path, target_path: str) -> str:
    if not target_path:
        return ""
    target = Path(target_path)
    try:
        relative = os.path.relpath(target, start=output_path.parent)
    except ValueError:
        return f"file://{target}"
    return quote(relative)


def _breakdown_values(raw_json: str, empty_kind: str = "dict") -> list[dict]:
    fallback = "{}" if empty_kind == "dict" else "[]"
    payload = json.loads(raw_json or fallback)
    if isinstance(payload, dict):
        return list(payload.values())
    if isinstance(payload, list):
        return payload
    return []


def _llm_category_scores(rows: list[dict], level: str, rubric_keys: set[str]) -> list[dict]:
    merged: dict[str, dict] = {}
    total_rows = len(rows)
    max_score_per_item = 2.0
    for row in rows:
        payload = json.loads(row.get("llm_judge_breakdown_json") or "{}")
        items = payload.get("items", []) if isinstance(payload, dict) else []
        present_keys = set()
        for item in items:
            primary = item.get("primary_category_en", "")
            secondary = item.get("secondary_category_en", "")
            if level == "primary":
                key = primary
            else:
                key = f"{primary} / {secondary}"
            if not key:
                continue
            present_keys.add(key)
            bucket = merged.setdefault(
                key,
                {
                    "key": key,
                    "primary_category": primary,
                    "secondary_category": secondary,
                    "item_count": 0,
                    "earned_points": 0.0,
                    "max_points": 0.0,
                    "weighted_score": 0.0,
                },
            )
            score = float(item.get("score", 0) or 0)
            bucket["item_count"] += 1
            bucket["earned_points"] += score
            bucket["max_points"] += max_score_per_item
            present_keys.add(key)
        for key in rubric_keys:
            if level == "primary":
                primary = key
                secondary = ""
            else:
                primary, secondary = key.split(" / ", 1) if " / " in key else (key, "")
            bucket = merged.setdefault(
                key,
                {
                    "key": key,
                    "primary_category": primary,
                    "secondary_category": secondary,
                    "item_count": 0,
                    "earned_points": 0.0,
                    "max_points": 0.0,
                    "weighted_score": 0.0,
                },
            )
            if key not in present_keys:
                bucket["item_count"] += 1
                bucket["max_points"] += max_score_per_item
    result = []
    for bucket in merged.values():
        max_points = float(bucket["max_points"])
        ratio = 0.0 if max_points == 0 else float(bucket["earned_points"]) / max_points
        bucket["ratio"] = ratio
        bucket["weighted_score"] = ratio
        bucket["judged_rows"] = total_rows
        result.append(bucket)
    result.sort(key=lambda item: item["key"])
    return result


def build_viewer(records_path: Path, rubric_catalog_path: Path, output_path: Path) -> Path:
    rows = json.loads(records_path.read_text(encoding="utf-8"))
    rubric_catalog = json.loads(rubric_catalog_path.read_text(encoding="utf-8"))
    overall, per_model = _build_summary(rows)

    for row in rows:
        row["svg_url"] = _to_viewer_url(output_path, row.get("svg_path", ""))
        row["png_url"] = _to_viewer_url(output_path, row.get("png_path", ""))
        row["render_png_url"] = _to_viewer_url(output_path, row.get("render_png_path", ""))
        row["render_mesh_url"] = _to_viewer_url(output_path, row.get("render_mesh_path", ""))
        row["render_step_url"] = _to_viewer_url(output_path, row.get("render_step_path", ""))
        row["code_url"] = _to_viewer_url(output_path, row.get("code_path", ""))
        row["rubric_breakdown"] = json.loads(row.get("rubric_breakdown_json") or "[]")
        row["rubric_primary_breakdown"] = _breakdown_values(row.get("rubric_primary_breakdown_json", ""), "dict")
        row["rubric_category_breakdown"] = _breakdown_values(row.get("rubric_category_breakdown_json", ""), "dict")
        row["llm_judge_breakdown"] = json.loads(row.get("llm_judge_breakdown_json") or "{}")

    payload = {
        "overall": overall,
        "per_model": per_model,
        "rows": rows,
        "rubric_catalog": rubric_catalog,
        "metric_explanations": METRIC_EXPLANATIONS,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Judge System Viewer</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: #fffaf0;
      --ink: #1f2937;
      --muted: #6b7280;
      --line: #d8cdb9;
      --accent: #0f766e;
      --accent-2: #c2410c;
      --good: #166534;
      --bad: #991b1b;
      --warn: #92400e;
      --shadow: 0 18px 40px rgba(74, 52, 24, 0.12);
      --radius: 18px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "PingFang SC", "Helvetica Neue", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.14), transparent 28%),
        radial-gradient(circle at top right, rgba(194,65,12,0.12), transparent 24%),
        linear-gradient(180deg, #f7f2e9 0%, #efe7da 100%);
    }}
    .shell {{
      width: min(1480px, calc(100vw - 32px));
      margin: 20px auto 40px;
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 18px;
    }}
    .panel {{
      background: rgba(255, 250, 240, 0.92);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }}
    .sidebar {{
      padding: 18px;
      position: sticky;
      top: 16px;
      align-self: start;
      max-height: calc(100vh - 32px);
      overflow: auto;
    }}
    .main {{
      padding: 18px;
      display: grid;
      gap: 18px;
    }}
    h1, h2, h3, h4, p {{ margin: 0; }}
    h1 {{
      font-size: 28px;
      letter-spacing: -0.03em;
      margin-bottom: 8px;
    }}
    .lede {{
      color: var(--muted);
      line-height: 1.5;
      margin-bottom: 18px;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .stat-card {{
      padding: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.88), rgba(255,246,233,0.92));
      border: 1px solid var(--line);
      border-radius: 16px;
    }}
    .stat-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .stat-value {{
      font-size: 28px;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .bar {{
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: rgba(31,41,55,0.08);
      overflow: hidden;
    }}
    .bar > span {{
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      border-radius: inherit;
    }}
    .filter-row {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }}
    .filter-box {{
      display: grid;
      gap: 6px;
    }}
    label {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    select {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
    }}
    .explain-list {{
      display: grid;
      gap: 12px;
      margin-top: 16px;
    }}
    .explain-item {{
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,0.72);
    }}
    .explain-item h4 {{
      font-size: 14px;
      margin-bottom: 6px;
    }}
    .explain-item p {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .summary-table, .rubric-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    .summary-table th, .summary-table td, .rubric-table th, .rubric-table td {{
      padding: 10px 8px;
      border-bottom: 1px solid rgba(216,205,185,0.7);
      text-align: left;
      vertical-align: top;
    }}
    .summary-table th {{
      position: sticky;
      top: 0;
      background: rgba(255,250,240,0.96);
      z-index: 1;
    }}
    .matrix-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255,255,255,0.72);
    }}
    .matrix-note {{
      color: var(--muted);
      font-size: 12px;
      margin: 8px 0 12px;
      line-height: 1.5;
    }}
    .cell-score {{
      font-weight: 700;
      font-size: 13px;
    }}
    .cell-meta {{
      color: var(--muted);
      font-size: 11px;
      margin-top: 4px;
      line-height: 1.35;
    }}
    .row-bad {{
      background: rgba(153,27,27,0.07);
    }}
    .status-bad {{
      color: var(--bad);
      font-weight: 700;
    }}
    .status-warn {{
      color: var(--warn);
      font-weight: 700;
    }}
    .status-good {{
      color: var(--good);
      font-weight: 700;
    }}
    .record-list {{
      display: grid;
      gap: 16px;
    }}
    .record-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
      background: rgba(255,255,255,0.84);
    }}
    .record-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      padding: 16px 18px;
      background: linear-gradient(135deg, rgba(15,118,110,0.1), rgba(194,65,12,0.08));
      border-bottom: 1px solid var(--line);
    }}
    .record-title {{
      font-size: 20px;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .record-meta {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .pill-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    .pill {{
      padding: 7px 10px;
      border-radius: 999px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: #fff;
    }}
    .pill.good {{ color: var(--good); }}
    .pill.bad {{ color: var(--bad); }}
    .record-body {{
      display: grid;
      grid-template-columns: minmax(520px, 60%) 1fr;
      gap: 16px;
      padding: 16px 18px 18px;
    }}
    .visual-stack {{
      display: grid;
      gap: 14px;
    }}
    .svg-panel {{
      border: 1px solid var(--line);
      border-radius: 16px;
      min-height: 320px;
      background: #fffdf8;
      overflow: hidden;
    }}
    .visual-label {{
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      background: rgba(255,255,255,0.7);
    }}
    .svg-frame {{
      width: 100%;
      height: 420px;
      border: 0;
      background: white;
    }}
    .render-image {{
      width: 100%;
      display: block;
      background: white;
      max-height: 420px;
      object-fit: contain;
    }}
    .empty {{
      display: grid;
      place-items: center;
      min-height: 320px;
      color: var(--muted);
      padding: 20px;
      text-align: center;
      line-height: 1.6;
    }}
    .detail-grid {{
      display: grid;
      gap: 14px;
    }}
    .detail-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255,255,255,0.72);
    }}
    .metric-kv {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px 16px;
      font-size: 14px;
    }}
    .metric-kv div:nth-child(odd) {{ color: var(--muted); }}
    .error-box {{
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      line-height: 1.5;
      color: var(--bad);
      background: rgba(153,27,27,0.06);
      border-radius: 12px;
      padding: 12px;
      overflow: auto;
      max-height: 220px;
    }}
    .small-note {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .link-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }}
    .link-row a {{
      color: var(--accent);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
    }}
    @media (max-width: 1180px) {{
      .shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; max-height: none; }}
      .record-body {{ grid-template-columns: 1fr; }}
      .filter-row, .stat-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 720px) {{
      .filter-row, .stat-grid {{ grid-template-columns: 1fr; }}
      .record-head {{ flex-direction: column; }}
      .pill-row {{ justify-content: flex-start; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="panel sidebar">
      <h1>Judge Viewer</h1>
      <p class="lede">这个页面把每个样本的 SVG、几何指标、组件指标、rubric 评分和失败原因放到同一处，方便你逐条比对。顶部汇总里的比率指标只在 Sandbox OK 成功的样本上统计；一级类目和二级类目表则专门按模型展开，方便横向比较。</p>
      <div class="explain-list" id="metricExplanations"></div>
    </aside>
    <main class="panel main">
      <section>
        <h2 style="margin-bottom: 12px;">总体概览</h2>
        <div class="stat-grid" id="overallStats"></div>
      </section>
      <section>
        <h2 style="margin-bottom: 12px;">按模型汇总</h2>
        <table class="summary-table" id="summaryTable"></table>
      </section>
      <section>
        <h2 style="margin-bottom: 12px;">一级类目总分</h2>
        <table class="summary-table" id="primaryCategoryTable"></table>
      </section>
      <section>
        <h2 style="margin-bottom: 12px;">二级类目总分</h2>
        <table class="summary-table" id="secondaryCategoryTable"></table>
      </section>
      <section>
        <h2 style="margin-bottom: 12px;">筛选样本</h2>
        <div class="filter-row">
          <div class="filter-box">
            <label for="taskFilter">Task</label>
            <select id="taskFilter"></select>
          </div>
          <div class="filter-box">
            <label for="modelFilter">Model</label>
            <select id="modelFilter"></select>
          </div>
          <div class="filter-box">
            <label for="statusFilter">Status</label>
            <select id="statusFilter"></select>
          </div>
          <div class="filter-box">
            <label for="svgFilter">SVG</label>
            <select id="svgFilter"></select>
          </div>
        </div>
        <p class="small-note" style="margin-top: 10px;">Status 里 `geometry fail` 常见于 loft / fillet 失败；`svg missing` 表示代码通过了但四视图没有产出。</p>
      </section>
      <section>
        <h2 style="margin-bottom: 12px;">逐样本查看</h2>
        <div class="record-list" id="recordList"></div>
      </section>
    </main>
  </div>
  <script>
    const DATA = {payload_json};

    const fmtPct = (value) => `${{(value * 100).toFixed(2)}}%`;
    const fmtScore = (value) => Number(value).toFixed(3);
    const byId = (id) => document.getElementById(id);

    function renderMetricExplanations() {{
      byId("metricExplanations").innerHTML = DATA.metric_explanations.map(item => `
        <div class="explain-item">
          <h4>${{item.name}}</h4>
          <p>${{item.description}}</p>
        </div>
      `).join("");
    }}

    function renderOverall() {{
      const stats = [
        ["Geometry Valid Rate", fmtPct(DATA.overall.geometry_valid_rate)],
        ["Watertight Rate", fmtPct(DATA.overall.watertight_rate)],
        ["Component Match Rate", fmtPct(DATA.overall.component_match_rate)],
        ["Sandbox Success Rate", fmtPct(DATA.overall.sandbox_success_rate)],
        ["LLM Judge Done", String(DATA.rows.filter(row => !row.llm_judge_error && row.llm_judge_score > 0).length)],
        ["Sandbox Sample Count", String(DATA.overall.sandbox_samples)],
      ];
      byId("overallStats").innerHTML = stats.map(([label, value], index) => {{
        const rateValue = index < 4 ? parseFloat(value) / 100 : 1;
        const width = index < 5 ? Math.max(0, Math.min(100, rateValue * 100)) : 100;
        return `
          <div class="stat-card">
            <div class="stat-label">${{label}}</div>
            <div class="stat-value">${{value}}</div>
            <div class="bar"><span style="width:${{width}}%"></span></div>
          </div>
        `;
      }}).join("");
    }}

    function renderSummaryTable() {{
      const rows = DATA.per_model;
      const statusLabel = (row) => {{
        if (row.sandbox_success_rate === 0) return '<span class="status-bad">All Failed</span>';
        if (row.sandbox_success_rate < 0.5) return '<span class="status-warn">Mostly Failed</span>';
        return '<span class="status-good">Mixed / Usable</span>';
      }};
      byId("summaryTable").innerHTML = `
        <thead>
          <tr>
            <th>Model</th>
            <th>Samples</th>
            <th>Sandbox Samples</th>
            <th>Geometry Valid Rate</th>
            <th>Watertight Rate</th>
            <th>Component Match Rate</th>
            <th>Sandbox Success Rate</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          ${{
            rows.map(row => `
              <tr class="${{row.sandbox_success_rate === 0 ? 'row-bad' : ''}}">
                <td>${{row.scope}}</td>
                <td>${{row.samples}}</td>
                <td>${{row.sandbox_samples}}</td>
                <td>${{fmtPct(row.geometry_valid_rate)}}</td>
                <td>${{fmtPct(row.watertight_rate)}}</td>
                <td>${{fmtPct(row.component_match_rate)}}</td>
                <td>${{fmtPct(row.sandbox_success_rate)}}</td>
                <td>${{statusLabel(row)}}</td>
              </tr>
            `).join("")
          }}
        </tbody>
      `;
    }}

    function renderCategoryTables() {{
      const models = DATA.per_model.map(row => row.scope);
      const buildMatrix = (rowsByModelKey, labelBuilder) => {{
        const allKeys = [];
        const keySet = new Set();
        DATA.per_model.forEach(model => {{
          (model[rowsByModelKey] || []).forEach(row => {{
            const key = labelBuilder(row);
            if (!keySet.has(key)) {{
              keySet.add(key);
              allKeys.push(key);
            }}
          }});
        }});
        const lookup = Object.fromEntries(
          DATA.per_model.map(model => [
            model.scope,
            Object.fromEntries((model[rowsByModelKey] || []).map(row => [labelBuilder(row), row]))
          ])
        );
        return {{ models, allKeys, lookup }};
      }};

      const renderMatrix = (matrix, titleKey) => `
        <div class="matrix-wrap">
          <table class="summary-table">
            <thead>
              <tr>
                <th>${{titleKey}}</th>
                ${{matrix.models.map(model => `<th>${{model}}</th>`).join("")}}
              </tr>
            </thead>
            <tbody>
              ${{
                matrix.allKeys.map(key => `
                  <tr>
                    <td>${{key}}</td>
                    ${{
                      matrix.models.map(model => {{
                        const row = matrix.lookup[model][key];
                        if (!row) return `<td><span class="small-note">n/a</span></td>`;
                        return `
                          <td>
                            <div class="cell-score">${{fmtScore(row.weighted_score)}}</div>
                            <div class="cell-meta">
                              ratio: ${{fmtPct(row.ratio)}}<br/>
                              pts: ${{Number(row.earned_points).toFixed(2)}} / ${{Number(row.max_points).toFixed(2)}}
                            </div>
                          </td>
                        `;
                      }}).join("")
                    }}
                  </tr>
                `).join("")
              }}
            </tbody>
          </table>
        </div>
      `;

      const primaryMatrix = buildMatrix("primary_category_scores", row => row.primary_category);
      const secondaryMatrix = buildMatrix("secondary_category_scores", row => `${{row.primary_category}} / ${{row.secondary_category}}`);

      byId("primaryCategoryTable").innerHTML = `
        <p class="matrix-note">这里不再显示单一的 Avg Rubric Score，而是直接展示一级类目在不同模型上的得分。每个单元格显示 weighted score、ratio 和 points。</p>
        ${{renderMatrix(primaryMatrix, "Primary Category")}}
      `;

      byId("secondaryCategoryTable").innerHTML = `
        <p class="matrix-note">二级类目也按模型横向展开，方便一眼看出是哪一类能力把某个模型拖垮了。全失败模型通常会整列接近空白或低分。</p>
        ${{renderMatrix(secondaryMatrix, "Secondary Category")}}
      `;
    }}

    function setupFilters() {{
      const rows = DATA.rows;
      const taskOptions = ["all", ...new Set(rows.map(row => row.task_name))];
      const modelOptions = ["all", ...new Set(rows.map(row => row.model_label))];
      const statusOptions = ["all", "pass", "geometry fail", "sandbox fail"];
      const svgOptions = ["all", "with svg", "without svg"];

      const setOptions = (id, options) => {{
        byId(id).innerHTML = options.map(option => `<option value="${{option}}">${{option}}</option>`).join("");
      }};

      setOptions("taskFilter", taskOptions);
      setOptions("modelFilter", modelOptions);
      setOptions("statusFilter", statusOptions);
      setOptions("svgFilter", svgOptions);

      ["taskFilter", "modelFilter", "statusFilter", "svgFilter"].forEach(id => {{
        byId(id).addEventListener("change", renderRecords);
      }});
    }}

    function getFilteredRows() {{
      return DATA.rows.filter(row => {{
        const task = byId("taskFilter").value;
        const model = byId("modelFilter").value;
        const status = byId("statusFilter").value;
        const svg = byId("svgFilter").value;

        if (task !== "all" && row.task_name !== task) return false;
        if (model !== "all" && row.model_label !== model) return false;
        if (status === "pass" && !row.geometry_valid) return false;
        if (status === "geometry fail" && row.geometry_valid) return false;
        if (status === "sandbox fail" && row.sandbox_ok) return false;
        if (svg === "with svg" && !row.svg_url) return false;
        if (svg === "without svg" && row.svg_url) return false;
        return true;
      }});
    }}

    function renderRecords() {{
      const rows = getFilteredRows();
      const container = byId("recordList");
      if (!rows.length) {{
        container.innerHTML = `<div class="detail-card"><p class="small-note">当前筛选条件下没有样本。</p></div>`;
        return;
      }}

      container.innerHTML = rows.map(row => {{
        const rubricRows = row.rubric_breakdown.map(item => `
          <tr>
            <td>#${{item.item_id}} ${{item.title}}<br/><span class="small-note">${{item.primary_category}} / ${{item.secondary_category}}</span></td>
            <td>${{fmtScore(item.weight)}}</td>
            <td>${{fmtScore(item.score)}}<br/><span class="small-note">${{Number(item.points).toFixed(2)}} / ${{Number(item.max_points).toFixed(2)}} pts</span></td>
            <td>${{item.rationale}}</td>
          </tr>
        `).join("");
        const primaryCategoryRows = row.rubric_primary_breakdown.map(item => `
          <tr>
            <td>${{item.primary_category}}</td>
            <td>${{item.item_count}}</td>
            <td>${{Number(item.earned_points).toFixed(2)}} / ${{Number(item.max_points).toFixed(2)}}</td>
            <td>${{fmtPct(item.ratio)}}</td>
          </tr>
        `).join("");
        const secondaryCategoryRows = row.rubric_category_breakdown.map(item => `
          <tr>
            <td>${{item.primary_category}} / ${{item.secondary_category}}</td>
            <td>${{item.item_count}}</td>
            <td>${{Number(item.earned_points).toFixed(2)}} / ${{Number(item.max_points).toFixed(2)}}</td>
            <td>${{fmtPct(item.ratio)}}</td>
          </tr>
        `).join("");
        const llmItems = (row.llm_judge_breakdown && Array.isArray(row.llm_judge_breakdown.items)) ? row.llm_judge_breakdown.items : [];
        const llmRows = llmItems.map(item => `
          <tr>
            <td>${{item.primary_category_en}} / ${{item.secondary_category_en}}</td>
            <td>${{item.score ?? ""}}</td>
            <td>${{item.rationale ?? ""}}</td>
          </tr>
        `).join("");
        const llmJudgeBlock = llmRows
          ? `
            <div class="small-note" style="margin-bottom:10px;">
              Overall Score: ${{fmtScore(row.llm_judge_score)}}<br/>
              Summary: ${{row.llm_judge_summary || "n/a"}}
            </div>
            <table class="rubric-table">
              <thead>
                <tr>
                  <th>Gemini Rubric Item</th>
                  <th>Score</th>
                  <th>Rationale</th>
                </tr>
              </thead>
              <tbody>${{llmRows}}</tbody>
            </table>
          `
          : `<div class="small-note">这个样本没有可用的 LLM judge 明细。可能是 SVG 失败被跳过，或 judge 请求本身失败。</div>`;

        const renderPanel = row.render_png_url
          ? `
            <div class="svg-panel">
              <div class="visual-label">3D Render</div>
              <img class="render-image" src="${{row.render_png_url}}" alt="3D rendered preview" />
            </div>
          `
          : `
            <div class="svg-panel">
              <div class="visual-label">3D Render</div>
              <div class="empty">这个样本还没有生成 3D 实体渲染图。</div>
            </div>
          `;

        const drawingPreviewPanel = row.png_url
          ? `
            <div class="svg-panel">
              <div class="visual-label">Drawing Preview</div>
              <img class="render-image" src="${{row.png_url}}" alt="Drawing preview" />
            </div>
          `
          : ``;

        const svgPanel = row.svg_url
          ? `
            <div class="svg-panel">
              <div class="visual-label">Four-view SVG</div>
              <object class="svg-frame" data="${{row.svg_url}}" type="image/svg+xml"></object>
            </div>
          `
          : `
            <div class="svg-panel">
              <div class="visual-label">Four-view SVG</div>
              <div class="empty">这个样本没有生成 SVG。<br/>通常是代码执行失败，或 DrawCAD 超时/出图失败。</div>
            </div>
          `;

        const errorBlock = row.sandbox_error
          ? `<div class="detail-card"><h3 style="margin-bottom:10px;">失败原因</h3><div class="error-box">${{row.sandbox_error}}</div></div>`
          : "";

        return `
          <article class="record-card">
            <div class="record-head">
              <div>
                <div class="record-title">${{row.task_name}} / ${{row.model_label}} / sample_${{row.sample_index}}</div>
                <div class="record-meta">
                  Geometry Valid: ${{row.geometry_valid}} · Sandbox OK: ${{row.sandbox_ok}} · Rubric Score: ${{fmtScore(row.rubric_score)}}<br/>
                  GT Component Count: ${{row.gt_component_count}} · Result Solid Count: ${{row.result_solid_count}} · SVG Component Estimate: ${{row.svg_component_count_estimate}}
                </div>
              </div>
              <div class="pill-row">
                <span class="pill ${{row.geometry_valid ? 'good' : 'bad'}}">Geometry ${{row.geometry_valid ? 'Pass' : 'Fail'}}</span>
                <span class="pill ${{row.component_count_match ? 'good' : 'bad'}}">Component ${{row.component_count_match ? 'Match' : 'Mismatch'}}</span>
                <span class="pill ${{row.svg_url ? 'good' : 'bad'}}">SVG ${{row.svg_url ? 'Ready' : 'Missing'}}</span>
              </div>
            </div>
            <div class="record-body">
              <div class="visual-stack">${{renderPanel}}${{drawingPreviewPanel}}${{svgPanel}}</div>
              <div class="detail-grid">
                <div class="detail-card">
                  <h3 style="margin-bottom:10px;">核心指标</h3>
                  <div class="metric-kv">
                    <div>Geometry Valid</div><div>${{row.geometry_valid}}</div>
                    <div>Watertight</div><div>${{String(row.watertight)}}</div>
                    <div>Self-Intersection Free</div><div>${{String(row.self_intersection_free)}}</div>
                    <div>Normal Consistency</div><div>${{String(row.normal_consistency)}}</div>
                    <div>Volume Valid</div><div>${{String(row.volume_valid)}}</div>
                    <div>BBox Valid</div><div>${{String(row.bbox_valid)}}</div>
                    <div>OCCT Valid</div><div>${{String(row.occt_valid)}}</div>
                    <div>Result Solid Count</div><div>${{row.result_solid_count}}</div>
                    <div>GT Component Count</div><div>${{row.gt_component_count}}</div>
                    <div>SVG Component Estimate</div><div>${{row.svg_component_count_estimate}}</div>
                    <div>Component Count Delta</div><div>${{row.component_count_delta}}</div>
                    <div>BBox DX / DY / DZ</div><div>${{row.bbox_dx_mm}} / ${{row.bbox_dy_mm}} / ${{row.bbox_dz_mm}}</div>
                    <div>SVG Path Count</div><div>${{row.svg_path_count}}</div>
                  </div>
                  <div class="link-row">
                    ${{row.render_png_url ? `<a href="${{row.render_png_url}}" target="_blank">打开 3D 渲染图</a>` : ""}}
                    ${{row.render_mesh_url ? `<a href="${{row.render_mesh_url}}" target="_blank">打开 STL</a>` : ""}}
                    ${{row.render_step_url ? `<a href="${{row.render_step_url}}" target="_blank">打开 STEP</a>` : ""}}
                    ${{row.png_url ? `<a href="${{row.png_url}}" target="_blank">打开图纸预览</a>` : ""}}
                    ${{row.svg_url ? `<a href="${{row.svg_url}}" target="_blank">打开 SVG</a>` : ""}}
                    ${{row.code_url ? `<a href="${{row.code_url}}" target="_blank">打开代码</a>` : ""}}
                  </div>
                </div>
                ${{errorBlock}}
                <div class="detail-card">
                  <h3 style="margin-bottom:10px;">Heuristic Rubric Breakdown</h3>
                  <p class="small-note" style="margin-bottom:10px;">这是旧的规则扣分结果，不是 Gemini rubric 结合 grading descriptions 的 VLM 判分。</p>
                  <table class="rubric-table">
                    <thead>
                      <tr>
                        <th>Rubric</th>
                        <th>Weight</th>
                        <th>Score</th>
                        <th>Reason</th>
                      </tr>
                    </thead>
                    <tbody>${{rubricRows}}</tbody>
                  </table>
                </div>
                <div class="detail-card">
                  <h3 style="margin-bottom:10px;">Primary Category Scores</h3>
                  <table class="rubric-table">
                    <thead>
                      <tr>
                        <th>Category</th>
                        <th>Items</th>
                        <th>Points</th>
                        <th>Ratio</th>
                      </tr>
                    </thead>
                    <tbody>${{primaryCategoryRows}}</tbody>
                  </table>
                </div>
                <div class="detail-card">
                  <h3 style="margin-bottom:10px;">Secondary Category Scores</h3>
                  <table class="rubric-table">
                    <thead>
                      <tr>
                        <th>Category</th>
                        <th>Items</th>
                        <th>Points</th>
                        <th>Ratio</th>
                      </tr>
                    </thead>
                    <tbody>${{secondaryCategoryRows}}</tbody>
                  </table>
                </div>
                <div class="detail-card">
                  <h3 style="margin-bottom:10px;">LLM Judge Breakdown</h3>
                  <p class="small-note" style="margin-bottom:10px;">这里才是 VLM 按 Gemini rubric 的 grading_descriptions 返回的逐项评分。</p>
                  ${{llmJudgeBlock}}
                </div>
              </div>
            </div>
          </article>
        `;
      }}).join("");
    }}

    renderMetricExplanations();
    renderOverall();
    renderSummaryTable();
    renderCategoryTables();
    setupFilters();
    renderRecords();
  </script>
</body>
</html>
"""

    output_path.write_text(html_text, encoding="utf-8")
    return output_path
