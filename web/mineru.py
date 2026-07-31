from __future__ import annotations

"""Small synchronous client for MinerU's v4 precision extraction API."""

import io
import mimetypes
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


DEFAULT_BASE_URL = "https://mineru.net"
SUPPORTED_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
}


class MinerUError(RuntimeError):
    pass


@dataclass(frozen=True)
class MinerUResult:
    file_name: str
    markdown: str
    batch_id: str
    zip_url: str


class MinerUClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = (token if token is not None else os.environ.get("MINERU_API_TOKEN", "")).strip()
        if not self.token:
            raise MinerUError(
                "缺少 MINERU_API_TOKEN，请在 .env 中配置 MinerU 精准解析 API Token。"
            )
        self.base_url = (base_url or os.environ.get("MINERU_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.poll_interval = poll_interval if poll_interval is not None else _env_float("MINERU_POLL_INTERVAL_SECONDS", 3.0)
        self.timeout = timeout if timeout is not None else _env_float("MINERU_PARSE_TIMEOUT_SECONDS", 900.0)
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0))
        self._owns_client = http_client is None
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MinerUClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def parse_file(self, path: Path) -> MinerUResult:
        path = Path(path)
        if not path.is_file():
            raise MinerUError(f"附件不存在：{path.name}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise MinerUError(f"MinerU 精准解析暂不支持该文件类型：{path.suffix or path.name}")
        if path.stat().st_size > 200 * 1024 * 1024:
            raise MinerUError(f"附件超过 MinerU 的 200MB 限制：{path.name}")

        request_payload: dict[str, Any] = {
            "files": [{
                "name": path.name,
                "data_id": _safe_data_id(path.stem),
                "is_ocr": _env_bool("MINERU_IS_OCR", False),
            }],
            "model_version": os.environ.get("MINERU_MODEL_VERSION", "vlm").strip() or "vlm",
            "language": os.environ.get("MINERU_LANGUAGE", "ch").strip() or "ch",
            "enable_formula": _env_bool("MINERU_ENABLE_FORMULA", True),
            "enable_table": _env_bool("MINERU_ENABLE_TABLE", True),
        }
        page_ranges = os.environ.get("MINERU_PAGE_RANGES", "").strip()
        if page_ranges:
            request_payload["files"][0]["page_ranges"] = page_ranges

        created = self._request_json("POST", "/api/v4/file-urls/batch", json=request_payload)
        data = _api_data(created)
        batch_id = str(data.get("batch_id") or "")
        upload_urls = data.get("file_urls") or []
        if not batch_id or not isinstance(upload_urls, list) or len(upload_urls) != 1:
            raise MinerUError("MinerU 未返回有效的上传地址。")

        with path.open("rb") as handle:
            upload = self._client.put(str(upload_urls[0]), content=_file_chunks(handle))
        if upload.status_code < 200 or upload.status_code >= 300:
            raise MinerUError(f"上传附件到 MinerU 失败（HTTP {upload.status_code}）。")

        item = self._wait_for_result(batch_id, path.name)
        zip_url = str(item.get("full_zip_url") or "")
        if not zip_url:
            raise MinerUError("MinerU 任务已完成，但没有返回结果压缩包。")
        archive = self._client.get(zip_url)
        try:
            archive.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MinerUError(f"下载 MinerU 解析结果失败（HTTP {exc.response.status_code}）。") from exc
        markdown = _markdown_from_zip(archive.content)
        return MinerUResult(path.name, markdown, batch_id, zip_url)

    def _wait_for_result(self, batch_id: str, file_name: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = self._request_json("GET", f"/api/v4/extract-results/batch/{batch_id}")
            data = _api_data(response)
            results = data.get("extract_result") or []
            if not isinstance(results, list):
                raise MinerUError("MinerU 返回了无法识别的任务结果。")
            item = next(
                (row for row in results if isinstance(row, dict) and row.get("file_name") == file_name),
                results[0] if len(results) == 1 and isinstance(results[0], dict) else None,
            )
            if item is not None:
                state = str(item.get("state") or "")
                if state == "done":
                    return item
                if state == "failed":
                    raise MinerUError(f"MinerU 解析失败：{item.get('err_msg') or '未知错误'}")
            self._sleep(max(0.1, self.poll_interval))
        raise MinerUError(f"MinerU 解析超时（{int(self.timeout)} 秒）：{file_name}")

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self._client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MinerUError(f"调用 MinerU API 失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise MinerUError("MinerU API 返回了无法识别的数据。")
        if payload.get("code") != 0:
            raise MinerUError(f"MinerU API 错误：{payload.get('msg') or payload.get('code')}")
        return payload


def build_llm_message(user_message: str, results: list[MinerUResult]) -> str:
    """Compatibility helper; parsed attachment text is no longer user content."""
    del results
    return user_message.strip()


def build_attachment_payloads(
    results: list[MinerUResult],
    paths: list[Path],
) -> list[dict[str, Any]]:
    """Keep the user's text separate from attachment data until externalization."""
    by_name = {path.name: path for path in paths}
    payloads = []
    for result in results:
        path = by_name.get(result.file_name)
        media_type = mimetypes.guess_type(result.file_name)[0] or "application/octet-stream"
        payloads.append({
            "name": result.file_name,
            "media_type": media_type,
            "size_bytes": path.stat().st_size if path is not None else 0,
            "content": result.markdown,
        })
    return payloads


def display_message(user_message: str, results: list[MinerUResult]) -> str:
    names = "、".join(result.file_name for result in results)
    return f"{user_message.strip()}\n\n附件：{names}".strip()


def _api_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MinerUError("MinerU API 响应缺少 data。")
    return data


def _markdown_from_zip(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            markdown_names = [name for name in names if Path(name).name == "full.md"]
            if not markdown_names:
                markdown_names = [name for name in names if name.lower().endswith(".md")]
            if not markdown_names:
                raise MinerUError("MinerU 结果压缩包中没有 Markdown 文件。")
            documents = []
            for name in markdown_names:
                info = archive.getinfo(name)
                if info.file_size > 100 * 1024 * 1024:
                    raise MinerUError("MinerU 返回的 Markdown 文件过大。")
                documents.append(archive.read(info).decode("utf-8", errors="replace"))
    except zipfile.BadZipFile as exc:
        raise MinerUError("MinerU 返回的结果不是有效的 ZIP 文件。") from exc
    markdown = "\n\n".join(item.strip() for item in documents if item.strip())
    if not markdown:
        raise MinerUError("MinerU 返回的 Markdown 内容为空。")
    return markdown


def _file_chunks(handle: Any, chunk_size: int = 1024 * 1024):
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            return
        yield chunk


def _safe_data_id(stem: str) -> str:
    safe = "".join(char if char.isalnum() or char in "_.-" else "-" for char in stem)
    return (safe.strip("-.") or "attachment")[:128]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default
