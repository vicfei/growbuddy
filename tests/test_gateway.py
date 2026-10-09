"""WS 网关测试:hello 流程 / 首帧校验 / 同会话串行 / 幂等缓存 / 帧上限。

全部走真实 aiohttp server(随机回环端口),单事件循环内完成生命周期
(aiohttp 对象绑定创建它的 loop),FakeAdapter 驱动,零外网。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from naga.gateway import FRAME_MAX_BYTES, Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse

_HELLO = {"type": "hello", "proto": "samsara/1", "auth": {"token": ""},
          "device": {"id": "t", "platform": "test"}, "role": "client", "caps": []}


class ScriptAdapter:
    """按 goal 内容脚本化的 LLM:sleep* 任务延迟 N 秒,其余立即回复。"""

    def __init__(self):
        self.calls: list[str] = []

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        goal = messages[1]["content"]
        self.calls.append(goal)
        if goal.startswith("sleep"):
            await asyncio.sleep(float(goal.split(":")[1]))
        return ChatResponse(content=f"done:{goal}", prompt_tokens=1, completion_tokens=1)


@asynccontextmanager
async def _server(home: str) -> AsyncIterator[tuple[DurableLedger, ScriptAdapter, int]]:
    led = DurableLedger(home)
    adapter = ScriptAdapter()
    gw = Gateway(led, make_adapter=lambda: adapter)
    port = await gw.start("127.0.0.1", 0)
    try:
        yield led, adapter, port
    finally:
        await gw.stop()
        led.close()


@asynccontextmanager
async def _client(port: int, *, max_msg_size: int = FRAME_MAX_BYTES * 2) -> AsyncIterator[Any]:
    async with aiohttp.ClientSession() as s, \
            s.ws_connect(f"ws://127.0.0.1:{port}/gw", max_msg_size=max_msg_size) as ws:
        yield ws


def _req(rid, goal, session="webchat:dm:owner", ik=None):
    return {"type": "req", "id": rid, "method": "agent.run",
            "params": {"goal": goal, "session_key": session, "budget": {"tokens": 5000}},
            "idempotency_key": ik or f"ik_{rid}"}


async def _exchange(ws, rid: str, events: list[dict]) -> dict:
    """取 rid 的 res;途中遇到的事件帧存入 events 缓冲(不吞,容忍交错)。"""
    while True:
        frame = json.loads((await ws.receive()).data)
        if frame.get("type") == "res" and frame.get("id") == rid:
            return frame
        if frame.get("type") == "event":
            events.append(frame)


async def _events_until(ws, name: str, count: int, buffer: list[dict]) -> list[dict]:
    out: list[dict] = []
    while len(out) < count:
        frame = buffer.pop(0) if buffer else json.loads((await ws.receive()).data)
        if frame.get("type") == "event" and frame.get("event") == name:
            out.append(frame)
    return out


def test_hello_then_agent_run_completed_event(tmp_path):
    async def scenario():
        async with _server(str(tmp_path / "home")) as (led, adapter, port), _client(port) as ws:
            await ws.send_json(_HELLO)
            ok = json.loads((await ws.receive()).data)
            assert ok["type"] == "hello-ok" and ok["trust_level"] == "owner"
            assert ok["ledger_seq"] == 0

            events: list[dict] = []
            await ws.send_json(_req("r1", "调研 naga"))
            res = await _exchange(ws, "r1", events)
            assert res["ok"] is True and res["result"]["status"] == "accepted"
            assert res["result"]["lane"].startswith("ln_")

            (evt,) = await _events_until(ws, "agent.completed", 1, events)
            assert evt["event_seq"] == 1
            assert evt["payload"]["reply"] == "done:调研 naga"
            assert evt["payload"]["session_key"] == "webchat:dm:owner"
            assert adapter.calls == ["调研 naga"]
            assert led.query("SELECT outcome FROM traces")[0][0] == "success"

    asyncio.run(scenario())


def test_first_frame_must_be_hello_and_bad_proto(tmp_path):
    async def scenario():
        async with _server(str(tmp_path / "home")) as (_, _, port), _client(port) as ws:
            await ws.send_json(_req("r0", "x"))
            assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE
        async with _server(str(tmp_path / "home2")) as (_, _, port), _client(port) as ws:
            await ws.send_json({**_HELLO, "proto": "samsara/9"})
            msg = await ws.receive()
            assert json.loads(msg.data)["type"] == "hello-err"

    asyncio.run(scenario())


def test_same_session_serialized_across_tasks(tmp_path):
    async def scenario():
        async with _server(str(tmp_path / "home")) as (_, _, port), _client(port) as ws:
            await ws.send_json(_HELLO)
            await ws.receive()  # hello-ok
            # 同 session_key 两个任务:慢任务先入队,快任务必须等它(同 lane 串行)
            await ws.send_json(_req("r1", "sleep:0.3", ik="ik_a"))
            await ws.send_json(_req("r2", "quick", ik="ik_b"))
            events: list[dict] = []
            assert (await _exchange(ws, "r1", events))["ok"] is True
            assert (await _exchange(ws, "r2", events))["ok"] is True
            evts = await _events_until(ws, "agent.completed", 2, events)
            replies = [e["payload"]["reply"] for e in evts]
            assert replies == ["done:sleep:0.3", "done:quick"]

    asyncio.run(scenario())


def test_idempotency_cache_replays_result(tmp_path):
    async def scenario():
        async with _server(str(tmp_path / "home")) as (led, adapter, port), _client(port) as ws:
            await ws.send_json(_HELLO)
            await ws.receive()
            await ws.send_json(_req("r1", "任务", ik="ik_same"))
            events: list[dict] = []
            first = (await _exchange(ws, "r1", events))["result"]
            await ws.send_json(_req("r2", "任务", ik="ik_same"))
            second = (await _exchange(ws, "r2", events))["result"]
            assert first == second  # 同 trace_id,缓存重放,不重复入队
            assert len(await _events_until(ws, "agent.completed", 1, events)) == 1
            assert adapter.calls == ["任务"]
            assert led.query("SELECT COUNT(*) FROM idempotency_keys")[0][0] == 1

    asyncio.run(scenario())


def test_param_missing_error_frame(tmp_path):
    async def scenario():
        async with _server(str(tmp_path / "home")) as (_, _, port), _client(port) as ws:
            await ws.send_json(_HELLO)
            await ws.receive()
            await ws.send_json({"type": "req", "id": "r9", "method": "agent.run",
                                "params": {}, "idempotency_key": "ik_x"})
            res = json.loads((await ws.receive()).data)
            assert res["ok"] is False and res["error"]["code"] == "GATE.PARAM_MISSING"

    asyncio.run(scenario())


def test_frame_over_limit_drops_connection(tmp_path):
    async def scenario():
        async with _server(str(tmp_path / "home")) as (_, _, port), _client(port) as ws:
            await ws.send_json(_HELLO)
            await ws.receive()
            await ws.send_str("x" * (FRAME_MAX_BYTES + 1024))
            msg = await ws.receive()
            assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR)

    asyncio.run(scenario())
