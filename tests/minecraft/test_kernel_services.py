from runtime.services import RuntimeServices


def test_runtime_services_excludes_agent_loop_and_preserves_identity():
    marker = object()
    services = RuntimeServices(
        model_pool=marker,
        model_task_runner=marker,
        tool_registry=marker,
        tool_executor=marker,
        plugin_manager=marker,
        memory_store=marker,
        context_builder=marker,
        session_manager=marker,
        trace_store=marker,
        cancellation_registry=marker,
        message_bus=marker,
    )
    assert services.model_pool is marker
    assert not hasattr(services, "agent_loop")
