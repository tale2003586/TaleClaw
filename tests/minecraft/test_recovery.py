from applications.minecraft.models import BridgeActionType
from applications.minecraft.recovery import RecoveryController


def test_known_recovery_is_bounded_without_model():
    controller = RecoveryController()
    first = controller.decide("path_failed", attempts=0, limit=2)
    assert first.retryable
    assert first.action is BridgeActionType.RETURN_SAFE
    exhausted = controller.decide("path_failed", attempts=2, limit=2)
    assert not exhausted.retryable
    assert exhausted.reason_code == "recovery_exhausted"


def test_unknown_failure_is_not_retried():
    decision = RecoveryController().decide("server_rejected", attempts=0, limit=2)
    assert not decision.retryable
    assert decision.action is None
