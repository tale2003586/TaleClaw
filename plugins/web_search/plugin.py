import json

from plugins.base import Plugin, ToolRegistration
from plugins.web_search.client import TavilySearchClient
from tools.schema import function_tool
from tools.spec import ToolExposure, ToolRisk


class WebSearchPlugin(Plugin):
    name = "web_search"

    def tools(self) -> list[ToolRegistration]:
        return [
            ToolRegistration(
                schema=function_tool(
                    "web_search",
                    (
                        "Search the public web for current information. Return sources with URLs. "
                        "This tool has a per-turn runtime budget; each result begins with a "
                        "web-search-budget notice showing remaining calls. Consolidate queries "
                        "and do not call this tool again when the notice says remaining=0."
                    ),
                    {
                        "query": {
                            "type": "string",
                            "description": "Search query.",
                        },
                        "topic": {
                            "type": "string",
                            "enum": ["general", "news", "finance"],
                            "description": "Search category. Defaults to general.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Number of results. Defaults to 5, maximum 8.",
                        },
                        "time_range": {
                            "type": "string",
                            "enum": ["day", "week", "month", "year"],
                            "description": "Optional recency filter.",
                        },
                    },
                    ["query"],
                ),
                handler=self.search,
                risk=ToolRisk.LOW,
                allowed_modes=frozenset({"bot", "coding"}),
                exposure=ToolExposure.DEFERRED,
                discovery_summary="Search the public web for current information and official sources.",
                capabilities=("web search", "internet research", "current information"),
                aliases=("search online", "联网搜索", "网络搜索", "网上查"),
                keywords=("web", "internet", "latest", "查最新资料", "查官网"),
                idempotent=True,
                side_effect=False,
                source="plugin:web_search",
            )
        ]

    def search(
        self,
        query: str,
        topic: str = "general",
        max_results: int = 5,
        time_range: str | None = None,
    ) -> str:
        results = TavilySearchClient().search(
            query=query,
            topic=topic,
            max_results=max_results,
            time_range=time_range,
        )
        return json.dumps(results, ensure_ascii=False, indent=2)
