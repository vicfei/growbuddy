"""Workflow (DAG) support — the third tier of experience solidification
(appendix F, M4b-2): the interpreter is the runtime backup of the compiled.

三级固化:轨迹(原料)→ 技能(解释执行,每步需 LLM 决策)→ 工作流(编译
执行,节点间零决策成本)。DAG 从轨迹中长出,不从画布上画出——编译由数据
触发(复用 ≥ 50 次、成功率 ≥ 0.95、路径方差低),LLM 提议,影子验证,
R2 闸(测试套件通过 → 自动合并,§6.4)。

失败降级(F.4,核心差异化):节点失败不停摆——该节点降级为技能态执行
(agent loop 以源技能强制注入现场接管),工作流继续;全节点降级则整跑
回到解释器。工作流是 L2 版本树资产(CAS 寻址);每次执行绑 COW 分支,
节点 = effect(工具节点经 ctx.perform 记账),执行轨迹照常入账。
"""

from __future__ import annotations

import datetime
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, field_validator

from naga import kernel
from naga.agent import AgentLoop
from naga.branch import SessionBranch
from naga.curator import iter_trace_records, scan_skill_events
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter
from naga.skills import SkillStore
from naga.trace import TraceRecorder

WORKFLOW_SCHEMA = "naga-workflow/0"
WORKFLOW_COMPILE_MIN_USES = 50          # F.1:复用 ≥ 50(数据触发,不允许人工冲动编译)
WORKFLOW_COMPILE_MIN_SUCCESS = 0.95     # spec-constants workflow_compile_min_success_rate
WORKFLOW_COMPILE_MAX_STEPS_STDDEV = 2.0  # 候选:路径方差低(步数标准差)
NODE_KINDS = ("tool", "subagent", "human-gate", "condition", "subworkflow")
DEGRADE_BUDGET = 30_000                 # 候选 degrade_budget(F.4 接管预算)


class WorkflowNode(BaseModel):
    id: str
    kind: str
    tool: str | None = None
    goal: str | None = None             # subagent 目标;支持 {goal} 占位
    args: dict[str, Any] | None = None  # tool 参数;值支持 {goal} 占位
    budget: int | None = None           # subagent 配额(授权代数生效)
    r_level: int | None = None          # human-gate
    when: str | None = None             # condition:上次结果含此键走 then
    then: list[str] = []
    els: list[str] = []
    next: list[str] = []

    @field_validator("kind")
    @classmethod
    def _legal_kind(cls, v: str) -> str:
        if v not in NODE_KINDS:
            raise ValueError(f"非法节点类型: {v}(合法:{'/'.join(NODE_KINDS)})")
        return v


class WorkflowSpec(BaseModel):
    schema_: str = WORKFLOW_SCHEMA
    name: str
    version: int = 1
    compiled_from: str | None = None
    source_skill: str | None = None      # 降级回退的解释器(F.4)
    entry: str = "n1"
    nodes: list[WorkflowNode]

    def node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def validate_dag(self) -> list[str]:
        problems: list[str] = []
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            problems.append("节点 id 重复")
        if self.entry not in ids:
            problems.append(f"入口 {self.entry!r} 不在节点集")
        for n in self.nodes:
            targets = n.next + n.then + n.els
            for t in targets:
                if t not in ids:
                    problems.append(f"节点 {n.id} 指向不存在的 {t!r}")
            if n.kind == "condition" and not n.then:
                problems.append(f"条件节点 {n.id} 缺 then 分支")
            if n.kind == "tool" and not n.tool:
                problems.append(f"工具节点 {n.id} 缺 tool")
            if n.kind == "subworkflow" and not n.next:
                problems.append(f"子工作流节点 {n.id} 缺承接(next)")
        # 可达性:从入口能走到每个节点
        reach: set[str] = set()
        frontier: list[str] = [self.entry]
        while frontier:
            cur = frontier.pop()
            if cur in reach:
                continue
            reach.add(cur)
            node = self.node(cur)
            if node is not None:
                frontier.extend(node.next + node.then + node.els)
        unreachable = set(ids) - reach
        if unreachable:
            problems.append(f"不可达节点: {sorted(unreachable)}")
        return problems


class WorkflowStore:
    """工作流 = L2 版本树资产:CAS 本体 + workflows/workflow_runs 投影。"""

    def __init__(self, ledger: DurableLedger):
        self._ledger = ledger

    def commit(self, spec: WorkflowSpec, *, actor: str = "root") -> str:
        problems = spec.validate_dag()
        if problems:
            raise ValueError("DAG 非法: " + ";".join(problems))
        rows = self._ledger.query(
            "SELECT version FROM workflows WHERE name=? ORDER BY version DESC "
            "LIMIT 1", (spec.name,))
        version = (rows[0][0] + 1) if rows else 1
        spec.version = version
        cas = self._ledger.cas.put(spec.model_dump_json().encode())
        self._ledger.execute(
            "INSERT INTO workflows (name, version, cas_id, compiled_from, "
            "created_at) VALUES (?,?,?,?,?)",
            (spec.name, version, cas, spec.compiled_from,
             datetime.datetime.now(datetime.UTC).isoformat()))
        self._ledger.append("workflow.compiled", actor,
                            {"name": spec.name, "version": version, "cas": cas,
                             "compiled_from": spec.compiled_from})
        return cas

    def latest(self, name: str) -> WorkflowSpec | None:
        rows = self._ledger.query(
            "SELECT cas_id FROM workflows WHERE name=? ORDER BY version DESC "
            "LIMIT 1", (name,))
        if not rows:
            return None
        return WorkflowSpec.model_validate_json(
            self._ledger.cas.get(rows[0][0]))

    def record_run(self, run_id: str, name: str, outcome: str,
                   detail: str = "") -> None:
        self._ledger.execute(
            "INSERT OR REPLACE INTO workflow_runs (run_id, workflow, outcome, "
            "detail, started_at) VALUES (?,?,?,?,?)",
            (run_id, name, outcome, detail,
             datetime.datetime.now(datetime.UTC).isoformat()))


# ---- 编译触发判据(F.1:数据触发) ----

@dataclass
class CompileEligibility:
    name: str
    uses: int = 0
    success_rate: float = 0.0
    steps_stddev: float = 0.0
    eligible: bool = False
    blockers: list[str] = None  # type: ignore[assignment]

    def reasons(self) -> list[str]:
        out = list(self.blockers or [])
        if self.uses < WORKFLOW_COMPILE_MIN_USES:
            out.append(f"复用 {self.uses} < {WORKFLOW_COMPILE_MIN_USES}")
        if self.success_rate < WORKFLOW_COMPILE_MIN_SUCCESS:
            out.append(f"成功率 {self.success_rate:.2f} < "
                       f"{WORKFLOW_COMPILE_MIN_SUCCESS}")
        if self.steps_stddev > WORKFLOW_COMPILE_MAX_STEPS_STDDEV:
            out.append(f"路径方差 {self.steps_stddev:.2f} > "
                       f"{WORKFLOW_COMPILE_MAX_STEPS_STDDEV}")
        return out


def compile_eligibility(ledger: DurableLedger, name: str) -> CompileEligibility:
    """从账本判据:skill.use 计数 + 使用该技能的轨迹成功率与步数方差。"""
    events = scan_skill_events(ledger)
    uses = 0
    for (skill_name, branch) in events["committed"]:
        if skill_name != name or branch != "main":
            continue
    rows = ledger.query("SELECT cas_id FROM skill_nodes WHERE name=? AND "
                        "branch='main' ORDER BY version DESC LIMIT 1", (name,))
    if rows:
        uses = events["uses"].get(rows[0][0], 0)
    uri_frag = f"skill://{name}@"
    outcomes: list[str] = []
    step_counts: list[int] = []
    for payload in iter_trace_records(ledger):
        raw_loaded: Any = payload.get("skills_loaded") or []
        loaded = cast("list[Any]", raw_loaded)
        if not any(isinstance(u, str) and uri_frag in u for u in loaded):
            continue
        outcomes.append(str(payload.get("outcome")))
        raw_steps: Any = payload.get("steps")
        steps = cast("list[Any]", raw_steps if isinstance(raw_steps, list) else [])
        step_counts.append(len(steps))
    success_rate = (outcomes.count("success") / len(outcomes)) if outcomes else 0.0
    stddev = statistics.pstdev(step_counts) if len(step_counts) >= 2 else 0.0
    el = CompileEligibility(name=name, uses=uses, success_rate=success_rate,
                            steps_stddev=stddev)
    el.eligible = (uses >= WORKFLOW_COMPILE_MIN_USES
                   and success_rate >= WORKFLOW_COMPILE_MIN_SUCCESS
                   and stddev <= WORKFLOW_COMPILE_MAX_STEPS_STDDEV)
    return el


# ---- LLM 编译器(F.2:从技能与轨迹长出 DAG) ----

COMPILE_PROMPT = (
    "你是工作流编译器。根据以下技能文档与它的历史执行模式,把它编译为一个"
    "可执行 DAG(节点间零决策成本)。\n"
    "输出一个 JSON 对象(只输出 JSON,不要解释):"
    '{"name": "<短横线小写英文>", "entry": "<入口节点id>", "nodes": ['
    '{"id": "...", "kind": "tool|subagent|condition|human-gate|subworkflow", '
    '"tool": "<工具名,kind=tool 时>", "args": {"参数": "值,{goal} 会替换为任务目标"}, '
    '"goal": "<subagent 目标,{goal} 占位>", "budget": <subagent token 配额>, '
    '"when": "<condition: 上次结果含此键>", "then": ["..."], "els": ["..."], '
    '"next": ["后续节点id"]}]}\n'
    "规则:节点 id 唯一;从 entry 可达全部节点;工具节点给具体参数;子任务节点"
    "给明确目标与预算;线性流程用 next,分支用 condition+then/els。\n"
    "技能文档:\n{skill}\n\n历史执行模式(步骤统计):{pattern}"
)


class WorkflowCompiler:
    def __init__(self, *, ledger: DurableLedger,
                 make_adapter: Callable[[], ModelAdapter]):
        self._ledger = ledger
        self._make = make_adapter

    async def compile_skill(self, name: str) -> WorkflowSpec | None:
        got = SkillStore(self._ledger).get(name, branch="main")
        if got is None:
            return None
        _meta, content = got
        el = compile_eligibility(self._ledger, name)
        if not el.eligible:
            raise ValueError("编译判据未满足: " + ";".join(el.reasons()))
        pattern = (f"复用 {el.uses} 次,成功率 {el.success_rate:.0%},"
                   f"步数方差 {el.steps_stddev:.2f}")
        adapter = self._make()
        prompt = (COMPILE_PROMPT
                  .replace("{skill}", content[:4000])
                  .replace("{pattern}", pattern))
        resp = await adapter.chat(
            [{"role": "user", "content": prompt}],
            None, max_tokens=8192)
        raw = (resp.content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        spec = WorkflowSpec(source_skill=name,
                            compiled_from=f"skill://{name}",
                            **{k: v for k, v in data.items()
                               if k in ("name", "entry", "nodes")})
        if spec.validate_dag():
            return None  # 非法 DAG:拒绝(测试套件口径,R2 闸的一部分)
        return spec


# ---- 执行器(F.3/F.4) ----

class WorkflowExecutor:
    """DAG 执行:节点 = effect;节点失败降级为技能态执行(F.4)。"""

    def __init__(self, *, ledger: DurableLedger,
                 make_adapter: Callable[[], ModelAdapter],
                 tools: list[Any] | None = None):
        self._ledger = ledger
        self._make = make_adapter
        self._tools = {t.name: t for t in (tools or [])}

    async def run(self, spec: WorkflowSpec, goal: str,
                  *, session_key: str = "workflow:run") -> dict[str, Any]:
        run_id = "wf_" + datetime.datetime.now(datetime.UTC).strftime(
            "%H%M%S") + str(len(spec.nodes))
        kern = kernel.Kernel(ledger=self._ledger)  # type: ignore[arg-type]
        root_branch = SessionBranch(
            owner_session=f"workflow:{spec.name}", base_get=lambda k: None,
            base_has=lambda k: False, cas_put=self._ledger.cas.put)
        skill = self._skill_content(spec.source_skill)
        rec = TraceRecorder(self._ledger, trace_id=f"tr_{run_id}",
                            session_key=session_key, inbound={"text": goal},
                            env={"workflow": spec.name, "version": spec.version})
        results: dict[str, Any] = {}
        node_id: str | None = spec.entry
        degraded: list[str] = []
        outcome, stopped_at = "success", None
        step_no = 0
        while node_id is not None:
            node = spec.node(node_id)
            if node is None:
                outcome, stopped_at = "failed", f"未知节点 {node_id}"
                break
            step_no += 1
            ok, value, ms, node_degraded = await self._run_node(
                node, goal, results, kern, root_branch, skill)
            degraded.append(node_id) if node_degraded else None
            rec.step(step_no, node.kind, node_id, ok, ms, 0,
                     args={"goal": goal[:60]}, results=value)
            if node.kind == "human-gate":
                outcome, stopped_at = "awaiting_review", node_id
                break
            if not ok:
                outcome, stopped_at = "failed", node_id
                break
            results[node_id] = value
            node_id = self._next_of(node, value)
        rec.finish(outcome=outcome, task_cluster=f"workflow:{spec.name}",
                   failure_analysis=stopped_at)
        WorkflowStore(self._ledger).record_run(run_id, spec.name, outcome,
                                               f"degraded={degraded}" if degraded
                                               else "")
        return {"run_id": run_id, "outcome": outcome, "stopped_at": stopped_at,
                "degraded_nodes": degraded, "results": results}

    def _next_of(self, node: WorkflowNode, value: Any) -> str | None:
        if node.kind == "condition":
            hit = isinstance(value, dict) and node.when in value
            branch = node.then if hit else node.els
            return branch[0] if branch else None
        return node.next[0] if node.next else None

    async def _run_node(self, node: WorkflowNode, goal: str,
                        results: dict[str, Any], kern: kernel.Kernel,
                        branch: SessionBranch,
                        skill: str | None) -> tuple[bool, Any, int, bool]:
        import time as _time
        import uuid as _uuid

        t0 = _time.monotonic()
        try:
            if node.kind == "condition":
                last = _last_id(results)
                raw: Any = results.get(last) if last is not None else {}
                value = cast("dict[str, Any]",
                             raw if isinstance(raw, dict) else {})
                return True, value, 0, False
            if node.kind == "human-gate":
                return True, {"gate": node.id, "r_level": node.r_level}, 0, False
            if node.kind == "tool":
                tool = self._tools.get(node.tool or "")
                if tool is None:
                    raise ValueError(f"工作流引用未知工具 {node.tool!r}")
                args = {k: (v.replace("{goal}", goal) if isinstance(v, str) else v)
                        for k, v in (node.args or {}).items()}

                async def _apply() -> Any:
                    return await tool.run(args)

                value = await kern.root.perform(
                    f"node:{node.id}:{node.tool}", _apply, lambda: None)
                return True, value, int((_time.monotonic() - t0) * 1000), False
            if node.kind in ("subagent", "subworkflow"):
                node_goal = (node.goal or goal).replace("{goal}", goal)
                budget = node.budget or DEGRADE_BUDGET
                loop = AgentLoop(
                    ledger=self._ledger, ctx=kern.root,
                    budget=kernel.Budget(budget),
                    adapter=self._make(), tools=list(self._tools.values()),
                    skill_store=SkillStore(self._ledger),
                    session_key="workflow:node",
                    trace_id="tr_wf_" + _uuid.uuid4().hex[:16],
                    forced_skills=([(f"skill://{node.id}", skill)]
                                   if skill else None),
                    meter_skills=False, branch=branch.spawn_child("wf:node"),
                    max_steps=4)
                result = await loop.run(node_goal)
                if result.outcome == "success":
                    return (True, {"reply": result.reply, "tokens": result.tokens},
                            int((_time.monotonic() - t0) * 1000), False)
                # F.4:节点失败(含配额耗尽的 aborted 形态)→ 技能态接管
                value = await self._degrade(
                    node, goal, kern, branch, skill,
                    RuntimeError(f"subagent {result.outcome}: "
                                 f"{result.failure_analysis}"))
                ms = int((_time.monotonic() - t0) * 1000)
                return ((value is not None),
                        value or {"degrade_failed": True}, ms, True)
            return False, {"error": f"未支持节点类型 {node.kind}"}, 0, False
        except Exception as err:  # noqa: BLE001 —— F.4:节点失败降级为技能态执行
            value = await self._degrade(node, goal, kern, branch, skill, err)
            ms = int((_time.monotonic() - t0) * 1000)
            return (value is not None), value or {"degrade_failed": True}, ms, True

    async def _degrade(self, node: WorkflowNode, goal: str,
                       kern: kernel.Kernel, branch: SessionBranch,
                       skill: str | None, err: Exception) -> Any:
        """F.4 失败降级:解释器(源技能 + agent loop)现场接管该节点。"""
        import uuid as _uuid

        objective = (f"工作流节点 {node.id}({node.kind}"
                     + (f":{node.tool}" if node.tool else "")
                     + f")执行失败({type(err).__name__}: {err})。"
                     f"请按技能指引直接完成该节点的目标:{(node.goal or goal)}"
                     .replace("{goal}", goal))
        loop = AgentLoop(
            ledger=self._ledger, ctx=kern.root,
            budget=kernel.Budget(DEGRADE_BUDGET),
            adapter=self._make(), tools=list(self._tools.values()),
            skill_store=SkillStore(self._ledger),
            session_key="workflow:degrade",
            trace_id="tr_wfd_" + _uuid.uuid4().hex[:16],
            forced_skills=([("skill://degrade", skill)] if skill else None),
            meter_skills=False, branch=branch.spawn_child("wf:degrade"))
        result = await loop.run(objective)
        return {"reply": result.reply, "degraded_from": node.id,
                "outcome": result.outcome} if result.outcome == "success" else None

    def _skill_content(self, source_skill: str | None) -> str | None:
        if not source_skill:
            return None
        got = SkillStore(self._ledger).get(source_skill, branch="main")
        return got[1] if got else None


def _last_id(results: dict[str, Any]) -> str | None:
    return next(iter(reversed(list(results))), None) if results else None
