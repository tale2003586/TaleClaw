from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.swebench_adapter import SweBenchInstance, prepare_swebench_workspace
from evaluation.verifiers import verify_task


@pytest.mark.parametrize("diff", [None, {}, {"modified": [], "created": []}, {
    "modified": [], "created": [], "deleted": "",
}, {"modified": [{}], "created": [], "deleted": []}])
def test_missing_or_invalid_diff_cannot_prove_file_unchanged(tmp_path, diff):
    if diff is not None:
        (tmp_path / "workspace_diff.json").write_text(json.dumps(diff))
    result = verify_task(
        workspace=tmp_path, run_dir=tmp_path,
        task=SimpleNamespace(expected={"not_modified": ["implementation.py"]}),
    )
    assert result["outcome"] == "inconclusive"
    assert result["passed"] is None


def test_complete_empty_diff_can_prove_file_unchanged(tmp_path):
    (tmp_path / "workspace_diff.json").write_text(json.dumps({
        "modified": [], "created": [], "deleted": [],
    }))
    result = verify_task(
        workspace=tmp_path, run_dir=tmp_path,
        task=SimpleNamespace(expected={"not_modified": ["implementation.py"]}),
    )
    assert result["outcome"] == "pass"
    assert result["passed"] is True


@pytest.mark.parametrize("instance_id", ["", "  ", ".", "..", " .. "])
def test_invalid_instance_id_rejected_before_workspace_operations(tmp_path, instance_id):
    parent = tmp_path / "workspaces"
    parent.mkdir()
    sentinel = parent / "keep.txt"
    sentinel.write_text("untouched")
    instance = SweBenchInstance(instance_id, "owner/repo", "abc", "fix")
    with (
        patch("evaluation.swebench_adapter.shutil.rmtree") as remove,
        patch("evaluation.swebench_adapter.clone_swebench_repo") as clone,
        patch("evaluation.swebench_adapter._run_git") as git,
    ):
        with pytest.raises(ValueError):
            prepare_swebench_workspace(instance, workspace_parent=parent)
        remove.assert_not_called()
        clone.assert_not_called()
        git.assert_not_called()
    assert sentinel.read_text() == "untouched"
