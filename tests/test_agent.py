"""Agent Loop 全链路测试(FakeLLM/FakeTool 驱动,零外网)+ 真实烟测(门控)。"""

import asyncio
import json
import os
from typing import Any, ClassVar

import pytest

from naga import kernel
from naga.agent import AgentLoop
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.skills import SkillStore

CRED_PATH = os.path.join(os.path.expanduser("~"), ".samsara", "credentials", "providers.json")


class FakeAdapter:
    """脚本化 LLM:按序弹出预设响应;记录收到的 messages 供断言。"""

    def __init__(self, script: list[ChatResponse]):
        self._script = list(script)
        self.seen: list[list[dict]] = []

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        self.seen.append([dict(m) for m in messages])
        return self._script.pop(0)


class FakeTool:
    name = "web_search"
    description = "测试工具"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"]}

    def __init__(self, result=None, error=None):
        self.calls: list[dict] = []
        self._result = result or {"results": [{"name": "stub", "url": "https://x", "summary": "s"}]}
        self._error = error

    async def run(self, args):
        self.calls.append(args)
        if self._error:
            raise RuntimeError(self._error)
        return self._result


def _agent(led, adapter, tools, budget=10_000, max_steps=8, store=None):
    k = kernel.Kernel(ledger=led)
    return AgentLoop(ledger=led, ctx=k.root, budget=kernel.Budget(budget),
                     adapter=adapter, tools=tools, skill_store=store,
                     max_steps=max_steps)


def test_full_chain_tool_then_reply(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    tool = FakeTool()
    adapter = FakeAdapter([
        ChatResponse(content=None, tool_calls=[
            {"id": "c1", "name": "web_search", "arguments": '{"query": "naga"}'}],
            prompt_tokens=10, completion_tokens=20),
        ChatResponse(content="调研完成:Naga 是一个内核项目。", prompt_tokens=30, completion_tokens=40),
    ])
    agent = _agent(led, adapter, [tool])
    result = asyncio.run(agent.run("调研 naga 项目"))

    assert result.outcome == "success" and "内核" in (result.reply or "")
    assert result.tokens == 100 and result.steps == 3  # llm + tool + llm
    assert tool.calls == [{"query": "naga"}]

    kinds = [e["kind"] for e in led.entries]
    assert "effect.apply_begin" in kinds and "effect.apply_ok" in kinds \
        and "trace.record" in kinds
    trace_row = led.query("SELECT trace_id, outcome, tokens, replay_bundle_cas FROM traces")[0]
    assert trace_row[1] == "success" and trace_row[2] == 100
    bundle = json.loads(led.cas.get(trace_row[3]))
    tool_step = next(s for s in bundle["steps"] if s["kind"] == "tool")
    assert tool_step["args"]["data"] == {"query": "naga"}
    assert tool_step["results"]["data"]["results"][0]["name"] == "stub"
    # 对话流完整性:tool 消息以 tool_call_id 回填
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1"
               for m in adapter.seen[-1])
    led.close()


def test_budget_exhaustion_aborts(tmp_path):
    led = DurableLedger(str(tmp_path / "home"))
    adapter = FakeAdapter([ChatResponse(content="x", prompt_tokens=700, completion_tokens=500)])
    agent = _agent(led, adapter, [], budget=1000)
    result = asyncio.run(agent.run("任务"))
    assert result.outcome == "aborted" and result.failure_analysis == "预算耗尽"
    assert led.query("SELECT outcome FROM traces")[0][0] == "aborted"
    led.close()


def test_max_steps_aborts(tmp_path):
    led = DurableLedger(str(tmp_path / "home"))
    tc = {"id": "c", "name": "web_search", "arguments": '{"query": "q"}'}
    adapter = FakeAdapter([ChatResponse(tool_calls=[tc], prompt_tokens=1, completion_tokens=1)
                           for _ in range(3)])
    agent = _agent(led, adapter, [FakeTool()], max_steps=2)
    result = asyncio.run(agent.run("任务"))
    assert result.outcome == "aborted" and result.failure_analysis == "步数上限"
    led.close()


def test_tool_failure_feeds_error_back(tmp_path):
    led = DurableLedger(str(tmp_path / "home"))
    tool = FakeTool(error="boom")
    adapter = FakeAdapter([
        ChatResponse(tool_calls=[{"id": "c1", "name": "web_search", "arguments": "{}"}],
                     prompt_tokens=5, completion_tokens=5),
        ChatResponse(content="工具失败但任务继续", prompt_tokens=5, completion_tokens=5),
    ])
    agent = _agent(led, adapter, [tool])
    result = asyncio.run(agent.run("任务"))
    assert result.outcome == "success" and result.reply == "工具失败但任务继续"
    tool_msg = next(m for m in adapter.seen[-1] if m.get("role") == "tool")
    assert "boom" in tool_msg["content"]
    kinds = [e["kind"] for e in led.entries]
    assert "effect.apply_failed" in kinds
    led.close()


def test_skill_selection_via_keywords(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    store = SkillStore(led)
    store.commit("pricing", "---\nname: pricing\n---\n# 何时使用\n竞品定价 调研 任务。\n",
                 {"source": "human"})
    adapter = FakeAdapter([ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)])
    agent = _agent(led, adapter, [], store=store)
    result = asyncio.run(agent.run("帮我调研竞品定价"))
    assert result.outcome == "success"
    trace = next(e for e in led.entries if e["kind"] == "trace.record")["payload"]
    assert trace["skills_loaded"] == ["skill://pricing@v1"]
    bundle = json.loads(led.cas.get(trace["replay_bundle_cas"]))
    assert bundle["assets"]["skills"] == ["skill://pricing@v1"]
    led.close()


def test_secret_never_enters_bundle(tmp_path):
    led = DurableLedger(str(tmp_path / "home"))
    leak = {"results": [{"name": "d", "url": "https://x", "summary": "key sk-" + "a" * 30}]}
    adapter = FakeAdapter([
        ChatResponse(tool_calls=[{"id": "c1", "name": "web_search", "arguments": "{}"}],
                     prompt_tokens=1, completion_tokens=1),
        ChatResponse(content="done", prompt_tokens=1, completion_tokens=1),
    ])
    agent = _agent(led, adapter, [FakeTool(result=leak)])
    asyncio.run(agent.run("任务"))
    trace = next(e for e in led.entries if e["kind"] == "trace.record")["payload"]
    blob = led.cas.get(trace["replay_bundle_cas"]).decode("utf-8")
    assert "sk-" + "a" * 30 not in blob and "[REDACTED:secret]" in blob
    led.close()


@pytest.mark.skipif(not (os.environ.get("SAMSARA_SMOKE") and os.path.exists(CRED_PATH)),
                    reason="真实 API 烟测需 SAMSARA_SMOKE=1 且凭据仓存在")
def test_real_smoke_deepseek_and_bocha(tmp_path):
    from naga.llm import CredentialStore, DeepSeekAdapter
    from naga.tools import WebSearchTool

    async def scenario():
        creds = CredentialStore()
        adapter = DeepSeekAdapter.from_credentials(creds)
        resp = await adapter.chat([{"role": "user", "content": "回复两个字:收到"}],
                                  max_tokens=512)
        assert resp.content and "收到" in resp.content
        assert resp.completion_tokens > 0
        hits = await WebSearchTool(creds, count=2).run({"query": "Naga"})
        assert len(hits["results"]) >= 1
        return resp.completion_tokens, len(hits["results"])

    ct, n = asyncio.run(scenario())
    print(f"\nsmoke: completion_tokens={ct}, hits={n}")
