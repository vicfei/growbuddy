"""渠道对端信任派生(主文档 §4.4 / K.4,M2-S5;形态借鉴 Samsara trust.ts)。

信任栈 T1-T3 认证的是 WS 设备;聊天渠道的对端(微信 peer/浏览器 peer)
无设备身份——信任级别由渠道配置推导:peer→trust 映射(allowlist)+
未列名默认(K.4:未列名私聊 guest;webchat 回环=owner 属宪法层条款)。

载体:home 下 trust.json(0600,渠道配置——非账本事实);派生来源
(trust_source)随 session.open 入账,审计可查。映射变更 = R2 级语义:
文件仅 owner 本地可写。
"""

from __future__ import annotations

import datetime
import os
from typing import Any

from pydantic import BaseModel, ConfigDict

from naga import storage

TRUST_LEVELS = ("owner", "known", "guest", "untrusted")

# 渠道级代码默认(文件未配置时):wechat 未列名=guest(K.4 谨慎优先);
# webchat=owner(回环绑定属宪法层条款);未知渠道=untrusted(宁严勿宽)
DEFAULT_TRUST: dict[str, str] = {"wechat": "guest", "webchat": "owner"}
UNKNOWN_CHANNEL_TRUST = "untrusted"


class TrustGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peer_id: str
    trust: str
    granted_at: str
    note: str | None = None


class ChannelTrustConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_trust: str | None = None
    allowlist: list[TrustGrant] = []


class TrustConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    channels: dict[str, ChannelTrustConfig] = {}


class DerivedTrust(BaseModel):
    trust: str
    source: str  # allowlist | default(K.4:随 session.open 入账)


def trust_file_path(home: str | None = None) -> str:
    return os.path.join(storage.resolve_home(home), "trust.json")


def load_trust_config(path: str | None = None) -> TrustConfig:
    file = path or trust_file_path()
    try:
        with open(file, encoding="utf-8") as fh:
            cfg = TrustConfig.model_validate_json(fh.read())
        if cfg.version != 1:
            return TrustConfig()
        return cfg
    except (OSError, ValueError):
        return TrustConfig()


def _save(cfg: TrustConfig, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(cfg.model_dump_json(indent=2))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def grant_trust(channel: str, peer_id: str, trust: str, *,
                note: str | None = None, path: str | None = None) -> TrustConfig:
    """授予/更新一条映射(幂等:同 peer 覆盖;R2 语义=仅 owner 本地进程可调)。"""
    if trust not in TRUST_LEVELS:
        raise ValueError(f"非法信任级: {trust}(合法:{'/'.join(TRUST_LEVELS)})")
    if not channel or not peer_id:
        raise ValueError("channel/peer_id 不能为空")
    file = path or trust_file_path()
    cfg = load_trust_config(file)
    ch = cfg.channels.get(channel) or ChannelTrustConfig()
    ch.allowlist = [g for g in ch.allowlist if g.peer_id != peer_id]
    ch.allowlist.append(TrustGrant(
        peer_id=peer_id, trust=trust,
        granted_at=datetime.datetime.now(datetime.UTC).isoformat(),
        note=note))
    cfg.channels[channel] = ch
    _save(cfg, file)
    return cfg


def revoke_trust(channel: str, peer_id: str, *, path: str | None = None) -> TrustConfig:
    """撤销映射(未列名回退默认级;幂等:不存在也成功)。"""
    file = path or trust_file_path()
    cfg = load_trust_config(file)
    ch = cfg.channels.get(channel)
    if ch is not None:
        ch.allowlist = [g for g in ch.allowlist if g.peer_id != peer_id]
        cfg.channels[channel] = ch
        _save(cfg, file)
    return cfg


def derive_trust(channel: str, peer_id: str, *,
                 path: str | None = None) -> DerivedTrust:
    """信任派生(§4.4):allowlist 命中→该级;否则渠道默认(文件覆盖或代码默认)。"""
    cfg = load_trust_config(path)
    ch = cfg.channels.get(channel)
    grant = next((g for g in (ch.allowlist if ch else []) if g.peer_id == peer_id
                  and g.trust in TRUST_LEVELS), None)
    if grant is not None:
        return DerivedTrust(trust=grant.trust, source="allowlist")
    trust: Any = UNKNOWN_CHANNEL_TRUST
    if ch is not None and ch.default_trust in TRUST_LEVELS:
        trust = ch.default_trust
    elif channel in DEFAULT_TRUST:
        trust = DEFAULT_TRUST[channel]
    return DerivedTrust(trust=trust, source="default")
