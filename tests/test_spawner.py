"""M3-1 tests: sub-agent spawner — authorization algebra, depth policy,
wall-clock kill & reclamation, spawn-tree properties, gateway integration."""

import asyncio
import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from naga import kernel, storage
from naga.agent import AgentResult
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.spawner import (
    SPAWN_DEPTH_HARD_CAP,
    SPAWN_DEPTH_SOFT_LIMIT,
    SpawnRejected,
    SpawnTool,
    SubAgentSpawner,
)


def _spawner(tmp_path, *, trust="owner", budget=100_000, depth=0,
             adapter_cls=None, raw_tools=None):
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    kern = kernel.Kernel(ledger=led)
    make_adapter = (lambda: adapter_cls()) if adapter_cls else lambda: _IdleAdapter()
    sp = SubAgentSpawner(ledger=led, ctx=kern.root, make_adapter=make_adapter,
                         raw_tools=raw_tools or [], skill_store=None,
                         session_key="s", actor_trust=trust,
                         budget=kernel.Budget(budget), depth=depth,
                         parent_trace="tr_parent")
    return sp, led


class _IdleAdapter:
    """One-shot answer, minimal tokens."""

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="子任务完成", prompt_tokens=10, completion_tokens=10)


class _ScriptedAdapter:
    """Pops scripted ChatResponses (same shape as other suites)."""

    def __init__(self, script: list[dict[str, Any]]):
        self._script = list(script)

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        item = self._script.pop(0) if self._script else {
            "content": "(剧本耗尽)", "prompt_tokens": 1, "completion_tokens": 1}
        return ChatResponse(**item)


class _HangAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        await asyncio.sleep(30)
        return ChatResponse(content="不该到这", prompt_tokens=1, completion_tokens=1)


class _ReadTool:
    name, description, parameters = "web_search", "d", {"type": "object"}
    side_effect, risk_level = "read", 0


class _WriteTool:
    name, description, parameters = "send_mail", "d", {"type": "object"}
    side_effect, risk_level = "write", 2


# ---- 不变式①③ + 深度策略 ----

def test_child_budget_strictly_less_and_ratchets(tmp_path):
    sp, led = _spawner(tmp_path, budget=100_000)

    async def scenario():
        before = sp.budget.remaining
        out = await sp.spawn_many([{"goal": "g"}])
        return before, out

    before, out = asyncio.run(scenario())
    child = out["subagents"][0]
    assert child["outcome"] == "success"
    assert child["tokens"] == 20  # 10 prompt + 10 completion
    # 树上账:子的实际消耗留在父账,未用份额归还 → 无锁死配额
    assert sp.budget.remaining == before - child["tokens"]
    assert sp.budget.used == child["tokens"]
    led.close()


def test_budget_must_be_strictly_less(tmp_path):
    sp, led = _spawner(tmp_path, budget=100)

    async def scenario():
        with pytest.raises(SpawnRejected, match="配额不足"):
            await sp.spawn_many([{"goal": "g", "budget_tokens": 100}])  # 不小于剩余
        with pytest.raises(SpawnRejected):
            await sp.spawn_many([{"goal": "g", "budget_tokens": 99},
                                 {"goal": "g2", "budget_tokens": 99}])  # 第二个必超
        return sp.budget.remaining

    assert asyncio.run(scenario()) == 100  # 失败授权不留账:配额原封不动
    led.close()


def test_depth_policy_soft_hard_known(tmp_path):
    soft, led = _spawner(tmp_path, depth=SPAWN_DEPTH_SOFT_LIMIT)
    hard, led2 = _spawner(tmp_path, depth=SPAWN_DEPTH_HARD_CAP)
    known_child, led3 = _spawner(tmp_path, trust="known", depth=1)

    async def scenario():
        results = []
        for sp in (soft, hard, known_child):
            with pytest.raises(SpawnRejected):
                await sp.spawn_many([{"goal": "g"}])
            results.append(sp.budget.remaining)
        return results

    budget_before = [sp.budget for sp in (soft, hard, known_child)]
    remain = asyncio.run(scenario())
    assert all(r == b.total for r, b in zip(remain, budget_before))  # 拒绝零划转
    led.close(); led2.close(); led3.close()


def test_depth_within_limits_allowed(tmp_path):
    sp, led = _spawner(tmp_path, depth=SPAWN_DEPTH_SOFT_LIMIT - 1)
    out = asyncio.run(sp.spawn_many([{"goal": "g"}]))
    assert out["subagents"][0]["outcome"] == "success"
    led.close()


# ---- 不变式②:工具集 ⊆(R 过滤 + 派生工具可见性) ----

def test_toolset_subset_and_spawn_tool_visibility(tmp_path):
    tools = [_ReadTool(), _WriteTool()]
    owner_sp, led = _spawner(tmp_path, raw_tools=tools, trust="owner")
    guest_sp, led2 = _spawner(tmp_path, raw_tools=tools, trust="guest")
    known_sp, led3 = _spawner(tmp_path, raw_tools=tools, trust="known")

    owner_tools = [t.name for t in owner_sp.make_tools()]
    assert owner_tools == ["web_search", "send_mail", "spawn_subagents"]
    guest_tools = [t.name for t in guest_sp.make_tools()]
    assert "send_mail" not in guest_tools and "spawn_subagents" not in guest_tools
    known_tools = [t.name for t in known_sp.make_tools()]
    assert known_tools == ["web_search", "spawn_subagents"]  # R1:只见派生,不见写类
    # 深度硬顶处不再挂派生工具(结构性无孙代)
    deep_sp, led4 = _spawner(tmp_path, depth=SPAWN_DEPTH_HARD_CAP - 1)
    assert "spawn_subagents" in [t.name for t in deep_sp.make_tools()]
    led.close(); led2.close(); led3.close(); led4.close()


def test_spawn_tool_is_r1_guest_invisible():
    assert SpawnTool.risk_level == 1  # §3.2:guest ❌ / known depth≤1 / owner 默认


# ---- wall-clock kill 与回收无残留 ----

def test_wall_clock_kill_reclaims_budget(tmp_path):
    sp, led = _spawner(tmp_path, adapter_cls=_HangAdapter, budget=100_000)

    async def scenario():
        before = sp.budget.remaining
        out = await sp.spawn_many([{"goal": "挂死任务", "budget_tokens": 40_000,
                                    "wall_ms": 200}])
        return before, out

    before, out = asyncio.run(scenario())
    child = out["subagents"][0]
    assert child["outcome"] == "killed" and "wall-clock" in str(child["failure"])
    assert child["tokens"] == 0
    assert sp.budget.remaining == before  # kill 归还全部未用份额(消耗为零)
    rows = led.query("SELECT kind FROM ledger_entries WHERE kind LIKE 'agent.%' "
                     "ORDER BY seq")
    assert [r[0] for r in rows] == ["agent.spawned", "agent.terminated"]  # 生死都入账
    led.close()


def test_child_budget_exhaustion_degrades(tmp_path):
    """子任务预算耗尽 → outcome aborted,父代不受影响。"""

    class _GreedyAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(content="话痨", prompt_tokens=10,
                                completion_tokens=90_000)

    sp, led = _spawner(tmp_path, adapter_cls=_GreedyAdapter, budget=100_000)
    out = asyncio.run(sp.spawn_many([{"goal": "g", "budget_tokens": 50_000}]))
    child = out["subagents"][0]
    assert child["outcome"] == "aborted"
    # 子预算耗尽:consume 先校验后扣账 → 已耗部分留在父账,未用归还
    assert sp.budget.remaining == 100_000 - child["tokens"]
    led.close()


# ---- 网关全链路:父任务经工具派生并发子任务 ----

def _spawn_call(subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"content": None, "prompt_tokens": 5, "completion_tokens": 5,
            "tool_calls": [{"id": "c1", "name": "spawn_subagents",
                            "arguments": json.dumps({"subtasks": subtasks})}]}


def test_gateway_parent_spawns_children_end_to_end(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    calls = {"n": 0}

    class _RouterAdapter:
        """调用序确定性路由:1=父派生,2/3=并发子任务,4=父汇总。"""

        async def chat(self, messages, tools=None, *, max_tokens=1024):
            calls["n"] += 1
            n = calls["n"]
            if n == 1:
                return ChatResponse(**_spawn_call([{"goal": "调研A"}, {"goal": "调研B"}]))
            if n in (2, 3):
                return ChatResponse(content="child-done", prompt_tokens=3,
                                    completion_tokens=3)
            return ChatResponse(content="A与B的汇总结论", prompt_tokens=5,
                                completion_tokens=5)

    gw = Gateway(led, make_adapter=lambda: _RouterAdapter(), home=home)
    result = asyncio.run(gw.run_agent(
        session_key="wechat:dm:p", goal="分别调研A和B再汇总",
        budget=200_000, trace_id="tr_m3", actor_trust="owner"))
    assert result.outcome == "success" and result.reply == "A与B的汇总结论"
    rows = led.query("SELECT kind FROM ledger_entries WHERE kind LIKE 'agent.%' "
                     "ORDER BY seq")
    from collections import Counter
    assert Counter(r[0] for r in rows) == {"agent.spawned": 2, "agent.terminated": 2}
    led.close()


def test_gateway_guest_cannot_spawn(tmp_path):
    """guest 的工具集里根本没有派生工具(结构性封死,§3.2 spawn ❌)。"""
    from naga.gateway import tools_for_trust

    class _FakeSpawn:
        name, description, parameters = "spawn_subagents", "d", {}
        risk_level = 1

    assert tools_for_trust("guest", [_FakeSpawn()]) == []
    assert [t.name for t in tools_for_trust("known", [_FakeSpawn()])] == \
        ["spawn_subagents"]


# ---- 随机派生树性质(无残留/必终止) ----

@given(st.lists(st.integers(min_value=1, max_value=90), min_size=1, max_size=6))
@settings(max_examples=40, deadline=None)
def test_random_child_charges_always_reclaimed(tmp_path_factory, spends):
    """任意子任务消耗模式:终止后父账 = 划转-归还 ⇒ 恢复原状(无残留)。"""
    tmp = tmp_path_factory.mktemp("m3pbt")
    home = str(tmp)
    storage.init_home(home)
    led = DurableLedger(home)
    kern = kernel.Kernel(ledger=led)

    idx = {"i": 0}

    class _SpendAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            i = idx["i"] = idx.get("i", 0) + 1
            spend = spends[(i - 1) % len(spends)]
            return ChatResponse(content="ok", prompt_tokens=1,
                                completion_tokens=spend)

    parent_budget = kernel.Budget(10_000)
    sp = SubAgentSpawner(ledger=led, ctx=kern.root, make_adapter=lambda: _SpendAdapter(),
                         raw_tools=[], skill_store=None, session_key="s",
                         actor_trust="owner", budget=parent_budget, depth=0,
                         parent_trace="tr_p")
    before = parent_budget.remaining

    async def scenario():
        tasks = [{"goal": f"g{i}", "budget_tokens": 100 + spends[i % len(spends)]}
                 for i in range(len(spends))]
        return await sp.spawn_many(tasks)

    out = asyncio.run(scenario())
    consumed = sum(c["tokens"] for c in out["subagents"])
    assert parent_budget.remaining == before - consumed  # 无锁死配额:消耗留下,余量归还
    led.close()


def test_spawn_depth_ladder(tmp_path):
    """深度阶梯:depth 0/1/2 可派生,depth 3 触软限拒绝(owner 审批表 M4);
    can_spawn 与 spawn_many 的判定一致。"""
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    kern = kernel.Kernel(ledger=led)

    def at(depth: int, budget: kernel.Budget) -> SubAgentSpawner:
        return SubAgentSpawner(ledger=led, ctx=kern.root,
                               make_adapter=lambda: _IdleAdapter(), raw_tools=[],
                               skill_store=None, session_key="s", actor_trust="owner",
                               budget=budget, depth=depth, parent_trace="tr_x")

    async def scenario():
        budget = kernel.Budget(10_000_000)
        outcomes = []
        for depth in range(SPAWN_DEPTH_SOFT_LIMIT + 1):
            sp = at(depth, budget)
            if depth < SPAWN_DEPTH_SOFT_LIMIT:
                out = await sp.spawn_many([{"goal": "d"}])
                outcomes.append(out["subagents"][0]["outcome"])
                budget = kernel.Budget(1_000_000)  # 下一层父预算(独立节点)
            else:
                with pytest.raises(SpawnRejected, match="软限"):
                    await sp.spawn_many([{"goal": "d"}])
        return outcomes

    assert asyncio.run(scenario()) == ["success"] * SPAWN_DEPTH_SOFT_LIMIT
    assert at(SPAWN_DEPTH_SOFT_LIMIT, kernel.Budget(100)).can_spawn() is True
    assert at(SPAWN_DEPTH_HARD_CAP, kernel.Budget(100)).can_spawn() is False
    led.close()


def test_agentresult_rejected_shape_unchanged():
    r = AgentResult(reply="x", outcome="rejected", trace_id="t", steps=0,
                    tokens=0, cost_cents=0)
    assert r.outcome == "rejected"
