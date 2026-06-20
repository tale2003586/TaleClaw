from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
import traceback

from bus import AgentMessage, MessageType, render_agent_message
from bus.team_bus import BUS
from config import WORKDIR
from coding_runtime.protocols import PROTOCOLS
from runtime.agent_runner import AgentRunner
from runtime.agent_spec import AgentSpec
from runtime.context import ContextBundle
from coding_runtime.task import TASKS
from modes.base import ModeProfile
from sessions import Session
from tools.executor import ToolExecutor
from tools.hooks import FileWriteScopeHook, ToolLoopGuardHook, ToolTraceHook
from tools.tool_registry import build_teammate_tool_registry


@dataclass
class TeammateCycleState:
    should_idle: bool = False
    should_shutdown: bool = False


class TeammateContextBuilder:
    def __init__(self, name: str) -> None:
        self.name = name

    def build(self, *, session, profile) -> ContextBundle:
        inbox = BUS.read_inbox(self.name)
        for msg in inbox:
            PROTOCOLS.notify_message(msg)
            session.messages.append({
                "role": "user",
                "content": _render_inbound_message(msg),
                "metadata": {"kind": "teammate_inbox"},
            })
        return ContextBundle(messages=[
            {"role": "system", "content": profile.system_prompt},
            *session.messages,
        ])


class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}
        self._clear_stale_working_members()
        self.idle_timeout = 60  # seconds to wait in idle state before checking for new tasks
        self.poll_interval = 10  # seconds to check for new tasks while idle
        self.model_pool = None
        self.provider = None
        self.model = ""
        self.tool_executor = ToolExecutor([
            FileWriteScopeHook(),
            ToolLoopGuardHook(),
            ToolTraceHook(),
        ])
        self.reflection_agent = None
        self.max_tokens = 8000
        self.max_reasoning_steps = 50

    def _ensure_model_defaults(self) -> None:
        if self.provider is not None and self.model:
            return
        from runtime.bootstrap import get_model_pool

        self.model_pool = get_model_pool()
        self.provider = self.model_pool.routed_provider("teammate")
        self.model = self.model_pool.model_for("teammate")

    def configure(
        self,
        *,
        provider=None,
        model: str | None = None,
        model_pool=None,
        tool_executor=None,
        reflection_agent=None,
        max_tokens: int | None = None,
        max_reasoning_steps: int | None = None,
    ) -> None:
        if model_pool is not None:
            self.model_pool = model_pool
            self.provider = model_pool.routed_provider("teammate")
            self.model = model_pool.model_for("teammate")
        elif provider is not None:
            self.model_pool = None
            self.provider = provider
            if model:
                self.model = model
        elif model is not None:
            self.model = model
        if tool_executor is not None:
            self.tool_executor = tool_executor
        if reflection_agent is not None:
            self.reflection_agent = reflection_agent
        if max_tokens is not None:
            self.max_tokens = max(1, int(max_tokens))
        if max_reasoning_steps is not None:
            self.max_reasoning_steps = max(1, int(max_reasoning_steps))

    def _load_config(self):
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None
    
    def _set_status(self, name: str, status: str):
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def _clear_stale_working_members(self):
        changed = False
        for member in self.config["members"]:
            if member.get("status") == "working":
                member["status"] = "idle"
                changed = True
        if changed:
            self._save_config()
    
    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            if member["status"] not in {"idle", "shutdown"}:
                return f"Error: Member '{name}' already active"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(target=self._run_member, 
                                  args=(name, role, prompt), daemon=True)
        self.threads[name] = thread
        thread.start()
        return f"Teammate '{name}' spawned with role '{role}'"
    
    def _run_member(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        profile = self._profile_for(name, role, team_name)
        spec = self._agent_spec(name=name, role=role, profile=profile)
        session = self._new_session(name, role, prompt)
        tools = build_teammate_tool_registry(name)
        context_builder = TeammateContextBuilder(name)
        while True:
            try:
                state = self._run_reasoning_cycle(
                    name=name,
                    session=session,
                    spec=spec,
                    tools=tools,
                    context_builder=context_builder,
                )
                if state.should_shutdown:
                    self._set_status(name, "shutdown")
                    return
            except Exception as e:
                print(f"[{name}] Error: {e}")
                self._send_error_to_lead(name, e)
                self._set_status(name, "error")
                break

            #进入空闲状态，需要检测是否有新任务
            self._set_status(name, "idle")
            resume = False
            polls = self.idle_timeout // max(self.poll_interval, 1)

            for _ in range(polls):
                time.sleep(self.poll_interval)
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        PROTOCOLS.notify_message(msg)
                        session.messages.append({
                            "role": "user",
                            "content": _render_inbound_message(msg),
                            "metadata": {"kind": "teammate_inbox"},
                        })
                    resume = True
                    break
                unclaimed_tasks = TASKS.scan_unclaimed_tasks()
                if unclaimed_tasks:
                    task = unclaimed_tasks[0]
                    result = TASKS.claim_task(task["id"], name)
                    if result.startswith("Error"):
                        print(f"[{name}] Failed to claim task #{task['id']}: {result}")
                        continue
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    if len(session.messages) <= 3:
                        session.messages.insert(0, TASKS.make_identity_block(name, role, team_name))
                        session.messages.insert(1, {
                            "role": "assistant",
                            "content": f"I am {name}. Continuing.",
                        })
                    session.messages.append({"role": "user", "content": task_prompt})
                    session.messages.append({
                        "role": "assistant",
                        "content": f"Claimed task #{task['id']}. Working on it.",
                    })
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def _new_session(self, name: str, role: str, prompt: str) -> Session:
        session = Session(
            id=f"teammate:{name}",
            current_mode="teammate",
            metadata={
                "kind": "teammate",
                "teammate_name": name,
                "teammate_role": role,
                "user_role": "admin",
            },
        )
        session.add_message("user", _render_initial_prompt(prompt))
        return session

    def _profile_for(self, name: str, role: str, team_name: str) -> ModeProfile:
        return ModeProfile(
            name=f"teammate:{name}",
            tool_mode="teammate",
            system_prompt=(
                f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
                "Use idle when you have no more work in the current cycle. "
                "You will auto-claim new task-board items while idle. "
                "Some high-risk tools are deferred; use tool_search with "
                "query='select:<tool_name>' before calling a hidden tool."
            ),
        )

    def _run_reasoning_cycle(
        self,
        *,
        name: str,
        session: Session,
        spec: AgentSpec,
        tools,
        context_builder: TeammateContextBuilder,
    ) -> TeammateCycleState:
        self._ensure_model_defaults()
        state = TeammateCycleState()
        runner = AgentRunner(
            tools=tools,
            tool_executor=self.tool_executor,
            provider=self.provider,
            model=self.model,
            model_pool=self.model_pool,
            reflection_agent=self.reflection_agent,
            max_tokens=self.max_tokens,
            max_reasoning_steps=self.max_reasoning_steps,
        )
        runner.reset_turn_state(session)
        runner.run_turn(
            session=session,
            spec=spec,
            build_context=lambda session, profile: context_builder.build(
                session=session,
                profile=profile,
            ),
            after_turn=lambda session: session.touch(),
            after_tool_calls=lambda _session, _response, execution: (
                self._after_teammate_tool_calls(name, state, execution)
            ),
        )
        return state

    def _agent_spec(self, *, name: str, role: str, profile: ModeProfile) -> AgentSpec:
        return AgentSpec(
            name=f"teammate:{name}",
            role=role,
            profile=profile,
            model_purpose="teammate",
            max_tokens=self.max_tokens,
            max_reasoning_steps=self.max_reasoning_steps,
        )

    def _after_teammate_tool_calls(
        self,
        name: str,
        state: TeammateCycleState,
        execution,
    ) -> bool:
        for item in execution.tool_results:
            tool_name = item.get("name", "")
            output = str(item.get("output", ""))
            if tool_name == "shutdown_response" and output.startswith("Approved"):
                state.should_shutdown = True
            if tool_name == "idle":
                state.should_idle = True
            print(f"[{name}] {tool_name}: {output[:120]}")
        return state.should_shutdown or state.should_idle

    def _send_error_to_lead(self, name: str, exc: Exception) -> None:
        message = AgentMessage(
            sender=name,
            recipient="lead",
            type=MessageType.ERROR,
            payload={
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
        BUS.send(
            message.sender,
            message.recipient,
            message.to_json(),
            message.type.value,
            {
                "id": message.id,
                "recipient": message.recipient,
                "payload": message.payload,
                "ttl_seconds": message.ttl_seconds,
            },
        )
                            

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)
    
    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(WORKDIR / ".team")


def _render_initial_prompt(prompt: str) -> str:
    try:
        return render_agent_message(prompt)
    except Exception:
        return prompt


def _render_inbound_message(message) -> str:
    try:
        return render_agent_message(message)
    except Exception:
        return json.dumps(message, ensure_ascii=False)
