"""GEPA 周循环(§7.5,M4a-3)——离线反思优化的大循环(受控递归)。

流程:失败聚类 → 归因分析(LLM 阅读失败轨迹) → 定向变异提案(技能草稿)
     → 影子验证(真实历史目标对打) → R 闸(复用 M4a-1 晋升管道)。
预算护栏:gepa_run_cost_cap_cents=1000(spec-constants 已注册;价格表未生效
期间以 token 二级护栏执行,机制就位)。周期:gepa_cycle_days=7,空闲窗口。

提案落 local:gepa 分支后走完整管道(体检/影子/R 路由全同人工路径)——
优化器没有任何绕闸特权(§7 双飞轮:探索在分支,合并在闸门)。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from naga.curator import iter_trace_records
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse, ModelAdapter, price_cents
from naga.promotion import PromotionPipeline, health_check
from naga.skills import SkillStore

GEPA_RUN_COST_CAP_CENTS = 1000    # spec-constants gepa_run_cost_cap_cents(§7.5)
GEPA_RUN_TOKEN_CAP = 2_000_000    # 候选 gepa_run_token_cap(价格表生效前的二级护栏)
GEPA_MAX_CLUSTERS = 3             # 单次循环处理的失败聚类数上限
GEPA_CLUSTER_GOALS = 2            # 每聚类影子回放目标数

ATTRIBUTION_PROMPT = (
    "你是系统的离线优化器。以下是近期失败的任务轨迹摘要(目标与失败原因)。\n"
    "请提出恰好一个技能(skill)来预防这类失败——技能会在未来同类任务时注入"
    "智能体的上下文。输出格式:\n"
    "第一行 id: <短横线小写英文技能名>,随后是 Markdown 正文,必须包含三个"
    "二级标题节:## 何时使用、## 步骤、## 已知陷阱。\n"
    "只输出技能本身,不要任何解释。\n\n失败轨迹:\n{failures}"
)


class CountingAdapter:
    """包装任意 ModelAdapter,累计 tokens/费用(预算护栏的计量点)。"""

    def __init__(self, inner: ModelAdapter, counter: UsageCounter):
        self._inner = inner
        self._counter = counter

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[Any] | None = None, *,
                   max_tokens: int = 1024) -> ChatResponse:
        resp = await self._inner.chat(messages, tools, max_tokens=max_tokens)
        self._counter.add(resp.prompt_tokens, resp.completion_tokens)
        return resp


class UsageCounter:
    def __init__(self, model_id: str = "deepseek-v4-flash") -> None:
        self.tokens = 0
        self.cost_cents = 0
        self._model = model_id

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.tokens += prompt_tokens + completion_tokens
        self.cost_cents += price_cents(self._model, prompt_tokens,
                                       completion_tokens)

    @property
    def over_cap(self) -> bool:
        return (self.cost_cents > GEPA_RUN_COST_CAP_CENTS
                or self.tokens > GEPA_RUN_TOKEN_CAP)


@dataclass
class FailureCluster:
    signature: str
    items: list[dict[str, Any]] = field(
        default_factory=list[dict[str, Any]])

    @property
    def goals(self) -> list[str]:
        return [it["goal"] for it in self.items[:GEPA_CLUSTER_GOALS]
                if it.get("goal")]


@dataclass
class GepaCycleReport:
    failures_found: int = 0
    clusters: int = 0
    proposals: int = 0
    promoted: list[str] = field(default_factory=list[str])
    pending_review: list[str] = field(default_factory=list[str])
    rejected: list[str] = field(default_factory=list[str])
    cost_cents: int = 0
    tokens: int = 0
    stopped_reason: str | None = None


class GepaOptimizer:
    """一次 run_cycle = 一轮完整优化(§7.5);无失败种群则为空转(合法结局)。"""

    def __init__(self, *, ledger: DurableLedger,
                 make_adapter: Callable[[], ModelAdapter],
                 pipeline: PromotionPipeline,
                 model_id: str = "deepseek-v4-flash"):
        self._ledger = ledger
        self._counter = UsageCounter(model_id)
        self._make = make_adapter
        self._pipeline = pipeline

    @property
    def counter(self) -> UsageCounter:
        return self._counter

    def failure_population(self) -> list[dict[str, Any]]:
        """失败轨迹种群:outcome 非 success 且带 inbound 目标(shadow 自身除外)。"""
        out: list[dict[str, Any]] = []
        for payload in iter_trace_records(self._ledger):
            if payload.get("outcome") == "success":
                continue
            if str(payload.get("session_key", "")).startswith("shadow:"):
                continue  # 影子运行自身不计入种群(避免自反馈)
            out.append({"trace_id": payload.get("trace_id"),
                        "session_key": payload.get("session_key"),
                        "failure": str(payload.get("failure_analysis") or "unknown"),
                        "goal": self._goal_of(payload)})
        return [it for it in out if it["goal"]]

    def _goal_of(self, payload: dict[str, Any]) -> str | None:
        cas = payload.get("replay_bundle_cas")
        if not cas:
            return None
        import json
        try:
            bundle = json.loads(self._ledger.cas.get(str(cas)))
            text = bundle.get("session", {}).get("inbound", {}).get("text")
        except (OSError, ValueError):
            return None
        return text.strip() if isinstance(text, str) and text.strip() else None

    def cluster_failures(self, population: list[dict[str, Any]]
                         ) -> list[FailureCluster]:
        """按失败签名聚类(签名=去数字的 failure_analysis;宁粗勿细)。"""
        groups: dict[str, FailureCluster] = {}
        for item in population:
            sig = re.sub(r"\d+", "#", item["failure"])[:60] or "unknown"
            groups.setdefault(sig, FailureCluster(signature=sig)).items.append(item)
        clusters = sorted(groups.values(), key=lambda c: -len(c.items))
        return clusters[:GEPA_MAX_CLUSTERS]

    async def propose(self, cluster: FailureCluster) -> tuple[str, str] | None:
        """归因分析 + 定向变异提案(LLM);返回 (skill_name, content) 或 None。"""
        failures = "\n".join(
            f"- 目标: {it['goal'][:80]}\n  失败: {it['failure'][:60]}"
            for it in cluster.items[:5])
        prompt = ATTRIBUTION_PROMPT.format(failures=failures)
        adapter = CountingAdapter(self._make(), self._counter)
        resp = await adapter.chat([{"role": "user", "content": prompt}], None,
                                  max_tokens=8192)
        body = (resp.content or "").strip()
        if not body:
            return None
        m = re.match(r"^id:\s*([a-z0-9][a-z0-9-]*)\s*\n", body)
        if not m:
            return None
        name = m.group(1)
        content = f"---\nname: {name}\n---\n{body[m.end():]}\n"
        if not health_check(content).ok:
            return None  # 提案连体检都不过:丢弃(对抗性/畸形输出)
        return name, content

    async def run_cycle(self) -> GepaCycleReport:
        report = GepaCycleReport()
        population = self.failure_population()
        report.failures_found = len(population)
        clusters = self.cluster_failures(population)
        report.clusters = len(clusters)
        for cluster in clusters:
            if self._counter.over_cap:
                report.stopped_reason = "预算护栏触发(§7.5)"
                break
            proposal = await self.propose(cluster)
            if proposal is None:
                continue
            name, content = proposal
            report.proposals += 1
            SkillStore(self._ledger).commit(
                name, content, provenance={"source": "agent", "origin": "gepa",
                                           "cluster": cluster.signature},
                branch="local:gepa")
            result = await self._pipeline.promote(name, from_session="gepa",
                                                  goals=cluster.goals)
            if result.status == "promoted":
                report.promoted.append(name)
            elif result.status == "pending_review":
                report.pending_review.append(name)
            else:
                report.rejected.append(name)
        report.cost_cents = self._counter.cost_cents
        report.tokens = self._counter.tokens
        return report
