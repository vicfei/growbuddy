"""WS 网关最小形态(M1 批次 3,接口设计文档 §2/§3.1)。

回环绑定(ws_port=18789);首帧必须是 hello(proto samsara/1),否则硬关闭;
回环连接按 owner 信任(B7:owner 仅限本地回环)。req/res 帧 + 每连接
event_seq 递增的事件推送(R6-A3:不走断线续传)。会话队列:N = min(CPU,8)
条 lane,每 lane 一个 asyncio.Queue + 专属消费者——同 session_key 物理
串行(INV-4 会话粒度,§13.2 纪律①);lane = sha256(session_key) mod N。

声明(见 issue #27):幂等缓存直接写 SQLite 不入账(丢失由 read 类工具
可重放性兜底);事件可见性 §4.1 矩阵子集已实现(event_visible_to,回环
连接即 owner;非 owner 连接的会话绑定属后续批次);心跳用 aiohttp
内建 ping/pong 策略等效覆盖 30/90 语义。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from aiohttp import WSMsgType, web

from naga import kernel, storage
from naga.agent import AgentLoop, AgentResult
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter
from naga.skills import SkillStore

WS_PORT = 18789               # spec-constants ws_port / 接口 §2.1
LANE_MAX_COUNT = 8            # spec-constants lane_max_count / GAP9
FRAME_MAX_BYTES = 1048576     # spec-constants frame_max_bytes / 接口 §2.2
HEARTBEAT_SEC = 30            # ws_heartbeat_interval_sec

_LOOPBACK = {"127.0.0.1", "::1"}


# ── §10.1 信任级策略(单点:run_agent;候选 spec-constants 随 #23 批 RFC)──

GUEST_TASK_TOKEN_BUDGET = 50_000   # spec-constants guest_task_token_budget(§10.1,已注册)
KNOWN_TASK_TOKEN_BUDGET = 500_000  # spec-constants known_task_token_budget(§10.1,已注册)


def task_budget(actor_trust: str | None, requested: int) -> int:
    """§10.1 单任务 token 预算:guest/known 封顶,owner/未标注用请求值;
    untrusted 及未知级 → 0(仅 hello 握手与只读事件流,不执行任务)。"""
    if actor_trust == "guest":
        return min(requested, GUEST_TASK_TOKEN_BUDGET)
    if actor_trust == "known":
        return min(requested, KNOWN_TASK_TOKEN_BUDGET)
    if actor_trust is None or actor_trust == "owner":
        return requested
    return 0


# ── §5.6 cap→R 级映射 + §10.1 R 档封顶(全映射;候选注册随 #23 批 RFC)──

R0 = 0  # 只读感知(fs.read / screen.capture 本地 / web.fetch)
R1 = 1  # 工作区内写
R2 = 2  # 工作区外写 / 任意 shell
R3 = 3  # 外发通信(net.send / channel.broadcast / email.send)
R4 = 4  # 不可逆破坏;未声明 cap 的默认档(宁严勿宽)

# §10.1 R 级封顶:guest R0 / known R1 / owner 无封顶(以 R4 表达)
TRUST_R_CEILING: dict[str, int] = {"owner": R4, "known": R1, "guest": R0}

# 旧 side_effect 声明的归一映射(§5.6 表:read=只读感知;write 泛写近似 R2)
SIDE_EFFECT_TO_R: dict[str, int] = {"read": R0, "write": R2}


def tool_risk(tool: Any) -> int:
    """§5.6 cap→R:显式 risk_level > side_effect 归一 > 未声明默认 R4。"""
    level = getattr(tool, "risk_level", None)
    if isinstance(level, int) and R0 <= level <= R4:
        return level
    se = getattr(tool, "side_effect", None)
    if isinstance(se, str):
        return SIDE_EFFECT_TO_R.get(se, R4)
    return R4


def tools_for_trust(actor_trust: str | None, tools: list[Any]) -> list[Any]:
    """§10.1 R 级封顶:超出信任档位的工具不可见;未标注=回环 owner;未知级无工具。"""
    ceiling = TRUST_R_CEILING.get(actor_trust or "owner")
    if ceiling is None:
        return []
    return [t for t in tools if tool_risk(t) <= ceiling]


# ── §4.1 事件可见性矩阵(全行表驱动;「矩阵即代码」,测试逐格覆盖)──
# 决策四态:none 不可见 / own 仅本会话(需 session_key 匹配;他人会话=none)
# / summary 状态变更摘要(内容裁剪)/ full 全文。
_EVENT_CLASS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("session.", "session"), ("agent.", "session"),
    ("skill.", "asset"), ("promotion.", "asset"), ("memory.", "asset"),
    ("job.", "ops"), ("shadow.", "ops"), ("health.", "ops"),
    ("rollback.", "governance"), ("trust.", "governance"),
    ("constitution.", "governance"),
    ("node.", "node"),
)

_VISIBILITY: dict[str, dict[str, str]] = {
    # 事件类        untrusted  guest     known     owner  node(仅自身相关)
    "session":     {"untrusted": "none", "guest": "own",     "known": "own",
                    "owner": "full", "node": "none"},
    "asset":       {"untrusted": "none", "guest": "summary", "known": "full",
                    "owner": "full", "node": "none"},
    "ops":         {"untrusted": "none", "guest": "none",    "known": "full",
                    "owner": "full", "node": "none"},
    "governance":  {"untrusted": "none", "guest": "none",    "known": "summary",
                    "owner": "full", "node": "own"},
    "node":        {"untrusted": "none", "guest": "none",    "known": "none",
                    "owner": "full", "node": "own"},
    # 未登记事件前缀:宁严勿宽,仅 owner 可见
    "other":       {"untrusted": "none", "guest": "none",    "known": "none",
                    "owner": "full", "node": "none"},
}

# summary 投递保留的状态变更摘要字段(§4.1 guest「仅状态变更摘要」/known 内容裁剪)
_SUMMARY_KEYS = ("status", "kind", "name", "version", "outcome")


def event_class(event: str) -> str:
    for prefix, cls in _EVENT_CLASS_PREFIXES:
        if event.startswith(prefix):
            return cls
    return "other"


def event_decision(viewer_trust: str, viewer_session: str | None,
                   event: str, session_key: str | None) -> str:
    """§4.1 矩阵单点查表:事件类 × 观察者信任级 → 可见决策(四态)。"""
    row = _VISIBILITY[event_class(event)]
    decision = row.get(viewer_trust, row["untrusted"])  # 未知级宁严勿宽
    if decision != "own":
        return decision
    if session_key is not None and session_key == viewer_session:
        return "full"  # 本会话事件全文(他人会话在调用方已折算 none)
    return "none"


def event_visible_to(viewer_trust: str, viewer_session: str | None,
                     event: str, session_key: str | None) -> bool:
    return event_decision(viewer_trust, viewer_session, event, session_key) != "none"


def summarize_event(payload: dict[str, Any]) -> dict[str, Any]:
    """summary 决策的投递形态:仅保留状态变更摘要字段,并标记已裁剪。"""
    clipped = {k: payload[k] for k in _SUMMARY_KEYS if k in payload}
    clipped["summarized"] = True
    return clipped


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
                 default_budget: int = 200_000,
                 home: str | None = None,
                 wechat_request: Any | None = None):
        self._ledger = ledger
        self._make_adapter = make_adapter
        self._tools = tools or []
        self._default_budget = default_budget
        self._home = home
        self._wechat_request = wechat_request
        self._wechat: Any | None = None  # WeChatChannel(attach_wechat 注入,避免循环依赖)
        self._n_lanes = min(os.cpu_count() or 1, LANE_MAX_COUNT)
        self._queues: list[asyncio.Queue[dict[str, Any]]] = [
            asyncio.Queue() for _ in range(self._n_lanes)]
        self._workers: list[asyncio.Task[None]] = []
        self._conns: set[web.WebSocketResponse] = set()
        self._seq: dict[int, int] = {}
        self._conn_trust: dict[int, str] = {}  # id(ws) → 信任级(§4.1 事件可见性)
        self._runner: web.AppRunner | None = None
        self.port: int = 0

    def attach_wechat(self, channel: Any) -> None:
        """注入微信渠道实例(绑定管理面操作它;daemon 接线时调用)。"""
        self._wechat = channel

    # ---- 生命周期 ----
    async def start(self, host: str = "127.0.0.1", port: int = WS_PORT) -> int:
        app = web.Application()
        app.router.add_get("/gw", self._handle_ws)
        app.router.add_get("/wechat/bind/start", self._wechat_bind_start)
        app.router.add_get("/wechat/bind/poll/{qrcode:.+}", self._wechat_bind_poll)
        app.router.add_get("/wechat/status", self._wechat_status)
        app.router.add_get("/wechat/unbind", self._wechat_unbind)
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
        self._conn_trust[id(ws)] = "owner"  # 回环连接按 owner(B7;§4.1 可见性基准)
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
            self._conn_trust.pop(id(ws), None)
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
        await self._queues[lane].put({"kind": "agent", "session_key": session_key,
                                      "goal": goal, "budget": budget,
                                      "trace_id": trace_id})
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
                job: Callable[[], Awaitable[None]] | None = task.get("job")
                if job is not None:
                    await job()  # 通用 job(如微信渠道):自带事件与回复
                else:
                    await self._run_task(task)
            except Exception as err:  # noqa: BLE001 —— 车道逐任务隔离:坏任务不得杀死消费者
                await self._broadcast("agent.failed", {
                    "session_key": task.get("session_key"),
                    "trace_id": task.get("trace_id"),
                    "reply": None, "outcome": "failed",
                    "tokens": 0,
                    "failure_analysis": f"{type(err).__name__}: {err}"})
            finally:
                queue.task_done()

    async def submit(self, session_key: str, job: Callable[[], Awaitable[None]]) -> None:
        """通用会话任务入车道(会话路由单一权威:渠道消息与 WS 请求共用 lane)。"""
        lane = _lane_of(session_key, self._n_lanes)
        await self._queues[lane].put({"kind": "job", "job": job,
                                      "session_key": session_key})

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        await self._broadcast(event, payload)

    async def run_agent(self, *, session_key: str, goal: str, budget: int,
                        trace_id: str, actor_trust: str | None = None) -> Any:
        """会话内执行一次 AgentLoop(渠道 runner 复用;结果事件由调用方处理)。

        §10.1 信任级策略单点:预算封顶 + guest R0 工具过滤 + untrusted 拒绝。
        工具集与预算树经 SubAgentSpawner 装配(M3-1:派生授权代数单点)。
        """
        budget = task_budget(actor_trust, budget)
        if budget <= 0:
            return AgentResult(
                reply=("当前信任级不足以执行任务(仅支持握手与只读事件流,§10.1)"
                       "——请联系 owner 完成绑定或升级信任级"),
                outcome="rejected", trace_id=trace_id, steps=0, tokens=0, cost_cents=0)
        kern = kernel.Kernel(ledger=cast("kernel.Ledger", self._ledger))  # 鸭子类型
        from naga.spawner import SubAgentSpawner  # 局部导入:spawner 依赖本模块,避免环

        spawner = SubAgentSpawner(
            ledger=self._ledger, ctx=kern.root, make_adapter=self._make_adapter,
            raw_tools=self._tools, skill_store=SkillStore(self._ledger),
            session_key=session_key, actor_trust=actor_trust,
            budget=kernel.Budget(budget), depth=0, parent_trace=trace_id)
        loop = AgentLoop(ledger=self._ledger, ctx=kern.root,
                         budget=spawner.budget,
                         adapter=self._make_adapter(), tools=spawner.make_tools(),
                         skill_store=SkillStore(self._ledger),
                         session_key=session_key, trace_id=trace_id,
                         actor_trust=actor_trust)
        return await loop.run(goal)

    async def _run_task(self, task: dict[str, Any]) -> None:
        result = await self.run_agent(session_key=str(task["session_key"]),
                                      goal=str(task["goal"]),
                                      budget=int(task["budget"]),
                                      trace_id=str(task["trace_id"]))
        event = "agent.completed" if result.outcome == "success" else "agent.failed"
        await self._broadcast(event, {
            "session_key": task["session_key"], "trace_id": result.trace_id,
            "reply": result.reply, "outcome": result.outcome,
            "tokens": result.tokens, "failure_analysis": result.failure_analysis})

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        session_key = payload.get("session_key")
        for ws in list(self._conns):
            # §4.1 可见性:按连接信任级查表;未登记连接按 untrusted(宁严勿宽)
            trust = self._conn_trust.get(id(ws), "untrusted")
            decision = event_decision(trust, None, event,
                                      session_key if isinstance(session_key, str) else None)
            if decision == "none":
                continue
            out = summarize_event(payload) if decision == "summary" else payload
            key = id(ws)
            self._seq[key] = self._seq.get(key, 0) + 1
            try:
                await ws.send_json({"type": "event", "event_seq": self._seq[key],
                                    "event": event, "payload": out})
            except ConnectionError:
                self._conns.discard(ws)


    # ---- 微信绑定管理面(owner-only 回环;宪法层条款由网关回环绑定保证) ----
    async def _wechat_bind_start(self, _request: web.Request) -> web.Response:
        from naga.wechat import ILinkClient
        req = self._wechat_request  # 测试注入
        try:
            binding = await (ILinkClient.request_qrcode(req) if req is not None
                             else ILinkClient.request_qrcode())
        except (ValueError, OSError) as err:
            return web.json_response({"ok": False, "error": f"iLink 不可达: {str(err)[:100]}"},
                                     status=502)
        import segno
        qr = segno.make(binding.qr_content, error="m")
        data_url = qr.png_data_uri(scale=8, border=2)  # 本地生成,不走公网第三方(~300px 级)
        return web.json_response({
            "ok": True, "qrcode": binding.qrcode, "qr_data_url": data_url,
            "instruction": "用微信扫码授权(仅支持单聊)"})

    async def _wechat_bind_poll(self, request: web.Request) -> web.Response:
        from naga.trust import derive_trust, grant_trust
        from naga.wechat import ILinkClient, WeChatCredential, save_credential
        qrcode = request.match_info["qrcode"]
        req = self._wechat_request
        status = await (ILinkClient.poll_binding_status(qrcode, req) if req is not None
                        else ILinkClient.poll_binding_status(qrcode))
        if status.status != "confirmed" or not status.bot_token:
            return web.json_response({"ok": True, "status": status.status})
        home = self._home or storage.resolve_home()
        cred = WeChatCredential(
            bot_token=status.bot_token,
            ilink_user_id=status.ilink_user_id,
            baseurl=status.baseurl,
            bound_at=datetime.datetime.now(datetime.UTC).isoformat())
        await asyncio.to_thread(save_credential, cred, home)
        # 扫码绑定者自动授 owner(T3 配对审批的运行时形态;已有 allowlist 不覆盖)
        trust_file = os.path.join(home, "trust.json")
        if status.ilink_user_id:
            def _grant() -> None:
                d = derive_trust("wechat", status.ilink_user_id or "", path=trust_file)
                if d.source != "allowlist":
                    grant_trust("wechat", status.ilink_user_id or "", "owner",
                                note="QR 绑定自动授予", path=trust_file)
            await asyncio.to_thread(_grant)
        self._ledger.append("channel.bound", "root", {
            "channel": "wechat",
            "ilink_user_id": status.ilink_user_id,
            "baseurl": status.baseurl})
        # 热启动:绑定确认即启动消息长轮询(无需重启守护;鸭子检查,可注入测试替身)
        ch = self._wechat
        if ch is not None and not ch.is_running:
            ch.start(cred)
        return web.json_response({"ok": True, "status": "confirmed",
                                  "message": "绑定成功,消息通道已启动(无需重启)"})

    async def _wechat_status(self, _request: web.Request) -> web.Response:
        from naga.wechat import load_credential
        home = self._home or storage.resolve_home()
        cred = await asyncio.to_thread(load_credential, home)
        ch = self._wechat
        return web.json_response({
            "ok": True, "bound": cred is not None,
            "bound_at": cred.bound_at if cred else None,
            "polling": bool(getattr(ch, "is_running", False))})

    async def _wechat_unbind(self, _request: web.Request) -> web.Response:
        from naga.wechat import clear_credential
        home = self._home or storage.resolve_home()
        await asyncio.to_thread(clear_credential, home)
        ch = self._wechat
        if ch is not None:
            ch.stop()
        self._ledger.append("channel.unbound", "root", {"channel": "wechat"})
        return web.json_response({"ok": True, "message": "已解绑"})


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
