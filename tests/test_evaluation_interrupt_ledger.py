"""Completed experiment rows survive a later task or progress interruption."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evaluation.context_experiments import run_matrix
from evaluation.harness import CodingBenchmarkHarness, DEFAULT_BENCHMARK_PATH


@pytest.fixture
def benchmark(tmp_path, monkeypatch):
    monkeypatch.setattr("evaluation.harness.cleanup_expired_sandboxes", lambda: None)
    payload = json.loads(DEFAULT_BENCHMARK_PATH.read_text())
    payload["tasks"] = payload["tasks"][:3]
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(payload))
    return path, [task["id"] for task in payload["tasks"]]


class InterruptingHarness(CodingBenchmarkHarness):
    first_is_error = False
    restore_from_disk = False
    assert_matrix_incremental = False

    def run(self):
        try:
            return super().run()
        finally:
            if self.restore_from_disk:
                del self.latest_result

    def run_task(self, task, *, eval_dir, workspaces_root):
        self.calls = getattr(self, "calls", 0) + 1
        if self.calls == 2:
            persisted = json.loads((eval_dir / "rows.json").read_text())
            assert len(persisted) == 1 and persisted[0]["id"] != task.id
            if self.assert_matrix_incremental:
                matrix = json.loads((self.eval_root.parent / "matrix.json").read_text())
                active = next(cell for cell in matrix["cells"] if cell["status"] == "running")
                assert active["rows"] == persisted
            raise KeyboardInterrupt("stop after the first completed row")
        if self.first_is_error:
            raise ValueError("first fixture failed before verification")
        return {"id": task.id, "status": "fail", "passed": False, "execution_status": "completed",
                "verification": {"outcome": "fail"}, "marker": "do not lose this completed row",
                "execution_budget": {"model_attempts": 1, "known_tokens": 19, "usage_complete": False},
                "policy_observation": {"resolution": {"effective": self.context_policy["name"]}}}


@pytest.mark.parametrize("first_is_error", [False, True])
def test_harness_atomically_checkpoints_each_finished_task_and_closes_interrupted_ledger(benchmark, tmp_path, first_is_error):
    path, ids = benchmark
    harness = InterruptingHarness(benchmark_path=path, eval_root=tmp_path / "harness",
                                  workspace_root=tmp_path / "workspace")
    harness.first_is_error = first_is_error
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    payload = json.loads(harness.ledger_path.read_text())
    rows = json.loads(harness.ledger_path.with_name("rows.json").read_text())
    assert payload["status"] == "interrupted" and payload["rows"] == rows
    assert [row["id"] for row in rows] == ids
    assert rows[0]["status"] == ("error" if first_is_error else "fail")
    assert rows[1]["verification"]["outcome"] == rows[2]["verification"]["outcome"] == "inconclusive"
    assert rows[1]["execution_status"] == "interrupted"
    assert rows[2]["execution_status"] == "not_run"
    assert payload["manifest_after"] is not None
    assert payload["manifest"]["configuration"] == payload["manifest_after"]["configuration"]
    assert payload["summary"]["total_tasks"] == 3
    if not first_is_error:
        assert rows[0]["marker"] == "do not lose this completed row"
        assert rows[0]["execution_budget"]["known_tokens"] == 19


@pytest.mark.parametrize("disk_only", [False, True])
def test_matrix_keeps_incremental_rows_and_all_unfinished_planned_cells_on_interrupt(benchmark, tmp_path, disk_only):
    path, ids = benchmark
    class Harness(InterruptingHarness):
        restore_from_disk = disk_only
        assert_matrix_incremental = True
    with pytest.raises(KeyboardInterrupt):
        run_matrix(policies=["current", "adaptive"], benchmark_path=path,
                   eval_root=tmp_path / "matrix", harness_factory=Harness)
    matrix_path = next((tmp_path / "matrix").glob("*/matrix.json"))
    payload = json.loads(matrix_path.read_text())
    assert payload["status"] == "interrupted" and len(payload["cells"]) == 2
    interrupted, unstarted = payload["cells"]
    assert interrupted["status"] == "interrupted" and unstarted["status"] == "not_run"
    assert all([row["id"] for row in cell["rows"]] == ids for cell in payload["cells"])
    assert interrupted["rows"][0]["marker"] == "do not lose this completed row"
    assert interrupted["rows"][0]["verification"]["outcome"] == "fail"
    assert interrupted["manifest_after"] is not None
    assert all(row["verification"]["outcome"] == "inconclusive" for row in unstarted["rows"])
    assert all(value["total_tokens"] is None for value in payload["summary"].values())
    assert sum(value["planned_tasks"] for value in payload["summary"].values()) == 6
    with matrix_path.with_name("rows.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == 6
    assert "interrupted" in matrix_path.with_name("report.html").read_text()


def test_progress_callback_interruption_occurs_after_the_row_is_durable(benchmark, tmp_path):
    path, ids = benchmark
    harness = InterruptingHarness(benchmark_path=path, eval_root=tmp_path / "harness",
                                  workspace_root=tmp_path / "workspace")
    def progress(event):
        if event["event"] == "task_finished":
            rows = json.loads(harness.ledger_path.with_name("rows.json").read_text())
            assert rows[0]["marker"] == "do not lose this completed row"
            raise KeyboardInterrupt("interrupt progress delivery")
    harness.progress = progress
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    payload = json.loads(harness.ledger_path.read_text())
    assert payload["status"] == "interrupted"
    assert payload["rows"][0]["id"] == ids[0] and payload["rows"][0]["passed"] is False
    assert all(row["execution_status"] == "not_run" for row in payload["rows"][1:])


def test_interrupt_during_initial_matrix_checkpoint_closes_every_planned_cell(benchmark, tmp_path, monkeypatch):
    import evaluation.context_experiments as experiments
    original = experiments._write_report
    writes = 0
    def interrupt_first(directory, payload):
        nonlocal writes
        original(directory, payload)
        writes += 1
        if writes == 1:
            raise KeyboardInterrupt("interrupt initial report checkpoint")
    monkeypatch.setattr(experiments, "_write_report", interrupt_first)
    path, ids = benchmark
    with pytest.raises(KeyboardInterrupt):
        experiments.run_matrix(policies=["current", "adaptive"], benchmark_path=path, eval_root=tmp_path / "matrix")
    matrix_path = next((tmp_path / "matrix").glob("*/matrix.json"))
    payload = json.loads(matrix_path.read_text())
    assert payload["status"] == "interrupted"
    assert all(cell["status"] == "not_run" for cell in payload["cells"])
    assert all([row["id"] for row in cell["rows"]] == ids for cell in payload["cells"])


def test_final_fingerprint_failure_cannot_mask_interrupt_or_drop_finished_rows(benchmark, tmp_path, monkeypatch):
    import evaluation.harness as module
    original = module.evaluation_manifest
    calls = 0
    def fail_final_fingerprint(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("fingerprint source became unavailable")
        return original(**kwargs)
    monkeypatch.setattr(module, "evaluation_manifest", fail_final_fingerprint)
    path, ids = benchmark
    harness = InterruptingHarness(benchmark_path=path, eval_root=tmp_path / "harness", workspace_root=tmp_path / "workspace")
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    payload = json.loads(harness.ledger_path.read_text())
    assert payload["status"] == "interrupted"
    assert payload["manifest_after"] is None and "OSError" in payload["manifest_after_error"]
    assert payload["manifest"] and payload["rows"][0]["marker"] == "do not lose this completed row"
    assert [row["id"] for row in payload["rows"]] == ids
