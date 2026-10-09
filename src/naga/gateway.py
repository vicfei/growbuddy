"""WS 网关最小形态(M1 批次 3,接口设计文档 §2/§3.1)。

回环绑定(ws_port=18789);首帧必须是 hello(proto samsara/1),否则硬关闭;
回环连接按 owner 信任(B7:owner 仅限本地回环)。req/res 帧 + 每连接
event_seq 递增的事件推送(R6-A3:不走断线续传)。会话队列:N = min(CPU,8)
条 lane,每 lane 一个 asyncio.Queue + 专属消费者——同 session_key 物理
串行(INV-4 会话粒度,§13.2 纪律①);lane = sha256(session_key) mod N。

声明(见 issue #27):幂等缓存直接写 SQLite 不入账(丢失由 read 类工具
可重放性兜底);事件全广播(可见性矩阵完整实现属 M2);心跳用 aiohttp
内建 ping/pong 策略等效覆盖 30/90 语义。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from aiohttp import WSMsgType, web

from naga import kernel
from naga.agent import AgentLoop
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter
from naga.skills import SkillStore

WS_PORT = 18789               # spec-constants ws_port / 接口 §2.1
LANE_MAX_COUNT = 8            # spec-constants lane_max_count / GAP9
FRAME_MAX_BYTES = 1048576     # spec-constants frame_max_bytes / 接口 §2.2
HEARTBEAT_SEC = 30            # ws_heartbeat_interval_sec

_LOOPBACK = {"127.0.0.1", "::1"}


def _lane_of(session_key: str, n_lanes: int) -> int:
    return int(hashlib.sha256(session_key.encode("utf-8")).hexdigest(), 16) % n_lanes


def _parse_json(text: str) -> dict[str, Any] | None:
    """帧解析助手:非 JSON 或非对象返回 None(类型边界,避免 Any 收窄传染)。"""
    try:
        value: dict[str, Any] | None = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class Gateway:
    """控制面最小集:hello 握手 / agent.run / lane 队列 / 事件广播 / 幂等缓存。"""

    def __init__(self, ledger: DurableLedger, *,
                 make_adapter: Callable[[], ModelAdapter],
                 tools: list[Any] | None = None,
                 default_budget: int = 200_000):
        self._ledger = ledger
        self._make_adapter = make_adapter
        self._tools = tools or []
        self._default_budget = default_budget
        self._n_lanes = min(os.cpu_count() or 1, LANE_MAX_COUNT)
        self._queues: list[asyncio.Queue[dict[str, Any]]] = [
            asyncio.Queue() for _ in range(self._n_lanes)]
        self._workers: list[asyncio.Task[None]] = []
        self._conns: set[web.WebSocketResponse] = set()
        self._seq: dict[int, int] = {}
        self._runner: web.AppRunner | None = None
        self.port: int = 0

    # ---- 生命周期 ----
    async def start(self, host: str = "127.0.0.1", port: int = WS_PORT) -> int:
        app = web.Application()
        app.router.add_get("/gw", self._handle_ws)
        webchat = Path(__file__).parent / "webchat" / "index.html"
        if webchat.is_file():
            async def _index(_request: web.Request) -> web.StreamResponse:
                return web.FileResponse(webchat)
            app.router.add_get("/", _index)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        addresses = self._runner.addresses
        self.port = addresses[0][1] if addresses else port
        self._workers = [asyncio.create_task(self._consume(i, q))
                         for i, q in enumerate(self._queues)]
        return self.port

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- WS 连接处理 ----
    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if request.remote not in _LOOPBACK:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4003, message=b"loopback only")
            return ws
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT_SEC, max_msg_size=FRAME_MAX_BYTES)
        await ws.prepare(request)

        first = await ws.receive(timeout=HEARTBEAT_SEC)
        if first.type != WSMsgType.TEXT:
            await ws.close(code=4000, message=b"first frame must be hello")
            return ws
        frame = _parse_json(first.data)
        if frame is None or frame.get("type") != "hello":
            await ws.close(code=4000, message=b"first frame must be hello")
            return ws
        if frame.get("proto") != "samsara/1":
            await ws.send_json({"type": "hello-err",
                                "error": {"code": "AUTH_FAILED", "message": "bad proto",
                                          "retryable": False}})
            await ws.close(code=4001, message=b"bad proto")
            return ws

        await ws.send_json({"type": "hello-ok", "session_ticket": "st_" + uuid.uuid4().hex[:20],
                            "trust_level": "owner", "ledger_seq": len(self._ledger.entries),
                            "server_time": datetime.datetime.now(datetime.UTC).isoformat(),
                            "mode_defaults": {}})
        self._conns.add(ws)
        self._seq[id(ws)] = 0
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                req = _parse_json(msg.data)
                if req is not None and req.get("type") == "req":
                    await self._handle_req(ws, req)
        finally:
            self._conns.discard(ws)
            self._seq.pop(id(ws), None)
        return ws

    async def _handle_req(self, ws: web.WebSocketResponse, req: dict[str, Any]) -> None:
        rid = str(req.get("id", ""))
        try:
            result = await self._dispatch(req)
            await ws.send_json({"type": "res", "id": rid, "ok": True, "result": result})
        except GatewayError as err:
            await ws.send_json({"type": "res", "id": rid, "ok": False,
                                "error": {"code": err.code, "message": err.message,
                                          "retryable": False}})

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        method = req.get("method")
        if method != "agent.run":
            raise GatewayError("GATE.METHOD_UNKNOWN", f"method {method!r} not in M1 set")
        params: dict[str, Any] = req.get("params") or {}
        goal = str(params.get("goal") or "").strip()
        if not goal:
            raise GatewayError("GATE.PARAM_MISSING", "agent.run 需要 goal")
        ik = str(req.get("idempotency_key") or "")
        if not ik:
            raise GatewayError("GATE.PARAM_MISSING", "副作用方法必须携带幂等键(接口 §2.2)")

        cached = self._ledger.query(
            "SELECT result_cas FROM idempotency_keys WHERE key=?", (ik,))
        if cached and cached[0][0]:
            return json.loads(self._ledger.cas.get(cached[0][0]))

        session_key = str(params.get("session_key") or f"webchat:dm:anon-{uuid.uuid4().hex[:8]}")
        budget_obj: dict[str, Any] = params.get("budget") or {}
        budget = int(budget_obj.get("tokens", self._default_budget))
        trace_id = "tr_" + uuid.uuid4().hex[:24]
        lane = _lane_of(session_key, self._n_lanes)
        await self._queues[lane].put({"session_key": session_key, "goal": goal,
                                      "budget": budget, "trace_id": trace_id})
        result = {"trace_id": trace_id, "status": "accepted", "lane": f"ln_{lane}"}
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        cas = self._ledger.cas.put(payload)
        self._ledger.execute(
            "INSERT OR REPLACE INTO idempotency_keys VALUES (?,?,?,?,?)",
            (ik, "agent.run", len(self._ledger.entries), cas,
             datetime.datetime.now(datetime.UTC).isoformat()))
        return result

    # ---- lane 消费者 ----
    async def _consume(self, lane: int, queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            task = await queue.get()
            try:
                await self._run_task(task)
            finally:
                queue.task_done()

    async def _run_task(self, task: dict[str, Any]) -> None:
        # 每任务独立 Kernel 实例:root ctx 的 disposers 随实例回收,无泄漏;
        # 工具效应保持 applied(任务括号闭合的 revert 入账语义推迟至 L1 会话生命周期)。
        kern = kernel.Kernel(ledger=cast("kernel.Ledger", self._ledger))  # 鸭子类型
        loop = AgentLoop(ledger=self._ledger, ctx=kern.root,
                         budget=kernel.Budget(int(task["budget"])),
                         adapter=self._make_adapter(), tools=self._tools,
                         skill_store=SkillStore(self._ledger),
                         session_key=str(task["session_key"]),
                         trace_id=str(task["trace_id"]))
        result = await loop.run(str(task["goal"]))
        event = "agent.completed" if result.outcome == "success" else "agent.failed"
        await self._broadcast(event, {
            "session_key": task["session_key"], "trace_id": result.trace_id,
            "reply": result.reply, "outcome": result.outcome,
            "tokens": result.tokens, "failure_analysis": result.failure_analysis})

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        for ws in list(self._conns):
            key = id(ws)
            self._seq[key] = self._seq.get(key, 0) + 1
            try:
                await ws.send_json({"type": "event", "event_seq": self._seq[key],
                                    "event": event, "payload": payload})
            except ConnectionError:
                self._conns.discard(ws)


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
