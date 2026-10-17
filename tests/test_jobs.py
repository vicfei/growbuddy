"""定时任务测试(附录 C/接口 §3.5,Fake 注入零外网)。

时钟纪律:调度判定的锚点来自账本 ts,故用 fake clock 同时驱动
ledger_store.time.time(入账时间)与调度器 now——同一合成世界,
due/misfire 语义完全确定(见 _clocked fixture)。

覆盖:规格校验(cron/时区/goal/R3+ 有效期)/ 投影与重建 / NL 编译 /
到点判定与不重触 / misfire 三态 / 信任降级与过期守卫 / 封存不丢锚 /
fire 链路(job.fire 先入账 + lane 执行)/ gateway job.* 方法面 /
rCeiling 工具收紧。
"""

import asyncio
import datetime
import json
import os
import zoneinfo
from collections.abc import Callable
from typing import Any

import pytest

from naga.gateway import Gateway
from naga.jobs import (
    JobScheduler,
    JobSpec,
    JobStore,
    compile_natural_language,
    fires_per_month,
    scan_job_events,
)
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.trust import grant_trust, revoke_trust

TZ = zoneinfo.ZoneInfo("Asia/Shanghai")


@pytest.fixture
def clocked(monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[[], datetime.datetime], Callable[[datetime.timedelta], None]]:
    """合成时钟:同步驱动账本 ts 与调度器 now。返回 (now, advance)。"""
    state = {"t": datetime.datetime(2026, 10, 19, 8, 59, 30, tzinfo=TZ)}

    def now() -> datetime.datetime:
        return state["t"]

    def advance(delta: datetime.timedelta) -> None:
        state["t"] = state["t"] + delta

    import naga.ledger_store as ls
    monkeypatch.setattr(ls.time, "time", lambda: state["t"].timestamp())
    return now, advance


def _led(tmp_path, name="home") -> DurableLedger:
    return DurableLedger(str(tmp_path / name))


def _spec(**over: Any) -> JobSpec:
    base: dict[str, Any] = {
        "job_id": "job_test01", "schedule": "0 9 * * *",
        "timezone": "Asia/Shanghai", "goal": "汇总今日要闻",
        "trust_snapshot": "owner",
        "budget": {"tokens": 5000, "wall_ms": 600_000}, "r_ceiling": "R1"}
    base.update(over)
    return JobSpec(**base)


def _recorder(fired: list[str]) -> Any:
    async def _fire(row: dict[str, Any], goal: str, backfill: bool) -> None:
        fired.append(f"{row['id']}:{goal}:{backfill}")
    return _fire


# ── 规格校验与投影 ──────────────────────────────────────────────

def test_create_validates_spec(tmp_path):
    led = _led(tmp_path)
    store = JobStore(led)
    with pytest.raises(Exception, match="cron"):
        store.create(_spec(schedule="not a cron"))
    with pytest.raises(Exception, match="时区"):
        store.create(_spec(timezone="Mars/Olympus"))
    with pytest.raises(Exception, match="goal"):
        store.create(_spec(goal=""))
    with pytest.raises(Exception, match="有效期"):
        store.create(_spec(r_ceiling="R3"))          # R3+ 必须 expires_at
    with pytest.raises(Exception, match="misfire"):
        store.create(_spec(misfire="backfill"))
    store.create(_spec(r_ceiling="R3",               # R3 带有效期可建
                       expires_at="2099-01-01T00:00:00+00:00"))
    store.create(_spec(job_id="job_test02", r_ceiling="R2"))  # R2 无需有效期
    assert len(store.list_jobs()) == 2
    led.close()


def test_projection_rows_and_rebuild(tmp_path):
    led = _led(tmp_path)
    store = JobStore(led)
    jid = store.create(_spec(expires_at="2099-01-01T00:00:00+00:00"),
                       actor="webchat:dm:owner")
    row = store.list_jobs()[0]
    assert row["schedule"] == "0 9 * * *"
    assert row["goal_cas"].startswith("sha256:")
    assert (row["state"], row["trust_snapshot"]) == ("active", "owner")
    led.close()
    led2 = DurableLedger.open(led.home)
    rows2 = JobStore(led2).list_jobs()
    assert rows2[0]["id"] == jid and rows2[0]["state"] == "active"
    assert JobStore(led2).goal_of(rows2[0]["goal_cas"]) == "汇总今日要闻"
    led2.close()


def test_lifecycle_transitions(tmp_path):
    led = _led(tmp_path)
    store = JobStore(led)
    jid = store.create(_spec())
    assert store.pause(jid, reason="test") is True
    assert store.list_jobs()[0]["state"] == "paused"
    assert store.resume(jid) is True                 # paused → active
    with pytest.raises(Exception, match="不可"):
        store.resume(jid)                            # active 再 resume 拒绝
    assert store.delete(jid) is True
    assert store.list_jobs()[0]["state"] == "deleted"
    assert store.renew(jid, "2099-06-01T00:00:00+00:00") is True
    assert store.list_jobs()[0]["expires_at"] == "2099-06-01T00:00:00+00:00"
    assert store.pause("job_missing") is False       # 不存在:幂等 False
    led.close()


# ── NL 编译 ─────────────────────────────────────────────────────

class FakeAdapter:
    def __init__(self, content: str):
        self._content = content
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        self.calls.append([dict(m) for m in messages])
        return ChatResponse(content=self._content, prompt_tokens=1,
                            completion_tokens=1)


def test_compile_natural_language_ok():
    adapter = FakeAdapter('{"cron": "30 8 * * *", "timezone": "Asia/Shanghai",'
                          ' "goal": "提醒喝水", "misfire": "skip"} 前后噪音')
    spec = asyncio.run(compile_natural_language(adapter, "每天早上八点半提醒我喝水"))
    assert spec.schedule == "30 8 * * *"
    assert spec.goal == "提醒喝水"
    assert "喝水" in adapter.calls[0][0]["content"]


@pytest.mark.parametrize("body", [
    "完全没有 JSON 的输出",
    '{"cron": "bad cron", "goal": "x"}',
    '{"cron": "0 9 * * *", "goal": ""}',
])
def test_compile_rejects_bad_output(body):
    from naga.jobs import JobError
    with pytest.raises(JobError):
        asyncio.run(compile_natural_language(FakeAdapter(body), "需求"))


def test_fires_per_month_estimate():
    assert fires_per_month("0 9 * * *", "Asia/Shanghai") == 30
    assert fires_per_month("0 9 * * FRI", "Asia/Shanghai") in (4, 5)


# ── 到点判定(合成世界,确定性) ────────────────────────────────

def test_due_occurrence_semantics(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    JobStore(led).create(_spec(schedule="0 9 * * *"))  # 每天 09:00
    fired: list[str] = []
    sched = JobScheduler(led, fire_callback=_recorder(fired), now=now)

    report = asyncio.run(sched.tick())   # 08:59:30 — 今天的 09:00 还没到
    assert report.fired == []

    advance(datetime.timedelta(minutes=2))  # 09:01:30
    report = asyncio.run(sched.tick())
    assert report.fired == ["job_test01"]   # 09:00 触发

    advance(datetime.timedelta(minutes=5))  # 09:06 — 同一 occurrence 不重触
    report = asyncio.run(sched.tick())
    assert report.fired == []

    advance(datetime.timedelta(days=1))     # 次日 09:06
    report = asyncio.run(sched.tick())
    assert report.fired == ["job_test01"]
    assert len(fired) == 2 and fired[0].endswith(":False")
    led.close()


def test_paused_not_fired(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    store = JobStore(led)
    store.create(_spec(schedule="0 9 * * *"))
    store.pause("job_test01", reason="manual")
    advance(datetime.timedelta(hours=2))
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder([]),
                                      now=now).tick())
    assert report.fired == []              # 暂停态不触发
    led.close()


# ── misfire(GAP12) ────────────────────────────────────────────

def test_misfire_skip_audits_only(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    JobStore(led).create(_spec(schedule="0 * * * *", misfire="skip"))  # 每小时
    advance(datetime.timedelta(hours=3))   # 守护"停机"三小时后恢复
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder([]),
                                      now=now).recover())
    assert report.fired == []              # 错过即弃
    assert report.missed["job_test01"] == 3
    missed = [e for e in led.entries if e["kind"] == "job.missed"]
    assert missed and missed[0]["payload"]["count"] == 3
    led.close()


def test_misfire_runonce_fires_once(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    JobStore(led).create(_spec(schedule="0 * * * *", misfire="runOnce"))
    advance(datetime.timedelta(hours=5))   # 错过 5 次
    fired: list[str] = []
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder(fired),
                                      now=now).recover())
    assert report.fired == ["job_test01"]  # 只补一次,不逐次补齐
    assert fired == ["job_test01:汇总今日要闻:True"]
    fires = [e for e in led.entries if e["kind"] == "job.fire"]
    assert fires and fires[0]["payload"]["backfill"] is True
    led.close()


def test_no_missed_when_fresh(tmp_path, clocked):
    now, _advance = clocked
    led = _led(tmp_path)
    JobStore(led).create(_spec(schedule="0 9 * * *"))
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder([]),
                                      now=now).recover())
    assert report.fired == [] and report.missed == {}
    led.close()


# ── C.4 守卫:降级暂停 / 过期暂停 ──────────────────────────────

def test_trust_downgrade_pauses(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    trust_file = os.path.join(led.home, "trust.json")
    grant_trust("wechat", "peer_1", "owner", path=trust_file)
    store = JobStore(led)
    store.create(_spec(schedule="0 9 * * *", trust_snapshot="owner",
                       creator_channel="wechat", creator_peer="peer_1"))
    revoke_trust("wechat", "peer_1", path=trust_file)  # owner → guest 默认
    advance(datetime.timedelta(hours=2))
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder([]),
                                      trust_path=trust_file,
                                      now=now).tick())
    assert report.fired == []
    assert "降级" in report.paused["job_test01"]
    assert store.list_jobs()[0]["state"] == "paused"
    led.close()


def test_trust_upgrade_does_not_pause(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    trust_file = os.path.join(led.home, "trust.json")
    JobStore(led).create(_spec(schedule="0 9 * * *", trust_snapshot="known",
                               creator_channel="wechat", creator_peer="peer_2"))
    grant_trust("wechat", "peer_2", "owner", path=trust_file)  # 升级不暂停
    advance(datetime.timedelta(hours=2))
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder([]),
                                      trust_path=trust_file,
                                      now=now).tick())
    assert report.paused == {}
    led.close()


def test_expired_job_pauses(tmp_path, clocked):
    now, advance = clocked
    led = _led(tmp_path)
    store = JobStore(led)
    store.create(_spec(schedule="0 9 * * *", r_ceiling="R3",
                       expires_at="2020-01-01T00:00:00+00:00"))
    advance(datetime.timedelta(hours=2))
    report = asyncio.run(JobScheduler(led, fire_callback=_recorder([]),
                                      now=now).tick())
    assert "有效期" in report.paused["job_test01"]
    assert store.list_jobs()[0]["state"] == "paused"
    led.close()


def test_scan_job_events_survives_seal(tmp_path):
    """封存不丢锚:job.fire 入 segment 后仍被扫描(防重复触发)。"""
    led = DurableLedger(str(tmp_path / "home"), segment_max=3)
    JobStore(led).create(_spec(schedule="0 9 * * *"))
    for _ in range(6):
        led.append("job.fire", "scheduler",
                   {"id": "job_test01", "goal_cas": "sha256:x", "backfill": False})
    events = scan_job_events(led)
    assert events["job_test01"]["last_fire"] is not None
    assert events["job_test01"]["creator"] == {"channel": "webchat", "peer": ""}
    led.close()


# ── gateway job.* 方法面 ────────────────────────────────────────

class ScriptAdapter:
    def __init__(self, script: list[ChatResponse]):
        self._script = list(script)
        self.tool_specs: list[list[str] | None] = []

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        self.tool_specs.append([t.name for t in tools] if tools else None)
        return self._script.pop(0)


def test_gateway_job_create_list_transitions(tmp_path):
    async def scenario() -> None:
        led = _led(tmp_path, "gw")
        adapter = ScriptAdapter([])
        gw = Gateway(led, make_adapter=lambda: adapter)
        await gw.start("127.0.0.1", 0)
        try:
            with pytest.raises(Exception, match="幂等键"):
                await gw._dispatch({"method": "job.create",
                                    "params": {"cron": "0 9 * * *",
                                               "goal": "g"}})
            res = await gw._dispatch({
                "method": "job.create", "idempotency_key": "ik_j1",
                "params": {"cron": "0 9 * * FRI", "goal": "写周报",
                           "session_key": "webchat:dm:owner"}})
            assert res["cron"] == "0 9 * * FRI" and res["fires_per_month"] in (4, 5)
            listed = await gw._dispatch({"method": "job.list", "params": {}})
            assert listed["count"] == 1
            assert listed["items"][0]["state"] == "active"
            assert listed["items"][0]["creator"] == {"channel": "webchat",
                                                     "peer": "owner"}
            out = await gw._dispatch({"method": "job.pause",
                                      "params": {"job_id": res["job_id"]}})
            assert out["ok"] is True
            with pytest.raises(Exception, match="任务不存在"):
                await gw._dispatch({"method": "job.pause",
                                    "params": {"job_id": "job_none"}})
            # guest 创建拒绝(§10 矩阵)
            with pytest.raises(Exception, match="guest"):
                await gw._dispatch({
                    "method": "job.create", "idempotency_key": "ik_j2",
                    "params": {"cron": "0 9 * * *", "goal": "x",
                               "session_key": "wechat:dm:guestpeer"}})
            # R3 无有效期拒绝
            with pytest.raises(Exception, match="有效期"):
                await gw._dispatch({
                    "method": "job.create", "idempotency_key": "ik_j3",
                    "params": {"cron": "0 9 * * *", "goal": "x",
                               "r_ceiling": "R3",
                               "session_key": "webchat:dm:owner"}})
        finally:
            await gw.stop()
            led.close()

    asyncio.run(scenario())


def test_gateway_job_create_from_natural_language(tmp_path):
    async def scenario() -> None:
        led = _led(tmp_path, "gwnl")
        adapter = ScriptAdapter([
            ChatResponse(content='{"cron": "0 7 * * *", "timezone":'
                                 ' "Asia/Shanghai", "goal": "早安摘要"}',
                         prompt_tokens=1, completion_tokens=1)])
        gw = Gateway(led, make_adapter=lambda: adapter)
        res = await gw._dispatch({
            "method": "job.create", "idempotency_key": "ik_jnl",
            "params": {"text": "每天早上七点给我早安摘要",
                       "session_key": "webchat:dm:owner"}})
        assert res["cron"] == "0 7 * * *" and res["goal"] == "早安摘要"
        # 幂等重放
        again = await gw._dispatch({
            "method": "job.create", "idempotency_key": "ik_jnl",
            "params": {"text": "每天早上七点给我早安摘要",
                       "session_key": "webchat:dm:owner"}})
        assert again["job_id"] == res["job_id"]
        led.close()

    asyncio.run(scenario())


# ── fire 链路 + rCeiling 收紧 ──────────────────────────────────

def test_fire_chain_runs_agent(tmp_path, clocked):
    """fire → job.fire 先入账 → lane 执行标准 agent.run(job:{id} 会话)。"""
    now, advance = clocked
    led = _led(tmp_path, "fire")

    async def scenario() -> None:
        adapter = ScriptAdapter([
            ChatResponse(content="任务完成", prompt_tokens=1, completion_tokens=1)])
        gw = Gateway(led, make_adapter=lambda: adapter)
        await gw.start("127.0.0.1", 0)
        try:
            JobStore(led).create(_spec(schedule="0 9 * * *", budget={
                "tokens": 5000, "wall_ms": 600_000}))
            advance(datetime.timedelta(minutes=2))  # 到 09:01:30

            async def fire(row: dict[str, Any], goal: str,
                           backfill: bool) -> None:
                session_key = f"job:{row['id']}"
                budget = json.loads(row["budget"])

                async def runner() -> None:
                    result = await asyncio.wait_for(
                        gw.run_agent(session_key=session_key, goal=goal,
                                     budget=budget["tokens"],
                                     trace_id="tr_job1",
                                     actor_trust=row["trust_snapshot"],
                                     r_ceiling=int(row["r_ceiling"][1:])),
                        timeout=budget["wall_ms"] / 1000)
                    await gw.broadcast("job.completed", {
                        "session_key": session_key, "job_id": row["id"],
                        "outcome": result.outcome, "tokens": result.tokens,
                        "reply": result.reply, "backfill": backfill})
                await gw.submit(session_key, runner)

            sched = JobScheduler(led, fire_callback=fire, now=now)
            report = await sched.tick()
            assert report.fired == ["job_test01"]
            for _ in range(40):  # 等 lane 消化(trace 落地为完成信号)
                if led.query("SELECT 1 FROM traces LIMIT 1"):
                    break
                await asyncio.sleep(0.05)
            kinds = [e["kind"] for e in led.entries]
            assert "job.fire" in kinds and "trace.record" in kinds
            rows = led.query("SELECT session_key, outcome FROM traces")
            assert rows and rows[0] == ("job:job_test01", "success")
        finally:
            await gw.stop()

    asyncio.run(scenario())
    led.close()


def test_run_agent_r_ceiling_filters_tools(tmp_path):
    """rCeiling 在信任档位之上收紧:owner + R0 帽下,写类工具不可见。"""
    led = _led(tmp_path, "cap")

    _params: dict[str, Any] = {"type": "object", "properties": {}}

    class RiskyTool:
        name, description = "risky_write", "写类工具"
        parameters = _params
        risk_level = 2

        async def run(self, args: dict[str, Any]) -> dict[str, Any]:
            return {}

    class SafeTool:
        name, description = "safe_read", "读类工具"
        parameters = _params
        risk_level = 0

        async def run(self, args: dict[str, Any]) -> dict[str, Any]:
            return {}

    async def scenario() -> None:
        adapter = ScriptAdapter([
            ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1),
            ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)])
        gw = Gateway(led, make_adapter=lambda: adapter,
                     tools=[RiskyTool(), SafeTool()])
        await gw.start("127.0.0.1", 0)
        try:
            await gw.run_agent(session_key="job:cap", goal="做点事",
                               budget=5000, trace_id="tr_cap1",
                               actor_trust="owner", r_ceiling=0)
            assert adapter.tool_specs[0] == ["safe_read"]     # R0 帽滤掉写类
            await gw.run_agent(session_key="job:cap", goal="做点事",
                               budget=5000, trace_id="tr_cap2",
                               actor_trust="owner")
            assert "risky_write" in (adapter.tool_specs[1] or [])  # 无帽全量
        finally:
            await gw.stop()

    asyncio.run(scenario())
    led.close()


def test_cli_job_id_resolves_from_text_slot(monkeypatch):
    """#85:单独的 id 被 argparse 贪心分进 text 槽,非 add 动作须两槽择一取。"""
    from naga import cli

    captured: list[tuple[str, dict[str, Any]]] = []

    async def fake_ws(method: str, params: dict[str, Any],
                      idempotency_key: str | None = None) -> dict[str, Any]:
        captured.append((method, params))
        return {"job_id": params.get("job_id"), "ok": True}

    monkeypatch.setattr(cli, "_ws_call", fake_ws)
    assert cli.main(["job", "pause", "job_abc"]) == 0
    assert captured[-1] == ("job.pause", {"job_id": "job_abc"})
    assert cli.main(["job", "renew", "job_abc", "--until",
                     "2099-01-01T00:00:00+00:00"]) == 0
    assert captured[-1] == ("job.renew", {
        "job_id": "job_abc", "until": "2099-01-01T00:00:00+00:00"})
    assert cli.main(["job", "delete"]) == 2   # 无 id:参数错误退出码
