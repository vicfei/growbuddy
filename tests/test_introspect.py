"""自省挂载测试(#95):agent 看见并操作自己的运行时。"""

import asyncio
import json
import os

from naga.gateway import Gateway, tools_for_trust
from naga.jobs import JobStore
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.system_tools import (
    JobCreateTool,
    SystemStatusTool,
    record_delivery,
    session_system_tools,
)


def _led(tmp_path, name="home") -> DurableLedger:
    return DurableLedger(str(tmp_path / name))


class FakeAdapter:
    def __init__(self, script: list[ChatResponse]):
        self._script = list(script)
        self.seen: list[list[dict]] = []

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        self.seen.append([dict(m) for m in messages])
        return self._script.pop(0)


def _tool_call(name: str, arguments: str) -> ChatResponse:
    return ChatResponse(content=None, prompt_tokens=1, completion_tokens=1,
                        tool_calls=[{"id": "c1", "name": name,
                                     "arguments": arguments}])


# ── system_status ───────────────────────────────────────────────

def test_system_status_owner_vs_guest(tmp_path):
    led = _led(tmp_path)
    led.append("session.open", "u", {"session_key": "wechat:dm:u",
                                     "trust": "owner"})
    JobStore(led).create(_job_spec())
    home = led.home
    record_delivery(os.path.join(home, "state", "notify-log.json"),
                    "wechat:dm:u", "晨报", "queued")
    owner = SystemStatusTool(led, home, "owner")
    out = asyncio.run(owner.run({}))
    assert out["jobs_active"] and out["jobs_active"][0]["cron"] == "0 7 * * *"
    assert out["notify_pending"] == {}
    assert out["recent_deliveries"][-1]["outcome"] == "queued"
    guest = SystemStatusTool(led, home, "guest")
    out_g = asyncio.run(guest.run({}))
    assert out_g["jobs_active"] == "requires known+ (10-matrix)"  # ops 细节过滤
    assert "recent_deliveries" not in out_g
    led.close()


def _job_spec():
    from naga.jobs import JobSpec
    return JobSpec(job_id="job_t1", schedule="0 7 * * *",
                   timezone="Asia/Shanghai", goal="摘要",
                   trust_snapshot="owner",
                   budget={"tokens": 1000, "wall_ms": 60000}, r_ceiling="R1")


def test_record_delivery_ring_cap(tmp_path):
    path = str(tmp_path / "notify-log.json")
    for i in range(60):
        record_delivery(path, "s", f"m{i}", "delivered")
    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)
    assert len(entries) == 50 and entries[-1]["text"] == "m59"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


# ── 工具信任门控 ───────────────────────────────────────────────

def test_job_tools_trust_ceiling():
    tools = session_system_tools(None, "/tmp", "webchat:dm:o", "owner")
    names = {t.name for t in tools}
    assert names == {"system_status", "job_create", "job_pause", "job_delete"}
    guest_visible = {t.name for t in tools_for_trust("guest", tools)}
    assert guest_visible == {"system_status"}   # R1 工具对 guest 不可见
    assert {t.name for t in tools_for_trust("owner", tools)} == names


# ── job_create(自然语言,走 agent 回路) ────────────────────────

def test_job_create_tool_from_natural_language(tmp_path):
    led = _led(tmp_path, "jc")

    class _Compile:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(
                content='{"cron": "30 8 * * *", "timezone": "Asia/Shanghai",'
                        ' "goal": "提醒喝水"}',
                prompt_tokens=1, completion_tokens=1)

    tool = JobCreateTool(led, "webchat:dm:owner", "owner",
                         make_adapter=lambda: _Compile())
    out = asyncio.run(tool.run({"text": "每天早上八点半提醒我喝水"}))
    assert out["job_id"] and out["cron"] == "30 8 * * *"
    row = JobStore(led).list_jobs()[0]
    assert row["trust_snapshot"] == "owner"
    events = led.entries[-1]
    assert events["payload"]["creator"] == {"channel": "webchat",
                                            "peer": "owner"}
    led.close()


def test_job_create_bad_cron_rejected(tmp_path):
    led = _led(tmp_path, "jcbad")
    tool = JobCreateTool(led, "webchat:dm:o", "owner")
    out = asyncio.run(tool.run({"cron": "not cron", "goal": "x"}))
    assert "error" in out and "JOBS.INVALID" in out["error"]
    led.close()


# ── 端到端:agent 自省回答(元事实+工具调用) ─────────────────────

def test_agent_introspects_via_gateway(tmp_path):
    async def scenario() -> None:
        led = _led(tmp_path, "e2e")
        JobStore(led).create(_job_spec())
        adapter = FakeAdapter([
            _tool_call("system_status", "{}"),
            ChatResponse(content="我有一个每天 07:00 的摘要任务,推送曾被拒"
                                 "入队,补发积压 0 条。",
                         prompt_tokens=1, completion_tokens=1)])
        gw = Gateway(led, make_adapter=lambda: adapter,
                     home=os.path.join(led.home))
        await gw.start("127.0.0.1", 0)
        try:
            result = await gw.run_agent(session_key="webchat:dm:owner",
                                        goal="你现在有哪些定时任务?为什么"
                                             "昨天没推送?",
                                        budget=8000, trace_id="tr_i1")
            assert result.outcome == "success"
            sys_prompt = adapter.seen[0][0]["content"]   # 系统提示含元事实
            assert "定时任务调度器" in sys_prompt and "system_status" in sys_prompt
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())


def test_agent_creates_job_when_asked(tmp_path):
    """名场面回放:'每天…提醒' → agent 自己建任务,不再说'我没有定时器'。"""
    async def scenario() -> None:
        led = _led(tmp_path, "e2ec")
        adapter = FakeAdapter([
            _tool_call("job_create",
                       '{"text": "每天早上七点给我一份三句话科技新闻摘要"}'),
            ChatResponse(content='{"cron": "0 7 * * *", "timezone":'
                                 ' "Asia/Shanghai", "goal": "科技新闻三句话"}',
                         prompt_tokens=1, completion_tokens=1),
            ChatResponse(content="已建好每天 07:00 的任务,结果会自动推送。",
                         prompt_tokens=1, completion_tokens=1)])
        gw = Gateway(led, make_adapter=lambda: adapter, home=led.home)
        await gw.start("127.0.0.1", 0)
        try:
            result = await gw.run_agent(
                session_key="webchat:dm:owner",
                goal="每天早上七点给我一份三句话科技新闻摘要",
                budget=8000, trace_id="tr_i2")
            assert result.outcome == "success"
            jobs = JobStore(led).list_jobs(state="active")
            assert jobs and jobs[0]["schedule"] == "0 7 * * *"
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())
