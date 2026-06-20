import os
from typing import Any, Callable

import httpx


class TavilySearchClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = "https://api.tavily.com/search",
        timeout: int = 20,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.post = post or httpx.post

    def search(
        self,
        *,
        query: str,
        topic: str = "general",
        max_results: int = 5,
        time_range: str | None = None,
    ) -> list[dict[str, Any]]:
        api_key = self.api_key or os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            raise ValueError("TAVILY_API_KEY is not configured.")

        query = query.strip()
        if not query:
            raise ValueError("Search query is required.")

        payload: dict[str, Any] = {
            "query": query,
            "topic": topic,
            "search_depth": "basic",
            "max_results": max(1, min(int(max_results), 8)),
            "include_answer": False,
            "include_raw_content": False,
        }
        if time_range:
            payload["time_range"] = time_range

        response = self.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:1200],
                "score": item.get("score"),
            }
            for item in results
        ]
