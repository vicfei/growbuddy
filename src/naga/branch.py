"""Session branches — the in-process COW write-capture overlay (§6.2/K.2, M3-2).

One active sessionKey binds one branch: **read global, write local**. The
overlay stores only the diff against the base (overlay_ops: put/del — a
branch is a diff, not a copy), so untouched keys cost nothing. Session end is
an explicit two-way choice (K.2): **commit** (diff persisted to CAS as a
manifest linked to the trace; sink materializes the writes) or **discard**
(dropped wholesale, zero global effect — the environment is restored).

Kill semantics (§5.4): a killed sub-agent's branch stays **open** — the
commit/discard decision belongs to the parent session ("人走茶不凉,资产留
下"). When the parent commits, open child branches cascade-commit; when the
parent discards, open child branches are discarded with it (ledger-recorded);
already-committed assets are unaffected either way.

Naga's honest form of K.2: agents never touch the raw filesystem — every
write goes through the runtime — so write capture is this in-process layer,
and the ≤10% SLO (R3-4/K.2, 100 concurrent sessions) is measured against it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

# 写捕获 SLO(R3-4/K.2):拦截开销 ≤10%,百并发实测——M3 出口③
WRITE_CAPTURE_OVERHEAD_SLO = 0.10

BRANCH_MANIFEST_SCHEMA = "naga-branch/0"
# manifest 为裸 dict(K.2 SLO:结算路径不建模型):{schema, branch_id,
# owner_session, trace_id, ops:[{op, key, value_cas}], parent_branch}


class SessionBranch:
    """一条 COW 经验分支:overlay 覆盖 base(全局视图),只存差异。

    base_get/base_has:全局只读视图(会话间不共享内存态,仅经全局库通信)。
    sink_put/sink_delete:commit 时的物化回调(如 SkillStore.commit)。
    """

    def __init__(self, *, owner_session: str,
                 base_get: Callable[[str], bytes | None],
                 base_has: Callable[[str], bool],
                 cas_put: Callable[[bytes], str],
                 sink_put: Callable[[str, bytes], Any] | None = None,
                 sink_delete: Callable[[str], Any] | None = None,
                 parent: SessionBranch | None = None,
                 ledger_append: Callable[[str, str, dict[str, Any]], Any] | None = None,
                 branch_id: str | None = None):
        self.branch_id = branch_id or "br_" + uuid.uuid4().hex[:12]
        self.owner_session = owner_session
        self._base_get = base_get
        self._base_has = base_has
        self._cas_put = cas_put
        self._sink_put = sink_put
        self._sink_delete = sink_delete
        self._parent = parent
        self._ledger_append = ledger_append
        self._overlay: dict[str, bytes | None] = {}  # None = 墓碑(delete)
        self._children: list[SessionBranch] = []
        self._state = "open"  # open | committed | discarded
        if parent is not None:
            parent._children.append(self)

    # ---- 生命周期 ----

    @property
    def state(self) -> str:
        return self._state

    @property
    def parent(self) -> SessionBranch | None:
        return self._parent

    def spawn_child(self, owner_session: str | None = None) -> SessionBranch:
        """派生子分支(同一 base/CAS/sink 上下文,挂父链供级联裁决)。"""
        self._require_open()
        return SessionBranch(
            owner_session=owner_session or self.owner_session,
            base_get=self._base_get, base_has=self._base_has,
            cas_put=self._cas_put, sink_put=self._sink_put,
            sink_delete=self._sink_delete, parent=self,
            ledger_append=self._ledger_append)

    # ---- COW 读写(读全局写本地) ----

    def get(self, key: str) -> bytes | None:
        """overlay ∪ base,overlay 胜;墓碑=已删除。"""
        if key in self._overlay:
            return self._overlay[key]
        return self._base_get(key)

    def has(self, key: str) -> bool:
        if key in self._overlay:
            return self._overlay[key] is not None
        return self._base_has(key)

    def put(self, key: str, value: bytes) -> None:
        """写捕获(K.2 拦截路径,纯内存——CAS 推迟到 commit):只记差异,
        不触全局;与全局一致则消除差异。"""
        self._require_open()
        if self._base_get(key) == value:
            self._overlay.pop(key, None)   # 与全局一致:不产生差异
            return
        self._overlay[key] = value

    def delete(self, key: str) -> None:
        self._require_open()
        if not self._base_has(key):
            self._overlay.pop(key, None)   # 全局本就无此键:无差异
            return
        self._overlay[key] = None          # 墓碑

    def diff_keys(self) -> list[str]:
        """差异键集合(未动的键零开销——分支=差异而非副本)。"""
        return list(self._overlay.keys())

    # ---- 会话结束二选一(K.2) ----

    def commit(self, trace_id: str | None = None) -> str | None:
        """差异入 CAS(值+manifest)+ 物化 sink;级联:open 子分支一并 commit。
        op 集合由 overlay 终态导出(同键多次写=终态一次,天然去重)。"""
        self._require_open()
        cascaded = self._cascade("commit", trace_id)
        ops: list[dict[str, Any]] = []
        for key, value in self._overlay.items():
            if value is None:
                ops.append({"op": "del", "key": key})
            else:
                ops.append({"op": "put", "key": key,
                            "value_cas": self._cas_put(value)})
        cas_id: str | None = None
        if ops:
            manifest = {"schema": BRANCH_MANIFEST_SCHEMA,
                        "branch_id": self.branch_id,
                        "owner_session": self.owner_session,
                        "trace_id": trace_id, "ops": ops,
                        "parent_branch": (self._parent.branch_id
                                          if self._parent else None)}
            cas_id = self._cas_put(
                json.dumps(manifest, ensure_ascii=False,
                           separators=(",", ":")).encode())
            for op in ops:
                if op["op"] == "put" and self._sink_put is not None:
                    value = self._overlay[op["key"]]
                    if value is not None:  # 终态导出:put 键必有值
                        self._sink_put(op["key"], value)
                elif op["op"] == "del" and self._sink_delete is not None:
                    self._sink_delete(op["key"])
        self._state = "committed"
        # commit 不另入账:manifest 已入 CAS、物化自带 skill.commit 事件
        # (审计轨迹完备);fsync 级账本追加会令捕获开销翻倍,违反 K.2 SLO 本意
        del cascaded
        return cas_id

    def discard(self) -> None:
        """整体丢弃(环境还原);级联:open 子分支陪葬(入账)。"""
        self._require_open()
        cascaded = self._cascade("discard", None)
        self._overlay.clear()
        self._state = "discarded"
        self._record("branch.discarded", {"cascaded_children": cascaded})

    def _cascade(self, action: str, trace_id: str | None) -> int:
        """§5.4:父 commit → open 子分支级联 commit;父 discard → 陪葬。"""
        count = 0
        for child in self._children:
            if child._state != "open":
                continue
            if action == "commit":
                child.commit(trace_id)
            else:
                child.discard()
            count += 1
        return count

    def _require_open(self) -> None:
        if self._state != "open":
            raise ValueError(f"branch {self.branch_id} 已 {self._state},不可再操作")

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        if self._ledger_append is not None:
            try:
                self._ledger_append(kind, self.owner_session, payload)
            except OSError:
                pass  # 分支语义不因账本 IO 抖动中断(事件尽力入账)


class BranchPool:
    """会话→活跃分支的登记表(gateway 每任务建分支;会话级常驻绑定属会话注册表批次)。"""

    def __init__(self) -> None:
        self._branches: dict[str, SessionBranch] = {}

    def bind(self, branch: SessionBranch) -> SessionBranch:
        self._branches[branch.owner_session] = branch
        return branch

    def of(self, owner_session: str) -> SessionBranch | None:
        return self._branches.get(owner_session)

    def unbind(self, owner_session: str) -> None:
        self._branches.pop(owner_session, None)
