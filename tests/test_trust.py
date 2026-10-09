"""K.4 信任框架测试:默认派生 / allowlist / grant-revoke 幂等 / 损坏安全 / 0600。"""

import os
import stat

from naga.trust import (
    DEFAULT_TRUST,
    UNKNOWN_CHANNEL_TRUST,
    derive_trust,
    grant_trust,
    load_trust_config,
    revoke_trust,
)


def test_defaults_and_unknown_channel(tmp_path):
    f = str(tmp_path / "trust.json")
    assert derive_trust("wechat", "peer-a", path=f).trust == DEFAULT_TRUST["wechat"]
    assert derive_trust("wechat", "peer-a", path=f).source == "default"
    assert derive_trust("webchat", "browser", path=f).trust == "owner"  # 回环宪法条款
    assert derive_trust("telegram", "x", path=f).trust == UNKNOWN_CHANNEL_TRUST  # 宁严勿宽


def test_grant_then_allowlist_hit_overrides_default(tmp_path):
    f = str(tmp_path / "trust.json")
    grant_trust("wechat", "boss", "owner", note="QR 绑定自动授予", path=f)
    d = derive_trust("wechat", "boss", path=f)
    assert d.trust == "owner" and d.source == "allowlist"
    # 其他人不受影响
    assert derive_trust("wechat", "other", path=f).trust == "guest"


def test_grant_idempotent_overwrite_and_invalid_level(tmp_path):
    f = str(tmp_path / "trust.json")
    grant_trust("wechat", "p", "known", path=f)
    grant_trust("wechat", "p", "owner", note="升级", path=f)
    cfg = load_trust_config(f)
    grants = [g for ch in cfg.channels.values() for g in ch.allowlist if g.peer_id == "p"]
    assert len(grants) == 1 and grants[0].trust == "owner"  # 幂等覆盖
    try:
        grant_trust("wechat", "p", "superuser", path=f)
        raise AssertionError("非法信任级必须被拒")
    except ValueError:
        pass


def test_revoke_falls_back_to_default_and_idempotent(tmp_path):
    f = str(tmp_path / "trust.json")
    grant_trust("wechat", "p", "owner", path=f)
    revoke_trust("wechat", "p", path=f)
    assert derive_trust("wechat", "p", path=f).trust == "guest"
    revoke_trust("wechat", "p", path=f)  # 幂等:不存在也成功
    revoke_trust("never", "p", path=f)


def test_corrupt_file_safe_empty(tmp_path):
    f = str(tmp_path / "trust.json")
    with open(f, "w") as fh:
        fh.write("{not json")
    assert load_trust_config(f).channels == {}
    assert derive_trust("wechat", "p", path=f).trust == "guest"


def test_file_permissions_0600(tmp_path):
    f = str(tmp_path / "trust.json")
    grant_trust("wechat", "p", "known", path=f)
    mode = stat.S_IMODE(os.stat(f).st_mode)
    assert mode == 0o600
