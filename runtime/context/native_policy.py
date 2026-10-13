"""Session-scoped native Responses policy; canonical windows are never sliced."""
from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import hashlib
from html import escape
import json
from typing import Any, Mapping

from models.context_state import (
    NativeContextConflict, NativeContextLineage, assistant_message_digest,
    commit_native_context, load_native_context, native_items, prepare_native_context,
)
from models.provider import NativeContextUnsupported, messages_to_native_input
from runtime.context.events import is_real_user_message
from runtime.workspace import workspace_root_for_session


def _setting(policy, name: str, default=None):
    return policy.get(name, default) if isinstance(policy, Mapping) else getattr(policy, name, default)


def _message_key(message: dict) -> str:
    return json.dumps({key: message.get(key) for key in ("role", "content", "tool_calls", "tool_call_id")},
                      ensure_ascii=False, sort_keys=True, default=str)



def _source_digest(messages) -> str:
    return hashlib.sha256(json.dumps([_message_key(item) for item in messages]).encode()).hexdigest()

def _instructions(messages, source_messages) -> str:
    source_keys = {_message_key(item) for item in source_messages}
    blocks = []
    for item in messages:
        if item.get("role") in {"system", "developer"}:
            content = item.get("content")
            blocks.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
        elif item.get("role") not in {"assistant", "tool"} and (
            not is_real_user_message(item) or _message_key(item) not in source_keys
        ):
            # Retrieval and runtime guidance are per-call context, not a new user
            # turn to accumulate in the native window. Keep their trust labels.
            blocks.append('<per-call-context instructions="false">\n' + escape(
                json.dumps(item, ensure_ascii=False, default=str), quote=False,
            ) + '\n</per-call-context>')
    return "\n\n".join(blocks)


def _update_native_visibility(session, canonical_items, source_messages) -> None:
    from runtime.context.artifact_access import update_artifact_prompt_visibility
    # An encrypted compaction item does not prove any preceding observation is
    # still visible verbatim. Only complete outputs after that boundary qualify.
    boundary = max((index for index, item in enumerate(canonical_items)
                    if item.get("type") == "compaction"), default=-1)
    visible_outputs = {(item.get("call_id"), item.get("output"))
                       for item in canonical_items[boundary + 1:]
                       if item.get("type") == "function_call_output" and isinstance(item.get("output"), str)}
    visible_messages = [item for item in source_messages if item.get("role") == "tool"
                        and isinstance(item.get("content"), str)
                        and (item.get("tool_call_id"), item["content"]) in visible_outputs]
    update_artifact_prompt_visibility(session.metadata, visible_messages)


def _resolve_provider(provider, model):
    if callable(getattr(provider, "native_context_capabilities", None)):
        return provider, model, {}
    pool = getattr(provider, "pool", None)
    purpose = getattr(provider, "purpose", None)
    if pool is not None and purpose is not None:
        profiles = pool.route_profiles_for_call(purpose)
        if not profiles:
            raise NativeContextUnsupported("No available native model profile.")
        profile = profiles[0]
        configured = pool.route_profile_names(purpose)
        selected_model = model if configured and profile.name == configured[0] and model else profile.model
        return pool.provider_for_profile(profile.name), selected_model, {
            "selected_profile": profile.name, "route_purpose": purpose,
            "native_route_locked": True,
        }
    raise NativeContextUnsupported("Selected provider exposes no native Responses adapter.")


def invoke_native_policy(*, session, provider, model: str, messages: list[dict],
                         tools: list[dict], max_tokens: int, on_text=None,
                         thinking_enabled: bool = False, hard_limit: int,
                         policy, on_thinking=None):
    """Use original session deltas plus refreshed nonpersistent instructions.

    The completed native response is committed once, after all provider checks.
    A pending assistant fingerprint lets the next call skip precisely the raw
    assistant message subsequently persisted by the host, never an arbitrary +1.
    """
    if type(hard_limit) is not int or hard_limit <= 0:
        raise ValueError("Native hard_limit must be a positive integer.")
    selected, selected_model, route = _resolve_provider(provider, model)
    capabilities = selected.native_context_capabilities()
    mode = _setting(policy, "native_mode", "server")
    if mode not in {"server", "standalone"}:
        raise ValueError("Native mode must be server or standalone.")
    required = "server_compaction" if mode == "server" else "standalone_compaction"
    if not capabilities.get(required) or not capabilities.get("input_token_count"):
        raise NativeContextUnsupported(f"Native {mode} requires {required} and exact input-token counting.")
    lineage = NativeContextLineage(
        session_id=str(session.id), model=selected_model,
        provider_key=str(capabilities["provider_key"]) + ":" + str(route.get("selected_profile", "")),
        workspace_key=str(workspace_root_for_session(session)) + ":" + str(session.metadata.get("repository") or ""),
    )
    state = load_native_context(session, lineage)
    source_messages = deepcopy(list(session.messages))
    cursor = state.event_cursor if state else 0
    if cursor > len(source_messages):
        raise NativeContextConflict("Native source cursor is beyond the current session history.")
    if state and state.source_digest and state.source_digest != _source_digest(source_messages[:cursor]):
        raise NativeContextConflict("Previously consumed native source history changed.")
    if state and state.expected_assistant_digest:
        if cursor >= len(source_messages) or assistant_message_digest(source_messages[cursor]) != state.expected_assistant_digest:
            raise NativeContextConflict("Host did not persist the expected native assistant turn; refusing to skip a source message.")
        cursor += 1
    persistent = [item for item in source_messages[cursor:]
                  if item.get("role") in {"assistant", "tool"} or is_real_user_message(item)]
    instructions = _instructions(messages, source_messages)
    # Runtime-origin user messages in the source tail may have been omitted from
    # the rendered context. Carry them once as per-call data, never as user intent.
    rendered_keys = {_message_key(item) for item in messages}
    missing_host = [item for item in source_messages[cursor:]
                    if item.get("role") in {"system", "developer", "user"}
                    and not is_real_user_message(item) and _message_key(item) not in rendered_keys]
    if missing_host:
        instructions += "\n\n" + _instructions(missing_host, [])
    transaction = prepare_native_context(
        session, lineage, new_items=messages_to_native_input(persistent), event_cursor=len(source_messages),
        source_digest=_source_digest(source_messages),
    )
    threshold = _setting(policy, "native_compact_threshold") or max(1, int(hard_limit * _setting(policy, "soft_ratio", 0.70)))
    request = session.metadata.get("context_compaction_request")
    pending = isinstance(request, dict) and request.get("status") == "pending"
    compacted = None
    try:
        should_compact = pending
        if mode == "standalone" and not should_compact:
            count = selected.count_native_input(input_items=transaction.input_items, model=selected_model,
                                                instructions=instructions, tools=tools)
            should_compact = count >= threshold
        if should_compact:
            focus = str(request.get("focus") or "") if pending else ""
            compact_instructions = instructions
            if focus:
                compact_instructions += "\nCompaction focus hint (does not amend task facts): " + json.dumps(focus)
            compacted = selected.compact_context(
                input_items=transaction.input_items, model=selected_model,
                instructions=compact_instructions, hard_limit_tokens=hard_limit,
            )
            canonical = native_items(compacted.provider_metadata["native_context"]["output_items"])
            transaction = replace(transaction, input_items=canonical)
        if on_thinking is None:
            on_thinking = getattr(on_text, "on_thinking", None)
        response = selected.native_chat(
            input_items=transaction.input_items, tools=tools, model=selected_model,
            max_tokens=max_tokens, instructions=instructions,
            compact_threshold=threshold if mode == "server" else None,
            on_text=on_text, on_thinking=on_thinking, thinking_enabled=thinking_enabled,
            hard_limit_tokens=hard_limit,
        )
        response.provider_metadata.update(route)
        response.provider_metadata["selected_model"] = selected_model
        if response.content or response.tool_calls:
            if len(session.messages) != len(source_messages) or any(
                _message_key(old) != _message_key(new) for old, new in zip(source_messages, session.messages)
            ):
                raise NativeContextConflict("Source messages changed during the native call.")
            committed = commit_native_context(session, transaction, response,
                                  expected_assistant_digest=assistant_message_digest(response.raw_message or {}))
            _update_native_visibility(session, committed.input_items, source_messages)
            if pending:
                request["status"] = "completed" if compacted else "no_change"
            response.provider_metadata["context_policy"] = "native"
            response.provider_metadata["native_mode"] = mode
            if compacted is not None:
                usage = compacted.usage
                response.provider_metadata["compaction_usage"] = {
                    key: getattr(usage, key, None) for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")
                }
        elif pending:
            request["status"] = "failed"
            request["reason"] = "empty_native_response"
        return response
    except Exception as exc:
        if pending:
            request["status"] = "failed"
            request["reason"] = type(exc).__name__
        raise
