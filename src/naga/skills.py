"""最小 L2:技能资产(主文档 §6.1/§6.2,数据模型 B.1)。

技能 = CAS 中的 SKILL.md 文本(frontmatter + 正文);一切写入经账本
skill.commit / skill.quarantine 入账,skill_nodes 为可重建投影(数据模型 §7)。
尺寸硬限 skill_max_bytes=15360(spec-constants);版本树:同 (name, branch)
version 单调递增,parent_cas 指向上一版本节点。
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from naga.ledger_store import DurableLedger

SKILL_MAX_BYTES = 15360  # spec-constants skill_max_bytes / 数据模型 §4 B.1 CHECK


class SkillMeta(BaseModel):
    """SKILL.md frontmatter(协议边界,主文档 §6.1)。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = 0  # 0 = 未指定,由 store 分配
    scope: str = "global"
    status: str = "active"
    provenance: dict[str, Any]
    metrics: dict[str, Any] | None = None


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise ValueError("SKILL.md 必须以 frontmatter(---)开头")
    end = content.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter 未闭合")
    raw: dict[str, Any] | None = yaml.safe_load(content[4:end])
    if not isinstance(raw, dict):
        raise ValueError("SKILL.md frontmatter 必须是映射")  # noqa: TRY004 —— 边界内容校验语义,非类型误用
    body = content[end + len("\n---\n"):]
    return raw, body


def _render(meta: SkillMeta, body: str) -> str:
    fm: dict[str, Any] = {
        "name": meta.name,
        "version": meta.version,
        "scope": meta.scope,
        "status": meta.status,
        "provenance": meta.provenance,
        "metrics": meta.metrics or {"uses": 0},
    }
    return "---\n" + yaml.safe_dump(fm, sort_keys=True, allow_unicode=True) + "---\n" + body


class SkillStore:
    """技能读写:校验 → 盖版本戳 → CAS → 账本;查询走投影。"""

    def __init__(self, ledger: DurableLedger):
        self._ledger = ledger

    def commit(self, name: str, content: str, provenance: dict[str, Any], *,
               actor: str = "root", branch: str = "main",
               metrics: dict[str, Any] | None = None) -> str:
        fm, body = _split_frontmatter(content)
        if fm.get("name") not in (None, name):
            raise ValueError(f"frontmatter name {fm.get('name')!r} 与请求 {name!r} 不符")
        rows = self._ledger.query(
            "SELECT version, cas_id FROM skill_nodes WHERE name=? AND branch=? "
            "ORDER BY version DESC LIMIT 1", (name, branch))
        version = (rows[0][0] + 1) if rows else 1
        parent_cas: str | None = rows[0][1] if rows else None
        meta = SkillMeta.model_validate({
            **fm, "name": name, "version": version, "scope": fm.get("scope", "global"),
            "status": "active", "provenance": provenance, "metrics": metrics,
        })
        stamped = _render(meta, body)
        data = stamped.encode("utf-8")
        if len(data) > SKILL_MAX_BYTES:
            raise ValueError(f"技能 {len(data)} 字节超上限 {SKILL_MAX_BYTES}")
        cas = self._ledger.cas.put(data)
        self._ledger.append("skill.commit", actor, {
            "cas_id": cas, "name": name, "version": version, "parent_cas": parent_cas,
            "branch": branch, "status": "active",
            "provenance": _canon(provenance),
            "metrics_json": _canon(metrics or {"uses": 0}),
            "size_bytes": len(data),
        })
        return cas

    def _latest_row(self, name: str, branch: str, status: str | None) -> tuple[Any, ...] | None:
        sql = ("SELECT cas_id, version, status, size_bytes, provenance, metrics_json "
               "FROM skill_nodes WHERE name=? AND branch=?")
        params: tuple[Any, ...] = (name, branch)
        if status is not None:
            sql += " AND status=?"
            params = (name, branch, status)
        sql += " ORDER BY version DESC LIMIT 1"
        rows = self._ledger.query(sql, params)
        return rows[0] if rows else None

    def get(self, name: str, *, branch: str = "main",
            status: str | None = "active") -> tuple[SkillMeta, str] | None:
        row = self._latest_row(name, branch, status)
        if row is None:
            return None
        content = self._ledger.cas.get(row[0]).decode("utf-8")
        fm, _ = _split_frontmatter(content)
        return SkillMeta.model_validate(fm), content

    def list(self, *, branch: str = "main", status: str = "active") -> list[tuple[Any, ...]]:
        """每 name 取最新版本的 (cas_id, name, version, status, size_bytes)。"""
        return self._ledger.query(
            "SELECT s.cas_id, s.name, s.version, s.status, s.size_bytes FROM skill_nodes s "
            "JOIN (SELECT name, MAX(version) AS v FROM skill_nodes "
            "      WHERE branch=? AND status=? GROUP BY name) m "
            "ON s.name=m.name AND s.version=m.v AND s.branch=? AND s.status=? "
            "ORDER BY s.name", (branch, status, branch, status))

    def set_status(self, name: str, status: str, *, branch: str = "main",
                   reason: str = "", actor: str = "root") -> bool:
        """状态迁移(active/stale/archived;Curator §6.3)。版本树全保留——
        归档非删除,可恢复(INV-2);迁移事件入账(投影可重建)。"""
        if status not in ("active", "stale", "archived"):
            raise ValueError(f"非法状态: {status}")
        row = self._latest_row(name, branch, None)
        if row is None:
            return False
        self._ledger.execute(
            "UPDATE skill_nodes SET status=? WHERE cas_id=?", (status, row[0]))
        self._ledger.append(f"skill.{status}", actor,
                            {"cas_id": row[0], "name": name,
                             "branch": branch, "reason": reason})
        return True

    def quarantine(self, name: str, reason: str, *, actor: str = "root",
                   branch: str = "main") -> str | None:
        row = self._latest_row(name, branch, None)
        if row is None:
            return None
        self._ledger.append("skill.quarantine", actor,
                            {"cas_id": row[0], "name": name, "reason": reason})
        return row[0]


def _canon(obj: Any) -> str:
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
