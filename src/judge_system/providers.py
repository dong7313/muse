from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from .config import ModelSpec


CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str:
    match = CODE_BLOCK_PATTERN.search(text)
    if match:
        return match.group(1).strip() + "\n"
    return text.strip() + "\n"


def _extract_text(text: str) -> str:
    return text.strip() + "\n"


class ModelProvider(Protocol):
    def generate_code(self, *, system_prompt: str, user_prompt: str, spec: ModelSpec, timeout_seconds: int) -> str:
        ...


@dataclass
class OpenAIResponsesProvider:
    def _build_client(self, spec: ModelSpec):
        api_key = os.environ.get(spec.api_key_env)
        if not api_key:
            return None
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=spec.base_url)

    def is_available(self, spec: ModelSpec) -> bool:
        return self._build_client(spec) is not None

    def generate_code(self, *, system_prompt: str, user_prompt: str, spec: ModelSpec, timeout_seconds: int) -> str:
        client = self._build_client(spec)
        if client is None:
            raise RuntimeError(f"{spec.api_key_env} is not configured for model {spec.label}.")

        response = client.responses.create(
            model=spec.model,
            temperature=spec.temperature,
            timeout=timeout_seconds,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
        )
        return _extract_code(response.output_text)

    def generate_text(self, *, system_prompt: str, user_prompt: str, spec: ModelSpec, timeout_seconds: int) -> str:
        client = self._build_client(spec)
        if client is None:
            raise RuntimeError(f"{spec.api_key_env} is not configured for model {spec.label}.")

        response = client.responses.create(
            model=spec.model,
            temperature=spec.temperature,
            timeout=timeout_seconds,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
        )
        return _extract_text(response.output_text)


@dataclass
class OpenRouterChatProvider:
    def is_available(self, spec: ModelSpec) -> bool:
        return bool(os.environ.get(spec.api_key_env))

    def generate_code(self, *, system_prompt: str, user_prompt: str, spec: ModelSpec, timeout_seconds: int) -> str:
        api_key = os.environ.get(spec.api_key_env)
        if not api_key:
            raise RuntimeError(f"{spec.api_key_env} is not configured for model {spec.label}.")

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    f"{spec.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://local.codex.app",
                        "X-Title": "judge_system code generation",
                    },
                    json={
                        "model": spec.model,
                        "temperature": spec.temperature,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                    timeout=timeout_seconds,
                )
                if not response.ok:
                    detail = (response.text or "").strip()
                    if len(detail) > 2000:
                        detail = detail[:2000] + "...[truncated]"
                    raise RuntimeError(
                        f"OpenRouter request failed for {spec.label} ({spec.model}) with status "
                        f"{response.status_code}: {detail}"
                    )
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                return _extract_code(content)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(min(2 * attempt, 5))
        raise RuntimeError(f"OpenRouter request failed for {spec.label} ({spec.model}): {last_error}")

    def generate_text(self, *, system_prompt: str, user_prompt: str, spec: ModelSpec, timeout_seconds: int) -> str:
        api_key = os.environ.get(spec.api_key_env)
        if not api_key:
            raise RuntimeError(f"{spec.api_key_env} is not configured for model {spec.label}.")

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    f"{spec.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://local.codex.app",
                        "X-Title": "judge_system task-plan inference",
                    },
                    json={
                        "model": spec.model,
                        "temperature": spec.temperature,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                    timeout=timeout_seconds,
                )
                if not response.ok:
                    detail = (response.text or "").strip()
                    if len(detail) > 2000:
                        detail = detail[:2000] + "...[truncated]"
                    raise RuntimeError(
                        f"OpenRouter request failed for {spec.label} ({spec.model}) with status "
                        f"{response.status_code}: {detail}"
                    )
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                return _extract_text(content)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(min(2 * attempt, 5))
        raise RuntimeError(f"OpenRouter request failed for {spec.label} ({spec.model}): {last_error}")


@dataclass
class ManualImportProvider:
    input_root: Path

    def generate_code(self, *, system_prompt: str, user_prompt: str, spec: ModelSpec, timeout_seconds: int) -> str:
        raise RuntimeError("Manual provider does not generate code directly.")

    def load_code(self, task_name: str, model_label: str, sample_index: int) -> str:
        code_path = self.input_root / task_name / model_label / f"sample_{sample_index}.py"
        if not code_path.exists():
            raise FileNotFoundError(
                f"Manual input not found: {code_path}. Place chat-generated CadQuery code here and rerun."
            )
        return code_path.read_text(encoding="utf-8")

    def write_prompt_bundle(self, task_name: str, model_label: str, sample_index: int, system_prompt: str, user_prompt: str) -> Path:
        target_dir = self.input_root / task_name / model_label
        target_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = target_dir / f"sample_{sample_index}.prompt.json"
        prompt_path.write_text(
            json.dumps(
                {
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return prompt_path
