"""自省挂载(#95):agent 看见并操作自己的运行时(设计哲学的最后一寸)。

设计本意(主文档附录 C.1"对执行层透明"≠不可见;L3 元对象清单):
agent 的世界观 = 工具箱;调度器/任务/推送/补发队列早已是可审视的
投影与状态文件,缺的只是挂载。本模块提供四件挂载:

- system_status(R0):读现成投影与状态——活跃任务+最近触发、补发积压、
  推送三态(经 daemon 投递日志 state/notify-log.json,封顶环形)、
  会话/记忆/技能清单计数。按 §10 矩阵过滤:guest 不见 ops 细节。
- job_create / job_pause / job_delete(R1):任务操作挂载,每任务实例化
  绑定当前会话(creator)与信任快照;自然语言走既有单次 LLM 编译路径。
  guest 经 tools_for_trust 的 R0 封顶天然不可见。

推送三态持久化为独立状态文件而非新 kind(候选 additive kind 记
研究文档 E'''')。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, ClassVar, cast

from naga.jobs import JobStore, compile_natural_language, scan_job_events
from naga.notify import load as load_pending

DELIVERY_LOG_MAX = 50  # 投递日志封顶(环形)


def record_delivery(path: str, session_key: str, text: str,
                    outcome: str, detail: str = "") -> None:
    """daemon 每次推送尝试后记一行三态(delivered/refused/queued)。"""
    try:
        entries: list[dict[str, Any]] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw: object = json.load(fh)
            if isinstance(raw, list):
                entries = cast("list[dict[str, Any]]", raw)
    except (OSError, ValueError):
        entries = []
    entry: dict[str, Any] = {
        "session_key": session_key, "text": text[:80],
        "outcome": outcome, "detail": detail[:60],
        "ts": datetime.datetime.now(datetime.UTC).isoformat()}
    entries.append(entry)
    del entries[:-DELIVERY_LOG_MAX]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(entries, ensure_ascii=False))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read_delivery_log(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw: object = json.load(fh)
        return cast("list[dict[str, Any]]", raw) if isinstance(raw, list) else []
    except (OSError, ValueError):
        return []


class SystemStatusTool:
    """R0 只读自省:读投影与状态文件,零新 kind;按信任过滤 ops 细节。"""

    name = "system_status"
    description = ("查询本运行时的实时状态:定时任务与最近触发、通知投递三态"
                   "(送达/被拒入队/已补发)、补发积压、会话/记忆/技能概况。"
                   "凡用户问到'你有没有定时器''为什么没推送''你现在能做什么'"
                   "类问题,先调用本工具再回答。")
    parameters: ClassVar[dict[str, Any]] = {"type": "object",
                                            "properties": {}}
    risk_level = 0

    def __init__(self, ledger: Any, home: str, actor_trust: str | None = None):
        self._ledger = ledger
        self._home = home
        self._trust = actor_trust or "owner"
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(args))
        out: dict[str, Any] = {
            "memory_items": [list(r) for r in self._ledger.query(
                "SELECT layer, COUNT(*) FROM memory_items "
                "WHERE status='active' GROUP BY layer")],
            "skills": self._ledger.query(
                "SELECT COUNT(*) FROM skill_nodes WHERE status='active' "
                "AND branch='main'")[0][0],
            "sessions_open": self._ledger.query(
                "SELECT COUNT(*) FROM sessions WHERE state='open'")[0][0],
        }
        if self._trust in ("owner", "known"):  # §10 矩阵:ops 细节 known+
            events = scan_job_events(self._ledger)
            jobs: list[dict[str, Any]] = []
            for row in JobStore(self._ledger).list_jobs(state="active"):
                slot = events.get(row["id"], {})
                entry: dict[str, Any] = {
                    "id": row["id"], "cron": row["schedule"],
                    "timezone": row["timezone"],
                    "last_fire": slot.get("last_fire")}
                jobs.append(entry)
            pending = load_pending(
                os.path.join(self._home, "state", "pending-notifies.json"))
            deliveries = _read_delivery_log(
                os.path.join(self._home, "state", "notify-log.json"))[-8:]
            out["jobs_active"] = jobs
            out["notify_pending"] = {k: len(v) for k, v in pending.items()}
            out["recent_deliveries"] = deliveries
        else:
            out["jobs_active"] = "requires known+ (10-matrix)"
        return out


class _JobToolBase:
    risk_level = 1  # §10 矩阵:job.create known+;guest 经 R0 封顶不可见

    def __init__(self, ledger: Any, session_key: str, actor_trust: str | None):
        self._ledger = ledger
        self._session_key = session_key
        self._trust = actor_trust or "owner"


class JobCreateTool(_JobToolBase):
    name = "job_create"
    description = ("为用户创建定时任务(自然语言或 cron+goal)。凡用户表达"
                   "'每天/每周/定时/到点提醒我'类需求,直接调用本工具建任务,"
                   "不要声称无法定时。返回编译后的 cron 与预估频次。")
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "自然语言需求,如'每天早上七点给我科技新闻摘要'"},
            "cron": {"type": "string", "description": "已是合法 cron 时可直传"},
            "goal": {"type": "string", "description": "cron 直传时的任务目标"},
        },
    }

    def __init__(self, ledger: Any, session_key: str, actor_trust: str | None,
                 make_adapter: Any = None):
        super().__init__(ledger, session_key, actor_trust)
        self._make_adapter = make_adapter
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(args))
        channel = self._session_key.split(":", 1)[0]
        peer = self._session_key.split(":dm:", 1)[1] \
            if ":dm:" in self._session_key else ""
        try:
            if str(args.get("cron") or "").strip():
                from naga.jobs import JobSpec
                spec = JobSpec(
                    job_id="job_" + __import__("uuid").uuid4().hex[:20],
                    schedule=str(args["cron"]).strip(),
                    timezone="Asia/Shanghai",
                    goal=str(args.get("goal") or "").strip() or "(未提供目标)",
                    trust_snapshot=self._trust,
                    budget={"tokens": 200_000, "wall_ms": 1_800_000},
                    r_ceiling="R1",
                    creator_channel=channel, creator_peer=peer)
            else:
                text = str(args.get("text") or "").strip()
                if not text:
                    return {"error": "需要 text(自然语言)或 cron+goal"}
                if self._make_adapter is None:
                    return {"error": "自然语言编译不可用,请直传 cron+goal"}
                spec = await compile_natural_language(
                    self._make_adapter(), text)
                spec.trust_snapshot = self._trust
                spec.creator_channel = channel
                spec.creator_peer = peer
            jid = JobStore(self._ledger).create(spec, actor=self._session_key)
        except ValueError as err:
            return {"error": f"JOBS.INVALID: {err}"}
        from naga.jobs import fires_per_month
        return {"job_id": jid, "cron": spec.schedule, "goal": spec.goal,
                "timezone": spec.timezone,
                "fires_per_month": fires_per_month(spec.schedule,
                                                   spec.timezone),
                "note": "已创建,到点自动执行;结果推送受平台窗口限制,"
                        "窗口外将排队并在用户下次开口时先达"}


class JobPauseTool(_JobToolBase):
    name = "job_pause"
    description = "暂停定时任务(用户说'停掉/暂停那个提醒'时用)。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}}}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        ok = JobStore(self._ledger).pause(str(args.get("job_id") or ""),
                                          reason="by-agent", actor=self._session_key)
        return {"job_id": args.get("job_id"), "ok": ok}


class JobDeleteTool(_JobToolBase):
    name = "job_delete"
    description = "删除定时任务(用户明确说'不要这个定时任务'时用)。"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}}}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        ok = JobStore(self._ledger).delete(str(args.get("job_id") or ""),
                                           actor=self._session_key)
        return {"job_id": args.get("job_id"), "ok": ok}


def session_system_tools(ledger: Any, home: str, session_key: str,
                         actor_trust: str | None,
                         make_adapter: Any = None) -> list[Any]:
    """每任务实例化(绑定会话与信任);run_agent 装配进本任务的工具集。"""
    return [SystemStatusTool(ledger, home, actor_trust),
            JobCreateTool(ledger, session_key, actor_trust, make_adapter),
            JobPauseTool(ledger, session_key, actor_trust),
            JobDeleteTool(ledger, session_key, actor_trust)]
