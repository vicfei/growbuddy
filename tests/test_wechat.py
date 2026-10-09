"""微信 iLink 渠道测试:客户端契约(8 接口,fake 请求函数)/ 渠道编排 /
凭据与状态 IO / 绑定路由 / fake 全链路。零外网。"""

import asyncio
import base64
import os
from typing import Any

from naga import storage
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.trust import derive_trust, grant_trust
from naga.wechat import (
    ILinkClient,
    WeChatChannel,
    WeChatCredential,
    clear_credential,
    extract_text,
    load_credential,
    load_state,
    save_credential,
    save_state,
)


class FakeReq:
    """脚本化请求函数:记录 (url, method, headers, body);按序弹出响应或异常。"""

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[tuple[str, str, dict, Any]] = []

    async def __call__(self, url, method, headers, body, timeout):
        self.calls.append((url, method, headers, body))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def _noop_runner(goal: str, session_key: str, actor: dict, ctx: dict) -> None:
    return None


# ---- 客户端契约 ----

def test_ilink_headers_and_getupdates_body():
    req = FakeReq([(200, {"ret": 0, "msgs": [], "get_updates_buf": "c1"})])
    client = ILinkClient("tok", "https://x", req)
    asyncio.run(client.get_updates(""))
    url, method, headers, body = req.calls[0]
    assert url.endswith("/ilink/bot/getupdates") and method == "POST"
    assert headers["AuthorizationType"] == "ilink_bot_token"   # 缺了就是 -14
    assert headers["authorization"] == "Bearer tok"
    assert 0 <= int(base64.b64decode(headers["X-WECHAT-UIN"]).decode()) <= 0xFFFFFFFF
    assert body["get_updates_buf"] == "" and "base_info" in body  # 不是 {timeout}


def test_get_updates_mapping_errcode_cursor():
    req = FakeReq([(200, {"ret": 0, "msgs": [{"from_user_id": "u1"}],
                          "get_updates_buf": "cur9"}),
                   (200, {"ret": -14, "errmsg": "bad token"})])
    client = ILinkClient("t", "https://x", req)
    r1 = asyncio.run(client.get_updates(None))
    assert r1.errcode == 0 and r1.updates[0]["from_user_id"] == "u1"
    assert r1.next_cursor == "cur9"
    r2 = asyncio.run(client.get_updates("cur9"))
    assert r2.errcode == -14 and req.calls[1][3]["get_updates_buf"] == "cur9"


def test_send_message_body_contract():
    req = FakeReq([(200, {"ret": 0}), (200, {"ret": 0})])
    client = ILinkClient("t", "https://x", req)
    assert asyncio.run(client.send_message("peer1", "你好", "ctx-1")).errcode == 0
    msg = req.calls[0][3]["msg"]
    assert msg["from_user_id"] == "" and msg["to_user_id"] == "peer1"
    assert msg["message_type"] == 2 and msg["message_state"] == 2
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]
    assert msg["context_token"] == "ctx-1"                     # 串线必传
    asyncio.run(client.send_message("peer1", "x", None))
    assert "context_token" not in req.calls[1][3]["msg"]       # 无 token 不带空串


def test_get_config_ilink_user_id_and_ticket_shapes():
    req = FakeReq([(200, {"ret": 0, "data": {"typing_ticket": "tk"}}),
                   (200, {"ret": 0, "typing_ticket": "tk2"})])
    client = ILinkClient("t", "https://x", req)
    r, ticket = asyncio.run(client.get_config("user-9"))
    assert r.errcode == 0 and ticket == "tk"
    assert req.calls[0][3]["ilink_user_id"] == "user-9"        # 缺了 ret=-2
    _, ticket2 = asyncio.run(client.get_config("user-9"))
    assert ticket2 == "tk2"                                    # 顶层与 data 嵌套双形态


def test_send_typing_status_values():
    req = FakeReq([(200, {"ret": 0}), (200, {"ret": 0})])
    client = ILinkClient("t", "https://x", req)
    asyncio.run(client.send_typing("u", "tk", 1))
    asyncio.run(client.send_typing("u", "tk", 2))              # 2=结束,不是 0
    assert req.calls[0][3]["status"] == 1 and req.calls[1][3]["status"] == 2


def test_binding_flow_statuses():
    req = FakeReq([(200, {"ret": 0, "qrcode": "Q1", "qrcode_img_content": "http://qr"}),
                   (200, {"ret": 0, "status": "scaned"}),
                   (200, {"ret": 0, "status": "confirmed", "bot_token": "bt",
                          "ilink_user_id": "iu", "baseurl": "https://b"}),
                   (500, {}),
                   (OSError("boom"))])
    b = asyncio.run(ILinkClient.request_qrcode(req))
    assert b.qrcode == "Q1" and b.qr_content == "http://qr"
    assert asyncio.run(ILinkClient.poll_binding_status("Q1", req)).status == "scaned"
    c = asyncio.run(ILinkClient.poll_binding_status("Q1", req))
    assert c.status == "confirmed" and c.bot_token == "bt" and c.baseurl == "https://b"
    assert asyncio.run(ILinkClient.poll_binding_status("Q1", req)).status == "wait"  # 非 200
    assert asyncio.run(ILinkClient.poll_binding_status("Q1", req)).status == "wait"  # 超时折算


# ---- 提取与凭据/状态 IO ----

def test_extract_text_fallbacks():
    assert extract_text({"content": "直接"}) == "直接"
    assert extract_text({"item_list": [{"type": 1, "text_item": {"text": "列表"}}]}) == "列表"
    assert extract_text({"item_list": [{"voice_item": {"text": "转写"}}]}) == "转写"
    assert extract_text({"data": {"text": "嵌套"}}) == "嵌套"
    assert extract_text({"detail": "d"}) == "d"
    assert extract_text({"item_list": [{"type": 2}]}) is None


def test_credential_and_state_io(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    assert load_credential(home) is None
    save_credential(WeChatCredential(bot_token="bt", bound_at="t"), home)
    assert os.stat(os.path.join(home, "credentials", "wechat-ilink.json")).st_mode & 0o777 == 0o600
    assert load_credential(home) is not None
    clear_credential(home)
    assert load_credential(home) is None  # 空对象视为未绑定
    st = load_state(home)
    st.context_tokens["p1"] = "tok1"
    save_state(st, home)
    assert load_state(home).context_tokens["p1"] == "tok1"
    assert os.stat(os.path.join(home, "state", "wechat-ilink-state.json")).st_mode & 0o777 == 0o600


# ---- 渠道编排 ----

def _chan(home, req, runner, ledger=None, expired=None):
    return WeChatChannel(
        home=home, runner=runner, request=req,
        on_token_expired=expired,
        ledger_append=(lambda k, o, p: ledger.append(k, o, p)) if ledger is not None else None)


def test_ingest_derives_trust_persists_token_session_open(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    seen: list[tuple[str, str, dict, dict]] = []

    async def runner(goal, session_key, actor, ctx):
        seen.append((goal, session_key, dict(actor), dict(ctx)))

    async def scenario():
        req = FakeReq([])
        ch = _chan(home, req, runner, ledger=led)
        ch._state = load_state(home)
        ch._client = ILinkClient("t", "https://x", req)
        ch._ingest({"from_user_id": "wx-u1",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                    "context_token": "ct-1"})
        await asyncio.sleep(0)
        ch._ingest({"from_user_id": "wx-u1", "content": "第二条", "context_token": "ct-1"})
        await asyncio.sleep(0)

    asyncio.run(scenario())
    goal, session_key, actor, ctx = seen[0]
    assert goal == "你好" and session_key == "wechat:dm:wx-u1"
    assert actor == {"kind": "human", "id": "wx-u1", "trust": "guest"}  # K.4 默认
    assert ctx == {"trust_source": "default"}
    assert load_state(home).context_tokens["wx-u1"] == "ct-1"   # 持久化串线
    opens = [e for e in led.entries if e["kind"] == "session.open"]
    assert len(opens) == 1 and opens[0]["payload"]["trust"] == "guest"  # 首见一次
    led.close()


def test_owner_allowlist_overrides_guest_default(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    tf = os.path.join(home, "trust.json")
    grant_trust("wechat", "boss", "owner", path=tf)
    assert derive_trust("wechat", "boss", path=tf).trust == "owner"


def test_err14_pauses_after_threshold_and_fires_callback(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    expired: list[bool] = []
    req = FakeReq([(200, {"ret": -14}) for _ in range(5)])      # 连续 5 次 -14
    ch = _chan(home, req, _noop_runner, expired=lambda: expired.append(True))

    async def scenario():
        WeChatChannel._ERR14_RETRY_SEC = 0                      # 测试压缩重试间隔
        ch.start(WeChatCredential(bot_token="t"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not ch.is_running:
                break
        ch.stop()

    asyncio.run(scenario())
    assert not ch.is_running and expired == [True]              # 暂停 + 回调


def test_reply_token_fallback(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    req = FakeReq([(200, {"ret": 0}), (200, {"ret": 0})])
    ch = _chan(home, req, _noop_runner)
    st = load_state(home)
    st.context_tokens["p"] = "persisted"
    ch._state = st
    ch._client = ILinkClient("t", "https://x", req)
    asyncio.run(ch.send_reply("p", "hi", "fresh"))              # 新 token 优先
    assert req.calls[0][3]["msg"]["context_token"] == "fresh"
    asyncio.run(ch.send_reply("p", "hi", None))                 # 持久态兜底
    assert req.calls[1][3]["msg"]["context_token"] == "persisted"


# ---- 绑定路由(fake 全链路) ----

def _qr_pair() -> list[Any]:
    return [(200, {"ret": 0, "qrcode": "Q1", "qrcode_img_content": "http://qr"}),
            (200, {"ret": 0, "status": "confirmed", "bot_token": "bt",
                   "ilink_user_id": "iu-1", "baseurl": "https://b"}),]


def test_bind_routes_full_flow(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    script = _qr_pair()
    started: list[str] = []

    class FakeChannel:
        is_running = False

        def start(self, cred):
            self.is_running = True
            started.append(cred.bot_token)

        def stop(self):
            self.is_running = False

    async def scenario():
        import aiohttp
        led = DurableLedger(home)
        req: Any = FakeReq(script)
        gw = Gateway(led, make_adapter=lambda: _NoopAdapter(), home=home, wechat_request=req)
        fake_ch = FakeChannel()
        gw.attach_wechat(fake_ch)
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                    j = await r.json()
                    assert r.status == 200 and j["ok"] and j["qrcode"] == "Q1"
                    assert j["qr_data_url"].startswith("data:image/png;base64,")  # 本地生成
                async with s.get(
                        f"http://127.0.0.1:{port}/wechat/bind/poll/Q1") as r:
                    j = await r.json()
                    assert j["status"] == "confirmed"
                async with s.get(f"http://127.0.0.1:{port}/wechat/status") as r:
                    j = await r.json()
                    assert j["bound"] is True and j["polling"] is True
                async with s.get(f"http://127.0.0.1:{port}/wechat/unbind") as r:
                    assert (await r.json())["ok"] is True
                async with s.get(f"http://127.0.0.1:{port}/wechat/status") as r:
                    j = await r.json()
                    assert j["bound"] is False and j["polling"] is False
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())
    assert started == ["bt"]                                   # 热启动
    cred = load_credential(home)
    assert cred is None                                        # unbind 清除
    tf = os.path.join(home, "trust.json")
    assert derive_trust("wechat", "iu-1", path=tf).trust == "owner"  # 自动授 owner


class _NoopAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="微信回复", prompt_tokens=1, completion_tokens=1)


def test_full_chain_message_to_reply_via_gateway_lane(tmp_path):
    """全链路:微信消息 → 信任派生 → 网关 lane → AgentLoop(fake LLM)→ 回复 sendMessage。"""
    home = str(tmp_path / "h")
    storage.init_home(home)

    class RoutingFakeReq:
        """单客户端双剧本:按 URL 分发 getupdates / sendmessage(顺序确定)。"""

        def __init__(self):
            self.poll = FakeReq([(200, {"ret": 0, "msgs": [{
                "from_user_id": "wx-friend",
                "item_list": [{"type": 1, "text_item": {"text": "帮我调研量子计算"}}],
                "context_token": "ct-x"}], "get_updates_buf": "c2"})]
                + [(200, {"ret": 0, "msgs": [], "get_updates_buf": "c3"})] * 8)
            self.sends = FakeReq([(200, {"ret": 0})])

        async def __call__(self, url, method, headers, body, timeout):
            if url.endswith("/sendmessage"):
                return await self.sends(url, method, headers, body, timeout)
            return await self.poll(url, method, headers, body, timeout)

    router = RoutingFakeReq()
    ch_box: dict[str, WeChatChannel] = {}
    led = DurableLedger(home)          # gw/led 提到 runner 闭包可见层
    gw = Gateway(led, make_adapter=lambda: _NoopAdapter(), home=home)

    async def runner(goal: str, session_key: str, actor: dict, ctx: dict) -> None:
        async def job() -> None:
            result = await gw.run_agent(session_key=session_key, goal=goal,
                                        budget=50_000, trace_id="tr_fullchain000000",
                                        actor_trust=actor["trust"])
            peer = session_key.split(":", 2)[2]
            await ch_box["ch"].send_reply(peer, result.reply or f"({result.outcome})", None)
            await gw.broadcast("agent.completed", {
                "session_key": session_key, "trace_id": "tr_fullchain000000",
                "reply": result.reply, "outcome": result.outcome, "tokens": result.tokens})
        await gw.submit(session_key, job)                          # 经网关 lane

    async def scenario():
        ch = WeChatChannel(
            home=home, runner=runner, request=router,
            ledger_append=lambda k, o, p: led.append(k, o, p))
        ch_box["ch"] = ch
        gw.attach_wechat(ch)
        await gw.start("127.0.0.1", 0)
        try:
            ch.start(WeChatCredential(bot_token="t"))
            for _ in range(200):
                await asyncio.sleep(0.02)
                if len(router.sends.calls) >= 1:
                    break
            ch.stop()
            assert gw._queues and len(led.query("SELECT * FROM traces")) >= 1
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())
    assert len(router.sends.calls) == 1
    url, _method, _headers, body = router.sends.calls[0]
    assert url.endswith("/ilink/bot/sendmessage")
    assert body["msg"]["to_user_id"] == "wx-friend"
    assert body["msg"]["context_token"] == "ct-x"               # 串线
    assert body["msg"]["item_list"][0]["text_item"]["text"] == "微信回复"
