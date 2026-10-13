"""Adversarial reporting checks: no native/model/zero-cost claims by omission."""
from copy import deepcopy
import json

import pytest

from evaluation.context_experiments import run_matrix, summarize_cells
from evaluation.harness import CodingBenchmarkHarness, _tool_access_proxies


def row(policy, *, identifier="coding-git-diff-008", model="test-model", provider="test-provider", outcome="pass"):
    return {"id": identifier, "verification": {"outcome": outcome},
            "policy_observation": {"resolution": {"requested": policy, "effective": policy}},
            "actual_model_calls": [{"model": model, "provider": provider, "profile": "route", "policy": policy}],
            "execution_budget": {"model_attempts": 1, "known_tokens": 10, "usage_complete": True}}


def cell(policy, rows):
    manifest = {"source_set_sha256": "a" * 64,
                "source_files": {"fixture.py": {"status": "available", "sha256": "a" * 64}},
                "fixtures_sha256": "b" * 64,
                "fixtures": {item["id"]: {"status": "available", "files": {"source.py": {"status": "available", "sha256": "c" * 64}}} for item in rows},
                "dataset_file": {"status": "available", "sha256": "d" * 64},
                "configuration": {"context_policy": {"name": policy}, "execution_budget_limits": {"max_model_attempts": 8}}}
    return {"policy": {"name": policy}, "repeat": 1, "runner_mode": "real", "status": "completed", "rows": rows,
            "manifest": manifest, "manifest_after": deepcopy(manifest)}


@pytest.mark.parametrize("defect", ["missing_calls", "missing_model", "different_model", "different_provider", "mixed_policy", "untraced_helper"])
def test_unobserved_or_different_routes_do_not_form_same_model_pairs(defect):
    first, second = row("current"), row("native")
    if defect == "missing_calls":
        first["actual_model_calls"] = second["actual_model_calls"] = []
    elif defect == "missing_model":
        first["actual_model_calls"][0]["model"] = second["actual_model_calls"][0]["model"] = None
    elif defect == "different_model":
        second["actual_model_calls"][0]["model"] = "other"
    elif defect == "different_provider":
        second["actual_model_calls"][0]["provider"] = "other"
    elif defect == "untraced_helper":
        first["execution_budget"]["model_attempts"] = 2
    else:
        second["actual_model_calls"].append({**second["actual_model_calls"][0], "policy": "masking"})
    summary = summarize_cells([cell("current", [first]), cell("native", [second])])
    assert summary["native"]["paired_vs_current"]["valid_pairs"] == 0


def test_fallback_cannot_win_native_but_its_real_cost_remains_counted():
    fallback = row("masking")
    fallback["policy_observation"]["resolution"]["requested"] = "native"
    summary = summarize_cells([cell("current", [row("current")]), cell("native", [fallback])])["native"]
    assert summary["verified_pass"] == 0 and summary["fallback_tasks"] == 1
    assert summary["paired_vs_current"]["valid_pairs"] == 0
    assert summary["known_tokens"] == 10 and summary["total_tokens"] == 10
    assert summary["pass_rate_all_attempts"] == 0


def test_missing_usage_is_unknown_even_when_known_token_subtotal_is_zero():
    item = row("current")
    item["execution_budget"] = {"known_tokens": 0, "model_attempts": 1, "usage_complete": False}
    summary = summarize_cells([cell("current", [item])])["current"]
    assert summary["total_tokens"] is None and not summary["usage_complete"]


@pytest.mark.parametrize("failure", ["raise", "omit"])
def test_harness_cell_failure_keeps_every_planned_task_in_denominator(tmp_path, failure):
    class Harness:
        def __init__(self, **kwargs):
            pass
        def run(self):
            if failure == "raise":
                raise RuntimeError("simulated fixture preparation error")
            return {"rows": [], "runner": {}, "manifest": {}, "eval_id": "missing"}
    result = run_matrix(policies=["current"], runner_mode="real", task_id="coding-git-diff-008",
                        eval_root=tmp_path, harness_factory=Harness)
    assert result["status"] == "completed_with_errors"
    summary = result["summary"]["current"]
    assert summary["planned_tasks"] == 1 and summary["tasks"] == 1 and summary["unverified"] == 1
    assert summary["pass_rate_all_attempts"] == 0
    assert summary["total_tokens"] is None and result["same_actual_model"] is None


@pytest.mark.parametrize("options", [{"limits": {"max_seconds": 0}}, {"limits": {"typo": 2}},
    {"max_reasoning_steps": -1}, {"task_id": "missing"}, {"policies": {"name": "current"}}])
def test_invalid_cli_equivalent_config_fails_before_creating_any_report(tmp_path, options):
    with pytest.raises(ValueError):
        run_matrix(eval_root=tmp_path, **options)
    assert not list(tmp_path.iterdir())


def test_real_harness_records_actual_native_fallback_resolution(tmp_path):
    result = CodingBenchmarkHarness(task_id="coding-git-diff-008", runner_mode="scripted", eval_root=tmp_path,
        context_policy={"name": "native", "native_fallback": "masking"}).run()
    observation = result["rows"][0]["policy_observation"]["resolution"]
    assert observation["requested"] == "native" and observation["effective"] == "masking"
    assert result["rows"][0]["tool_access_proxies"]["trace_available"]


def test_tool_access_counts_exact_arguments_and_failed_recovery_separately(tmp_path):
    events = []
    def add(name, identifier, arguments=None, status="success", **kwargs):
        payload = {"tool_name": name, "tool_call_id": identifier, "status": status, **kwargs}
        if arguments is not None:
            payload["final_arguments_preview"] = json.dumps(arguments)
        events.append({"event": "tool.call.completed" if status == "success" else "tool.call.failed", "payload": payload})
    add("read_file", "a", {"path": "a.py", "offset": 0})
    add("read_file", "b", {"offset": 0, "path": "a.py"})
    events.append(deepcopy(events[-1]))  # Duplicate receipt is not a second execution.
    add("read_file", "c", {"path": "a.py", "offset": 10})
    add("read_artifact", "d", {"artifact_ref": "event://x"})
    add("retrieve_tool_result", "e", {"id": "y"}, status="denied")
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(event) for event in events))
    proxies = _tool_access_proxies(tmp_path)
    assert proxies["repeat_read_file_calls"] == 1
    assert proxies["recovery_attempts"] == 2 and proxies["recovery_tool_successes"] == 1
    events[0]["payload"]["final_arguments_preview"] = "{truncated..."
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(event) for event in events))
    assert _tool_access_proxies(tmp_path)["repeat_read_file_calls"] is None


def test_missing_tool_trace_does_not_mean_zero_repeated_reads_or_zero_recovery(tmp_path):
    result = _tool_access_proxies(tmp_path)
    assert result["repeat_read_file_calls"] is None
    assert result["recovery_attempts"] is None
    (tmp_path / "trace.jsonl").write_text("unparseable receipt")
    result = _tool_access_proxies(tmp_path)
    assert result["repeat_read_file_calls"] is None
    assert result["recovery_attempts"] is None


from evaluation.harness import _context_intervention_evidence
from runtime.trace.context_intervention import request_context_intervention
from runtime.context.policies import ContextPolicy, plan_context
from applications.coding.context_state import group_messages
from runtime.sessions.session import Session


def intervention(status="none", **kwargs):
    return {"schema_version": 1, "status": status, "requested_calls": 1,
            "audited_calls": 1, "completed_calls": 1,
            "intervention_observed": status == "observed", "reasons": [],
            "hard_trim_calls": 0, **kwargs}


def write_intervention_trace(path, policy="masking", audit=None, native=None, events=()):
    summary = {"context_policy": policy}
    if audit is not None:
        summary["context_intervention"] = audit
    rows = [{"event": "model.call.started", "span_id": "m1", "step": 1,
             "payload": {"context_summary": summary}},
            {"event": "model.call.completed", "span_id": "m1", "step": 1,
             "payload": {"context_policy": policy, "provider_metadata": native or {}}}, *events]
    (path / "trace.jsonl").write_text("\n".join(json.dumps(item) for item in rows))


def local_audit(policy="masking", **kwargs):
    return {"schema_version": 1, "policy": policy, "audit_complete": True,
            "masked_observations": 0, "dropped_groups": 0, "snapshot_id": "", **kwargs}


def test_missing_intervention_audit_never_means_no_intervention(tmp_path):
    assert _context_intervention_evidence(tmp_path)["status"] == "unknown"
    write_intervention_trace(tmp_path)
    assert _context_intervention_evidence(tmp_path)["status"] == "unknown"


def test_policy_selection_without_changes_is_not_compression_evidence(tmp_path):
    write_intervention_trace(tmp_path, audit=local_audit())
    evidence = _context_intervention_evidence(tmp_path)
    assert evidence["status"] == "none" and evidence["masked_observations"] == 0
    a, b = row("current"), row("masking")
    a["context_intervention"] = b["context_intervention"] = intervention()
    summary = summarize_cells([cell("current", [a]), cell("masking", [b])])["masking"]
    assert summary["verified_pass"] == 1
    assert summary["paired_vs_current"]["valid_pairs"] == 1
    assert summary["paired_vs_current"]["intervention_pairs"] == 0
    assert summary["compression_comparison_status"] == "not_established"


def test_audit_counts_real_masks_drops_and_distinct_snapshots(tmp_path):
    write_intervention_trace(tmp_path, policy="adaptive", audit=local_audit("adaptive", masked_observations=2, dropped_groups=3))
    evidence = _context_intervention_evidence(tmp_path)
    assert evidence["status"] == "observed" and evidence["masked_observations"] == 2 and evidence["dropped_groups"] == 3
    write_intervention_trace(tmp_path, policy="current", audit=local_audit("current", snapshot_id="snapshot-1"))
    assert _context_intervention_evidence(tmp_path)["snapshots_used"] == 1


@pytest.mark.parametrize("metadata,status,count", [
    ({"context_policy": "native", "native_context": {"opaque_state": True, "compaction_item_count": 0}}, "none", 0),
    ({"context_policy": "native", "native_context": {"compaction_item_count": 1}}, "observed", 1),
    ({"context_policy": "native", "native_context": {"compaction_item_count": 0}, "compaction_usage": {}}, "observed", 1),
    ({"native_context": {"compaction_item_count": 1}}, "unknown", 0),
    ({"context_policy": "native", "native_context": {"opaque_state": True}}, "unknown", 0),
])
def test_native_reasoning_or_uncommitted_output_is_not_compaction(tmp_path, metadata, status, count):
    write_intervention_trace(tmp_path, policy="native", audit=local_audit("native"), native=metadata)
    evidence = _context_intervention_evidence(tmp_path)
    assert evidence["status"] == status and evidence["native_compactions"] == count


def test_small_abstract_plan_without_changed_observation_is_not_masking():
    session = Session("eval:small")
    session.add_message("user", "fix")
    for index in range(3):
        session.add_message("assistant", None, tool_calls=[{"id": str(index), "type": "function", "function": {"name": "read_file", "arguments": "{}"}}])
        session.add_message("tool", "tiny", tool_call_id=str(index))
    plan = plan_context(session, groups=group_messages(session.messages), policy=ContextPolicy("masking", recent_groups=1), max_tokens=1000)
    session.metadata["context_policy_audit"] = plan.audit()
    assert plan.audit()["group_counts"]["abstract"] > 0
    audit = request_context_intervention(session, plan.messages, "masking")
    assert audit["audit_complete"] and audit["masked_observations"] == 0


def test_final_request_removal_cannot_count_a_mask_only_present_in_plan():
    from tests.test_context_policy_experiments import session_fixture
    session = session_fixture()
    plan = plan_context(session, groups=group_messages(session.messages), policy=ContextPolicy("masking", recent_groups=1), max_tokens=1000)
    session.metadata["context_policy_audit"] = plan.audit()
    assert request_context_intervention(session, plan.messages, "masking")["masked_observations"] > 0
    final_messages = [message for message in plan.messages if not (message.get("metadata") or {}).get("context_projection")]
    assert request_context_intervention(session, final_messages, "masking")["masked_observations"] == 0


def test_policy_audit_mismatch_and_malformed_trace_are_unknown(tmp_path):
    write_intervention_trace(tmp_path, policy="raw", audit=local_audit("masking", masked_observations=3))
    assert _context_intervention_evidence(tmp_path)["status"] == "unknown"
    write_intervention_trace(tmp_path, audit=local_audit(masked_observations=3))
    with (tmp_path / "trace.jsonl").open("a") as handle:
        handle.write("\nnot-json")
    result = _context_intervention_evidence(tmp_path)
    assert result["intervention_observed"] and result["status"] == "unknown"


@pytest.mark.parametrize("defect", ["unknown", "execution_failed", "usage_missing", "hard_trim"])
def test_intervention_alone_cannot_establish_successful_cost_comparison(defect):
    a, b = row("current"), row("masking")
    for item in (a, b):
        item["execution_status"] = "completed"
        item["context_intervention"] = intervention("observed")
    if defect == "unknown":
        b["context_intervention"] = {}
    elif defect == "execution_failed":
        b["execution_status"] = "failed"
    elif defect == "usage_missing":
        b["execution_budget"]["usage_complete"] = False
    else:
        b["context_intervention"]["hard_trim_calls"] = 1
    summary = summarize_cells([cell("current", [a]), cell("masking", [b])])["masking"]
    assert summary["verified_pass"] == 1
    assert summary["paired_vs_current"]["verified_cost_pairs"] == 0
    assert summary["compression_comparison_status"] == "not_established"


def test_completed_verified_usage_complete_intervention_pair_is_eligible_only():
    a, b = row("current"), row("masking")
    for item in (a, b):
        item["execution_status"] = "completed"
        item["context_intervention"] = intervention()
    b["context_intervention"] = intervention("observed")
    result = summarize_cells([cell("current", [a]), cell("masking", [b])])["masking"]
    assert result["paired_vs_current"]["verified_cost_pairs"] == 1
    assert result["compression_comparison_status"] == "eligible_verified_cost_pairs"


def test_provenance_hashes_policy_configuration_fixture_bytes_and_context_sources(tmp_path):
    from evaluation.provenance import evaluation_manifest, SOURCE_PATHS
    benchmarks = tmp_path / "benchmarks"
    fixture = benchmarks / "fixtures" / "example"
    fixture.mkdir(parents=True)
    (fixture / "source.txt").write_text("initial")
    (fixture / ".env").write_text("secret-placeholder")
    dataset = benchmarks / "tasks.json"
    dataset.write_text(json.dumps({"tasks": [{"id": "t", "fixture_repo": "benchmarks/fixtures/example"}]}))
    config = {"context_policy": {"name": "masking", "recent_groups": 1}, "execution_budget_limits": {"max_model_attempts": 8}, "selected_ids": ["t"]}
    first = evaluation_manifest(dataset_path=dataset, configuration=config)
    assert first["configuration"]["context_policy"] == config["context_policy"]
    assert first["configuration"]["execution_budget_limits"] == config["execution_budget_limits"]
    assert first["fixtures"]["t"]["files"][".env"] == {"status": "excluded"}
    (fixture / "source.txt").write_text("changed")
    second = evaluation_manifest(dataset_path=dataset, configuration=config)
    assert first["fixtures_sha256"] != second["fixtures_sha256"]
    assert first["dataset_file"] == second["dataset_file"]
    assert {"runtime/context/policies.py", "applications/coding/context_state.py", "runtime/context/snapshots.py"} <= set(SOURCE_PATHS)
    third = evaluation_manifest(dataset_path=dataset, configuration={**config, "context_policy": {"name": "raw"}})
    assert second["configuration_sha256"] != third["configuration_sha256"]


def test_actual_scripted_harness_has_complete_none_audit_for_small_fixture(tmp_path):
    result = CodingBenchmarkHarness(task_id="coding-git-diff-008", runner_mode="scripted", eval_root=tmp_path,
                                   context_policy="current").run()
    evidence = result["rows"][0]["context_intervention"]
    assert evidence["status"] == "none"
    assert evidence["requested_calls"] == evidence["audited_calls"] == evidence["completed_calls"]
    assert evidence["requested_calls"] > 0 and not evidence["reasons"]


@pytest.mark.parametrize("dataset_kind", ["env", "env_variant", "symlink"])
def test_excluded_dataset_is_never_read_for_fixture_provenance(tmp_path, monkeypatch, dataset_kind):
    from pathlib import Path
    from evaluation.provenance import evaluation_manifest
    dataset = tmp_path / (".env.local" if dataset_kind == "env_variant" else ".env")
    if dataset_kind == "symlink":
        target = tmp_path / "ordinary.json"
        target.write_text('{"tasks": []}')
        dataset = tmp_path / "aliased.json"
        dataset.symlink_to(target)
    else:
        dataset.write_text('{"tasks": []}')
    original_read_text = Path.read_text
    def guarded_read(path, *args, **kwargs):
        if path == dataset:
            raise AssertionError("Excluded dataset must not be read")
        return original_read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    result = evaluation_manifest(repo_root=tmp_path, dataset_path=dataset, source_paths=())
    assert result["dataset_file"] == {"status": "excluded"}
    assert "fixtures" not in result and "fixtures_sha256" not in result


def test_scripted_rows_never_establish_real_cost_comparison_even_with_usage():
    a, b = row("current"), row("masking")
    for item in (a, b):
        item["execution_status"] = "completed"
        item["context_intervention"] = intervention("observed")
    cells = [cell("current", [a]), cell("masking", [b])]
    for item in cells:
        item["runner_mode"] = "scripted"
    result = summarize_cells(cells)["masking"]
    assert result["paired_vs_current"]["valid_pairs"] == 1
    assert result["paired_vs_current"]["intervention_pairs"] == 1
    assert result["paired_vs_current"]["verified_cost_pairs"] == 0
    assert result["compression_comparison_status"] == "not_established"
    assert result["total_tokens"] is None and result["usage_complete"] is False


@pytest.mark.parametrize("defect,reason", [
    ("missing_manifest", "provenance_unknown"),
    ("missing_final_manifest", "provenance_unknown"),
    ("missing_fixture", "provenance_unknown"),
    ("unavailable_source", "provenance_unknown"),
    ("source_changed_between_cells", "source_changed"),
    ("source_changed_during_cell", "source_changed"),
    ("fixture_changed", "fixture_changed"),
    ("dataset_changed", "dataset_changed"),
    ("budget_changed", "execution_configuration_changed"),
])
def test_same_model_different_source_fixture_or_budget_cannot_be_paired(defect, reason):
    cells = [cell("current", [row("current")]), cell("masking", [row("masking")])]
    target = cells[1]
    if defect == "missing_manifest":
        target.pop("manifest")
    elif defect == "missing_final_manifest":
        target.pop("manifest_after")
    elif defect == "missing_fixture":
        target["manifest"]["fixtures"] = {}
    elif defect == "unavailable_source":
        target["manifest"]["source_files"]["fixture.py"]["status"] = "unavailable"
    elif defect == "source_changed_between_cells":
        for name in ("manifest", "manifest_after"):
            target[name]["source_set_sha256"] = "e" * 64
    elif defect == "source_changed_during_cell":
        target["manifest_after"]["source_set_sha256"] = "e" * 64
    elif defect == "fixture_changed":
        target["manifest_after"]["fixtures_sha256"] = "e" * 64
    elif defect == "dataset_changed":
        target["manifest_after"]["dataset_file"]["sha256"] = "e" * 64
    else:
        target["manifest"]["configuration"]["execution_budget_limits"]["max_model_attempts"] = 99
    result = summarize_cells(cells)["masking"]
    assert result["verified_pass"] == 1
    assert result["paired_vs_current"]["valid_pairs"] == 0
    assert result["paired_vs_current"]["excluded_pairs"][reason] == 1


def test_pressure_grader_and_trial_runner_are_in_source_fingerprint():
    from evaluation.provenance import SOURCE_PATHS
    assert {"evaluation/context_pressure.py", "scripts/run_context_policy_trial.py"} <= set(SOURCE_PATHS)


def test_harness_records_before_and_after_fingerprints(tmp_path):
    result = CodingBenchmarkHarness(task_id="coding-git-diff-008", runner_mode="scripted", eval_root=tmp_path).run()
    for key in ("source_set_sha256", "fixtures_sha256", "configuration_sha256"):
        assert result["manifest"][key] == result["manifest_after"][key]
