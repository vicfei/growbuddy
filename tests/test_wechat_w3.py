"""W3 测试:密钥三格式归一 / AES 解密向量 / CDN URL 自构 / 媒体消息全链路 / 失败降级。"""

import asyncio
import base64
import glob
import hashlib
import json
import os
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from naga import storage, wechat
from naga.ledger_store import CAS
from naga.wechat import (
    ILinkClient,
    WeChatChannel,
    cdn_download_url,
    decrypt_ilink_media,
    extract_media,
    media_cas_object,
    parse_ilink_aes_key,
)

KEY_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"  # 32 hex 字符 → 16 字节
KEY_RAW = bytes.fromhex(KEY_HEX)


def _encrypt_ecb(plaintext: bytes) -> bytes:
    """测试向量构造:AES-128-ECB + PKCS#7(与解密侧互为逆)。"""
    pad = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(KEY_RAW), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


# ---- 密钥三格式归一 ----

def test_parse_aes_key_three_formats_converge():
    as_b64_raw16 = base64.b64encode(KEY_RAW).decode()          # ① base64(16 原始字节)
    as_b64_hex32 = base64.b64encode(KEY_HEX.encode()).decode()  # ② base64(hex32 字符串)
    assert parse_ilink_aes_key(KEY_HEX) == KEY_RAW              # ③ 原生 hex32
    assert parse_ilink_aes_key(as_b64_raw16) == KEY_RAW
    assert parse_ilink_aes_key(as_b64_hex32) == KEY_RAW


@pytest.mark.parametrize("bad", ["", "abcd", base64.b64encode(b"x" * 8).decode(),
                                 base64.b64encode(b"not-hex-32-bytes-okay!!!!!??").decode()])
def test_parse_aes_key_rejects_bad_formats(bad):
    with pytest.raises(ValueError):
        parse_ilink_aes_key(bad)


# ---- AES-128-ECB 解密向量 ----

def test_decrypt_roundtrip_various_lengths():
    for n in (1, 15, 16, 17, 100, 4096):
        plaintext = bytes(range(256)) * (n // 256 + 1)
        plaintext = plaintext[:n]
        assert decrypt_ilink_media(_encrypt_ecb(plaintext), KEY_HEX) == plaintext


def test_decrypt_rejects_bad_ciphertext():
    with pytest.raises(ValueError, match="非 16 的倍数"):
        decrypt_ilink_media(b"\x00" * 10, KEY_HEX)
    with pytest.raises(ValueError, match="非 16 的倍数"):
        decrypt_ilink_media(b"", KEY_HEX)
    wrong_key = "ffffffffffffffffffffffffffffffff"
    with pytest.raises(ValueError, match="PKCS#7"):
        decrypt_ilink_media(_encrypt_ecb(b"hello world"), wrong_key)


# ---- CDN URL 自构(SSRF 免疫的契约面) ----

def test_cdn_download_url_fixed_base_and_encoding():
    url = cdn_download_url("abc+def/ghi&jkl=")
    assert url.startswith("https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=")
    # 特殊字符全被百分号编码,对端无法借参数注入额外查询串
    assert url.endswith("abc%2Bdef%2Fghi%26jkl%3D")


# ---- 媒体载荷提取 ----

def test_extract_media_image_prefers_aeskey():
    u = {"message_id": "m123", "item_list": [
        {"type": 2, "image_item": {"aeskey": KEY_HEX,
                                    "media": {"encrypt_query_param": "EQ1"}}}]}
    m = extract_media(u)
    assert m is not None and m.kind == "image"
    assert m.file_name == "img_m123.png"
    assert m.aes_key == KEY_HEX and m.encrypt_query_param == "EQ1"


def test_extract_media_image_falls_back_to_media_key():
    as_b64 = base64.b64encode(KEY_RAW).decode()
    u = {"item_list": [{"type": 2, "image_item": {
        "media": {"encrypt_query_param": "EQ2", "aes_key": as_b64}}}]}
    m = extract_media(u)
    assert m is not None and m.kind == "image" and m.aes_key == as_b64


def test_extract_media_file_keeps_name():
    u = {"item_list": [{"type": 4, "file_item": {
        "file_name": "报表.pdf",
        "media": {"encrypt_query_param": "EQ3", "aes_key": KEY_HEX}}}]}
    m = extract_media(u)
    assert m is not None and m.kind == "file" and m.file_name == "报表.pdf"


def test_extract_media_rejects_incomplete():
    assert extract_media({"item_list": [{"type": 2, "image_item": {
        "media": {"encrypt_query_param": "EQ"}}}]}) is None  # 无密钥
    assert extract_media({"item_list": [{"type": 2, "image_item": {
        "media": {"aes_key": KEY_HEX}}}]}) is None            # 无下载参数
    assert extract_media({"item_list": [{"type": 1, "text_item": {"text": "hi"}}]}) is None
    assert extract_media({}) is None


# ---- CAS 对象形态 ----

def test_media_cas_object_shape():
    blob = b"\x89PNG fake bytes"
    obj = json.loads(media_cas_object("image", "img_1.png", blob))
    assert obj["schema"] == "naga-media/0"
    assert obj["kind"] == "image" and obj["file_name"] == "img_1.png"
    assert obj["size_bytes"] == len(blob)
    assert obj["sha256"] == hashlib.sha256(blob).hexdigest()
    assert base64.b64decode(obj["bytes_b64"]) == blob


# ---- 全链路与降级(fake 媒体下载 + 真实 CAS) ----

class _MediaChan:
    """手搓渠道:不起轮询循环,只驱动 _ingest(同 W2 测试模式)。"""

    def __init__(self, tmp_path, media_impl, cas):
        self.goals: list[str] = []
        self.sessions: list[tuple[str, dict]] = []
        self.fetch_urls: list[str] = []

        async def runner(goal, session_key, actor, ctx):
            self.goals.append(goal)

        async def media_fetch(url):
            self.fetch_urls.append(url)
            return await media_impl(url)

        home = str(tmp_path / "h")
        storage.init_home(home)
        self.home = home

        async def req(url, method, headers, body, timeout):
            return 200, {"ret": 0}  # typing getconfig 无 ticket → 静默降级

        self.ch = WeChatChannel(home=home, runner=runner, request=req,
                                media_fetch=media_fetch, cas=cas,
                                ledger_append=lambda k, o, p: self.sessions.append((k, p)))
        self.ch._client = ILinkClient("t", "https://x", req)

    async def ingest(self, update):
        self.ch._ingest(update)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if self.goals:
                await asyncio.sleep(0.02)  # runner 完成落定
                return
        raise AssertionError("消息未被处理")


def test_image_message_full_chain_to_cas(tmp_path):
    plaintext = b"\x89PNG\r\n\x1a\n" + os.urandom(300)
    ciphertext = _encrypt_ecb(plaintext)
    cas = CAS(str(tmp_path / "h"))  # home 由 _MediaChan.init 同目录

    async def fetch(url):
        return 200, ciphertext

    mc = _MediaChan(tmp_path, fetch, cas)
    asyncio.run(_drive(mc, {"from_user_id": "peer-img", "context_token": "ct",
                            "message_id": "m9", "item_list": [
                                {"type": 2, "image_item": {"aeskey": KEY_HEX,
                                                            "media": {"encrypt_query_param": "EQ+1&x="}}}]}))

    # CDN 契约:固定基址 + 参数百分号编码
    assert len(mc.fetch_urls) == 1
    assert mc.fetch_urls[0] == cdn_download_url("EQ+1&x=")
    # 档案引用文案入 lane
    assert len(mc.goals) == 1
    goal = mc.goals[0]
    assert goal.startswith("[图片] 用户发来图片 img_m9.png(")
    assert f"({len(plaintext)}B)" in goal
    assert "已解密存档 CAS " in goal
    # CAS 落档:唯一 blob 可寻回,内容即 naga-media/0 对象
    blobs = glob.glob(os.path.join(mc.home, "assets", "blobs", "**", "*"), recursive=True)
    blobs = [b for b in blobs if os.path.isfile(b)]
    assert len(blobs) == 1
    obj = json.loads(cas.get("sha256:" + os.path.basename(blobs[0])))
    assert obj["schema"] == "naga-media/0" and obj["kind"] == "image"
    assert base64.b64decode(obj["bytes_b64"]) == plaintext
    assert obj["size_bytes"] == len(plaintext)
    # 媒体路径同样入 session.open 账(信任派生随行)
    assert [s[0] for s in mc.sessions] == ["session.open"]
    assert mc.sessions[0][1]["channel"] == "wechat"
    # context_token 记忆(媒体消息也是消息)
    assert mc.ch.context_token_of("peer-img") == "ct"


async def _drive(mc: _MediaChan, update: dict[str, Any]) -> None:
    await mc.ingest(update)


def test_file_message_full_chain_to_cas(tmp_path):
    plaintext = "月度报表内容,".encode() * 40
    ciphertext = _encrypt_ecb(plaintext)
    cas = CAS(str(tmp_path / "h"))

    async def fetch(url):
        return 200, ciphertext

    mc = _MediaChan(tmp_path, fetch, cas)
    asyncio.run(_drive(mc, {"from_user_id": "peer-file", "context_token": "cf",
                            "item_list": [{"type": 4, "file_item": {
                                "file_name": "月报.txt",
                                "media": {"encrypt_query_param": "EQF",
                                          "aes_key": base64.b64encode(KEY_RAW).decode()}}}]}))
    assert len(mc.goals) == 1
    assert mc.goals[0].startswith("[文件] 用户发来文件 月报.txt(")
    assert "已解密存档 CAS " in mc.goals[0]


def test_media_fetch_error_degrades_not_drops(tmp_path):
    async def fetch(url):
        raise OSError("connection reset")

    cas = CAS(str(tmp_path / "h"))
    mc = _MediaChan(tmp_path, fetch, cas)
    asyncio.run(_drive(mc, {"from_user_id": "peer-x",
                            "item_list": [{"type": 2, "image_item": {
                                "aeskey": KEY_HEX,
                                "media": {"encrypt_query_param": "EQ"}}}]}))
    assert len(mc.goals) == 1  # 不静默丢:降级文案照样入 lane
    assert mc.goals[0].startswith("[图片] ")
    assert "接收失败" in mc.goals[0] and "请重发" in mc.goals[0]
    assert glob.glob(os.path.join(mc.home, "assets", "blobs", "**", "*"),
                     recursive=True) == []  # 失败不落档


def test_media_oversize_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat, "WECHAT_MEDIA_MAX_BYTES", 16)
    plaintext = b"z" * 64
    ciphertext = _encrypt_ecb(plaintext)  # 合法密文但超(压缩后的)上限

    cas = CAS(str(tmp_path / "h"))

    async def fetch(url):
        return 200, ciphertext

    mc = _MediaChan(tmp_path, fetch, cas)
    asyncio.run(_drive(mc, {"from_user_id": "peer-big",
                            "item_list": [{"type": 4, "file_item": {
                                "file_name": "big.bin",
                                "media": {"encrypt_query_param": "EQ",
                                          "aes_key": KEY_HEX}}}]}))
    assert "接收失败" in mc.goals[0]
    assert "wechat_media_max_bytes" in mc.goals[0]


def test_media_http_error_degrades(tmp_path):
    cas = CAS(str(tmp_path / "h"))

    async def fetch(url):
        return 403, b"forbidden"

    mc = _MediaChan(tmp_path, fetch, cas)
    asyncio.run(_drive(mc, {"from_user_id": "peer-e",
                            "item_list": [{"type": 2, "image_item": {
                                "aeskey": KEY_HEX,
                                "media": {"encrypt_query_param": "EQ"}}}]}))
    assert "CDN HTTP 403" in mc.goals[0]


def test_voice_transcription_wins_over_media(tmp_path):
    """语音消息走 extract_text 的服务端转写,不进媒体分支。"""
    cas = CAS(str(tmp_path / "h"))
    fetched: list[str] = []

    async def fetch(url):
        fetched.append(url)
        return 200, b""

    mc = _MediaChan(tmp_path, fetch, cas)
    asyncio.run(_drive(mc, {"from_user_id": "peer-voice", "context_token": "cv",
                            "item_list": [{"type": 3, "voice_item": {
                                "text": "今天天气怎么样",
                                "media": {"encrypt_query_param": "EQV",
                                          "aes_key": KEY_HEX}}}]}))
    assert mc.goals == ["今天天气怎么样"]
    assert fetched == []  # 未触发 CDN 下载
