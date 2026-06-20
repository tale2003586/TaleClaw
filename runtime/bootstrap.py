import os

from bus.user_bus import MessageBus
from coding_runtime.teammate import TEAM
from config import MODEL_HEALTHCHECK_PURPOSES, SUBAGENT_MAX_REASONING_STEPS, WORKDIR
from models.model_pool import build_model_pool_from_env
from runtime.agent_loop import AgentLoop
from runtime.context import ContextBuilder
from runtime.agent_spec import AgentSpec
from models.model_task_runner import ModelTaskRunner
from runtime.pipeline import Pipeline
from runtime.reflection import ReflectionAgent
from runtime.app_runtime import AppRuntime
from runtime.env_loader import load_dotenv_file
from runtime.background_memory import BackgroundMemoryLifecycle
from runtime.trace.trace_store import TraceStore
from tools.hooks import FileWriteScopeHook, ToolLoopGuardHook, ToolTraceHook
from tools.executor import ToolExecutor
from tools.handlers import cleanup_expired_sandboxes, configure_subagent_runner
from tools.tool_registry import build_lead_tool_registry
from runtime.routing.router import ModeRouter
from modes.hybrid_classifier import HybridModeClassifier
from sessions import SessionManager
from agents.coding.runner import TaskSessionRunner
from agents.subagent.runner import TaskSubagentRunner
from memory.archive_store import MemoryArchiveStore
from memory.history_summary import HistorySummarizer
from memory.lifecycle import MemoryLifecycle
from memory.processor import CandidateMemoryExtractor, MemoryProcessingDevice
from memory.store import MemoryStore
from memory.scoped_store import ScopedMemoryStore
from memory.vector_runtime import (
    build_history_vector_index_from_env,
    history_vector_scope_for_session,
)
from knowledge.security_rag import (
    build_security_embedding_provider_from_env,
    build_security_index_from_env,
)
from retrieval import (
    build_security_route_classifier_from_env,
    build_security_retrieval_router_from_env,
)
from plugins import PluginManager
from plugins.shell_safety import ShellSafetyPlugin
from plugins.status_commands import StatusCommandsPlugin
from plugins.web_search import WebSearchPlugin
from plugins.markdown_pdf import MarkdownPdfPlugin
from plugins.run_report import RunReportPlugin
from plugins.security_rag import SecurityRagPlugin


_MODEL_POOL = None
_MODEL_HEALTHCHECK_RESULTS = None
_ENV_INITIALIZED = False


def initialize_runtime_environment() -> None:
    global _ENV_INITIALIZED
    if _ENV_INITIALIZED:
        return
    load_dotenv_file(WORKDIR / ".env", override=True)
    _configure_proxy_from_env()
    _ENV_INITIALIZED = True


def get_model_pool():
    global _MODEL_POOL
    initialize_runtime_environment()
    if _MODEL_POOL is None:
        _MODEL_POOL = build_model_pool_from_env()
    return _MODEL_POOL


def get_model_healthcheck_results() -> list[dict]:
    global _MODEL_HEALTHCHECK_RESULTS
    initialize_runtime_environment()
    if _MODEL_HEALTHCHECK_RESULTS is None:
        model_pool = get_model_pool()
        purposes = [
            item.strip()
            for item in os.getenv(
                "LLM_HEALTHCHECK_PURPOSES",
                ",".join(MODEL_HEALTHCHECK_PURPOSES),
            ).split(",")
            if item.strip()
        ]
        _MODEL_HEALTHCHECK_RESULTS = (
            model_pool.health_check_purposes(purposes)
            if _env_bool("LLM_HEALTHCHECK_ON_STARTUP", False)
            else []
        )
    return list(_MODEL_HEALTHCHECK_RESULTS)


def build_runtime() -> AppRuntime:
    initialize_runtime_environment()
    model_pool = get_model_pool()
    model = model_pool.model_for("chat")
    cleanup_expired_sandboxes()
    bus = MessageBus()
    sessions = SessionManager()
    trace_store = TraceStore()
    tools = build_lead_tool_registry(TEAM)

    provider = model_pool.routed_provider("chat")
    router = ModeRouter(
        hybrid_classifier=HybridModeClassifier(
            provider=model_pool.routed_provider("hybrid"),
            model=model_pool.model_for("hybrid"),
        ),
    )

    memory_store = ScopedMemoryStore(WORKDIR, legacy_store=MemoryStore())
    memory_archive_store = MemoryArchiveStore()
    history_vector_index = build_history_vector_index_from_env()
    security_retrieval_router = None
    security_route_classifier = None
    security_knowledge_index = None
    if _env_bool("SECURITY_RAG_AUTO_CONTEXT_ENABLED", True):
        try:
            security_embeddings = build_security_embedding_provider_from_env()
            security_retrieval_router = build_security_retrieval_router_from_env(
                embeddings=security_embeddings,
            )
            security_route_classifier = build_security_route_classifier_from_env(
                config=security_retrieval_router.config,
                model_pool=model_pool,
            )
            security_knowledge_index = build_security_index_from_env(
                embeddings=security_embeddings,
            )
        except Exception:
            security_retrieval_router = None
            security_route_classifier = None
            security_knowledge_index = None
    context_builder = ContextBuilder(
        memory_store=memory_store,
        history_vector_index=history_vector_index,
        history_scope_resolver=history_vector_scope_for_session,
        retrieval_top_k=_env_int("HISTORY_RETRIEVAL_TOP_K", 6),
        retrieval_min_score=_env_float("HISTORY_RETRIEVAL_MIN_SCORE", 0.35),
        security_retrieval_router=security_retrieval_router,
        security_route_classifier=security_route_classifier,
        security_knowledge_index=security_knowledge_index,
        security_auto_context_enabled=_env_bool("SECURITY_RAG_AUTO_CONTEXT_ENABLED", True),
    )
    model_task_runner = ModelTaskRunner(
        model_pool=model_pool,
        default_max_tokens=800,
    )
    memory_processor = MemoryProcessingDevice(
        history_vector_index=history_vector_index,
        scope_resolver=history_vector_scope_for_session,
        similar_top_k=_env_int("MEMORY_CANDIDATE_SIMILAR_TOP_K", 8),
        similar_min_score=_env_float("MEMORY_CANDIDATE_SIMILAR_MIN_SCORE", 0.55),
        similar_min_hits=_env_int("MEMORY_CANDIDATE_SIMILAR_MIN_HITS", 2),
        extractor=CandidateMemoryExtractor(
            runner=model_task_runner,
            spec=AgentSpec(
                name="candidate_memory_extractor",
                profile=None,
                model_purpose="summary",
                max_tokens=_env_int("MEMORY_CANDIDATE_EXTRACT_MAX_TOKENS", 220),
            ),
            max_tokens=_env_int("MEMORY_CANDIDATE_EXTRACT_MAX_TOKENS", 220),
        ),
    )
    memory_lifecycle = MemoryLifecycle(
        memory_store,
        summarizer=HistorySummarizer(
            runner=model_task_runner,
            spec=AgentSpec(
                name="history_summarizer",
                profile=None,
                model_purpose="summary",
                max_tokens=220,
            ),
        ),
        archive_store=memory_archive_store,
        history_vector_index=history_vector_index,
        memory_processor=memory_processor,
        scope_resolver=history_vector_scope_for_session,
        promotion_confidence=_env_float("MEMORY_CANDIDATE_PROMOTION_CONFIDENCE", 0.85),
        promotion_evidence_count=_env_int("MEMORY_CANDIDATE_PROMOTION_EVIDENCE_COUNT", 3),
    )
    if _env_bool("MEMORY_LIFECYCLE_BACKGROUND", True):
        memory_lifecycle = BackgroundMemoryLifecycle(
            memory_lifecycle,
            max_workers=_env_int("MEMORY_LIFECYCLE_BACKGROUND_WORKERS", 1),
        )

    plugin_manager = PluginManager(
        [
            ShellSafetyPlugin(),
            StatusCommandsPlugin(),
            WebSearchPlugin(),
            SecurityRagPlugin(),
            MarkdownPdfPlugin(),
            RunReportPlugin(),
        ],
        workspace=WORKDIR,
        tool_registry=tools,
        sessions=sessions,
        memory_store=memory_store,
    )

    executor = ToolExecutor([
        FileWriteScopeHook(),
        #ToolLoopGuardHook(),
        ToolTraceHook(),
        *plugin_manager.tool_hooks,
    ])
    reflection_agent = None
    if _env_bool("REFLECTION_ENABLED", False):
        reflection_agent = ReflectionAgent(
            provider=model_pool.routed_provider("reflection"),
            model=model_pool.model_for("reflection"),
            max_tokens=_env_int("REFLECTION_MAX_TOKENS", 500),
            min_reasoning_steps=_env_int("REFLECTION_MIN_REASONING_STEPS", 10),
            reflection_interval=_env_int("REFLECTION_INTERVAL", 5),
        )

    pipeline = Pipeline(
        tools=tools,
        provider=provider,
        model=model,
        model_pool=model_pool,
        max_tokens=8000,
        context_builder=context_builder,
        memory_lifecycle=memory_lifecycle,
        tool_executor=executor,
        reflection_agent=reflection_agent,
    )
    TEAM.configure(
        model_pool=model_pool,
        tool_executor=executor,
        reflection_agent=reflection_agent,
        max_tokens=pipeline.max_tokens,
        max_reasoning_steps=50,
    )

    task_session_runner = TaskSessionRunner(
        sessions=sessions,
        base_pipeline=pipeline,
        global_memory=memory_store,
    )
    subagent_runner = TaskSubagentRunner(
        base_pipeline=pipeline,
        max_reasoning_steps=_env_int(
            "SUBAGENT_MAX_REASONING_STEPS",
            SUBAGENT_MAX_REASONING_STEPS,
        ),
    )
    configure_subagent_runner(subagent_runner)

    loop = AgentLoop(
        bus,
        sessions,
        pipeline,
        router,
        plugin_manager,
        task_session_runner,
        subagent_runner,
        trace_store,
    )

    return AppRuntime(
        bus=bus,
        loop=loop,
    )


def _configure_proxy_from_env() -> None:
    use_local_proxy = os.getenv("USE_LOCAL_PROXY", "1").lower() not in {"0", "false", "no"}
    if not use_local_proxy:
        return
    proxy_url = os.getenv("LOCAL_PROXY_URL", "http://127.0.0.1:7897")
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        os.environ[key] = proxy_url


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}
