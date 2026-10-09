"""最小 L2 技能资产测试:往返 / 版本树 / 15KB / quarantine / 删库重建。"""

import os

from naga.ledger_store import DurableLedger
from naga.skills import SKILL_MAX_BYTES, SkillStore

_SKILL_MD = """---
name: pricing-snapshot-diff
---

# 何时使用
竞品定价对比任务。

# 步骤
1. 抓取定价页
2. 生成差异表

# 已知陷阱
- 页面缓存导致旧价格。
"""


def _store(home: str) -> tuple[DurableLedger, SkillStore]:
    led = DurableLedger(home)
    return led, SkillStore(led)


def test_commit_get_roundtrip_with_stamped_frontmatter(tmp_path):
    led, store = _store(str(tmp_path / "home"))
    prov = {"source": "agent", "trace_id": "tr_001"}
    cas = store.commit("pricing-snapshot-diff", _SKILL_MD, prov)
    assert cas.startswith("sha256:")

    got = store.get("pricing-snapshot-diff")
    assert got is not None
    meta, content = got
    assert meta.name == "pricing-snapshot-diff" and meta.version == 1
    assert meta.status == "active" and meta.provenance == prov
    assert "# 步骤" in content and "1. 抓取定价页" in content
    led.close()


def test_version_tree_increments_with_parent_link(tmp_path):
    led, store = _store(str(tmp_path / "home"))
    cas1 = store.commit("svc", _SKILL_MD.replace("pricing-snapshot-diff", "svc"), {"source": "agent"})
    cas2 = store.commit("svc", _SKILL_MD.replace("pricing-snapshot-diff", "svc"), {"source": "agent"})
    rows = led.query("SELECT cas_id, version, parent_cas FROM skill_nodes "
                     "WHERE name='svc' ORDER BY version")
    assert [(r[1], r[2]) for r in rows] == [(1, None), (2, cas1)]
    assert rows[1][0] == cas2
    meta, _ = store.get("svc")
    assert meta.version == 2
    led.close()


def test_size_limit_rejected(tmp_path):
    led, store = _store(str(tmp_path / "home"))
    huge = _SKILL_MD.replace("pricing-snapshot-diff", "huge").replace(
        "竞品定价对比任务。", "竞品定价对比任务。" + "x" * SKILL_MAX_BYTES)
    try:
        store.commit("huge", huge, {"source": "agent"})
        raise AssertionError("超限技能必须被拒")
    except ValueError as err:
        assert "15360" in str(err)
    assert led.query("SELECT COUNT(*) FROM skill_nodes")[0][0] == 0
    led.close()


def test_name_mismatch_and_bad_frontmatter_rejected(tmp_path):
    led, store = _store(str(tmp_path / "home"))
    try:
        store.commit("other", _SKILL_MD, {"source": "agent"})
        raise AssertionError("name 不符必须被拒")
    except ValueError:
        pass
    try:
        store.commit("bad", "no frontmatter here", {"source": "agent"})
        raise AssertionError("缺 frontmatter 必须被拒")
    except ValueError:
        pass
    led.close()


def test_quarantine_flips_status(tmp_path):
    led, store = _store(str(tmp_path / "home"))
    store.commit("svc", _SKILL_MD.replace("pricing-snapshot-diff", "svc"), {"source": "agent"})
    assert store.quarantine("svc", "含绕过审批指令") is not None
    assert store.get("svc") is None                      # active 视角不可见
    assert store.get("svc", status="quarantined") is not None
    assert [r[1] for r in store.list()] == []            # active 清单排除
    assert [r[1] for r in store.list(status="quarantined")] == ["svc"]
    led.close()


def test_projection_rebuild_from_scratch(tmp_path):
    home = str(tmp_path / "home")
    led, store = _store(home)
    store.commit("a", _SKILL_MD.replace("pricing-snapshot-diff", "a"), {"source": "agent"})
    store.commit("b", _SKILL_MD.replace("pricing-snapshot-diff", "b"), {"source": "human"})
    store.quarantine("b", "测试隔离")
    rows = led.query("SELECT * FROM skill_nodes ORDER BY name")
    led.close()

    db = os.path.join(home, "ledger", "index.sqlite")
    os.remove(db)
    led2 = DurableLedger.open(home)
    assert led2.query("SELECT * FROM skill_nodes ORDER BY name") == rows
    store2 = SkillStore(led2)
    meta, content = store2.get("a")
    assert meta.version == 1 and "# 步骤" in content
    assert store2.get("b") is None  # 隔离状态在重建后保持
    led2.close()
