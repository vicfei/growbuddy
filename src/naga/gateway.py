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
                 wechat_request: Any | None = None,
                 router: Any | None = None,
                 memory: Any | None = None):
        self._ledger = ledger
        self._make_adapter = make_adapter
        self._tools = tools or []
        self._default_budget = default_budget
        self._home = home
        self._memory = memory  # MemoryStore(§6.5;缺省关记忆面)
        self._wechat_request = wechat_request
        self._router = router  # ModelRouter(M4b-1:任务级模型选型;缺省用 make_adapter)
        self._routing_bg: set[asyncio.Task[None]] = set()  # 影子探索后台任务(可等待)
        self._wechat: Any | None = None  # WeChatChannel(attach_wechat 注入,避免循环依赖)
        self._wechat_channels: dict[str, Any] = {}  # binder → 渠道(多绑定并行)
        self.on_task_done: Any | None = None  # run_agent 完成回调(daemon 注入;M4a-2 自动晋升)
        self._wechat_factory: Callable[[Any], Any] | None = None  # 每凭据建渠道(daemon 注入)
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
        """注入微信渠道实例(兼容单渠道形态;绑定管理面操作它)。"""
        self._wechat = channel

    def attach_wechat_factory(self, factory: Callable[[Any], Any]) -> None:
        """注入渠道工厂(多绑定形态):每份凭据一个专属渠道实例。"""
        self._wechat_factory = factory

    # ---- 生命周期 ----
    async def start(self, host: str = "127.0.0.1", port: int = WS_PORT) -> int:
        app = web.Application()
        app.router.add_get("/gw", self._handle_ws)
        app.router.add_get("/wechat/bind/start", self._wechat_bind_start)
        app.router.add_get("/wechat/bind/poll/{qrcode:.+}", self._wechat_bind_poll)
        app.router.add_get("/wechat/status", self._wechat_status)
        app.router.add_get("/wechat/unbind", self._wechat_unbind)
        if self._router is not None:
            async def _routing_stats(_request: web.Request) -> web.Response:
                from naga.routing import routing_dashboard as _dash
                router = self._router
                assert router is not None  # 分支条件保证
                return web.json_response(_dash(self._ledger, router.registry))
            app.router.add_get("/routing/stats", _routing_stats)
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
        if method == "memory.list":
            return self._memory_list(req)
        if method == "memory.forget":
            return await self._memory_forget(req)
        if method == "job.list":
            return self._job_list(req)
        if method in ("job.pause", "job.resume", "job.delete", "job.renew"):
            return self._job_transition(method, req)
        if method == "job.create":
            return await self._job_create(req)
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

    # ---- memory.* 方法面(接口 §3.2;回环=owner,INC4 owner 路径) ----

    def _memory_list(self, req: dict[str, Any]) -> dict[str, Any]:
        if self._memory is None:
            raise GatewayError("GATE.METHOD_UNKNOWN", "memory 未启用")
        params: dict[str, Any] = req.get("params") or {}
        rows = self._memory.list_items(
            session_key=str(params.get("session_key") or "") or None,
            layer=str(params.get("layer") or "") or None,
            status=str(params.get("status") or "") or None)
        items = [{"cas_id": r[0], "layer": r[1], "session_key": r[2],
                  "status": r[3], "created_at": r[4], "use_count": r[6]}
                 for r in rows]
        return {"items": items, "count": len(items)}

    async def _memory_forget(self, req: dict[str, Any]) -> dict[str, Any]:
        if self._memory is None:
            raise GatewayError("GATE.METHOD_UNKNOWN", "memory 未启用")
        params: dict[str, Any] = req.get("params") or {}
        session_key = str(params.get("session_key") or "")
        if not session_key:
            raise GatewayError("GATE.PARAM_MISSING", "memory.forget 需要 session_key")
        ik = str(req.get("idempotency_key") or "")
        if not ik:
            raise GatewayError("GATE.PARAM_MISSING", "副作用方法必须携带幂等键(接口 §2.2)")
        cached = self._ledger.query(
            "SELECT result_cas FROM idempotency_keys WHERE key=?", (ik,))
        if cached and cached[0][0]:
            return json.loads(self._ledger.cas.get(cached[0][0]))
        try:
            report = self._memory.forget(
                session_key,
                cas_id=str(params.get("cas_id") or "") or None,
                since=str(params.get("since") or "") or None,
                until=str(params.get("until") or "") or None,
                actor="root", actor_trust="owner")  # 回环连接按 owner(B7)
        except ValueError as err:
            raise GatewayError("MEMORY.FORBIDDEN", str(err)) from err
        result = {"forgotten": report.forgotten,
                  "expires_at": report.expires_at,
                  "rollback_hint": "托管期内可经 memory.forget.rollback 恢复"}
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        cas = self._ledger.cas.put(payload)
        self._ledger.execute(
            "INSERT OR REPLACE INTO idempotency_keys VALUES (?,?,?,?,?)",
            (ik, "memory.forget", len(self._ledger.entries), cas,
             datetime.datetime.now(datetime.UTC).isoformat()))
        await self._broadcast("memory.forgotten", {
            "session_key": session_key, "count": report.forgotten,
            "status": "forgotten", "kind": "memory.forgotten"})
        return result

    # ---- job.* 方法面(接口 §3.5,附录 C;回环=owner) ----

    async def _job_create(self, req: dict[str, Any]) -> dict[str, Any]:
        from naga.jobs import (
            JOB_DEFAULT_BUDGET_TOKENS,
            JOB_WALL_MS_DEFAULT,
            JobSpec,
            compile_natural_language,
            fires_per_month,
        )
        from naga.trust import derive_trust

        ik = str(req.get("idempotency_key") or "")
        if not ik:
            raise GatewayError("GATE.PARAM_MISSING", "副作用方法必须携带幂等键(接口 §2.2)")
        cached = self._ledger.query(
            "SELECT result_cas FROM idempotency_keys WHERE key=?", (ik,))
        if cached and cached[0][0]:
            return json.loads(self._ledger.cas.get(cached[0][0]))
        params: dict[str, Any] = req.get("params") or {}
        session_key = str(params.get("session_key") or "webchat:dm:owner")
        channel, _, peer = session_key.partition(":dm:")
        derived = derive_trust(channel or "webchat", peer)
        if derived.trust in ("guest", "untrusted"):
            raise GatewayError("JOB.FORBIDDEN",
                               "guest/untrusted 不可创建定时任务(§10 矩阵)")
        raw_cron = str(params.get("cron") or "").strip()
        if raw_cron:
            spec = JobSpec(
                job_id="job_" + uuid.uuid4().hex[:20], schedule=raw_cron,
                timezone=str(params.get("timezone") or "Asia/Shanghai"),
                goal=str(params.get("goal") or "").strip(),
                trust_snapshot=derived.trust,
                budget={"tokens": int(params.get("budget_tokens")
                                      or JOB_DEFAULT_BUDGET_TOKENS),
                        "wall_ms": int(params.get("wall_ms")
                                       or JOB_WALL_MS_DEFAULT)},
                r_ceiling=str(params.get("r_ceiling") or "R1"),
                notification=str(params.get("notification") or "smart"),
                misfire=str(params.get("misfire") or "skip"),
                expires_at=str(params.get("expires_at") or "") or None,
                creator_channel=channel or "webchat", creator_peer=peer)
        else:
            text = str(params.get("text") or "").strip()
            if not text:
                raise GatewayError("GATE.PARAM_MISSING", "job.create 需要 text 或 cron")
            try:
                spec = await compile_natural_language(self._make_adapter(), text)
            except ValueError as err:
                raise GatewayError("JOB.COMPILE_FAILED", str(err)) from err
            spec.trust_snapshot = derived.trust
            spec.creator_channel = channel or "webchat"
            spec.creator_peer = peer
            if params.get("budget_tokens"):
                spec.budget["tokens"] = int(params["budget_tokens"])
            if params.get("misfire"):
                spec.misfire = str(params["misfire"])
            if params.get("r_ceiling"):
                spec.r_ceiling = str(params["r_ceiling"])
            if params.get("expires_at"):
                spec.expires_at = str(params["expires_at"])
        try:
            from naga.jobs import JobStore
            JobStore(self._ledger).create(spec, actor=session_key)
        except ValueError as err:
            raise GatewayError("JOB.INVALID", str(err)) from err
        result = {
            "job_id": spec.job_id, "cron": spec.schedule,
            "timezone": spec.timezone, "goal": spec.goal,
            "misfire": spec.misfire, "r_ceiling": spec.r_ceiling,
            "budget": spec.budget,
            "fires_per_month": fires_per_month(spec.schedule, spec.timezone),
            "notification": spec.notification,
        }
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        cas = self._ledger.cas.put(payload)
        self._ledger.execute(
            "INSERT OR REPLACE INTO idempotency_keys VALUES (?,?,?,?,?)",
            (ik, "job.create", len(self._ledger.entries), cas,
             datetime.datetime.now(datetime.UTC).isoformat()))
        await self._broadcast("job.created", {
            "session_key": session_key, "job_id": spec.job_id,
            "cron": spec.schedule, "state": "active", "kind": "job.created"})
        return result

    def _job_list(self, req: dict[str, Any]) -> dict[str, Any]:
        from naga.jobs import JobStore, scan_job_events

        params: dict[str, Any] = req.get("params") or {}
        events = scan_job_events(self._ledger)
        rows = JobStore(self._ledger).list_jobs(
            state=str(params.get("state") or "") or None)
        items: list[dict[str, Any]] = []
        for r in rows:
            slot: dict[str, Any] = events.get(r["id"], {})
            raw_creator: object = slot.get("creator")
            items.append({**r,
                          "last_fire": slot.get("last_fire"),
                          "creator": (cast("dict[str, Any]", raw_creator)
                                      if isinstance(raw_creator, dict) else {})})
        return {"items": items, "count": len(items)}

    def _job_transition(self, method: str, req: dict[str, Any]) -> dict[str, Any]:
        from naga.jobs import JobStore

        params: dict[str, Any] = req.get("params") or {}
        job_id = str(params.get("job_id") or "")
        if not job_id:
            raise GatewayError("GATE.PARAM_MISSING", f"{method} 需要 job_id")
        store = JobStore(self._ledger)
        try:
            if method == "job.pause":
                ok = store.pause(job_id, reason=str(params.get("reason") or "manual"))
            elif method == "job.resume":
                ok = store.resume(job_id)
            elif method == "job.delete":
                ok = store.delete(job_id)
            else:
                until = str(params.get("until") or "")
                if not until:
                    raise GatewayError("GATE.PARAM_MISSING", "job.renew 需要 until")
                ok = store.renew(job_id, until)
        except ValueError as err:
            raise GatewayError("JOB.INVALID", str(err)) from err
        if not ok:
            raise GatewayError("JOB.NOT_FOUND", f"任务不存在: {job_id}")
        return {"job_id": job_id, "ok": True}

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
                        trace_id: str, actor_trust: str | None = None,
                        r_ceiling: int | None = None) -> Any:
        """会话内执行一次 AgentLoop(渠道 runner 复用;结果事件由调用方处理)。

        §10.1 信任级策略单点:预算封顶 + guest R0 工具过滤 + untrusted 拒绝。
        工具集与预算树经 SubAgentSpawner 装配(M3-1:派生授权代数单点)。
        r_ceiling(附录 C.4):任务级风险帽,在信任档位之上再收紧工具集
        (定时任务快照;None=不额外收紧)。宪法锚点(task_budget/
        TRUST_R_CEILING)不动——帽子只能更严。
        """
        budget = task_budget(actor_trust, budget)
        if budget <= 0:
            return AgentResult(
                reply=("当前信任级不足以执行任务(仅支持握手与只读事件流,§10.1)"
                       "——请联系 owner 完成绑定或升级信任级"),
                outcome="rejected", trace_id=trace_id, steps=0, tokens=0, cost_cents=0)
        self._note_activity(session_key, actor_trust)
        kern = kernel.Kernel(ledger=cast("kernel.Ledger", self._ledger))  # 鸭子类型
        from naga.spawner import SubAgentSpawner  # 局部导入:spawner 依赖本模块,避免环

        # M4b-1 路由(E.3):任务级选型——已证实簇用预算模型,否则旗舰(只向下探索)
        spec = self._router.pick(goal) if self._router is not None else None
        make_adapter = spec.make_adapter if spec is not None else self._make_adapter
        model_id = spec.model_id if spec is not None else None

        skills = SkillStore(self._ledger)
        branch = self._make_branch(skills, session_key, actor_trust, trace_id)

        spawner = SubAgentSpawner(
            ledger=self._ledger, ctx=kern.root, make_adapter=make_adapter,
            raw_tools=self._tools, skill_store=skills,
            session_key=session_key, actor_trust=actor_trust,
            budget=kernel.Budget(budget), depth=0, parent_trace=trace_id,
            branch=branch)
        tools: list[Any] = spawner.make_tools()
        if r_ceiling is not None:  # 任务级风险帽罩住全部装配产物(含 spawn)
            tools = [t for t in tools if tool_risk(t) <= r_ceiling]
        loop = AgentLoop(ledger=self._ledger, ctx=kern.root,
                         budget=spawner.budget,
                         adapter=make_adapter(), tools=tools,
                         skill_store=skills,
                         session_key=session_key, trace_id=trace_id,
                         actor_trust=actor_trust, branch=branch,
                         model_id=model_id or "deepseek-v4-flash",
                         memory_store=self._memory)
        try:
            result = await loop.run(goal)
            if self._router is not None and spec is not None:
                self._routing_bookkeeping(goal, session_key, spec, result)
            return result
        finally:
            if self.on_task_done is not None:
                try:
                    await self.on_task_done(session_key)  # 后台钩子:异常不杀任务
                except Exception:  # noqa: BLE001, S110
                    pass

    def _note_activity(self, session_key: str,
                       actor_trust: str | None) -> None:
        """会话注册表开门(§5 sessions):无行或已关闭 → session.open(新开
        一段对话,created_at 重置);活跃则不动作(last_active 由 trace 投影
        更新)。job:* 是时间驱动执行体,不入注册表。"""
        if session_key.startswith("job:"):
            return
        rows = self._ledger.query(
            "SELECT state FROM sessions WHERE session_key=?", (session_key,))
        if rows and rows[0][0] == "open":
            return
        lane = _lane_of(session_key, self._n_lanes)
        self._ledger.append("session.open", session_key, {
            "session_key": session_key,
            "channel": session_key.split(":", 1)[0],
            "lane_id": f"ln_{lane}",
            "trust": actor_trust or "owner",
            "trust_source": "runtime-reopen" if rows else "runtime"})

    async def notify_session(self, session_key: str, text: str) -> bool:
        """按会话路由投递(定时任务结果→微信;附录 C.5 的当前形态):
        wechat:dm:peer → 绑定渠道实例(peer==binder,allowlist 派生实证;
        单绑定回落)。投递尽力而为,失败不抛(返回 False)。"""
        from naga.sessions import parse_session_channel
        channel, peer = parse_session_channel(session_key)
        if channel != "wechat" or not peer:
            return False
        ch = self._wechat_channels.get(peer)
        if ch is None and len(self._wechat_channels) == 1:
            ch = next(iter(self._wechat_channels.values()))
        if ch is None or not getattr(ch, "is_running", False):
            return False
        try:
            await ch.send_reply(peer, text, None)
            return True
        except Exception:  # noqa: BLE001 —— 通知尽力而为
            return False

    def _routing_bookkeeping(self, goal: str, session_key: str,
                             spec: Any, result: Any) -> None:
        """路由记账 + 触发影子探索(E.4);后台化,异常绝不影响任务结果。"""
        from naga.agent import cluster_of
        router = self._router
        if router is None:
            return
        try:
            cluster = cluster_of(goal)
            flagship = router.registry.flagship()
            router.record_task(
                trace_id=str(result.trace_id or "tr_rt"), cluster=cluster,
                model=spec.model_id,
                prompt_tokens=max(0, result.tokens), completion_tokens=0,
                flagship_model=flagship.model_id)
            router.scorecard.record_result(
                cluster, spec.model_id, result.outcome == "success")
            if (spec.tier == "flagship" and result.outcome == "success"
                    and router.scorecard.proven_budget(cluster) is None):
                task = asyncio.create_task(
                    router.shadow_explore(goal, session_key=session_key))
                self._routing_bg.add(task)
                task.add_done_callback(self._routing_bg.discard)
        except Exception as err:  # noqa: BLE001 —— 路由旁路:绝不杀任务
            print(f"[routing] 记账/探索异常: {type(err).__name__}: {err}",
                  flush=True)

    def _make_branch(self, skills: SkillStore, session_key: str,
                     actor_trust: str | None = None,
                     trace_id: str | None = None) -> Any:
        """会话任务分支(M3-2):技能域 COW——base=全局 main 视图,sink=物化到
        local:{session} 分支;manifest 与差异值入 CAS;生死入账。
        §6.5:记忆域同机制——memory:{layer}:{id} 键 commit 才物化(闸门在
        MemoryStore 单点;坏条目丢弃不阻断结算)。"""
        from naga.branch import SessionBranch

        def base_get(key: str) -> bytes | None:
            if not key.startswith("skill:"):
                return None  # 记忆键无全局 base diff(每次提炼即差异)
            row = skills.get(key.removeprefix("skill:"))
            return str(row[1]).encode() if row else None  # row[1]=content

        def base_has(key: str) -> bool:
            return base_get(key) is not None

        def sink_put(key: str, content: bytes) -> str | None:
            if key.startswith("memory:"):
                if self._memory is None:
                    return "memory:disabled"
                _, layer, _item = key.split(":", 2)
                try:
                    # 返回密文 CAS(manifest 只引用密文,K.6);绝不可回落
                    # branch 自持——那会把明文写进 CAS
                    return str(self._memory.write(
                        layer, session_key, content.decode("utf-8"),
                        {"source": "agent", "trace_id": trace_id},
                        actor_trust=actor_trust))
                except ValueError:
                    return "memory:gate-rejected"  # 闸门/体检拒绝:丢该条,不阻断结算
            name = key.removeprefix("skill:")
            return skills.commit(name, content.decode("utf-8"),
                                 provenance={"source": "agent"},
                                 branch=f"local:{session_key}")

        return SessionBranch(
            owner_session=session_key,
            base_get=base_get, base_has=base_has,
            cas_put=self._ledger.cas.put, sink_put=sink_put,
            ledger_append=lambda kind, owner, payload: self._ledger.append(
                kind, owner, payload))

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
        from naga.wechat import ILinkClient, WeChatCredential, save_binding
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
        binder = await asyncio.to_thread(save_binding, cred, home)
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
            "channel": "wechat", "binder": binder,
            "ilink_user_id": status.ilink_user_id,
            "baseurl": status.baseurl})
        # 热启动:绑定确认即启动该绑定专属的消息长轮询(多绑定=多渠道并行;
        # 同 binder 重绑=先停旧渠道;无工厂时回落单渠道兼容形态)
        await self._start_binding_channel(binder, cred)
        return web.json_response({"ok": True, "status": "confirmed", "binder": binder,
                                  "message": "绑定成功,消息通道已启动(无需重启)"})

    async def start_binding_channel(self, binder: str, cred: Any) -> None:
        """(公共)启动/替换某绑定的渠道(daemon 启动存量绑定时调用)。"""
        await self._start_binding_channel(binder, cred)

    def register_binding_channel(self, binder: str, channel: Any) -> None:
        """(公共)登记已有渠道到绑定(如 daemon 的首绑定复用)。"""
        self._wechat_channels[binder] = channel

    @property
    def wechat_channels(self) -> dict[str, Any]:
        return dict(self._wechat_channels)

    async def _start_binding_channel(self, binder: str, cred: Any) -> None:
        old = self._wechat_channels.get(binder)
        if old is not None and getattr(old, "is_running", False):
            old.stop()  # 同绑定者重绑:先停旧轮询(新 token 接管)
        if self._wechat_factory is not None:
            ch = self._wechat_factory(cred)
            if ch is not None and not getattr(ch, "is_running", False):
                ch.start(cred)
            self._wechat_channels[binder] = ch
        else:
            ch = self._wechat  # 兼容形态:单渠道替身(既有测试)
            if ch is not None and not getattr(ch, "is_running", False):
                ch.start(cred)

    async def _wechat_status(self, _request: web.Request) -> web.Response:
        from naga.wechat import load_bindings
        home = self._home or storage.resolve_home()
        bindings = await asyncio.to_thread(load_bindings, home)
        entries: list[dict[str, Any]] = []
        for binder, cred in bindings.items():
            ch = self._wechat_channels.get(binder, self._wechat)
            entries.append({"binder": binder, "bound_at": cred.bound_at,
                            "polling": bool(getattr(ch, "is_running", False))})
        return web.json_response({
            "ok": True,
            "bound": bool(entries),                       # 兼容:任一绑定在
            "polling": any(e["polling"] for e in entries) or
                       bool(getattr(self._wechat, "is_running", False)),
            "bindings": entries})

    async def _wechat_unbind(self, request: web.Request) -> web.Response:
        from naga.wechat import clear_credential, remove_binding
        home = self._home or storage.resolve_home()
        binder = request.query.get("binder")
        if binder is None:
            await asyncio.to_thread(clear_credential, home)  # 全量解绑(兼容旧语义)
            for ch in self._wechat_channels.values():
                ch.stop()
            self._wechat_channels.clear()
            if self._wechat is not None:
                self._wechat.stop()
            self._ledger.append("channel.unbound", "root",
                                {"channel": "wechat", "binder": None})
            return web.json_response({"ok": True, "message": "已全部解绑"})
        await asyncio.to_thread(remove_binding, binder, home)
        ch = self._wechat_channels.pop(binder, None)
        if ch is not None:
            ch.stop()
        self._ledger.append("channel.unbound", "root",
                            {"channel": "wechat", "binder": binder})
        from naga.wechat import load_bindings
        remaining = len(await asyncio.to_thread(load_bindings, home))
        return web.json_response({"ok": True, "message": f"已解绑 {binder}",
                                  "remaining": remaining})


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
