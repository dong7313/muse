# Geometry metric notes

Current judge metrics combine reusable CadQuery validator checks with lightweight SVG and rubric heuristics.

## Geometry validity

The implementation currently scores these as first-class hard checks:

- watertightness / closed-solid validity
- self-intersection and generic topological invalidity
- non-manifold edge detection
- zero or invalid volume
- degenerate bounding box

Implementation source:

- CadQuery validator: `external/validator/validator.py` (see README → "External dependencies")
- OCCT-based validity analysis through `BRepCheck_Analyzer`

External references that motivated these checks:

- Jun, `"A geometric processing framework for CAD model quality control"`, Computer-Aided Design, 2002. This frames manifoldness, self-intersection, and validity as core CAD quality dimensions.
- Peters et al., `"Representational validity verification of 3D solid models in explicit and procedural form"`, Journal of Computational Design and Engineering, 2022. This separates representational validity from higher-level semantic correctness and supports using closed-solid validity as a base gate.
- Open CASCADE `BRepCheck_Analyzer` documentation, for practical validity checking at the B-Rep level.

## Component count

Two complementary signals are used:

- `solid_count` from executed CadQuery geometry
- `estimated_component_count` from SVG path-cluster grouping

The plan-derived expected component count comes from:

- `## 计划装配体数量`
- fallback: counting `###` component sections in `plan.md`

## Rubric scoring

`Functions and evaluation weights` / `功能需求与评价权重` are extracted into normalized rubric items.

Current scoring is intentionally transparent and heuristic:

- closed-solid requirements -> geometry validity + watertightness
- assembly-equivalence requirements -> expected vs actual component count
- size/proportion requirements -> non-degenerate bounding box presence
- manufacturability requirements -> self-intersection / non-manifold / volume failures
- stability requirements -> valid geometry with at least one solid

This is good enough for batch triage, but the next upgrade should add an optional LLM judge over SVG plus task/plan context.
