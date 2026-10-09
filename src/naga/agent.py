"""L1 任务回路(M1 批次 2,主文档 §5.1 的最小形态)。

回路:装配上下文(系统提示 + 技能检索)→ LLM 决策 → 工具调用(经
ctx.perform 入账)→ 结果回填 → 终答 | 步数/预算上限。
每步进 TraceRecorder(M1 出口:ReplayBundle 非空);预算逐调用
consume(prompt+completion 全额——deepseek 推理型模型的 completion 含
reasoning 消耗)。QuotaExhausted → outcome=aborted。
"""

from __future__ import annotations

import itertools
import json
import re
import time
import uuid
from typing import Any

from pydantic import BaseModel

from naga import kernel
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter, ToolSpec, price_cents
from naga.skills import SkillStore
from naga.trace import TraceRecorder


class AgentResult(BaseModel):
    reply: str | None
    outcome: str  # success | aborted
    trace_id: str
    steps: int
    tokens: int
    cost_cents: int
    failure_analysis: str | None = None


def _noop_revert() -> None:
    return None


class AgentLoop:
    """单任务执行体(M1:CLI 直驱;会话/lane 归属批次 3)。"""

    def __init__(self, *, ledger: DurableLedger, ctx: kernel.Context,
                 budget: kernel.Budget, adapter: ModelAdapter,
                 tools: list[Any] | None = None, skill_store: SkillStore | None = None,
                 session_key: str = "cli:local:owner", model_id: str = "deepseek-v4-flash",
                 max_steps: int = 8):
        self._ledger = ledger
        self._ctx = ctx
        self._budget = budget
        self._adapter = adapter
        self._tools = {t.name: t for t in (tools or [])}
        self._skills = skill_store
        self._session_key = session_key
        self._model_id = model_id
        self._max_steps = max_steps
        self._step_seq = 0

    # ---- 技能检索(初版:拉丁词 + CJK 二元组;M2 换 embedding+rerank) ----
    @staticmethod
    def _keywords(goal: str) -> list[str]:
        words = [w for w in re.split(r"[^\w]+", goal) if len(w) >= 2]
        cjk = re.findall(r"[\u4e00-\u9fff]", goal)
        return words + [a + b for a, b in itertools.pairwise(cjk)]

    def _select_skills(self, goal: str, limit: int = 3) -> list[str]:
        if self._skills is None:
            return []
        keywords = self._keywords(goal)
        scored: list[tuple[int, str]] = []
        for _, name, _, _, _ in self._skills.list():
            got = self._skills.get(name)
            if got is None:
                continue
            meta, content = got
            text = (name + " " + content).lower()
            score = sum(1 for k in keywords if k.lower() in text)
            if score > 0:
                scored.append((score, f"skill://{name}@v{meta.version}"))
        scored.sort(key=lambda p: (-p[0], p[1]))
        return [uri for _, uri in scored[:limit]]

    def _system_prompt(self, skills: list[str]) -> str:
        lines = [
            ("你是 Naga(Samsara Python 路线)的任务执行智能体。"
             "能直接回答时直接回答;需要外部事实或资料时调用工具。"),
        ]
        if skills:
            lines += ["", "## 可参考技能", *[f"- {uri}" for uri in skills]]
        return "\n".join(lines)

    async def _call_tool(self, tc: dict[str, Any]) -> tuple[Any, bool, int]:
        name = str(tc.get("name", ""))
        tool = self._tools.get(name)
        t0 = time.monotonic()
        try:
            args = json.loads(tc.get("arguments") or "{}")
            if tool is None:
                raise ValueError(f"unknown tool: {name}")

            async def _apply(tool: Any = tool, args: dict[str, Any] = args) -> Any:
                return await tool.run(args)

            result: Any = await self._ctx.perform(f"tool:{name}", _apply, _noop_revert)
            ok = True
        except (RuntimeError, ValueError) as err:
            result = {"error": str(err)}
            ok = False
        return result, ok, int((time.monotonic() - t0) * 1000)

    async def run(self, goal: str) -> AgentResult:
        skills = self._select_skills(goal)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(skills)},
            {"role": "user", "content": goal},
        ]
        tool_specs = [ToolSpec(name=t.name, description=t.description,
                               parameters=t.parameters) for t in self._tools.values()]
        rec = TraceRecorder(self._ledger, trace_id="tr_" + uuid.uuid4().hex[:24],
                            session_key=self._session_key, inbound={"text": goal},
                            skills_loaded=skills,
                            env={"model": self._model_id, "channel": "cli"})
        self._step_seq = 0
        tokens = 0
        cost = 0
        aborted: str | None = None
        try:
            for _ in range(self._max_steps):
                t0 = time.monotonic()
                resp = await self._adapter.chat(messages, tool_specs or None)
                ms = int((time.monotonic() - t0) * 1000)
                usage = resp.prompt_tokens + resp.completion_tokens
                self._budget.consume(usage)
                tokens += usage
                cost += price_cents(self._model_id, resp.prompt_tokens, resp.completion_tokens)
                self._step_seq += 1
                rec.step(self._step_seq, "llm", self._model_id, True, ms, usage,
                         args={"turn": self._step_seq},
                         results={"content": resp.content,
                                  "tool_calls": [tc["name"] for tc in resp.tool_calls]})
                if resp.tool_calls:
                    messages.append({"role": "assistant", "content": resp.content or "",
                                     "tool_calls": resp.tool_calls})
                    for tc in resp.tool_calls:
                        result, ok, tool_ms = await self._call_tool(tc)
                        self._step_seq += 1
                        rec.step(self._step_seq, "tool", str(tc["name"]), ok, tool_ms, 0,
                                 args=json.loads(tc.get("arguments") or "{}"),
                                 results=result)
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": json.dumps(result, ensure_ascii=False,
                                                                default=str)})
                    continue
                messages.append({"role": "assistant", "content": resp.content or ""})
                trace = rec.finish(outcome="success", cost_tokens=tokens, cost_cents=cost)
                return AgentResult(reply=resp.content, outcome="success",
                                   trace_id=str(trace["trace_id"]), steps=self._step_seq,
                                   tokens=tokens, cost_cents=cost)
            aborted = "步数上限"
        except kernel.QuotaExhausted:
            aborted = "预算耗尽"
        trace = rec.finish(outcome="aborted", cost_tokens=tokens, cost_cents=cost,
                           failure_analysis=aborted)
        return AgentResult(reply=None, outcome="aborted",
                           trace_id=str(trace["trace_id"]), steps=self._step_seq,
                           tokens=tokens, cost_cents=cost, failure_analysis=aborted)
