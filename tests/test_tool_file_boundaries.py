"""File tools must not escape their workspace or block on named pipes."""
import multiprocessing
import os
import shutil
from types import SimpleNamespace

import pytest

from tools import handlers


def session_for(root):
    return SimpleNamespace(id="file-boundaries", metadata={"workspace_root": str(root)})


@pytest.mark.parametrize("recursive", [False, True])
def test_grep_skips_external_file_symlink_while_searching_workspace(tmp_path, recursive):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("needle external-private-data", encoding="utf-8")
    (workspace / "external.txt").symlink_to(outside)
    (workspace / "ordinary.txt").write_text("needle 普通文本", encoding="utf-8")

    result = handlers.run_grep("needle", path=".", recursive=recursive,
                               literal=True, _session=session_for(workspace))

    assert "ordinary.txt:1:needle 普通文本" in result
    assert "external-private-data" not in result
    assert "external.txt" not in result


@pytest.mark.parametrize("search", ["grep", "rg"])
def test_search_does_not_follow_external_directory_symlinks(tmp_path, search):
    if search == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not available")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("needle external-private-data", encoding="utf-8")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    (workspace / "ordinary.txt").write_text("needle ordinary", encoding="utf-8")
    handler = handlers.run_grep if search == "grep" else handlers.run_rg

    result = handler("needle", path=".", literal=True, _session=session_for(workspace))

    assert "ordinary.txt" in result
    assert "external-private-data" not in result
    denied = handler("needle", path="linked", literal=True, _session=session_for(workspace))
    assert denied.status == "error"
    assert "Path escapes workspace" in denied


def test_grep_retains_internal_file_symlink_and_glob_behavior(tmp_path):
    (tmp_path / "source.data").write_text("needle 中文", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to("source.data")
    result = handlers.run_grep("needle", glob="*.txt", _session=session_for(tmp_path))
    assert "alias.txt:1:needle 中文" in result


def _invoke_file_tool(connection, tool_name, workspace):
    handlers_by_name = {
        "read_file": lambda: handlers.run_read("named-pipe", _session=session_for(workspace)),
        "write_file": lambda: handlers.run_write("named-pipe", "new", _session=session_for(workspace)),
        "edit_file": lambda: handlers.run_edit("named-pipe", "old", "new", _session=session_for(workspace)),
    }
    connection.send("started")
    try:
        result = handlers_by_name[tool_name]()
        connection.send((str(result), getattr(result, "status", None),
                         getattr(result, "effect_state", None)))
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "posix", reason="Named-pipe regression requires POSIX")
@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "edit_file"])
def test_file_tools_reject_fifo_before_open_without_blocking(tmp_path, tool_name):
    os.mkfifo(tmp_path / "named-pipe")
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_invoke_file_tool, args=(sender, tool_name, tmp_path))
    process.start()
    sender.close()
    try:
        assert receiver.poll(5), "File tool worker did not start"
        assert receiver.recv() == "started"
        assert receiver.poll(1), f"{tool_name} blocked while opening a FIFO"
        output, status, effects = receiver.recv()
        assert status == "error"
        assert "not a regular file" in output
        assert effects == "not_started"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        receiver.close()
    assert not process.is_alive()


def test_regular_utf8_file_read_write_edit_and_binary_error_are_preserved(tmp_path):
    session = session_for(tmp_path)
    written = handlers.run_write("nested/notes.txt", "中文 old\nsecond", _session=session)
    assert written.status == "success"
    edited = handlers.run_edit("nested/notes.txt", "old", "新内容", _session=session)
    assert edited.status == "success"
    output = handlers.run_read("nested/notes.txt", offset=0, limit=1, _session=session)
    assert "中文 新内容" in output
    assert "second" not in output
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00\x80")
    failed = handlers.run_read("binary.bin", _session=session)
    assert failed.status == "error"
    assert failed.error_type == "UnicodeDecodeError"
