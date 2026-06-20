import json
from dataclasses import dataclass, field
from typing import Any, Callable


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
    ) -> None:
        self.client = client
        self.max_tokens_param = max_tokens_param or "max_tokens"
        self.wire_api = wire_api or "chat_completions"

    def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        if self.wire_api == "responses":
            return self._responses_chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
            )

        request = {
            "model": model,
            "messages": _sanitize_chat_messages(messages),
            self.max_tokens_param: max_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice
        response = self.client.chat.completions.create(
            **request,
        )

        message = response.choices[0].message
        tool_calls: list[ToolCall] = []

        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except Exception:
                arguments = {}

            tool_calls.append(ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=arguments,
            ))

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            raw_message=message.model_dump(exclude_none=True),
            usage=_usage_from_response(response),
        )

    def stream_chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        on_text: Callable[[str], None],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        if self.wire_api == "responses":
            response = self._responses_chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
            )
            if response.content:
                on_text(response.content)
            return response

        request = {
            "model": model,
            "messages": _sanitize_chat_messages(messages),
            self.max_tokens_param: max_tokens,
            "stream": True,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice

        content_parts: list[str] = []
        streamed_tool_calls: dict[int, dict[str, str]] = {}
        for chunk in self.client.chat.completions.create(**request):
            choices = getattr(chunk, "choices", [])
            if not choices:
                continue
            delta = choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                on_text(text)
            for call in getattr(delta, "tool_calls", None) or []:
                item = streamed_tool_calls.setdefault(
                    call.index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if getattr(call, "id", None):
                    item["id"] += call.id
                function = getattr(call, "function", None)
                if function is not None:
                    item["name"] += getattr(function, "name", "") or ""
                    item["arguments"] += getattr(function, "arguments", "") or ""

        content = "".join(content_parts)
        tool_calls = []
        raw_tool_calls = []
        for index in sorted(streamed_tool_calls):
            item = streamed_tool_calls[index]
            try:
                arguments = json.loads(item["arguments"])
            except Exception:
                arguments = {}
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

        raw_message = _chat_raw_message(content, raw_tool_calls)
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw_message=raw_message,
        )

    def _responses_chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        request = {
            "model": model,
            "input": _messages_to_responses_input(messages),
            "max_output_tokens": max_tokens,
        }
        response_tools = _tools_to_responses_tools(tools)
        if response_tools:
            request["tools"] = response_tools
            request["tool_choice"] = tool_choice

        response = self.client.responses.create(**request)
        content = _responses_text(response)
        tool_calls = _responses_tool_calls(response)
        raw_message = _responses_raw_message(content, tool_calls)
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw_message=raw_message,
            usage=_usage_from_response(response),
        )


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
    if tool_calls:
        return False
    if isinstance(content, str):
        return content == ""
    if isinstance(content, list):
        return len(content) == 0
    return content is None


def _chat_raw_message(
    content: str,
    raw_tool_calls: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not content and not raw_tool_calls:
        return None
    raw_message: dict[str, Any] = {"role": "assistant"}
    if content:
        raw_message["content"] = content
    if raw_tool_calls:
        raw_message["tool_calls"] = raw_tool_calls
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
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    return LLMUsage(
        input_tokens=get("prompt_tokens", "input_tokens"),
        output_tokens=get("completion_tokens", "output_tokens"),
        total_tokens=get("total_tokens"),
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
        function = tool.get("function", {})
        if not isinstance(function, dict):
            continue
        converted.append({
            "type": "function",
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        })
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
        raw_arguments = _field(item, "arguments", "{}") or "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except Exception:
                arguments = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {}
        calls.append(ToolCall(
            id=call_id,
            name=name,
            arguments=arguments,
        ))
    return calls


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
    output = getattr(response, "output", None)
    if output is not None:
        return list(output)
    if isinstance(response, dict):
        return list(response.get("output", []) or [])
    return []


def _field(item, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)
