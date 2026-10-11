"""M4a-2 tests: Curator state machine (§6.3), non-agent protection, INV-2
recovery, ReplayBundle goal sourcing, and the auto-promotion trigger."""

import asyncio
import datetime

from naga import kernel, storage  # noqa: F401
from naga.curator import Curator, scan_skill_events
from naga.ledger_store import DurableLedger
from naga.llm import ChatResponse
from naga.promotion import (
    PromotionPipeline,
    ShadowVerifier,
    auto_trigger_candidates,
    goals_from_bundles,
)
from naga.skills import SkillStore

GOOD_SKILL = """---
name: sk
---
## 何时使用
触发词命中时。
## 步骤
按流程办。
## 已知陷阱
无。
"""


def _home(tmp_path) -> tuple[str, DurableLedger]:
    home = str(tmp_path / "h")
    storage.init_home(home)
    return home, DurableLedger(home)


# ---- Curator 状态机 ----

def test_curator_active_stale_archived(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("sk", GOOD_SKILL, provenance={"source": "agent"},
                  branch="local:s1")
    now = datetime.datetime.now(datetime.UTC)

    fresh = Curator(ledger=led, skills=skills,
                    now=now + datetime.timedelta(days=1)).sweep()
    assert not fresh.changed  # 新技能保持 active

    staled = Curator(ledger=led, skills=skills,
                     now=now + datetime.timedelta(days=31)).sweep()
    assert staled.to_stale == ["local:s1/sk"]
    assert skills.get("sk", branch="local:s1") is None          # stale 不在 active 视图
    assert skills.get("sk", branch="local:s1", status=None) is not None  # 版本仍在(INV-2)

    archived = Curator(ledger=led, skills=skills,
                       now=now + datetime.timedelta(days=91)).sweep()
    assert archived.to_archived == ["local:s1/sk"]
    rows = led.query("SELECT kind FROM ledger_entries WHERE kind LIKE 'skill.%' "
                     "AND kind != 'skill.commit' ORDER BY seq")
    assert [r[0] for r in rows] == ["skill.stale", "skill.archived"]  # 迁移入账
    led.close()


def test_curator_never_touches_non_agent_skills(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("human-skill", GOOD_SKILL.replace("name: sk", "name: human-skill"),
                  provenance={"source": "human"}, branch="main")
    skills.commit("hub-skill", GOOD_SKILL.replace("name: sk", "name: hub-skill"),
                  provenance={"source": "hub"}, branch="main")
    now = datetime.datetime.now(datetime.UTC)
    report = Curator(ledger=led, skills=skills,
                     now=now + datetime.timedelta(days=400)).sweep()
    assert report.skipped_non_agent == 2 and not report.changed  # §6.3 红线
    led.close()


def test_curator_recent_use_keeps_active(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("sk", GOOD_SKILL, provenance={"source": "agent"},
                  branch="local:s1")
    cas = led.query("SELECT cas_id FROM skill_nodes WHERE name='sk'")[0][0]
    # 记一条近期使用(31 天前的世界,但昨天刚用过)
    now = datetime.datetime.now(datetime.UTC)
    led.append("skill.use", "root", {"cas_id": cas})
    report = Curator(ledger=led, skills=skills,
                     now=now + datetime.timedelta(days=1)).sweep()
    assert not report.changed  # 用得勤:不迁 stale
    led.close()


def test_curator_idempotent_rerun(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("sk", GOOD_SKILL, provenance={"source": "agent"},
                  branch="local:s1")
    now = datetime.datetime.now(datetime.UTC)
    far = Curator(ledger=led, skills=skills, now=now + datetime.timedelta(days=40))
    assert far.sweep().to_stale
    again = Curator(ledger=led, skills=skills, now=now + datetime.timedelta(days=40))
    assert not again.sweep().changed  # active→stale 只迁一次(40<90 不越档)
    led.close()


# ---- 事件扫描 + ReplayBundle 取目标 ----

def test_scan_skill_events_counts(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("sk", GOOD_SKILL, provenance={"source": "agent"},
                  branch="local:s1")
    cas = led.query("SELECT cas_id FROM skill_nodes WHERE name='sk'")[0][0]
    led.append("skill.use", "root", {"cas_id": cas})
    led.append("skill.use", "root", {"cas_id": cas})
    events = scan_skill_events(led)
    assert events["uses"][cas] == 2
    assert ("sk", "local:s1") in events["committed"]
    led.close()


def test_goals_from_bundles(tmp_path):
    _home_dir, led = _home(tmp_path)
    from naga.trace import TraceRecorder

    for goal in ("任务甲:调研 X", "任务乙:总结 Y"):
        rec = TraceRecorder(led, trace_id="tr_" + goal[-1],
                            session_key="wechat:dm:u1", inbound={"text": goal})
        rec.finish(outcome="success", cost_tokens=10)
    goals = goals_from_bundles(led, limit=2)
    assert goals == ["任务乙:总结 Y", "任务甲:调研 X"]  # 最近优先
    assert goals_from_bundles(led, session_key="wechat:dm:other") == []
    led.close()


# ---- 晋升自动触发 ----

def test_auto_trigger_candidates_threshold(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    skills.commit("hot", GOOD_SKILL.replace("name: sk", "name: hot"),
                  provenance={"source": "agent"}, branch="local:s1")
    skills.commit("cold", GOOD_SKILL.replace("name: sk", "name: cold"),
                  provenance={"source": "agent"}, branch="local:s1")
    hot_cas = led.query("SELECT cas_id FROM skill_nodes WHERE name='hot'")[0][0]
    for _ in range(3):
        led.append("skill.use", "root", {"cas_id": hot_cas})
    assert auto_trigger_candidates(led, "s1") == [("hot", 3)]  # 阈值外不动
    led.close()


def test_auto_trigger_excludes_main_and_pending(tmp_path):
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    named = GOOD_SKILL.replace("name: sk", "name: promoted")
    skills.commit("promoted", named, provenance={"source": "agent"},
                  branch="local:s1")
    skills.commit("promoted", named, provenance={"source": "agent"},
                  branch="main")
    # 同内容跨分支同 CAS(内容寻址):投影行归 main——名字在 main 即不再触发
    cas = led.query("SELECT cas_id FROM skill_nodes "
                    "WHERE name='promoted'")[0][0]
    for _ in range(5):
        led.append("skill.use", "root", {"cas_id": cas})
    assert auto_trigger_candidates(led, "s1") == []

    # pending 人审队列同样排除
    skills.commit("pending-skill",
                  GOOD_SKILL.replace("name: sk", "name: pending-skill"),
                  provenance={"source": "agent"}, branch="local:s1")
    pcas = led.query("SELECT cas_id FROM skill_nodes "
                     "WHERE name='pending-skill'")[0][0]
    for _ in range(4):
        led.append("skill.use", "root", {"cas_id": pcas})
    led.execute(
        "INSERT INTO promotion_requests (name, from_branch, r_level, status, "
        "shadow_report, requested_at) VALUES ('pending-skill','local:s1',2,"
        "'pending','{}','t')")
    assert auto_trigger_candidates(led, "s1") == []
    led.close()


def test_full_auto_promotion_flow(tmp_path):
    """任务后钩子全链:uses 达阈值 → bundle 取目标 → 影子对打 → R1 入 main。"""
    _home_dir, led = _home(tmp_path)
    skills = SkillStore(led)
    marker_skill = GOOD_SKILL.replace("触发词命中时。", "候选标记命中时执行。"
                                      ).replace("name: sk", "name: auto-me")
    skills.commit("auto-me", marker_skill, provenance={"source": "agent"},
                  branch="local:s1")
    cas = led.query("SELECT cas_id FROM skill_nodes WHERE name='auto-me'")[0][0]
    for _ in range(3):
        led.append("skill.use", "root", {"cas_id": cas})

    from naga.trace import TraceRecorder
    rec = TraceRecorder(led, trace_id="tr_seed", session_key="s1",
                        inbound={"text": "候选标记相关的任务"})
    rec.finish(outcome="success", cost_tokens=5)

    class _MarkerAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            system = messages[0]["content"] if messages else ""
            if "候选标记" in system:
                return ChatResponse(content="好答案", prompt_tokens=5,
                                    completion_tokens=5)
            raise RuntimeError("基线失败")

    pipe = PromotionPipeline(
        ledger=led,
        shadow=ShadowVerifier(ledger=led, make_adapter=lambda: _MarkerAdapter()))

    async def scenario():
        candidates = auto_trigger_candidates(led, "s1")
        assert candidates == [("auto-me", 3)]
        goals = goals_from_bundles(led, session_key="s1")
        assert goals and "候选标记" in goals[0]
        return await pipe.promote(candidates[0][0], from_session="s1", goals=goals)

    result = asyncio.run(scenario())
    assert result.status == "promoted"
    assert SkillStore(led).get("auto-me", branch="main") is not None
    led.close()
