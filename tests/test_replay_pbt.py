"""P5 重放幂等属性(M0 出口标准①属性集 4/5,以 WS2 投影重建为地基)。

属性:随机账本历史(kind 混合含 effects 生命周期、payload 尺寸跨 1KB
外置阈值、随机分段/快照配置)经两次 DurableLedger.open() 重放——

1. 内存条目序列三方全等(原始 vs 两次重放);
2. SQLite 投影(ledger_entries + effects)三方全等——重放是纯函数;
3. 重放后继续追加,seq 连续且哈希链完好(重放不改变账本事实的续写能力)。
"""

import os
import shutil
import sqlite3
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from naga.ledger_store import DurableLedger

KINDS = [
    "effect.register",
    "effect.apply_ok",
    "effect.apply_failed",
    "effect.revert",
    "session.open",
    "job.fire",
    "memory.write",
]

ops_strategy = st.lists(
    st.tuples(st.sampled_from(KINDS), st.integers(min_value=0, max_value=1800)),
    min_size=1,
    max_size=10,
)
seg_strategy = st.integers(min_value=2, max_value=5)
snap_strategy = st.sampled_from([0, 3, 4, 5])  # 0 = 中途手动快照


def _payload(kind: str, i: int, blob: str) -> dict:
    if kind == "effect.register":
        return {"token": f"fx_{i:06d}", "desc": f"op-{i}", "compensable": i % 3,
                "owner_kind": "plugin", "owner_id": "f0001"}
    if kind == "effect.revert":
        return {"desc": "r", "ref_seq": max(1, i // 2), "status": "reverted", "ok": True}
    return {"n": i, "blob": blob}


def _dump(db_path: str) -> tuple[list, list]:
    conn = sqlite3.connect(db_path)
    try:
        entries = conn.execute("SELECT * FROM ledger_entries ORDER BY seq").fetchall()
        effects = conn.execute("SELECT * FROM effects ORDER BY token").fetchall()
    finally:
        conn.close()
    return entries, effects


class TestReplayIdempotence:
    @settings(deadline=None, max_examples=60)
    @given(ops=ops_strategy, seg=seg_strategy, snap=snap_strategy)
    def test_double_replay_same_entries_and_projection(self, ops, seg, snap):
        home = tempfile.mkdtemp()
        try:
            led = DurableLedger(home, segment_max=seg, snapshot_every=snap)
            for i, (kind, blob_size) in enumerate(ops, start=1):
                led.append(kind, "f0001", _payload(kind, i, "x" * blob_size))
                if snap == 0 and i == len(ops) // 2:
                    led.snapshot()
            db = os.path.join(home, "ledger", "index.sqlite")
            orig_entries, orig_rows = led.entries, _dump(db)
            led.close()

            r1 = DurableLedger.open(home, segment_max=seg, snapshot_every=snap)
            e1, rows1 = r1.entries, _dump(db)
            assert r1.verify() and r1.last_recovery["entries"] == len(ops)
            r1.close()

            r2 = DurableLedger.open(home, segment_max=seg, snapshot_every=snap)
            e2, rows2 = r2.entries, _dump(db)
            assert r2.verify()
            r2.close()

            assert orig_entries == e1 == e2
            assert orig_rows == rows1 == rows2

            # 重放不改变续写能力:再开一次并追加,seq 连续且链完好
            r3 = DurableLedger.open(home, segment_max=seg, snapshot_every=snap)
            extra = r3.append("session.open", "f0001", {"tail": True})
            assert extra["seq"] == len(ops) + 1 and r3.verify()
            r3.close()
        finally:
            shutil.rmtree(home, ignore_errors=True)
