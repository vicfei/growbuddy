"""LLM 适配器(M1 批次 2):OpenAI 兼容 chat + 凭据仓。

E.1 纪律:密钥只从 ~/.samsara/credentials/providers.json(0600)读取,
永不入日志/轨迹/bundle(trace.py 脱敏闸 sk- 规则兜底)。
deepseek-v4-flash 为推理型模型:max_tokens 默认给足(1024),
completion_tokens 全额计入预算;reasoning_content 不回传调用方。
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict

TOOL_DESC_MAX_CHARS = 500  # spec-constants tool_desc_max_chars / 接口 §5.4(防投毒)

DEFAULT_CREDENTIALS_PATH = os.path.join(
    os.path.expanduser("~"), ".samsara", "credentials", "providers.json")


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    base_url: str
    api_key: str


class CredentialStore:
    """厂商凭据仓的只读入口(密钥不出 0600 文件)。"""

    def __init__(self, path: str = DEFAULT_CREDENTIALS_PATH):
        self._path = path

    def provider(self, name: str) -> ProviderConfig:
        with open(self._path, encoding="utf-8") as fh:
            data = json.load(fh)
        if name not in data:
            raise KeyError(f"凭据仓无供应商 {name!r}(查 {self._path})")
        return ProviderConfig.model_validate(data[name])

    def has(self, name: str) -> bool:
        try:
            with open(self._path, encoding="utf-8") as fh:
                return name in json.load(fh)
        except (OSError, ValueError):
            return False


class ToolSpec(BaseModel):
    """OpenAI function-calling 工具描述(接口 §5.4;sideEffect 语义由工具方声明)。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def model_post_init(self, __context: Any, /) -> None:
        if len(self.description) > TOOL_DESC_MAX_CHARS:
            raise ValueError(f"工具描述超 {TOOL_DESC_MAX_CHARS} 字符上限")

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[dict[str, Any]] = []  # [{id, name, arguments(json str)}]
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelAdapter(Protocol):
    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[ToolSpec] | None = None, *,
                   max_tokens: int = 1024) -> ChatResponse: ...


def price_cents(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """M1 记 0:价格是带版本的资产(E.1),M2 落地价格表后生效。"""
    del model, prompt_tokens, completion_tokens
    return 0


class DeepSeekAdapter:
    """OpenAI 兼容 chat 适配器(deepseek;推理型模型纪律见模块 docstring)。"""

    def __init__(self, config: ProviderConfig, model: str, timeout: float = 60.0):
        self._config = config
        self._model = model
        self._timeout = timeout

    @classmethod
    def from_credentials(cls, store: CredentialStore, provider: str = "deepseek",
                         model: str = "deepseek-v4-flash") -> DeepSeekAdapter:
        cfg = store.provider(provider)
        return cls(cfg, model)

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[ToolSpec] | None = None, *,
                   max_tokens: int = 1024) -> ChatResponse:
        payload: dict[str, Any] = {"model": self._model, "messages": messages,
                                   "max_tokens": max_tokens}
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
        headers = {"Authorization": "Bearer " + self._config.api_key,
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._config.base_url + "/chat/completions",
                                     json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        msg: dict[str, Any] = data["choices"][0]["message"]
        usage: dict[str, Any] = data.get("usage") or {}
        raw_calls: list[dict[str, Any]] = msg.get("tool_calls") or []
        return ChatResponse(
            content=msg.get("content"),
            tool_calls=[{"id": c["id"], "name": c["function"]["name"],
                         "arguments": c["function"].get("arguments") or "{}"}
                        for c in raw_calls],
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)))
