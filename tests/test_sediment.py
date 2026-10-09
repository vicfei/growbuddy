"""技能沉淀复用闭环测试(M1 出口③):触发阈值 / local 分支 / 复用注入 /
skill.use 计量 / 投影重建确定性 / 低于阈值不沉淀。"""

import asyncio
import json
import os
from typing import ClassVar

from naga import kernel
from naga.agent import AgentLoop
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.skills import SkillStore

_DISTILL = ("id: multi-topic-research\n## 何时使用\n多主题 调研 竞品 对比\n"
            "## 步骤\n1. 逐主题搜索\n2. 汇总对比\n## 已知陷阱\n- 主题混搜易漏")


def _tc(i: int) -> dict:
    return {"id": f"c{i}", "name": "web_search", "arguments": '{"query": "q"}'}


class _Tool:
    name = "web_search"
    description = "t"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    async def run(self, args):
        return {"results": []}


class _Fake:
    def __init__(self, script):
        self._script = list(script)
        self.seen: list[list[dict]] = []

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        self.seen.append([dict(m) for m in messages])
        return self._script.pop(0)


def test_sediment_then_reuse_in_same_session(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    store = SkillStore(led)
    k = kernel.Kernel(ledger=led)  # type: ignore[arg-type]

    # 任务一:5 次工具调用 + 终答 + 蒸馏应答 → 触发沉淀
    adapter = _Fake([
        ChatResponse(tool_calls=[_tc(1), _tc(2)], prompt_tokens=1, completion_tokens=1),
        ChatResponse(tool_calls=[_tc(3), _tc(4)], prompt_tokens=1, completion_tokens=1),
        ChatResponse(tool_calls=[_tc(5)], prompt_tokens=1, completion_tokens=1),
        ChatResponse(content="五个主题对比完成", prompt_tokens=1, completion_tokens=1),
        ChatResponse(content=_DISTILL, prompt_tokens=2, completion_tokens=2),
    ])
    a1 = AgentLoop(ledger=led, ctx=k.root, budget=kernel.Budget(10_000),
                   adapter=adapter, tools=[_Tool()], skill_store=store,
                   session_key="webchat:dm:owner")
    r1 = asyncio.run(a1.run("多主题 竞品 调研 对比"))
    assert r1.outcome == "success" and r1.reply == "五个主题对比完成"

    rows = led.query("SELECT name, branch, provenance FROM skill_nodes WHERE branch LIKE 'local:%'")
    assert len(rows) == 1 and rows[0][0] == "multi-topic-research"
    assert json.loads(rows[0][2])["source"] == "agent"

    # 任务二(同会话):命中注入 + 计量
    adapter2 = _Fake([ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)])
    a2 = AgentLoop(ledger=led, ctx=kernel.Kernel(ledger=led).root,  # type: ignore[arg-type]
                   budget=kernel.Budget(10_000), adapter=adapter2,
                   skill_store=store, session_key="webchat:dm:owner")
    r2 = asyncio.run(a2.run("再来一次 竞品 调研"))
    assert r2.outcome == "success"
    assert "skill://multi-topic-research@v1" in adapter2.seen[0][0]["content"]  # 系统提示注入
    traces = [e["payload"] for e in led.entries if e["kind"] == "trace.record"]
    assert traces[-1]["skills_loaded"] == ["skill://multi-topic-research@v1"]
    kinds = [e["kind"] for e in led.entries]
    assert "skill.use" in kinds

    metrics = json.loads(led.query(
        "SELECT metrics_json FROM skill_nodes WHERE name='multi-topic-research'")[0][0])
    assert metrics["uses"] == 1 and "lastUsed" in metrics

    # 投影重建确定性(含 skill.use 的 metrics 更新)
    rows_all = led.query("SELECT cas_id, name, metrics_json FROM skill_nodes ORDER BY cas_id")
    led.close()
    os.remove(os.path.join(home, "ledger", "index.sqlite"))
    led2 = DurableLedger.open(home)
    assert led2.query("SELECT cas_id, name, metrics_json FROM skill_nodes "
                      "ORDER BY cas_id") == rows_all
    led2.close()


def test_no_sediment_below_threshold(tmp_path):
    led = DurableLedger(str(tmp_path / "home"))
    store = SkillStore(led)
    adapter = _Fake([
        ChatResponse(tool_calls=[_tc(1)], prompt_tokens=1, completion_tokens=1),
        ChatResponse(content="done", prompt_tokens=1, completion_tokens=1),
    ])
    agent = AgentLoop(ledger=led, ctx=kernel.Kernel(ledger=led).root,  # type: ignore[arg-type]
                      budget=kernel.Budget(10_000), adapter=adapter,
                      tools=[_Tool()], skill_store=store, session_key="s")
    r = asyncio.run(agent.run("小任务"))
    assert r.outcome == "success"
    assert len(adapter.seen) == 2  # 无蒸馏调用
    assert led.query("SELECT COUNT(*) FROM skill_nodes")[0][0] == 0
    led.close()


def test_cross_session_local_skill_not_visible(tmp_path):
    """§6.4 默认局部:local:{session} 技能对他会话不可见。"""
    led = DurableLedger(str(tmp_path / "home"))
    store = SkillStore(led)
    store.commit("private", "---\nname: private\n---\n调研 专属 技能\n",
                 {"source": "agent"}, branch="local:session-a")
    adapter = _Fake([ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)])
    agent = AgentLoop(ledger=led, ctx=kernel.Kernel(ledger=led).root,  # type: ignore[arg-type]
                      budget=kernel.Budget(1000), adapter=adapter,
                      skill_store=store, session_key="session-b")
    asyncio.run(agent.run("帮我 调研"))
    trace = [e["payload"] for e in led.entries if e["kind"] == "trace.record"][-1]
    assert trace["skills_loaded"] == []  # session-b 看不到 session-a 的局部技能
    led.close()
