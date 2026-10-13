from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.harness import CodingBenchmarkHarness
from evaluation.outcomes import finish_generation, import_official_verification, patch_digest, token_summary
from evaluation.verifiers import verify_task
from scripts import run_swebench_verified as verified
from scripts.run_swebench_verified_matrix import build_cell_result, summarize_matrix


def test_completed_without_checks_is_not_verification(tmp_path):
    (tmp_path / "run_state.json").write_text('{"status":"completed"}')
    result = verify_task(workspace=tmp_path, run_dir=tmp_path, task=SimpleNamespace(expected={}))
    assert result["outcome"] == "not_run"
    assert result["passed"] is None


def test_grader_timeout_is_inconclusive_not_model_failure(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture test", 60)
    monkeypatch.setattr("evaluation.verifiers.subprocess.run", timeout)
    result = verify_task(workspace=tmp_path, run_dir=tmp_path, task=SimpleNamespace(expected={"must_pass_command": "python test.py"}))
    assert result["outcome"] == "inconclusive"
    assert result["passed"] is None
    assert result["error_type"] == "TimeoutExpired"


def test_execution_status_does_not_replace_behavior_verification(tmp_path):
    (tmp_path / "run_state.json").write_text('{"status":"stopped"}')
    (tmp_path / "result.txt").write_text("expected")
    result = verify_task(workspace=tmp_path, run_dir=tmp_path, task=SimpleNamespace(expected={
        "file_contains": [{"path": "result.txt", "text": "expected"}],
    }))
    assert result["outcome"] == "pass"
    assert result["run_status"] == "stopped"


def test_one_task_error_remains_in_ledger_and_next_task_runs(tmp_path, monkeypatch):
    tasks = [SimpleNamespace(id="broken", category="fixture"), SimpleNamespace(id="next", category="fixture")]
    monkeypatch.setattr("evaluation.harness.load_benchmark", lambda _: tasks)
    harness = CodingBenchmarkHarness(eval_root=tmp_path / "evals", workspace_root=tmp_path / "workspaces")
    def run_task(task, **kwargs):
        if task.id == "broken":
            raise ValueError("invalid fixture")
        return {"id": task.id, "status": "pass", "passed": True, "verification": {"outcome": "pass"}}
    monkeypatch.setattr(harness, "run_task", run_task)
    monkeypatch.setattr(harness, "_plugin_manager", lambda: SimpleNamespace(after_eval=lambda **kwargs: None))
    result = harness.run()
    assert len(result["rows"]) == 2
    assert result["rows"][0]["verification"]["outcome"] == "inconclusive"
    assert result["rows"][1]["passed"] is True
    assert result["summary"]["total_tasks"] == 2
    assert result["summary"]["failed"] == 0
    assert result["summary"]["unverified"] == 1
    assert result["summary"]["pass_rate"] == 0.5


def _official_report(tmp_path, *, patch=b"patch\n", resolved=True, model="org__model"):
    directory = tmp_path / model / "repo__issue-1"
    directory.mkdir(parents=True)
    (directory / "patch.diff").write_bytes(patch)
    (directory / "report.json").write_text(json.dumps({"repo__issue-1": {"resolved": resolved, "patch_applied": True}}))
    return directory


@pytest.mark.parametrize("resolved,expected", [(True, "pass"), (False, "fail"), ("true", "inconclusive")])
def test_official_import_requires_typed_result_and_same_patch(tmp_path, resolved, expected):
    _official_report(tmp_path, resolved=resolved)
    result = import_official_verification(report_root=tmp_path, instance_id="repo__issue-1", model_name="org/model", expected_patch_sha256=patch_digest(b"patch\n"))
    assert result["outcome"] == expected


@pytest.mark.parametrize("change,reason", [("patch", "official_patch_mismatch"), ("missing", "official_report_missing"), ("model", "official_report_missing")])
def test_stale_or_other_model_official_report_is_not_accepted(tmp_path, change, reason):
    directory = _official_report(tmp_path, model="other" if change == "model" else "org__model")
    if change == "patch":
        (directory / "patch.diff").write_bytes(b"old patch")
    if change == "missing":
        (directory / "report.json").unlink()
    result = import_official_verification(report_root=tmp_path, instance_id="repo__issue-1", model_name="org/model", expected_patch_sha256=patch_digest(b"patch\n"))
    assert result["outcome"] == "inconclusive"
    assert result["reason"] == reason


def test_missing_usage_and_partial_usage_are_not_zero_cost():
    rows = [{"metrics": {"total_tokens": 10}}, {"metrics": {}}, {"metrics": {"total_tokens": None, "known_total_tokens": 4, "usage_status": "partial"}}]
    summary = token_summary(rows)
    assert summary["total_tokens"] is None
    assert summary["known_total_tokens"] == 14
    assert summary["unknown_usage_rows"] == 2
    assert token_summary([{"metrics": {"total_tokens": 0, "usage_status": "complete"}}])["total_tokens"] == 0


def test_legacy_generation_pass_is_unverified_in_batch_and_matrix(tmp_path):
    rows = [{"instance_id": "a", "status": "pass", "run_status": "completed", "metrics": {}}]
    summary = verified.summarize_rows(rows)
    assert summary["execution_completed"] == 1
    assert summary["agent_passed"] == 0
    assert summary["verified_pass_rate"] is None
    cell = build_cell_result({"cell_id": "fixture"}, cell_dir=tmp_path, batch_payload={"rows": rows, "summary": {"agent_pass_rate": 1.0}}, returncode=0, process_error="", wall_duration_ms=1)
    assert cell["status"] == "not_run"
    assert cell["tasks"][0]["passed"] is None
    assert cell["metrics"]["total_tokens"] is None
    assert cell["metrics"]["avg_total_tokens_per_instance"] is None
    matrix = summarize_matrix([cell])
    assert matrix["passed_cells"] == 0
    assert matrix["sum_total_tokens"] is None


def _fake_instance_run(tmp_path, monkeypatch, model_patch):
    instance = SimpleNamespace(instance_id="repo__issue-1", repo="owner/repo", base_commit="abc", problem_statement="fix")
    monkeypatch.setattr(verified, "load_swebench_instances", lambda **kwargs: [instance])
    monkeypatch.setattr(verified, "run_swebench_instance", lambda **kwargs: SimpleNamespace(
        instance=instance, run_state=SimpleNamespace(status="completed"), model_patch=model_patch,
        run_dir=tmp_path / "run", eval_dir=tmp_path / "eval", workspace=tmp_path / "workspace", predictions_path=tmp_path / "prediction.jsonl",
    ))


@pytest.mark.parametrize("model_patch", ["", "patch\n"])
def test_generation_only_never_counts_as_pass(tmp_path, monkeypatch, model_patch):
    _fake_instance_run(tmp_path, monkeypatch, model_patch)
    assert verified.main(["--eval-root", str(tmp_path / "batches"), "--quiet"]) == 0
    payload = json.loads(next((tmp_path / "batches").glob("*/summary.json")).read_text())
    assert payload["summary"]["agent_passed"] == 0
    assert payload["rows"][0]["execution_status"] == "completed"
    assert payload["rows"][0]["verification"]["outcome"] == "not_run"
    assert payload["summary"]["total_tokens"] is None


@pytest.mark.parametrize("same_patch,expected", [(True, "pass"), (False, "inconclusive")])
def test_batch_imports_official_results_after_harness(tmp_path, monkeypatch, same_patch, expected):
    _fake_instance_run(tmp_path, monkeypatch, "patch\n")
    official = tmp_path / "official"
    official.mkdir()
    original_run = subprocess.run
    def harness(command, **kwargs):
        if "--run_id" not in command:
            return original_run(command, **kwargs)
        run_id = command[command.index("--run_id") + 1]
        _official_report(official / "logs" / "evaluation" / run_id, patch=b"patch\n" if same_patch else b"different", model="coding-runtime")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(verified.subprocess, "run", harness)
    assert verified.main(["--eval-root", str(tmp_path / "batches"), "--swebench-repo", str(official), "--evaluate", "--quiet"]) == 0
    payload = json.loads(next((tmp_path / "batches").glob("*/summary.json")).read_text())
    assert payload["rows"][0]["verification"]["outcome"] == expected
    assert payload["summary"]["agent_passed"] == int(same_patch)


def test_scripted_coding_integration_records_execution_and_verification(tmp_path):
    # Goes through CodingApplication and its task_created/task_report events,
    # catching lifecycle metadata read before initialization as well as grading.
    result = CodingBenchmarkHarness(
        task_id="coding-git-diff-008", eval_root=tmp_path / "evals",
        workspace_root=tmp_path / "workspaces",
    ).run()
    row = result["rows"][0]
    assert row["error"] == ""
    assert row["execution_status"] == "completed"
    assert row["verification"]["outcome"] == "pass"
    assert row["passed"] is True
    assert result["manifest"]["dataset_file"]["status"] == "available"
    assert result["manifest"]["configuration"]["model_purpose"] == "coding"


def test_empty_patch_cannot_import_an_official_pass(tmp_path):
    _official_report(tmp_path, patch=b"", resolved=True)
    result = import_official_verification(report_root=tmp_path, instance_id="repo__issue-1", model_name="org/model", expected_patch_sha256=patch_digest(b""))
    assert result["outcome"] == "not_run"
    assert result["reason"] == "empty_patch"


def test_generation_completion_preserves_explicit_runtime_stop():
    from runtime.trace.run_state import RunState
    state = RunState.create(session_id="fixture:stop")
    state.stop("user_cancelled", "stopped")
    finish_generation(state, "wrapper returned normally")
    assert state.status == "stopped"
    assert state.stop_reason == "user_cancelled"
    state = RunState.create(session_id="fixture:task-stop")
    state.metadata["coding_application"] = {"execution_status": "hard_budget_exceeded"}
    finish_generation(state, "wrapper returned normally")
    assert state.status == "stopped"
    assert state.stop_reason == "hard_budget_exceeded"


def test_scripted_loop_guard_stop_can_pass_behavior_verification(tmp_path):
    result = CodingBenchmarkHarness(
        task_id="coding-repeat-tool-010", eval_root=tmp_path / "evals",
        workspace_root=tmp_path / "workspaces",
    ).run()
    row = result["rows"][0]
    assert row["error"] == ""
    assert row["execution_status"] == "stopped"
    assert row["verification"]["outcome"] == "pass"
    assert row["passed"] is True
    assert result["summary"]["passed"] == 1


def test_manifest_records_actual_source_data_config_without_environment(tmp_path, monkeypatch):
    from evaluation.provenance import evaluation_manifest
    (tmp_path / "engine.py").write_text("version = 1\n")
    dataset = tmp_path / "data.json"
    dataset.write_text('[{"id":"synthetic"}]')
    monkeypatch.setenv("API_KEY", "SECRET-PROCESS-ENV")
    (tmp_path / ".env").write_text("API_KEY=SECRET-ENV-FILE")
    first = evaluation_manifest(repo_root=tmp_path, dataset_path=dataset,
        source_paths=("engine.py", ".env"), configuration={"model_name":"fake", "api_key":"SECRET-ARG"})
    assert first["code_state"]["status"] == "unavailable"
    assert first["code_state"]["dirty"] is None
    assert first["source_files"][".env"]["status"] == "excluded"
    assert first["dataset_file"]["status"] == "available"
    assert first["configuration"] == {"model_name":"fake"}
    assert "SECRET" not in json.dumps(first)
    (tmp_path / "engine.py").write_text("version = 2\n")
    second = evaluation_manifest(repo_root=tmp_path, source_paths=("engine.py",))
    assert first["source_files"]["engine.py"]["sha256"] != second["source_files"]["engine.py"]["sha256"]


def test_manifest_records_git_head_and_dirty_state_without_changing_index(tmp_path):
    from evaluation.provenance import evaluation_manifest
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (tmp_path / "engine.py").write_text("original")
    git("add", "engine.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    head = git("rev-parse", "HEAD")
    clean = evaluation_manifest(repo_root=tmp_path, source_paths=("engine.py",))
    assert clean["code_state"]["head"] == head and clean["code_state"]["dirty"] is False
    (tmp_path / "engine.py").write_text("modified")
    before = (tmp_path / ".git" / "index").read_bytes()
    dirty = evaluation_manifest(repo_root=tmp_path, source_paths=("engine.py",))
    assert dirty["code_state"]["dirty"] is True
    assert (tmp_path / ".git" / "index").read_bytes() == before


@pytest.mark.parametrize("verification", [{"outcome":"future_unknown"}, {"outcome":[]}, "corrupt"])
def test_invalid_verification_cannot_make_matrix_cell_pass(tmp_path, verification):
    rows = [{"instance_id":"a", "status":"pass", "verification":verification}]
    summary = verified.summarize_rows(rows)
    assert summary["verification_inconclusive"] == 1
    cell = build_cell_result({"cell_id":"fixture"}, cell_dir=tmp_path, batch_payload={"rows":rows}, returncode=0, process_error="", wall_duration_ms=1)
    assert cell["status"] == "inconclusive"
    assert cell["tasks"][0]["passed"] is None
