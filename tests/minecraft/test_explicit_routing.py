from types import SimpleNamespace

from agents.minecraft import MINECRAFT_AGENT_SPEC
from runtime.routing.agent_router import AgentRouter


class Session:
    def __init__(self):
        self.active_agent = "hybrid"
        self.metadata = {}

    def selected_agent(self):
        return self.active_agent

    def set_mode(self, mode):
        self.active_agent = mode


def test_explicit_minecraft_command_routes_to_minecraft_agent():
    route = AgentRouter().route(Session(), "/minecraft 收集 4 个原木")
    assert route.agent_spec is MINECRAFT_AGENT_SPEC
    assert route.intent == "minecraft"


def test_minecraft_question_remains_chat():
    route = AgentRouter().route(Session(), "Minecraft 里的钻石是什么？")
    assert route.agent_spec.name == "bot"


def test_application_mode_metadata_routes_explicitly():
    session = Session()
    session.metadata["application_mode"] = "minecraft"
    route = AgentRouter().route(session, "收集 4 个原木")
    assert route.agent_spec is MINECRAFT_AGENT_SPEC
