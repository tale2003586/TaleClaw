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
# Legacy knobs remain readable for one migration cycle. They no longer select
# the authoritative context path or define the final model-call limit.
CODING_CONTEXT_STATE_ENABLED = _env_bool("CODING_CONTEXT_STATE_ENABLED", True)
CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS = int(
    os.getenv("CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS", "12000")
)
CODING_CONTEXT_COMPACTION_TARGET_TOKENS = int(
    os.getenv("CODING_CONTEXT_COMPACTION_TARGET_TOKENS", "8000")
)
CODING_CONTEXT_RECENT_GROUPS = int(os.getenv("CODING_CONTEXT_RECENT_GROUPS", "4"))
TASK_STATE_CONTEXT_ENABLED = _env_bool("TASK_STATE_CONTEXT_ENABLED", True)
SEMANTIC_COMPACTION_ENABLED = _env_bool("SEMANTIC_COMPACTION_ENABLED", True)
ARTIFACT_OFFLOADING_ENABLED = _env_bool("ARTIFACT_OFFLOADING_ENABLED", True)
DYNAMIC_PROMPT_BUDGET_ENABLED = _env_bool("DYNAMIC_PROMPT_BUDGET_ENABLED", True)
PROMPT_SOFT_COMPACTION_RATIO = _env_float("PROMPT_SOFT_COMPACTION_RATIO", 0.70)
PROMPT_COMPACTION_TARGET_RATIO = _env_float("PROMPT_COMPACTION_TARGET_RATIO", 0.45)
PROMPT_HARD_INPUT_RATIO = _env_float("PROMPT_HARD_INPUT_RATIO", 0.92)
PROMPT_SAFETY_MARGIN_TOKENS = int(os.getenv("PROMPT_SAFETY_MARGIN_TOKENS", "0"))
LONG_CONTENT_MAX_TOKENS = int(os.getenv("LONG_CONTENT_MAX_TOKENS", "4000"))
LONG_CONTENT_MAX_CHARS = int(os.getenv("LONG_CONTENT_MAX_CHARS", "20000"))
LONG_CONTENT_MAX_BYTES = int(os.getenv("LONG_CONTENT_MAX_BYTES", "64000"))
CONTEXT_PRESSURE_OBSERVATION_ENABLED = _env_bool(
    "CONTEXT_PRESSURE_OBSERVATION_ENABLED",
    False,
)
CONTEXT_PRESSURE_WINDOW_TOKENS = int(
    os.getenv("CONTEXT_PRESSURE_WINDOW_TOKENS", "128000")
)
MEMORY_PENDING_ENRICHMENT_ENABLED = _env_bool(
    "MEMORY_PENDING_ENRICHMENT_ENABLED",
    False,
)
MEMORY_INJECTION_TRACE_ENABLED = _env_bool(
    "MEMORY_INJECTION_TRACE_ENABLED",
    False,
)
WORKDIR = Path.cwd() 
WORKSPACE_ROOTS = [
    Path(item).expanduser().resolve()
    for item in os.getenv("WORKSPACE_ROOTS", str(WORKDIR)).split(os.pathsep)
    if item.strip()
]
DEFAULT_CODING_WORKSPACE = Path(
    os.getenv("DEFAULT_CODING_WORKSPACE", str(WORKDIR))
).expanduser().resolve()
CONTEXT_ARTIFACT_ROOT = Path(
    os.getenv("CONTEXT_ARTIFACT_ROOT", str(WORKDIR / ".coding_applications" / "artifacts"))
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
