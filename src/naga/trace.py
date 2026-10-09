"""Trace 记录器 + ReplayBundle 采集(K.5 / RM2:M1 起强制)。

Trace(接口 §6.1,samsara-trace/1):轻量 steps(no/kind/name/ok/ms/tokens),
finish 时入账(>1KB 自动外置 CAS)。ReplayBundle(samsara-replay/1):入站
消息全文、逐步工具 args/results、资产快照清单、环境指纹——组装后过两级
脱敏闸(secrets + PII)再落 CAS;单步超 replay_bundle_step_truncate_bytes
截断并标 partial:true。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from naga.ledger_store import DurableLedger

# 递归 JSON 值类型(redact 的输入输出;Any 会把 Unknown 传染进 strict)
type JsonLike = str | int | float | bool | None | list[JsonLike] | dict[str, JsonLike]

REPLAY_BUNDLE_STEP_TRUNCATE_BYTES = 10485760  # spec-constants / 接口 §6.1

TRACE_SCHEMA = "samsara-trace/1"
REPLAY_SCHEMA = "samsara-replay/1"

# 两级脱敏闸(接口 §6.1:①secrets ②PII + 可配置词表)
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
]
# PEM 块:块匹配正则在无标记长串上有灾难性回溯,先做包含性快检再启用
_PEM_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.|\n)*?-----END [A-Z ]*PRIVATE KEY-----")
_PII_PATTERNS = [
    re.compile(r"(?<!\d)(?:\+86)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
]
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(value: JsonLike, extra_terms: list[str] | None = None) -> JsonLike:
    """递归脱敏:secrets → [REDACTED:secret];PII → [REDACTED:pii]。

    继承 E.1 纪律:凭据永不入轨迹/bundle。截断发生在脱敏之后(K.5 语义),
    故本函数必须对超长字符串保持线性。
    """
    if isinstance(value, str):
        for pat in _SECRET_PATTERNS:
            value = pat.sub("[REDACTED:secret]", value)
        if "-----BEGIN" in value and "PRIVATE KEY-----" in value:
            value = _PEM_PATTERN.sub("[REDACTED:secret]", value)
        # 邮箱模式无字面前缀,`+@` 结构在长字母数字串上 O(n²) 回溯——@ 快检守卫
        if "@" in value:
            value = _EMAIL_PATTERN.sub("[REDACTED:pii]", value)
        for pat in _PII_PATTERNS:  # 定长模式(手机号/证件号),线性
            value = pat.sub("[REDACTED:pii]", value)
        for term in extra_terms or []:
            if term:
                value = value.replace(term, "[REDACTED:custom]")
        return value
    if isinstance(value, dict):
        return {k: redact(v, extra_terms) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, extra_terms) for v in value]
    return value


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _truncate(value: Any) -> dict[str, Any]:
    data = _canon(value).encode("utf-8")
    if len(data) <= REPLAY_BUNDLE_STEP_TRUNCATE_BYTES:
        return {"data": value, "partial": False}
    return {"data": data[:REPLAY_BUNDLE_STEP_TRUNCATE_BYTES].decode("utf-8", errors="ignore"),
            "partial": True, "original_bytes": len(data)}


class TraceRecorder:
    """一次任务的轨迹与重放包采集器。"""

    def __init__(self, ledger: DurableLedger, *, trace_id: str, session_key: str,
                 agent_id: str | None = None, inbound: Any = None,
                 skills_loaded: list[str] | None = None,
                 env: dict[str, Any] | None = None,
                 extra_redact_terms: list[str] | None = None):
        self._ledger = ledger
        self._t0 = time.monotonic()
        self._steps: list[dict[str, Any]] = []
        self._bundle_steps: list[dict[str, Any]] = []
        self._trace = {
            "trace_id": trace_id, "session_key": session_key, "agent_id": agent_id,
            "parent_trace": None, "skills_loaded": skills_loaded or [],
            "steps": self._steps, "outcome": "success", "failure_analysis": None,
            "cost": {"tokens": 0, "usd_cents": 0}, "duration_ms": 0,
            "replay_bundle_cas": None, "task_cluster": None,
            "schema": TRACE_SCHEMA,
        }
        self._bundle: dict[str, Any] = {
            "schema": REPLAY_SCHEMA,
            "session": {"inbound": redact(inbound, extra_redact_terms),
                        "session_key": session_key},
            "steps": self._bundle_steps,
            "assets": {"skills": skills_loaded or [], "prompts": [], "memory": []},
            "env": env or {},
        }

    def step(self, no: int, kind: str, name: str, ok: bool, ms: int, tokens: int = 0,
             args: Any = None, results: Any = None) -> None:
        self._steps.append({"no": no, "kind": kind, "name": name, "ok": ok,
                            "ms": ms, "tokens": tokens})
        self._bundle_steps.append({
            "no": no, "kind": kind, "name": name, "ok": ok, "ms": ms, "tokens": tokens,
            "args": _truncate(redact(args)),
            "results": _truncate(redact(results)),
        })

    def finish(self, *, outcome: str, cost_tokens: int = 0, cost_cents: int = 0,
               task_cluster: str | None = None, failure_analysis: Any = None,
               actor: str = "root") -> dict[str, Any]:
        self._trace.update({
            "outcome": outcome, "failure_analysis": failure_analysis,
            "cost": {"tokens": cost_tokens, "usd_cents": cost_cents},
            "duration_ms": int((time.monotonic() - self._t0) * 1000),
            "task_cluster": task_cluster,
        })
        bundle_cas = self._ledger.cas.put(_canon(self._bundle).encode("utf-8"))
        self._trace["replay_bundle_cas"] = bundle_cas
        self._ledger.append("trace.record", actor, self._trace)
        return self._trace
