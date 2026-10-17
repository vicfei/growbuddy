"""定时任务与调度器(主文档附录 C,数据模型 C.3,接口 §3.5)。

调度器是"到点发事件"的 L0 特权形态:任务注册/迁移全入账(账本重放即
恢复),触发后复用既有架构——sessionKey 路由进独立 lane → 标准 agent.run
(COW 分支+轨迹)→ notification 策略投递。

- **任务规格(C.2)**:cron + timezone + goal(CAS 资产)+ 信任快照 +
  预算 + rCeiling + misfire;R3+ 强制有效期(DDL CHECK 同款校验)。
- **自然语言创建(C.2,约 1 次 LLM 调用)**:编译为 {cron, timezone,
  goal, misfire};编译产物过 croniter 校验,非法即拒;已是合法 cron 的
  输入直接采用(零 LLM)。响应附每月触发次数估算。
- **权限快照与时效(C.4)**:每次触发重校验创建者**当前**信任级
  (创建者 channel/peer 从 job.created 载荷恢复;账本为权威)——降级即
  自动暂停并入账告警;过期即暂停;R3+ 到期需 job.renew 续期。
- **misfire(GAP12)**:守护进程停机期间错过的触发出账本 last-fire
  推算——skip 错过即弃(仅 job.missed 审计);runOnce/catchUp 恢复后
  补跑一次(job.fire{backfill:true}),不逐次补齐,无积压风暴。
- **触发链路(C.3)**:fire 先入账 job.fire,再经 gateway.submit 进
  lane 执行;job 级 wallMs 用 asyncio.wait_for 兜底。
- rCeiling 在触发时对工具集取子集(叠在宪法信任档位之上的收紧,
  task_budget/TRUST_R_CEILING 不动)。

新增 additive kind(RFC 积压,研究文档 E''):job.created/paused/
resumed/deleted/renewed;job.fire|missed 已在冻结 DM §3.1 枚举。
候选 spec-constants:job_scheduler_tick_seconds / job_default_budget_tokens
/ job_goal_max_bytes / job_wall_ms_default。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import uuid
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, cast

from croniter import croniter

from naga.ledger_store import DurableLedger
from naga.trust import TRUST_LEVELS, derive_trust

SCHEDULER_TICK_SEC = 30        # 候选 spec-constants job_scheduler_tick_seconds
JOB_DEFAULT_BUDGET_TOKENS = 200_000  # 候选 job_default_budget_tokens
JOB_GOAL_MAX_BYTES = 4096      # 候选 job_goal_max_bytes
JOB_WALL_MS_DEFAULT = 1_800_000  # 候选 job_wall_ms_default(C.2 示例值)
MISFIRE_MODES = ("skip", "runOnce", "catchUp")  # GAP12 语义
R_LEVELS = ("R0", "R1", "R2", "R3", "R4")
DEFAULT_TZ = "Asia/Shanghai"
_ESTIMATE_WINDOW_DAYS = 30
_ESTIMATE_MAX_FIRES = 400


class JobError(ValueError):
    """任务规格/迁移非法(cron/时区/预算/有效期/状态)。"""


def validate_cron(expr: str) -> str:
    expr = expr.strip()
    if not expr or not croniter.is_valid(expr):
        raise JobError(f"非法 cron 表达式: {expr!r}")
    return expr


def validate_timezone(name: str) -> str:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as err:
        raise JobError(f"非法时区: {name!r}") from err
    return name


def fires_per_month(schedule: str, timezone: str, *,
                    now: datetime.datetime | None = None) -> int:
    """未来 30 天触发次数估算(接口 §3.5:响应含预估;封顶防滥用)。"""
    base = (now or datetime.datetime.now(datetime.UTC)).astimezone(
        zoneinfo.ZoneInfo(timezone))
    it = croniter(schedule, base)
    count = 0
    horizon = base + datetime.timedelta(days=_ESTIMATE_WINDOW_DAYS)
    while count < _ESTIMATE_MAX_FIRES:
        nxt = it.get_next(datetime.datetime)
        if nxt > horizon:
            break
        count += 1
    return count


def parse_budget(budget_json: str) -> dict[str, int]:
    try:
        raw: object = json.loads(budget_json)
    except ValueError as err:
        raise JobError("budget_json 非法") from err
    if not isinstance(raw, dict):
        raise JobError("budget_json 必须是映射")
    return cast_budget(cast("dict[str, Any]", raw))


def cast_budget(raw: dict[str, Any]) -> dict[str, int]:
    tokens = int(raw.get("tokens") or JOB_DEFAULT_BUDGET_TOKENS)
    wall_ms = int(raw.get("wall_ms") or JOB_WALL_MS_DEFAULT)
    if tokens <= 0 or wall_ms <= 0:
        raise JobError("预算必须为正(tokens/wall_ms)")
    return {"tokens": tokens, "wall_ms": wall_ms}


@dataclass
class JobSpec:
    job_id: str
    schedule: str
    timezone: str
    goal: str
    trust_snapshot: str
    budget: dict[str, int]
    r_ceiling: str
    notification: str = "smart"
    misfire: str = "skip"
    expires_at: str | None = None
    creator_channel: str = "webchat"
    creator_peer: str = ""

    def to_payload(self, goal_cas: str) -> dict[str, Any]:
        return {
            "id": self.job_id, "schedule_cron": self.schedule,
            "timezone": self.timezone, "goal_cas": goal_cas,
            "trust_snapshot": self.trust_snapshot,
            "budget": self.budget, "r_ceiling": self.r_ceiling,
            "notification": self.notification, "misfire": self.misfire,
            "expires_at": self.expires_at,
            "creator": {"channel": self.creator_channel,
                        "peer": self.creator_peer}}


class JobStore:
    """任务生命周期:校验 → goal 入 CAS → 入账;查询走投影(可重建)。"""

    def __init__(self, ledger: DurableLedger):
        self._ledger = ledger

    def create(self, spec: JobSpec, *, actor: str = "root") -> str:
        validate_cron(spec.schedule)
        validate_timezone(spec.timezone)
        data = spec.goal.encode("utf-8")
        if not data or len(data) > JOB_GOAL_MAX_BYTES:
            raise JobError(f"goal {len(data)} 字节超限(1..{JOB_GOAL_MAX_BYTES})")
        if spec.trust_snapshot not in TRUST_LEVELS:
            raise JobError(f"非法信任快照: {spec.trust_snapshot}")
        if spec.r_ceiling not in R_LEVELS:
            raise JobError(f"非法 rCeiling: {spec.r_ceiling}(合法:{R_LEVELS})")
        if spec.misfire not in MISFIRE_MODES:
            raise JobError(f"非法 misfire: {spec.misfire}(合法:{MISFIRE_MODES})")
        if spec.notification not in ("smart", "immediate", "silent"):
            raise JobError(f"非法 notification: {spec.notification}")
        spec.budget = cast_budget(spec.budget)
        # C.4/DDL CHECK:R3+ 高危任务强制有效期
        high_risk = int(spec.r_ceiling[1:]) >= 3
        if high_risk and not spec.expires_at:
            raise JobError("rCeiling >= R3 的任务必须设定 expires_at(C.4 有效期)")
        goal_cas = self._ledger.cas.put(data)
        self._ledger.append("job.created", actor, spec.to_payload(goal_cas))
        return spec.job_id

    def _transition(self, kind: str, job_id: str, *, actor: str,
                    payload: dict[str, Any] | None = None,
                    require_state: str | None = None) -> bool:
        rows = self._ledger.query(
            "SELECT state FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return False
        if require_state is not None and rows[0][0] != require_state:
            raise JobError(f"任务 {job_id} 当前 {rows[0][0]},不可 {kind.removeprefix('job.')}")
        body = {"id": job_id, **(payload or {})}
        self._ledger.append(kind, actor, body)
        return True

    def pause(self, job_id: str, *, reason: str = "", actor: str = "root") -> bool:
        return self._transition("job.paused", job_id, actor=actor,
                                payload={"reason": reason})

    def resume(self, job_id: str, *, actor: str = "root") -> bool:
        return self._transition("job.resumed", job_id, actor=actor,
                                require_state="paused")

    def delete(self, job_id: str, *, actor: str = "root") -> bool:
        return self._transition("job.deleted", job_id, actor=actor)

    def renew(self, job_id: str, until: str, *, actor: str = "root") -> bool:
        validate_expiry(until)
        return self._transition("job.renewed", job_id, actor=actor,
                                payload={"expires_at": until})

    def list_jobs(self, *, state: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT id, schedule_cron, timezone, goal_cas, trust_snapshot, "
               "budget_json, r_ceiling, notification, misfire, expires_at, state "
               "FROM jobs")
        params: tuple[Any, ...] = ()
        if state is not None:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY id"
        return [dict(zip(
            ("id", "schedule", "timezone", "goal_cas", "trust_snapshot",
             "budget", "r_ceiling", "notification", "misfire", "expires_at",
             "state"), row)) for row in self._ledger.query(sql, params)]

    def goal_of(self, goal_cas: str) -> str:
        return self._ledger.cas.get(goal_cas).decode("utf-8")


def validate_expiry(until: str) -> str:
    try:
        parsed = datetime.datetime.fromisoformat(until)
    except ValueError as err:
        raise JobError(f"非法 expires_at: {until!r}(ISO8601)") from err
    if parsed.tzinfo is None:
        raise JobError("expires_at 必须带时区(ISO8601)")
    return until


# ── 自然语言编译(C.2,约 1 次 LLM 调用) ─────────────────────────

_COMPILE_PROMPT = """把用户的定时需求编译为 JSON 任务规格。只输出一个 JSON 对象,不要多余解释。
字段:
- "cron": 标准 5 字段 cron 表达式(分 时 日 月 周)
- "timezone": IANA 时区(用户未提则 "Asia/Shanghai")
- "goal": 任务目标(祈使句,将来到点执行的内容;提醒类写明提醒内容)
- "misfire": "skip" | "runOnce" | "catchUp"(用户未提则 "skip")

用户需求:{text}"""


async def compile_natural_language(adapter: Any, text: str) -> JobSpec:
    """自然语言 → JobSpec(缺省信任快照 owner/预算默认,由创建面补齐)。"""
    resp = await adapter.chat(
        [{"role": "user", "content": _COMPILE_PROMPT.format(text=text)}],
        None, max_tokens=8192)  # 推理模型纪律:reasoning+content 共享上限
    body = (resp.content or "").strip()
    m = re.search(r"\{.*\}", body, re.DOTALL)
    if not m:
        raise JobError(f"编译输出无 JSON 对象: {body[:80]!r}")
    try:
        parsed: object = json.loads(m.group(0))
    except ValueError as err:
        raise JobError(f"编译输出 JSON 非法: {err}") from err
    if not isinstance(parsed, dict):
        raise JobError("编译输出必须是 JSON 对象")
    out = cast("dict[str, Any]", parsed)
    cron = validate_cron(str(out.get("cron") or ""))
    tz = validate_timezone(str(out.get("timezone") or DEFAULT_TZ))
    goal = str(out.get("goal") or "").strip()
    if not goal:
        raise JobError("编译输出缺少 goal")
    misfire = str(out.get("misfire") or "skip")
    if misfire not in MISFIRE_MODES:
        misfire = "skip"
    return JobSpec(job_id="job_" + uuid.uuid4().hex[:20], schedule=cron,
                   timezone=tz, goal=goal, trust_snapshot="owner",
                   budget={"tokens": JOB_DEFAULT_BUDGET_TOKENS,
                           "wall_ms": JOB_WALL_MS_DEFAULT},
                   r_ceiling="R1", misfire=misfire)


# ── 调度器(到点发事件;判定纯函数化,时钟可注入) ─────────────────

@dataclass
class DueReport:
    fired: list[str] = field(default_factory=list[str])
    paused: dict[str, str] = field(default_factory=dict[str, str])
    missed: dict[str, int] = field(default_factory=dict[str, int])


def scan_job_events(ledger: DurableLedger) -> dict[str, dict[str, Any]]:
    """segments + head.log 扫 job 域事件:{job_id: {last_fire, creator,
    created_ts}}。封存不丢锚:last_fire 是防重复触发的正确性依据。"""
    import os

    out: dict[str, dict[str, Any]] = {}
    seg_dir = os.path.join(ledger.home, "ledger", "segments")
    files = []
    if os.path.isdir(seg_dir):
        files = sorted(os.path.join(seg_dir, n) for n in os.listdir(seg_dir)
                       if n.endswith(".log"))
    files.append(ledger.head_log_path)
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("kind")
            if not isinstance(kind, str) or not kind.startswith("job."):
                continue
            raw_payload: object = obj.get("payload")
            if not isinstance(raw_payload, dict):
                continue
            payload = cast("dict[str, Any]", raw_payload)
            jid = str(payload.get("id") or "")
            if not jid:
                continue
            slot: dict[str, Any] = out.setdefault(jid, {})
            if kind == "job.created":
                creator: object = payload.get("creator")
                slot["creator"] = (cast("dict[str, Any]", creator)
                                   if isinstance(creator, dict) else {})
                slot["created_ts"] = obj.get("ts")
            elif kind == "job.fire":
                slot["last_fire"] = obj.get("ts")
    return out


def _ts_to_dt(ts: object) -> datetime.datetime:
    """head.log 的 ts:float 纪元或 ISO 字符串(两态兼容,curator 同款)。"""
    if isinstance(ts, (int, float)):
        return datetime.datetime.fromtimestamp(float(ts), tz=datetime.UTC)
    try:
        return datetime.datetime.fromisoformat(str(ts))
    except ValueError:
        return datetime.datetime.now(datetime.UTC)


class JobScheduler:
    """触发判定纯函数 + 可注入时钟;执行经 gateway lane(C.3)。

    fire_callback:异步 (job_row, goal, backfill) → None(由装配面注入,
    生产形态走 gateway.submit;测试可替换)。
    """

    def __init__(self, ledger: DurableLedger, *,
                 fire_callback: Any,
                 trust_path: str | None = None,
                 now: Any | None = None):
        self._ledger = ledger
        self._fire = fire_callback
        self._trust_path = trust_path
        self._now = now or (lambda: datetime.datetime.now(datetime.UTC))

    def _tz_now(self, timezone: str) -> datetime.datetime:
        return self._now().astimezone(zoneinfo.ZoneInfo(timezone))

    def _due_occurrence(self, row: dict[str, Any], events: dict[str, Any]
                        ) -> datetime.datetime | None:
        """最近一次应触发时刻(晚于 last_fire/created 才算到期)。"""
        tz = zoneinfo.ZoneInfo(row["timezone"])
        local_now = self._now().astimezone(tz)
        prev = croniter(row["schedule"], local_now).get_prev(datetime.datetime)
        slot = events.get(row["id"], {})
        anchor_ts = slot.get("last_fire") or slot.get("created_ts")
        if anchor_ts is None:
            return None  # 无账本锚(异常态):不触发,宁严勿宽
        anchor = _ts_to_dt(anchor_ts).astimezone(tz)
        return prev if prev > anchor else None

    def _count_missed(self, row: dict[str, Any], events: dict[str, Any]) -> int:
        tz = zoneinfo.ZoneInfo(row["timezone"])
        local_now = self._now().astimezone(tz)
        slot = events.get(row["id"], {})
        anchor_ts = slot.get("last_fire") or slot.get("created_ts")
        anchor = (_ts_to_dt(anchor_ts).astimezone(tz) if anchor_ts is not None
                  else local_now)
        it = croniter(row["schedule"], anchor)
        count = 0
        while count < _ESTIMATE_MAX_FIRES:
            nxt = it.get_next(datetime.datetime)
            if nxt > local_now:
                break
            count += 1
        return count

    def _creator_trust_now(self, row: dict[str, Any],
                           events: dict[str, Any]) -> str:
        raw_creator: object = events.get(row["id"], {}).get("creator")
        creator = (cast("dict[str, Any]", raw_creator)
                   if isinstance(raw_creator, dict) else {})
        channel = str(creator.get("channel") or "webchat")
        peer = str(creator.get("peer") or "")
        derived = derive_trust(channel, peer, path=self._trust_path)
        return derived.trust

    def check_guards(self, rows: list[dict[str, Any]], events: dict[str, Any]
                     ) -> list[tuple[dict[str, Any], str]]:
        """C.4 守卫:过期暂停 + 信任降级暂停;返回 (row, reason)。"""
        paused: list[tuple[dict[str, Any], str]] = []
        now_iso = self._now().isoformat()
        for row in rows:
            if row["expires_at"] and row["expires_at"] <= now_iso:
                paused.append((row, f"有效期已过({row['expires_at']})"))
                continue
            current = self._creator_trust_now(row, events)
            if TRUST_LEVELS.index(current) > TRUST_LEVELS.index(
                    row["trust_snapshot"]):
                reason = (f"创建者信任降级 {row['trust_snapshot']}→{current}"
                          f"(C.4 自动暂停)")
                paused.append((row, reason))
        return paused

    async def _dispatch(self, row: dict[str, Any], goal: str,
                        *, backfill: bool) -> None:
        """先入账 job.fire(防重复触发的账本锚),再执行。"""
        self._ledger.append("job.fire", "scheduler", {
            "id": row["id"], "goal_cas": row["goal_cas"],
            "schedule": row["schedule"], "backfill": backfill})
        try:
            await self._fire(row, goal, backfill)
        except Exception as err:  # noqa: BLE001 —— 单任务失败不杀调度环
            print(f"[jobs] 触发异常 {row['id']}: "
                  f"{type(err).__name__}: {err}", flush=True)

    async def recover(self) -> DueReport:
        """守护启动时的 misfire 处置(GAP12)。"""
        report = DueReport()
        events = scan_job_events(self._ledger)
        store = JobStore(self._ledger)
        rows = [r for r in store.list_jobs(state="active")
                if r["id"] in events]  # 无账本锚的孤儿投影不处置
        for row in rows:
            missed = self._count_missed(row, events)
            if missed <= 0:
                continue
            if row["misfire"] == "skip":
                self._ledger.append("job.missed", "scheduler",
                                    {"id": row["id"], "count": missed,
                                     "action": "skip"})
                report.missed[row["id"]] = missed
            else:  # runOnce / catchUp:恢复后补跑一次,不逐次补齐
                report.fired.append(row["id"])
                await self._dispatch(row, store.goal_of(row["goal_cas"]),
                                     backfill=True)
        return report

    async def tick(self) -> DueReport:
        """单次调度判定:守卫 → 到点触发。异常不外抛(环内消化)。"""
        report = DueReport()
        store = JobStore(self._ledger)
        events = scan_job_events(self._ledger)
        active = store.list_jobs(state="active")
        for row, reason in self.check_guards(active, events):
            store.pause(row["id"], reason=reason, actor="scheduler")
            report.paused[row["id"]] = reason
        active = [r for r in active if r["id"] not in report.paused]
        for row in active:
            if self._due_occurrence(row, events) is None:
                continue
            report.fired.append(row["id"])
            await self._dispatch(row, store.goal_of(row["goal_cas"]),
                                 backfill=False)
        return report

    async def run_forever(self, interval: int = SCHEDULER_TICK_SEC) -> None:
        await self.recover()
        while True:
            await asyncio.sleep(interval)
            try:
                await self.tick()
            except Exception as err:  # noqa: BLE001 —— 调度环不死
                print(f"[jobs] tick 异常: {type(err).__name__}: {err}", flush=True)
