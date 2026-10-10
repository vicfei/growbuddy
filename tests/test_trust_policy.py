"""批次 4 测试:§10.1 信任级策略(预算封顶/R0 工具/untrusted 拒绝)
+ §4.1 事件可见性矩阵(逐格)+ 非(owner) 对端全链路降权。"""

import asyncio
import json
from typing import Any

import pytest

from naga import storage
from naga.agent import AgentResult
from naga.daemon import OWNER_TASK_BASE_BUDGET, reply_for
from naga.gateway import (
    GUEST_TASK_TOKEN_BUDGET,
    Gateway,
    event_visible_to,
    task_budget,
    tools_for_trust,
)
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.wechat import ILinkClient, WeChatChannel

# ---- §10.1 预算矩阵 ----

@pytest.mark.parametrize("trust,requested,expected", [
    ("guest", 200_000, 50_000),      # guest 封顶(spec-constants guest_task_token_budget)
    ("guest", 30_000, 30_000),       # 低于上限不抬升
    ("known", 200_000, 200_000),     # known 上限 500k,基线 200k 不触顶
    ("known", 800_000, 500_000),     # known 封顶
    ("owner", 999_999, 999_999),     # owner 自设
    (None, 123_456, 123_456),        # 未标注(WS 路径=回环 owner)
    ("untrusted", 200_000, 0),       # 仅 hello+只读事件流
    ("weird-level", 100, 0),         # 未知级宁严勿宽
])
def test_task_budget_matrix(trust, requested, expected):
    assert task_budget(trust, requested) == expected


def test_guest_budget_constant_is_spec_value():
    assert GUEST_TASK_TOKEN_BUDGET == 50_000  # spec-constants §10.1


# ---- §10.1 R0 工具封顶 ----

class _ReadTool:
    name, description, parameters = "web_search", "搜", {"type": "object"}
    side_effect = "read"


class _WriteTool:
    name, description, parameters = "send_mail", "发", {"type": "object"}
    side_effect = "write"


class _UndeclaredTool:
    name, description, parameters = "mystery", "?", {"type": "object"}


def test_tools_for_trust_guest_r0_only():
    tools = [_ReadTool(), _WriteTool(), _UndeclaredTool()]
    guest_tools = tools_for_trust("guest", tools)
    assert [t.name for t in guest_tools] == ["web_search"]  # 只留显式 read
    assert [t.name for t in tools_for_trust("owner", tools)] == \
        ["web_search", "send_mail", "mystery"]
    # known 档位 R1(§10.1):write(R2)与未声明(R4)同样不可见
    assert [t.name for t in tools_for_trust("known", tools)] == ["web_search"]
    assert tools_for_trust(None, tools) == tools
    assert tools_for_trust("untrusted", tools) == []      # 未知/拒绝级无工具


# ---- run_agent 行为:封顶→aborted / untrusted→rejected / R0 过滤 ----

class _HeavyAdapter:
    """单步即耗 60k tokens——超过 guest 上限,低于 owner 基线。"""
    calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        type(self).calls += 1
        return ChatResponse(content="done", prompt_tokens=100, completion_tokens=59_900)


class _CountingAdapter:
    calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        type(self).calls += 1
        return ChatResponse(content="ok", prompt_tokens=10, completion_tokens=10)


def _gateway(tmp_path, adapter_cls) -> tuple[Gateway, DurableLedger]:
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    return Gateway(led, make_adapter=lambda: adapter_cls(), home=home), led


def test_run_agent_guest_budget_cap_aborts(tmp_path):
    gw, led = _gateway(tmp_path, _HeavyAdapter)

    async def scenario():
        guest = await gw.run_agent(session_key="wechat:dm:peer1", goal="长任务",
                                   budget=200_000, trace_id="tr_g1", actor_trust="guest")
        owner = await gw.run_agent(session_key="wechat:dm:peer2", goal="长任务",
                                   budget=200_000, trace_id="tr_o1", actor_trust="owner")
        return guest, owner

    guest, owner = asyncio.run(scenario())
    assert guest.outcome == "aborted" and guest.failure_analysis == "预算耗尽"
    assert owner.outcome == "success"
    led.close()


def test_run_agent_untrusted_rejected_without_llm_call(tmp_path):
    _CountingAdapter.calls = 0
    gw, led = _gateway(tmp_path, _CountingAdapter)

    async def scenario():
        return await gw.run_agent(session_key="wechat:dm:bad", goal="hi",
                                  budget=200_000, trace_id="tr_u1",
                                  actor_trust="untrusted")

    result = asyncio.run(scenario())
    assert result.outcome == "rejected"
    assert "信任级" in (result.reply or "")
    assert _CountingAdapter.calls == 0  # 未触达模型即拒绝
    led.close()


def test_run_agent_guest_tool_filter_reaches_loop(tmp_path):
    """guest 任务的 AgentLoop 只见 R0 工具(以 trace 步态断言太重;此处用
    spec 注入断言——_HeavyAdapter 不带 tool_calls,以 tools 列表直达为准)。"""
    seen: list[list[str]] = []

    class _SpyAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            seen.append([t.name for t in tools] if tools else [])
            return ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)

    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    gw = Gateway(led, make_adapter=lambda: _SpyAdapter(),
                 tools=[_ReadTool(), _WriteTool()], home=home)

    async def scenario():
        await gw.run_agent(session_key="wechat:dm:p", goal="g", budget=1000,
                           trace_id="tr_t1", actor_trust="guest")
        await gw.run_agent(session_key="wechat:dm:p", goal="g", budget=1000,
                           trace_id="tr_t2", actor_trust="owner")

    asyncio.run(scenario())
    assert seen[0] == ["web_search"]   # guest:写类工具不可见
    # owner 自 M3-1 起额外可见派生工具(授权代数单点装配)
    assert seen[1] == ["web_search", "send_mail", "spawn_subagents"]
    led.close()


# ---- §4.1 事件可见性矩阵(逐格;M2 子集 = 会话作用域 agent.* 事件) ----

@pytest.mark.parametrize("viewer,viewer_session,event,session_key,expected", [
    ("owner", None, "agent.completed", "wechat:dm:a", True),      # owner 全部
    ("owner", None, "agent.failed", None, True),
    ("guest", "s1", "agent.completed", "s1", True),               # guest 本会话 ✅
    ("guest", "s1", "agent.completed", "s2", False),              # 他人会话 ❌
    ("guest", None, "agent.completed", "s1", False),              # 未绑定会话身份 ❌
    ("guest", "s1", "skill.commit", "s1", True),                  # 资产事件=summary 可见(§4.1)
    ("known", "s1", "agent.completed", "s1", True),               # known 本会话 ✅
    ("known", "s1", "agent.completed", "s9", False),
    ("untrusted", "s1", "agent.completed", "s1", False),          # live 流不可见
    ("untrusted", None, "session.message", None, False),
    ("node", None, "agent.completed", "wechat:dm:a", False),      # node 仅自身相关(M2 无)
])
def test_event_visibility_matrix(viewer, viewer_session, event, session_key, expected):
    assert event_visible_to(viewer, viewer_session, event, session_key) is expected


# ---- §4.1 全矩阵逐格(decision 四态;事件类 × 信任级) ----

@pytest.mark.parametrize("event,viewer,viewer_session,session_key,expected", [
    # session 类(agent./session.)
    ("agent.completed", "owner", None, "any", "full"),
    ("agent.completed", "guest", "s1", "s1", "full"),             # 本会话全文
    ("agent.completed", "guest", "s1", "s2", "none"),             # 他人会话
    ("session.message", "untrusted", "s1", "s1", "none"),
    ("agent.completed", "node", None, "s1", "none"),
    # asset 类(skill./promotion./memory.):guest 仅摘要
    ("skill.commit", "guest", None, None, "summary"),
    ("promotion.approved", "guest", None, None, "summary"),
    ("memory.write", "known", None, None, "full"),
    ("skill.commit", "untrusted", None, None, "none"),
    ("skill.commit", "node", None, None, "none"),
    # ops 类(job./shadow./health.):guest 不可见
    ("job.fire", "guest", None, None, "none"),
    ("health.loop_score", "known", None, None, "full"),
    ("shadow.diff", "owner", None, None, "full"),
    # governance 类(rollback./trust./constitution.):known 内容裁剪,node 自身相关
    ("trust.linked", "known", None, None, "summary"),
    ("rollback.marker", "guest", None, None, "none"),
    ("constitution.violation", "owner", None, None, "full"),
    ("trust.anchor_missing", "node", "n1", "n1", "full"),         # 自身相关(M2 语义占位)
    ("trust.anchor_missing", "node", "n1", "n2", "none"),
    # node 类:仅 owner 全文 / node 自身
    ("node.joined", "known", None, None, "none"),
    ("node.joined", "node", "n1", "n1", "full"),
    # 未登记前缀:宁严勿宽,仅 owner
    ("whoami.what", "known", None, None, "none"),
    ("whoami.what", "owner", None, None, "full"),
])
def test_event_decision_full_matrix(event, viewer, viewer_session, session_key, expected):
    from naga.gateway import event_decision
    assert event_decision(viewer, viewer_session, event, session_key) == expected


def test_event_class_classification():
    from naga.gateway import event_class
    assert event_class("agent.completed") == "session"
    assert event_class("session.open") == "session"
    assert event_class("skill.use") == "asset"
    assert event_class("promotion.granted") == "asset"
    assert event_class("memory.forget") == "asset"
    assert event_class("job.missed") == "ops"
    assert event_class("shadow.diff") == "ops"
    assert event_class("health.loop_score") == "ops"
    assert event_class("rollback.marker") == "governance"
    assert event_class("trust.fused") == "governance"
    assert event_class("constitution.violation") == "governance"
    assert event_class("node.joined") == "node"
    assert event_class("mystery.event") == "other"


# ---- §5.6 cap→R 映射逐格 ----

class _RiskyTool:
    def __init__(self, name: str, risk: int | None):
        self.name, self.description, self.parameters = name, "d", {"type": "object"}
        if risk is not None:
            self.risk_level = risk


def test_tool_risk_precedence():
    from naga.gateway import tool_risk
    assert tool_risk(_RiskyTool("explicit", 3)) == 3            # 显式 risk_level 优先
    assert tool_risk(_ReadTool()) == 0                          # side_effect=read → R0
    assert tool_risk(_WriteTool()) == 2                         # side_effect=write → R2
    assert tool_risk(_UndeclaredTool()) == 4                    # 未声明默认 R4(§5.6)


def test_tools_for_trust_r_ceilings():
    tools = [_RiskyTool("r0", 0), _RiskyTool("r1", 1),
             _RiskyTool("r2", 2), _RiskyTool("r4", 4)]
    assert [t.name for t in tools_for_trust("guest", tools)] == ["r0"]        # R0 封顶
    assert [t.name for t in tools_for_trust("known", tools)] == ["r0", "r1"]  # R1 封顶
    assert [t.name for t in tools_for_trust("owner", tools)] == \
        ["r0", "r1", "r2", "r4"]                                              # 无封顶
    assert tools_for_trust("weird", tools) == []                              # 未知级


def test_web_search_declared_r0():
    from naga.gateway import tool_risk
    from naga.tools import WebSearchTool
    assert WebSearchTool.risk_level == 0
    assert tool_risk(WebSearchTool) == 0


# ---- summary 投递(§4.1 guest/known 内容裁剪) ----

def test_summarize_event_clips_to_status_fields():
    from naga.gateway import summarize_event
    payload = {"name": "web-research", "version": 3, "status": "active",
               "branch": "local:s1", "content": "全文不外泄", "trace_id": "tr_x"}
    clipped = summarize_event(payload)
    assert clipped == {"name": "web-research", "version": 3, "status": "active",
                       "summarized": True}


def test_broadcast_delivers_summary_to_guest_full_to_owner(tmp_path):
    from naga.gateway import summarize_event  # noqa: F401 —— 同族断言见下
    gw, led = _gateway(tmp_path, _CountingAdapter)
    owner_ws, guest_ws = _FakeWS(), _FakeWS()
    gw._conns.update({owner_ws, guest_ws})
    gw._conn_trust[id(owner_ws)] = "owner"
    gw._conn_trust[id(guest_ws)] = "guest"

    async def scenario():
        await gw.broadcast("skill.commit", {"name": "s", "version": 2,
                                            "status": "active",
                                            "content": "全文仅 owner 可见"})

    asyncio.run(scenario())
    assert owner_ws.frames[0]["payload"]["content"] == "全文仅 owner 可见"
    guest_payload = guest_ws.frames[0]["payload"]
    assert guest_payload == {"name": "s", "version": 2, "status": "active",
                             "summarized": True}
    led.close()


class _FakeWS:
    """广播投递探测:记录 send_json 帧。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, obj: dict) -> None:
        self.frames.append(obj)


def test_broadcast_filters_per_connection_trust(tmp_path):
    gw, led = _gateway(tmp_path, _CountingAdapter)
    owner_ws, guest_ws, stranger_ws = _FakeWS(), _FakeWS(), _FakeWS()
    gw._conns.update({owner_ws, guest_ws, stranger_ws})
    gw._conn_trust[id(owner_ws)] = "owner"
    gw._conn_trust[id(guest_ws)] = "guest"
    # stranger_ws 故意不登记 → 按未标注 untrusted(宁严勿宽)

    async def scenario():
        await gw.broadcast("agent.completed", {"session_key": "wechat:dm:peer",
                                               "reply": "hi"})

    asyncio.run(scenario())
    assert len(owner_ws.frames) == 1                      # owner 收到
    assert owner_ws.frames[0]["event"] == "agent.completed"
    assert guest_ws.frames == []                          # guest 无会话绑定 → 不可见
    assert stranger_ws.frames == []                       # 未登记连接 → 不可见
    led.close()


def test_webchat_hello_registers_owner_trust(tmp_path):
    """真实 WS:hello 之后事件可达(回环 owner 注册生效)。"""
    gw, led = _gateway(tmp_path, _CountingAdapter)

    async def scenario():
        port = await gw.start("127.0.0.1", 0)
        import aiohttp
        async with (aiohttp.ClientSession() as http,
                    http.ws_connect(f"http://127.0.0.1:{port}/gw") as ws):
                await ws.send_json({"type": "hello", "proto": "samsara/1",
                                    "auth": {"token": ""}})
                ok = await ws.receive_json()
                assert ok["type"] == "hello-ok"
                await gw.broadcast("agent.completed", {"session_key": "wechat:dm:x"})
                evt = await ws.receive(timeout=5)
                assert evt.type.name == "TEXT"
                assert json.loads(evt.data)["event"] == "agent.completed"
        await gw.stop()

    asyncio.run(scenario())
    led.close()


# ---- 非_owner 对端全链路(渠道视角) ----

class _PeerReq:
    """微信渠道 fake:统计 sendMessage,其余 ret=0。"""

    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def __call__(self, url, method, headers, body, timeout):
        if url.endswith("/sendmessage"):
            self.sends.append(body["msg"])
            return 200, {"ret": 0}
        if url.endswith("/getconfig"):
            return 200, {"ret": 0}  # 无 ticket → typing 静默降级
        return 200, {"ret": 0, "msgs": [], "get_updates_buf": "c"}


def _channel_with_gateway(tmp_path, trust_file: str | None):
    """channel + gateway + daemon 同构 runner(预算封顶全链路)。"""
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    gw = Gateway(led, make_adapter=lambda: _HeavyAdapter(), home=home)
    req = _PeerReq()
    box: dict[str, Any] = {}

    async def runner(goal, session_key, actor, ctx):
        trace_id = "tr_" + "0" * 20
        result = await gw.run_agent(session_key=session_key, goal=goal,
                                    budget=OWNER_TASK_BASE_BUDGET, trace_id=trace_id,
                                    actor_trust=actor["trust"])
        peer = session_key.split(":", 2)[2]
        ch: WeChatChannel = box["ch"]
        await ch.send_reply(peer, reply_for(result), None)
        box.setdefault("actors", []).append((dict(actor), dict(ctx)))

    # trust_file 显式指向 tmp(缺省 None 会按 home 解析读到真实 ~/.naga)
    box["ch"] = WeChatChannel(home=home, runner=runner, request=req,
                              trust_file=trust_file or str(tmp_path / "trust-absent.json"),
                              typing_refresh_sec=99)
    box["ch"]._client = ILinkClient("t", "https://x", req)
    box["req"] = req
    box["led"] = led
    return box



def test_unknown_peer_runs_as_guest_capped(tmp_path):
    """非 owner 对端(未列名微信 peer)全链路:guest 派生 → 预算封顶 → aborted 文案。"""
    box = _channel_with_gateway(tmp_path, trust_file=None)

    async def scenario():
        box["ch"]._ingest({"from_user_id": "stranger@im.wechat",
                           "content": "超长任务", "context_token": "ct"})
        for _ in range(300):
            await asyncio.sleep(0.01)
            if box["req"].sends:
                await asyncio.sleep(0.05)
                return

    asyncio.run(scenario())
    actor, ctx = box["actors"][0]
    assert actor["trust"] == "guest" and ctx["trust_source"] == "default"  # K.4 未列名
    reply = box["req"].sends[0]["item_list"][0]["text_item"]["text"]
    assert "预算上限" in reply and "50,000" in reply  # 升级路径提示(§10.1)
    box["led"].close()


def test_untrusted_peer_gets_friendly_rejection(tmp_path):
    """trust.json 将渠道默认降为 untrusted:不跑模型,友好拒绝文案。"""
    trust_file = str(tmp_path / "trust.json")
    with open(trust_file, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "channels": {
            "wechat": {"default_trust": "untrusted", "allowlist": []}}}, fh)
    _HeavyAdapter.calls = 0
    box = _channel_with_gateway(tmp_path, trust_file=trust_file)

    async def scenario():
        box["ch"]._ingest({"from_user_id": "who@im.wechat",
                           "content": "hi", "context_token": "ct"})
        for _ in range(300):
            await asyncio.sleep(0.01)
            if box["req"].sends:
                await asyncio.sleep(0.05)
                return

    asyncio.run(scenario())
    actor, _ = box["actors"][0]
    assert actor["trust"] == "untrusted"
    reply = box["req"].sends[0]["item_list"][0]["text_item"]["text"]
    assert "信任级" in reply
    assert _HeavyAdapter.calls == 0  # 未触达模型
    box["led"].close()


# ---- daemon.reply_for 文案矩阵 ----

def test_reply_for_branches():
    assert reply_for(AgentResult(reply="答案", outcome="success", trace_id="t",
                                 steps=1, tokens=1, cost_cents=0)) == "答案"
    assert "未产出正文" in reply_for(AgentResult(reply=None, outcome="success",
                                                 trace_id="t", steps=1, tokens=1, cost_cents=0))
    assert "预算上限" in reply_for(AgentResult(reply=None, outcome="aborted", trace_id="t",
                                               steps=1, tokens=1, cost_cents=0,
                                               failure_analysis="预算耗尽"))
    assert "(aborted) 步数上限" == reply_for(
        AgentResult(reply=None, outcome="aborted", trace_id="t", steps=9, tokens=1,
                    cost_cents=0, failure_analysis="步数上限"))
    assert "(rejected)" in reply_for(
        AgentResult(reply=None, outcome="rejected", trace_id="t", steps=0, tokens=0,
                    cost_cents=0, failure_analysis=None))
