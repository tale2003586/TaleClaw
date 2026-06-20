import os
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()

MODEL_HEALTHCHECK_ON_STARTUP = os.getenv(
    "LLM_HEALTHCHECK_ON_STARTUP",
    "0",
).lower() in {"1", "true", "yes"}
MODEL_HEALTHCHECK_PURPOSES = [
    item.strip()
    for item in os.getenv(
        "LLM_HEALTHCHECK_PURPOSES",
        "chat,coding,summary,hybrid",
    ).split(",")
    if item.strip()
]
REFLECTION_ENABLED = _env_bool("REFLECTION_ENABLED", False)
REFLECTION_MAX_TOKENS = int(os.getenv("REFLECTION_MAX_TOKENS", "500"))
REFLECTION_MIN_REASONING_STEPS = int(os.getenv("REFLECTION_MIN_REASONING_STEPS", "10"))
REFLECTION_INTERVAL = int(os.getenv("REFLECTION_INTERVAL", "5"))
SUBAGENT_MAX_REASONING_STEPS = int(os.getenv("SUBAGENT_MAX_REASONING_STEPS", "16"))
SUBAGENT_MAX_FANOUTS_PER_RUN = int(os.getenv("SUBAGENT_MAX_FANOUTS_PER_RUN", "4"))
SUBAGENT_MAX_FAILURES_PER_CLUE = int(os.getenv("SUBAGENT_MAX_FAILURES_PER_CLUE", "2"))
SUBAGENT_MAX_SCOPE_FILES = int(os.getenv("SUBAGENT_MAX_SCOPE_FILES", "5"))
REPO_MAP_MAX_CHARS = int(os.getenv("REPO_MAP_MAX_CHARS", "50000"))
REPO_MAP_MAX_FILE_BYTES = int(os.getenv("REPO_MAP_MAX_FILE_BYTES", "1000000"))
REPO_MAP_DEFAULT_MAX_DEPTH = int(os.getenv("REPO_MAP_DEFAULT_MAX_DEPTH", "2"))
CODE_OUTLINE_MAX_CHARS = int(os.getenv("CODE_OUTLINE_MAX_CHARS", "50000"))
CODE_OUTLINE_LARGE_FILE_LINES = int(os.getenv("CODE_OUTLINE_LARGE_FILE_LINES", "300"))
ORCHESTRATION_REPAIR_ROUNDS = int(os.getenv("ORCHESTRATION_REPAIR_ROUNDS", "1"))
REASONING_FINISHING_REMINDER_RATIO = min(
    1.0,
    max(0.0, _env_float("REASONING_FINISHING_REMINDER_RATIO", 0.7)),
)
WORKING_MEMORY_CHECKPOINT_ENABLED = _env_bool(
    "WORKING_MEMORY_CHECKPOINT_ENABLED",
    True,
)
WORKING_MEMORY_RESUME_ENABLED = _env_bool(
    "WORKING_MEMORY_RESUME_ENABLED",
    True,
)
WORKING_MEMORY_CONTEXT_BUDGET = int(os.getenv("WORKING_MEMORY_CONTEXT_BUDGET", "4000"))
WORKDIR = Path.cwd() 
WORKSPACE_ROOTS = [
    Path(item).expanduser().resolve()
    for item in os.getenv("WORKSPACE_ROOTS", str(WORKDIR)).split(os.pathsep)
    if item.strip()
]
DEFAULT_CODING_WORKSPACE = Path(
    os.getenv("DEFAULT_CODING_WORKSPACE", str(WORKDIR))
).expanduser().resolve()

SKILLS_DIR = WORKDIR / "skills"

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "task_assign",
    "task_result",
    "task_progress",
    "query",
    "response",
    "plan_request",
    "plan_response",
    "shutdown_request",
    "shutdown_response",
    "error",
    "plan_approval_response",
    "plan_approval_request",
}

def get_system_prompt() -> str:
    from skill_runtime import SKILL_LOADER

    return f"""You are a team lead at {WORKDIR}.Spawn teammates and communicate via inboxes.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{SKILL_LOADER.get_descriptions()}"""

TASKS_DIR = WORKDIR / ".tasks"
