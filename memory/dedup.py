import re
import math


_BULLET_RE = re.compile(r"^\s*[-*]\s*")
_TAG_RE = re.compile(r"^\[[^\]]+\]\s*")
_SOURCE_RE = re.compile(r"\s*\(source:\s*`[^`]+`\)\s*$")
_PUNCT_RE = re.compile(r"[`*_#>\-\[\]（）()，。,.!！?？:：；;\s\"']")


def normalize_memory_text(text: str) -> str:
    value = str(text or "").strip().lower()
    value = _BULLET_RE.sub("", value)
    value = _TAG_RE.sub("", value)
    value = _SOURCE_RE.sub("", value)
    value = _PUNCT_RE.sub("", value)
    return value


def parse_memory_items(markdown: str) -> list[str]:
    items: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*")):
            items.append(stripped)
    return items


def is_duplicate_memory(new_text: str, existing_markdown: str) -> bool:
    new_norm = normalize_memory_text(new_text)
    if not new_norm:
        return True
    for item in parse_memory_items(existing_markdown):
        if normalize_memory_text(item) == new_norm:
            return True
        if is_semantic_duplicate(new_text, item):
            return True
    return False


def is_semantic_duplicate(
    left: str,
    right: str,
    *,
    threshold: float = 0.92,
    embedding_provider=None,
) -> bool:
    if embedding_provider is not None:
        try:
            left_vec = embedding_provider.embed(left)
            right_vec = embedding_provider.embed(right)
            return _cosine(left_vec, right_vec) >= float(threshold)
        except Exception:
            pass

    left_tokens = _semantic_tokens(left)
    right_tokens = _semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens.intersection(right_tokens)
    containment = len(overlap) / max(1, min(len(left_tokens), len(right_tokens)))
    jaccard = len(overlap) / max(1, len(left_tokens.union(right_tokens)))
    return containment >= 0.8 and jaccard >= 0.45


def _semantic_tokens(text: str) -> set[str]:
    value = str(text or "").strip().lower()
    value = _BULLET_RE.sub("", value)
    value = _TAG_RE.sub("", value)
    value = _SOURCE_RE.sub("", value)
    raw = re.findall(r"[a-z0-9_\u4e00-\u9fff]+", value)
    tokens = set()
    for token in raw:
        if token in _STOPWORDS or len(token) <= 1:
            continue
        tokens.add(_stem_token(token))
    return tokens


def _stem_token(token: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "please",
    "run",
    "the",
    "to",
    "use",
    "using",
    "with",
}
