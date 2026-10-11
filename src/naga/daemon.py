"""Naga 守护进程(M2:网关宿主 + 微信渠道接线)。

daemon start 拉起(detached):打开 DurableLedger,启动 WS 网关
(127.0.0.1:18789,/gw + WebChat 静态页 + /wechat/* 绑定管理面)与微信
iLink 渠道(已绑定时),SIGTERM 干净关闭。宪法层条款:仅回环绑定。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from typing import Any

from naga import storage
from naga.gateway import WS_PORT, Gateway

OWNER_TASK_BASE_BUDGET = 200_000  # owner 自设基线(§10.1);信任级封顶由 gateway 单点执行
from naga.ledger_store import DurableLedger
from naga.llm import CredentialStore, DeepSeekAdapter


def reply_for(result: Any) -> str:
    """任务结果 → 微信回复文案(aborted 预算耗尽给出升级路径提示,§10.1)。"""
    if result.reply:
        return result.reply
    if result.outcome == "success":
        return "(模型已完成推理但未产出正文——请缩短任务描述后重试)"
    if result.outcome == "aborted" and result.failure_analysis == "预算耗尽":
        return ("本次任务超出当前信任级的 token 预算上限(guest 单任务 50,000)"
                "——长任务请拆分发送,或联系 owner 升级信任级")
    return f"({result.outcome}) {result.failure_analysis or ''}"


def _write_marker(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def wechat_media_options(creds: CredentialStore) -> dict[str, Any]:
    """渠道媒体能力装配:有 dashscope 凭据则开图片内容识别,否则仅存档。"""
    if not creds.has("dashscope"):
        return {}
    from naga.llm import DashScopeVisionDescriber

    describer = DashScopeVisionDescriber.from_credentials(creds)
    return {"vision_describe": describer.describe}


async def amain(home: str) -> int:
    print(f"[daemon] starting home={home}", flush=True)  # → state/daemon.log
    storage.init_home(home)
    pidfile = os.path.join(home, "daemon.pid")
    ready = os.path.join(home, "daemon.ready")
    for stale in (pidfile, ready):
        if os.path.exists(stale):
            os.remove(stale)

    led = DurableLedger.open(home)
    _write_marker(pidfile, str(os.getpid()))

    providers_path = os.path.join(home, "credentials", "providers.json")
    creds = CredentialStore(providers_path)
    adapter = DeepSeekAdapter.from_credentials(creds)
    tools: list[Any] = []
    if creds.has("bocha"):
        # K.3(M2 行):外部来源插件一律入子进程——元数据在父进程,执行在 worker
        from naga.tools import WebSearchTool
        from naga.worker import WorkerTool

        tools.append(WorkerTool.of(WebSearchTool, providers_path=providers_path))
    gateway = Gateway(led, make_adapter=lambda: adapter, tools=tools, home=home)

    # 微信渠道:runner 经网关 lane(会话路由单一权威);guest 预算封顶(§10.1 首项)
    from naga.wechat import WeChatChannel

    def make_wechat_channel(_cred: Any = None) -> WeChatChannel:
        """每份绑定凭据一个专属渠道(多绑定并行;runner 闭包绑定自己的渠道)。"""
        box: dict[str, WeChatChannel] = {}

        async def runner(goal: str, session_key: str,
                         actor: dict[str, str], _ctx: dict[str, str]) -> None:
            trace_id = "tr_" + uuid.uuid4().hex[:24]
            result = await gateway.run_agent(session_key=session_key, goal=goal,
                                             budget=OWNER_TASK_BASE_BUDGET,
                                             trace_id=trace_id,
                                             actor_trust=actor["trust"])
            peer = (session_key.split(":", 2)[2]
                    if session_key.count(":") >= 2 else session_key)
            ch = box.get("ch")
            if ch is not None:
                await ch.send_reply(peer, reply_for(result), None)
            event = "agent.completed" if result.outcome == "success" else "agent.failed"
            await gateway.broadcast(event, {
                "session_key": session_key, "trace_id": trace_id, "reply": result.reply,
                "outcome": result.outcome, "tokens": result.tokens,
                "failure_analysis": result.failure_analysis})

        ch = WeChatChannel(
            home=home, runner=runner,
            on_token_expired=lambda: print("[wechat] Token 失效,渠道已暂停;请重新扫码绑定"),
            ledger_append=lambda kind, owner, payload: led.append(kind, owner, payload),
            cas=led.cas,  # W3:媒体解密后入档(assets/blobs,>64MB 走 K.7 旁路)
            **wechat_media_options(creds))  # W3.5:图片内容识别(best-effort)
        box["ch"] = ch
        return ch

    first_channel = make_wechat_channel()
    gateway.attach_wechat(first_channel)          # 兼容单渠道形态(首绑定)
    gateway.attach_wechat_factory(make_wechat_channel)  # 多绑定:每凭据专属渠道

    port = int(os.environ.get("NAGA_WS_PORT", str(WS_PORT)))  # 0 = 临时端口(测试/并行)
    port = await gateway.start("127.0.0.1", port)

    # 已有绑定全部随守护启动(未绑定等热启动;多绑定=多渠道并行轮询)
    from naga.wechat import load_bindings

    bindings = await asyncio.to_thread(load_bindings, home)
    first = True
    for binder, cred in bindings.items():
        if first:  # 首绑定复用已接线渠道,避免多余实例
            if not first_channel.is_running:
                first_channel.start(cred)
            gateway.register_binding_channel(binder, first_channel)
            first = False
        else:
            await gateway.start_binding_channel(binder, cred)
    await asyncio.to_thread(_write_marker, ready, f"{os.getpid()} {port}")
    print(f"[daemon] ready pid={os.getpid()} port={port} "
          f"bindings={len(bindings)}", flush=True)

    stop = asyncio.Event()

    def _on_term() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_term)
    await stop.wait()

    first_channel.stop()
    for ch in gateway.wechat_channels.values():
        ch.stop()
    print("[daemon] stopping", flush=True)

    async def _cleanup() -> None:
        await gateway.stop()
        for tool in tools:
            aclose = getattr(tool, "aclose", None)
            if aclose is not None:
                await aclose()  # worker 子进程:关 stdin → EOF 自灭,超时强杀
        led.close()

    try:  # #39:清理超时兜底——SIGTERM 后卡死则强制退出,不让僵尸进程占口
        await asyncio.wait_for(_cleanup(), timeout=10.0)
    except TimeoutError:
        print("[daemon] 清理超时(10s),强制退出(#39)")

    for marker in (ready, pidfile):
        if os.path.exists(marker):
            os.remove(marker)
    return 0


def main() -> int:
    home = storage.resolve_home(sys.argv[1] if len(sys.argv) > 1 else None)
    return asyncio.run(amain(home))


if __name__ == "__main__":
    raise SystemExit(main())
