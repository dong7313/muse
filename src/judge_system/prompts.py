from __future__ import annotations

SYSTEM_PROMPT = """You are a senior CAD engineer writing CadQuery code from a confirmed task and plan.

Return only executable Python code.
Rules:
- Use only Python stdlib, math, and cadquery.
- Do not write files.
- Do not print explanations.
- Create a global variable named result.
- Prefer a Compound or Assembly-like disconnected solid layout when the plan requires multiple independent components.
- Preserve object count, proportions, and assembly intent from the task and plan.
- Keep the code deterministic and directly executable.
"""


TASKPLAN_INFERENCE_PROMPT = """You are an expert CAD data engineer.

Task:
From the provided case name, CAD code, and SVG preview, reconstruct both task.md and plan.md in the same style as the examples.

Rules:
- Keep Chinese-language markdown output.
- Return strict JSON only, with keys: task_markdown, plan_markdown, rationale.
- No markdown fences or prose outside JSON.
- task_markdown should include all required sections to describe the confirmed design task.
- plan_markdown must include plan details for component decomposition and assembly intent.
- Use existing formatting conventions ("##" section titles and concise bullet/number lists).
- If some fields are uncertain, mark them as empty string rather than inventing unrelated details.
- Keep component names and counts concise and realistic; stay faithful to provided CAD code, svg, and examples.

Output format:
{
  "task_markdown": "...",
  "plan_markdown": "...",
  "rationale": "short note on uncertainty points"
}

Reference examples:
{examples}

Target case:
Case name: {case_name}

CadQuery code:
```python
{code_text}
```

SVG source:
```xml
{svg_text}
```
"""


def build_user_prompt(task_text: str, plan_text: str) -> str:
    return f"""Task markdown:
```markdown
{task_text.strip()}
```

Plan markdown:
```markdown
{plan_text.strip()}
```

Return only executable CadQuery Python code.
"""
