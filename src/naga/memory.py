"""记忆系统(主文档 §6.5 + K.6 加密擦除,数据模型 B.3)。

两层记忆:episodic(关键事件摘要,按 sessionKey 分片)/ semantic(用户
偏好与事实,长期)。工作记忆=会话上下文本身,不落库。

- **写入闸门(§6.5)**:semantic 仅 owner 自动写入;guest/known/untrusted
  只允许写 episodic(进 semantic 必须人审——本批形态为直接拒绝,闸门
  单点在 MemoryStore.write)。每条记忆携带 provenance(轨迹/会话/信任级)
  供污染溯源;写入前内容体检(授权类/指令类模式拒绝,§6.5 防御第二层)。
- **加密擦除(K.6)**:payload 一律外置 CAS 且每条记忆独立 DEK
  (AES-256-GCM,AAD 绑定主体),DEK 由 per-subject KEK(AES-KW)封装——
  账本与 CAS 只见密文,遗忘只销毁单条 DEK 不株连同主体其余记忆。
  遗忘=DEK 移入托管库(keys/escrow/,0600,到期 forget_escrow_days=30
  已注册常量);托管期内可回滚(memory.forget.rollback);到期物理销毁,
  账本仅存密文哈希——密码学意义上不可恢复,链完整性不破。
- **读取路径(§6.5)**:确定性关键词召回(与技能检索同构;embedding 召回+
  重排留待嵌入供应方落地)→ 注入上下文;召回计量 memory.use(检索质量
  进 L3 食料)。
- **INC4 裁决(memory.forget 信任级)**:owner 任意;guest 仅自己
  sessionKey(数据主体权利);known 对他人记忆仅提案权(拒绝)。

新增 additive kind:memory.use / memory.stale / memory.archived(RFC 积压,
记研究文档 E';memory.write/forget/forget.rollback 已在冻结 DM §3.1 枚举)。
候选 spec-constants:memory_max_bytes / memory_inject_max_chars /
memory_recall_limit / memory_distill_min_tool_calls /
memory_consolidate_min_recurrence / memory_consolidate_similarity。
"""

from __future__ import annotations

import base64
import datetime
import itertools
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap

from naga.ledger_store import DurableLedger

MEMORY_MAX_BYTES = 16384      # 候选 spec-constants memory_max_bytes
MEMORY_INJECT_MAX_CHARS = 2000  # 候选 memory_inject_max_chars(注入上下文预算)
MEMORY_RECALL_LIMIT = 5       # 候选 memory_recall_limit
MEMORY_DISTILL_MIN_TOOL_CALLS = 1  # 候选 memory_distill_min_tool_calls
MEMORY_CONSOLIDATE_MIN_RECURRENCE = 3  # 候选 memory_consolidate_min_recurrence
MEMORY_CONSOLIDATE_SIMILARITY = 0.5    # 候选 memory_consolidate_similarity
FORGET_ESCROW_DAYS = 30       # spec-constants forget_escrow_days(数据模型 §6)

# §6.5 内容体检:记忆不应包含"以后都直接执行 X"类授权/指令模式
# (与技能体检的 DANGEROUS_PATTERNS 语义平行;独立清单避免 agent↔promotion 环)
MEMORY_POISON_PATTERNS: tuple[str, ...] = (
    "以后都直接", "以后都不用", "以后都不再", "从今以后直接", "从现在起直接",
    "无需确认", "无需审批", "无需人工", "跳过确认", "跳过审批", "跳过安全",
    "自动批准", "自动审批", "永久授权", "完全信任", "最高权限", "管理员权限",
    "直接执行所有", "不再询问",
    "always execute", "without confirmation", "without approval",
    "skip confirmation", "skip approval", "auto-approve", "auto approve",
    "full access", "grant all permissions", "no longer ask",
)

MEMORY_LAYERS = ("episodic", "semantic")  # working=会话上下文本身,不落库

_BLOB_MAGIC = b"NGMEM1"  # 密文对象头:版本化 magic + 12B nonce + AES-GCM ct


class MemoryGateError(ValueError):
    """写入闸门拒绝(§6.5:semantic 非 owner / known 提案 / 越权遗忘)。"""


class MemoryPoisonError(ValueError):
    """内容体检拒绝(§6.5 防御第二层:授权类/指令类模式)。"""


def memory_poison_check(content: str) -> list[str]:
    """返回命中的投毒模式(空=通过);大小写不敏感。"""
    low = content.lower()
    return [p for p in MEMORY_POISON_PATTERNS if p in low or p in content]


def _keywords(text: str) -> list[str]:
    """确定性关键词(与技能检索同构;独立实现避免 agent↔memory 导入环)。"""
    words = [w for w in re.split(r"[^\w]+", text) if len(w) >= 2]
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return words + [a + b for a, b in itertools.pairwise(cjk)]


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data: object = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        return cast("dict[str, Any]", data)
    return {}


def _entry_of(store: dict[str, Any], key: str) -> dict[str, Any] | None:
    """键值必须是映射才有效(None/标量一律按缺失处理)。"""
    value: object = store.get(key)
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


def _save_json(path: str, obj: dict[str, Any]) -> None:
    """原子写 + 0600(K.6:密钥库与 credentials 同级备份纪律)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_canon(obj))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


class MemoryKeyStore:
    """密钥分层(N1 裁决):DEK per-item(精确遗忘粒度)+ KEK per-subject
    (收敛密钥数量)。主体 = session_key(消息层 peer 贯穿到记忆层,§6.5)。

    - keys/memory-keks.json: {subject: b64(KEK)}
    - keys/memory-deks.json: {cas_id: {subject, wrapped}}
    - keys/escrow/memory-escrow.json: {cas_id: {subject, wrapped, expires_at}}
    """

    def __init__(self, home: str):
        self._keys_dir = os.path.join(home, "keys")
        self._escrow_dir = os.path.join(home, "keys", "escrow")
        os.makedirs(self._keys_dir, exist_ok=True)
        os.makedirs(self._escrow_dir, exist_ok=True)
        os.chmod(self._keys_dir, 0o700)
        os.chmod(self._escrow_dir, 0o700)

    @property
    def kek_path(self) -> str:
        return os.path.join(self._keys_dir, "memory-keks.json")

    @property
    def dek_path(self) -> str:
        return os.path.join(self._keys_dir, "memory-deks.json")

    @property
    def escrow_path(self) -> str:
        return os.path.join(self._escrow_dir, "memory-escrow.json")

    def kek_for(self, subject: str) -> bytes:
        keks = _load_json(self.kek_path)
        raw = keks.get(subject)
        if isinstance(raw, str):
            return base64.b64decode(raw)
        kek = os.urandom(32)
        keks[subject] = base64.b64encode(kek).decode("ascii")
        _save_json(self.kek_path, keks)
        return kek

    def wrap_and_store(self, cas_id: str, subject: str, dek: bytes) -> None:
        wrapped = aes_key_wrap(self.kek_for(subject), dek)
        deks = _load_json(self.dek_path)
        deks[cas_id] = {"subject": subject,
                        "wrapped": base64.b64encode(wrapped).decode("ascii")}
        _save_json(self.dek_path, deks)

    def unwrap(self, cas_id: str) -> bytes | None:
        """取回 DEK;不在活跃库(已遗忘/已销毁)→ None。"""
        entry = _entry_of(_load_json(self.dek_path), cas_id)
        if entry is None:
            return None
        try:
            wrapped = base64.b64decode(str(entry.get("wrapped") or ""))
            return aes_key_unwrap(self.kek_for(str(entry.get("subject") or "")),
                                  wrapped)
        except ValueError:
            return None

    def escrow_keys(self, cas_ids: list[str], expires_at: str) -> list[str]:
        """DEK 从活跃库移入托管库(K.6 遗忘语义);返回实际移入者。"""
        deks = _load_json(self.dek_path)
        escrow = _load_json(self.escrow_path)
        moved: list[str] = []
        for cas in cas_ids:
            popped: object = deks.pop(cas, None)
            if isinstance(popped, dict):
                entry = cast("dict[str, Any]", popped)
                entry["expires_at"] = expires_at
                escrow[cas] = entry
                moved.append(cas)
        if moved:
            _save_json(self.dek_path, deks)
            _save_json(self.escrow_path, escrow)
        return moved

    def restore_keys(self, cas_ids: list[str]) -> list[str]:
        """托管期内回滚遗忘:取回未到期密钥;已销毁者不可恢复。"""
        deks = _load_json(self.dek_path)
        escrow = _load_json(self.escrow_path)
        now = datetime.datetime.now(datetime.UTC).isoformat()
        restored: list[str] = []
        for cas in cas_ids:
            entry = _entry_of(escrow, cas)
            if entry is None:
                continue
            if str(entry.get("expires_at") or "") <= now:
                continue  # 已到期(销毁在即/已销毁):密码学上不可恢复
            escrow.pop(cas)
            entry.pop("expires_at", None)
            deks[cas] = entry
            restored.append(cas)
        if restored:
            _save_json(self.dek_path, deks)
            _save_json(self.escrow_path, escrow)
        return restored

    def destroy_expired(self) -> list[str]:
        """到期物理销毁(K.6:密钥删除,账本仅存密文哈希)。"""
        escrow = _load_json(self.escrow_path)
        now = datetime.datetime.now(datetime.UTC).isoformat()
        destroyed: list[str] = []
        for cas in escrow:
            entry = _entry_of(escrow, cas)
            if entry is not None and str(entry.get("expires_at") or "") <= now:
                destroyed.append(cas)
        for cas in destroyed:
            escrow.pop(cas, None)
        if destroyed:
            _save_json(self.escrow_path, escrow)
        return destroyed


@dataclass
class MemoryHit:
    cas_id: str
    layer: str
    content: str


@dataclass
class ForgetReport:
    forgotten: int = 0
    cas_ids: list[str] = field(default_factory=list[str])
    expires_at: str = ""


@dataclass
class ConsolidationCandidate:
    session_key: str
    content: str
    sources: list[str] = field(default_factory=list[str])


class MemoryStore:
    """两层记忆读写:闸门/体检 → DEK 加密 → CAS → 账本;查询走投影。"""

    def __init__(self, ledger: DurableLedger, *, home: str | None = None):
        self._ledger = ledger
        self._keys = MemoryKeyStore(home or ledger.home)

    # ---- 写路径(闸门单点) ----

    def write(self, layer: str, session_key: str, content: str,
              provenance: dict[str, Any], *, actor_trust: str | None = None,
              actor: str = "root") -> str:
        if layer not in MEMORY_LAYERS:
            raise ValueError(f"非法记忆层: {layer}(合法:{MEMORY_LAYERS})")
        if layer == "semantic" and actor_trust not in (None, "owner"):
            raise MemoryGateError(
                f"语义记忆写入被拒:{actor_trust} 会话只允许写情景记忆,"
                "进语义记忆必须人审(§6.5 写入闸门)")
        hits = memory_poison_check(content)
        if hits:
            raise MemoryPoisonError(f"记忆内容体检未过(§6.5):命中 {hits[:3]}")
        data = content.encode("utf-8")
        if not data or len(data) > MEMORY_MAX_BYTES:
            raise ValueError(f"记忆 {len(data)} 字节超限(1..{MEMORY_MAX_BYTES})")
        full_prov = {**provenance, "session_key": session_key,
                     "trust": actor_trust or "owner"}
        dek = os.urandom(32)
        nonce = os.urandom(12)
        blob = _BLOB_MAGIC + nonce + AESGCM(dek).encrypt(
            nonce, data, session_key.encode("utf-8"))  # AAD 绑定主体
        cas = self._ledger.cas.put(blob)
        self._keys.wrap_and_store(cas, session_key, dek)  # 先密钥后账本:事件隐含密钥在库
        self._ledger.append("memory.write", actor, {
            "cas_id": cas, "layer": layer, "session_key": session_key,
            "provenance": _canon(full_prov), "size_bytes": len(data)})
        return cas

    # ---- 读路径 ----

    def read_content(self, cas_id: str) -> str | None:
        """解密单条(遗忘/销毁后=None;篡改=ValueError,响亮失败)。"""
        rows = self._ledger.query(
            "SELECT status FROM memory_items WHERE cas_id=?", (cas_id,))
        if not rows or rows[0][0] == "forgotten":
            return None
        return self._decrypt(cas_id)

    def _decrypt(self, cas_id: str) -> str:
        dek = self._keys.unwrap(cas_id)
        if dek is None:
            raise ValueError(f"密钥不在活跃库(已遗忘?): {cas_id}")
        blob = self._ledger.cas.get(cas_id)
        if not blob.startswith(_BLOB_MAGIC):
            raise ValueError(f"非记忆密文对象: {cas_id}")
        nonce = blob[len(_BLOB_MAGIC):len(_BLOB_MAGIC) + 12]
        ct = blob[len(_BLOB_MAGIC) + 12:]
        row = self._ledger.query(
            "SELECT session_key FROM memory_items WHERE cas_id=?", (cas_id,))
        aad = (row[0][0] if row else "").encode("utf-8")
        try:
            return AESGCM(dek).decrypt(nonce, ct, aad).decode("utf-8")
        except Exception as err:
            # ValueError:篡改/错钥响亮失败,且不裸穿 recall 的防御边界
            raise ValueError(f"记忆解密失败(篡改或密钥不匹配): {cas_id}") from err

    def recall(self, session_key: str, query: str,
               limit: int = MEMORY_RECALL_LIMIT) -> list[MemoryHit]:
        """跨会话检索注入(§6.5 读取路径的确定性形态):关键词命中计分,
        语义/情景同池,平分近者优先;命中即计量 memory.use。"""
        keywords = [k.lower() for k in _keywords(query)]
        rows = self._ledger.query(
            "SELECT cas_id, layer, created_at FROM memory_items "
            "WHERE session_key=? AND status='active'", (session_key,))
        scored: list[tuple[int, str, str, str, str]] = []
        for cas, layer, created in rows:
            try:
                content = self._decrypt(str(cas))
            except ValueError:
                continue  # 不可读条目不进召回(防御:坏条目不杀任务)
            low = content.lower()
            score = sum(1 for k in keywords if k in low)
            if score > 0:
                scored.append((score, str(created), str(cas), str(layer), content))
        scored.sort(key=lambda p: (-p[0], p[1]))
        picked = scored[:limit]
        picked.sort(key=lambda p: p[1])  # 注入按时间序(叙事连续)
        if picked:
            self._ledger.append("memory.use", "root",
                                {"cas_ids": [p[2] for p in picked]})
        return [MemoryHit(cas_id=p[2], layer=p[3], content=p[4]) for p in picked]

    def list_items(self, *, session_key: str | None = None,
                   layer: str | None = None, status: str | None = "active"
                   ) -> list[tuple[Any, ...]]:
        """元数据查询(接口 §3.2 memory.list;不含内容本体)。"""
        sql = ("SELECT cas_id, layer, session_key, status, created_at, "
               "last_used_at, use_count, provenance FROM memory_items")
        conds: list[str] = []
        params: list[Any] = []
        if session_key is not None:
            conds.append("session_key=?")
            params.append(session_key)
        if layer is not None:
            conds.append("layer=?")
            params.append(layer)
        if status is not None:
            conds.append("status=?")
            params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at"
        return self._ledger.query(sql, tuple(params))

    # ---- 遗忘与回滚(K.6 / INC4) ----

    def forget(self, session_key: str, *, cas_id: str | None = None,
               since: str | None = None, until: str | None = None,
               actor: str = "root", actor_trust: str = "owner",
               now: datetime.datetime | None = None) -> ForgetReport:
        """按 sessionKey+时段精确遗忘(K.6):DEK 入托管库,条目 forgotten。

        INC4:owner 任意;guest 仅自己 sessionKey(调用方以 actor=本会话键
        表达);known 仅提案权(拒绝)。
        """
        if actor_trust == "known":
            raise MemoryGateError("known 对他人记忆仅提案权(INC4):请经 owner 审批")
        if actor_trust in ("guest", "untrusted") and actor != session_key:
            raise MemoryGateError("guest 仅可申请遗忘自己 sessionKey 的记忆(INC4)")
        now_dt = now or datetime.datetime.now(datetime.UTC)
        params: list[Any] = [session_key]
        sql = ("SELECT cas_id, created_at FROM memory_items WHERE session_key=? "
               "AND status != 'forgotten'")
        if cas_id is not None:
            sql += " AND cas_id=?"
            params.append(cas_id)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND created_at <= ?"
            params.append(until)
        rows = self._ledger.query(sql, tuple(params))
        ids = [str(r[0]) for r in rows]
        if not ids:
            return ForgetReport()  # 幂等:无可遗忘即无操作
        expires = (now_dt + datetime.timedelta(days=FORGET_ESCROW_DAYS)).isoformat()
        moved = self._keys.escrow_keys(ids, expires)  # 先托管后入账(fail-closed)
        self._ledger.append("memory.forget", actor, {
            "session_key": session_key, "cas_ids": moved, "count": len(moved),
            "since": since, "until": until, "expires_at": expires})
        self._keys.destroy_expired()  # 顺带清扫到期托管
        return ForgetReport(forgotten=len(moved), cas_ids=moved, expires_at=expires)

    def rollback_forget(self, *, cas_id: str | None = None,
                        session_key: str | None = None,
                        actor: str = "root") -> list[str]:
        """托管期内回滚遗忘(取回密钥;动作自身入账 K.6②)。"""
        sql = "SELECT cas_id FROM memory_items WHERE status='forgotten'"
        params: list[Any] = []
        if cas_id is not None:
            sql += " AND cas_id=?"
            params.append(cas_id)
        if session_key is not None:
            sql += " AND session_key=?"
            params.append(session_key)
        ids = [str(r[0]) for r in self._ledger.query(sql, tuple(params))]
        restored = self._keys.restore_keys(ids)
        if restored:
            self._ledger.append("memory.forget.rollback", actor,
                                {"cas_ids": restored, "count": len(restored)})
        return restored

    # ---- Curator 域(§6.5 巩固与保鲜) ----

    def set_status(self, cas_id: str, status: str, *, reason: str = "",
                   actor: str = "curator") -> bool:
        """保鲜状态机迁移(active→stale→archived;归档非删除,INV-2)。"""
        if status not in ("active", "stale", "archived"):
            raise ValueError(f"非法状态: {status}")
        rows = self._ledger.query(
            "SELECT status FROM memory_items WHERE cas_id=?", (cas_id,))
        if not rows:
            return False
        self._ledger.execute(
            "UPDATE memory_items SET status=? WHERE cas_id=?", (status, cas_id))
        self._ledger.append(f"memory.{status}", actor,
                            {"cas_id": cas_id, "reason": reason})
        return True

    def consolidation_candidates(self, *, min_recurrence: int =
                                 MEMORY_CONSOLIDATE_MIN_RECURRENCE,
                                 similarity: float =
                                 MEMORY_CONSOLIDATE_SIMILARITY
                                 ) -> list[ConsolidationCandidate]:
        """巩固候选(§6.5 睡眠机制):同主体情景记忆中高频复现的事实——
        关键词袋单链聚类(重叠系数 inter/min(|a|,|b|) ≥ similarity,
        召回导向),簇大小 ≥ min_recurrence。低频是琐事,高频是偏好。"""
        by_session: dict[str, list[tuple[str, str, str]]] = {}
        for cas, _layer, created, session in self._ledger.query(
                "SELECT cas_id, layer, created_at, session_key FROM memory_items "
                "WHERE status='active' AND layer='episodic'"):
            try:
                content = self._decrypt(str(cas))
            except ValueError:
                continue
            by_session.setdefault(str(session), []).append(
                (str(created), content, str(cas)))
        out: list[ConsolidationCandidate] = []
        for session, items in by_session.items():
            # 成员元组:(created, content, cas, bag)
            clusters: list[list[tuple[str, str, str, set[str]]]] = []
            for created, content, cas in sorted(items):
                bag = {k.lower() for k in _keywords(content)}
                # 单链聚类,相似度=重叠系数 inter/min(|a|,|b|):召回导向——
                # 措辞漂移(第三次/再次)不应淹没复现事实本身
                target = next(
                    (cl for cl in clusters
                     if any(min(len(m[3]), len(bag)) >= 1
                            and len(m[3] & bag) / min(len(m[3]), len(bag))
                            >= similarity for m in cl)), None)
                if target is None:
                    clusters.append([(created, content, cas, bag)])
                else:
                    target.append((created, content, cas, bag))
            for cl in clusters:
                if len(cl) >= min_recurrence:
                    newest = max(cl, key=lambda m: m[0])
                    out.append(ConsolidationCandidate(
                        session_key=session, content=newest[1],
                        sources=[m[2] for m in cl]))
        return out

    def consolidate(self, candidate: ConsolidationCandidate, *,
                    actor: str = "curator") -> str | None:
        """情景→语义晋升(Curator 以 owner 语境执行;内容仍过体检闸)。"""
        try:
            return self.write(
                "semantic", candidate.session_key, candidate.content,
                {"source": "curator",
                 "consolidated_from": candidate.sources},
                actor_trust="owner", actor=actor)
        except (MemoryGateError, MemoryPoisonError, ValueError):
            return None  # 巩固是提议性的:坏候选丢弃,不杀巡检

    def destroy_expired_escrow(self) -> list[str]:
        return self._keys.destroy_expired()


def render_memory_section(hits: list[MemoryHit]) -> str:
    """召回注入的系统提示节(§6.5 读取路径终点;字符预算封顶)。"""
    if not hits:
        return ""
    lines = ["## 相关记忆(此前会话沉淀的长期上下文,供参考)"]
    used = 0
    for hit in hits:
        line = f"- [{hit.layer}] {hit.content}"
        if used + len(line) > MEMORY_INJECT_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def parse_distilled(body: str) -> tuple[str | None, list[str]]:
    """解析任务收尾提炼输出:`[E] 事件摘要` + 0..N 行 `[S] 长期事实`。"""
    episodic: str | None = None
    semantic: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("[E]") and episodic is None:
            episodic = stripped[3:].strip()
        elif stripped.startswith("[S]"):
            fact = stripped[3:].strip()
            if fact:
                semantic.append(fact)
    return episodic, semantic[:3]


def new_memory_id() -> str:
    return "mem-" + uuid.uuid4().hex[:12]
