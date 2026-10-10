"""W3.5 测试:typing 覆盖下载期 / 下载总时限 / 图片内容识别 / 描述器契约 / 观测。"""

import asyncio
from typing import Any

from naga import storage
from naga.ledger_store import CAS
from naga.llm import (
    VISION_DESCRIBE_MAX_CHARS,
    VISION_DESCRIBE_PROMPT,
    DashScopeVisionDescriber,
    ProviderConfig,
    sniff_image_mime,
)
from naga.wechat import ILinkClient, WeChatChannel, extract_media

KEY_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
KEY_RAW = bytes.fromhex(KEY_HEX)


def _encrypt_ecb(plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    pad = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(KEY_RAW), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def _img_update(**extra: Any) -> dict[str, Any]:
    return {"from_user_id": "peer-w35", "context_token": "ct", "item_list": [
        {"type": 2, "image_item": {"aeskey": KEY_HEX,
                                    "media": {"encrypt_query_param": "EQ"}}}], **extra}


class _Chan:
    """驱动 _ingest 的渠道支架:事件序记录 + 可注入下载/识别行为。"""

    def __init__(self, tmp_path, *, fetch_impl=None, vision=None,
                 media_timeout=None, typing_refresh=99):
        self.events: list[str] = []
        self.goals: list[str] = []
        home = str(tmp_path / "h")
        storage.init_home(home)
        self.home = home

        async def runner(goal, session_key, actor, ctx):
            self.events.append("runner")
            self.goals.append(goal)

        async def req(url, method, headers, body, timeout):
            if url.endswith("/getconfig"):
                self.events.append("getconfig")
                return 200, {"ret": 0, "typing_ticket": "tk"}
            if url.endswith("/sendtyping"):
                self.events.append(f"typing-{int(body.get('status', 0))}")
                return 200, {"ret": 0}
            return 200, {"ret": 0}

        async def media_fetch(url):
            self.events.append("fetch-start")
            if fetch_impl is not None:
                return await fetch_impl()
            self.events.append("fetch-end")
            return 200, _encrypt_ecb(b"\x89PNG\r\n\x1a\n" + b"imgdata" * 40)

        kwargs: dict[str, Any] = {"home": home, "runner": runner, "request": req,
                                      "media_fetch": media_fetch, "cas": CAS(home),
                                      "typing_refresh_sec": typing_refresh}
        if vision is not None:
            kwargs["vision_describe"] = vision
        if media_timeout is not None:
            kwargs["media_timeout_sec"] = media_timeout
        self.ch = WeChatChannel(**kwargs)
        self.ch._client = ILinkClient("t", "https://x", req)

    async def run(self, update: dict[str, Any]) -> None:
        self.ch._ingest(update)
        for _ in range(300):
            await asyncio.sleep(0.01)
            if self.goals:
                await asyncio.sleep(0.03)  # typing stop 落定
                return
        raise AssertionError("消息未被处理")


# ---- typing 覆盖下载窗口(W3 实证:下载可拖数分钟,不能死寂) ----

def test_typing_covers_download_window(tmp_path):
    async def slow_fetch():
        await asyncio.sleep(0.25)  # 下载窗口
        return 200, _encrypt_ecb(b"\x89PNGdata" * 20)

    async def scenario():
        mc = _Chan(tmp_path, fetch_impl=slow_fetch, typing_refresh=0.05)
        await mc.run(_img_update())
        return mc

    mc = asyncio.run(scenario())
    assert mc.events.index("typing-1") < mc.events.index("runner")
    assert mc.events[-1] == "typing-2"  # 结束补 status=2
    assert mc.goals[0].startswith("[图片] 用户发来图片")


# ---- 下载总时限(逐读超时防不了慢滴流) ----

def test_download_total_timeout_degrades(tmp_path):
    async def hang_fetch():
        await asyncio.sleep(5.0)  # 远超测试时限
        return 200, b""

    async def scenario():
        mc = _Chan(tmp_path, fetch_impl=hang_fetch, media_timeout=0.1)
        await mc.run(_img_update())
        return mc

    mc = asyncio.run(scenario())
    assert "下载超时" in mc.goals[0]
    assert "请重发" in mc.goals[0]
    assert mc.goals[0].startswith("[图片] ")  # 降级文案照样入 lane


# ---- 图片内容识别(best-effort) ----

def _vision_result(text: str | None = None, err: Exception | None = None):
    async def vision(blob: bytes) -> str | None:
        if err is not None:
            raise err
        return text
    return vision


def test_vision_description_embedded_in_goal(tmp_path):
    async def scenario():
        mc = _Chan(tmp_path, vision=_vision_result(text="一只橘猫蹲在窗台上晒太阳"))
        await mc.run(_img_update())
        return mc

    mc = asyncio.run(scenario())
    assert "图像内容:一只橘猫蹲在窗台上晒太阳" in mc.goals[0]
    assert "已解密存档 CAS" in mc.goals[0]
    assert "内容为二进制" not in mc.goals[0]


def test_vision_failure_falls_back_to_archive_text(tmp_path):
    async def scenario():
        raised = _Chan(tmp_path, vision=_vision_result(err=OSError("boom")))
        await raised.run(_img_update())
        silent = _Chan(tmp_path, vision=_vision_result(text=None))
        await silent.run(_img_update())
        return raised, silent

    raised, silent = asyncio.run(scenario())
    for mc in (raised, silent):
        assert "已解密存档 CAS" in mc.goals[0]
        assert "内容为二进制" in mc.goals[0]  # 识别失败退回纯存档文案,不阻断


def test_file_kind_skips_vision(tmp_path):
    calls: list[bytes] = []

    async def vision(blob: bytes) -> str | None:
        calls.append(blob)
        return "不应被调用"

    async def scenario():
        mc = _Chan(tmp_path, vision=vision)
        await mc.run({"from_user_id": "peer-f", "item_list": [{"type": 4, "file_item": {
            "file_name": "doc.pdf",
            "media": {"encrypt_query_param": "EQ", "aes_key": KEY_HEX}}}]})
        return mc

    mc = asyncio.run(scenario())
    assert calls == []  # 文件类不识别(仅 kind=image)
    assert "图像内容" not in mc.goals[0]


# ---- 描述器契约(dashscope qwen-vl,请求函数注入零外网) ----

def _describer(capture: list[tuple[str, dict, dict]], respond: Any):
    """respond = (status, json) 或 Exception(注入失败)。"""
    async def request(url, headers, body):
        capture.append((url, headers, dict(body)))
        if isinstance(respond, Exception):
            raise respond
        return respond
    return DashScopeVisionDescriber(
        ProviderConfig(base_url="https://ds.example/v1", api_key="sk-x"),
        request=request)


def test_describer_contract_body_and_success():
    capture: list[tuple[str, dict, dict]] = []
    ok = (200, {"choices": [{"message": {"content": " 一张红色方块的图片。 "}}]})
    d = _describer(capture, ok)
    out = asyncio.run(d.describe(b"\x89PNG\r\n\x1a\nfakepng"))
    assert out == "一张红色方块的图片。"
    url, headers, body = capture[0]
    assert url == "https://ds.example/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-x"
    assert body["model"] == "qwen-vl-plus" and body["max_tokens"] == 512
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1]["text"] == VISION_DESCRIBE_PROMPT


def test_describer_mime_sniffing():
    capture: list[tuple[str, dict, dict]] = []
    ok = (200, {"choices": [{"message": {"content": "x"}}]})
    d = _describer(capture, ok)
    asyncio.run(d.describe(b"\xff\xd8\xff\xe0jepgdata"))
    assert capture[0][2]["messages"][0]["content"][0]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,")
    asyncio.run(d.describe(b"RIFF____WEBPjunk"))
    assert capture[1][2]["messages"][0]["content"][0]["image_url"]["url"].startswith(
        "data:image/webp;base64,")


def test_describer_failures_return_none():
    capture: list[tuple[str, dict, dict]] = []
    cases: list[Any] = [
        (500, {"error": "x"}),
        (200, {"choices": []}),
        (200, {"choices": [{"no_message": 1}]}),
        (200, {"choices": [{"message": {"content": None}}]}),
        (200, {"choices": [{"message": {"content": "   "}}]}),
        OSError("net down"),
    ]
    for respond in cases:
        out = asyncio.run(_describer(capture, respond).describe(b"\x89PNGx"))
        assert out is None
    assert len(capture) == len(cases)


def test_describer_truncates_long_content():
    capture: list[tuple[str, dict, dict]] = []
    long = "字" * 1000
    d = _describer(capture, (200, {"choices": [{"message": {"content": long}}]}))
    out = asyncio.run(d.describe(b"\x89PNGx"))
    assert out is not None and len(out) == VISION_DESCRIBE_MAX_CHARS


def test_sniff_image_mime_table():
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff") == "image/jpeg"
    assert sniff_image_mime(b"RIFF1234WEBP") == "image/webp"
    assert sniff_image_mime(b"GIF89a") == "image/gif"
    assert sniff_image_mime(b"BM\x00") == "image/bmp"
    assert sniff_image_mime(b"????") == "image/jpeg"  # 兜底


# ---- 观测缝:ingest 决策日志(capsys 捕获;生产落 state/daemon.log) ----

def test_ingest_logs_decision_line(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("NAGA_WECHAT_DEBUG", raising=False)

    async def scenario():
        mc = _Chan(tmp_path)
        await mc.run(_img_update())
        return mc

    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "msg peer-w35… -> media" in out
    assert "media 存档 img_" in out and "cas=sha256:" in out
    assert "raw:" not in out  # 非调试模式不 dump 原文


def test_debug_env_dumps_raw_update(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NAGA_WECHAT_DEBUG", "1")

    async def scenario():
        mc = _Chan(tmp_path)
        await mc.run({"from_user_id": "peer-d", "item_list": [
            {"type": 2, "image_item": {"aeskey": KEY_HEX,
                                        "media": {"encrypt_query_param": "EQ"}}}]})
        return mc

    asyncio.run(scenario())
    assert "raw: " in capsys.readouterr().out


# ---- 集成:提取→解密→识别→入 lane 全链(带 vision) ----

def test_media_with_vision_end_to_end(tmp_path):
    async def scenario():
        mc = _Chan(tmp_path, vision=_vision_result(text="夜景城市天际线"))
        update = _img_update(message_id="m77")
        assert extract_media(update) is not None
        await mc.run(update)
        return mc

    mc = asyncio.run(scenario())
    goal = mc.goals[0]
    assert "img_m77.png" in goal
    assert "已解密存档 CAS " in goal and "图像内容:夜景城市天际线" in goal
