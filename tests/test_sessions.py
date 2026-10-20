"""会话注册表测试(§5 sessions,DM 冻结 DDL;Fake 注入零外网)。

时钟纪律:闲置判定走注入 now(账本 ts 不参与判定,无跨时钟问题)。
覆盖:投影生命周期(开/活/关/重开/重建,含生产存量事件回放)/
闲置关闭/收尾整合蒸馏(owner 与 guest 两态)/任务结果推送路由/
note_activity 的 job 会话排除与重开语义/CLI。
"""

import asyncio
import datetime
import json
from typing import Any

from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.memory import MemoryStore
from naga.sessions import (
    SessionRegistry,
    creator_session_of,
    job_notify_text,
    parse_session_channel,
)


def _led(tmp_path, name="home") -> DurableLedger:
    return DurableLedger(str(tmp_path / name))


class ScriptAdapter:
    def __init__(self, script: list[ChatResponse]):
        self._script = list(script)

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return self._script.pop(0)


def _open(led, key="wechat:dm:u1", trust="owner", **extra: Any) -> None:
    led.append("session.open", key, {"session_key": key, "channel":
                                     key.split(":")[0], "trust": trust, **extra})


def _trace(led, key="wechat:dm:u1", tid="t1") -> None:
    led.append("trace.record", key, {"trace_id": tid, "session_key": key,
                                     "outcome": "success", "cost": {}})


# ── 投影生命周期 ────────────────────────────────────────────────

def test_projection_lifecycle_and_rebuild(tmp_path):
    led = _led(tmp_path)
    _open(led)                      # 存量形态:无 lane_id
    _trace(led)
    rows = led.query("SELECT lane_id, trust_level, state FROM sessions")
    assert rows[0] == ("legacy", "owner", "open")
    _trace(led, tid="t2")
    led.append("session.close", "wechat:dm:u1",
               {"session_key": "wechat:dm:u1", "reason": "idle"})
    assert led.query("SELECT state FROM sessions")[0][0] == "closed"
    _open(led, trust="owner", lane_id="ln_3")   # 重开=新一段对话
    assert led.query("SELECT state, lane_id FROM sessions")[0] == ("open", "ln_3")
    led.close()

    led2 = DurableLedger.open(led.home)          # 投影自账本重建
    assert led2.query("SELECT lane_id, trust_level, state FROM sessions") \
        == [("ln_3", "owner", "open")]
    led2.close()


def test_last_active_only_touches_open_rows(tmp_path):
    led = _led(tmp_path)
    _open(led, key="webchat:dm:x")
    led.append("session.close", "x", {"session_key": "webchat:dm:x"})
    _trace(led, key="webchat:dm:x", tid="late")  # 关闭后的轨迹不动 last_active
    row = led.query("SELECT created_at, last_active_at FROM sessions")[0]
    assert row[0] == row[1]
    led.close()


# ── 闲置关闭 ────────────────────────────────────────────────────

def test_close_idle_synthetic_clock(tmp_path):
    led = _led(tmp_path)
    clock = {"t": datetime.datetime.now(datetime.UTC)}

    def now() -> datetime.datetime:
        return clock["t"]

    _open(led, key="wechat:dm:a")
    _trace(led, key="wechat:dm:a")
    _open(led, key="wechat:dm:b")
    _trace(led, key="wechat:dm:b")
    reg = SessionRegistry(led, now=now)
    assert reg.close_idle() == []               # 刚活跃:不关

    # 回拨 b 的 last_active(直接 UPDATE 投影行,等价于历史闲置)
    old = (now() - datetime.timedelta(minutes=90)).isoformat()
    led.execute("UPDATE sessions SET last_active_at=? WHERE session_key=?",
                (old, "wechat:dm:b"))
    closed = reg.close_idle()
    assert closed == ["wechat:dm:b"]            # 只关闲置者
    assert led.query("SELECT state FROM sessions WHERE session_key='wechat:dm:a'"
                     )[0][0] == "open"
    events = [e for e in led.entries if e["kind"] == "session.close"]
    assert events and events[0]["payload"]["reason"] == "idle"
    led.close()


# ── 收尾整合蒸馏(§6.5 会话收尾形态) ───────────────────────────

def test_consolidate_owner_writes_note_and_facts(tmp_path):
    led = _led(tmp_path)
    ms = MemoryStore(led, home=led.home)
    _open(led, key="wechat:dm:u1", trust="owner")
    for i, text in enumerate(["查了上海天气,得知降温",
                              "查了东京天气,得知下雨",
                              "总结了天气对比"]):
        ms.write("episodic", "wechat:dm:u1", text, {"source": "agent",
                                                    "trace_id": f"t{i}"},
                 actor_trust="owner")
    adapter = ScriptAdapter([
        ChatResponse(content="[E] 本段对话围绕上海与东京天气查询与对比\n"
                             "[S] 用户关注两地天气对比",
                     prompt_tokens=1, completion_tokens=1)])
    reg = SessionRegistry(led, memory=ms, make_adapter=lambda: adapter)
    out = asyncio.run(reg.consolidate("wechat:dm:u1"))
    assert out == {"episodic": 1, "semantic": 1}
    prov = json.loads(ms.list_items(layer="semantic")[0][7])
    assert prov["source"] == "session-close"
    note = [r for r in ms.list_items(layer="episodic")
            if json.loads(r[7])["source"] == "session-close"]
    assert note and led.verify() is True
    led.close()


def test_consolidate_guest_drops_semantic(tmp_path):
    led = _led(tmp_path)
    ms = MemoryStore(led, home=led.home)
    _open(led, key="wechat:dm:g1", trust="guest")
    ms.write("episodic", "wechat:dm:g1", "guest 的事件", {"source": "agent"},
             actor_trust="guest")
    adapter = ScriptAdapter([
        ChatResponse(content="[E] guest 会话纪要\n[S] 不该落库的事实",
                     prompt_tokens=1, completion_tokens=1)])
    reg = SessionRegistry(led, memory=ms, make_adapter=lambda: adapter)
    out = asyncio.run(reg.consolidate("wechat:dm:g1"))
    assert out == {"episodic": 1, "semantic": 0}   # 闸门:guest 的 [S] 拒绝
    led.close()


def test_consolidate_empty_session_no_llm_call(tmp_path):
    led = _led(tmp_path)
    ms = MemoryStore(led, home=led.home)
    _open(led, key="wechat:dm:u1")
    called: list[int] = []

    class Counting:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            called.append(1)
            raise AssertionError("不应调用 LLM")

    reg = SessionRegistry(led, memory=ms, make_adapter=lambda: Counting())
    out = asyncio.run(reg.consolidate("wechat:dm:u1"))
    assert out == {"episodic": 0, "semantic": 0} and called == []
    led.close()


# ── note_activity(gateway)与 job 会话排除 ──────────────────────

def test_note_activity_open_and_reopen(tmp_path):
    async def scenario() -> None:
        adapter = ScriptAdapter([
            ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1),
            ChatResponse(content="ok too", prompt_tokens=1, completion_tokens=1),
            ChatResponse(content="job done", prompt_tokens=1, completion_tokens=1)])
        led = _led(tmp_path, "gw")
        gw = Gateway(led, make_adapter=lambda: adapter)
        await gw.start("127.0.0.1", 0)
        try:
            await gw.run_agent(session_key="webchat:dm:owner", goal="hi",
                               budget=5000, trace_id="tr_s1")
            rows = led.query("SELECT session_key, state FROM sessions")
            assert rows == [("webchat:dm:owner", "open")]     # webchat 首开
            opens = [e for e in led.entries if e["kind"] == "session.open"]
            assert len(opens) == 1 and opens[0]["payload"]["lane_id"].startswith("ln_")
            await gw.run_agent(session_key="webchat:dm:owner", goal="again",
                               budget=5000, trace_id="tr_s2")
            assert len([e for e in led.entries if e["kind"] == "session.open"]) == 1
            # job 会话不入注册表
            await gw.run_agent(session_key="job:job_x", goal="run",
                               budget=5000, trace_id="tr_s3")
            assert led.query("SELECT COUNT(*) FROM sessions")[0][0] == 1
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())


def test_note_activity_reopens_closed(tmp_path):
    async def scenario() -> None:
        adapter = ScriptAdapter([
            ChatResponse(content="a", prompt_tokens=1, completion_tokens=1),
            ChatResponse(content="b", prompt_tokens=1, completion_tokens=1)])
        led = _led(tmp_path, "gwr")
        gw = Gateway(led, make_adapter=lambda: adapter)
        await gw.run_agent(session_key="webchat:dm:o", goal="一",
                           budget=5000, trace_id="tr_r1")
        led.append("session.close", "webchat:dm:o",
                   {"session_key": "webchat:dm:o", "reason": "idle"})
        await gw.run_agent(session_key="webchat:dm:o", goal="二",
                           budget=5000, trace_id="tr_r2")
        opens = [e for e in led.entries if e["kind"] == "session.open"]
        assert len(opens) == 2                     # 重开=新一段对话
        assert opens[1]["payload"]["trust_source"] == "runtime-reopen"
        led.close()

    asyncio.run(scenario())


# ── 任务结果推送路由 ────────────────────────────────────────────

class FakeWeChatChannel:
    is_running = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_reply(self, peer: str, text: str,
                         _token: str | None) -> Any:
        self.sent.append((peer, text))


def test_notify_session_routes_to_bound_channel(tmp_path):
    async def scenario() -> None:
        adapter = ScriptAdapter([])
        led = _led(tmp_path, "notify")
        gw = Gateway(led, make_adapter=lambda: adapter)
        ch = FakeWeChatChannel()
        gw.register_binding_channel("peer_1", ch)      # peer==binder
        assert await gw.notify_session("wechat:dm:peer_1", "任务结果") is True
        assert ch.sent == [("peer_1", "任务结果")]
        # 未知会话/webchat:不投递
        assert await gw.notify_session("webchat:dm:owner", "x") is False
        ch2 = FakeWeChatChannel()                        # 第二绑定在位:无回落
        gw.register_binding_channel("binder_2", ch2)
        assert await gw.notify_session("wechat:dm:nobody", "x") is False
        assert ch.sent == [("peer_1", "任务结果")] and ch2.sent == []
        led.close()

    asyncio.run(scenario())


def test_notify_session_single_channel_fallback(tmp_path):
    async def scenario() -> None:
        adapter = ScriptAdapter([])
        led = _led(tmp_path, "notify1")
        gw = Gateway(led, make_adapter=lambda: adapter)
        ch = FakeWeChatChannel()
        gw.register_binding_channel("唯一绑定", ch)
        ok = await gw.notify_session("wechat:dm:任意peer", "结果")
        assert ok is True and ch.sent and ch.sent[0][0] == "任意peer"
        led.close()

    asyncio.run(scenario())


def test_helpers():
    assert parse_session_channel("wechat:dm:u1") == ("wechat", "u1")
    assert parse_session_channel("webchat:dm:owner") == ("webchat", "owner")
    assert creator_session_of({"channel": "wechat", "peer": "p"}) == "wechat:dm:p"
    assert creator_session_of(None) is None
    text = job_notify_text("job_abc123", "success", "长" * 1000)
    assert text.startswith("[定时任务 abc123] success") and "…" in text


# ── CLI(只读直查) ──────────────────────────────────────────────

def test_cli_session_list(tmp_path, capsys):
    from naga.cli import main

    led = _led(tmp_path, "cli")
    _open(led, key="wechat:dm:u1")
    _trace(led, key="wechat:dm:u1")
    led.close()
    home = led.home
    assert main(["--home", home, "session", "list"]) == 0
    out = capsys.readouterr().out
    assert "wechat:dm:u1" in out and "[owner/open]" in out
    assert main(["--home", home, "session", "list", "--state", "closed"]) == 0
    assert "0 条" in capsys.readouterr().out
