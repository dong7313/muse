from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    error: str
    result_type: str
    solid_count: int
    bbox: list[float]


SANDBOX_TEMPLATE = r"""
import json
import math
import cadquery as cq

namespace = {{"__builtins__": __builtins__, "cq": cq, "cadquery": cq, "math": math}}
code = {code!r}
exec(code, namespace, namespace)
result = namespace.get("result")
if result is None:
    raise RuntimeError("Missing global variable 'result'")

shape = result.val() if hasattr(result, "val") else result
solids = []
if hasattr(shape, "Solids"):
    solids = list(shape.Solids())
elif hasattr(result, "solids"):
    solids = list(result.solids().vals())
elif hasattr(shape, "Volume"):
    solids = [shape]

bbox = None
if hasattr(shape, "BoundingBox"):
    bb = shape.BoundingBox()
    bbox = [bb.xlen, bb.ylen, bb.zlen]

payload = {{
    "ok": True,
    "error": "",
    "result_type": type(result).__name__,
    "solid_count": len(solids),
    "bbox": bbox or [0.0, 0.0, 0.0],
}}
print(json.dumps(payload))
"""


def execute_in_sandbox(code: str, timeout_seconds: int, python_executable: Path) -> SandboxResult:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(SANDBOX_TEMPLATE.format(code=code))
        temp_path = Path(handle.name)

    try:
        completed = subprocess.run(
            [str(python_executable), str(temp_path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            return SandboxResult(
                ok=False,
                error=(completed.stderr or completed.stdout).strip(),
                result_type="",
                solid_count=0,
                bbox=[0.0, 0.0, 0.0],
            )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        return SandboxResult(
            ok=bool(payload["ok"]),
            error=str(payload["error"]),
            result_type=str(payload["result_type"]),
            solid_count=int(payload["solid_count"]),
            bbox=[float(x) for x in payload["bbox"]],
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(ok=False, error=f"Sandbox timed out after {timeout_seconds}s", result_type="", solid_count=0, bbox=[0.0, 0.0, 0.0])
    finally:
        temp_path.unlink(missing_ok=True)
