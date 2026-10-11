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
from naga.branch import SessionBranch
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter, ToolSpec, price_cents
from naga.skills import SKILL_MAX_BYTES, SkillStore
from naga.trace import TraceRecorder

SKILL_DISTILL_MIN_TOOL_CALLS = 5  # spec-constants skill_distill_min_tool_calls / H.4


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
                 max_steps: int = 8, trace_id: str | None = None,
                 actor_trust: str | None = None,
                 max_tokens: int = 8192, trace_parent: str | None = None,
                 branch: SessionBranch | None = None):
        self._ledger = ledger
        self._ctx = ctx
        self._budget = budget
        self._adapter = adapter
        self._tools = {t.name: t for t in (tools or [])}
        self._skills = skill_store
        self._session_key = session_key
        self._model_id = model_id
        self._max_steps = max_steps
        self._trace_id = trace_id
        self._actor_trust = actor_trust
        self._max_tokens = max_tokens  # 推理模型:reasoning+content 共享上限,须给足
        self._trace_parent = trace_parent
        self._branch = branch
        self._step_seq = 0

    # ---- 技能检索(local:{session} + main 双分支;拉丁词 + CJK 二元组) ----
    @staticmethod
    def _keywords(goal: str) -> list[str]:
        words = [w for w in re.split(r"[^\w]+", goal) if len(w) >= 2]
        cjk = re.findall(r"[\u4e00-\u9fff]", goal)
        return words + [a + b for a, b in itertools.pairwise(cjk)]

    def _select_skills(self, goal: str, limit: int = 3) -> list[str]:
        if self._skills is None:
            return []
        keywords = self._keywords(goal)
        scored: list[tuple[int, str, str]] = []  # (score, uri, cas_id)
        seen: set[str] = set()
        for branch in (f"local:{self._session_key}", "main"):
            for cas, name, _, _, _ in self._skills.list(branch=branch):
                if name in seen:
                    continue
                got = self._skills.get(name, branch=branch)
                if got is None:
                    continue
                meta, content = got
                text = (name + " " + content).lower()
                score = sum(1 for k in keywords if k.lower() in text)
                if score > 0:
                    scored.append((score, f"skill://{name}@v{meta.version}", cas))
                    seen.add(name)
        scored.sort(key=lambda p: (-p[0], p[1]))
        picked = scored[:limit]
        for _, _, cas in picked:  # 命中即计量(uses/lastUsed,投影确定性更新)
            self._ledger.append("skill.use", "root", {"cas_id": cas})
        return [uri for _, uri, _ in picked]

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
        except Exception as err:  # noqa: BLE001 —— 工具失败=ok=False 步(K.1),
            # 不得杀死任务:网络错/worker 崩溃/参数错一律降级续跑(出口③前提)
            result = {"error": f"{type(err).__name__}: {err}"}
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
        trace_id = self._trace_id or ("tr_" + uuid.uuid4().hex[:24])
        rec = TraceRecorder(self._ledger,
                            trace_id=trace_id,
                            session_key=self._session_key, inbound={"text": goal},
                            skills_loaded=skills,
                            env={"model": self._model_id,
                                 "channel": self._session_key.split(":", 1)[0],
                                 "actor_trust": self._actor_trust},
                            parent_trace=self._trace_parent)
        self._step_seq = 0
        self._tool_steps = 0
        self._tool_log: list[dict[str, Any]] = []
        tokens = 0
        cost = 0
        aborted: str | None = None
        try:
            for _ in range(self._max_steps):
                t0 = time.monotonic()
                resp = await self._adapter.chat(messages, tool_specs or None,
                                                max_tokens=self._max_tokens)
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
                    # 回填必须用 OpenAI 完整格式(id/type/function 嵌套)——简化 dict 会被 422 拒
                    messages.append({"role": "assistant", "content": resp.content or "",
                                     "tool_calls": [
                                         {"id": tc["id"], "type": "function",
                                          "function": {"name": tc["name"],
                                                       "arguments": tc["arguments"]}}
                                         for tc in resp.tool_calls]})
                    for tc in resp.tool_calls:
                        args = json.loads(tc.get("arguments") or "{}")
                        result, ok, tool_ms = await self._call_tool(tc)
                        self._tool_steps += 1
                        self._tool_log.append({"tool": str(tc["name"]), "args": args})
                        self._step_seq += 1
                        rec.step(self._step_seq, "tool", str(tc["name"]), ok, tool_ms, 0,
                                 args=args, results=result)
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": json.dumps(result, ensure_ascii=False,
                                                                default=str)})
                    continue
                messages.append({"role": "assistant", "content": resp.content or ""})
                reply = resp.content or ""
                # 沉淀判定(H.4:≥5 工具调用才值得记;§6.4 默认局部——local:{session} 分支)
                if self._skills is not None and self._tool_steps >= SKILL_DISTILL_MIN_TOOL_CALLS:
                    sed = await self._sediment(goal, reply, rec, trace_id)
                    tokens += sed["tokens"]
                    cost += sed["cost_cents"]
                trace = rec.finish(outcome="success", cost_tokens=tokens, cost_cents=cost)
                self._settle_branch("commit", trace_id)
                return AgentResult(reply=reply, outcome="success",
                                   trace_id=str(trace["trace_id"]), steps=self._step_seq,
                                   tokens=tokens, cost_cents=cost)
            aborted = "步数上限"
        except kernel.QuotaExhausted:
            aborted = "预算耗尽"
        trace = rec.finish(outcome="aborted", cost_tokens=tokens, cost_cents=cost,
                           failure_analysis=aborted)
        self._settle_branch("discard", trace_id)
        return AgentResult(reply=None, outcome="aborted",
                           trace_id=str(trace["trace_id"]), steps=self._step_seq,
                           tokens=tokens, cost_cents=cost, failure_analysis=aborted)

    def _settle_branch(self, action: str, trace_id: str | None) -> None:
        """任务末分支裁决(M3-2):根分支 commit/discard;子分支保持 open,
        裁决权属父会话(§5.4 人走茶不凉)——父级联时一并处理。"""
        if self._branch is None or self._branch.parent is not None:
            return
        try:
            if action == "commit":
                self._branch.commit(trace_id)
            else:
                self._branch.discard()
        except ValueError:
            pass  # 分支已被外部处置(如 owner 介入):尊重现状

    # ---- 技能沉淀(④ 类蒸馏调用,H.1;§6.4 默认局部) ----
    @staticmethod
    def _slug(text: str, fallback: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
        return s or fallback

    async def _sediment(self, goal: str, reply: str, rec: TraceRecorder,
                        trace_id: str) -> dict[str, int]:
        """蒸馏任务经验为 SKILL.md,提交 local:{session} 分支;返回计量。"""
        t0 = time.monotonic()
        prompt = (
            "根据以下任务执行记录,为\"下次同类任务直接复用\"沉淀一份技能文档。\n"
            "第一行输出技能 ID(格式:id: <短横线小写英文>),随后是 Markdown 正文,"
            "必须包含三个二级标题节:## 何时使用、## 步骤、## 已知陷阱。\n\n"
            f"任务:{goal}\n"
            f"执行记录:{json.dumps(self._tool_log, ensure_ascii=False, default=str)}\n"
            f"最终结果:{reply}")
        try:
            resp = await self._adapter.chat([{"role": "user", "content": prompt}],
                                            None, max_tokens=self._max_tokens)
        except kernel.QuotaExhausted:
            self._step_seq += 1
            rec.step(self._step_seq, "llm", "skill-distill", False,
                     int((time.monotonic() - t0) * 1000), 0,
                     args={"goal": goal}, results={"error": "预算耗尽,跳过沉淀"})
            return {"tokens": 0, "cost_cents": 0}
        usage = resp.prompt_tokens + resp.completion_tokens
        try:
            self._budget.consume(usage)
        except kernel.QuotaExhausted:
            return {"tokens": 0, "cost_cents": 0}
        body = (resp.content or "").strip()
        m = re.match(r"^id:\s*([a-z0-9][a-z0-9-]*)\s*\n", body)
        skill_id = m.group(1) if m else self._slug(goal, f"skill-{trace_id[-6:]}")
        if m:
            body = body[m.end():]
        content = f"---\nname: {skill_id}\n---\n{body}\n"
        data = content.encode("utf-8")
        if len(data) > SKILL_MAX_BYTES:  # 按字节截断(CJK 多字节;15KB 硬限在 commit 兜底)
            content = data[:SKILL_MAX_BYTES - 1].decode("utf-8", errors="ignore") + "\n"
        ok = True
        try:
            if self._branch is not None:
                # M3-2:沉淀写进会话分支(覆盖层),任务末由 commit 物化/discard 丢弃
                self._branch.put(f"skill:{skill_id}", content.encode("utf-8"))
            else:
                assert self._skills is not None
                self._skills.commit(skill_id, content,
                                    provenance={"source": "agent", "trace_id": trace_id},
                                    branch=f"local:{self._session_key}")
        except (ValueError, AssertionError):
            ok = False
        self._step_seq += 1
        rec.step(self._step_seq, "llm", "skill-distill", ok,
                 int((time.monotonic() - t0) * 1000), usage,
                 args={"goal": goal}, results={"skill": skill_id if ok else None})
        return {"tokens": usage, "cost_cents": price_cents(self._model_id, usage, 0)}
