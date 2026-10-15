"""Soak runtime (M5-2): 30-day clock, P0 detection, health heartbeats.

P0(敢放手失败)= 守护失活 / 账本链校验失败 / 宪法违宪 / 渠道全灭。
心跳周期性自检(账本 verify + 渠道轮询 + 磁盘余量),结果落
state/soak.json;`naga soak` 出报告(起始日/已运行天数/P0 计数/最近心跳)。
30 天无 P0 = M5 出口①;守护重启检测(pidfile 断档)计一次 P0 并续跑
(soak 不因 P0 清零重计——报告如实累计,出口评审看 P0==0)。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, cast

SOAK_DAYS = 30               # 路线图 M5:30 天 soak 无 P0
HEARTBEAT_SEC = 3600         # 心跳周期(1h)
DISK_MIN_FREE_MB = 512       # 磁盘余量地板(低于即 P0)
MAX_HEARTBEATS_KEPT = 24 * SOAK_DAYS  # 心跳保留上限(即每小时一次整程)


def soak_path(home: str) -> str:
    return os.path.join(home, "state", "soak.json")


def load_soak(home: str) -> dict[str, Any]:
    try:
        with open(soak_path(home), encoding="utf-8") as fh:
            raw: Any = json.load(fh)
        return cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save(home: str, state: dict[str, Any]) -> None:
    path = soak_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def ensure_started(home: str) -> dict[str, Any]:
    """幂等启动 soak 时钟;守护重启检测(pidfile 断档)由 daemon 记 P0。"""
    state: dict[str, Any] = load_soak(home)
    if not state.get("started_at"):
        state = {"started_at": datetime.datetime.now(datetime.UTC).isoformat(),
                 "p0_events": [], "heartbeats": []}
        save(home, state)
    return state


def record_p0(home: str, kind: str, detail: str = "") -> None:
    state = load_soak(home)
    state.setdefault("p0_events", []).append(
        {"kind": kind, "detail": detail[:200],
         "at": datetime.datetime.now(datetime.UTC).isoformat()})
    save(home, state)


def record_heartbeat(home: str, health: dict[str, Any]) -> None:
    state = load_soak(home)
    beats = state.setdefault("heartbeats", [])
    beats.append({"at": datetime.datetime.now(datetime.UTC).isoformat(),
                  **health})
    del beats[:-MAX_HEARTBEATS_KEPT]
    save(home, state)


def health_check(ledger: Any, channels: dict[str, bool],
                 home: str) -> dict[str, Any]:
    """一次心跳自检:P0 判定项逐项求值(结果同时落心跳与 P0)。"""
    issues: list[str] = []
    entries = len(ledger.entries)
    try:
        chain_ok = bool(ledger.verify())
    except Exception:  # noqa: BLE001 —— 校验异常按失败计
        chain_ok = False
    if not chain_ok:
        issues.append("账本链校验失败")
    if channels and not any(channels.values()):
        issues.append("渠道全灭(已绑定但无一轮询)")
    try:
        usage = os.statvfs(home)
        free_mb = usage.f_bavail * usage.f_frsize // (1024 * 1024)
        if free_mb < DISK_MIN_FREE_MB:
            issues.append(f"磁盘余量 {free_mb}MB 低于 {DISK_MIN_FREE_MB}MB")
    except OSError:
        free_mb = -1
        issues.append("磁盘余量不可测")
    return {"chain_ok": chain_ok, "entries": entries,
            "channels": channels, "disk_free_mb": free_mb,
            "issues": issues, "p0": bool(issues)}


def soak_report(home: str) -> dict[str, Any]:
    state = load_soak(home)
    started = str(state.get("started_at") or "")
    p0_events = list(state.get("p0_events") or [])
    beats = list(state.get("heartbeats") or [])
    days = 0.0
    if started:
        try:
            start = datetime.datetime.fromisoformat(started)
            days = (datetime.datetime.now(datetime.UTC) - start).total_seconds() / 86400
        except ValueError:
            pass
    return {"started_at": started or None, "days_elapsed": round(days, 2),
            "target_days": SOAK_DAYS, "p0_count": len(p0_events),
            "p0_events": p0_events[-10:], "heartbeats": len(beats),
            "last_heartbeat": beats[-1] if beats else None,
            "on_track": days >= SOAK_DAYS and not p0_events}
