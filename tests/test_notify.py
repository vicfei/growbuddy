"""#93 修复测试:推送 errcode 真检 / 补发队列 / promotion 内联。"""

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from naga import notify
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse


@dataclass
class _SendResult:
    errcode: int
    errmsg: str = ""


class _Channel:
    is_running = True

    def __init__(self, results: list[_SendResult]):
        self._results = list(results)
        self.sent: list[str] = []

    async def send_reply(self, peer: str, text: str,
                         _token: str | None) -> _SendResult:
        self.sent.append(text)
        return self._results.pop(0) if self._results else _SendResult(0)


class _Adapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)


# ── errcode 真检(#93 根因:无异常≠送达) ────────────────────────

def test_notify_rejected_errcode_returns_false(tmp_path, capsys):
    async def scenario() -> None:
        led = DurableLedger(str(tmp_path / "h"))
        gw = Gateway(led, make_adapter=lambda: _Adapter())
        ch = _Channel([_SendResult(-2, "prepare failed")])
        gw.register_binding_channel("peer", ch)
        assert await gw.notify_session("wechat:dm:peer", "早报") is False
        assert ch.sent == ["早报"]                    # 已尝试发送
        out = capsys.readouterr().out
        assert "errcode=-2" in out                    # 真实拒绝已记日志
        assert await gw.notify_session("wechat:dm:peer", "再试") is True  # 恢复后成功
        led.close()

    asyncio.run(scenario())


def test_notify_send_raises_returns_false(tmp_path):
    async def scenario() -> None:
        led = DurableLedger(str(tmp_path / "h2"))
        gw = Gateway(led, make_adapter=lambda: _Adapter())

        class _Boom:
            is_running = True

            async def send_reply(self, *_a: Any, **_k: Any) -> Any:
                raise ConnectionError("reset")

        gw.register_binding_channel("peer", _Boom())
        assert await gw.notify_session("wechat:dm:peer", "x") is False
        led.close()

    asyncio.run(scenario())


# ── 补发队列 ────────────────────────────────────────────────────

def test_pending_queue_roundtrip_and_cap(tmp_path):
    path = str(tmp_path / "pending.json")
    for i in range(notify.PENDING_MAX + 3):           # 超上限:老的被挤掉
        notify.enqueue(path, "wechat:dm:u", f"digest-{i}")
    queue = notify.take(path, "wechat:dm:u")
    assert len(queue) == notify.PENDING_MAX
    assert queue[0]["text"] == "digest-3"             # 最老的被丢弃
    assert queue[-1]["text"] == f"digest-{notify.PENDING_MAX + 2}"
    assert all("ts" in item for item in queue)
    assert notify.take(path, "wechat:dm:u") == []     # 取出即清空
    assert notify.take(path, "wechat:dm:other") == []  # 未知会话空
    # 文件权限 0600
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    # 损坏文件容忍
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert notify.load(path) == {}


def test_pending_flush_pattern(tmp_path):
    """端到端模式:拒发→入队→(用户来消息)补发成功→队列空。"""
    path = str(tmp_path / "p2.json")

    async def scenario() -> None:
        led = DurableLedger(str(tmp_path / "h3"))
        gw = Gateway(led, make_adapter=lambda: _Adapter())
        ch = _Channel([_SendResult(-2, "prepare failed"), _SendResult(0)])
        gw.register_binding_channel("u", ch)
        # 第一次:被拒 → 入队
        if not await gw.notify_session("wechat:dm:u", "晨报"):
            notify.enqueue(path, "wechat:dm:u", "晨报")
        assert ch.sent == ["晨报"]
        # 用户来消息:取出补发(token 已新鲜→成功)
        backlog = notify.take(path, "wechat:dm:u")
        flushed = 0
        for item in backlog:
            if await gw.notify_session("wechat:dm:u", str(item["text"])):
                flushed += 1
            else:
                notify.enqueue(path, "wechat:dm:u", str(item["text"]))
                break
        assert flushed == 1
        assert ch.sent == ["晨报", "晨报"]
        assert notify.load(path).get("wechat:dm:u") is None
        led.close()

    asyncio.run(scenario())


def test_auto_promotion_hook_inline_no_thread_error(tmp_path):
    """#93 副修:promotion 钩子内联查询(修复 to_thread 跨线程 sqlite)。"""
    import inspect

    from naga import daemon
    src = inspect.getsource(daemon)
    assert "asyncio.to_thread(\n                auto_trigger_candidates" not in src
    assert "auto_trigger_candidates(led, session_key)" in src
