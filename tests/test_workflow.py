"""M4b-2 tests: workflow compilation (appendix F) — trigger criteria, LLM
compiler validation, DAG execution, condition branches, subagent quotas,
human gates, and the F.4 failure degradation (interpreter takes over)."""

import asyncio
import json

import pytest

from naga import storage
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.skills import SkillStore
from naga.trace import TraceRecorder
from naga.workflow import (
    WorkflowCompiler,
    WorkflowExecutor,
    WorkflowNode,
    WorkflowSpec,
    WorkflowStore,
    compile_eligibility,
)

SKILL = """---
name: multi-summary
---
## 何时使用
分别调研两个主题并汇总时使用。
## 步骤
每个主题搜索一次,再合并要点。
## 已知陷阱
不要混入无关主题。
"""


_HOME_SEQ = {"n": 0}


def _home(tmp_path) -> tuple[str, DurableLedger]:
    _HOME_SEQ["n"] += 1
    home = str(tmp_path / f"h{_HOME_SEQ['n']}")
    storage.init_home(home)
    return home, DurableLedger(home)


class _SearchTool:
    name, description, parameters = "web_search", "d", {"type": "object"}
    side_effect, risk_level = "read", 0
    fail_next = False

    async def run(self, args):
        if _SearchTool.fail_next:
            _SearchTool.fail_next = False
            raise RuntimeError("工具坏了")
        return {"results": [{"name": f"关于{args.get('query', '')[:8]}的摘要"}]}


class _SummarizeAdapter:
    """subagent/degrade 调用:见 forced 技能在场即给优质回答。"""

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        system = next((m["content"] for m in messages
                       if m.get("role") == "system"), "")
        user = messages[-1].get("content", "") if messages else ""
        if "多主题" in system or "技能" in system or "技能" in user:
            return ChatResponse(content="按技能完成汇总", prompt_tokens=5,
                                completion_tokens=5)
        return ChatResponse(content="泛泛回答", prompt_tokens=5, completion_tokens=5)


class _GreedyAdapter:
    """耗尽配额的 subagent:单步吃光预算 → 节点失败 → 触发 F.4 降级。
    降级目标(含「工作流节点」标记)则正常作答——降级路径使用独立预算。"""

    async def chat(self, messages, tools=None, *, max_tokens=1024):
        user = messages[-1].get("content", "") if messages else ""
        if "工作流节点" in user:
            return ChatResponse(content="降级接管完成", prompt_tokens=5,
                                completion_tokens=5)
        return ChatResponse(content="话痨", prompt_tokens=10,
                            completion_tokens=10_000_000)


def _seed_usage(led: DurableLedger, name: str = "multi-summary", uses: int = 50,
                successes: int = 50, failures: int = 0) -> None:
    skills = SkillStore(led)
    skills.commit(name, SKILL, provenance={"source": "agent"}, branch="main")
    cas = led.query("SELECT cas_id FROM skill_nodes WHERE name=? "
                    "AND branch='main'", (name,))[0][0]
    for _ in range(uses):
        led.append("skill.use", "root", {"cas_id": cas})
    uri = f"skill://{name}@v1"
    [{"no": i, "kind": "llm", "ok": True} for i in range(3)]
    for i in range(successes):
        rec = TraceRecorder(led, trace_id=f"tr_ok{i}", session_key="s",
                            inbound={"text": f"调研任务{i}"},
                            skills_loaded=[uri])
        rec.finish(outcome="success", cost_tokens=5)
    for i in range(failures):
        rec = TraceRecorder(led, trace_id=f"tr_bad{i}", session_key="s",
                            inbound={"text": f"任务{i}"},
                            skills_loaded=[uri])
        rec.finish(outcome="aborted", cost_tokens=5, failure_analysis="x")


# ---- 触发判据 ----

def test_compile_eligibility_thresholds(tmp_path):
    _home_dir, led = _home(tmp_path)
    _seed_usage(led, uses=49, successes=20)
    el = compile_eligibility(led, "multi-summary")
    assert not el.eligible and any("复用" in r for r in el.reasons())

    _home2, led2 = _home(tmp_path)
    _seed_usage(led2, uses=50, successes=18, failures=2)  # 成功率 0.9
    el2 = compile_eligibility(led2, "multi-summary")
    assert not el2.eligible and any("成功率" in r for r in el2.reasons())

    _home3, led3 = _home(tmp_path)
    _seed_usage(led3, uses=50, successes=50)
    el3 = compile_eligibility(led3, "multi-summary")
    assert el3.eligible, el3.reasons()
    led.close(); led2.close(); led3.close()


# ---- 编译器:LLM 提议 + 结构校验 ----

DAG_JSON = json.dumps({
    "name": "multi-summary-dag",
    "entry": "q1",
    "nodes": [
        {"id": "q1", "kind": "tool", "tool": "web_search",
         "args": {"query": "第一个主题 {goal}"}, "next": ["q2"]},
        {"id": "q2", "kind": "tool", "tool": "web_search",
         "args": {"query": "第二个主题"}, "next": ["sum"]},
        {"id": "sum", "kind": "subagent", "goal": "汇总两次搜索结果:{goal}",
         "budget": 8000, "next": []},
    ]})


def test_compiler_parses_and_validates(tmp_path):
    _home_dir, led = _home(tmp_path)
    _seed_usage(led)

    class _CompilerAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(content=DAG_JSON, prompt_tokens=10,
                                completion_tokens=10)

    compiler = WorkflowCompiler(ledger=led, make_adapter=lambda: _CompilerAdapter())
    spec = asyncio.run(compiler.compile_skill("multi-summary"))
    assert spec is not None and spec.name == "multi-summary-dag"
    assert spec.source_skill == "multi-summary"
    assert [n.id for n in spec.nodes] == ["q1", "q2", "sum"]
    led.close()


def test_compiler_rejects_ineligible_or_malformed(tmp_path):
    _home_dir, led = _home(tmp_path)
    _seed_usage(led, uses=10)  # 不够 50

    compiler = WorkflowCompiler(ledger=led, make_adapter=lambda: _SummarizeAdapter())
    with pytest.raises(ValueError, match="判据未满足"):
        asyncio.run(compiler.compile_skill("multi-summary"))

    _home2, led2 = _home(tmp_path)
    _seed_usage(led2)

    class _Garbage:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            return ChatResponse(content="不是 JSON", prompt_tokens=1,
                                completion_tokens=1)

    compiler2 = WorkflowCompiler(ledger=led2, make_adapter=lambda: _Garbage())
    assert asyncio.run(compiler2.compile_skill("multi-summary")) is None
    led.close(); led2.close()


def test_store_versions_and_dag_validation(tmp_path):
    _home_dir, led = _home(tmp_path)
    store = WorkflowStore(led)
    spec = WorkflowSpec.model_validate(json.loads(DAG_JSON))
    cas1 = store.commit(spec)
    cas2 = store.commit(spec)  # 第二版
    assert cas1 != cas2
    assert store.latest("multi-summary-dag").version == 2

    bad = WorkflowSpec(name="bad", entry="x", nodes=[
        WorkflowNode(id="x", kind="tool", next=["ghost"])])  # 悬空边
    with pytest.raises(ValueError, match="DAG 非法"):
        store.commit(bad)
    led.close()


# ---- 执行器 ----

def test_execute_linear_dag(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("multi-summary", SKILL, provenance={"source": "agent"},
                  branch="main")
    spec = WorkflowSpec.model_validate(json.loads(DAG_JSON))
    ex = WorkflowExecutor(ledger=led, make_adapter=lambda: _SummarizeAdapter(),
                          tools=[_SearchTool()])
    out = asyncio.run(ex.run(spec, "调研 电动车 与 固态电池"))
    assert out["outcome"] == "success" and not out["degraded_nodes"]
    assert out["results"]["q1"]["results"][0]["name"].startswith("关于第一个主题")
    assert out["results"]["sum"]["reply"] == "按技能完成汇总"
    runs = led.query("SELECT workflow, outcome FROM workflow_runs")
    assert runs and runs[0] == ("multi-summary-dag", "success")
    led.close()


def test_execute_condition_branch(tmp_path):
    _home_dir, led = _home(tmp_path)
    spec = WorkflowSpec(name="cond", entry="c", nodes=[
        WorkflowNode(id="c", kind="condition", when="results",
                     then=["hit"], els=["miss"]),
        WorkflowNode(id="hit", kind="tool", tool="web_search",
                     args={"query": "{goal}"}, next=[]),
        WorkflowNode(id="miss", kind="tool", tool="web_search",
                     args={"query": "兜底"}, next=[]),
    ])
    ex = WorkflowExecutor(ledger=led, make_adapter=lambda: _SummarizeAdapter(),
                          tools=[_SearchTool()])
    out = asyncio.run(ex.run(spec, "任意目标"))  # 无上游结果 → miss 分支
    assert "miss" in out["results"] and "hit" not in out["results"]
    led.close()


def test_human_gate_pauses(tmp_path):
    _home_dir, led = _home(tmp_path)
    spec = WorkflowSpec(name="gated", entry="g", nodes=[
        WorkflowNode(id="g", kind="human-gate", r_level=2, next=[]),
    ])
    ex = WorkflowExecutor(ledger=led, make_adapter=lambda: _SummarizeAdapter())
    out = asyncio.run(ex.run(spec, "目标"))
    assert out["outcome"] == "awaiting_review" and out["stopped_at"] == "g"
    led.close()


def test_f4_degradation_on_tool_failure(tmp_path):
    """核心出口:F.4——工具节点失败不停摆,降级为技能态执行后继续。"""
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("multi-summary", SKILL, provenance={"source": "agent"},
                  branch="main")
    spec = WorkflowSpec.model_validate(json.loads(DAG_JSON))
    _SearchTool.fail_next = True  # q1 将真实失败
    ex = WorkflowExecutor(ledger=led, make_adapter=lambda: _SummarizeAdapter(),
                          tools=[_SearchTool()])
    out = asyncio.run(ex.run(spec, "调研 电动车 与 固态电池"))
    assert out["outcome"] == "success"           # 不停摆
    assert out["degraded_nodes"] == ["q1"]       # 失败节点被降级接管
    assert out["results"]["q1"]["reply"] == "按技能完成汇总"
    assert out["results"]["q2"]["results"]       # 后续节点照常
    led.close()


def test_f4_degradation_on_subagent_budget_exhaustion(tmp_path):
    """subagent 配额耗尽 → 节点失败 → 降级接管(F.4 的配额路径)。"""
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("multi-summary", SKILL, provenance={"source": "agent"},
                  branch="main")
    spec = WorkflowSpec(name="tight", entry="sum", source_skill="multi-summary",
                        nodes=[
                            WorkflowNode(id="sum", kind="subagent",
                                         goal="汇总 {goal}", budget=100,
                                         next=[])])  # 配额 100:必失败
    ex = WorkflowExecutor(ledger=led, make_adapter=lambda: _GreedyAdapter(),
                          tools=[])
    out = asyncio.run(ex.run(spec, "调研 X 与 Y"))
    assert out["outcome"] == "success"
    assert out["degraded_nodes"] == ["sum"]
    assert out["results"]["sum"]["degraded_from"] == "sum"
    led.close()
