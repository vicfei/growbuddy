"""Naga CLI(M0 最小面,主文档附录 I.1:CLI 最先可用)。

子命令:daemon start|stop|status(生命周期)、doctor(自检:布局/账本链/
快照/宪法哈希占位)。--json 输出机器可读(接口文档 §7 约定:幂等可脚本化)。
命令名 naga——Samsara 双轨赛马中 Python 路线的本地入口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

from naga import storage
from naga.ledger_store import DurableLedger

_WAIT_SEC = 5.0


def _emit(args: argparse.Namespace, payload: dict[str, Any], human: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(human)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属他人:仍视为运行中


def _read_pid(pidfile: str) -> int | None:
    try:
        with open(pidfile, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _daemon_pid(home: str) -> int | None:
    pid = _read_pid(os.path.join(home, "daemon.pid"))
    return pid if pid is not None and _pid_alive(pid) else None


def cmd_daemon(args: argparse.Namespace) -> int:
    home = storage.init_home(args.home)
    pid = _daemon_pid(home)
    if args.action == "status":
        _emit(args, {"daemon": "running" if pid else "stopped", "pid": pid},
              f"daemon: {'running' if pid else 'stopped'}" + (f" (pid {pid})" if pid else ""))
        return 0
    if args.action == "start":
        if pid is not None:
            _emit(args, {"daemon": "running", "pid": pid}, f"daemon: already running (pid {pid})")
            return 0  # 幂等
        # stdout/stderr 落 home 内观测日志(0600):渠道/任务异常不再随 DEVNULL 丢失
        log_path = os.path.join(home, "state", "daemon.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)  # state/ 按需创建
        log_fh = open(log_path, "ab", buffering=0)  # noqa: SIM115 —— 句柄由子进程继承
        os.chmod(log_path, 0o600)
        subprocess.Popen(
            [sys.executable, "-m", "naga.daemon", home],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=log_fh, stderr=subprocess.STDOUT)
        log_fh.close()
        deadline = time.monotonic() + _WAIT_SEC
        while time.monotonic() < deadline:
            new_pid = _daemon_pid(home)
            if new_pid is not None and os.path.exists(os.path.join(home, "daemon.ready")):
                _emit(args, {"daemon": "started", "pid": new_pid},
                      f"daemon: started (pid {new_pid})")
                return 0
            time.sleep(0.1)
        print("daemon: start timeout", file=sys.stderr)
        return 1
    # stop
    if pid is None:
        _emit(args, {"daemon": "stopped", "pid": None}, "daemon: not running")
        return 0  # 幂等
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _WAIT_SEC
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    _emit(args, {"daemon": "stopped", "pid": pid}, f"daemon: stopped (pid {pid})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home = storage.resolve_home(args.home)
    missing = [d for d in storage.LAYOUT_DIRS if not os.path.isdir(os.path.join(home, d))]
    layout_ok = not missing

    ledger_dir = os.path.join(home, "ledger")
    segments = os.path.join(ledger_dir, "segments")
    has_log = (os.path.exists(os.path.join(ledger_dir, "head.log"))
               and os.path.getsize(os.path.join(ledger_dir, "head.log")) > 0)
    has_segments = os.path.isdir(segments) and any(n.endswith(".log") for n in os.listdir(segments))
    ledger: dict[str, Any] = {"entries": 0, "verify": True, "recovery": None}
    if has_log or has_segments:
        try:
            led = DurableLedger.open(home)
            ledger = {"entries": len(led.entries), "verify": led.verify(),
                      "recovery": led.last_recovery}
            led.close()
        except ValueError as err:
            ledger = {"entries": -1, "verify": False, "error": str(err)}

    snaps = sorted(n for n in os.listdir(os.path.join(ledger_dir, "snapshots"))
                   if n.startswith("snapshot_")) if os.path.isdir(
                       os.path.join(ledger_dir, "snapshots")) else []

    payload: dict[str, Any] = {
        "home": home,
        "layout": {"ok": layout_ok, "missing": missing},
        "ledger": ledger,
        "snapshots": {"count": len(snaps), "latest": snaps[-1] if snaps else None},
        "constitution_hash": {"ok": None, "note": "M5 硬化议题,占位"},
    }
    ok = layout_ok and ledger["verify"] is True
    _emit(args, payload, "\n".join([
        f"home: {home}",
        f"layout: {'ok' if layout_ok else 'missing ' + ','.join(missing)}",
        f"ledger: {ledger['entries']} entries, verify={'ok' if ledger['verify'] else 'FAIL'}",
        f"snapshots: {len(snaps)} (latest: {snaps[-1] if snaps else '-'})",
        "constitution hash: placeholder (M5)",
        f"doctor: {'OK' if ok else 'FAIL'}",
    ]))
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    from typing import cast

    from naga import kernel
    from naga.agent import AgentLoop, AgentResult
    from naga.llm import CredentialStore, DeepSeekAdapter
    from naga.skills import SkillStore
    from naga.tools import WebSearchTool

    async def scenario() -> AgentResult:
        creds = CredentialStore(os.path.join(home, "credentials", "providers.json"))
        adapter = DeepSeekAdapter.from_credentials(creds)
        tools: list[Any] = []
        if creds.has("bocha"):
            tools.append(WebSearchTool(creds))
        led = DurableLedger.open(home)
        kern = kernel.Kernel(ledger=cast("kernel.Ledger", led))  # 鸭子类型:append/verify/close
        agent = AgentLoop(ledger=led, ctx=kern.root,
                          budget=kernel.Budget(args.budget), adapter=adapter,
                          tools=tools, skill_store=SkillStore(led),
                          max_steps=args.max_steps)
        try:
            return await agent.run(args.goal)
        finally:
            led.close()

    home = storage.init_home(args.home)
    result = asyncio.run(scenario())
    if args.json:
        print(result.model_dump_json())
    else:
        print(result.reply if result.reply else f"[{result.outcome}] {result.failure_analysis}")
        print(f"trace: {result.trace_id}  steps: {result.steps}  tokens: {result.tokens}")
    return 0 if result.outcome == "success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="naga", description="Naga(Samsara Python 路线)CLI")
    parser.add_argument("--home", help="数据根目录(默认 SAMSARA_HOME 或 ~/.samsara)")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon", help="守护进程生命周期")
    d.add_argument("action", choices=["start", "stop", "status"])
    sub.add_parser("doctor", help="自检:布局/账本链/快照")
    r = sub.add_parser("run", help="执行一个任务(L1 任务回路)")
    r.add_argument("goal", help="任务目标(自然语言)")
    r.add_argument("--budget", type=int, default=200_000, help="token 预算上限")
    r.add_argument("--max-steps", type=int, default=8, help="回路步数上限")
    args = parser.parse_args(argv)
    if args.cmd == "daemon":
        return cmd_daemon(args)
    if args.cmd == "run":
        return cmd_run(args)
    return cmd_doctor(args)


if __name__ == "__main__":
    raise SystemExit(main())
