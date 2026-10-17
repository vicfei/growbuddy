"""Curator 保鲜进程(§6.3,M4a-2)+ 技能事件扫描 + 晋升自动触发条件。

状态机:active →(CURATOR_STALE_DAYS 未用)→ stale →(CURATOR_ARCHIVE_DAYS)
→ archived。只动 Agent 自创技能(provenance.source=agent)——人工编写与
市场安装的技能永不自动归档(§6.3 红线)。归档非删除:版本树全保留,
可从账本随时恢复(INV-2)。

触发节奏:curator_cycle_days=7 且系统空闲 curator_idle_hours=2(spec-
constants 已注册);daemon 启动时扫一次 + 周期定时,幂等可重入。

活性判定以账本为权威(投影可重建):skill.use 事件的最后时间戳;
从未使用则以 skill.commit 时间计。附:晋升自动触发(uses 达阈值)与
ReplayBundle 目标提取供管道使用。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from typing import Any, cast

from naga.ledger_store import DurableLedger
from naga.memory import MemoryStore
from naga.skills import SkillStore

CURATOR_STALE_DAYS = 30        # 主文档 §6.3 状态机(30 天未用 → stale)
CURATOR_ARCHIVE_DAYS = 90      # 90 天 → archived
SKILL_PROMOTION_TRIGGER_USES = 3  # 候选 spec-constants skill_promotion_trigger_uses


def iter_trace_records(ledger: DurableLedger) -> list[dict[str, Any]]:
    """全部 trace.record 载荷(内联小载荷 + CAS 外置大载荷,按 seq 序)。"""
    out: list[dict[str, Any]] = []
    payload: dict[str, Any] | None
    with open(ledger.head_log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("kind") != "trace.record":
                continue
            payload = obj.get("payload")
            if payload is None:
                cas = obj.get("payload_cas")
                if cas:
                    try:
                        payload = json.loads(ledger.cas.get(str(cas)))
                    except (OSError, ValueError):
                        continue
            if isinstance(payload, dict):
                out.append(payload)
    return out


def scan_skill_events(ledger: DurableLedger) -> dict[str, Any]:
    """扫 head.log 的技能域事件,返回活性/计量权威视图(个人级规模,全扫可承受)。

    返回 {last_used_by_cas, uses_by_cas, committed_ts_by(name,branch)}。
    """
    last_used: dict[str, str] = {}
    uses: dict[str, int] = {}
    committed: dict[tuple[str, str], str] = {}
    with open(ledger.head_log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("kind")
            payload = cast("dict[str, Any]", obj.get("payload") or {})
            if kind == "skill.use":
                cas = str(payload.get("cas_id") or "")
                if cas:
                    last_used[cas] = str(obj.get("ts") or "")
                    uses[cas] = uses.get(cas, 0) + 1
            elif kind == "skill.commit":
                committed[(str(payload.get("name")), str(payload.get("branch")))] = \
                    str(obj.get("ts") or "")
    return {"last_used": last_used, "uses": uses, "committed": committed}


def _parse_ts(raw: str) -> datetime.datetime | None:
    """head.log 的 ts 为 float 纪元(append 默认),投影层才是 ISO——两者兼容。"""
    if not raw:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(raw), tz=datetime.UTC)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass
class CuratorReport:
    scanned: int = 0
    to_stale: list[str] = field(default_factory=list[str])
    to_archived: list[str] = field(default_factory=list[str])
    skipped_non_agent: int = 0
    memory_stale: list[str] = field(default_factory=list[str])
    memory_archived: list[str] = field(default_factory=list[str])
    consolidated: int = 0
    escrow_destroyed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.to_stale or self.to_archived or self.memory_stale
                    or self.memory_archived or self.consolidated)


class Curator:
    """保鲜进程:按状态机迁移 agent 自创技能;幂等(已迁移者不再动)。

    记忆域(§6.5):同样的状态机(active→stale→archived,时间依据=
    memory_items.last_used_at,DM B.3 R6-B10)+ 巩固(情景高频复现 →
    提议晋升语义)+ 托管到期销毁(K.6)。memory=None 时跳过记忆域
    (旧装配兼容)。"""

    def __init__(self, *, ledger: DurableLedger, skills: SkillStore,
                 memory: MemoryStore | None = None,
                 now: datetime.datetime | None = None):
        self._ledger = ledger
        self._skills = skills
        self._memory = memory
        self._now = now or datetime.datetime.now(datetime.UTC)

    def sweep(self) -> CuratorReport:
        events = scan_skill_events(self._ledger)
        report = CuratorReport()
        rows = self._ledger.query(
            "SELECT s.branch, s.name, s.cas_id, s.status, s.provenance "
            "FROM skill_nodes s JOIN (SELECT branch, name, MAX(version) AS v "
            "FROM skill_nodes GROUP BY branch, name) m "
            "ON s.name=m.name AND s.version=m.v AND s.branch=m.branch")
        for branch, name, cas, status, provenance_json in rows:
            report.scanned += 1
            try:
                source = json.loads(provenance_json).get("source")
            except ValueError:
                source = None
            if source != "agent":
                report.skipped_non_agent += 1  # §6.3:人工/市场永不自动归档
                continue
            if status not in ("active", "stale"):
                continue  # archived/quarantined 终态或隔离:不动
            last_ts = (events["last_used"].get(cas)
                       or events["committed"].get((name, branch)) or "")
            if not last_ts:
                continue
            last = _parse_ts(last_ts)
            if last is None:
                continue
            idle_days = (self._now - last).days
            label = f"{branch}/{name}"
            if status == "active" and idle_days > CURATOR_STALE_DAYS:
                self._skills.set_status(name, "stale", branch=branch,
                                        reason=f"{idle_days} 天未用")
                report.to_stale.append(label)
            elif status == "stale" and idle_days > CURATOR_ARCHIVE_DAYS:
                self._skills.set_status(name, "archived", branch=branch,
                                        reason=f"{idle_days} 天未用")
                report.to_archived.append(label)
        if self._memory is not None:
            self._sweep_memory(report)
        return report

    def _sweep_memory(self, report: CuratorReport) -> None:
        """记忆域巡检:保鲜状态机 + 巩固提议 + 托管到期销毁(K.6)。"""
        assert self._memory is not None
        for cas, status, last_used in self._ledger.query(
                "SELECT cas_id, status, last_used_at FROM memory_items "
                "WHERE status IN ('active','stale')"):
            last = _parse_ts(str(last_used))
            if last is None:
                continue
            idle_days = (self._now - last).days
            label = f"{cas[:18]}…"
            if status == "active" and idle_days > CURATOR_STALE_DAYS:
                self._memory.set_status(str(cas), "stale",
                                        reason=f"{idle_days} 天未用")
                report.memory_stale.append(label)
            elif status == "stale" and idle_days > CURATOR_ARCHIVE_DAYS:
                self._memory.set_status(str(cas), "archived",
                                        reason=f"{idle_days} 天未用")
                report.memory_archived.append(label)
        for candidate in self._memory.consolidation_candidates():
            if self._memory.consolidate(candidate) is not None:
                report.consolidated += 1
        report.escrow_destroyed = len(self._memory.destroy_expired_escrow())
