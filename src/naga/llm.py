"""LLM 适配器(M1 批次 2):OpenAI 兼容 chat + 凭据仓。

E.1 纪律:密钥只从 ~/.samsara/credentials/providers.json(0600)读取,
永不入日志/轨迹/bundle(trace.py 脱敏闸 sk- 规则兜底)。
deepseek-v4-flash 为推理型模型:max_tokens 默认给足(1024),
completion_tokens 全额计入预算;reasoning_content 不回传调用方。
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict

from naga import storage

TOOL_DESC_MAX_CHARS = 500  # spec-constants tool_desc_max_chars / 接口 §5.4(防投毒)


def default_credentials_path() -> str:
    """凭据仓路径跟随 home 解析(数据模型 §2:credentials 在 home 下)。"""
    return os.path.join(storage.resolve_home(), "credentials", "providers.json")


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    base_url: str
    api_key: str


class CredentialStore:
    """厂商凭据仓的只读入口(密钥不出 0600 文件)。"""

    def __init__(self, path: str | None = None):
        self._path = path or default_credentials_path()

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


# ── 价格表(E.1:带版本的资产;来源与时效标注,随价格表资产版本化更新)──
PRICE_TABLE_VERSION = "2026-10-est"  # 估算版:厂商公开定价页量级,待用户确认后升版
# (prompt, completion) 美分/1M tokens
PRICE_CENTS_PER_MTOK: dict[str, tuple[int, int]] = {
    "deepseek-v4-flash": (27, 110),  # 估算:该档公开定价量级(≈$0.27/$1.10 每 1M)
    "qwen-flash": (3, 9),            # 估算:轻量档公开定价量级
}


def price_cents(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """按价格表计费(美元分);未登记模型计 0(宁少记不虚记)。"""
    rate = PRICE_CENTS_PER_MTOK.get(model)
    if rate is None:
        return 0
    pin, pout = rate
    return round((prompt_tokens * pin + completion_tokens * pout) / 1_000_000)


def price_micro_cents(model: str, prompt_tokens: int,
                      completion_tokens: int) -> float:
    """仪表盘精度:微美分(1e-6 美元分级的费率换算,避免小任务截断为 0)。"""
    rate = PRICE_CENTS_PER_MTOK.get(model)
    if rate is None:
        return 0.0
    pin, pout = rate
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


class OpenAIChatAdapter:
    """通用 OpenAI 兼容 chat 适配器(任意厂商 compatible-mode;deepseek/dashscope 同构)。"""

    def __init__(self, config: ProviderConfig, model: str, timeout: float = 60.0):
        self._config = config
        self._model = model
        self._timeout = timeout

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


class DeepSeekAdapter(OpenAIChatAdapter):
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


# ── 图片内容识别(M2-W3.5:渠道媒体入 lane 前的 best-effort 描述)──────

VISION_DESCRIBE_PROMPT = ("用不超过 120 字客观描述这张图片的内容(人物/文字/场景/数据各概括要点);"
                          "若是纯文字截图,转述其中的关键文字。")
VISION_DESCRIBE_MAX_CHARS = 400  # 描述截断上限(防失控长文灌爆 goal)


def sniff_image_mime(data: bytes) -> str:
    """魔数嗅探(png/jpeg/webp/gif/bmp),兜底 jpeg。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


# (url, headers, body) -> (status, parsed_json);注入供契约测试
VisionRequest = Callable[..., Awaitable[tuple[int, Any]]]


async def _httpx_post_json(url: str, headers: dict[str, str],
                           body: dict[str, Any]) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}


class DashScopeVisionDescriber:
    """dashscope qwen-vl 图片描述(best-effort:一切失败返回 None,不阻断媒体链)。"""

    def __init__(self, config: ProviderConfig, model: str = "qwen-vl-plus",
                 request: VisionRequest = _httpx_post_json, max_tokens: int = 512):
        self._config = config
        self._model = model
        self._request = request
        self._max_tokens = max_tokens

    @classmethod
    def from_credentials(cls, store: CredentialStore, provider: str = "dashscope",
                         model: str = "qwen-vl-plus") -> DashScopeVisionDescriber:
        return cls(store.provider(provider), model)

    async def describe(self, image: bytes) -> str | None:
        data_url = (f"data:{sniff_image_mime(image)};base64,"
                    + base64.b64encode(image).decode())
        body: dict[str, Any] = {
            "model": self._model, "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": VISION_DESCRIBE_PROMPT}]}]}
        try:
            status, raw = await self._request(
                self._config.base_url + "/chat/completions",
                {"Authorization": "Bearer " + self._config.api_key,
                 "Content-Type": "application/json"}, body)
        except (httpx.HTTPError, OSError, ValueError):
            return None
        if status != 200 or not isinstance(raw, dict):
            return None
        data = cast("dict[str, Any]", raw)
        choices_any: Any = data.get("choices")
        if not isinstance(choices_any, list) or not choices_any:
            return None
        choices = cast("list[Any]", choices_any)
        if not isinstance(choices[0], dict):
            return None
        choice = cast("dict[str, Any]", choices[0])
        msg_any: Any = choice.get("message")
        if not isinstance(msg_any, dict):
            return None
        message = cast("dict[str, Any]", msg_any)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return content.strip()[:VISION_DESCRIBE_MAX_CHARS]
