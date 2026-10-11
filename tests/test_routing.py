"""M4b-1 tests: model auto-routing — cluster determinism, scorecard, cold-start
router (explore down only), shadow downgrade, savings dashboard, gateway wiring."""

import asyncio
from typing import Any

from naga import storage
from naga.agent import cluster_of
from naga.gateway import Gateway
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.routing import (
    SHADOW_DOWNGRADE_WINS,
    ModelRegistry,
    ModelRouter,
    ModelSpec,
    Scorecard,
    routing_dashboard,
)

FLAG = "flagship-model"
BUDGET = "budget-model"


class _OkAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        return ChatResponse(content="ok", prompt_tokens=10, completion_tokens=10)


class _FailAdapter:
    async def chat(self, messages, tools=None, *, max_tokens=1024):
        raise RuntimeError("模型不行")


def _router(tmp_path, budget_adapter: Any = None) -> tuple[ModelRouter, DurableLedger]:
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    registry = ModelRegistry([
        ModelSpec(model_id=FLAG, tier="flagship", make_adapter=lambda: _OkAdapter()),
        ModelSpec(model_id=BUDGET, tier="budget",
                  make_adapter=lambda: budget_adapter or _OkAdapter()),
    ])
    return ModelRouter(registry=registry, ledger=led), led


# ---- 簇与价格 ----

def test_cluster_deterministic_and_bounded():
    a = cluster_of("写电动车电池技术综述并对比")
    assert a == cluster_of("写电动车电池技术综述并对比")
    assert a != cluster_of("hello separate latin task")
    assert len(a) <= 48 and a
    assert cluster_of("好") == "default"


def test_price_table_math():
    from naga.llm import PRICE_TABLE_VERSION, price_cents, price_micro_cents
    assert PRICE_TABLE_VERSION
    assert price_cents("qwen-flash", 500_000, 500_000) == 6
    assert price_micro_cents("qwen-flash", 1000, 500) > 0
    assert price_cents("unknown-model", 999, 999) == 0  # 未登记宁少记


# ---- 记分卡与降档裁决 ----

def test_scorecard_downgrade_rule(tmp_path):
    _h, led = DurableLedger(str(tmp_path / "x")), None
    import tempfile
    home = str(tempfile.mkdtemp())
    storage.init_home(home)
    led = DurableLedger(home)
    card = Scorecard(led)
    for _ in range(SHADOW_DOWNGRADE_WINS):
        card.record_shadow("c1", BUDGET, True)
    assert card.proven_budget("c1") == BUDGET

    card.record_shadow("c2", BUDGET, True)
    card.record_shadow("c2", BUDGET, False)  # 一次失败阻断
    assert card.proven_budget("c2") is None

    card.record_result("c3", FLAG, True)
    card.record_result("c3", FLAG, False)
    rows = {(c, m): (s, f) for c, m, s, f, _, _ in card.rows()}
    assert rows[("c3", FLAG)] == (1, 1)
    led.close()


# ---- 路由器:冷启动从强往弱,只向下 ----

def test_router_cold_start_and_downgrade(tmp_path):
    router, led = _router(tmp_path)
    goal = "写电动车电池技术综述"
    assert router.pick(goal).model_id == FLAG  # 未证实簇:旗舰(宁贵勿错)

    card = Scorecard(led)
    for _ in range(SHADOW_DOWNGRADE_WINS):
        card.record_shadow(cluster_of(goal), BUDGET, True)
    assert router.pick(goal).model_id == BUDGET  # 证实后:预算接手
    assert router.pick("another unrelated latin goal").model_id == FLAG  # 只对已证簇
    led.close()


def test_shadow_explore_records_win(tmp_path):
    router, led = _router(tmp_path)
    goal = "总结一下今天的新闻要点"

    async def scenario():
        return await router.shadow_explore(goal, session_key="s")

    win = asyncio.run(scenario())
    assert win is True
    rows = led.query("SELECT model, kind FROM routing_tasks")
    assert rows == [(BUDGET, "shadow")]
    card = Scorecard(led)
    assert card.rows() and card.rows()[0][4] == 1  # shadow_wins=1
    led.close()


def test_shadow_explore_loss_blocks(tmp_path):
    router, led = _router(tmp_path, budget_adapter=_FailAdapter())
    goal = "总结一下今天的新闻要点"

    async def scenario():
        return await router.shadow_explore(goal, session_key="s")

    win = asyncio.run(scenario())
    assert win is False
    assert Scorecard(led).proven_budget(cluster_of(goal)) is None
    led.close()


# ---- 仪表盘 ----

def test_dashboard_savings_math(tmp_path):
    router, led = _router(tmp_path)
    cluster = cluster_of("总结一下今天的新闻要点")
    router.record_task(trace_id="t1", cluster=cluster, model=FLAG,
                       prompt_tokens=100_000, completion_tokens=100_000,
                       flagship_model=FLAG)
    router.record_task(trace_id="t2", cluster=cluster, model=BUDGET,
                       prompt_tokens=50_000, completion_tokens=50_000,
                       flagship_model=FLAG)
    router.record_task(trace_id="s1", cluster=cluster, model=BUDGET,
                       prompt_tokens=10_000, completion_tokens=10_000,
                       flagship_model=FLAG, kind="shadow")
    dash = routing_dashboard(led, router.registry)
    assert dash["tasks"] == 2
    # 毛节省 = 旗舰价跑 t2 的差额;净节省再减影子探索支出
    from naga.llm import price_micro_cents
    gross = (price_micro_cents(FLAG, 50_000, 50_000)
             - price_micro_cents(BUDGET, 50_000, 50_000))
    explore = price_micro_cents(BUDGET, 10_000, 10_000)
    assert abs(dash["gross_savings_micro_cents"] - gross) < 0.01
    assert abs(dash["net_savings_micro_cents"] - (gross - explore)) < 0.01
    assert dash["per_model"][BUDGET]["shadow_runs"] == 1
    led.close()


# ---- 网关全链 ----

def test_gateway_routes_and_explores(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    router, led2 = _router(tmp_path)  # 独立账本的 router(注册表可复用)
    led2.close()
    gw = Gateway(led, make_adapter=lambda: _OkAdapter(), home=home,
                 router=ModelRouter(registry=router.registry, ledger=led))
    goal = "总结一下今天的新闻要点"

    async def scenario():
        result = await gw.run_agent(session_key="s", goal=goal, budget=5000,
                                    trace_id="tr_r1", actor_trust="owner")
        if gw._routing_bg:
            await asyncio.gather(*gw._routing_bg)
        return result

    result = asyncio.run(scenario())
    assert result.outcome == "success"
    rows = led.query("SELECT model, kind FROM routing_tasks")
    kinds = {(m, k) for m, k in rows}
    assert (FLAG, "task") in kinds and (BUDGET, "shadow") in kinds

    from naga.curator import iter_trace_records
    records = iter_trace_records(led)
    assert any(r.get("task_cluster") == cluster_of(goal) for r in records)
    led.close()


def test_downgraded_cluster_uses_budget_model(tmp_path):
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)
    _r, led2 = _router(tmp_path)
    led2.close()
    goal = "总结一下今天的新闻要点"
    card = Scorecard(led)
    for _ in range(SHADOW_DOWNGRADE_WINS):
        card.record_shadow(cluster_of(goal), BUDGET, True)
    used: list[str] = []

    class _TrackingAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            used.append("x")
            return ChatResponse(content="便宜模型也能干", prompt_tokens=5,
                                completion_tokens=5)

    registry = ModelRegistry([
        ModelSpec(model_id=FLAG, tier="flagship", make_adapter=lambda: _OkAdapter()),
        ModelSpec(model_id=BUDGET, tier="budget", make_adapter=lambda: _TrackingAdapter()),
    ])
    gw = Gateway(led, make_adapter=lambda: _OkAdapter(), home=home,
                 router=ModelRouter(registry=registry, ledger=led))

    async def scenario():
        result = await gw.run_agent(session_key="s", goal=goal, budget=5000,
                                    trace_id="tr_r2", actor_trust="owner")
        if gw._routing_bg:
            await asyncio.gather(*gw._routing_bg)
        return result

    result = asyncio.run(scenario())
    assert result.outcome == "success" and used  # 预算模型真被调用
    rows = led.query("SELECT model FROM routing_tasks WHERE kind='task'")
    assert rows[-1][0] == BUDGET
    led.close()
