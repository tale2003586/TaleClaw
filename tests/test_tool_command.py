import shlex
import sys
import threading
import time

import pytest

from tools.command import run_command


def python_command(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.parametrize("command,code,status", [
    ("exit 0", 0, "success"), ("exit 9", 9, "error"),
    ("printf diagnostic >&2; exit 2", 2, "error"),
    ("printf 'Error: an example'; exit 0", 0, "success"),
])
def test_command_preserves_actual_exit(command, code, status, tmp_path):
    result = run_command(command, cwd=tmp_path)
    assert result.exit_code == code and result.status == status
    assert result.metadata["process_exited"] is True
    assert result.metadata["process_group_exited"] is True
    assert result.effect_state == "unknown"


def test_output_memory_preview_is_bounded_and_truncation_is_explicit(tmp_path):
    result = run_command(python_command("print('x' * 200000)"), cwd=tmp_path, max_output_bytes=1000)
    assert result.status == "success"
    assert len(result) == 1000
    assert result.metadata["output_truncated"] is True
    assert result.metadata["observed_output_bytes"] == 200001
    assert not result.metadata.get("output_ref")


@pytest.mark.parametrize("options,status", [
    ({"cancel_requested": lambda: True}, "cancelled"),
    ({"deadline": time.monotonic() - 1}, "timeout"),
])
def test_preflight_stop_does_not_start_command(tmp_path, options, status):
    result = run_command("touch must-not-exist", cwd=tmp_path, **options)
    assert result.status == status and result.effect_state == "not_started"
    assert not (tmp_path / "must-not-exist").exists()


def test_cancellation_callback_error_does_not_allow_execution(tmp_path):
    def unavailable():
        raise RuntimeError("cancel unavailable")
    result = run_command("touch must-not-exist", cwd=tmp_path, cancel_requested=unavailable)
    assert result.status == "error" and result.error_type == "CancellationCheckError"
    assert result.effect_state == "not_started"
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_stop_terminates_group_before_delayed_child_write(tmp_path, stop):
    delayed = python_command("import pathlib,time; time.sleep(0.5); pathlib.Path('late').write_text('bad')")
    command = f"{delayed} & wait"
    cancelled = threading.Event()
    timer = threading.Timer(0.04, cancelled.set)
    if stop == "cancel":
        timer.start()
    try:
        result = run_command(command, cwd=tmp_path, timeout_seconds=0.06 if stop == "timeout" else 2,
                             cancel_requested=cancelled.is_set)
    finally:
        if stop == "cancel":
            timer.cancel()
            timer.join()
    assert result.status == ("timeout" if stop == "timeout" else "cancelled")
    assert result.effect_state == "unknown"
    assert result.metadata["process_exited"] and result.metadata["process_group_exited"]
    time.sleep(0.55)
    assert not (tmp_path / "late").exists()


def test_detached_shell_child_is_not_reported_as_completed_success(tmp_path):
    delayed = python_command("import pathlib,time; time.sleep(0.5); pathlib.Path('late').write_text('bad')")
    result = run_command(f"{delayed} >/dev/null 2>&1 &", cwd=tmp_path)
    assert result.status == "error"
    assert result.error_type == "UnfinishedCommandChildren"
    assert result.metadata["process_group_exited"] is True
    time.sleep(0.55)
    assert not (tmp_path / "late").exists()
