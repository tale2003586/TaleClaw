from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
