"""WS4 崩溃恢复模糊的子进程载体:跑随机负载后按模式模拟崩溃。

由 tests/test_crash_fuzz.py 以独立进程拉起——os._exit 绕过一切清理路径,
等价 kill -9 语义(文件系统状态保留);本文件不被 pytest 收集。

用法:
    python tests/_fuzz_child.py <home> <seed> <crash_at> <seg> <snap> <mode>

mode:
    hard       逐条 fsync 的 append 完成后硬退
    torn       追加断写半行(无换行,显式 flush 模拟半途落盘)后硬退
    midappend  行已 fsync、SQLite 投影前硬退(补丁在 _project 入口拦截)
    tamper     干净关闭后篡改随机行 owner(不改哈希)——负对照
"""

import json
import os
import random
import sys

from naga.ledger_store import DurableLedger

KINDS = [
    "effect.register",
    "effect.apply_ok",
    "effect.apply_failed",
    "effect.revert",
    "session.open",
    "job.fire",
    "job.missed",
]


def _payload(rng: random.Random, kind: str, i: int) -> dict:
    if kind == "effect.register":
        return {"token": f"fx_{i:06d}", "desc": f"op-{i}", "compensable": rng.randrange(3),
                "owner_kind": "plugin", "owner_id": "f0001"}
    if kind == "effect.revert":
        return {"desc": "r", "ref_seq": rng.randint(1, i), "status": "reverted", "ok": True}
    return {"n": i, "blob": "x" * rng.randrange(0, 1800)}


def main() -> None:
    home, seed, crash_at, seg, snap, mode = sys.argv[1:7]
    seed, crash_at, seg, snap = int(seed), int(crash_at), int(seg), int(snap)
    rng = random.Random(seed)

    if mode == "midappend":
        import naga.ledger_store as ls

        orig = ls.DurableLedger._project
        state = {"n": 0}

        def patched(self, entry, payload_cas):
            state["n"] += 1
            if state["n"] >= crash_at:
                os._exit(9)  # 该行已 fsync 为事实,投影前死亡
            orig(self, entry, payload_cas)

        ls.DurableLedger._project = patched

    led = DurableLedger(home, segment_max=seg, snapshot_every=snap)
    for i in range(1, crash_at + 1):
        led.append(rng.choice(KINDS), "f0001", _payload(rng, KINDS[i % len(KINDS)], i))
        if snap == 0 and i == max(1, crash_at // 2):
            led.snapshot()

    if mode == "torn":
        with open(os.path.join(home, "ledger", "head.log"), "a", encoding="utf-8") as fh:
            fh.write(f'{{"seq": {crash_at + 1}, "tra')
            fh.flush()  # 半行确实到达磁盘(断写可见),但不给换行
        os._exit(9)
    if mode in ("hard", "midappend"):
        os._exit(9)

    # tamper:干净关闭后改一行 owner,哈希不动 → 重开必须检出
    led.close()
    led_dir = os.path.join(home, "ledger")
    candidates = [os.path.join(led_dir, "head.log")]
    segs = os.path.join(led_dir, "segments")
    candidates += [os.path.join(segs, n) for n in os.listdir(segs) if n.endswith(".log")]
    logs = [p for p in candidates if os.path.exists(p) and os.path.getsize(p) > 0]
    victim = rng.choice(logs)
    with open(victim, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    idx = rng.randrange(len(lines))
    line = json.loads(lines[idx])
    line["owner"] = line["owner"] + "!"
    lines[idx] = json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with open(victim, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os._exit(0)


if __name__ == "__main__":
    main()
