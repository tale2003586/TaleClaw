from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any


DEFAULT_CONTEXT_WINDOW_TOKENS = 128000
DEFAULT_SAFE_CONTEXT_RATIO = 0.85
DEFAULT_OUTPUT_RESERVE_TOKENS = 4096
DEFAULT_OUTPUT_RESERVE_CONTEXT_RATIO = 0.20


def estimate_tokens(messages: list[dict] | None, *, provider: Any | None = None) -> int:
    """Estimate prompt tokens for a model call.

    Providers may expose a better counter later. The fallback is a conservative
    multilingual estimate: CJK characters are counted close to one token each,
    while ASCII/code-heavy text is grouped roughly four chars per token.
    """
    items = list(messages or [])
    counter = getattr(provider, "count_tokens", None)
    if callable(counter):
        try:
            value = counter(items)
            if isinstance(value, dict):
                value = value.get("total_tokens") or value.get("input_tokens")
            if value is not None:
                return max(0, int(value))
        except Exception:
            pass
    if bool(getattr(provider, "bpe_tokenizer_enabled", False)):
        model = str(getattr(provider, "tokenizer_model", "") or "").strip()
        tiktoken_count = _count_with_tiktoken(items, model=model)
        if tiktoken_count is not None:
            return tiktoken_count
    return max(0, sum(_estimate_message_tokens(message) for message in items))


def context_window_tokens(
    provider: Any | None = None,
    *,
    default: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
) -> int:
    """Return the model's total context window, not the output token cap."""
    parsed = _first_positive_attr(
        provider,
        ("context_window_tokens", "context_window", "max_context_tokens", "context_limit"),
    )
    if parsed is not None:
        return parsed
    return int(default)


def output_reserve_tokens(
    provider: Any | None = None,
    *,
    requested_output_tokens: int = 0,
    context_window: int | None = None,
) -> int:
    explicit = _first_positive_attr(
        provider,
        ("output_reserve_tokens", "reserved_output_tokens", "max_output_reserve_tokens"),
    )
    requested = max(0, _int_or_default(requested_output_tokens, 0))
    window = max(1, int(context_window or context_window_tokens(provider)))
    window_cap = max(1, int(window * DEFAULT_OUTPUT_RESERVE_CONTEXT_RATIO))

    if explicit is not None:
        reserve = explicit
    elif requested > 0:
        reserve = min(requested, DEFAULT_OUTPUT_RESERVE_TOKENS, window_cap)
    else:
        reserve = 0

    if requested > 0:
        reserve = min(reserve, requested)
    return max(0, min(reserve, window_cap))


def safe_context_limit(
    provider: Any | None = None,
    *,
    reserved_output_tokens: int = 0,
    ratio: float = DEFAULT_SAFE_CONTEXT_RATIO,
) -> int:
    window = context_window_tokens(provider)
    reserve = output_reserve_tokens(
        provider,
        requested_output_tokens=reserved_output_tokens,
        context_window=window,
    )
    window_safe_limit = max(1, int(window * float(ratio)) - reserve)

    explicit_input = _first_positive_attr(
        provider,
        ("max_input_tokens", "input_context_limit", "input_token_limit"),
    )
    if explicit_input is None:
        return max(1, window_safe_limit)
    return max(1, min(int(explicit_input * float(ratio)), window_safe_limit))


def output_tokens_for_call(
    provider: Any | None = None,
    *,
    requested_output_tokens: int,
    input_tokens: int,
    ratio: float = DEFAULT_SAFE_CONTEXT_RATIO,
) -> int:
    requested = max(1, _int_or_default(requested_output_tokens, 1))
    window = context_window_tokens(provider)
    safe_total = max(1, int(window * float(ratio)))
    available = max(1, safe_total - max(0, _int_or_default(input_tokens, 0)))

    explicit_output = _first_positive_attr(
        provider,
        ("max_output_tokens", "output_token_limit", "max_completion_tokens"),
    )
    if explicit_output is not None:
        available = min(available, explicit_output)
    return max(1, min(requested, available))


def _estimate_message_tokens(message: dict) -> int:
    try:
        text = json.dumps(message, ensure_ascii=False, default=str)
    except TypeError:
        text = str(message)
    return _estimate_text_tokens(text)


def _estimate_text_tokens(text: str) -> int:
    ascii_chars = 0
    cjk_chars = 0
    other_chars = 0
    for char in str(text or ""):
        codepoint = ord(char)
        if codepoint < 128:
            ascii_chars += 1
        elif _is_cjk(char):
            cjk_chars += 1
        else:
            other_chars += 1
    return max(
        0,
        math.ceil((ascii_chars / 4.0) + (cjk_chars * 1.1) + (other_chars * 1.0)),
    )


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _count_with_tiktoken(messages: list[dict], *, model: str) -> int | None:
    if not model:
        return None
    encoding = _tiktoken_encoding(model)
    if encoding is None:
        return None

    try:
        text = json.dumps(messages, ensure_ascii=False, default=str)
        return max(0, len(encoding.encode(text)))
    except Exception:
        return None


@lru_cache(maxsize=16)
def _tiktoken_encoding(model: str):
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None

    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def _first_positive_attr(provider: Any | None, attrs: tuple[str, ...]) -> int | None:
    if provider is None:
        return None
    for attr in attrs:
        value = getattr(provider, attr, None)
        if value is None:
            continue
        parsed = _positive_int_or_none(value() if callable(value) else value)
        if parsed is not None:
            return parsed
    return None


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed > 0:
        return parsed
    return None


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)
