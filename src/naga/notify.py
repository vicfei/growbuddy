"""补发队列(#93):iLink 主动推送受 context_token 时效窗口限制——
token 过期即拒发(errcode=-2 "prepare failed")。被拒的通知按会话
持久化(0600),用户下次来消息(token 新鲜)时先行补发。每会话封顶,
防止长期不来的会话无限积压。"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, cast

PENDING_MAX = 5  # 每会话积压上限(候选 spec-constants notify_pending_max)


def load(path: str) -> dict[str, list[dict[str, Any]]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data: object = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cast("dict[str, list[dict[str, Any]]]", data) \
        if isinstance(data, dict) else {}


def save(path: str, pending: dict[str, list[dict[str, Any]]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(pending, ensure_ascii=False, sort_keys=True))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def enqueue(path: str, session_key: str, text: str) -> None:
    pending = load(path)
    queue = pending.setdefault(session_key, [])
    queue.append({"text": text,
                  "ts": datetime.datetime.now(datetime.UTC).isoformat()})
    del queue[:-PENDING_MAX]
    save(path, pending)


def take(path: str, session_key: str) -> list[dict[str, Any]]:
    """取出并清空该会话的积压(补发失败时由调用方重新入队)。"""
    pending = load(path)
    queue = pending.pop(session_key, None)
    if queue:
        save(path, pending)
    return queue or []
