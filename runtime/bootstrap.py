import os

from runtime.messaging.user_bus import MessageBus
from applications.coding.orchestration.teammate import TEAM
from config import MODEL_HEALTHCHECK_PURPOSES, SUBAGENT_MAX_REASONING_STEPS, WORKDIR
from models.model_pool import build_model_pool_from_env
from runtime.agent_loop import AgentLoop
from runtime.context import ContextBuilder, ContextMemoryService, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from runtime.context.retrieval import ContextRetrievalService
from runtime.working_memory import render_working_memory_block
from skill_runtime import SKILL_LOADER
from applications.coding.context_state import build_coding_context_view
from runtime.agent_spec import AgentSpec
from models.model_task_runner import ModelTaskRunner
from runtime.runtime import Runtime
from runtime.execution.reflection import ReflectionAgent
from runtime.execution.loop_policies import standard_execution_policies
from runtime.app_runtime import AppRuntime
from runtime.env_loader import load_dotenv_file
from memory.background_lifecycle import BackgroundMemoryLifecycle
from runtime.trace.trace_store import TraceStore
from tools.hooks import (
    FileWriteScopeHook,
    ToolLoopGuardHook,
    ToolResultStoreHook,
    ToolTraceHook,
)
from tools.executor import ToolExecutor
from tools.handlers import (
    cleanup_expired_sandboxes,
    configure_semantic_memory_services,
    configure_subagent_runner,
)
from tools.tool_registry import build_lead_tool_registry
from runtime.routing.agent_router import AgentRouter
from runtime.routing.hybrid_classifier import HybridModeClassifier
from runtime.sessions import SessionManager
from applications.coding.runner import CodingApplication
from agents.subagent.runner import TaskSubagentRunner
from memory.archive_store import MemoryArchiveStore
from memory.history_summary import HistorySummarizer
from memory.lifecycle import MemoryLifecycle
from memory.processor import CandidateMemoryExtractor, MemoryProcessingDevice
from memory.command_service import MemoryCommandService
from memory.index_sync import MemoryIndexSynchronizer
from memory.postgres_repository import PostgresMemoryRepository
from memory.promotion_service import MemoryPromotionService
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from memory.store import MemoryStore
from memory.scoped_store import ScopedMemoryStore
from memory.vector_runtime import (
    build_history_vector_index_from_env,
    build_semantic_memory_index_from_env,
    history_vector_scope_for_session,
)
from plugins import PluginManager
from runtime.cancellation import CancellationRegistry
from runtime.services import RuntimeServices
from plugins.shell_safety import ShellSafetyPlugin
from plugins.status_commands import StatusCommandsPlugin
from plugins.web_search import WebSearchPlugin
from plugins.markdown_pdf import MarkdownPdfPlugin
from plugins.run_report import RunReportPlugin


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
    cancellation_registry = CancellationRegistry()
    tools = build_lead_tool_registry(TEAM)

    provider = model_pool.routed_provider("chat")
    router = AgentRouter(
        hybrid_classifier=HybridModeClassifier(
            provider=model_pool.routed_provider("hybrid"),
            model=model_pool.model_for("hybrid"),
        ),
    )

    memory_store = ScopedMemoryStore(WORKDIR, legacy_store=MemoryStore())
    memory_archive_store = MemoryArchiveStore()
    history_vector_index = (
        build_history_vector_index_from_env()
        if _history_vector_enabled()
        else None
    )
    semantic_memory_repository = None
    semantic_memory_command_service = None
    semantic_memory_retrieval_service = None
    semantic_memory_index_synchronizer = None
    semantic_memory_promotion_service = None
    semantic_flags_enabled = _env_bool("SEMANTIC_MEMORY_ENABLED", False) or any(
        _env_bool(name, False)
        for name in (
            "SEMANTIC_MEMORY_WRITE_ENABLED",
            "SEMANTIC_MEMORY_READ_ENABLED",
            "SEMANTIC_MEMORY_CONTEXT_ENABLED",
        )
    )
    if semantic_flags_enabled:
        semantic_memory_repository = PostgresMemoryRepository()
        semantic_memory_index = build_semantic_memory_index_from_env()
        semantic_memory_command_service = MemoryCommandService(
            semantic_memory_repository,
        )
        semantic_memory_retrieval_service = SemanticMemoryRetrievalService(
            semantic_memory_repository,
            semantic_memory_index,
            top_k=_env_int("SEMANTIC_MEMORY_RETRIEVAL_TOP_K", 8),
        )
        semantic_memory_index_synchronizer = MemoryIndexSynchronizer(
            semantic_memory_repository,
            semantic_memory_index,
        )
        semantic_memory_promotion_service = MemoryPromotionService(
            semantic_memory_command_service,
            semantic_memory_repository,
            min_confidence=_env_float("MEMORY_CANDIDATE_PROMOTION_CONFIDENCE", 0.85),
            min_independent_evidence=_env_int(
                "MEMORY_CANDIDATE_PROMOTION_EVIDENCE_COUNT",
                2,
            ),
        )
    configure_semantic_memory_services(
        command_service=(
            semantic_memory_command_service
            if _env_bool("SEMANTIC_MEMORY_WRITE_ENABLED", False)
            else None
        ),
        retrieval_service=(
            semantic_memory_retrieval_service
            if _env_bool("SEMANTIC_MEMORY_READ_ENABLED", False)
            else None
        ),
        index_synchronizer=semantic_memory_index_synchronizer,
    )
    security_retrieval_router = None
    security_route_classifier = None
    security_knowledge_index = None
    security_auto_context_enabled = _security_rag_auto_context_enabled()
    if security_auto_context_enabled:
        try:
            from knowledge.security_rag import (
                build_security_embedding_provider_from_env,
                build_security_index_from_env,
            )
            from retrieval import (
                build_security_route_classifier_from_env,
                build_security_retrieval_router_from_env,
            )

            security_embeddings = build_security_embedding_provider_from_env()
            security_retrieval_router = build_security_retrieval_router_from_env(
                embeddings=security_embeddings,
                model_pool=model_pool,
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
    context_budgeter = ContextBudgeter.from_env()
    context_builder = ContextBuilder(
        budgeter=context_budgeter,
        prompt_assets_service=PromptAssetsService(
            budgeter=context_budgeter,
            skill_loader=SKILL_LOADER,
        ),
        memory_service=ContextMemoryService(
            memory_store=memory_store,
            working_memory_renderer=render_working_memory_block,
            semantic_memory_retriever=(
                semantic_memory_retrieval_service
                if _env_bool("SEMANTIC_MEMORY_CONTEXT_ENABLED", False)
                else None
            ),
        ),
        retrieval_service=ContextRetrievalService(
            history_vector_index=history_vector_index,
            history_scope_resolver=history_vector_scope_for_session,
            retrieval_top_k=_env_int("HISTORY_RETRIEVAL_TOP_K", 6),
            retrieval_min_score=_env_float("HISTORY_RETRIEVAL_MIN_SCORE", 0.35),
            security_retrieval_router=security_retrieval_router,
            security_route_classifier=security_route_classifier,
            security_knowledge_index=security_knowledge_index,
            security_auto_context_enabled=security_auto_context_enabled,
        ),
        coding_context_view_builder=build_coding_context_view,
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
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
        command_service=(
            semantic_memory_command_service
            if _env_bool("SEMANTIC_MEMORY_WRITE_ENABLED", False)
            else None
        ),
        promotion_service=(
            semantic_memory_promotion_service
            if _env_bool("SEMANTIC_MEMORY_WRITE_ENABLED", False)
            else None
        ),
        write_legacy_history_files=_env_bool(
            "MEMORY_LEGACY_HISTORY_FILES_ENABLED",
            False,
        ),
    )
    if _env_bool("MEMORY_LIFECYCLE_BACKGROUND", True):
        memory_lifecycle = BackgroundMemoryLifecycle(
            memory_lifecycle,
            max_workers=_env_int("MEMORY_LIFECYCLE_BACKGROUND_WORKERS", 1),
        )

    plugins = [
        ShellSafetyPlugin(),
        StatusCommandsPlugin(),
        WebSearchPlugin(),
        MarkdownPdfPlugin(),
        RunReportPlugin(),
    ]
    if _security_rag_plugin_enabled():
        from plugins.security_rag import SecurityRagPlugin

        plugins.insert(3, SecurityRagPlugin())

    plugin_manager = PluginManager(
        plugins,
        workspace=WORKDIR,
        tool_registry=tools,
        sessions=sessions,
        memory_store=memory_store,
    )

    executor_hooks = [FileWriteScopeHook()]
    if _tool_loop_guard_enabled():
        executor_hooks.append(ToolLoopGuardHook())
    executor_hooks.extend([
        ToolResultStoreHook(),
        ToolTraceHook(),
        *plugin_manager.tool_hooks,
    ])
    executor = ToolExecutor(executor_hooks)
    reflection_agent = None
    if _env_bool("REFLECTION_ENABLED", False):
        reflection_agent = ReflectionAgent(
            provider=model_pool.routed_provider("reflection"),
            model=model_pool.model_for("reflection"),
            max_tokens=_env_int("REFLECTION_MAX_TOKENS", 500),
            min_reasoning_steps=_env_int("REFLECTION_MIN_REASONING_STEPS", 10),
            reflection_interval=_env_int("REFLECTION_INTERVAL", 5),
        )

    pipeline = Runtime(
        tools=tools,
        provider=provider,
        model=model,
        model_pool=model_pool,
        max_tokens=130000,
        context_builder=context_builder,
        memory_lifecycle=memory_lifecycle,
        tool_executor=executor,
        reflection_agent=reflection_agent,
        execution_policy_factory=standard_execution_policies,
    )
    TEAM.configure(
        model_pool=model_pool,
        tool_executor=executor,
        reflection_agent=reflection_agent,
        max_tokens=pipeline.max_tokens,
        max_reasoning_steps=50,
    )

    coding_application = CodingApplication(
        sessions=sessions,
        base_pipeline=pipeline,
        global_memory=memory_store,
        semantic_memory_command_service=(
            semantic_memory_command_service
            if _env_bool("SEMANTIC_MEMORY_WRITE_ENABLED", False)
            else None
        ),
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
        coding_application=coding_application,
        subagent_runner=subagent_runner,
        trace_store=trace_store,
        cancellation_registry=cancellation_registry,
    )

    services = RuntimeServices(
        model_pool=model_pool,
        model_task_runner=model_task_runner,
        tool_registry=tools,
        tool_executor=executor,
        plugin_manager=plugin_manager,
        memory_store=memory_store,
        context_builder=context_builder,
        session_manager=sessions,
        trace_store=trace_store,
        cancellation_registry=cancellation_registry,
        message_bus=bus,
        semantic_memory_repository=semantic_memory_repository,
        semantic_memory_command_service=semantic_memory_command_service,
        semantic_memory_retrieval_service=semantic_memory_retrieval_service,
        semantic_memory_index_synchronizer=semantic_memory_index_synchronizer,
    )
    return AppRuntime(
        bus=bus,
        loop=loop,
        services=services,
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


def _env_bool_any(names: list[str], *, default: bool) -> bool:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default)


def _rag_enabled() -> bool:
    return _env_bool("RAG_ENABLED", True)


def _history_vector_enabled() -> bool:
    return _rag_enabled() and _env_bool_any(
        ["HISTORY_VECTOR_ENABLED", "MEMORY_VECTOR_ENABLED"],
        default=True,
    )


def _security_rag_auto_context_enabled() -> bool:
    return _rag_enabled() and _env_bool("SECURITY_RAG_AUTO_CONTEXT_ENABLED", True)


def _security_rag_plugin_enabled() -> bool:
    return _rag_enabled() and _env_bool("SECURITY_RAG_PLUGIN_ENABLED", True)


def _tool_loop_guard_enabled() -> bool:
    return _env_bool("TOOL_LOOP_GUARD_ENABLED", True)
