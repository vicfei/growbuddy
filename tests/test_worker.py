"""批次 5 测试:worker 隔离穿透(出口③)+ 渠道热插拔 unbind→rebind(出口①)。

worker 测试起真实子进程(python -m naga.worker)但全部走本地管道与测试
工具注册表(NAGA_WORKER_TEST_TOOLS=1),零外网。
"""

import asyncio
import sys
from typing import Any

import pytest

from naga import storage
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.wechat import WeChatChannel, WeChatCredential, load_credential
from naga.worker import WorkerTool

_ENV = {"NAGA_WORKER_TEST_TOOLS": "1"}


def _worker(name: str, call_timeout: float = 10.0) -> WorkerTool:
    return WorkerTool(name=name, description="测试工具", parameters={"type": "object"},
                      providers_path="/nonexistent/providers.json",  # web_search 注册自动跳过
                      side_effect="read", risk_level=0,
                      call_timeout=call_timeout, spawn_env=_ENV)


# ---- 出口③:worker 隔离穿透 ----

def test_worker_roundtrip():
    tool = _worker("echo")

    async def scenario():
        out = await tool.run({"x": 1, "中文": True})
        await tool.aclose()
        return out

    assert asyncio.run(scenario()) == {"echo": {"x": 1, "中文": True}}


def test_worker_child_crash_raises_value_error():
    """boom = 子进程 os._exit(1)(kill -9 语义):当次降级 ValueError,标记重建。"""
    tool = _worker("boom")

    async def scenario():
        try:
            with pytest.raises(ValueError, match="崩溃"):
                await tool.run({})
            crashed = tool._proc is None  # 崩溃已感知,待重建
        finally:
            await tool.aclose()
        return crashed

    assert asyncio.run(scenario()) is True


def test_worker_crash_degrades_and_rebuilds_new_pid():
    """子进程崩溃(boom = kill -9 语义)→ 当次 ValueError 降级 → 下次调用
    自动重建;两次崩溃对应两个不同子进程 pid(重建证明)。"""
    tool = _worker("boom")
    pids: list[int] = []
    orig_ensure = tool._ensure

    async def spy_ensure() -> Any:
        proc = await orig_ensure()
        pids.append(proc.pid)
        return proc

    tool._ensure = spy_ensure  # type: ignore[method-assign]

    async def scenario():
        try:
            with pytest.raises(ValueError, match="崩溃"):
                await tool.run({})
            with pytest.raises(ValueError, match="崩溃"):
                await tool.run({})  # 重建后再次崩溃——验证的是重建本身
        finally:
            await tool.aclose()

    asyncio.run(scenario())
    assert len(pids) == 2 and pids[0] != pids[1]  # 每次崩溃后都在新子进程重建


def test_worker_hang_timeout_kills():
    tool = _worker("hang", call_timeout=0.5)

    async def scenario():
        with pytest.raises(ValueError, match="超时"):
            await tool.run({})
        killed = tool._proc is None  # 挂死 worker 已被终止
        await tool.aclose()
        return killed

    assert asyncio.run(scenario()) is True


def test_worker_tool_error_and_unknown_tool_degrade():
    tool = _worker("fail")

    async def scenario():
        with pytest.raises(ValueError, match="业务失败"):
            await tool.run({})
        await tool.aclose()

    asyncio.run(scenario())

    unknown = _worker("no_such_tool")

    async def scenario2():
        with pytest.raises(ValueError, match="unknown tool"):
            await unknown.run({})
        await unknown.aclose()

    asyncio.run(scenario2())


def test_worker_child_exits_on_stdin_eof():
    """父进程退出 → 管道 EOF → 子进程干净自灭(无孤儿)。"""
    proc: Any = None

    async def scenario():
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "naga.worker", "/nonexistent/providers.json",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        assert proc.stdin is not None
        proc.stdin.close()
        return await asyncio.wait_for(proc.wait(), 15)

    assert asyncio.run(scenario()) == 0


class _ScriptedAdapter:
    """按剧本回放:先诱导工具调用,再给最终回答。"""

    def __init__(self, script: list[dict[str, Any]]):
        self._script = list(script)

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        item = self._script.pop(0) if self._script else {
            "content": "(剧本耗尽)", "prompt_tokens": 1, "completion_tokens": 1}
        return ChatResponse(**item)


def test_agent_task_and_lane_survive_worker_crash(tmp_path):
    """出口③穿透:子进程崩溃 → 工具步 ok=False → 任务继续成功作答;
    同 lane 后续任务照常执行(消费者存活)。"""
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    boom = _worker("boom")
    script = [
        {"content": None, "prompt_tokens": 1, "completion_tokens": 1,
         "tool_calls": [{"id": "c1", "name": "boom", "arguments": "{}"}]},
        {"content": "工具失败后继续作答", "prompt_tokens": 1, "completion_tokens": 1},
    ]
    gw = Gateway(led, make_adapter=lambda: _ScriptedAdapter(script), tools=[boom],
                 home=home)

    async def scenario():
        first = await gw.run_agent(session_key="wechat:dm:p", goal="g",
                                   budget=1000, trace_id="tr_b5a", actor_trust="owner")
        # 第二个任务走同一 gateway(车道消费者存活证明)
        echo = _worker("echo")
        gw._tools = [echo]
        second = await gw.run_agent(session_key="wechat:dm:p", goal="g2",
                                    budget=1000, trace_id="tr_b5b", actor_trust="owner")
        await boom.aclose()
        await echo.aclose()
        return first, second

    first, second = asyncio.run(scenario())
    assert first.outcome == "success" and first.reply == "工具失败后继续作答"
    assert second.outcome == "success"
    led.close()


def test_direct_tool_error_does_not_kill_task(tmp_path):
    """_call_tool 异常面加固:非 ValueError 网络错同样降级(ok=False 步)。"""
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)

    class NetErrorTool:
        name, description, parameters = "net", "d", {"type": "object"}
        side_effect, risk_level = "read", 0

        async def run(self, args):
            raise OSError("connection reset by peer")

    script = [
        {"content": None, "prompt_tokens": 1, "completion_tokens": 1,
         "tool_calls": [{"id": "c1", "name": "net", "arguments": "{}"}]},
        {"content": "降级后作答", "prompt_tokens": 1, "completion_tokens": 1},
    ]
    gw = Gateway(led, make_adapter=lambda: _ScriptedAdapter(script),
                 tools=[NetErrorTool()], home=home)

    async def scenario():
        return await gw.run_agent(session_key="s", goal="g", budget=1000,
                                  trace_id="tr_net", actor_trust="owner")

    result = asyncio.run(scenario())
    assert result.outcome == "success" and result.reply == "降级后作答"
    led.close()


def test_worker_tool_metadata_mirrors_risk(tmp_path):
    from naga.gateway import tool_risk
    from naga.tools import WebSearchTool

    wt = WorkerTool.of(WebSearchTool, providers_path="/nonexistent")
    assert wt.name == "web_search" and wt.risk_level == 0
    assert tool_risk(wt) == 0  # 元数据镜像后 R 档不丢


# ---- 出口①:渠道热插拔 unbind→rebind ----

class _RouteReq:
    """绑定管理面 fake:按 URL 分发,可循环两轮绑定;getupdates 带现实节拍。"""

    def __init__(self):
        self.round = 0

    async def __call__(self, url, method, headers, body, timeout):
        if "get_bot_qrcode" in url:
            self.round += 1
            return 200, {"ret": 0, "qrcode": f"Q{self.round}",
                         "qrcode_img_content": "http://qr"}
        if "get_qrcode_status" in url:
            return 200, {"ret": 0, "status": "confirmed",
                         "bot_token": f"bt-{self.round}", "ilink_user_id": f"iu-{self.round}",
                         "baseurl": "https://b"}
        await asyncio.sleep(0.05)  # 模拟长轮询节拍(零延迟会让轮询循环满转)
        return 200, {"ret": 0, "msgs": [], "get_updates_buf": "c"}


def test_unbind_then_rebind_hot_cycle(tmp_path):
    """出口①:bind(热启动)→ unbind(停渠道+清凭据)→ rebind(再次热启动,
    游标重置)——全程不重启 daemon。"""
    home = str(tmp_path / "h")
    storage.init_home(home)
    req = _RouteReq()

    async def runner(goal, session_key, actor, ctx):
        return None

    async def scenario():
        import aiohttp
        led = DurableLedger(home)
        gw = Gateway(led, make_adapter=lambda: _NoopAdapter(), home=home,
                     wechat_request=req)
        ch = WeChatChannel(home=home, runner=runner, request=req)
        gw.attach_wechat(ch)
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                    assert (await r.json())["qrcode"] == "Q1"
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/Q1") as r:
                    assert (await r.json())["status"] == "confirmed"
                assert ch.is_running                       # 第一轮热启动
                ch._cursor = "stale-from-old-bot"          # 模拟旧 bot 游标残留
                async with s.get(f"http://127.0.0.1:{port}/wechat/unbind") as r:
                    assert (await r.json())["ok"] is True
                assert not ch.is_running                   # 解绑即停
                assert load_credential(home) is None
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                    assert (await r.json())["qrcode"] == "Q2"
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/Q2") as r:
                    assert (await r.json())["status"] == "confirmed"
                assert ch.is_running                       # 第二轮热启动
                assert ch._cursor is None                  # 游标重置(新 bot)
                cred = load_credential(home)
                assert cred is not None and cred.bot_token == "bt-2"
        finally:
            ch.stop()
            await gw.stop()
            led.close()

    asyncio.run(scenario())

    # 账本:bound → unbound → bound(热插拔全程入账)
    led2 = DurableLedger.open(home)
    try:
        rows = led2.query(
            "SELECT kind FROM ledger_entries WHERE kind LIKE 'channel.%' ORDER BY seq")
        assert [r[0] for r in rows] == ["channel.bound", "channel.unbound", "channel.bound"]
    finally:
        led2.close()


class _NoopAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)


def test_channel_start_resets_cursor(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)

    async def runner(goal, session_key, actor, ctx):
        return None

    async def req(url, method, headers, body, timeout):
        return 200, {"ret": 0, "msgs": [], "get_updates_buf": "c"}

    async def scenario():
        ch = WeChatChannel(home=home, runner=runner, request=req)
        ch._cursor = "stale"
        ch.start(WeChatCredential(bot_token="t"))
        assert ch._cursor is None
        ch.stop()

    asyncio.run(scenario())
