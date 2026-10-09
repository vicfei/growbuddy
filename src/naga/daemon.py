"""Naga 守护子进程(M0 占位形态)。

由 `naga daemon start` 拉起(detached):持有 DurableLedger 打开态,
SIGTERM 干净关闭。M2 起承载 WS 网关与渠道;当前仅证明生命周期与
账本打开态的持有(单写入者语义的运行时前提)。Unix only(signal.pause 语义)。
"""

from __future__ import annotations

import os
import signal
import sys
import time
from types import FrameType

from naga import storage
from naga.ledger_store import DurableLedger


def main() -> int:
    home = storage.resolve_home(sys.argv[1] if len(sys.argv) > 1 else None)
    storage.init_home(home)
    pidfile = os.path.join(home, "daemon.pid")
    ready = os.path.join(home, "daemon.ready")
    for stale in (pidfile, ready):
        if os.path.exists(stale):
            os.remove(stale)

    led = DurableLedger(home)
    with open(pidfile, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    stopping = False

    def _on_term(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _on_term)
    with open(ready, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    while not stopping:
        time.sleep(0.2)

    led.close()
    for marker in (ready, pidfile):
        if os.path.exists(marker):
            os.remove(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
