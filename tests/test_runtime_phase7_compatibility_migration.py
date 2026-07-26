from agents.definitions import BOT_AGENT_SPEC
from runtime.routing.execution_plan import ExecutionPlanner
from runtime.sessions import Session


def test_session_uses_agent_identity_as_single_source_of_truth():
    session = Session(id="phase7:session")

    session.set_mode("coding")

    assert session.active_agent == "coding"
    assert session.selected_agent() == "coding"


def test_routing_uses_explicit_agent_identity():
    session = Session(
        id="phase7:routing",
        active_agent="bot",
    )

    plan = ExecutionPlanner().plan(None, session)

    assert plan.agent_spec is BOT_AGENT_SPEC
