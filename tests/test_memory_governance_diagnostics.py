from runtime.trace.summary import build_trace_summary_payload, render_trace_summary_markdown


def test_runtime_governance_diagnostics_aggregate_safe_events() -> None:
    events = [
        {"event": "memory.candidate.evaluated", "payload": {}},
        {"event": "memory.candidate.upserted", "payload": {"pending_added": 1}},
        {"event": "memory.item.created", "payload": {}},
        {"event": "memory.governance.decided", "payload": {"action": "discard"}},
        {"event": "memory.relation.candidate", "payload": {}},
        {"event": "memory.evolution.proposed", "payload": {}},
        {
            "event": "tool.governance.observed",
            "payload": {"tool_name": "memorize", "tool_scope": "kernel"},
        },
        {"event": "context.pressure.observed", "payload": {"level": "high"}},
        {
            "event": "memory.injection.explained",
            "payload": {"selected_count": 2, "filtered_count": 1},
        },
    ]
    summary = build_trace_summary_payload(
        run_state={"run_id": "run-1", "session_id": "session-1"},
        metrics={},
        report={},
        events=events,
    )
    diagnostics = summary["runtime_governance"]

    assert diagnostics == {
        "candidate_memory": 1,
        "pending_added": 1,
        "stable_writes": 1,
        "rejected_memory": 1,
        "relation_candidates": 1,
        "evolution_proposals": 1,
        "memory_policy_actions": {"discard": 1},
        "tool_scopes": {"kernel": 1},
        "kernel_tool_actions": 1,
        "context_pressure_level": "high",
        "injection_selected": 2,
        "injection_filtered": 1,
    }
    markdown = render_trace_summary_markdown(summary)
    assert "## Runtime Governance" in markdown
    assert "Kernel Tool Actions: 1" in markdown


def test_diagnostics_do_not_copy_arbitrary_event_content() -> None:
    secret = "API_KEY=should-not-be-copied"
    summary = build_trace_summary_payload(
        run_state={}, metrics={}, report={},
        events=[{"event": "memory.governance.decided", "payload": {
            "action": "discard", "content": secret,
        }}],
    )["runtime_governance"]
    assert secret not in str(summary)
