"""微信个人号 Bot 通道(M2-W1 文本 / W2 typing+分段 / W3 媒体)。

底座:腾讯 iLink Bot API(ilinkai.weixin.qq.com)——纯出站长轮询,宪法层
不破(默认回环绑定由网关保证)。契约以 Samsara 生产代码为准(逆向自
Weknora),对照表见 docs/research/wechat-ilink-borrowing.md。

W3 媒体:CDN 下载(固定基址自构 URL,SSRF 免疫)→ AES-128-ECB 解密 →
CAS 入档(naga-media/0)→ 以档案引用文案入 lane;失败降级文案不静默丢。
请求函数构造注入(fake 零网络契约测试)。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel

from naga.ledger_store import CAS
from naga.trust import derive_trust

ILINK_BASE = "https://ilinkai.weixin.qq.com"
ERR14_PAUSE_THRESHOLD = 5      # wechat_err14_pause_threshold 候选(随 #23 批 RFC 入注册表)
BACKOFF_MAX_SEC = 60           # wechat_poll_backoff_max_sec 候选
WECHAT_TYPING_REFRESH_SEC = 45.0    # spec-constants wechat_typing_refresh_sec(60s 自过期,留 15s 余量)
WECHAT_TYPING_TICKET_TTL_SEC = 540.0  # spec-constants wechat_typing_ticket_ttl_sec(官方 ~10min)
WECHAT_REPLY_CHUNK_CHARS = 1900      # spec-constants wechat_reply_chunk_chars(单条 ~2000 余量)
WECHAT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"  # 固定基址——URL 自构,SSRF 免疫
WECHAT_MEDIA_MAX_BYTES = 20_971_520  # spec-constants wechat_media_max_bytes(超限拒收;K.7 分层属 M3)
WECHAT_MEDIA_DOWNLOAD_TIMEOUT_SEC = 240.0  # 候选 wechat_media_download_timeout_sec(逐读超时防不了慢滴流,须总额限)

# 请求函数:(url, method, headers, json_body|None, timeout_sec) -> (status, parsed_json)
RequestFn = Callable[[str, str, dict[str, str], dict[str, Any] | None, float],
                     Awaitable[tuple[int, Any]]]

# 媒体下载函数:(url) -> (status, body_bytes);独立于 RequestFn(CDN 响应为二进制)
MediaFetch = Callable[[str], Awaitable[tuple[int, bytes]]]


async def _httpx_media_fetch(url: str) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        return resp.status_code, resp.content


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


# ── 媒体消息(CDN + AES-128-ECB,Weknora adapter.go/crypto.go 契约;W3)──


class MediaPayload(BaseModel):
    kind: str              # image | file(语音走 extract_text 的转写分支)
    file_name: str
    encrypt_query_param: str
    aes_key: str           # 原始字符串(三格式,parse_ilink_aes_key 归一)


def extract_media(update: dict[str, Any]) -> MediaPayload | None:
    """从 item_list 提取媒体载荷(type 2=图片/4=文件;缺字段返回 None)。"""
    raw_items: Any = update.get("item_list")
    if not isinstance(raw_items, list):
        return None
    mid = update.get("message_id")
    stamp = mid if isinstance(mid, (str, int)) and str(mid) else int(time.time())
    for raw in cast("list[Any]", raw_items):
        item = _as_dict(raw)
        itype = item.get("type")
        if itype == 2:
            img = _as_dict(item.get("image_item"))
            media = _as_dict(img.get("media"))
            param = media.get("encrypt_query_param")
            aes: Any = img.get("aeskey")  # image_item.aeskey 为原生 hex32 首选
            if not (isinstance(aes, str) and aes):
                aes = media.get("aes_key")
            if isinstance(param, str) and param and isinstance(aes, str) and aes:
                return MediaPayload(kind="image", file_name=f"img_{stamp}.png",
                                    encrypt_query_param=param, aes_key=aes)
            return None
        if itype == 4:
            fle = _as_dict(item.get("file_item"))
            media = _as_dict(fle.get("media"))
            param = media.get("encrypt_query_param")
            aes = media.get("aes_key")
            name = fle.get("file_name")
            if isinstance(param, str) and param and isinstance(aes, str) and aes:
                fname = name if isinstance(name, str) and name else f"file_{stamp}"
                return MediaPayload(kind="file", file_name=fname,
                                    encrypt_query_param=param, aes_key=aes)
            return None
    return None


def cdn_download_url(encrypt_query_param: str) -> str:
    # 消息只携带加密查询参数,基址为固定常量——对端无法注入任意 URL(SSRF 免疫)
    return f"{WECHAT_CDN_BASE}/download?encrypted_query_param={quote(encrypt_query_param, safe='')}"


def parse_ilink_aes_key(raw: str) -> bytes:
    """AES 密钥三格式归一为 16 字节:① base64(16 原始字节)② base64(hex32 字符串)
    ③ 原生 hex32 字符(image_item.aeskey 常见形态)。"""
    if not raw:
        raise ValueError("aes key 为空")
    if len(raw) == 32 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return bytes.fromhex(raw)  # ③
    decoded = base64.b64decode(raw, validate=True)
    if len(decoded) == 16:
        return decoded  # ①
    if len(decoded) == 32:
        try:
            hex_str = decoded.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"无法解析 aes key(长度 {len(raw)})") from None
        if len(hex_str) == 32 and all(c in "0123456789abcdefABCDEF" for c in hex_str):
            return bytes.fromhex(hex_str)  # ②
    raise ValueError(f"无法解析 aes key(长度 {len(raw)})")


def decrypt_ilink_media(ciphertext: bytes, aes_key_raw: str) -> bytes:
    """AES-128-ECB 解密 + PKCS#7 去皮(ECB 无 IV——协议契约如此,非本方选型)。"""
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise ValueError(f"密文长度 {len(ciphertext)} 非 16 的倍数")
    key = parse_ilink_aes_key(aes_key_raw)
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS#7 填充非法(密钥错误或密文损坏)")
    return padded[:-pad]


def media_cas_object(kind: str, file_name: str, data: bytes) -> bytes:
    """媒体入 CAS 的对象形态(bytes 走 base64——CAS 为对象库;哈希供校验)。"""
    obj = {"schema": "naga-media/0", "kind": kind, "file_name": file_name,
           "bytes_b64": base64.b64encode(data).decode(),
           "sha256": hashlib.sha256(data).hexdigest(),
           "size_bytes": len(data),
           "saved_at": datetime.datetime.now(datetime.UTC).isoformat()}
    return json.dumps(obj, ensure_ascii=False, indent=2).encode()


def split_for_wechat(text: str, chunk_chars: int = WECHAT_REPLY_CHUNK_CHARS) -> list[str]:
    """长回复分段:阈值 1900;边界优先换行>句号>空格,硬切兜底;多条加 (i/n) 序号。"""
    if len(text) <= chunk_chars:
        return [text]
    raw: list[str] = []
    rest = text
    while len(rest) > chunk_chars:
        window = rest[:chunk_chars]
        cut = max(window.rfind("\n"), window.rfind("。"), window.rfind(" "))
        at = cut + 1 if cut > chunk_chars * 0.6 else chunk_chars  # 边界太靠前不如硬切
        raw.append(rest[:at].rstrip())
        rest = rest[at:].lstrip()
    if rest:
        raw.append(rest)
    n = len(raw)
    return [f"({i + 1}/{n}) {part}" for i, part in enumerate(raw)]


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
                 ledger_append: Callable[[str, str, dict[str, Any]], Any] | None = None,
                 typing_refresh_sec: float = WECHAT_TYPING_REFRESH_SEC,
                 typing_ticket_ttl_sec: float = WECHAT_TYPING_TICKET_TTL_SEC,
                 media_fetch: MediaFetch = _httpx_media_fetch,
                 cas: CAS | None = None,
                 media_timeout_sec: float = WECHAT_MEDIA_DOWNLOAD_TIMEOUT_SEC,
                 vision_describe: Callable[[bytes], Awaitable[str | None]] | None = None):
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
        self._tickets: dict[str, tuple[str, float]] = {}  # user_id → (ticket, monotonic)
        self._typing_refresh = typing_refresh_sec
        self._typing_ttl = typing_ticket_ttl_sec
        self._media_fetch = media_fetch
        self._cas = cas
        self._media_timeout_sec = media_timeout_sec
        self._vision_describe = vision_describe

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
        self._cursor = None  # 热插拔重绑:新 bot 的游标从空开始(旧游标对新 token 无效)
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
        """分段回复(新 token 优先,持久态兜底);首条失败即停(避免对端失效空刷)。"""
        if self._client is None:
            return None
        token = context_token or self.context_token_of(user_id)
        last: SendResult | None = None
        for part in split_for_wechat(content):
            last = await self._client.send_message(user_id, part, token)
            if last.errcode != 0:
                break  # 首条失败即停
        return last

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
                await asyncio.sleep(0)  # 显式让出:零延迟上游/连续空转不得饿死同 loop 任务
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
        goal = text.strip() if isinstance(text, str) and text.strip() else None
        media = extract_media(update) if goal is None else None  # 文本分支优先(语音转写同)
        if goal is None and media is None:
            self._log(f"msg {from_user[:12]}… -> drop(无可提取内容)")
            return
        self._log(f"msg {from_user[:12]}… -> {'text' if goal is not None else 'media'}")
        if os.environ.get("NAGA_WECHAT_DEBUG", "") == "1":  # 逆向契约观测缝(截 4096)
            self._log("raw: " + json.dumps(update, ensure_ascii=False)[:4096])
        token = update.get("context_token")
        if isinstance(token, str) and token:
            self._remember_token(from_user, token)
        session_key = f"wechat:dm:{from_user}"
        self._session_open(session_key, from_user)
        derived = derive_trust("wechat", from_user, path=self._trust_file)
        actor = {"kind": "human", "id": from_user, "trust": derived.trust}
        ctx = {"trust_source": derived.source}
        context_token = token if isinstance(token, str) else None
        if goal is not None:
            task = asyncio.ensure_future(
                self._run_with_typing(goal, session_key, actor, ctx, context_token))
        else:
            assert media is not None
            task = asyncio.ensure_future(
                self._ingest_media(media, session_key, actor, ctx, context_token))
        task.add_done_callback(self._log_task_failure)

    async def _ingest_media(self, media: MediaPayload, session_key: str,
                            actor: dict[str, str], ctx: dict[str, str],
                            context_token: str | None) -> None:
        """媒体落地:CDN 下载(固定基址,SSRF 免疫)→ AES 解密 → CAS 入档 →
        (图片)内容识别 → 以档案引用文案入 lane。typing 覆盖全程——下载再慢
        对端也看得到「正在输入」(W3 实证:跨境慢滴流可拖数分钟);失败降级
        文案(不静默丢)。"""
        label = "图片" if media.kind == "image" else "文件"
        typing = self._begin_typing(actor["id"], context_token)
        try:
            goal = await self._media_goal(media, label)
            await self._runner(goal, session_key, actor, ctx)
        finally:
            await typing.stop()

    async def _media_goal(self, media: MediaPayload, label: str) -> str:
        try:
            blob = await self._download_media(media)
        except Exception as err:  # noqa: BLE001 —— 媒体失败降级文案,不上抛杀消息处理
            detail = str(err) or type(err).__name__  # 空串异常(裸 Timeout/OSError)至少留类型名
            self._log(f"media 失败 {media.file_name}: {detail[:120]}")
            return f"[{label}] {media.file_name} 接收失败({detail[:80]})——请重发或改用文字描述"
        if self._cas is None:
            return (f"[{label}] 用户发来{label} {media.file_name}({len(blob)}B);"
                    f"未配置 CAS 存储,内容未存档")
        cas_id = self._cas.put(media_cas_object(media.kind, media.file_name, blob))
        self._log(f"media 存档 {media.file_name} {len(blob)}B cas={cas_id[:23]}…")
        base = (f"[{label}] 用户发来{label} {media.file_name}({len(blob)}B),"
                f"已解密存档 CAS {cas_id[7:23]}…")
        if media.kind == "image" and self._vision_describe is not None:
            try:
                desc = await self._vision_describe(blob)  # best-effort:失败退回纯存档文案
            except Exception:  # noqa: BLE001
                desc = None
            if desc:
                self._log(f"media 识别 {media.file_name}: {desc[:60]}")
                return f"{base};图像内容:{desc}"
        return f"{base};内容为二进制,需要时可引用该档案说明"

    async def _download_media(self, media: MediaPayload) -> bytes:
        url = cdn_download_url(media.encrypt_query_param)
        try:
            status, body = await asyncio.wait_for(self._media_fetch(url),
                                                  self._media_timeout_sec)
        except TimeoutError:
            raise ValueError(f"下载超时({self._media_timeout_sec:.0f}s)") from None
        if status != 200:
            raise ValueError(f"CDN HTTP {status}")
        if len(body) > WECHAT_MEDIA_MAX_BYTES:
            raise ValueError(
                f"密文 {len(body)}B 超 wechat_media_max_bytes={WECHAT_MEDIA_MAX_BYTES}B 上限")
        return decrypt_ilink_media(body, media.aes_key)

    @staticmethod
    def _log(message: str) -> None:
        # 观测缝:daemon stdout(state/daemon.log);peer/密钥等敏感字段不落
        print(f"[wechat] {message}", flush=True)

    async def _run_with_typing(self, goal: str, session_key: str,
                               actor: dict[str, str], ctx: dict[str, str],
                               context_token: str | None) -> None:
        """runner 期间显示「正在输入」(§4.6 L5);best-effort,失败静默降级。"""
        typing = self._begin_typing(actor["id"], context_token)
        try:
            await self._runner(goal, session_key, actor, ctx)
        finally:
            await typing.stop()

    def _begin_typing(self, user_id: str, context_token: str | None) -> _TypingHandle:
        if self._client is None:
            return _TypingHandle(None)
        client = self._client
        state = {"stopped": False}

        async def _ticket() -> str | None:
            hit = self._tickets.get(user_id)
            if hit is not None and time.monotonic() - hit[1] < self._typing_ttl:
                return hit[0]
            result, ticket = await client.get_config(user_id, context_token)
            if result.errcode != 0 or ticket is None:
                return None
            self._tickets[user_id] = (ticket, time.monotonic())
            return ticket

        async def _show() -> None:
            try:
                t = await _ticket()
                if t is not None and not state["stopped"]:
                    await client.send_typing(user_id, t, 1)
            except Exception:  # noqa: BLE001, S110 —— typing 是表达力增强,失败静默不打扰
                pass

        async def _loop() -> None:
            try:
                while not state["stopped"]:
                    await _show()
                    await asyncio.sleep(self._typing_refresh)
            except asyncio.CancelledError:
                pass  # stop() 取消循环

        async def _stop() -> None:
            state["stopped"] = True
            try:
                hit = self._tickets.get(user_id)
                if hit is not None:
                    await client.send_typing(user_id, hit[0], 2)  # 2=结束(非 0)
            except Exception:  # noqa: BLE001, S110
                pass  # 清除失败无所谓:~60s 自动过期

        loop_task = asyncio.create_task(_loop())
        return _TypingHandle(_stop, loop_task)

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
            err = task.exception()
            detail = str(err) or type(err).__name__  # 空串异常至少留类型名
            print(f"[wechat] 消息处理异常: {detail[:100]}")


class _TypingHandle:
    """typing 会话句柄:stop() 取消周期循环并补 status=2。"""

    def __init__(self, stop_fn: Callable[[], Awaitable[None]] | None,
                 loop_task: asyncio.Task[None] | None = None):
        self._stop_fn = stop_fn
        self._loop_task = loop_task

    async def stop(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
        if self._stop_fn is not None:
            await self._stop_fn()
