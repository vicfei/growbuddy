"""会话注册表(数据模型 §5 sessions 落地;DM 表在冻结基线,R03 在查)。

会话从"每条消息现场拼出的字符串"升级为一等实体:开(首次活动)、活
(trace 投影更新 last_active_at)、关(闲置超时 session.close)。一次
"关闭"即一次会话收尾,触发 §6.5 原文形态的整合蒸馏——把会话期内逐任务
的情景摘要一次 LLM 调用(④ 类)整合为连贯纪要并提炼长期事实;任务级
提炼保留(会话内连续性来源),收尾整合负责"整整齐齐"。

- **零新增 kind**:session.open|close 本在冻结枚举;last_active 经
  trace.record 投影重建(逐消息入账属噪音,违反账本纪律)。
- **重开语义**:关闭后再来消息 → 新一次 session.open(新开一段对话,
  created_at 重置;微信渠道自身的带 trust_source 首开仍由渠道负责,
  重开由 gateway.note_activity 补发)。
- **job 会话排除**:job:{id} 是时间驱动执行体,非人类对话,不入注册表
  (不参与闲置关闭与收尾蒸馏)。
- 通知投递:Gateway.notify_session 按会话前缀路由到绑定渠道实例
  (peer==binder,allowlist 信任派生实证),定时任务结果由此推到微信。

候选 spec-constants(RFC 积压,研究文档 E''''):session_idle_minutes /
session_idle_poll_seconds。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

from naga.ledger_store import DurableLedger
from naga.memory import MemoryStore, parse_distilled

SESSION_IDLE_MINUTES = 60    # 候选 spec-constants session_idle_minutes
SESSION_IDLE_POLL_SEC = 60   # 候选 session_idle_poll_seconds
_NOTIFY_MAX_CHARS = 600      # 任务结果推送摘要上限(微信分段由渠道负责)


class SessionRegistry:
    """闲置关闭 + 收尾整合蒸馏;时钟可注入(测试确定性)。"""

    def __init__(self, ledger: DurableLedger, *,
                 memory: MemoryStore | None = None,
                 make_adapter: Any = None,
                 now: Any = None):
        self._ledger = ledger
        self._memory = memory
        self._make_adapter = make_adapter  # 收尾蒸馏用(④ 类;缺省跳过蒸馏)
        self._now = now or (lambda: datetime.datetime.now(datetime.UTC))

    # ---- 查询 ----

    def list_sessions(self, *, state: str | None = None
                      ) -> list[dict[str, Any]]:
        sql = ("SELECT session_key, lane_id, trust_level, state, created_at, "
               "last_active_at FROM sessions")
        params: tuple[Any, ...] = ()
        if state is not None:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY last_active_at DESC"
        keys = ("session_key", "lane_id", "trust_level", "state",
                "created_at", "last_active_at")
        return [dict(zip(keys, row)) for row in self._ledger.query(sql, params)]

    # ---- 闲置关闭(会话收尾) ----

    def close_idle(self) -> list[str]:
        """关闭闲置超期的会话(session.close 入账);返回被关的键。"""
        cutoff = (self._now() - datetime.timedelta(minutes=SESSION_IDLE_MINUTES)
                  ).isoformat()
        rows = self._ledger.query(
            "SELECT session_key, trust_level FROM sessions "
            "WHERE state='open' AND last_active_at < ?", (cutoff,))
        closed: list[str] = []
        for session_key, _trust in rows:
            self._ledger.append("session.close", str(session_key), {
                "session_key": session_key, "reason": "idle",
                "closed_at": self._now().isoformat()})
            closed.append(str(session_key))
        return closed

    async def consolidate(self, session_key: str) -> dict[str, int]:
        """收尾整合蒸馏(§6.5 会话收尾形态):一次 LLM 调用,输入=本段
        会话期内的情景摘要,输出=连贯纪要 [E] + 长期事实 [S]。写闸仍在
        MemoryStore 单点(信任级取会话行)。返回 {episodic, semantic}。"""
        if self._memory is None or self._make_adapter is None:
            return {"episodic": 0, "semantic": 0}
        row = self._ledger.query(
            "SELECT trust_level, created_at FROM sessions WHERE session_key=?",
            (session_key,))
        if not row:
            return {"episodic": 0, "semantic": 0}
        trust, created = row[0]
        items = self._ledger.query(
            "SELECT cas_id FROM memory_items WHERE session_key=? "
            "AND layer='episodic' AND status='active' AND created_at >= ? "
            "ORDER BY created_at", (session_key, created))
        if not items:
            return {"episodic": 0, "semantic": 0}
        summaries: list[str] = []
        for (cas_id,) in items:
            content = self._memory.read_content(str(cas_id))
            if content:
                summaries.append(f"- {content}")
        if not summaries:
            return {"episodic": 0, "semantic": 0}
        prompt = (
            "以下是同一段对话期间逐次任务留下的事件摘要。请整合为该段对话的"
            "连贯纪要并提炼长期事实。\n"
            "第一行:[E] <本段对话的整合纪要(做了什么、结论如何,150字内)>\n"
            "随后每行一条:[S] <值得长期记住的用户偏好或事实>(最多3条,没有就"
            "不输出)\n只输出以上行,不要多余解释。\n\n"
            "事件摘要:\n" + "\n".join(summaries))
        try:
            resp = await self._make_adapter().chat(
                [{"role": "user", "content": prompt}], None, max_tokens=8192)
        except Exception:  # noqa: BLE001 —— 蒸馏失败不阻断关闭(事件已入账)
            return {"episodic": 0, "semantic": 0}
        episodic, semantic = parse_distilled((resp.content or "").strip())
        written_e = written_s = 0
        provenance = {"source": "session-close", "session_key": session_key}
        if episodic:
            try:
                self._memory.write("episodic", session_key, episodic,
                                   provenance, actor_trust=trust, actor="curator")
                written_e = 1
            except ValueError:
                pass
        for fact in semantic:
            try:
                self._memory.write("semantic", session_key, fact, provenance,
                                   actor_trust=trust, actor="curator")
                written_s += 1
            except ValueError:
                pass  # guest 会话的 [S] 被闸门拒绝(§6.5),逐条丢弃
        return {"episodic": written_e, "semantic": written_s}

    async def close_idle_and_consolidate(self) -> dict[str, Any]:
        """守护环单步:关闭闲置会话并逐个收尾蒸馏(蒸馏异常不扩散)。"""
        closed = self.close_idle()
        consolidated: dict[str, dict[str, int]] = {}
        for session_key in closed:
            consolidated[session_key] = await self.consolidate(session_key)
        return {"closed": closed, "consolidated": consolidated}

    async def run_forever(self, interval: int = SESSION_IDLE_POLL_SEC) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.close_idle_and_consolidate()
            except Exception as err:  # noqa: BLE001 —— 闲置环不死
                print(f"[sessions] 闲置关闭异常: {type(err).__name__}: {err}",
                      flush=True)


def job_notify_text(job_id: str, outcome: str, reply: str | None) -> str:
    """任务结果推送文案(截断;渠道侧另有分段)。"""
    head = f"[定时任务 {job_id[-6:]}] {outcome}"
    if not reply:
        return head
    body = reply if len(reply) <= _NOTIFY_MAX_CHARS \
        else reply[:_NOTIFY_MAX_CHARS] + "…"
    return f"{head}\n{body}"


def parse_session_channel(session_key: str) -> tuple[str, str]:
    """会话键 → (channel, peer);"wechat:dm:u1" → ("wechat", "u1")。"""
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[1] == "dm":
        return parts[0], ":".join(parts[2:])
    return (parts[0], "") if parts else ("", "")


def creator_session_of(creator: dict[str, Any] | None) -> str | None:
    """job.created 载荷的 creator → 会话键(channel:dm:peer)。"""
    if not isinstance(creator, dict):
        return None
    channel = str(creator.get("channel") or "")
    peer = str(creator.get("peer") or "")
    if channel and peer:
        return f"{channel}:dm:{peer}"
    return None
