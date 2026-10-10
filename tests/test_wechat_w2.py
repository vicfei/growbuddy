"""W2 测试:分段纯函数 / typing 编排(含降级) / 长回复渠道集成 / 首败即停。"""

import asyncio
from typing import Any

from naga import storage
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.wechat import ILinkClient, WeChatChannel, load_state, split_for_wechat


class _FakeReq:
    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url, method, headers, body, timeout):
        self.calls.append((url, dict(body) if body else {}))
        item = self._script.pop(0) if self._script else (200, {"ret": 0})
        if isinstance(item, Exception):
            raise item
        return item


class _CountingReq:
    """按 URL 计数的 fake(getconfig/sendtyping/getupdates/sendmessage)。"""

    def __init__(self):
        self.config_calls = 0
        self.typing_status: list[int] = []
        self.sends: list[dict] = []

    async def __call__(self, url, method, headers, body, timeout):
        if url.endswith("/getconfig"):
            self.config_calls += 1
            return 200, {"ret": 0, "typing_ticket": "tk-stable"}
        if url.endswith("/sendtyping"):
            self.typing_status.append(int(body.get("status", 0)))
            return 200, {"ret": 0}
        if url.endswith("/sendmessage"):
            self.sends.append(body["msg"])
            return 200, {"ret": 0}
        return 200, {"ret": 0, "msgs": [], "get_updates_buf": "c"}


# ---- 分段纯函数 ----

def test_split_short_single_no_marker():
    assert split_for_wechat("hello") == ["hello"]


def test_split_long_prefers_newline_and_adds_numbers():
    text = ("段落甲。" + "x" * 900 + "\n") * 5  # ~5000 字,含换行边界
    parts = split_for_wechat(text)
    assert len(parts) > 1
    assert all(len(p) <= 1900 + len(f"({len(parts)}/{len(parts)}) ") for p in parts)
    assert parts[0].startswith(f"(1/{len(parts)}) ")
    assert parts[-1].startswith(f"({len(parts)}/{len(parts)}) ")


def test_split_hard_cut_when_no_boundary():
    text = "z" * 5000  # 无任何边界 → 硬切
    parts = split_for_wechat(text)
    assert len(parts) == 3
    assert all(len(p) <= 1900 + 8 for p in parts)


# ---- typing 编排 ----

def test_typing_shows_during_runner_and_stops_after(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    counter = _CountingReq()

    async def runner(goal, session_key, actor, ctx):
        await asyncio.sleep(0.18)  # runner 期间 typing 周期刷新(测试 0.05s)

    async def scenario():
        ch = WeChatChannel(home=home, runner=runner, request=counter,
                           ledger_append=None, typing_refresh_sec=0.05)
        ch._client = ILinkClient("t", "https://x", counter)
        ch._state = load_state(home)
        # 直接走 _run_with_typing(不启动轮询循环)
        await ch._run_with_typing("goal", "wechat:dm:u1",
                                  {"kind": "human", "id": "u1", "trust": "owner"},
                                  {"trust_source": "allowlist"}, "ctx-1")
        await asyncio.sleep(0.05)  # 让 stop() 的 status=2 落定

    asyncio.run(scenario())
    assert counter.config_calls == 1                    # ticket 缓存只取一次
    assert 1 in counter.typing_status                   # runner 期间 status=1
    assert counter.typing_status[-1] == 2               # 结束补 status=2(非 0)
    assert counter.typing_status.count(1) >= 2          # 至少两次刷新(0.18s/0.05s)


def test_typing_degrades_silently_on_config_failure(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)

    class FailingConfigReq(_CountingReq):
        async def __call__(self, url, method, headers, body, timeout):
            if url.endswith("/getconfig"):
                self.config_calls += 1
                return 200, {"ret": -2}  # getconfig 失败
            if url.endswith("/sendmessage"):
                self.sends.append(body["msg"])
                return 200, {"ret": 0}
            return 200, {"ret": 0}

    counter = FailingConfigReq()

    async def runner(goal, session_key, actor, ctx):
        await asyncio.sleep(0.06)

    async def scenario():
        ch = WeChatChannel(home=home, runner=runner, request=counter,
                           typing_refresh_sec=0.02)
        ch._client = ILinkClient("t", "https://x", counter)
        ch._state = load_state(home)
        await ch._run_with_typing("g", "wechat:dm:u1",
                                  {"kind": "human", "id": "u1", "trust": "owner"},
                                  {"trust_source": "allowlist"}, None)
        await ch.send_reply("u1", "回复照常送达", "ctx")  # typing 失败不影响回复

    asyncio.run(scenario())
    assert counter.config_calls >= 1                    # 尝试过取 ticket
    assert counter.typing_status == []                  # 无 typing 发出(静默降级)
    assert len(counter.sends) == 1                      # 回复照常


# ---- 长回复渠道集成 + 首败即停 ----

class _LongAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="段落。" * 800,  # ~2400 字 → 2 段
                            prompt_tokens=1, completion_tokens=1)


def test_long_reply_chunked_via_gateway(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    counter = _CountingReq()

    async def runner(goal, session_key, actor, ctx):
        async def job():
            result = await gw.run_agent(session_key=session_key, goal=goal,
                                        budget=50_000, trace_id="tr_chunk",
                                        actor_trust=actor["trust"])
            await ch_box["ch"].send_reply(session_key.split(":", 2)[2],
                                          result.reply or "", None)
        await gw.submit(session_key, job)

    ch_box: dict[str, WeChatChannel] = {}
    led = DurableLedger(home)
    gw = Gateway(led, make_adapter=lambda: _LongAdapter(), home=home)

    async def scenario():
        # 手动 _ingest 一条消息(不起轮询循环)
        ch = WeChatChannel(home=home, runner=runner, request=counter,
                           typing_refresh_sec=99)  # 不让 typing 干扰 send 计数
        ch._client = ILinkClient("t", "https://x", counter)
        ch._state = load_state(home)
        ch_box["ch"] = ch
        await gw.start("127.0.0.1", 0)
        try:
            ch._ingest({"from_user_id": "peer-1", "content": "长文任务",
                        "context_token": "ct"})
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(counter.sends) >= 2:
                    break
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())
    assert len(counter.sends) == 2
    assert counter.sends[0]["item_list"][0]["text_item"]["text"].startswith("(1/2)")
    assert counter.sends[1]["item_list"][0]["text_item"]["text"].startswith("(2/2)")
    assert all(m["context_token"] == "ct" for m in counter.sends)


def test_first_send_failure_stops_remaining_parts(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)

    class FirstFailReq(_CountingReq):
        async def __call__(self, url, method, headers, body, timeout):
            if url.endswith("/sendmessage"):
                self.sends.append(body["msg"])
                if len(self.sends) == 1:
                    return 200, {"ret": -14}  # 首条失败
                return 200, {"ret": 0}
            return 200, {"ret": 0}

    counter = FirstFailReq()
    ch = WeChatChannel(home=home, runner=_noop, request=counter)
    ch._client = ILinkClient("t", "https://x", counter)
    ch._state = load_state(home)
    result = asyncio.run(ch.send_reply("u", "长" * 3000, "ctx"))
    assert len(counter.sends) == 1              # 首败即停,不空刷
    assert result is not None and result.errcode == -14


async def _noop(goal, session_key, actor, ctx):
    return None
