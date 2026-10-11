"""晋升管道 + 影子验证器 + 体检——L2 资产进化的核心闸(§6.4/§7.4,M4a-1)。

默认隔离,晋升需证:局部技能(local:{session} 分支)须经
  ① 体检:格式(必需节)/尺寸(15KB)/危险指令扫描(§6.4 示例:"跳过确认"
     "禁用审批"类模式一律拒绝——对抗用例在测试钉死)
  ② 影子验证:影子分支回放历史任务,新旧对打(成功率/成本;候选不得更差)
  ③ R 级路由:纯知识/流程指导=R1 自动合并入 main(skill.promote 入账,
     frozen 枚举在位);含写类/破坏性操作指导=R2 入 promotion_request
     待 owner 人审(§6.4 表:R2 测试套件+人审;§7.3 二维授权)
跑输或不达标 → 留在局部,全局零污染。

影子运行卫生:候选以 forced_skills 注入(不受检索命中约束);meter_skills
关闭(非真实使用不入 skill.use 计量);写沉降进影子分支(挂一个永不结算
的父分支之下,子分支保持 open——裁决权属父,父不结算即永不物化)。
"""

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from naga import kernel
from naga.agent import AgentLoop
from naga.branch import SessionBranch
from naga.ledger_store import DurableLedger
from naga.llm import ModelAdapter
from naga.skills import SKILL_MAX_BYTES, SkillStore

# 体检:危险指令模式(§6.4 示例形态;对抗样本在测试逐条钉死)
DANGEROUS_PATTERNS: tuple[str, ...] = (
    "跳过确认", "跳过审批", "禁用审批", "禁用确认", "绕过确认", "绕过审批",
    "无需人工", "无需确认", "无需审批", "跳过安全", "skip confirmation",
    "disable approval", "bypass approval", "ignore approval",
    "without human review",
)
REQUIRED_SECTIONS: tuple[str, ...] = ("何时使用", "步骤")
# R 级判定:写类/破坏性操作指导 → R2(人审档);纯知识 → R1(自动合并档)
WRITE_GUIDANCE_PATTERNS: tuple[str, ...] = (
    "send_mail", "发送邮件", "删除文件", "写入文件", "fs.write", "shell",
    "执行命令", "rm -rf", "db.drop", "直接写入",
)
SHADOW_VERIFY_GOALS = 3        # 候选 spec-constants shadow_verify_goals
SHADOW_BUDGET_PER_RUN = 20_000  # 候选 shadow_budget_per_run(防影子跑飞)
SHADOW_COST_TOLERANCE = 1.2    # 候选 spec-constants shadow_cost_tolerance(成本容差)


@dataclass
class HealthReport:
    ok: bool
    reasons: list[str] = field(default_factory=list[str])


def _default_notify(msg: str) -> None:
    print(f"[promotion] {msg}", flush=True)


@dataclass
class ShadowReport:
    verdict: str          # win | tie | lose
    reason: str
    goals: int = 0
    base_success: int = 0
    cand_success: int = 0
    base_tokens: int = 0
    cand_tokens: int = 0

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


@dataclass
class PromotionResult:
    status: str           # promoted | pending_review | rejected
    name: str
    r_level: int = 1
    reasons: list[str] = field(default_factory=list[str])
    shadow: ShadowReport | None = None


def health_check(content: str) -> HealthReport:
    """① 体检:格式(必需节)/尺寸/危险指令扫描。"""
    reasons: list[str] = []
    if len(content.encode("utf-8")) > SKILL_MAX_BYTES:
        reasons.append(f"超尺寸上限 {SKILL_MAX_BYTES}B")
    for section in REQUIRED_SECTIONS:
        if section not in content:
            reasons.append(f"缺必需节「## {section}」")
    lowered = content.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in lowered:
            reasons.append(f"危险指令模式: {pattern!r}")
            break
    return HealthReport(ok=not reasons, reasons=reasons)


def skill_r_level(content: str) -> int:
    """③ R 级路由的声明式判定:写类/破坏性操作指导 → R2,纯知识 → R1。"""
    lowered = content.lower()
    if any(p in lowered for p in WRITE_GUIDANCE_PATTERNS):
        return 2
    return 1


class ShadowVerifier:
    """② 影子验证(§7.4):影子分支回放历史任务,新旧对打。

    裁决指标:任务成功率(硬指标,候选不得更低)与成本(容差内)。
    回放目标由调用方给出(沉淀任务同类目标;自动化取数属后续批次)。
    """

    def __init__(self, *, ledger: DurableLedger,
                 make_adapter: Callable[[], ModelAdapter],
                 tools: list[Any] | None = None,
                 budget_per_run: int = SHADOW_BUDGET_PER_RUN):
        self._ledger = ledger
        self._make_adapter = make_adapter
        self._tools = tools or []
        self._budget_per_run = budget_per_run
        # 影子父分支:永不结算 → 其子分支(每 run 一个)永不物化(§7.4 跑输/跑完
        # 均自动卸载干净;M3-2 级联语义承载)
        self._shadow_root = SessionBranch(
            owner_session="shadow:root", base_get=lambda k: None,
            base_has=lambda k: False, cas_put=ledger.cas.put)

    async def verify(self, candidate_uri: str, candidate_content: str,
                     goals: list[str]) -> ShadowReport:
        if not goals:
            return ShadowReport(verdict="lose", reason="无回放目标(历史为空)")
        base_success = cand_success = base_tokens = cand_tokens = 0
        for goal in goals:
            base = await self._run(goal, forced=None)
            cand = await self._run(goal, forced=[(candidate_uri, candidate_content)])
            base_success += 1 if base.outcome == "success" else 0
            cand_success += 1 if cand.outcome == "success" else 0
            base_tokens += base.tokens
            cand_tokens += cand.tokens
        report = ShadowReport(
            goals=len(goals), base_success=base_success, cand_success=cand_success,
            base_tokens=base_tokens, cand_tokens=cand_tokens, verdict="", reason="")
        if cand_success < base_success:
            report.verdict = "lose"
            report.reason = (f"成功率回退: 候选 {cand_success}/{len(goals)}"
                             f" < 基线 {base_success}/{len(goals)}")
        elif cand_tokens > base_tokens * SHADOW_COST_TOLERANCE and base_tokens > 0:
            report.verdict = "lose"
            report.reason = (f"成本超容差: 候选 {cand_tokens} tokens > "
                             f"基线 {base_tokens}×{SHADOW_COST_TOLERANCE}")
        elif cand_success > base_success:
            report.verdict = "win"
            report.reason = f"成功率提升 {base_success}→{cand_success}"
        else:
            report.verdict = "tie"
            report.reason = (f"持平(成功 {cand_success}/{len(goals)},"
                             f"成本 {base_tokens}→{cand_tokens})")
        return report

    async def _run(self, goal: str,
                   forced: list[tuple[str, str]] | None) -> Any:
        """单次影子回放:候选强制注入、不计量、写沉降进 open 子分支(永不物化)。"""
        import uuid

        kern = kernel.Kernel(ledger=self._ledger)  # type: ignore[arg-type]
        shadow_branch = self._shadow_root.spawn_child("shadow:run")
        loop = AgentLoop(
            ledger=self._ledger, ctx=kern.root,
            budget=kernel.Budget(self._budget_per_run),
            adapter=self._make_adapter(), tools=self._tools,
            skill_store=SkillStore(self._ledger),
            session_key="shadow:verify",
            trace_id="tr_sh_" + uuid.uuid4().hex[:20],
            forced_skills=forced, meter_skills=False, branch=shadow_branch)
        try:
            return await loop.run(goal)
        except Exception:  # noqa: BLE001 —— 影子回放:任务级异常计为该次失败
            from naga.agent import AgentResult
            return AgentResult(reply=None, outcome="aborted",
                               trace_id="tr_sh_failed", steps=0, tokens=0,
                               cost_cents=0, failure_analysis="影子回放异常")


class PromotionPipeline:
    """局部技能 → 体检 → 影子对打 → R 级路由 → main(或人审队列)。"""

    def __init__(self, *, ledger: DurableLedger,
                 shadow: ShadowVerifier,
                 owner_notify: Callable[[str], None] | None = None):
        self._ledger = ledger
        self._skills = SkillStore(ledger)
        self._shadow = shadow
        self._notify: Callable[[str], None] = owner_notify or _default_notify

    async def promote(self, name: str, *, from_session: str,
                      goals: list[str]) -> PromotionResult:
        local_branch = f"local:{from_session}"
        got = self._skills.get(name, branch=local_branch, status=None)
        if got is None:
            return PromotionResult(status="rejected", name=name,
                                   reasons=[f"局部分支无此技能: {local_branch}/{name}"])
        _meta, content = got

        health = health_check(content)  # ① 体检
        if not health.ok:
            return PromotionResult(status="rejected", name=name,
                                   reasons=health.reasons)

        report = await self._shadow.verify(  # ② 影子对打
            f"skill://{name}", content, goals[:SHADOW_VERIFY_GOALS])
        if report.verdict == "lose":
            return PromotionResult(status="rejected", name=name,
                                   reasons=["影子验证未通过: " + report.reason],
                                   shadow=report)

        r_level = skill_r_level(content)  # ③ R 级路由
        if r_level >= 2:  # R2+:入人审队列(§6.4);不动 main
            self._ledger.execute(
                "INSERT INTO promotion_requests "
                "(name, from_branch, r_level, status, shadow_report, requested_at) "
                "VALUES (?,?,?,?,?,?)",
                (name, local_branch, r_level, "pending", report.to_json(),
                 datetime.datetime.now(datetime.UTC).isoformat()))
            self._notify(f"{name} 影子验证 {report.verdict}(R{r_level}),"
                         f"入人审队列待 owner 批准")
            return PromotionResult(status="pending_review", name=name,
                                   r_level=r_level, shadow=report)

        self._skills.commit(name, content, branch="main",
                            provenance={"source": "agent",
                                        "promoted_from": local_branch})
        self._ledger.append("skill.promote", "root",
                            {"name": name, "from_branch": local_branch,
                             "r_level": r_level,
                             "shadow": {"verdict": report.verdict,
                                        "goals": report.goals,
                                        "cand_success": report.cand_success,
                                        "base_success": report.base_success}})
        self._notify(f"{name} 晋升 main(R{r_level},影子 {report.verdict})")
        return PromotionResult(status="promoted", name=name,
                               r_level=r_level, shadow=report)


def goals_from_bundles(ledger: DurableLedger, *, limit: int = 3,
                       session_key: str | None = None) -> list[str]:
    """从最近的 trace.record ReplayBundle 提取回放目标(§7.4"回放真实历史
    轨迹"的字面化):bundle.session.inbound.text 即当年任务目标(已脱敏)。"""
    goals: list[str] = []
    with open(ledger.head_log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for line in reversed(lines):  # 最近优先
        if len(goals) >= limit:
            break
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("kind") != "trace.record":
            continue
        payload = cast("dict[str, Any]", obj.get("payload") or {})
        if session_key and payload.get("session_key") != session_key:
            continue
        cas = payload.get("replay_bundle_cas")
        if not cas:
            continue
        try:
            bundle = json.loads(ledger.cas.get(str(cas)))
            text = bundle.get("session", {}).get("inbound", {}).get("text")
        except (OSError, ValueError):
            continue
        if isinstance(text, str) and text.strip():
            goals.append(text.strip())
    return goals


def auto_trigger_candidates(ledger: DurableLedger, session_key: str,
                            trigger_uses: int = 3) -> list[tuple[str, int]]:
    """晋升自动触发条件:该会话局部分支上,uses 达阈值且未入 main/pending
    的技能(返回 (name, uses);候选 spec-constants skill_promotion_trigger_uses)。"""
    from naga.curator import scan_skill_events

    events = scan_skill_events(ledger)
    local_branch = f"local:{session_key}"
    candidates: list[tuple[str, int]] = []
    rows = ledger.query(
        "SELECT s.name, s.cas_id FROM skill_nodes s JOIN "
        "(SELECT name, MAX(version) AS v FROM skill_nodes WHERE branch=? "
        " GROUP BY name) m ON s.name=m.name AND s.version=m.v AND s.branch=?",
        (local_branch, local_branch))
    in_main = {r[0] for r in ledger.query("SELECT name FROM skill_nodes "
                                          "WHERE branch='main'")}
    pending = {r[0] for r in ledger.query(
        "SELECT name FROM promotion_requests WHERE status='pending'")}
    for name, cas in rows:
        if name in in_main or name in pending:
            continue
        uses = events["uses"].get(cas, 0)
        if uses >= trigger_uses:
            candidates.append((name, uses))
    return candidates
