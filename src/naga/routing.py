"""Model auto-routing — Auto Mode (appendix E, M4b-1): the third flywheel.

任务 × 模型结果 → 优化"什么任务配什么模型",持续降低前两轮的运行成本。

- **任务簇**:cluster_of(goal) —— 目标关键词的确定性分簇(E.2 按簇记账,
  不搞总排名);
- **记分卡** (E.2):正信号=成功,负信号=失败(负权 > 正权);影子胜负
  独立计数;信号持久于 model_scorecard(运营投影);
- **路由器** (E.3 带约束老虎机):冷启动从强往弱——未证实簇一律旗舰,
  **只向下探索,永不向上冒险**;某簇影子累计 SHADOW_DOWNGRADE_WINS 次且
  零失败 → 该簇正式降档到预算模型;
- **影子探索** (E.4):旗舰任务成功后,后台用便宜模型在影子分支回放同一
  任务(用户无感知);试错成本转移到算力账单,用户看到的永远是被验证过的
  选择;影子失败立即阻断降档;
- **节省仪表盘** (M4b 出口①):每次任务按"旗舰费率 - 实付费率"计节省,
  影子探索支出单列;CLI/HTTP 可查。
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from naga import kernel
from naga.agent import AgentLoop
from naga.branch import SessionBranch
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter, price_micro_cents
from naga.skills import SkillStore

SHADOW_DOWNGRADE_WINS = 3   # 候选 spec-constants shadow_downgrade_wins(E.4 累计 N 次)
SHADOW_EXPLORE_BUDGET = 30_000  # 候选 shadow_explore_budget(单次影子回放预算)
SHADOW_EXPLORE_MAX_PENDING = 2  # 同簇并发影子回放上-limit(防探索风暴)


from naga.agent import cluster_of


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    tier: str                       # flagship | budget
    make_adapter: Callable[[], ModelAdapter]


class ModelRegistry:
    def __init__(self, specs: list[ModelSpec]):
        tiers = {s.tier for s in specs}
        if "flagship" not in tiers:
            raise ValueError("registry 需要至少一个 flagship 档")
        self._specs = specs

    @property
    def specs(self) -> list[ModelSpec]:
        return list(self._specs)

    def flagship(self) -> ModelSpec:
        return next(s for s in self._specs if s.tier == "flagship")

    def budget_of(self, model_id: str) -> ModelSpec | None:
        return next((s for s in self._specs
                     if s.tier == "budget" and s.model_id == model_id), None)

    def find(self, model_id: str) -> ModelSpec | None:
        return next((s for s in self._specs if s.model_id == model_id), None)


class Scorecard:
    """(任务簇 × 模型)记分卡(E.2);运营投影,可从账本轨迹重建。"""

    def __init__(self, ledger: DurableLedger):
        self._ledger = ledger
        self._inflight: dict[str, int] = {}  # 簇 → 影子在飞数(进程内)

    def record_result(self, cluster: str, model: str, success: bool) -> None:
        self._bump(cluster, model, "successes" if success else "failures", 1)

    def record_shadow(self, cluster: str, model: str, win: bool) -> None:
        self._bump(cluster, model, "shadow_wins" if win else "shadow_losses", 1)

    def _bump(self, cluster: str, model: str, column: str, n: int) -> None:
        self._ledger.execute(
            "INSERT INTO model_scorecard (cluster, model) VALUES (?,?) "
            "ON CONFLICT DO NOTHING", (cluster, model))
        self._ledger.execute(
            f"UPDATE model_scorecard SET {column} = {column} + ? "
            "WHERE cluster=? AND model=?", (n, cluster, model))

    def proven_budget(self, cluster: str) -> str | None:
        """降档裁决(E.4):影子累计达标且零失败 → 该预算模型正式接手此簇。"""
        rows = self._ledger.query(
            "SELECT model, shadow_wins, shadow_losses FROM model_scorecard "
            "WHERE cluster=?", (cluster,))
        for model, wins, losses in rows:
            if wins >= SHADOW_DOWNGRADE_WINS and losses == 0:
                return str(model)
        return None

    def begin_shadow(self, cluster: str) -> None:
        self._inflight[cluster] = self._inflight.get(cluster, 0) + 1

    def end_shadow(self, cluster: str) -> None:
        self._inflight[cluster] = self._inflight.get(cluster, 1) - 1

    def pending_shadow(self, cluster: str) -> int:
        """该簇影子回放的**在飞**计数(探索风暴护栏;完成的不计——否则窗口内
        永久阻断,降档永远凑不齐;真 bug 由真实降档演示抓出,2026-10-11)。"""
        return self._inflight.get(cluster, 0)

    def rows(self) -> list[tuple[Any, ...]]:
        return self._ledger.query(
            "SELECT cluster, model, successes, failures, shadow_wins, "
            "shadow_losses FROM model_scorecard ORDER BY cluster, model")


class ModelRouter:
    """E.3 带约束老虎机:冷启动从强往弱,只向下探索。"""

    def __init__(self, *, registry: ModelRegistry, ledger: DurableLedger,
                 tools: list[Any] | None = None):
        self._registry = registry
        self._ledger = ledger
        self._scorecard = Scorecard(ledger)
        self._tools = tools or []
        # 影子父分支:永不结算 → 子分支(每次影子回放)永不物化(同 M4a 纪律)
        self._shadow_root = SessionBranch(
            owner_session="shadow:routing", base_get=lambda k: None,
            base_has=lambda k: False, cas_put=ledger.cas.put)

    @property
    def registry(self) -> ModelRegistry:
        return self._registry

    @property
    def scorecard(self) -> Scorecard:
        return self._scorecard

    def pick(self, goal: str) -> ModelSpec:
        cluster = cluster_of(goal)
        proven = self._scorecard.proven_budget(cluster)
        if proven is not None:
            budget = self._registry.budget_of(proven)
            if budget is not None:
                return budget  # 已证实:便宜模型接手此簇
        return self._registry.flagship()  # 冷启动从强往弱:未证实一律旗舰

    def record_task(self, *, trace_id: str, cluster: str, model: str,
                    prompt_tokens: int, completion_tokens: int,
                    flagship_model: str, kind: str = "task") -> None:
        self._ledger.execute(
            "INSERT OR REPLACE INTO routing_tasks (trace_id, cluster, model, "
            "prompt_tokens, completion_tokens, flagship_model, kind, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (trace_id, cluster, model, prompt_tokens, completion_tokens,
             flagship_model, kind, datetime.datetime.now(datetime.UTC).isoformat()))

    async def shadow_explore(self, goal: str, *, session_key: str,
                             budget: ModelSpec | None = None) -> bool | None:
        """E.4 影子回放:旗舰成功后,便宜模型跑同一任务(影子分支,用户无感知)。
        返回 win(True)/loss(False)/未探索(None)。"""
        cluster = cluster_of(goal)
        budget = budget or next(
            (s for s in self._registry.specs if s.tier == "budget"), None)
        if budget is None or self._scorecard.pending_shadow(cluster) >= \
                SHADOW_EXPLORE_MAX_PENDING:
            return None
        trace_id = "tr_rt_" + uuid.uuid4().hex[:18]
        self._scorecard.begin_shadow(cluster)
        try:
            return await self._shadow_explore_inner(goal, session_key, budget,
                                                    cluster, trace_id)
        finally:
            self._scorecard.end_shadow(cluster)

    async def _shadow_explore_inner(self, goal: str, session_key: str,
                                    budget: ModelSpec, cluster: str,
                                    trace_id: str) -> bool | None:
        assert budget is not None  # 外层已拦截 None(类型收窄)
        kern = kernel.Kernel(ledger=self._ledger)  # type: ignore[arg-type]
        branch = self._shadow_root.spawn_child("shadow:routing")
        loop = AgentLoop(
            ledger=self._ledger, ctx=kern.root,
            budget=kernel.Budget(SHADOW_EXPLORE_BUDGET),
            adapter=budget.make_adapter(), tools=self._tools,
            skill_store=SkillStore(self._ledger),
            session_key=f"shadow:routing:{cluster[:24]}", trace_id=trace_id,
            meter_skills=False, branch=branch, model_id=budget.model_id)
        self.record_task(trace_id=trace_id, cluster=cluster,
                         model=budget.model_id, prompt_tokens=0,
                         completion_tokens=0,
                         flagship_model=self._registry.flagship().model_id,
                         kind="shadow")  # 预占探索风暴护栏名额(完成时补 token)
        try:
            result = await loop.run(goal)
        except Exception:  # noqa: BLE001 —— 影子回放:异常计为探索失败
            result = None
        win = result is not None and result.outcome == "success"
        self._scorecard.record_shadow(cluster, budget.model_id, win)
        if result is not None:
            prompt_toks = max(0, result.tokens)
            self.record_task(trace_id=trace_id, cluster=cluster,
                             model=budget.model_id, prompt_tokens=prompt_toks,
                             completion_tokens=0,
                             flagship_model=self._registry.flagship().model_id,
                             kind="shadow")
        return win


def routing_dashboard(ledger: DurableLedger, registry: ModelRegistry) -> dict[str, Any]:
    """节省仪表盘(M4b 出口①):按旗舰费率折算的节省与探索支出,微美分计。"""
    rows = ledger.query(
        "SELECT cluster, model, kind, prompt_tokens, completion_tokens, "
        "flagship_model FROM routing_tasks")
    savings = exploration = 0.0
    per_model: dict[str, dict[str, Any]] = {}
    flagship = registry.flagship().model_id
    for _cluster, model, kind, ptoks, ctoks, flagship_model in rows:
        actual = price_micro_cents(model, ptoks, ctoks)
        at_flagship = price_micro_cents(flagship_model or flagship, ptoks, ctoks)
        if kind == "shadow":
            exploration += actual
        else:
            savings += at_flagship - actual
        entry = per_model.setdefault(model, {"tasks": 0, "shadow_runs": 0,
                                             "tokens": 0, "actual_micro_cents": 0.0})
        entry["tasks" if kind == "task" else "shadow_runs"] += 1
        entry["tokens"] += ptoks + ctoks
        entry["actual_micro_cents"] += actual
    return {
        "price_table_version": __import__("naga.llm", fromlist=["x"]).PRICE_TABLE_VERSION,
        "net_savings_micro_cents": round(savings - exploration, 3),
        "gross_savings_micro_cents": round(savings, 3),
        "exploration_spend_micro_cents": round(exploration, 3),
        "tasks": sum(e["tasks"] for e in per_model.values()),
        "per_model": per_model,
        "scorecard": [list(r) for r in Scorecard(ledger).rows()],
    }
