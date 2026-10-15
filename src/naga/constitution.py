"""Constitution layer (§7.6 / INV-5, M5-1) — the R5 set made hard.

宪法 = 不可变规则资产(R5 集合:内核安全闸/授权矩阵/信任栈默认/回滚协议/
本层自身),可枚举、可校验、**运行时不可修改**:

- **资产形态**:constitution.py 内的规则清单本身就是宪法文本——每条规则
  带可执行的**活体锚点**(模块/属性/期望值);启动与日巡检逐条求值。
- **账本锚定**:首次启动把宪法摘要(规则的规范化哈希)以
  `constitution.anchored` 事件写入追加式哈希链账本——账本防篡改,锚点随之
  防篡改;此后每次启动比对"活体宪法哈希 == 账本锚定哈希",不一致即违宪。
  ("禁止修改安全闸"这条规则写在闸外面:改规则的唯一合法路径 = RFC 批次
  重锚定,且重锚定事件本身入账可审计。)
- **硬熔断**:任何运行路径对 R5 的写(策略值漂移/内核孪生改写/规则清单
  改动)在下次校验即触发 `ConstitutionViolation`——守护进程记录 CRITICAL
  并优雅停机(敢放手的第一原则:失效即安全),绝不带病运行。

K.3 的完整形态(M5:特权决策进程独立 + IPC)是本层下一步;当前形态 =
进程内可枚举校验 + 账本外锚 + 熔断,已兑现 INV-5 的应用层语义。
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
from dataclasses import dataclass
from typing import Any, cast

from naga.ledger_store import DurableLedger

CONSTITUTION_SCHEMA = "naga-constitution/0"


@dataclass(frozen=True)
class Rule:
    rid: str
    domain: str          # kernel-gate | auth-matrix | trust-default | rollback | self
    statement: str
    module: str          # 活体锚点:求值对象
    attr: str
    equals: Any          # 期望值(标量或可 canonical 化的容器)


def _kernel_twin_sha() -> str:
    """内核孪生(docs/design 基线的运行副本)的内容哈希——冻结锚。"""
    import os

    import naga.kernel as k
    with open(os.path.abspath(k.__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# R5 集合(§7.6):每条 = 声明 + 活体锚点。修改本清单或任何锚点期望值
# 即改变宪法哈希 → 与账本锚定不符 → 违宪熔断;唯一合法路径是 RFC 批次
# 重锚定(constitution.reanchored 事件入账)。
def rules(kernel_sha: str | None = None) -> list[Rule]:
    sha = kernel_sha or _kernel_twin_sha()
    return [
        Rule("C1", "kernel-gate", "内核安全闸:种子内核为冻结孪生,哈希钉死",
             "naga.constitution", "_KERNEL_SHA", sha),
        Rule("C2a", "auth-matrix", "guest 单任务预算封顶 50,000(§10.1)",
             "naga.gateway", "GUEST_TASK_TOKEN_BUDGET", 50_000),
        Rule("C2b", "auth-matrix", "known 单任务预算封顶 500,000(§10.1)",
             "naga.gateway", "KNOWN_TASK_TOKEN_BUDGET", 500_000),
        Rule("C2c", "auth-matrix", "R 级档位:guest=R0 / known=R1 / owner=R4(无封顶)",
             "naga.gateway", "TRUST_R_CEILING", {"owner": 4, "known": 1, "guest": 0}),
        Rule("C2d", "auth-matrix", "untrusted 预算归零(仅握手与只读事件流)",
             "naga.gateway", "task_budget.__defaults__", None),  # 声明式:由 C2a-d 联合表达
        Rule("C3a", "trust-default", "渠道默认信任:wechat=guest / webchat=owner",
             "naga.trust", "DEFAULT_TRUST", {"wechat": "guest", "webchat": "owner"}),
        Rule("C3b", "trust-default", "未知渠道一律 untrusted(宁严勿宽)",
             "naga.trust", "UNKNOWN_CHANNEL_TRUST", "untrusted"),
        Rule("C4", "rollback", "回滚协议:一切自我修改可逆,内核 Budget 树强制严格递减",
             "naga.kernel", "Budget", "class"),  # 声明式:C1 的孪生哈希覆盖其实现
        Rule("C5", "self", "本层自身:规则清单哈希随账本锚定,校验器在闸外",
             "naga.constitution", "CONSTITUTION_SCHEMA", CONSTITUTION_SCHEMA),
    ]


# C2d/C4 为声明式规则(无独立活体锚点,由相邻规则联合覆盖)——求值时特判
_DECLARATIVE = {"C2d", "C4"}
_KERNEL_SHA = _kernel_twin_sha()  # 模块加载时钉一次(C1 的期望值来源)


def _canon(value: Any) -> str:
    return repr(value)


def constitution_sha(kernel_sha: str | None = None) -> str:
    """宪法摘要:规则清单(含锚点期望值)的规范化哈希。"""
    payload = "\n".join(
        f"{r.rid}|{r.domain}|{r.module}|{r.attr}|{_canon(r.equals)}"
        for r in rules(kernel_sha))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ConstitutionReport:
    ok: bool
    sha: str
    anchored: str | None          # 账本锚定的哈希(无锚点=None)
    violations: list[str]

    def detail(self) -> str:
        anchor_state = ("match" if self.anchored == self.sha
                        else f"DRIFT anchored={self.anchored}" if self.anchored
                        else "not-anchored")
        return f"sha={self.sha[:16]}… anchor={anchor_state} " \
               f"violations={self.violations or '无'}"


class ConstitutionViolation(RuntimeError):
    """硬熔断:R5 被触碰(§7.6)——调用方必须停机并告警,不得带病运行。"""


def evaluate(kernel_sha: str | None = None) -> ConstitutionReport:
    """逐条求值活体锚点;规则清单/锚点漂移 → violations → 违宪。"""
    violations: list[str] = []
    for rule in rules(kernel_sha):
        if rule.rid in _DECLARATIVE:
            continue
        try:
            mod = importlib.import_module(rule.module)
        except ImportError as err:
            violations.append(f"{rule.rid}: 模块不可载 {rule.module}({err})")
            continue
        actual = getattr(mod, rule.attr, None)
        if _canon(actual) != _canon(rule.equals):
            violations.append(
                f"{rule.rid}: {rule.module}.{rule.attr} 实际 "
                f"{_canon(actual)[:60]} ≠ 宪法 {_canon(rule.equals)[:60]}")
    sha = constitution_sha(kernel_sha)
    return ConstitutionReport(ok=not violations, sha=sha,
                              anchored=None, violations=violations)


def anchored_sha(ledger: DurableLedger) -> str | None:
    """账本中的宪法锚定(最近一次 anchored/reanchored 事件;载荷小,走内联,
    从 head.log 解析——追加式哈希链即防篡改锚点)。"""
    import json
    sha: str | None = None
    try:
        with open(ledger.head_log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("kind") in ("constitution.anchored",
                                       "constitution.reanchored"):
                    payload = cast("dict[str, Any]", obj.get("payload") or {})
                    value = payload.get("sha")
                    sha = str(value) if value else sha
    except OSError:
        return None
    return sha


def verify(ledger: DurableLedger, *, fuse: bool = True
           ) -> ConstitutionReport:
    """完整校验:活体锚点 + 账本锚定;违宪即熔断(默认抛 Violation)。"""
    report = evaluate()
    report.anchored = anchored_sha(ledger)
    if report.anchored is not None and report.anchored != report.sha:
        report.violations.append(
            f"宪法漂移: 活体 {report.sha[:16]}… ≠ 账本锚定 "
            f"{report.anchored[:16]}…(合法路径=RFC 重锚定入账)")
        report.ok = False
    if not report.ok and fuse:
        raise ConstitutionViolation("; ".join(report.violations))
    return report


def anchor(ledger: DurableLedger, *, reanchor: bool = False,
           note: str = "") -> str:
    """锚定(首启)或重锚定(RFC 批次;事件类型不同,可审计)。"""
    report = evaluate()
    kind = "constitution.reanchored" if reanchor else "constitution.anchored"
    payload = {"sha": report.sha, "schema": CONSTITUTION_SCHEMA,
               "rules": len(rules()), "note": note,
               "anchored_at": datetime.datetime.now(datetime.UTC).isoformat()}
    ledger.append(kind, "root", payload)
    return report.sha
