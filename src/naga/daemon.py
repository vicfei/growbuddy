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
from typing import Any, cast

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

    # ── M5-1 宪法层:首启锚定,此后每次启动完整校验(违宪硬熔断,不带病运行)──
    from naga import constitution, soak

    if constitution.anchored_sha(led) is None:
        sha = constitution.anchor(led, note="M5-1 首次锚定")
        print(f"[constitution] 首次锚定 sha={sha[:16]}…", flush=True)
    else:
        report = constitution.verify(led)  # 违宪即 ConstitutionViolation
        print(f"[constitution] 校验通过 {report.detail()}", flush=True)

    # ── M5-2 soak:时钟幂等启动;上次非干净退出记 P0 ──
    soak_state = soak.ensure_started(home)
    if soak_state.get("started") and not soak_state.get("clean_shutdown", True):
        soak.record_p0(home, "daemon-unclean-exit",
                       "上次守护进程未优雅退出(SIGTERM 兜底未及/强杀)")
        print("[soak] P0: 上次非干净退出已入账", flush=True)
    soak_state["started"] = True
    soak_state["clean_shutdown"] = False
    soak.save(home, soak_state)
    print(f"[soak] 时钟运行: {soak.soak_report(home)['days_elapsed']}/"
          f"{soak.SOAK_DAYS} 天, P0={soak.soak_report(home)['p0_count']}",
          flush=True)

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
    # M4b-1 模型自动路由(E):旗舰 deepseek + 预算 qwen-flash(dashscope 凭据在场时)
    from naga.llm import OpenAIChatAdapter, ProviderConfig
    from naga.routing import ModelRegistry, ModelRouter, ModelSpec

    registry_specs = [ModelSpec(model_id="deepseek-v4-flash", tier="flagship",
                                make_adapter=lambda: adapter)]
    if creds.has("dashscope"):
        dash_cfg = creds.provider("dashscope")
        registry_specs.append(ModelSpec(
            model_id="qwen-flash", tier="budget",
            make_adapter=lambda: OpenAIChatAdapter(
                ProviderConfig(base_url=dash_cfg.base_url,
                               api_key=dash_cfg.api_key), "qwen-flash")))
    registry = ModelRegistry(registry_specs)
    router = ModelRouter(registry=registry, ledger=led, tools=tools)
    from naga.memory import MemoryStore
    memory = MemoryStore(led, home=home)  # §6.5 记忆面(召回注入/收尾提炼/遗忘)
    gateway = Gateway(led, make_adapter=lambda: adapter, tools=tools, home=home,
                      router=router, memory=memory)

    # ── 会话注册表(§5 sessions):闲置关闭 + 收尾整合蒸馏 ──
    from naga.jobs import scan_job_events
    from naga.sessions import SessionRegistry, creator_session_of, job_notify_text

    sessions = SessionRegistry(led, memory=memory,
                               make_adapter=lambda: adapter)
    sessions_task = asyncio.create_task(sessions.run_forever())
    print("[sessions] 会话注册表已启动(闲置 60 分钟收尾整合)", flush=True)

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

    # ── 附录 C:定时任务调度器(fire 经 lane;wallMs 兜底;C.3 触发链路) ──
    from naga.jobs import JobScheduler, parse_budget

    def _creator_session(job_id: str) -> str | None:
        events = scan_job_events(led)
        raw_creator: object = events.get(job_id, {}).get("creator")
        creator = cast("dict[str, Any]", raw_creator) \
            if isinstance(raw_creator, dict) else None
        return creator_session_of(creator)

    async def _job_fire(row: dict[str, Any], goal: str, backfill: bool) -> None:
        session_key = f"job:{row['id']}"
        budget = parse_budget(str(row["budget"]))  # list_jobs 键名:budget
        notification = str(row["notification"])
        target_session = _creator_session(str(row["id"]))

        async def runner() -> None:
            trace_id = "tr_" + uuid.uuid4().hex[:24]
            result: Any = None
            outcome = "timeout"
            try:
                result = await asyncio.wait_for(
                    gateway.run_agent(
                        session_key=session_key, goal=goal,
                        budget=budget["tokens"], trace_id=trace_id,
                        actor_trust=row["trust_snapshot"],
                        r_ceiling=int(str(row["r_ceiling"])[1:])),
                    timeout=budget["wall_ms"] / 1000)
                outcome = str(result.outcome)
            except TimeoutError:
                print(f"[jobs] {row['id']} wall-clock 超时"
                      f"({budget['wall_ms']}ms)", flush=True)
            reply = result.reply if result is not None else None
            await gateway.broadcast("job.completed", {
                "session_key": session_key, "job_id": row["id"],
                "outcome": outcome, "backfill": backfill,
                "tokens": result.tokens if result is not None else 0,
                "reply": reply if notification != "silent" else None})
            if notification != "silent" and reply and target_session:
                delivered = await gateway.notify_session(
                    target_session, job_notify_text(str(row["id"]), outcome, reply))
                if delivered:
                    print(f"[jobs] 结果已推送 {target_session}", flush=True)
        await gateway.submit(session_key, runner)

    scheduler = JobScheduler(
        led, fire_callback=_job_fire,
        trust_path=os.path.join(home, "trust.json"))
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    print("[jobs] 调度器已启动(30s tick;misfire 处置见账本)", flush=True)

    # ── M4a-2:Curator 保鲜(启动扫一次 + 7 天周期;幂等)与晋升自动触发 ──
    from naga.curator import Curator
    from naga.promotion import (
        PromotionPipeline,
        ShadowVerifier,
        auto_trigger_candidates,
        goals_from_bundles,
    )
    from naga.skills import SkillStore

    async def _curator_sweep() -> None:
        report = await asyncio.to_thread(
            Curator(ledger=led, skills=SkillStore(led), memory=memory).sweep)
        if report.changed:
            print(f"[curator] stale={report.to_stale} "
                  f"archived={report.to_archived} "
                  f"mem_stale={len(report.memory_stale)} "
                  f"mem_archived={len(report.memory_archived)} "
                  f"consolidated={report.consolidated} "
                  f"escrow_destroyed={report.escrow_destroyed}", flush=True)

    promotion_pipeline = PromotionPipeline(
        ledger=led, shadow=ShadowVerifier(ledger=led,
                                          make_adapter=lambda: adapter))

    last_task_ts = {"t": 0.0}

    async def _auto_promote(session_key: str) -> None:
        """任务后钩子:局部技能 uses 达阈值 → 自动走晋升管道(影子对打+R 闸)。"""
        import time as _time
        last_task_ts["t"] = _time.monotonic()
        try:
            candidates = await asyncio.to_thread(
                auto_trigger_candidates, led, session_key)
            for name, uses in candidates:
                print(f"[promotion] 自动触发: {name}(uses={uses})", flush=True)
                goals = await asyncio.to_thread(
                    goals_from_bundles, led, session_key=session_key)
                await promotion_pipeline.promote(name, from_session=session_key,
                                                 goals=goals)
        except Exception as err:  # noqa: BLE001 —— 后台晋升不阻塞渠道任务
            print(f"[promotion] 自动触发异常: {type(err).__name__}: {err}",
                  flush=True)

    gateway.on_task_done = _auto_promote  # run_agent 完成后回调(见 gateway)

    async def _curator_loop() -> None:
        await _curator_sweep()
        while True:
            await asyncio.sleep(7 * 24 * 3600)  # curator_cycle_days=7(§6.3)
            await _curator_sweep()

    curator_task = asyncio.create_task(_curator_loop())

    async def _health_loop() -> None:
        while True:
            await asyncio.sleep(soak.HEARTBEAT_SEC)
            try:
                channels = {b: bool(getattr(ch, "is_running", False))
                            for b, ch in gateway.wechat_channels.items()}
                health = await asyncio.to_thread(
                    soak.health_check, led, channels, home)
                await asyncio.to_thread(soak.record_heartbeat, home, health)
                if health["p0"]:
                    await asyncio.to_thread(soak.record_p0, home,
                                            "heartbeat", ";".join(health["issues"]))
                    print(f"[soak] P0 心跳检出: {health['issues']}", flush=True)
            except Exception as err:  # noqa: BLE001 —— 心跳自身异常不杀守护
                print(f"[soak] 心跳异常: {type(err).__name__}: {err}", flush=True)

    async def _constitution_loop() -> None:
        while True:
            await asyncio.sleep(24 * 3600)  # 日巡检(§7.6 运行时校验)
            try:
                report = await asyncio.to_thread(constitution.verify, led)
                print(f"[constitution] 日巡检通过 {report.sha[:16]}…", flush=True)
            except constitution.ConstitutionViolation as err:
                print(f"[constitution] CRITICAL 违宪熔断: {err}", flush=True)
                await asyncio.to_thread(soak.record_p0, home,
                                        "constitution-violation", str(err)[:200])
                os.kill(os.getpid(), signal.SIGTERM)  # 硬熔断:优雅停机,不带病运行

    health_task = asyncio.create_task(_health_loop())
    constitution_task = asyncio.create_task(_constitution_loop())

    async def _gepa_loop() -> None:
        import time as _time

        from naga.gepa import GepaOptimizer
        while True:
            await asyncio.sleep(7 * 24 * 3600)  # gepa_cycle_days=7(§7.5)
            if _time.monotonic() - last_task_ts["t"] < 2 * 3600:
                continue  # curator_idle_hours=2:空闲窗口不足,顺延一周期
            report = await GepaOptimizer(
                ledger=led, make_adapter=lambda: adapter,
                pipeline=promotion_pipeline).run_cycle()
            print(f"[gepa] failures={report.failures_found} "
                  f"clusters={report.clusters} proposals={report.proposals} "
                  f"promoted={report.promoted} pending={report.pending_review} "
                  f"rejected={report.rejected} tokens={report.tokens} "
                  f"stopped={report.stopped_reason}", flush=True)

    gepa_task = asyncio.create_task(_gepa_loop())

    stop = asyncio.Event()

    def _on_term() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_term)
    await stop.wait()

    for t in (curator_task, gepa_task, health_task, constitution_task,
              scheduler_task, sessions_task):
        t.cancel()
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

    final = soak.load_soak(home)
    final["clean_shutdown"] = True  # 优雅退出旗标(下次启动核对)
    soak.save(home, final)
    for marker in (ready, pidfile):
        if os.path.exists(marker):
            os.remove(marker)
    return 0


def main() -> int:
    home = storage.resolve_home(sys.argv[1] if len(sys.argv) > 1 else None)
    return asyncio.run(amain(home))


if __name__ == "__main__":
    raise SystemExit(main())
