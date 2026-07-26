from runtime.cancellation import CancellationRegistry


def test_registry_reuses_then_replaces_released_token():
    registry = CancellationRegistry()
    first = registry.register("task:1")
    assert registry.register("task:1") is first
    assert registry.request("task:1")
    assert first.requested()
    assert registry.release("task:1")
    second = registry.register("task:1")
    assert second is not first
    assert not second.requested()


def test_registry_request_is_scoped_and_missing_is_idempotent():
    registry = CancellationRegistry()
    one = registry.register("one")
    two = registry.register("two")
    assert registry.request("one")
    assert one.requested()
    assert not two.requested()
    assert not registry.request("missing")
