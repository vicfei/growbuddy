"""首批工具(M1 批次 2):bocha web_search。

工具执行一律经 AgentLoop 的 ctx.perform 入账(K.1:web_search 为 read 类,
revert 为 no-op)。返回结构精简为 {name, url, summary},避免 bundle 膨胀。
"""

from __future__ import annotations

from typing import Any

import httpx

from naga.llm import CredentialStore

_WEB_SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词"},
    },
    "required": ["query"],
}


class WebSearchTool:
    """bocha 网页搜索(sideEffect=read)。"""

    name = "web_search"
    description = "搜索公开网页,返回标题、链接与摘要。适用于需要事实、最新信息或外部资料的任务。"
    parameters = _WEB_SEARCH_PARAMETERS
    side_effect = "read"

    def __init__(self, store: CredentialStore, count: int = 5, timeout: float = 30.0):
        cfg = store.provider("bocha")
        self._base_url = cfg.base_url
        self._api_key = cfg.api_key
        self._count = count
        self._timeout = timeout

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("web_search 需要 query 参数")
        headers = {"Authorization": "Bearer " + self._api_key,
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._base_url + "/web-search",
                                     json={"query": query, "count": self._count,
                                           "summary": True},
                                     headers=headers)
            resp.raise_for_status()
            data = resp.json()
        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        return {"results": [
            {"name": p.get("name", ""), "url": p.get("url", ""),
             "summary": (p.get("summary") or "")[:500]}
            for p in pages[:self._count]]}
