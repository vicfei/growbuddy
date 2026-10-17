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
from typing import Any, cast

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


def _constitution_report(home: str) -> dict[str, Any]:
    from naga import constitution
    led = None
    try:
        led = DurableLedger(home)
        report = constitution.evaluate()
        report.anchored = constitution.anchored_sha(led)
        anchored_match = (report.anchored is None
                          or report.anchored == report.sha)
        ok = report.ok and anchored_match
        if not anchored_match:
            report.violations = report.violations + ["宪法与账本锚定漂移"]
        return {"ok": ok, "sha": report.sha,
                "anchored_match": anchored_match,
                "violations": report.violations}
    except Exception as err:  # noqa: BLE001 —— 诊断只读:异常即报,不熔断
        return {"ok": False, "sha": "", "anchored_match": False,
                "violations": [f"{type(err).__name__}: {err}"]}
    finally:
        if led is not None:
            led.close()


def cmd_soak(args: argparse.Namespace) -> int:
    """soak 报告:30 天时钟 / P0 计数 / 最近心跳(M5 出口①的日常视图)。"""
    from naga.soak import soak_report
    home = storage.resolve_home(args.home)
    r = soak_report(home)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, sort_keys=True))
        return 0
    print(f"soak: {r['days_elapsed']}/{r['target_days']} 天 "
          f"(起始于 {r['started_at'] or '未启动'})")
    print(f"P0: {r['p0_count']}")
    from typing import cast
    for ev in cast("list[dict[str, Any]]", r["p0_events"]):
        print(f"  {ev['at']} {ev['kind']}: {ev['detail'][:60]}")
    last = cast("dict[str, Any]", r.get("last_heartbeat") or {})
    if last:
        print(f"最近心跳: {last.get('at')} chain={'ok' if last.get('chain_ok') else 'FAIL'} "
              f"disk={last.get('disk_free_mb')}MB issues={last.get('issues') or '无'}")
    print(f"出口进度: {'达标待时' if r['days_elapsed'] < r['target_days'] and r['p0_count'] == 0 else ('ON TRACK' if r['on_track'] else 'P0 在案,出口受阻')}")
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
        "constitution": _constitution_report(home),
    }
    ok = (layout_ok and ledger["verify"] is True
          and payload["constitution"]["ok"])
    _emit(args, payload, "\n".join([
        f"home: {home}",
        f"layout: {'ok' if layout_ok else 'missing ' + ','.join(missing)}",
        f"ledger: {ledger['entries']} entries, verify={'ok' if ledger['verify'] else 'FAIL'}",
        f"snapshots: {len(snaps)} (latest: {snaps[-1] if snaps else '-'})",
        (f"constitution: {'ok' if payload['constitution']['ok'] else 'VIOLATION'} "
        f"sha={payload['constitution']['sha'][:16]}… "
        f"anchor={'match' if payload['constitution']['anchored_match'] else 'drift/none'}"),
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
    sub.add_parser("routing", help="模型路由仪表盘(M4b:节省/记分卡)")
    sub.add_parser("soak", help="30 天 soak 报告(M5:P0/心跳)")
    j = sub.add_parser("job", help="定时任务面(附录 C:add/list/pause/resume/delete/renew)")
    j.add_argument("action", choices=["add", "list", "pause", "resume",
                                      "delete", "renew"])
    j.add_argument("text", nargs="?",
                   help="add:自然语言需求(goal 文本,须置于选项之前)")
    j.add_argument("job_id", nargs="?", help="pause/resume/delete/renew 的目标")
    j.add_argument("--cron", help="直接给 cron(跳过自然语言编译)")
    j.add_argument("--timezone", help="IANA 时区(默认 Asia/Shanghai)")
    j.add_argument("--misfire", choices=["skip", "runOnce", "catchUp"],
                   default="skip", help="错过语义(GAP12)")
    j.add_argument("--r-ceiling", default="R1", help="R0-R4(默认 R1)")
    j.add_argument("--expires", help="ISO8601 有效期(R3+ 必填)")
    j.add_argument("--state", help="list 按状态过滤")
    j.add_argument("--until", help="renew 的新有效期(ISO8601)")
    j.add_argument("--reason", help="pause 原因")
    m = sub.add_parser("memory", help="记忆面(§6.5:list/show/forget)")
    m.add_argument("action", choices=["list", "show", "forget"])
    m.add_argument("target", nargs="?",
                   help="show 的 cas_id / forget 的 session_key")
    m.add_argument("--cas-id", help="forget 精确到单条(可选)")
    m.add_argument("--session-key", help="list 按会话键过滤")
    m.add_argument("--layer", help="episodic|semantic")
    m.add_argument("--status", help="active|stale|archived|forgotten")
    m.add_argument("--since", help="遗忘窗口起(ISO8601,含)")
    m.add_argument("--until", help="遗忘窗口止(ISO8601,含)")
    args = parser.parse_args(argv)
    if args.cmd == "daemon":
        return cmd_daemon(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "memory":
        return cmd_memory(args)
    if args.cmd == "job":
        return cmd_job(args)
    if args.cmd == "routing":
        return cmd_routing(args)
    if args.cmd == "soak":
        return cmd_soak(args)
    return cmd_doctor(args)


def cmd_memory(args: argparse.Namespace) -> int:
    """记忆面(§6.5):list 元数据 / show 解密单条 / forget 精确遗忘。"""
    from naga.memory import MemoryStore

    home = storage.resolve_home(args.home)
    led = DurableLedger.open(home)
    try:
        store = MemoryStore(led, home=home)
        if args.action == "list":
            rows = store.list_items(session_key=args.session_key or None,
                                    layer=args.layer or None,
                                    status=args.status or None)
            if args.json:
                print(json.dumps(
                    [{"cas_id": r[0], "layer": r[1], "session_key": r[2],
                      "status": r[3], "created_at": r[4], "use_count": r[6]}
                     for r in rows], ensure_ascii=False, sort_keys=True))
                return 0
            print(f"memory: {len(rows)} 条"
                  + (f"(session={args.session_key})" if args.session_key else ""))
            for r in rows:
                print(f"  {str(r[0])[:22]}… [{r[1]}] {r[3]} uses={r[6]} "
                      f"created={r[4]}")
            return 0
        if args.action == "show":
            if not args.target:
                print("memory show 需要 cas_id", file=sys.stderr)
                return 2
            content = store.read_content(args.target)
            if content is None:
                print(f"memory: {args.target} 不可读(不存在或已遗忘)")
                return 1
            print(content)
            return 0
        # forget:target=session_key
        if not args.target:
            print("memory forget 需要 session_key", file=sys.stderr)
            return 2
        report = store.forget(args.target, cas_id=args.cas_id or None,
                              since=args.since, until=args.until,
                              actor="cli-owner", actor_trust="owner")
        if args.json:
            print(json.dumps({"forgotten": report.forgotten,
                              "expires_at": report.expires_at,
                              "cas_ids": report.cas_ids},
                             ensure_ascii=False, sort_keys=True))
        else:
            print(f"memory: 已遗忘 {report.forgotten} 条"
                  f"(托管到期 {report.expires_at},期间可回滚)")
        return 0
    finally:
        led.close()


async def _ws_call(method: str, params: dict[str, Any],
                   idempotency_key: str | None = None) -> dict[str, Any]:
    """回环 WS 单次调用(hello → req → res;CLI 与 daemon 同进程拓扑)。"""
    import aiohttp

    port = int(os.environ.get("NAGA_WS_PORT", "18789"))
    async with aiohttp.ClientSession() as session, \
            session.ws_connect(f"ws://127.0.0.1:{port}/gw") as ws:
        await ws.send_json({"type": "hello", "proto": "samsara/1"})
        await ws.receive()
        req: dict[str, Any] = {"type": "req", "id": "cli", "method": method,
                               "params": params}
        if idempotency_key:
            req["idempotency_key"] = idempotency_key
        await ws.send_json(req)
        while True:
            raw: object = json.loads((await ws.receive()).data)
            frame = cast("dict[str, Any]", raw if isinstance(raw, dict) else {})
            if frame.get("type") == "res" and frame.get("id") == "cli":
                if not frame.get("ok"):
                    err_raw: object = frame.get("error")
                    err = (cast("dict[str, Any]", err_raw)
                           if isinstance(err_raw, dict) else {})
                    raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
                result_raw: object = frame.get("result")
                return (cast("dict[str, Any]", result_raw)
                        if isinstance(result_raw, dict) else {})


def cmd_job(args: argparse.Namespace) -> int:
    """定时任务面(附录 C):add/list/pause/resume/delete/renew——经 daemon WS。"""
    import uuid as _uuid

    try:
        if args.action == "add":
            result = asyncio.run(_ws_call(
                "job.create",
                {"text": args.text} if not args.cron else {
                    "cron": args.cron, "goal": args.text,
                    "timezone": args.timezone or "Asia/Shanghai",
                    "misfire": args.misfire, "r_ceiling": args.r_ceiling,
                    "expires_at": args.expires},
                idempotency_key="ik_cli_" + _uuid.uuid4().hex[:16]))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(f"job: 已创建 {result['job_id']}")
                print(f"  cron: {result['cron']} ({result['timezone']})"
                      f"  ≈{result['fires_per_month']} 次/月")
                print(f"  goal: {result['goal']}")
                print(f"  misfire: {result['misfire']}  rCeiling: {result['r_ceiling']}"
                      f"  预算: {result['budget']['tokens']} tokens")
            return 0
        if args.action == "list":
            params: dict[str, Any] = {}
            if args.state:
                params["state"] = args.state
            result = asyncio.run(_ws_call("job.list", params))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            print(f"job: {result['count']} 条")
            for it in result["items"]:
                print(f"  {it['id']} [{it['state']}] {it['schedule']} "
                      f"({it['timezone']}) misfire={it['misfire']} "
                      f"r={it['r_ceiling']} last_fire={it.get('last_fire') or '-'}")
                print(f"    goal: {JobGoalLazy(it['goal_cas'])}")
            return 0
        # pause/resume/delete/renew(#85:argparse 双可选位置参数贪心错配,
        # 单独的 id 落进 text 槽——两槽择一取)
        job_id = args.job_id or args.text
        if not job_id:
            print(f"job {args.action} 需要 job_id", file=sys.stderr)
            return 2
        params = {"job_id": job_id}
        if args.action == "renew":
            if not args.until:
                print("job renew 需要 --until", file=sys.stderr)
                return 2
            params["until"] = args.until
        if args.action == "pause" and args.reason:
            params["reason"] = args.reason
        result = asyncio.run(_ws_call(f"job.{args.action}", params))
        print(f"job: {args.action} {result.get('job_id')} ok"
              if not args.json else json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError) as err:
        print(f"job: daemon 不可达或拒绝({err})——定时任务面需要运行中的 daemon",
              file=sys.stderr)
        return 1


class JobGoalLazy:
    """goal 的 CAS 文本按需取(纯文件读,不启账本句柄——CLI 与 daemon 并存)。"""

    def __init__(self, goal_cas: str):
        self._cas = goal_cas

    def __str__(self) -> str:
        hexd = self._cas.removeprefix("sha256:")
        path = os.path.join(storage.resolve_home(), "assets", "blobs",
                            hexd[:2], hexd[2:4], hexd)
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()[:60]
        except OSError:
            return "(不可读)"


def cmd_routing(args: argparse.Namespace) -> int:
    """路由仪表盘:读守护进程的 HTTP 口(与 WebChat 同源,仅回环)。"""
    import json as _json
    import urllib.request

    port = int(os.environ.get("NAGA_WS_PORT", "18789"))
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/routing/stats", timeout=5) as resp:
            data = _json.loads(resp.read())
    except (OSError, ValueError) as err:
        print(f"routing: 仪表盘不可达(daemon 未运行或路由未启用): {err}")
        return 1
    if args.json:
        print(_json.dumps(data, ensure_ascii=False, sort_keys=True))
        return 0
    print(f"价格表版本: {data['price_table_version']}")
    print(f"净节省: {data['net_savings_micro_cents']} µ¢ "
          f"(毛省 {data['gross_savings_micro_cents']} − 探索 "
          f"{data['exploration_spend_micro_cents']})")
    print(f"任务数: {data['tasks']}")
    for model, entry in sorted(data["per_model"].items()):
        print(f"  {model}: tasks={entry['tasks']} shadow={entry['shadow_runs']} "
              f"tokens={entry['tokens']} actual={entry['actual_micro_cents']} µ¢")
    print("记分卡(簇 × 模型: 成功/失败, 影子胜/负):")
    for cluster, model, succ, fail, swin, sloss in data["scorecard"]:
        print(f"  {cluster} × {model}: {succ}/{fail}, 影子 {swin}/{sloss}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
