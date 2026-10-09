"""微信个人号 Bot 通道(M2-W1 文本全链路)。

底座:腾讯 iLink Bot API(ilinkai.weixin.qq.com)——纯出站长轮询,宪法层
不破(默认回环绑定由网关保证)。契约以 Samsara 生产代码为准(逆向自
Weknora),对照表见 docs/research/wechat-ilink-borrowing.md。

W1 范围:文本收发全链路 + 绑定 + context_token 持久化 + -14 自愈暂停;
typing/分段/媒体属 W2/W3。请求函数构造注入(fake 零网络契约测试)。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
from pydantic import BaseModel

from naga.trust import derive_trust

ILINK_BASE = "https://ilinkai.weixin.qq.com"
ERR14_PAUSE_THRESHOLD = 5      # wechat_err14_pause_threshold 候选(随 #23 批 RFC 入注册表)
BACKOFF_MAX_SEC = 60           # wechat_poll_backoff_max_sec 候选
REPLY_HARD_LIMIT = 2000        # 单条上限;分段策略属 W2

# 请求函数:(url, method, headers, json_body|None, timeout_sec) -> (status, parsed_json)
RequestFn = Callable[[str, str, dict[str, str], dict[str, Any] | None, float],
                     Awaitable[tuple[int, Any]]]


async def _httpx_request(url: str, method: str, headers: dict[str, str],
                         body: dict[str, Any] | None, timeout: float) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=headers, json=body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return cast("dict[str, Any]", data)
    return {}


def _errcode(data: dict[str, Any]) -> int:
    raw = data.get("ret")
    if raw is None:
        raw = data.get("errcode", 0)
    return int(raw)


def _ilink_uin() -> str:
    # X-WECHAT-UIN:base64(str(random_uint32))——服务端不校验具体值
    return base64.b64encode(str(random.randrange(0xFFFFFFFF)).encode()).decode()


class QRCodeBinding(BaseModel):
    qrcode: str
    qr_content: str


class BindingStatus(BaseModel):
    status: str  # wait | scaned | confirmed | expired
    bot_token: str | None = None
    ilink_user_id: str | None = None
    baseurl: str | None = None


class UpdateResponse(BaseModel):
    errcode: int = 0
    errmsg: str | None = None
    updates: list[dict[str, Any]] = []
    next_cursor: str | None = None


class SendResult(BaseModel):
    errcode: int = 0
    errmsg: str | None = None


class ILinkClient:
    """iLink API 客户端(纯出站,长轮询 ~35s;请求函数注入供测试)。"""

    def __init__(self, token: str, baseurl: str = ILINK_BASE,
                 request: RequestFn = _httpx_request):
        self._token = token
        self._baseurl = baseurl
        self._request = request

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json",
                "authorization": f"Bearer {self._token}",
                "AuthorizationType": "ilink_bot_token",  # 缺了就是 -14
                "X-WECHAT-UIN": _ilink_uin()}

    @staticmethod
    async def request_qrcode(request: RequestFn = _httpx_request) -> QRCodeBinding:
        status, data = await request(f"{ILINK_BASE}/ilink/bot/get_bot_qrcode?bot_type=3",
                                     "GET", {"accept": "application/json"}, None, 15.0)
        d = _as_dict(data)
        qrcode, qr = d.get("qrcode"), d.get("qrcode_img_content")
        if status != 200 or _errcode(d) != 0 or not qrcode or not qr:
            raise ValueError(f"获取二维码失败: HTTP {status} ret={_errcode(d)}")
        return QRCodeBinding(qrcode=str(qrcode), qr_content=str(qr))

    @staticmethod
    async def poll_binding_status(qrcode: str,
                                  request: RequestFn = _httpx_request) -> BindingStatus:
        try:
            status, data = await request(
                f"{ILINK_BASE}/ilink/bot/get_qrcode_status?qrcode={qrcode}",
                "GET", {"accept": "application/json"}, None, 38.0)
        except (TimeoutError, httpx.HTTPError, OSError):
            return BindingStatus(status="wait")  # 超时优雅折算为 wait
        d = _as_dict(data)
        if status != 200 or _errcode(d) != 0:
            return BindingStatus(status="wait")
        st = str(d.get("status", "wait"))
        if st == "confirmed":
            return BindingStatus(status="confirmed",
                                 bot_token=d.get("bot_token"),
                                 ilink_user_id=d.get("ilink_user_id"),
                                 baseurl=d.get("baseurl"))
        return BindingStatus(status=st if st in ("scaned", "expired") else "wait")

    async def get_updates(self, get_updates_buf: str | None) -> UpdateResponse:
        # Weknora 契约:body = {get_updates_buf, base_info}(不是 {timeout});响应游标同名
        try:
            status, data = await self._request(
                f"{self._baseurl}/ilink/bot/getupdates", "POST", self._headers(),
                {"get_updates_buf": get_updates_buf or "",
                 "base_info": {"channel_version": "naga-1.0.0"}}, 38.0)
        except (TimeoutError, httpx.HTTPError, OSError) as err:
            return UpdateResponse(errcode=-1, errmsg=f"网络超时({type(err).__name__})")
        d = _as_dict(data)
        raw_msgs = cast("list[Any]", d.get("msgs") or [])
        updates: list[dict[str, Any]] = []
        for raw in raw_msgs:
            if isinstance(raw, dict):
                updates.append(cast("dict[str, Any]", raw))
        return UpdateResponse(
            errcode=-1 if status != 200 else _errcode(d),
            errmsg=d.get("errmsg") if status == 200 else f"HTTP {status}",
            updates=updates,
            next_cursor=d.get("get_updates_buf") if d.get("get_updates_buf") is not None else None)

    async def send_message(self, to_user_id: str, content: str,
                           context_token: str | None = None) -> SendResult:
        # from 空/to 填对方/type=2/state=2/纯文本 item_list[{type:1}];context_token 必须回传
        msg: dict[str, Any] = {
            "from_user_id": "", "to_user_id": to_user_id,
            "client_id": f"naga_{datetime.datetime.now(datetime.UTC).timestamp():.0f}_"
                         f"{random.randrange(10**6)}",
            "message_type": 2, "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": content}}],
        }
        if context_token:
            msg["context_token"] = context_token
        status, data = await self._request(
            f"{self._baseurl}/ilink/bot/sendmessage", "POST", self._headers(),
            {"msg": msg, "base_info": {"channel_version": "naga-1.0.0"}}, 15.0)
        d = _as_dict(data)
        return SendResult(errcode=-1 if status != 200 else _errcode(d),
                          errmsg=d.get("errmsg"))

    async def get_config(self, ilink_user_id: str,
                         context_token: str | None = None) -> tuple[SendResult, str | None]:
        # body 必须带 ilink_user_id,否则 ret=-2(无文档记载的实测坑)
        body: dict[str, Any] = {"ilink_user_id": ilink_user_id,
                                "base_info": {"channel_version": "naga-1.0.0"}}
        if context_token:
            body["context_token"] = context_token
        status, data = await self._request(
            f"{self._baseurl}/ilink/bot/getconfig", "POST", self._headers(), body, 15.0)
        d = _as_dict(data)
        nested = _as_dict(d.get("data"))
        ticket = d.get("typing_ticket") or nested.get("typing_ticket")
        return (SendResult(errcode=-1 if status != 200 else _errcode(d), errmsg=d.get("errmsg")),
                str(ticket) if ticket else None)

    async def send_typing(self, ilink_user_id: str, typing_ticket: str,
                          status_value: int) -> SendResult:
        # status:1=开始/2=结束(不是 0);W2 起编排使用
        send = await self._request(
            f"{self._baseurl}/ilink/bot/sendtyping", "POST", self._headers(),
            {"ilink_user_id": ilink_user_id, "typing_ticket": typing_ticket,
             "status": status_value,
             "base_info": {"channel_version": "naga-1.0.0"}}, 15.0)
        d = _as_dict(send[1])
        return SendResult(errcode=-1 if send[0] != 200 else _errcode(d), errmsg=d.get("errmsg"))


# ── 凭据与运行态(bot_token 永不出 credentials/;context_token 跨重启串线)──

class WeChatCredential(BaseModel):
    bot_token: str
    ilink_user_id: str | None = None
    baseurl: str | None = None
    bound_at: str = ""


class WeChatChannelState(BaseModel):
    version: int = 1
    context_tokens: dict[str, str] = {}
    updated_at: str = ""


def credential_path(home: str) -> str:
    return os.path.join(home, "credentials", "wechat-ilink.json")


def state_path(home: str) -> str:
    return os.path.join(home, "state", "wechat-ilink-state.json")


def save_credential(cred: WeChatCredential, home: str) -> None:
    os.makedirs(os.path.dirname(credential_path(home)), exist_ok=True)
    _write_private(credential_path(home), cred.model_dump_json(indent=2))


def load_credential(home: str) -> WeChatCredential | None:
    try:
        with open(credential_path(home), encoding="utf-8") as fh:
            cred = WeChatCredential.model_validate_json(fh.read())
        return cred if cred.bot_token else None
    except (OSError, ValueError):
        return None


def clear_credential(home: str) -> None:
    p = credential_path(home)
    if os.path.exists(p):
        _write_private(p, "{}")


def load_state(home: str) -> WeChatChannelState:
    try:
        with open(state_path(home), encoding="utf-8") as fh:
            st = WeChatChannelState.model_validate_json(fh.read())
        return st if st.version == 1 else WeChatChannelState()
    except (OSError, ValueError):
        return WeChatChannelState()


def save_state(state: WeChatChannelState, home: str) -> None:
    state.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
    os.makedirs(os.path.dirname(state_path(home)), exist_ok=True)
    _write_private(state_path(home), state.model_dump_json(indent=2))


def _write_private(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def extract_text(update: dict[str, Any]) -> str | None:
    """多字段兜底提取文本(content / item_list 文本/语音转写 / data / detail)。"""
    content = update.get("content")
    if isinstance(content, str) and content:
        return content
    raw_items: Any = update.get("item_list")
    if isinstance(raw_items, list):
        item_list: list[Any] = cast("list[Any]", raw_items)
        for raw in item_list:
            item = _as_dict(raw)
            text = _as_dict(item.get("text_item")).get("text")
            if isinstance(text, str) and text:
                return text
            voice = _as_dict(item.get("voice_item")).get("text")  # 服务端转写直通
            if isinstance(voice, str) and voice:
                return voice
    data = update.get("data")
    if isinstance(data, str) and data:
        return data
    if isinstance(data, dict):
        dmap = cast("dict[str, Any]", data)
        for key in ("text", "content"):
            v: Any = dmap.get(key)
            if isinstance(v, str) and v:
                return v
    detail = update.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return None


# runner 回调:(goal, session_key, actor{kind,id,trust}, ctx{trust_source}) -> Awaitable
Runner = Callable[[str, str, dict[str, str], dict[str, str]], Awaitable[None]]


class WeChatChannel:
    """微信渠道适配器(M2-W1):轮询 → 信任派生 → runner(经网关 lane)→ 回复。"""

    _ERR14_RETRY_SEC = 3.0  # -14 重试间隔(测试压缩为 0)

    def __init__(self, *, home: str, runner: Runner,
                 request: RequestFn = _httpx_request,
                 client_factory: Callable[[WeChatCredential], ILinkClient] | None = None,
                 on_token_expired: Callable[[], None] | None = None,
                 trust_file: str | None = None,
                 ledger_append: Callable[[str, str, dict[str, Any]], Any] | None = None):
        self._home = home
        self._runner = runner
        self._request = request
        self._on_token_expired = on_token_expired
        self._trust_file = trust_file
        self._ledger_append = ledger_append
        self._client_factory = client_factory
        self._client: ILinkClient | None = None
        self._cursor: str | None = None
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._err14 = 0
        self._state = WeChatChannelState()
        self._seen_sessions: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, cred: WeChatCredential) -> None:
        if self._running:
            return
        self._state = load_state(self._home)
        self._client = (self._client_factory(cred) if self._client_factory is not None
                        else ILinkClient(cred.bot_token, cred.baseurl or ILINK_BASE,
                                         self._request))
        self._running = True
        self._err14 = 0
        self._poll_task = asyncio.create_task(self._poll_loop())

    def stop(self) -> None:
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    def context_token_of(self, user_id: str) -> str | None:
        return self._state.context_tokens.get(user_id)

    async def send_reply(self, user_id: str, content: str,
                         context_token: str | None) -> SendResult | None:
        """回复(新 token 优先,持久态兜底);分段策略属 W2,先硬截 2000。"""
        if self._client is None:
            return None
        token = context_token or self.context_token_of(user_id)
        return await self._client.send_message(user_id, content[:REPLY_HARD_LIMIT], token)

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while self._running:
            assert self._client is not None
            try:
                r = await self._client.get_updates(self._cursor)
                if r.errcode == -14:
                    self._err14 += 1
                    if self._err14 >= ERR14_PAUSE_THRESHOLD:
                        self._running = False
                        if self._on_token_expired is not None:
                            self._on_token_expired()
                        break
                    await asyncio.sleep(self._ERR14_RETRY_SEC)
                    continue
                if r.errcode != 0:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                    continue
                backoff = 1.0
                self._err14 = 0
                if r.next_cursor is not None:
                    self._cursor = r.next_cursor
                for u in r.updates:
                    self._ingest(u)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 轮询循环必须存活,单轮失败退避重试
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)

    def _ingest(self, update: dict[str, Any]) -> None:
        from_user = update.get("from_user_id")
        if not isinstance(from_user, str) or not from_user:
            nested = _as_dict(update.get("message"))
            from_user = nested.get("from_user_id")
            if not isinstance(from_user, str) or not from_user:
                return
        text = extract_text(update)
        if text is None or not text.strip():
            return
        token = update.get("context_token")
        if isinstance(token, str) and token:
            self._remember_token(from_user, token)
        session_key = f"wechat:dm:{from_user}"
        self._session_open(session_key, from_user)
        derived = derive_trust("wechat", from_user, path=self._trust_file)
        task = asyncio.ensure_future(
            self._runner(text.strip(), session_key,
                         {"kind": "human", "id": from_user, "trust": derived.trust},
                         {"trust_source": derived.source}))
        task.add_done_callback(self._log_task_failure)

    def _session_open(self, session_key: str, peer: str) -> None:
        """K.4:派生来源随 session.open 入账(每会话每进程一次)。"""
        if session_key in self._seen_sessions:
            return
        self._seen_sessions.add(session_key)
        if self._ledger_append is not None:
            derived = derive_trust("wechat", peer, path=self._trust_file)
            self._ledger_append("session.open", peer,
                                {"session_key": session_key, "channel": "wechat",
                                 "trust": derived.trust, "trust_source": derived.source})

    def _remember_token(self, user_id: str, token: str) -> None:
        if self._state.context_tokens.get(user_id) == token:
            return
        self._state.context_tokens[user_id] = token
        try:
            save_state(self._state, self._home)
        except OSError:
            pass  # 持久化失败忽略,内存态继续(同 Samsara 纪律)

    @staticmethod
    def _log_task_failure(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            print(f"[wechat] 消息处理异常: {str(task.exception())[:100]}")
