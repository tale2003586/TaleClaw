import json
import logging
import hashlib
import inspect
from urllib.parse import urlsplit
from models.context_state import native_items, native_json
from contextlib import contextmanager
from copy import deepcopy
from contextvars import ContextVar
from functools import wraps
from time import monotonic
from dataclasses import dataclass, field
from typing import Any, Callable


logger = logging.getLogger(__name__)


class ModelProtocolError(RuntimeError):
    """The model response is incomplete or cannot safely form a tool action."""


class NativeContextUnsupported(ModelProtocolError):
    """Native state was explicitly requested, but this endpoint cannot serve it."""


_ATTEMPT = ContextVar("provider_attempt", default=None)


def _bounded_model_call(method):
    @wraps(method)
    def invoke(self, **kwargs):
        from runtime.execution.budget import current_budget
        budget = current_budget()
        if budget is not None:
            budget.check()
        from runtime.execution.budget import model_request_session_id
        parent_attempt = _ATTEMPT.get()
        attempt = {"claimed": False, "usage": None,
                   "session_id": (parent_attempt or {}).get("session_id") or model_request_session_id()}
        token = _ATTEMPT.set(attempt)
        try:
            result = method(self, **kwargs)
            seen_ids = set()
            for call in result.tool_calls:
                if not isinstance(call.id, str) or not call.id.strip() or not isinstance(call.name, str) or not call.name.strip():
                    raise ModelProtocolError("Tool call requires a non-empty id and name.")
                if call.id in seen_ids:
                    raise ModelProtocolError("Tool call ids must be unique within a response.")
                seen_ids.add(call.id)
            if budget is not None:
                budget.check()
            return result
        finally:
            if attempt["claimed"] and budget is not None:
                budget.record_usage(attempt["usage"])
            _ATTEMPT.reset(token)
    return invoke


def _observe_usage(response):
    attempt = _ATTEMPT.get()
    try:
        usage = _usage_from_response(response)
    except ModelProtocolError:
        if attempt is not None:
            attempt["usage"] = None
        raise
    if usage is not None and attempt is not None:
        attempt["usage"] = usage
    return usage


def _check_running():
    from runtime.execution.budget import current_budget
    budget = current_budget()
    if budget is not None:
        budget.check()


def _invalid_json_constant(value):
    raise ValueError("non-finite JSON number")


def _tool_arguments(value) -> dict[str, Any]:
    try:
        parsed = json.loads(value, parse_constant=_invalid_json_constant) if isinstance(value, str) else value
        if not isinstance(parsed, dict):
            raise ModelProtocolError("Tool arguments must be a JSON object.")
        json.dumps(parsed, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ModelProtocolError("Tool arguments are not valid JSON.") from exc
    return parsed


def _chat_terminal(choice) -> None:
    reason = _field(choice, "finish_reason")
    if reason not in {"stop", "tool_calls"}:
        raise ModelProtocolError(f"Chat completion is not complete: finish_reason={reason!r}.")


def _responses_terminal(response) -> None:
    status = _field(response, "status")
    if status != "completed":
        raise ModelProtocolError(f"Responses completion is not complete: status={status!r}.")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] | None = None
    usage: LLMUsage | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


class OpenAICompatibleProvider:
    def __init__(
        self,
        client,
        *,
        max_tokens_param: str = "max_tokens",
        wire_api: str = "chat_completions",
        context_window_tokens: int | None = None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        output_reserve_tokens: int | None = None,
        tokenizer_model: str | None = None,
        bpe_tokenizer_enabled: bool = False,
        supports_thinking: bool = False,
        thinking_param: str = "",
        thinking_value: Any = True,
        native_compaction_supported: bool | None = None,
    ) -> None:
        self.client = client
        self.max_tokens_param = max_tokens_param or "max_tokens"
        self.wire_api = wire_api or "chat_completions"
        self.context_window_tokens = context_window_tokens
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.tokenizer_model = tokenizer_model or ""
        self.bpe_tokenizer_enabled = bool(bpe_tokenizer_enabled)
        self.supports_thinking = bool(supports_thinking)
        self.thinking_param = str(thinking_param or "")
        self.thinking_value = thinking_value
        self.native_compaction_supported = native_compaction_supported

    def native_context_identity(self) -> str:
        # Endpoint/account identity is part of the lane; pooled clients never own
        # conversation state. Only a digest is persisted, never credentials.
        identity = [self.wire_api, str(getattr(self.client, "base_url", "")),
                    str(getattr(self.client, "organization", "")),
                    str(getattr(self.client, "project", "")),
                    str(getattr(self.client, "api_key", ""))]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def native_context_capabilities(self) -> dict[str, Any]:
        responses = getattr(self.client, "responses", None)
        declared = self.native_compaction_supported
        if declared is None:
            declared = urlsplit(str(getattr(self.client, "base_url", ""))).hostname == "api.openai.com"
        create = getattr(responses, "create", None)
        compact = getattr(responses, "compact", None)
        enabled = self.wire_api == "responses" and bool(declared)
        return {
            "responses": enabled and callable(create),
            "server_compaction": enabled and _accepts_keyword(create, "context_management"),
            "standalone_compaction": enabled and callable(compact),
            "input_token_count": enabled and callable(getattr(getattr(responses, "input_tokens", None), "count", None)),
            "provider_key": self.native_context_identity(),
            "reason": "Native Responses requires an official endpoint or explicit capability declaration; remote support is validated by the API call.",
        }

    @_bounded_model_call
    def native_chat(
        self, *, input_items: list[dict], tools: list[dict], model: str, max_tokens: int,
        instructions: str | None = None, compact_threshold: int | None = None,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        tool_choice: str = "auto", thinking_enabled: bool = False,
        hard_limit_tokens: int | None = None,
    ) -> LLMResponse:
        capabilities = self.native_context_capabilities()
        if not capabilities["responses"]:
            raise NativeContextUnsupported(capabilities["reason"])
        if compact_threshold is not None and not capabilities["server_compaction"]:
            raise NativeContextUnsupported("This SDK/endpoint does not expose server compaction.")
        if compact_threshold is not None and (isinstance(compact_threshold, bool) or not isinstance(compact_threshold, int) or compact_threshold <= 0):
            raise ValueError("compact_threshold must be a positive integer.")
        try:
            items = native_items(input_items)
        except ValueError as exc:
            raise ModelProtocolError(str(exc)) from exc
        options = {"store": False, "include": ["reasoning.encrypted_content"], "truncation": "disabled"}
        if instructions is not None:
            options["instructions"] = instructions
        if compact_threshold is not None:
            options["context_management"] = [{"type": "compaction", "compact_threshold": compact_threshold}]
        kwargs = dict(messages=[], tools=tools, model=model, max_tokens=max_tokens,
                      tool_choice=tool_choice, thinking_enabled=thinking_enabled,
                      native_input=items, native_options=options, hard_limit_tokens=hard_limit_tokens)
        try:
            if on_text is not None:
                return self._responses_stream_chat(**kwargs, on_text=on_text, on_thinking=on_thinking)
            return self._responses_chat(**kwargs)
        except Exception as exc:
            if _native_support_error(exc, "server_compaction"):
                raise NativeContextUnsupported("Endpoint rejected native server compaction support.") from exc
            raise

    @_bounded_model_call
    def compact_context(self, *, input_items: list[dict], model: str,
                        instructions: str | None = None,
                        hard_limit_tokens: int | None = None) -> LLMResponse:
        if not self.native_context_capabilities()["standalone_compaction"]:
            raise NativeContextUnsupported("This SDK/endpoint does not expose responses.compact.")
        try:
            items = native_items(input_items)
        except ValueError as exc:
            raise ModelProtocolError(str(exc)) from exc
        request = {"model": model, "input": items}
        if instructions is not None:
            request["instructions"] = instructions
        input_count = self._native_input_count(request)
        response = self._create("responses_compact", request, input_tokens=input_count,
                                hard_limit_tokens=hard_limit_tokens)
        _observe_usage(response)
        if _field(response, "object") != "response.compaction":
            raise ModelProtocolError("responses.compact did not return a canonical compaction window.")
        metadata = _native_response_metadata(response, model=model, mode="replace", input_count=input_count)
        if not any(item.get("type") == "compaction" for item in metadata["output_items"]):
            raise ModelProtocolError("responses.compact returned no opaque compaction item.")
        return LLMResponse(content=None, usage=_usage_from_response(response),
                           provider_metadata={"native_context": metadata})

    def _native_input_count(self, request: dict) -> int | None:
        if not self.native_context_capabilities()["input_token_count"]:
            raise NativeContextUnsupported("Native requests require the input-token-count endpoint; local text estimates cannot validate opaque state.")
        return self._count_native_input(request=request).provider_metadata["input_tokens"]

    def count_native_input(self, *, input_items: list[dict], model: str,
                           instructions: str | None = None, tools: list[dict] = ()) -> int:
        request = {"input": native_items(input_items), "model": model}
        if instructions is not None:
            request["instructions"] = instructions
        converted = _tools_to_responses_tools(tools)
        if converted:
            request["tools"] = converted
        return self._native_input_count(request)

    @_bounded_model_call
    def _count_native_input(self, *, request: dict) -> LLMResponse:
        # This additional API request consumes an attempt too. Its count is NOT
        # billed generation usage; absent usage remains explicitly unknown.
        keys = {"model", "input", "instructions", "tools", "tool_choice", "reasoning", "text", "truncation"}
        response = self._transport("responses_count", {key: value for key, value in request.items() if key in keys})
        count = _field(response, "input_tokens")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ModelProtocolError("Input-token-count endpoint returned an invalid count.")
        return LLMResponse(content=None, provider_metadata={"input_tokens": count, "operation": "input_token_count"})

    @_bounded_model_call
    def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        tool_choice: str = "auto",
        thinking_enabled: bool = False,
    ) -> LLMResponse:
        if self.wire_api == "responses":
            return self._responses_chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                thinking_enabled=thinking_enabled,
            )

        request = {
            "model": model,
            "messages": _sanitize_chat_messages(messages),
            self.max_tokens_param: max_tokens,
        }
        if thinking_enabled and self.supports_thinking and self.thinking_param:
            request[self.thinking_param] = self.thinking_value
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice
        response = self._create("chat", request)
        _observe_usage(response)
        choices = _protocol_list(_required_field(response, "choices"), "choices")
        if not choices:
            raise ModelProtocolError("Chat completion returned no choices.")
        _chat_terminal(choices[0])
        message = _required_field(choices[0], "message")
        content = _optional_text_field(_required_field(message, "content"), "message.content")
        tool_calls: list[ToolCall] = []
        raw_calls = []
        calls = _required_field(message, "tool_calls")
        for call in _protocol_list(calls, "message.tool_calls", allow_none=True):
            function = _required_field(call, "function")
            arguments = _tool_arguments(_required_field(function, "arguments"))
            call_id = _required_field(call, "id")
            name = _required_field(function, "name")
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
            raw_calls.append({"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
            }})
        reasoning = _optional_text_field(_field(message, "reasoning_content"), "message.reasoning_content")
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw_message=_chat_raw_message(content, raw_calls, reasoning_content=reasoning),
            usage=_usage_from_response(response),
        )

    @_bounded_model_call
    def stream_chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        on_text: Callable[[str], None],
        on_thinking: Callable[[str], None] | None = None,
        tool_choice: str = "auto",
        thinking_enabled: bool = False,
    ) -> LLMResponse:
        if self.wire_api == "responses":
            return self._responses_stream_chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                on_text=on_text,
                on_thinking=on_thinking,
                tool_choice=tool_choice,
                thinking_enabled=thinking_enabled,
            )

        request = {
            "model": model,
            "messages": _sanitize_chat_messages(messages),
            self.max_tokens_param: max_tokens,
            "stream": True,
        }
        if thinking_enabled and self.supports_thinking and self.thinking_param:
            request[self.thinking_param] = self.thinking_value
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        streamed_tool_calls: dict[int, dict[str, str]] = {}
        terminal_choice = None
        stream_usage = None
        request["stream_options"] = {"include_usage": True}
        raw_stream = self._create("chat", request)
        with _owned_stream(raw_stream) as stream:
            for chunk in _checked_stream(stream):
                stream_usage = _observe_usage(chunk) or stream_usage
                choices = _protocol_list(_required_field(chunk, "choices"), "stream.choices")
                if not choices:
                    continue  # A final usage-only chunk may follow the terminal.
                if terminal_choice is not None:
                    raise ModelProtocolError("Chat stream emitted a choice after its terminal event.")
                reason = _field(choices[0], "finish_reason")
                if reason is not None:
                    _chat_terminal(choices[0])
                delta = _required_field(choices[0], "delta")
                reasoning = _optional_text_field(_field(delta, "reasoning_content"), "delta.reasoning_content")
                if reasoning:
                    reasoning_text = str(reasoning)
                    reasoning_parts.append(reasoning_text)
                    if on_thinking is not None:
                        on_thinking(reasoning_text)
                text = _optional_text_field(_field(delta, "content"), "delta.content")
                if text:
                    content_parts.append(text)
                    on_text(text)
                for call in _protocol_list(_field(delta, "tool_calls"), "delta.tool_calls", allow_none=True):
                    item = streamed_tool_calls.setdefault(
                        _stream_index(_required_field(call, "index")),
                        {"id": "", "name": "", "arguments": ""},
                    )
                    item["id"] += _optional_text_field(_field(call, "id"), "delta.tool_call.id")
                    function = _field(call, "function")
                    if function is not None:
                        item["name"] += _optional_text_field(_field(function, "name"), "delta.function.name")
                        item["arguments"] += _optional_text_field(_field(function, "arguments"), "delta.function.arguments")
                # A terminal chunk may contain the last legal delta, but later
                # choice chunks can never append actions to that completed turn.
                if reason is not None:
                    terminal_choice = choices[0]

        if terminal_choice is None:
            raise ModelProtocolError("Chat stream ended without a terminal finish_reason.")
        content = "".join(content_parts)
        tool_calls = []
        raw_tool_calls = []
        for index in sorted(streamed_tool_calls):
            item = streamed_tool_calls[index]
            arguments = _tool_arguments(item["arguments"])
            tool_calls.append(ToolCall(
                id=item["id"],
                name=item["name"],
                arguments=arguments,
            ))
            raw_tool_calls.append({
                "id": item["id"],
                "type": "function",
                "function": {
                    "name": item["name"],
                    "arguments": item["arguments"],
                },
            })

        raw_message = _chat_raw_message(
            content,
            raw_tool_calls,
            reasoning_content="".join(reasoning_parts),
        )
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw_message=raw_message,
            usage=stream_usage,
        )

    def _responses_chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        tool_choice: str = "auto",
        thinking_enabled: bool = False,
        native_input: list[dict] | None = None,
        native_options: dict | None = None,
        hard_limit_tokens: int | None = None,
    ) -> LLMResponse:
        request = {
            "model": model,
            "input": native_input if native_input is not None else _messages_to_responses_input(messages),
            "max_output_tokens": max_tokens,
        }
        if native_options:
            request.update(native_options)
        if thinking_enabled and self.supports_thinking and self.thinking_param:
            request[self.thinking_param] = self.thinking_value
        response_tools = _tools_to_responses_tools(tools)
        if response_tools:
            request["tools"] = response_tools
            request["tool_choice"] = tool_choice

        input_count = self._native_input_count(request) if native_input is not None else None
        response = self._create("responses", request, input_tokens=input_count, hard_limit_tokens=hard_limit_tokens)
        _observe_usage(response)
        _responses_terminal(response)
        content = _responses_text(response)
        tool_calls = _responses_tool_calls(response)
        raw_message = _responses_raw_message(content, tool_calls)
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw_message=raw_message,
            usage=_usage_from_response(response),
            provider_metadata=({"native_context": _native_response_metadata(response, model=model, mode="append", input_count=input_count)} if native_input is not None else {}),
        )

    def _responses_stream_chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        on_text: Callable[[str], None],
        on_thinking: Callable[[str], None] | None = None,
        tool_choice: str = "auto",
        thinking_enabled: bool = False,
        native_input: list[dict] | None = None,
        native_options: dict | None = None,
        hard_limit_tokens: int | None = None,
    ) -> LLMResponse:
        request = {
            "model": model,
            "input": native_input if native_input is not None else _messages_to_responses_input(messages),
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        if native_options:
            request.update(native_options)
        if thinking_enabled and self.supports_thinking and self.thinking_param:
            request[self.thinking_param] = self.thinking_value
        response_tools = _tools_to_responses_tools(tools)
        if response_tools:
            request["tools"] = response_tools
            request["tool_choice"] = tool_choice

        content_parts: list[str] = []
        streamed_calls: dict[int, dict[str, str]] = {}
        final_response = None
        streamed_native_items = {}

        input_count = self._native_input_count(request) if native_input is not None else None
        raw_stream = self._create("responses", request, input_tokens=input_count, hard_limit_tokens=hard_limit_tokens)
        with _owned_stream(raw_stream) as stream:
            for event in _checked_stream(stream):
                event_type = _optional_text_field(_required_field(event, "type"), "event.type")
                if final_response is not None:
                    raise ModelProtocolError("Responses stream emitted data after response.completed.")
                if event_type == "response.output_text.delta":
                    delta = str(_field(event, "delta", "") or "")
                    if delta:
                        content_parts.append(delta)
                        on_text(delta)
                    continue

                if event_type in {
                    "response.output_item.added",
                    "response.output_item.done",
                }:
                    item = _field(event, "item")
                    if native_input is not None and event_type == "response.output_item.done":
                        streamed_native_items[_stream_index(_required_field(event, "output_index"))] = native_items([item])[0]
                    if _field(item, "type") == "function_call":
                        output_index = _stream_index(_required_field(event, "output_index"))
                        call = streamed_calls.setdefault(
                            output_index,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        call["id"] = str(
                            _field(item, "call_id", "")
                            or _field(item, "id", "")
                            or call["id"]
                        )
                        call["name"] = str(_field(item, "name", "") or call["name"])
                        item_arguments = _field(item, "arguments", None)
                        if item_arguments not in (None, ""):
                            call["arguments"] = str(item_arguments)
                    continue

                if event_type == "response.function_call_arguments.delta":
                    output_index = _stream_index(_required_field(event, "output_index"))
                    call = streamed_calls.setdefault(
                        output_index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    call["arguments"] += str(_field(event, "delta", "") or "")
                    continue

                if event_type == "response.function_call_arguments.done":
                    output_index = _stream_index(_required_field(event, "output_index"))
                    call = streamed_calls.setdefault(
                        output_index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    call["name"] = str(_field(event, "name", "") or call["name"])
                    arguments = _field(event, "arguments", None)
                    if arguments is not None:
                        call["arguments"] = str(arguments)
                    continue

                if event_type in {"response.completed", "response.incomplete"}:
                    final_response = _field(event, "response")
                    _observe_usage(final_response)
                    if event_type == "response.incomplete":
                        raise ModelProtocolError("Responses stream reported response.incomplete.")
                    _responses_terminal(final_response)
                    continue

                if event_type in {"error", "response.failed"}:
                    response = _field(event, "response")
                    _observe_usage(response)
                    error = _field(event, "error") or _field(response, "error")
                    message = str(
                        _field(error, "message", "")
                        or _field(event, "message", "")
                        or "Responses API stream failed"
                    )
                    raise ModelProtocolError(message)

        if final_response is None:
            raise ModelProtocolError("Responses stream ended without response.completed.")
        streamed_content = "".join(content_parts)
        content = _responses_text(final_response) if final_response is not None else ""
        if not content:
            content = streamed_content
        # Only the completed object's output is authoritative for executable
        # tools. Intermediate fragments cannot restore a tool omitted there.
        tool_calls = _responses_tool_calls(final_response)
        _validate_streamed_response_calls(streamed_calls, final_response)
        if native_input is not None:
            final_items = native_items(_response_output(final_response))
            for index, item in streamed_native_items.items():
                if index >= len(final_items) or item != final_items[index]:
                    raise ModelProtocolError("Streamed native item conflicts with the completed response window.")
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw_message=_responses_raw_message(content, tool_calls),
            usage=_usage_from_response(final_response),
            provider_metadata=({"native_context": _native_response_metadata(final_response, model=model, mode="append", input_count=input_count)} if native_input is not None else {}),
        )


    def _create(self, api: str, request: dict, *, input_tokens: int | None = None, hard_limit_tokens: int | None = None):
        from runtime.context.dynamic_budget import (
            PromptBudgetExceeded, calculate_dynamic_prompt_budget,
        )
        from runtime.execution.budget import current_budget
        from runtime.token_estimator import estimate_tokens

        output = int(request.get("max_output_tokens", request.get(self.max_tokens_param, 0)))
        prompt = [{"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)}]
        messages = request.get("messages", request.get("input", []))
        budget_info = calculate_dynamic_prompt_budget(
            provider=self,
            system_messages=[item for item in messages if item.get("role") in {"system", "developer"}],
            tools=request.get("tools", []), reserved_output_tokens=output,
        )
        actual = input_tokens if input_tokens is not None else estimate_tokens(prompt, provider=self)
        hard_limit = min(
            budget_info.model_context_window - output - budget_info.safety_margin_tokens,
            self.max_input_tokens or budget_info.model_context_window,
        )
        if hard_limit_tokens is not None:
            hard_limit = min(hard_limit, max(1, int(hard_limit_tokens)))
        if (output <= 0 and api != "responses_compact") or (self.max_output_tokens and output > self.max_output_tokens) or actual > hard_limit:
            raise PromptBudgetExceeded(
                actual_tokens=actual, hard_limit_tokens=hard_limit, budget=budget_info,
            )
        return self._transport(api, request)

    def _transport(self, api: str, request: dict):
        from runtime.execution.budget import current_budget
        root = current_budget()
        timeout = 60.0
        if root is not None:
            root.check()
            timeout = min(timeout, max(0.001, root.deadline - monotonic()))
        options = getattr(self.client, "with_options", None)
        client = options(max_retries=0, timeout=timeout) if callable(options) else self.client
        if api == "responses_compact":
            create = client.responses.compact
        elif api == "responses_count":
            create = client.responses.input_tokens.count
        else:
            create = client.responses.create if api == "responses" else client.chat.completions.create
        if root is not None:
            root.claim("model")
        attempt = _ATTEMPT.get()
        if attempt is not None:
            attempt["claimed"] = True
        # OpenCode Go permits other agents but requires their own identity and
        # one stable session header per conversation. Never mutate pooled clients
        # or attach this service-specific header to other endpoints.
        if urlsplit(str(getattr(self.client, "base_url", ""))).hostname == "opencode.ai":
            from runtime.execution.budget import model_request_session_id
            headers = {key: value for key, value in (request.get("extra_headers") or {}).items()
                       if key.lower() not in {"user-agent", "x-opencode-session"}}
            headers.update({"User-Agent": "TaleClaw",
                            "x-opencode-session": (attempt or {}).get("session_id") or model_request_session_id()})
            request = {**request, "extra_headers": headers}
        try:
            return create(**request)
        except Exception as exc:
            if api in {"responses_count", "responses_compact"} and _native_support_error(exc, api):
                raise NativeContextUnsupported(f"Endpoint does not support {api}.") from exc
            raise


def _native_support_error(exc, operation: str) -> bool:
    """Only explicit unsupported protocol signals select a configured fallback.

    Authentication, rate limits, timeouts and generic server errors keep their
    original meaning; they are not evidence that native context is unavailable.
    """
    status = getattr(exc, "status_code", None)
    if operation in {"responses_count", "responses_compact"} and status in {404, 405, 501}:
        return True
    body = getattr(exc, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(details, dict):
        details = {}
    code = str(getattr(exc, "code", None) or details.get("code") or "")
    parameter = str(getattr(exc, "param", None) or details.get("param") or "")
    return status in {400, 422} and code in {"unsupported_parameter", "unsupported_value", "not_supported"} and (
        operation != "server_compaction" or parameter.startswith("context_management")
    )


def _accepts_keyword(function, keyword: str) -> bool:
    if not callable(function):
        return False
    try:
        parameters = inspect.signature(function).parameters
        return keyword in parameters or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    except (TypeError, ValueError):
        return False


def _native_response_metadata(response, *, model: str, mode: str, input_count: int | None) -> dict:
    try:
        items = native_items(_response_output(response))
    except ValueError as exc:
        raise ModelProtocolError(str(exc)) from exc
    return {"mode": mode, "output_items": items, "model": model,
            "response_id": str(_field(response, "id", "") or ""),
            "input_tokens_counted": input_count, "opaque_state": any("encrypted_content" in item for item in items)}


def messages_to_native_input(messages: list[dict]) -> list[dict[str, Any]]:
    """Adapt ordinary messages, preserving raw Responses items when available."""
    items = []
    for message in messages:
        native = (message.get("metadata") or {}).get("native_context") if isinstance(message.get("metadata"), dict) else None
        if isinstance(native, dict) and "output_items" in native:
            items.extend(native_items(native["output_items"]))
        elif message.get("type") and (message.get("type") != "message" or "role" in message):
            items.extend(native_items([message]))
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ModelProtocolError("Native tool output requires a tool_call_id.")
            content = message.get("content")
            items.append({"type": "function_call_output", "call_id": call_id,
                          "output": content if isinstance(content, str) else json.dumps(native_json(content), ensure_ascii=False)})
        else:
            role = message.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise ModelProtocolError("Native message has an unsupported role.")
            content = message.get("content")
            if content is not None and content != "":
                items.append({"role": role, "content": native_json(content)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if not call.get("id") or not function.get("name"):
                    raise ModelProtocolError("Native function call requires an id and name.")
                args = function.get("arguments")
                _tool_arguments(args)
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": function["name"], "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})
    return native_items(items)


def _checked_stream(stream):
    iterator = iter(stream)
    while True:
        # A callback may request cancellation after the previous event. Check
        # before another potentially blocking read, as well as after it returns.
        _check_running()
        try:
            event = next(iterator)
        except StopIteration:
            return
        _check_running()
        yield event


@contextmanager
def _owned_stream(stream):
    try:
        yield stream
    except BaseException as primary_error:
        try:
            _close_stream(stream)
        except BaseException:
            logger.exception(
                "Model stream cleanup failed after the primary error: %s",
                primary_error,
            )
        raise
    else:
        _close_stream(stream)


def _close_stream(stream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _messages_to_responses_input(messages: list[dict]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "") or "").strip()
        content = _message_content_text(message)

        if role == "tool":
            call_id = str(message.get("tool_call_id", "") or "").strip()
            if call_id:
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": content,
                })
            continue

        if role:
            if content or role in {"system", "developer", "user"}:
                items.append({
                    "role": role,
                    "content": content,
                })

        for raw_call in message.get("tool_calls", []) or []:
            function = raw_call.get("function", {}) if isinstance(raw_call, dict) else {}
            call_id = str(raw_call.get("id", "") if isinstance(raw_call, dict) else "").strip()
            name = str(function.get("name", "") or "").strip()
            arguments = function.get("arguments", "{}")
            if not call_id or not name:
                continue
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, default=str)
            items.append({
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            })
    return items


def _sanitize_chat_messages(messages: list[dict]) -> list[dict[str, Any]]:
    sanitized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "") or "").strip()
        if role == "assistant" and _is_empty_assistant_message(message):
            continue
        sanitized.append(message)
    return sanitized


def _is_empty_assistant_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if tool_calls or message.get("reasoning_content"):
        return False
    if isinstance(content, str):
        return content == ""
    if isinstance(content, list):
        return len(content) == 0
    return content is None


def _chat_raw_message(
    content: str,
    raw_tool_calls: list[dict[str, Any]],
    *,
    reasoning_content: str = "",
) -> dict[str, Any] | None:
    if not content and not raw_tool_calls and not reasoning_content:
        return None
    raw_message: dict[str, Any] = {"role": "assistant"}
    if content:
        raw_message["content"] = content
    if raw_tool_calls:
        raw_message["tool_calls"] = raw_tool_calls
    if reasoning_content:
        raw_message["reasoning_content"] = reasoning_content
    return raw_message


def _usage_from_response(response: Any) -> LLMUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def get(*names: str) -> int | None:
        for name in names:
            if isinstance(usage, dict):
                value = usage.get(name)
            else:
                value = getattr(usage, name, None)
            if value is not None:
                try:
                    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                except (TypeError, ValueError):
                    return None
        return None

    input_tokens = get("prompt_tokens", "input_tokens")
    output_tokens = get("completion_tokens", "output_tokens")
    total_tokens = get("total_tokens")
    known_parts = [part for part in (input_tokens, output_tokens) if part is not None]
    if total_tokens is not None and (
        total_tokens < sum(known_parts)
        or (len(known_parts) == 2 and total_tokens != sum(known_parts))
    ):
        # No exact usage can be inferred from mutually inconsistent counters.
        # Stop the attempt; its finally records unknown usage exactly once.
        raise ModelProtocolError("Model usage counters are inconsistent.")
    # This is an optional provider-reported subset, not a discount to token budgets.
    details = _field(usage, "prompt_tokens_details")
    if details is None:
        details = _field(usage, "input_tokens_details")
    cached_input_tokens = _field(details, "cached_tokens")
    if (
        not isinstance(cached_input_tokens, int)
        or isinstance(cached_input_tokens, bool)
        or cached_input_tokens < 0
        or (input_tokens is not None and cached_input_tokens > input_tokens)
        or (total_tokens is not None and cached_input_tokens > total_tokens)
    ):
        cached_input_tokens = None
    return LLMUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def _message_content_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            elif hasattr(block, "text"):
                parts.append(str(block.text or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _tools_to_responses_tools(tools: list[dict]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools or []:
        if tool.get("type") != "function":
            converted.append(tool)
            continue
        # Chat tools nest the function; Responses also accepts this flat form.
        # Responses may normalize an omitted strict flag into required fields.
        # Keep the original optional-argument contract unless strict=True was
        # explicitly requested; never rewrite schemas or synthesize null values.
        function = tool.get("function", tool)
        if not isinstance(function, dict):
            continue
        strict = function.get("strict")
        if strict is not None and type(strict) is not bool:
            raise ValueError("Function tool strict must be a boolean or None.")
        converted_tool = ({"type": "function"} if "function" in tool else deepcopy(tool))
        converted_tool.update({
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": deepcopy(function.get("parameters", {"type": "object", "properties": {}})),
            "strict": strict if strict is not None else False,
        })
        converted.append(converted_tool)
    return converted


def _responses_text(response) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text

    parts: list[str] = []
    for item in _response_output(response):
        item_type = _field(item, "type")
        if item_type != "message":
            continue
        for block in _field(item, "content", []) or []:
            block_type = _field(block, "type")
            if block_type in {"output_text", "text"}:
                parts.append(str(_field(block, "text", "") or ""))
    return "".join(parts)


def _responses_tool_calls(response) -> list[ToolCall]:
    calls = []
    for item in _response_output(response):
        if _field(item, "type") != "function_call":
            continue
        call_id = str(_field(item, "call_id", "") or _field(item, "id", "") or "")
        name = str(_field(item, "name", "") or "")
        arguments = _tool_arguments(_field(item, "arguments", None))
        calls.append(ToolCall(
            id=call_id,
            name=name,
            arguments=arguments,
        ))
    return calls


def _validate_streamed_response_calls(streamed_calls, response) -> None:
    output = _response_output(response)
    for index, observed in streamed_calls.items():
        if index >= len(output) or _field(output[index], "type") != "function_call":
            raise ModelProtocolError("Streamed tool call is absent from completed response output.")
        item = output[index]
        final_id = _field(item, "call_id") or _field(item, "id")
        if ((observed["id"] and observed["id"] != final_id)
                or (observed["name"] and observed["name"] != _field(item, "name"))
                or (observed["arguments"] and _tool_arguments(observed["arguments"])
                    != _tool_arguments(_field(item, "arguments")))):
            raise ModelProtocolError("Streamed tool call conflicts with completed response output.")


def _responses_raw_message(content: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
    raw_message: dict[str, Any] = {
        "role": "assistant",
        "content": content or None,
    }
    if tool_calls:
        raw_message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in tool_calls
        ]
    return raw_message


def _response_output(response) -> list:
    return _protocol_list(_required_field(response, "output"), "response.output")


def _required_field(item, name):
    missing = object()
    value = _field(item, name, missing)
    if value is missing:
        raise ModelProtocolError(f"Model response is missing required field: {name}.")
    return value


def _protocol_list(value, name, *, allow_none=False):
    if value is None and allow_none:
        return []
    if not isinstance(value, (list, tuple)):
        raise ModelProtocolError(f"Model response field {name} must be an array.")
    return value


def _optional_text_field(value, name):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelProtocolError(f"Model response field {name} must be text or null.")
    return value


def _stream_index(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ModelProtocolError("Stream tool output index must be a nonnegative integer.")
    return value


def _field(item, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)
