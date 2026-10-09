"""WS2 持久化与恢复:head.log / segments / snapshots / SQLite 投影 / CAS。

数据模型设计文档 §2/§3/§7 的 M0 落地。DurableLedger 与种子 kernel.Ledger
鸭子类型兼容(append/entries/verify/close),可直接经 Kernel(ledger=...)
挂载,不 import 内核模块。磁盘格式一行一 JSON;payload 大于
ledger_inline_payload_max_bytes 时外置 CAS,行内存 payload_hash/payload_cas
(§3.1)。entry_hash 语义与种子一致:对含 payload 的完整条目 canonical JSON
计算;重放时逐行重算校验哈希链。

边界(见 issue #12):segment 条数上限为构造参数;plugins 表投影与快照内
refs 随后续批次。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import tarfile
import time
from typing import Any, TextIO

import zstandard
from pydantic import BaseModel, ConfigDict, ValidationError

from naga import storage

# 值来自 docs/design/spec-constants.yaml(authority 注明);代码常量模块化待后续批次
INLINE_PAYLOAD_MAX_BYTES = 1024           # ledger_inline_payload_max_bytes / 数据模型 §3.1
SNAPSHOT_EVERY_ENTRIES = 100_000          # ledger_snapshot_every_entries / 数据模型 §3.2
CAS_SIDECAR_MIN_BYTES = 64 * 1024 * 1024  # cas_sidecar_min_bytes / K.7
SEGMENT_MAX_ENTRIES = 10_000              # 未入注册表,见 issue #12 边界 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
  seq INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_json TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref_json TEXT,
  payload_hash TEXT NOT NULL,
  payload_cas TEXT,
  prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger_entries(kind, seq);
CREATE TABLE IF NOT EXISTS effects (
  token TEXT PRIMARY KEY,
  plugin_id TEXT,
  owner_kind TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  desc TEXT NOT NULL,
  apply_seq INTEGER NOT NULL,
  revert_seq INTEGER,
  compensable INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('applied','reverted','compensated','failed'))
);
CREATE TABLE IF NOT EXISTS skill_nodes (
  cas_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  parent_cas TEXT,
  branch TEXT NOT NULL DEFAULT 'main',
  status TEXT NOT NULL CHECK (status IN ('active','stale','archived','quarantined')),
  provenance TEXT NOT NULL,
  metrics_json TEXT,
  size_bytes INTEGER NOT NULL CHECK (size_bytes <= 15360)
);
CREATE INDEX IF NOT EXISTS idx_skill_lookup ON skill_nodes(name, branch, status);
CREATE TABLE IF NOT EXISTS traces (      -- M1 SQLite 镜像(Parquet 主存随 M2/M4,见 issue #23)
  trace_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  agent_id TEXT,
  outcome TEXT NOT NULL,
  failure_analysis TEXT,
  cost_cents INTEGER NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  task_cluster TEXT,
  replay_bundle_cas TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (  -- GAP8:副作用方法去重缓存(24h 级)
  key TEXT PRIMARY KEY,
  method TEXT NOT NULL,
  first_seq INTEGER NOT NULL,
  result_cas TEXT,
  created_at TEXT NOT NULL
);
"""

_SNAP_RE = re.compile(r"^snapshot_(\d+)\.tar\.zst$")


class LedgerLine(BaseModel):
    """磁盘行 schema(协议边界:head.log / segments 的一行,Naga 纪律③)。

    小 payload 内联(payload 字段),大 payload 外置(payload_cas + payload_hash);
    extra=forbid——未知字段在边界处拒绝而非静默透传。
    """

    model_config = ConfigDict(extra="forbid")

    seq: int
    ts: float
    kind: str
    owner: str
    payload: dict[str, Any] | None = None
    payload_hash: str
    payload_cas: str | None = None
    prev_hash: str
    hash: str


class SidecarPointer(BaseModel):
    """K.7 旁路指针对象(blobs 中代替本体的内容)。"""

    model_config = ConfigDict(extra="ignore")

    sidecar: bool = False
    path: str
    size: int
    sha256: str


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).isoformat()


def _merkle_root(hashes: list[str]) -> str:
    """段内 Merkle 根(奇数层复制末叶)。"""
    level = [h.encode() for h in hashes]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest()
                 for i in range(0, len(level), 2)]
    return level[0].hex()


class CAS:
    """内容寻址对象库(§2 assets/)。> sidecar_min 走 K.7 旁路:blobs 存指针对象。"""

    def __init__(self, home: str, sidecar_min: int = CAS_SIDECAR_MIN_BYTES):
        self._home = home
        self._sidecar_min = sidecar_min
        self._blobs = os.path.join(home, "assets", "blobs")
        self._sidecar = os.path.join(home, "assets", "sidecar")

    def _blob_path(self, hexd: str) -> str:
        return os.path.join(self._blobs, hexd[:2], hexd[2:4], hexd)

    def put(self, data: bytes) -> str:
        hexd = hashlib.sha256(data).hexdigest()
        if len(data) > self._sidecar_min:
            rel = os.path.join("assets", "sidecar", f"{hexd}.bin")
            side_path = os.path.join(self._home, rel)
            os.makedirs(os.path.dirname(side_path), exist_ok=True)
            if not os.path.exists(side_path):
                _write_atomic(side_path, data)
            data = _canonical({"sidecar": True, "path": rel,
                               "size": len(data), "sha256": hexd}).encode()
        return self._store(data)

    def get(self, cas_id: str) -> bytes:
        hexd = cas_id.removeprefix("sha256:")
        data = _read(self._blob_path(hexd))
        try:
            pointer = SidecarPointer.model_validate_json(data)
        except ValidationError:
            return data  # 普通对象:本体即内容
        if pointer.sidecar:
            raw = _read(os.path.join(self._home, pointer.path))
            if hashlib.sha256(raw).hexdigest() != pointer.sha256:
                raise ValueError(f"sidecar hash mismatch: {cas_id}")
            return raw
        return data

    def _store(self, data: bytes) -> str:
        hexd = hashlib.sha256(data).hexdigest()
        path = self._blob_path(hexd)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write_atomic(path, data)
        return f"sha256:{hexd}"


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class DurableLedger:
    """种子 Ledger 的持久化形态:追加 head.log + 投影 index.sqlite。"""

    GENESIS = "0" * 64

    def __init__(self, home: str | None = None, *,
                 inline_max: int = INLINE_PAYLOAD_MAX_BYTES,
                 segment_max: int = SEGMENT_MAX_ENTRIES,
                 snapshot_every: int = SNAPSHOT_EVERY_ENTRIES,
                 sidecar_min: int = CAS_SIDECAR_MIN_BYTES):
        self.home = storage.init_home(home)
        self._ledger_dir = os.path.join(self.home, "ledger")
        self._head_path = os.path.join(self._ledger_dir, "head.log")
        self._segments_dir = os.path.join(self._ledger_dir, "segments")
        self._snapshots_dir = os.path.join(self._ledger_dir, "snapshots")
        self._db_path = os.path.join(self._ledger_dir, "index.sqlite")
        self._inline_max = inline_max
        self._segment_max = segment_max
        self._snapshot_every = snapshot_every
        self.cas = CAS(self.home, sidecar_min)
        self._entries: list[dict[str, Any]] = []
        self._head_count = 0
        self._snapshot_seq = 0
        self.last_recovery: dict[str, int] = {}
        # append-only 日志句柄随实例存续,close() 统一回收
        self._fh: TextIO = open(self._head_path, "a", encoding="utf-8")  # noqa: SIM115
        self._db: sqlite3.Connection = sqlite3.connect(self._db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ---- 写路径 ----
    def append(self, kind: str, owner: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "seq": len(self._entries) + 1,
            "ts": time.time(),
            "kind": kind,
            "owner": owner,
            "payload": payload,
            "prev_hash": self._entries[-1]["hash"] if self._entries else self.GENESIS,
        }
        entry["hash"] = hashlib.sha256(_canonical(entry).encode()).hexdigest()
        self._entries.append(entry)

        pbytes = _canonical(payload).encode()
        line: dict[str, Any] = {k: entry[k] for k in ("seq", "ts", "kind", "owner")}
        line["payload_hash"] = hashlib.sha256(pbytes).hexdigest()
        cas_id: str | None = None
        if len(pbytes) > self._inline_max:
            cas_id = self.cas.put(pbytes)
            line["payload_cas"] = cas_id
        else:
            line["payload"] = payload
        line["prev_hash"] = entry["prev_hash"]
        line["hash"] = entry["hash"]
        self._fh.write(_canonical(line) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._head_count += 1

        self._project(entry, cas_id)
        if self._head_count >= self._segment_max:
            self._seal()
        if self._snapshot_every and len(self._entries) % self._snapshot_every == 0:
            self.snapshot()
        return entry

    # ---- 读路径 ----
    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def verify(self) -> bool:
        prev = self.GENESIS
        for e in self._entries:
            if e["prev_hash"] != prev:
                return False
            body = {k: e[k] for k in ("seq", "ts", "kind", "owner", "payload", "prev_hash")}
            if hashlib.sha256(_canonical(body).encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True

    # ---- 分段与快照 ----
    def _seal(self) -> None:
        end = len(self._entries)
        start = end - self._head_count + 1
        self._fh.close()
        seg = f"{start:012d}-{end:012d}.log"
        os.replace(self._head_path, os.path.join(self._segments_dir, seg))
        hashes = [e["hash"] for e in self._entries[start - 1:end]]
        seal = {"start": start, "end": end, "count": len(hashes),
                "merkle_root": _merkle_root(hashes)}
        with open(os.path.join(self._segments_dir, seg + ".seal"), "w", encoding="utf-8") as fh:
            fh.write(_canonical(seal))
        self._fh = open(self._head_path, "a", encoding="utf-8")  # noqa: SIM115
        self._head_count = 0

    def snapshot(self) -> int:
        """生成 snapshot_<seq>.tar.zst(内容:index.sqlite 的 VACUUM 副本)。"""
        self._db.commit()
        seq = len(self._entries)
        tmp_db = os.path.join(self._snapshots_dir, f".index.{seq}.tmp")
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
        self._db.execute("VACUUM INTO ?", (tmp_db,))
        tmp_tar = os.path.join(self._snapshots_dir, f".snapshot_{seq}.tmp")
        cctx = zstandard.ZstdCompressor()
        cctx = zstandard.ZstdCompressor()
        with open(tmp_tar, "wb") as raw, cctx.stream_writer(raw) as zf, \
                tarfile.open(fileobj=zf, mode="w|") as tf:
            tf.add(tmp_db, arcname="index.sqlite")
        os.replace(tmp_tar, os.path.join(self._snapshots_dir, f"snapshot_{seq:012d}.tar.zst"))
        os.remove(tmp_db)
        self._snapshot_seq = seq
        return seq

    # ---- 投影 ----
    def _project(self, entry: dict[str, Any], payload_cas: str | None) -> None:
        actor = _canonical({"id": entry["owner"]})
        pbytes = _canonical(entry["payload"]).encode()
        self._db.execute(
            "INSERT OR REPLACE INTO ledger_entries VALUES (?,?,?,?,?,?,?,?,?)",
            (entry["seq"], _iso(entry["ts"]), actor, entry["kind"], None,
             hashlib.sha256(pbytes).hexdigest(), payload_cas,
             entry["prev_hash"], entry["hash"]))
        p = entry["payload"]
        k = entry["kind"]
        if k in ("effect.register", "effect.apply_begin") and "token" in p:
            self._db.execute(
                "INSERT OR REPLACE INTO effects VALUES (?,?,?,?,?,?,?,?,?)",
                (p["token"], None, p.get("owner_kind", "plugin"),
                 p.get("owner_id", entry["owner"]), p.get("desc", ""),
                 entry["seq"], None, int(p.get("compensable", 0)), "applied"))
        elif k == "effect.apply_failed" and "token" in p:
            self._db.execute("UPDATE effects SET status='failed' WHERE token=?", (p["token"],))
        elif k == "effect.revert" and "ref_seq" in p:
            self._db.execute("UPDATE effects SET status=?, revert_seq=? WHERE apply_seq=?",
                             (p.get("status", "reverted"), entry["seq"], p["ref_seq"]))
        elif k == "skill.commit":
            self._db.execute(
                "INSERT OR REPLACE INTO skill_nodes VALUES (?,?,?,?,?,?,?,?,?)",
                (p["cas_id"], p["name"], p["version"], p.get("parent_cas"),
                 p.get("branch", "main"), p.get("status", "active"),
                 p["provenance"] if isinstance(p["provenance"], str)
                 else _canonical(p["provenance"]),
                 p.get("metrics_json") or _canonical(p.get("metrics") or {"uses": 0}),
                 p["size_bytes"]))
        elif k == "skill.quarantine" and "cas_id" in p:
            self._db.execute("UPDATE skill_nodes SET status='quarantined' WHERE cas_id=?",
                             (p["cas_id"],))
        elif k == "skill.use" and "cas_id" in p:
            row = self._db.execute("SELECT metrics_json FROM skill_nodes WHERE cas_id=?",
                                   (p["cas_id"],)).fetchone()
            metrics: dict[str, Any] = json.loads(row[0]) if row and row[0] else {}
            metrics["uses"] = int(metrics.get("uses", 0)) + 1
            metrics["lastUsed"] = _iso(entry["ts"])  # 以条目 ts 为准——重放确定性
            self._db.execute("UPDATE skill_nodes SET metrics_json=? WHERE cas_id=?",
                             (_canonical(metrics), p["cas_id"]))
        elif k == "trace.record":
            cost: dict[str, Any] = p.get("cost") or {}
            self._db.execute(
                "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?)",
                (p["trace_id"], p["session_key"], p.get("agent_id"), p["outcome"],
                 p.get("failure_analysis"), int(cost.get("usd_cents", 0)),
                 int(cost.get("tokens", 0)), int(p.get("duration_ms", 0)),
                 p.get("task_cluster"), p.get("replay_bundle_cas")))
        self._db.commit()

    # ---- 恢复 ----
    @classmethod
    def open(cls, home: str | None = None, **cfg: int) -> DurableLedger:
        led = cls(home, **cfg)
        led._fh.close()
        led._db.close()
        snap_seq = led._restore_latest_snapshot()
        pairs, truncated, head_lines = led._load_lines()
        if truncated:
            led._rewrite_head_without_tail()
        led._entries = [e for e, _ in pairs]
        led._fh = open(led._head_path, "a", encoding="utf-8")  # noqa: SIM115
        led._head_count = head_lines
        led._db = sqlite3.connect(led._db_path)
        led._db.execute("PRAGMA journal_mode=WAL")
        led._db.executescript(_SCHEMA)
        if snap_seq == 0:
            led._db.execute("DELETE FROM ledger_entries")
            led._db.execute("DELETE FROM effects")
        projected = 0
        for e, cas in pairs:
            if e["seq"] > snap_seq:
                led._project(e, cas)
                projected += 1
        led._db.commit()
        led.last_recovery = {"snapshot_seq": snap_seq, "truncated": truncated,
                             "entries": len(pairs), "projected": projected}
        return led

    def _restore_latest_snapshot(self) -> int:
        best_seq, best_name = 0, None
        for name in os.listdir(self._snapshots_dir):
            m = _SNAP_RE.match(name)
            if m and int(m.group(1)) > best_seq:
                best_seq, best_name = int(m.group(1)), name
        if best_name is None:
            return 0
        # r| 流模式下 extract 需 backward seek,而 zstd 流不可寻址——改为
        # 迭代式前向抽取:命中成员即流式落盘。
        dctx = zstandard.ZstdDecompressor()
        target = os.path.join(self._ledger_dir, "index.sqlite")
        with open(os.path.join(self._snapshots_dir, best_name), "rb") as raw, \
                dctx.stream_reader(raw) as zf, tarfile.open(fileobj=zf, mode="r|") as tf:
            for member in tf:
                if member.name == "index.sqlite":
                    src = tf.extractfile(member)
                    if src is None:
                        raise ValueError(f"snapshot member not a file: {best_name}")
                    with open(target, "wb") as out:
                        while chunk := src.read(1 << 16):
                            out.write(chunk)
                    break
            else:
                raise ValueError(f"snapshot missing index.sqlite: {best_name}")
        for suffix in ("-wal", "-shm"):
            stale = self._db_path + suffix
            if os.path.exists(stale):
                os.remove(stale)
        return best_seq

    def _load_lines(self) -> tuple[list[tuple[dict[str, Any], str | None]], int, int]:
        """读 segments + head.log,重放校验;返回 (条目, cas) 对、断写数、head 行数。

        断写容忍仅限 head.log 尾行(无换行即截断);分段为封存态,损坏属硬失败,
        由链校验(seq 连续/prev 链/条目哈希)以 ValueError 抛出。逐行过
        LedgerLine 模型(磁盘→内存即协议边界,Naga 纪律③)。
        """
        seg_files = sorted(
            os.path.join(self._segments_dir, n)
            for n in os.listdir(self._segments_dir) if n.endswith(".log"))
        files = [*seg_files, self._head_path]
        pairs: list[tuple[dict[str, Any], str | None]] = []
        truncated = 0
        head_lines = 0
        for i, path in enumerate(files):
            is_head = i == len(files) - 1
            text = _read(path).decode("utf-8") if os.path.exists(path) else ""
            if text and not text.endswith("\n"):
                text = text[:text.rfind("\n") + 1]
                if is_head:
                    truncated += 1
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if is_head:
                head_lines = len(lines)
            for ln in lines:
                line: dict[str, Any] = LedgerLine.model_validate_json(ln).model_dump()
                entry = {k: line[k] for k in ("seq", "ts", "kind", "owner", "prev_hash", "hash")}
                if line["payload"] is not None:
                    entry["payload"] = line["payload"]
                    cas: str | None = None
                else:
                    if line["payload_cas"] is None:
                        raise ValueError(f"line without payload or payload_cas at seq {line['seq']}")
                    raw = self.cas.get(line["payload_cas"])
                    entry["payload"] = json.loads(raw)
                    if hashlib.sha256(_canonical(entry["payload"]).encode()).hexdigest() \
                            != line["payload_hash"]:
                        raise ValueError(f"payload hash mismatch at seq {line['seq']}")
                    cas = line["payload_cas"]
                expected_prev = pairs[-1][0]["hash"] if pairs else self.GENESIS
                if entry["seq"] != len(pairs) + 1 or entry["prev_hash"] != expected_prev:
                    raise ValueError(f"chain break at seq {entry['seq']}")
                body = {k: entry[k] for k in ("seq", "ts", "kind", "owner", "payload", "prev_hash")}
                if hashlib.sha256(_canonical(body).encode()).hexdigest() != entry["hash"]:
                    raise ValueError(f"entry hash mismatch at seq {entry['seq']}")
                pairs.append((entry, cas))
        return pairs, truncated, head_lines

    def _rewrite_head_without_tail(self) -> None:
        text = _read(self._head_path).decode("utf-8", errors="ignore")
        with open(self._head_path, "wb") as fh:
            fh.write(text[:text.rfind("\n") + 1].encode("utf-8"))

    def close(self) -> None:
        self._db.commit()
        self._db.close()
        self._fh.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """投影只读查询(SQLite 镜像;真相在账本,此为便捷入口)。"""
        cur = self._db.execute(sql, params)
        return cur.fetchall()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """缓存类表写入入口(idempotency_keys 等,不经账本;见 issue #27 声明)。"""
        self._db.execute(sql, params)
        self._db.commit()
