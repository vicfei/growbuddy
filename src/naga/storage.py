"""M0 存储拓扑骨架。

数据模型设计文档 §2 的目录布局与 home 解析。本模块只负责布局存在性与
权限;head.log 追加、segments 封存、快照、CAS 读写逻辑属 WS2,不在此实现。
"""

from __future__ import annotations

import os

# 数据模型 §2 拓扑的目录部分(相对 home;ledger/ 父目录随子目录自动创建)
LAYOUT_DIRS = (
    "ledger/segments",
    "ledger/snapshots",
    "assets/blobs",
    "assets/sidecar",
    "assets/refs",
    "memory/episodic",
    "memory/semantic",
    "traces",
    "shadow",
    "credentials",
    "keys/escrow",
)

# 凭据与密钥库属敏感目录(数据模型 §2:credentials 独立权限 0600 级)
PRIVATE_DIRS = frozenset({"credentials", "keys", "keys/escrow"})


def resolve_home(explicit: str | None = None) -> str:
    """home 解析优先级:显式参数 > SAMSARA_HOME 环境变量 > ~/.samsara。"""
    if explicit:
        return explicit
    env = os.environ.get("SAMSARA_HOME")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".samsara")


def init_home(home: str | None = None) -> str:
    """幂等创建完整布局,返回 home 绝对路径;私有目录权限 0700。"""
    h = resolve_home(home)
    for d in LAYOUT_DIRS:
        path = os.path.join(h, d)
        os.makedirs(path, exist_ok=True)
        if d in PRIVATE_DIRS:
            os.chmod(path, 0o700)
    return h
