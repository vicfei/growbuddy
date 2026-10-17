"""WS2 持久化与恢复测试:往返 / CAS 外置 / 旁路路由 / 投影重建 / effects 生命周期 /
快照恢复 / 分段 Merkle / 断写截断 / 内核集成。"""

import asyncio
import json
import os
import sqlite3

from naga import kernel
from naga.ledger_store import CAS, DurableLedger, _merkle_root


def _rows(db_path: str, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_roundtrip_small_and_large_payloads(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    small = led.append("plugin.install", "f0001", {"name": "x", "v": 1})
    big = led.append("effect.register", "f0001",
                     {"token": "fx_000002", "desc": "big", "blob": "y" * 4096})
    led.close()

    led2 = DurableLedger.open(home)
    assert [e["seq"] for e in led2.entries] == [1, 2]
    assert led2.entries[0]["payload"] == small["payload"]
    assert led2.entries[1]["payload"] == big["payload"]
    assert led2.verify()
    assert led2.last_recovery == {"snapshot_seq": 0, "truncated": 0, "entries": 2, "projected": 2}
    led2.close()


def test_cas_externalization_inline_threshold(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    led.append("plugin.install", "f0001", {"blob": "z" * 4096})
    led.close()

    with open(os.path.join(home, "ledger", "head.log"), encoding="utf-8") as fh:
        text = fh.read()
    assert "payload_cas" in text and "zzzz" not in text
    line = json.loads(text.strip())
    blob = os.path.join(home, "assets", "blobs",
                        line["payload_cas"][7:9], line["payload_cas"][9:11], line["payload_cas"][7:])
    assert os.path.isfile(blob)


def test_cas_sidecar_routing(tmp_path):
    home = str(tmp_path / "home")
    cas = CAS(home, sidecar_min=10)
    data = b"x" * 100
    cas_id = cas.put(data)
    with open(cas._blob_path(cas_id[7:]), "rb") as fh:
        ptr = json.loads(fh.read())
    assert ptr["sidecar"] is True
    assert os.path.isfile(os.path.join(home, ptr["path"]))
    assert cas.get(cas_id) == data
    assert cas.put(b"x" * 100) == cas_id  # 内容寻址幂等


def test_projection_effects_lifecycle(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    led.append("effect.register", "f0001", {"token": "fx_000001", "owner_kind": "plugin",
                                            "owner_id": "f0001", "desc": "demo",
                                            "compensable": 1})
    led.append("effect.revert", "f0001", {"desc": "demo", "ref_seq": 1,
                                          "status": "compensated", "ok": True})
    led.append("effect.register", "f0001", {"token": "fx_000003", "desc": "doomed",
                                            "compensable": 0})
    led.append("effect.apply_failed", "f0001", {"token": "fx_000003", "desc": "doomed",
                                                "error": "ZeroDivisionError"})
    led.close()
    rows = _rows(os.path.join(home, "ledger", "index.sqlite"),
                 "SELECT token, status, revert_seq, compensable FROM effects ORDER BY token")
    assert rows == [("fx_000001", "compensated", 2, 1), ("fx_000003", "failed", None, 0)]


def test_projection_rebuild_from_scratch(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    for i in range(5):
        led.append("effect.register", "f0001", {"token": f"fx_{i}", "desc": str(i)})
    led.close()
    db = os.path.join(home, "ledger", "index.sqlite")
    before = _rows(db, "SELECT * FROM ledger_entries")

    os.remove(db)  # 投影可重建(数据模型 §7):删库重开即全量重放
    led2 = DurableLedger.open(home)
    after = _rows(db, "SELECT * FROM ledger_entries")
    assert before == after
    led2.close()


def test_snapshot_recovery(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home, snapshot_every=0)  # 关闭自动触发,手动快照
    for i in range(3):
        led.append("session.open", "root", {"n": i})
    snap_seq = led.snapshot()
    assert snap_seq == 3
    for i in range(3, 6):
        led.append("session.open", "root", {"n": i})
    led.close()

    assert os.path.isfile(os.path.join(home, "ledger", "snapshots", "snapshot_000000000003.tar.zst"))
    led2 = DurableLedger.open(home, snapshot_every=0)
    assert led2.last_recovery["snapshot_seq"] == 3
    assert led2.last_recovery["projected"] == 3  # 仅重放快照之后的 3 条
    assert len(led2.entries) == 6 and led2.verify()
    assert _rows(os.path.join(home, "ledger", "index.sqlite"),
                 "SELECT COUNT(*) FROM ledger_entries")[0][0] == 6
    led2.close()


def test_segment_sealing_with_merkle(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home, segment_max=3)
    for i in range(7):
        led.append("job.fire", "root", {"i": i})
    led.close()

    segs = sorted(n for n in os.listdir(os.path.join(home, "ledger", "segments"))
                  if n.endswith(".log"))
    assert segs == ["000000000001-000000000003.log", "000000000004-000000000006.log"]
    with open(os.path.join(home, "ledger", "segments", segs[0] + ".seal")) as fh:
        seal = json.load(fh)
    hashes = [e["hash"] for e in led.entries[0:3]]
    assert seal["merkle_root"] == _merkle_root(hashes)
    with open(os.path.join(home, "ledger", "head.log"), encoding="utf-8") as fh:
        assert fh.read().count("\n") == 1

    led2 = DurableLedger.open(home, segment_max=3)
    assert len(led2.entries) == 7 and led2.verify()  # 分段 + head 拼接重放
    led2.close()


def test_torn_tail_truncated_on_open(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    for i in range(3):
        led.append("session.open", "root", {"i": i})
    led.close()
    with open(os.path.join(home, "ledger", "head.log"), "a", encoding="utf-8") as fh:
        fh.write('{"seq": 4, "tras')  # 模拟崩溃期断写(无换行)

    led2 = DurableLedger.open(home)
    assert led2.last_recovery["truncated"] == 1
    assert len(led2.entries) == 3 and led2.verify()
    e4 = led2.append("session.open", "root", {"i": 3})  # 截断后可继续追加
    assert e4["seq"] == 4
    led2.close()


def test_kernel_integration(tmp_path):
    home = str(tmp_path / "home")

    async def scenario():
        led = DurableLedger(home)
        k = kernel.Kernel(ledger=led)
        f = await k.load(kernel.Plugin(kernel.counter_plugin))
        await k.unload(f)
        await k.shutdown()
        led.close()

    asyncio.run(scenario())

    led2 = DurableLedger.open(home)
    assert led2.verify()
    kinds = {e["kind"] for e in led2.entries}
    assert "fiber.activated" in kinds and "effect.register" in kinds
    effects = _rows(os.path.join(home, "ledger", "index.sqlite"),
                    "SELECT status FROM effects")
    assert effects and all(r[0] in ("reverted", "applied") for r in effects)
    led2.close()
