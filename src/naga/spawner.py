"""Sub-agent spawner — the runtime shape of the authorization algebra (§5.4, M3-1).

Three invariants (INV-3 operationalized; the kernel authorize()/Budget tree
carries them, this module is the L1 runtime on top):

  child.budget < parent.remaining    — via Budget.allocate (strictly less;
                                       multiplicative decay ⇒ every spawn tree
                                       terminates without a supervisor)
  child.tools  ⊆ parent.tools        — structural: same raw set, same R filter
  child.trust  ≤ parent.trust        — ratchet: children inherit, never raise

Depth is computed server-side along the chain (INC3): soft limit 3 (beyond it
requires owner approval — the approval queue itself is M4), hard cap 5;
``known`` peers may spawn but their children may not (depth ≤ 1, §3.2 method
table). ``wall_ms`` is enforced here (R6-B13: the L0 Budget has no wall clock).

Kill / reclamation: ``kill(child)`` cancels the task and releases the unused
budget back to the parent (``Budget.release``). Ledger facts survive the kill
— traces and sedimented assets stay ("人走茶不凉"); reverting side effects
rides on the kernel's LIFO unload semantics.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

from naga import kernel
from naga.agent import AgentLoop
from naga.branch import SessionBranch
from naga.gateway import tools_for_trust
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter
from naga.skills import SkillStore

SPAWN_DEPTH_SOFT_LIMIT = 3  # candidate spec-constant spawn_depth_soft_limit (§3.4)
SPAWN_DEPTH_HARD_CAP = 5    # candidate spec-constant spawn_depth_hard_cap (INC3)
SPAWN_WALL_MS_DEFAULT = 120_000  # candidate spawn_wall_ms_default (R6-B13)
SPAWN_CHILD_BUDGET_SHARE = 0.4   # §5.4 example tree: 子 ≤ 40% of parent pool


class SpawnRejected(ValueError):
    """Spawn request violates the algebra (depth / quota / caps)."""


class SubAgentSpawner:
    """Per-agent spawn authority bound to one budget node in the tree."""

    def __init__(self, *, ledger: DurableLedger, ctx: kernel.Context,
                 make_adapter: Callable[[], ModelAdapter],
                 raw_tools: list[Any], skill_store: SkillStore | None,
                 session_key: str, actor_trust: str | None,
                 budget: kernel.Budget, depth: int,
                 parent_trace: str | None = None,
                 branch: SessionBranch | None = None):
        self._ledger = ledger
        self._ctx = ctx
        self._make_adapter = make_adapter
        self._raw_tools = raw_tools
        self._skills = skill_store
        self._session_key = session_key
        self._trust = actor_trust or "owner"
        self._budget = budget
        self._depth = depth
        self._branch = branch
        self._parent_trace = parent_trace

    # ---- 工具集装配(不变式②:子 ⊆ 父,同一 R 过滤) ----

    @property
    def budget(self) -> kernel.Budget:
        return self._budget

    def can_spawn(self) -> bool:
        """当前深度/信任级是否还可派生(硬顶 + known 深度≤1 + guest/untrusted 封死)。"""
        if self._trust in ("guest", "untrusted"):
            return False  # §3.2 方法表:spawn guest ❌;R1 工具,R0 不可见
        if self._trust == "known":
            return self._depth < 1
        return self._depth < SPAWN_DEPTH_HARD_CAP

    def make_tools(self) -> list[Any]:
        """该深度可见的工具集:R 档过滤 + (允许时)派生工具。"""
        base = tools_for_trust(self._trust, self._raw_tools)
        return [*base, SpawnTool(self)] if self.can_spawn() else base

    # ---- 派生(不变式①③ + 深度策略 + wall-clock) ----

    def _check_depth(self) -> None:
        if self._trust == "known" and self._depth >= 1:
            raise SpawnRejected("known 级仅允许一层派生(§3.2 depth≤1)")
        if self._depth >= SPAWN_DEPTH_HARD_CAP:
            raise SpawnRejected(f"派生深度触及硬顶 {SPAWN_DEPTH_HARD_CAP}(INC3),不可逾越")
        if self._depth >= SPAWN_DEPTH_SOFT_LIMIT:
            raise SpawnRejected(
                f"派生深度超软限 {SPAWN_DEPTH_SOFT_LIMIT},需 owner 审批(审批队列表 M4)")

    def _child_budget(self, requested: int | None) -> int:
        if requested is not None and requested > 0:
            return requested  # 显式请求:allocate 强制"严格小于父剩余",违规即拒
        # 默认份额:父剩余的 40%,且严格小于父剩余(留父代自身余量)
        return max(1, min(int(self._budget.remaining * SPAWN_CHILD_BUDGET_SHARE),
                          self._budget.remaining - 1))

    async def spawn_many(self, subtasks: list[dict[str, Any]]) -> dict[str, Any]:
        """并行派生一批子任务(同批并发;各自独立预算/轨迹/wall-clock)。"""
        if not subtasks:
            raise SpawnRejected("subtasks 不能为空")
        self._check_depth()
        specs: list[tuple[str, kernel.Budget, int]] = []
        for item in subtasks:
            goal = str(item.get("goal") or "").strip()
            if not goal:
                raise SpawnRejected("每个 subtask 需要 goal")
            wall_ms = int(item.get("wall_ms") or SPAWN_WALL_MS_DEFAULT)
            try:
                child_budget = self._budget.allocate(self._child_budget(
                    item.get("budget_tokens")))
            except kernel.QuotaExhausted as err:
                for _, allocated, _ in specs:  # 失败授权不留账:整批回滚已划转
                    allocated.release()
                raise SpawnRejected(f"配额不足: {err}") from None
            specs.append((goal, child_budget, wall_ms))
        results = await asyncio.gather(
            *(self._run_child(goal, budget, wall_ms) for goal, budget, wall_ms in specs),
            return_exceptions=False)
        return {"subagents": [r for r in results]}

    async def _run_child(self, goal: str, budget: kernel.Budget,
                         wall_ms: int) -> dict[str, Any]:
        agent_id = "ag_" + uuid.uuid4().hex[:12]
        trace_id = "tr_" + uuid.uuid4().hex[:24]
        child_branch = (self._branch.spawn_child() if self._branch is not None
                        and self._branch.state == "open" else None)  # kill:分支保持 open 待父裁决
        child = SubAgentSpawner(
            ledger=self._ledger, ctx=self._ctx, make_adapter=self._make_adapter,
            raw_tools=self._raw_tools, skill_store=self._skills,
            session_key=self._session_key, actor_trust=self._trust,
            budget=budget, depth=self._depth + 1, parent_trace=trace_id,
            branch=child_branch)
        loop = AgentLoop(ledger=self._ledger, ctx=self._ctx, budget=budget,
                         adapter=self._make_adapter(), tools=child.make_tools(),
                         skill_store=self._skills, session_key=self._session_key,
                         trace_id=trace_id, actor_trust=self._trust,
                         trace_parent=self._parent_trace,  # 子轨迹挂派生方轨迹
                         branch=child_branch)
        self._append("agent.spawned", agent_id, {
            "goal": goal, "budget_tokens": budget.total, "depth": self._depth + 1,
            "parent_trace": self._parent_trace, "session_key": self._session_key})
        t0 = time.monotonic()
        task = asyncio.ensure_future(loop.run(goal))
        outcome, reply, tokens, failure = "success", None, 0, None
        try:
            result = await asyncio.wait_for(task, wall_ms / 1000)
            outcome, reply, tokens = result.outcome, result.reply, result.tokens
            failure = result.failure_analysis
        except TimeoutError:  # wall-clock kill(R6-B13):超时即杀,配额归还
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception) as err:  # noqa: BLE001 —— 取消路径异常不外抛
                print(f"[spawner] 子任务取消回收: {type(err).__name__}", flush=True)
            outcome, failure = "killed", f"wall-clock {wall_ms}ms 超限,已回收"
        except Exception as err:  # noqa: BLE001 —— 子任务异常降级为失败结局
            outcome, failure = "failed", f"{type(err).__name__}: {err}"
        finally:
            budget.release()  # 无残留:未用尽部分即刻归还父代
        self._append("agent.terminated", agent_id, {
            "outcome": outcome, "tokens": tokens, "budget_remaining": budget.remaining,
            "wall_ms": int((time.monotonic() - t0) * 1000),
            "failure_analysis": failure, "session_key": self._session_key})
        return {"agent_id": agent_id, "goal": goal, "outcome": outcome,
                "reply": reply, "tokens": tokens, "failure": failure}

    def _append(self, kind: str, agent_id: str, payload: dict[str, Any]) -> None:
        try:
            self._ledger.append(kind, agent_id, payload)
        except OSError:
            pass  # 事件入账尽力而为:派生/回收语义不因账本 IO 抖动中断


_SPAWN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array", "description": "子任务列表",
            "items": {"type": "object",
                      "properties": {"goal": {"type": "string"},
                                     "budget_tokens": {"type": "integer"},
                                     "wall_ms": {"type": "integer"}},
                      "required": ["goal"]}},
    },
    "required": ["subtasks"],
}


class SpawnTool:
    """LLM 可调用的派生工具(§3.4 spawn:guest 不可见——risk_level R1)。"""

    name = "spawn_subagents"
    description = ("并行派生子智能体执行独立的子任务,等全部完成后返回各自结论。"
                   "适用于可拆分的调研/生成类工作(如'分别调研A、B、C再汇总')。"
                   "每个子任务可给 budget_tokens(默认父预算的 40%)与 wall_ms(默认 120000)。"
                   "子任务应互不依赖;汇总综合由你自己在收到结果后完成。")
    parameters = _SPAWN_PARAMETERS
    side_effect = "write"
    risk_level = 1  # §3.2 spawn:owner 默认 / known depth≤1 / guest ❌

    def __init__(self, spawner: SubAgentSpawner):
        self._spawner = spawner

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raw: Any = args.get("subtasks")
        if not isinstance(raw, list) or not raw:
            raise SpawnRejected("subtasks 必须为非空数组")
        return await self._spawner.spawn_many(cast("list[dict[str, Any]]", raw))
