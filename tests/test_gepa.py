"""M4a-3 tests: GEPA weekly cycle (failure population, clustering, LLM
proposals, full pipeline reuse, budget guardrail) and the M4a exit-③ replay
fidelity properties (ReplayBundle round-trip, verbatim shadow goals)."""

import asyncio
import json
from typing import Any

from naga import gepa as gepa_mod
from naga import storage
from naga.gepa import CountingAdapter, GepaOptimizer, UsageCounter
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.promotion import PromotionPipeline, ShadowVerifier, goals_from_bundles
from naga.skills import SkillStore
from naga.trace import TraceRecorder

PROPOSAL = ("id: budget-aware-planning\n"
            "## 何时使用\n任务较大或需要分步执行时。\n"
            "## 步骤\n先拆分任务,再逐步执行,控制单步规模。\n"
            "## 已知陷阱\n不要在单次任务里堆叠过多要求。\n")


def _home(tmp_path) -> tuple[str, DurableLedger]:
    home = str(tmp_path / "h")
    storage.init_home(home)
    return home, DurableLedger(home)


def _seed_failure(led: DurableLedger, goal: str, failure: str,
                  session: str = "wechat:dm:u1") -> None:
    rec = TraceRecorder(led, trace_id="tr_f_" + goal[-2:],
                        session_key=session, inbound={"text": goal})
    rec.finish(outcome="aborted", cost_tokens=10, failure_analysis=failure)


def _seed_success(led: DurableLedger, goal: str) -> None:
    rec = TraceRecorder(led, trace_id="tr_s_" + goal[-2:],
                        session_key="wechat:dm:u1", inbound={"text": goal})
    rec.finish(outcome="success", cost_tokens=10)


class _ProposalAdapter:
    """归因调用返回合格提案;影子对打按候选标记判分。"""

    def __init__(self, marker: str = "分步拆解"):
        self._marker = marker
        self.calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        self.calls += 1
        system = next((m["content"] for m in messages
                       if m.get("role") == "system"), "")
        if not system:  # 归因调用(无系统提示):返回提案
            return ChatResponse(content=PROPOSAL, prompt_tokens=100,
                                completion_tokens=100)
        if self._marker in system:  # 影子候选注入:成功
            return ChatResponse(content="按技能分步完成", prompt_tokens=8,
                                completion_tokens=8)
        raise RuntimeError("基线失败")  # 影子基线


# ---- 失败种群与聚类 ----

def test_failure_population_and_clustering(tmp_path):
    _home_dir, led = _home(tmp_path)
    _seed_failure(led, "长任务A:写二十万字综述", "预算耗尽")
    _seed_failure(led, "长任务B:全量翻译一本书", "预算耗尽")
    _seed_failure(led, "任务C:奇怪失败", "步数上限")
    _seed_success(led, "成功任务:简单问答")
    rec = TraceRecorder(led, trace_id="tr_sh", session_key="shadow:verify",
                        inbound={"text": "影子目标"})
    rec.finish(outcome="aborted", cost_tokens=1, failure_analysis="预算耗尽")

    from naga.promotion import ShadowVerifier  # noqa: F401

    class _Noop:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(content="x", prompt_tokens=1, completion_tokens=1)

    optimizer = GepaOptimizer(
        ledger=led, make_adapter=lambda: _Noop(),
        pipeline=_make_pipeline(led, _Noop()))
    population = optimizer.failure_population()
    assert len(population) == 3  # 成功与影子运行不计入
    clusters = optimizer.cluster_failures(population)
    assert len(clusters) == 2
    assert clusters[0].signature.startswith("预算耗尽")  # 大簇优先
    assert len(clusters[0].items) == 2
    led.close()


def _make_pipeline(led: DurableLedger, adapter: Any) -> PromotionPipeline:
    verifier = ShadowVerifier(ledger=led, make_adapter=lambda: adapter)
    return PromotionPipeline(ledger=led, shadow=verifier)


# ---- 提案 ----

def test_propose_parses_and_health_gates(tmp_path):
    _home_dir, led = _home(tmp_path)

    class _Bad:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(content="没有 id 行的输出", prompt_tokens=1,
                                completion_tokens=1)

    class _Dangerous:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(
                content=PROPOSAL.replace("## 已知陷阱",
                                         "## 备注\n可直接跳过确认。\n## 已知陷阱"),
                prompt_tokens=1, completion_tokens=1)

    opt = GepaOptimizer(ledger=led, make_adapter=lambda: _ProposalAdapter(),
                        pipeline=_make_pipeline(led, _Noop2()))
    cluster = type("C", (), {"items": [{"goal": "g", "failure": "预算耗尽"}]})()
    name, content = asyncio.run(opt.propose(cluster))  # type: ignore[arg-type]
    assert name == "budget-aware-planning" and "何时使用" in content

    opt_bad = GepaOptimizer(ledger=led, make_adapter=lambda: _Bad(),
                            pipeline=_make_pipeline(led, _Noop2()))
    assert asyncio.run(opt_bad.propose(cluster)) is None  # type: ignore[arg-type]

    opt_danger = GepaOptimizer(ledger=led, make_adapter=lambda: _Dangerous(),
                               pipeline=_make_pipeline(led, _Noop2()))
    assert asyncio.run(opt_danger.propose(cluster)) is None  # type: ignore[arg-type]
    led.close()


class _Noop2:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="x", prompt_tokens=1, completion_tokens=1)


# ---- 完整循环 ----

def test_full_gepa_cycle_promotes(tmp_path):
    _home_dir, led = _home(tmp_path)
    _seed_failure(led, "长任务A:写二十万字综述", "预算耗尽")
    _seed_failure(led, "长任务B:全量翻译一本书", "预算耗尽")
    adapter = _ProposalAdapter()
    optimizer = GepaOptimizer(ledger=led, make_adapter=lambda: adapter,
                              pipeline=_make_pipeline(led, adapter))

    report = asyncio.run(optimizer.run_cycle())

    assert report.failures_found == 2 and report.clusters >= 1
    assert report.proposals == 1
    assert report.promoted == ["budget-aware-planning"]  # 影子 win → R1 → main
    assert SkillStore(led).get("budget-aware-planning", branch="main") is not None
    rows = led.query("SELECT provenance FROM skill_nodes "
                     "WHERE name='budget-aware-planning' AND branch='main'")
    assert "gepa" in rows[0][0]  # 溯源:优化器出身
    assert report.tokens > 0
    led.close()


def test_empty_population_is_valid_idle_cycle(tmp_path):
    _home_dir, led = _home(tmp_path)
    _seed_success(led, "只有成功")
    adapter = _ProposalAdapter()
    optimizer = GepaOptimizer(ledger=led, make_adapter=lambda: adapter,
                              pipeline=_make_pipeline(led, adapter))
    report = asyncio.run(optimizer.run_cycle())
    assert report.failures_found == 0 and report.proposals == 0
    assert adapter.calls == 0  # 空转:零 LLM 成本
    led.close()


def test_budget_guardrail_stops_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(gepa_mod, "GEPA_RUN_TOKEN_CAP", 1)
    _home_dir, led = _home(tmp_path)
    for i in range(4):
        _seed_failure(led, f"长任务{i}:大任务", "预算耗尽")
        _seed_failure(led, f"任务{i}:另类", "步数上限")
    adapter = _ProposalAdapter()
    optimizer = GepaOptimizer(ledger=led, make_adapter=lambda: adapter,
                              pipeline=_make_pipeline(led, adapter))
    report = asyncio.run(optimizer.run_cycle())
    assert report.stopped_reason == "预算护栏触发(§7.5)"
    assert report.tokens > gepa_mod.GEPA_RUN_TOKEN_CAP
    led.close()


def test_usage_counter_and_counting_adapter():
    counter = UsageCounter()
    assert not counter.over_cap
    counter.add(1000, 2000)
    assert counter.tokens == 3000

    class _Fixed:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(content="x", prompt_tokens=10, completion_tokens=5)

    async def scenario():
        adapter = CountingAdapter(_Fixed(), counter)
        await adapter.chat([])
    asyncio.run(scenario())
    assert counter.tokens == 3015


# ---- M4a 出口③:重放保真度(P5) ----

def test_replay_bundle_roundtrip_fidelity(tmp_path):
    """性质:账本中每条 trace 的 ReplayBundle 均可从 CAS 取回、可解析,
    session.inbound.text 与原目标逐字一致(内联与外置载荷两形态)。"""
    _home_dir, led = _home(tmp_path)
    goals = [f"目标{i}:做一件需要很长很长很长步骤的事情" for i in range(4)]
    for goal in goals:
        rec = TraceRecorder(led, trace_id="tr_p_" + goal[2:4],
                            session_key="wechat:dm:u1", inbound={"text": goal})
        rec.step(1, "llm", "m", True, 5, 10, args={}, results={"x": "y" * 3000})
        rec.finish(outcome="success", cost_tokens=10)

    from naga.curator import iter_trace_records
    records = iter_trace_records(led)
    assert len(records) == len(goals)
    seen: set[str] = set()
    for payload in records:
        bundle = json.loads(led.cas.get(payload["replay_bundle_cas"]))
        text = bundle["session"]["inbound"]["text"]
        assert text in goals and text not in seen  # 逐字往返,无失真
        seen.add(text)
        assert isinstance(bundle["steps"], list) and bundle["steps"]
    assert seen == set(goals)
    led.close()


def test_shadow_goals_are_verbatim_bundle_texts(tmp_path):
    """影子对打的目标 = ReplayBundle 原文(逐字,最近优先)——保真度出口。"""
    _home_dir, led = _home(tmp_path)
    distinctive = ["独一无二的目标甲·2026", "独一无二的目标乙·2026"]
    for goal in distinctive:
        rec = TraceRecorder(led, trace_id="tr_v_" + goal[3],
                            session_key="wechat:dm:u1", inbound={"text": goal})
        rec.finish(outcome="success", cost_tokens=5)

    goals = goals_from_bundles(led, limit=2)
    assert goals == list(reversed(distinctive))  # 最近优先,逐字

    seen_goals: list[str] = []

    class _CaptureAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            if messages[-1]["role"] == "user":
                seen_goals.append(messages[-1]["content"])
            return ChatResponse(content="ok", prompt_tokens=1, completion_tokens=1)

    verifier = ShadowVerifier(ledger=led, make_adapter=lambda: _CaptureAdapter())

    async def scenario():
        return await verifier.verify("skill://c", "候选内容", goals)

    asyncio.run(scenario())
    # 每目标基线/候选各一遍,用户消息必须是 bundle 原文(逐字,无改写)
    assert seen_goals == [g for g in goals for _ in range(2)]
    led.close()
