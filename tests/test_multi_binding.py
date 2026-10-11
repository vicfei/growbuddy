"""Multi-binding tests: two WeChat binders, equal owner status, independent
channels; per-binding unbind; rebinding replaces; legacy single-file migration.

All flows use the injected fake iLink (zero network)."""

import asyncio
import json
from typing import Any

from naga import storage
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.trust import derive_trust
from naga.wechat import (
    WeChatCredential,
    bindings_path,
    credential_path,
    load_bindings,
    save_binding,
)


class _MultiReq:
    """fake iLink:每个二维码绑定会话独立剧本(按 qrcode 路由,支持并发)。"""

    def __init__(self, binder_for_q1: str = "iu-1"):
        self._binder_for_q1 = binder_for_q1

    async def __call__(self, url, method, headers, body, timeout):
        if "get_bot_qrcode" in url:
            self._round = getattr(self, "_round", 0) + 1
            return 200, {"ret": 0, "qrcode": f"Q{self._round}",
                         "qrcode_img_content": "http://qr"}
        if "get_qrcode_status" in url:
            n = url.split("qrcode=Q")[1][:1]
            binder = self._binder_for_q1 if n == "1" else f"iu-{n}"
            return 200, {"ret": 0, "status": "confirmed",
                         "bot_token": f"bt-{n}", "ilink_user_id": binder,
                         "baseurl": "https://b"}
        await asyncio.sleep(0.02)
        return 200, {"ret": 0, "msgs": [], "get_updates_buf": "c"}


class _Chan:
    def __init__(self) -> None:
        self.is_running = False
        self.started: list[str] = []

    def start(self, cred) -> None:
        self.is_running = True
        self.started.append(cred.bot_token)

    def stop(self) -> None:
        self.is_running = False


class _NoopAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)


def _gateway(tmp_path, req) -> tuple[Gateway, list[_Chan], DurableLedger]:
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    made: list[_Chan] = []

    def factory(cred: Any) -> _Chan:
        ch = _Chan()
        made.append(ch)
        return ch

    gw = Gateway(led, make_adapter=lambda: _NoopAdapter(), home=home,
                 wechat_request=req)
    gw.attach_wechat_factory(factory)
    return gw, made, led


def test_two_binders_sequential(tmp_path):
    req = _MultiReq()
    gw, made, led = _gateway(tmp_path, req)

    async def scenario():
        import aiohttp
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                    assert (await r.json())["qrcode"] == "Q1"
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/Q1") as r:
                    j = await r.json()
                    assert j["status"] == "confirmed" and j["binder"] == "iu-1"
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                    assert (await r.json())["qrcode"] == "Q2"
                async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/Q2") as r:
                    j = await r.json()
                    assert j["binder"] == "iu-2"
                async with s.get(f"http://127.0.0.1:{port}/wechat/status") as r:
                    j = await r.json()
                    assert j["bound"] is True and j["polling"] is True
                    assert [(b["binder"], b["polling"]) for b in j["bindings"]] == \
                        [("iu-1", True), ("iu-2", True)]
        finally:
            await gw.stop()

    asyncio.run(scenario())
    assert len(made) == 2 and all(ch.is_running for ch in made)   # 双渠道并行
    assert made[0].started == ["bt-1"] and made[1].started == ["bt-2"]
    home = str(tmp_path / "h")
    bindings = load_bindings(home)
    assert set(bindings) == {"iu-1", "iu-2"}
    tf = f"{home}/trust.json"
    assert derive_trust("wechat", "iu-1", path=tf).trust == "owner"  # 地位一致
    assert derive_trust("wechat", "iu-2", path=tf).trust == "owner"
    rows = led.query("SELECT payload_cas IS NOT NULL, kind FROM ledger_entries "
                     "WHERE kind='channel.bound' ORDER BY seq")
    assert len(rows) == 2
    led.close()


def test_two_binders_concurrent_qr_sessions(tmp_path):
    """两个绑定会话并发挂起(两张二维码同时有效),先后确认。"""
    req = _MultiReq()
    gw, made, led = _gateway(tmp_path, req)

    async def scenario():
        import aiohttp
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                codes = []
                for _ in range(2):
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                        codes.append((await r.json())["qrcode"])
                assert codes == ["Q1", "Q2"]
                for q in codes:
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/{q}") as r:
                        assert (await r.json())["status"] == "confirmed"
        finally:
            await gw.stop()

    asyncio.run(scenario())
    assert len(made) == 2 and all(ch.is_running for ch in made)
    led.close()


def test_rebind_same_binder_replaces_channel(tmp_path):
    class _SameReq(_MultiReq):
        """两张二维码都返回同一 binder(重绑同一用户)。"""
        async def __call__(self, url, method, headers, body, timeout):
            status, data = await super().__call__(url, method, headers, body, timeout)
            if isinstance(data, dict) and data.get("status") == "confirmed":
                data["ilink_user_id"] = "iu-same"
            return status, data

    gw, made, led = _gateway(tmp_path, _SameReq())

    async def scenario():
        import aiohttp
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                for expect_round in ("Q1", "Q2"):
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                        q = (await r.json())["qrcode"]
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/{q}") as r:
                        assert (await r.json())["binder"] == "iu-same"
        finally:
            await gw.stop()

    asyncio.run(scenario())
    assert len(made) == 2
    assert made[0].is_running is False      # 旧渠道已被停(新 token 接管)
    assert made[1].is_running is True
    assert set(load_bindings(str(tmp_path / "h"))) == {"iu-same"}
    led.close()


def test_unbind_one_keeps_other(tmp_path):
    req = _MultiReq()
    gw, made, led = _gateway(tmp_path, req)

    async def scenario():
        import aiohttp
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                for q in ("Q1", "Q2"):
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                        code = (await r.json())["qrcode"]
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/{code}") as r:
                        await r.json()
                async with s.get(f"http://127.0.0.1:{port}/wechat/unbind?binder=iu-1") as r:
                    j = await r.json()
                    assert j["ok"] and j["remaining"] == 1
                async with s.get(f"http://127.0.0.1:{port}/wechat/status") as r:
                    j = await r.json()
                    assert [b["binder"] for b in j["bindings"]] == ["iu-2"]
                    assert j["bindings"][0]["polling"] is True
        finally:
            await gw.stop()

    asyncio.run(scenario())
    assert made[0].is_running is False and made[1].is_running is True
    assert set(load_bindings(str(tmp_path / "h"))) == {"iu-2"}
    led.close()


def test_unbind_all_clears_everything(tmp_path):
    req = _MultiReq()
    gw, made, led = _gateway(tmp_path, req)

    async def scenario():
        import aiohttp
        port = await gw.start("127.0.0.1", 0)
        try:
            async with aiohttp.ClientSession() as s:
                for q in ("Q1", "Q2"):
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/start") as r:
                        code = (await r.json())["qrcode"]
                    async with s.get(f"http://127.0.0.1:{port}/wechat/bind/poll/{code}") as r:
                        await r.json()
                async with s.get(f"http://127.0.0.1:{port}/wechat/unbind") as r:
                    assert (await r.json())["ok"]
                async with s.get(f"http://127.0.0.1:{port}/wechat/status") as r:
                    j = await r.json()
                    assert j["bound"] is False and j["bindings"] == []
        finally:
            await gw.stop()

    asyncio.run(scenario())
    assert all(not ch.is_running for ch in made)
    assert load_bindings(str(tmp_path / "h")) == {}
    led.close()


def test_legacy_single_file_absorbed(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    legacy = WeChatCredential(bot_token="bt-old", ilink_user_id="iu-old",
                              bound_at="t")
    with open(credential_path(home), "w", encoding="utf-8") as fh:
        fh.write(legacy.model_dump_json())          # 仅旧单文件(无多绑定仓)
    assert load_bindings(home) == {"iu-old": legacy}  # 吸收迁移
    save_binding(WeChatCredential(bot_token="bt-new", ilink_user_id="iu-new",
                                  bound_at="t2"), home)
    bindings = load_bindings(home)
    assert set(bindings) == {"iu-old", "iu-new"}     # 迁移条目与新增共存


def test_bindings_store_is_private(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    save_binding(WeChatCredential(bot_token="bt", ilink_user_id="iu-x",
                                  bound_at="t"), home)
    import os
    assert os.stat(bindings_path(home)).st_mode & 0o777 == 0o600
    with open(bindings_path(home), encoding="utf-8") as fh:
        assert set(json.load(fh)) == {"iu-x"}
