"""WS4 崩溃恢复模糊(M0 出口标准②)。

子进程(tests/_fuzz_child.py)按随机配置跑负载后模拟崩溃;父进程重开断言:
条目数 == 崩溃前已提交数(append 逐条 fsync)、哈希链完好、断写自报截断、
投影恢复路径 ≡ 从零重建路径、续写 seq 连续;tamper 负对照必须 ValueError。

轮数由 SAMSARA_FUZZ_ITERS 控制(CI 默认 40,四模式均衡);达标线
≥1000 次零不一致,深跑取证记入 PR 与登记册。
"""

import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

from naga.ledger_store import DurableLedger

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHILD = os.path.join(_HERE, "_fuzz_child.py")
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
ITERS = int(os.environ.get("SAMSARA_FUZZ_ITERS", "40"))
MODES = ("hard", "torn", "midappend", "tamper")


def _child(home: str, seed: int, crash_at: int, seg: int, snap: int, mode: str) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(
        [sys.executable, _CHILD, home, str(seed), str(crash_at), str(seg), str(snap), mode],
        env=env, check=False, timeout=60)


def _dump(db: str) -> tuple[list, list]:
    conn = sqlite3.connect(db)
    try:
        return (conn.execute("SELECT * FROM ledger_entries ORDER BY seq").fetchall(),
                conn.execute("SELECT * FROM effects ORDER BY token").fetchall())
    finally:
        conn.close()


def _remove_db(home: str) -> None:
    db = os.path.join(home, "ledger", "index.sqlite")
    for suffix in ("", "-wal", "-shm"):
        path = db + suffix
        if os.path.exists(path):
            os.remove(path)


def test_crash_recovery_fuzz():
    stats = {m: 0 for m in MODES}
    for it in range(ITERS):
        rng = random.Random(it)
        mode = MODES[it % len(MODES)]  # 均衡覆盖四模式
        crash_at = rng.randint(1, 12)
        seg = rng.randint(2, 5)
        snap = rng.choice([0, 3, 4, 5])
        tag = f"iter={it} mode={mode} crash_at={crash_at} seg={seg} snap={snap}"
        home = tempfile.mkdtemp()
        try:
            _child(home, it, crash_at, seg, snap, mode)
            stats[mode] += 1
            db = os.path.join(home, "ledger", "index.sqlite")

            if mode == "tamper":  # 负对照:损伤必须被精确拒绝,不得静默通过
                with pytest.raises(ValueError):
                    DurableLedger.open(home, segment_max=seg, snapshot_every=snap)
                continue

            led = DurableLedger.open(home, segment_max=seg, snapshot_every=snap)
            assert len(led.entries) == crash_at, f"{tag}: {len(led.entries)} 条"
            assert led.verify(), tag
            assert led.last_recovery["truncated"] == (1 if mode == "torn" else 0), tag
            rows = _dump(db)
            led.close()

            _remove_db(home)  # 从零重建路径:删投影后重放,必须与恢复路径全等
            led2 = DurableLedger.open(home, segment_max=seg, snapshot_every=snap)
            assert _dump(db) == rows, f"{tag}: 投影重建不一致"
            extra = led2.append("session.open", "f0001", {"tail": True})
            assert extra["seq"] == crash_at + 1 and led2.verify(), f"{tag}: 续写失败"
            led2.close()
        finally:
            shutil.rmtree(home, ignore_errors=True)

    assert all(v > 0 for v in stats.values()), f"模式覆盖缺口: {stats}"
