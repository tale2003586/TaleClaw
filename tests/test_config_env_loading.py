from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_env_example_uses_task_state_dynamic_budget_configuration() -> None:
    values = _dotenv_values(ROOT / ".env.example")

    assert {
        "ARTIFACT_OFFLOADING_ENABLED": "1",
        "DYNAMIC_PROMPT_BUDGET_ENABLED": "1",
        "PROMPT_SOFT_COMPACTION_RATIO": "0.70",
        "PROMPT_COMPACTION_TARGET_RATIO": "0.45",
        "PROMPT_HARD_INPUT_RATIO": "0.92",
        "PROMPT_SAFETY_MARGIN_TOKENS": "0",
        "LONG_CONTENT_MAX_TOKENS": "4000",
        "LONG_CONTENT_MAX_CHARS": "20000",
        "LONG_CONTENT_MAX_BYTES": "64000",
        "CONTEXT_ARTIFACT_ROOT": ".coding_applications/artifacts",
        "SUBAGENT_MAX_REASONING_STEPS": "16",
        "WORKSPACE_ROOTS": ".",
        "DEFAULT_CODING_WORKSPACE": ".",
    }.items() <= values.items()
    assert {
        "LLM_DEFAULT_PROVIDER",
        "CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS",
        "CODING_CONTEXT_COMPACTION_TARGET_TOKENS",
        "CODING_CONTEXT_RECENT_GROUPS",
    }.isdisjoint(values)


def test_config_loads_project_dotenv_before_evaluating_constants(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "config.py", tmp_path / "config.py")
    shutil.copy2(ROOT / "runtime" / "env_loader.py", runtime_dir / "env_loader.py")

    allowed = tmp_path / "allowed"
    default = allowed / "project"
    default.mkdir(parents=True)
    (tmp_path / ".env").write_text(
        f"WORKSPACE_ROOTS={allowed}\nDEFAULT_CODING_WORKSPACE={default}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("WORKSPACE_ROOTS", None)
    env.pop("DEFAULT_CODING_WORKSPACE", None)
    env["PYTHONPATH"] = str(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, config; "
                "print(json.dumps({"
                "'roots': [str(item) for item in config.WORKSPACE_ROOTS], "
                "'default': str(config.DEFAULT_CODING_WORKSPACE)"
                "}))"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload == {
        "roots": [str(allowed.resolve())],
        "default": str(default.resolve()),
    }


def test_explicit_environment_keeps_precedence_over_dotenv(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "config.py", tmp_path / "config.py")
    shutil.copy2(ROOT / "runtime" / "env_loader.py", runtime_dir / "env_loader.py")
    (tmp_path / ".env").write_text(
        "WORKSPACE_ROOTS=/from-dotenv\nDEFAULT_CODING_WORKSPACE=/from-dotenv\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    env = os.environ.copy()
    env["WORKSPACE_ROOTS"] = str(explicit)
    env["DEFAULT_CODING_WORKSPACE"] = str(explicit)
    env["PYTHONPATH"] = str(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.WORKSPACE_ROOTS[0]); print(config.DEFAULT_CODING_WORKSPACE)",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [str(explicit.resolve()), str(explicit.resolve())]
