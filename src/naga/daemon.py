"""Naga 守护进程(M1 批次 3:网关宿主)。

daemon start 拉起(detached):打开 DurableLedger,启动 WS 网关
(127.0.0.1:18789,/gw + WebChat 静态页),SIGTERM 干净关闭。
宪法层条款的 M1 实现:仅回环绑定,远程访问走 SSH 隧道(设计文档 §4.4)。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

from naga import storage
from naga.gateway import WS_PORT, Gateway
from naga.ledger_store import DurableLedger
from naga.llm import CredentialStore, DeepSeekAdapter


def _write_marker(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


async def amain(home: str) -> int:
    storage.init_home(home)
    pidfile = os.path.join(home, "daemon.pid")
    ready = os.path.join(home, "daemon.ready")
    for stale in (pidfile, ready):
        if os.path.exists(stale):
            os.remove(stale)

    led = DurableLedger.open(home)
    _write_marker(pidfile, str(os.getpid()))

    creds = CredentialStore(os.path.join(home, "credentials", "providers.json"))
    adapter = DeepSeekAdapter.from_credentials(creds)
    tools: list[Any] = []
    if creds.has("bocha"):
        from naga.tools import WebSearchTool
        tools.append(WebSearchTool(creds))
    gateway = Gateway(led, make_adapter=lambda: adapter, tools=tools)
    port = int(os.environ.get("NAGA_WS_PORT", str(WS_PORT)))  # 0 = 临时端口(测试/并行)
    port = await gateway.start("127.0.0.1", port)
    await asyncio.to_thread(_write_marker, ready, f"{os.getpid()} {port}")

    stop = asyncio.Event()

    def _on_term() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_term)
    await stop.wait()

    await gateway.stop()
    led.close()
    for marker in (ready, pidfile):
        if os.path.exists(marker):
            os.remove(marker)
    return 0


def main() -> int:
    home = storage.resolve_home(sys.argv[1] if len(sys.argv) > 1 else None)
    return asyncio.run(amain(home))


if __name__ == "__main__":
    raise SystemExit(main())
