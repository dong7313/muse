from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union


@dataclass(frozen=True)
class ModelSpec:
    label: str
    provider: str
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.2


@dataclass(frozen=True)
class AppConfig:
    source_root: Path
    workspace_root: Path
    drawcad_root: Path
    drawcad_runner: Path
    validator_root: Path
    python_executable: Path
    selected_tasks: list[str]
    models: list[ModelSpec]
    test_list_path: Path | None
    model_list_path: Path | None
    samples_per_task: int
    paper_size: str
    generation_timeout_seconds: int
    execution_timeout_seconds: int
    max_code_chars: int
    excel_filename: str
    llm_judge_enabled: bool
    llm_judge_model: str
    llm_judge_api_key_env: str
    llm_judge_base_url: str
    llm_judge_timeout_seconds: int
    llm_judge_rubric_root: Path | None

    @property
    def data_root(self) -> Path:
        return self.workspace_root / "data"

    @property
    def cleaned_data_root(self) -> Path:
        return self.data_root / "cleaned"

    @property
    def rubric_root(self) -> Path:
        return self.data_root / "rubrics"

    @property
    def results_root(self) -> Path:
        return self.workspace_root / "results"

    def resolve_selected_tasks(self) -> list[str]:
        if self.test_list_path:
            return _load_test_list(self.test_list_path)
        if self.selected_tasks:
            return self.selected_tasks
        if not self.cleaned_data_root.exists():
            return []
        return sorted(path.name for path in self.cleaned_data_root.iterdir() if path.is_dir())

    def resolve_models(self) -> list[ModelSpec]:
        if self.model_list_path:
            return _load_model_list(self.model_list_path)
        return self.models

    def resolve_llm_judge_rubric_root(self) -> Path:
        return self.llm_judge_rubric_root or self.rubric_root


def _normalize_list_line(line: str) -> str:
    value = line.strip()
    if not value or value.startswith("#"):
        return ""
    if value.startswith("- "):
        return value[2:].strip()
    if value[:2].isdigit() and value[2:4] == ". ":
        return value[4:].strip()
    return value


def _load_test_list(path: Path) -> list[str]:
    tasks: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = _normalize_list_line(raw_line)
        if not line:
            continue
        if line.startswith("`") and line.endswith("`"):
            line = line[1:-1].strip()
        tasks.append(line)
    return tasks


def _load_model_list(path: Path) -> list[ModelSpec]:
    models: list[ModelSpec] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            raise ValueError(
                f"Each model_list line must be 'label | provider | model | api_key_env | base_url [| temperature]': {raw_line}"
            )
        label, provider, model, api_key_env, base_url = parts[:5]
        temperature = float(parts[5]) if len(parts) >= 6 and parts[5] else 0.2
        models.append(
            ModelSpec(
                label=label,
                provider=provider,
                model=model,
                api_key_env=api_key_env,
                base_url=base_url,
                temperature=temperature,
            )
        )
    return models


_ENV_VAR_RE = __import__("re").compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


def _expand_env(value: Any) -> Any:
    import os
    if isinstance(value, str):
        def sub(match):
            name, default = match.group(1), match.group(2) or ""
            return os.environ.get(name, default)
        return _ENV_VAR_RE.sub(sub, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _resolve_path(value: str, base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(path: Union[str, Path]) -> AppConfig:
    config_path = Path(path).resolve()
    raw: dict[str, Any] = _expand_env(json.loads(config_path.read_text(encoding="utf-8")))
    base = config_path.parent.parent  # repo root (configs/ is one level under)
    _rp = lambda v: _resolve_path(v, base)
    return AppConfig(
        source_root=_rp(raw["source_root"]),
        workspace_root=_rp(raw["workspace_root"]),
        drawcad_root=_rp(raw["drawcad_root"]),
        drawcad_runner=_rp(raw["drawcad_runner"]),
        validator_root=_rp(raw["validator_root"]),
        python_executable=Path(raw["python_executable"]).expanduser(),
        selected_tasks=list(raw["selected_tasks"]),
        models=[ModelSpec(**item) for item in raw["models"]],
        test_list_path=_rp(raw["test_list_path"]) if raw.get("test_list_path") else None,
        model_list_path=_rp(raw["model_list_path"]) if raw.get("model_list_path") else None,
        samples_per_task=int(raw["samples_per_task"]),
        paper_size=str(raw["paper_size"]),
        generation_timeout_seconds=int(raw["generation_timeout_seconds"]),
        execution_timeout_seconds=int(raw["execution_timeout_seconds"]),
        max_code_chars=int(raw["max_code_chars"]),
        excel_filename=str(raw["excel_filename"]),
        llm_judge_enabled=bool(raw.get("llm_judge_enabled", False)),
        llm_judge_model=str(raw.get("llm_judge_model", "openai/gpt-4o-mini")),
        llm_judge_api_key_env=str(raw.get("llm_judge_api_key_env", "OPENROUTER_API_KEY")),
        llm_judge_base_url=str(raw.get("llm_judge_base_url", "https://openrouter.ai/api/v1")),
        llm_judge_timeout_seconds=int(raw.get("llm_judge_timeout_seconds", 180)),
        llm_judge_rubric_root=_rp(raw["llm_judge_rubric_root"]) if raw.get("llm_judge_rubric_root") else None,
    )
