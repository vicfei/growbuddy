"""M3-2 tests: session branches — COW overlay semantics, commit/discard
settlement, cascade adjudication, kill-keeps-branch, sedimentation through
branches end-to-end, and the 100-session write-capture SLO (K.2 ≤10%)."""

import asyncio
import time

import pytest

from naga import kernel, storage
from naga.branch import WRITE_CAPTURE_OVERHEAD_SLO, BranchPool, SessionBranch
from naga.gateway import Gateway
from naga.ledger_store import CAS, DurableLedger
from naga.llm import ChatResponse


def _branch(tmp_path, base: dict[str, bytes] | None = None,
            sink: dict[str, bytes] | None = None,
            events: list[str] | None = None) -> tuple[SessionBranch, CAS]:
    cas = CAS(str(tmp_path / "h"))
    store = base if base is not None else {}
    materialized = sink if sink is not None else {}

    def sink_put(key: str, value: bytes) -> None:
        materialized[key] = value

    def sink_delete(key: str) -> None:
        materialized.pop(key, None)

    br = SessionBranch(
        owner_session="s1",
        base_get=store.get, base_has=lambda k: k in store,
        cas_put=cas.put, sink_put=sink_put, sink_delete=sink_delete,
        ledger_append=(lambda kind, owner, payload: events.append(kind))
        if events is not None else None)
    return br, cas


# ---- COW 语义 ----

def test_overlay_read_wins_base_falls_back(tmp_path):
    br, _ = _branch(tmp_path, base={"a": b"global"})
    assert br.get("a") == b"global"          # base 兜底
    br.put("a", b"local")
    assert br.get("a") == b"local"           # overlay 胜
    assert br.diff_keys() == ["a"]           # 差异而非副本
    br.put("b", b"new")
    assert sorted(br.diff_keys()) == ["a", "b"]


def test_no_diff_put_and_tombstone(tmp_path):
    br, _ = _branch(tmp_path, base={"same": b"v"})
    br.put("same", b"v")                     # 与全局一致:不产生差异
    assert br.diff_keys() == []
    br.delete("same")                        # 全局有:墓碑
    assert br.get("same") is None and br.has("same") is False
    assert br.diff_keys() == ["same"]
    br2, _ = _branch(tmp_path, base={})
    br2.delete("absent")                     # 全局无:无差异
    assert br2.diff_keys() == []


# ---- commit / discard ----

def test_commit_materializes_and_manifests(tmp_path):
    sink: dict[str, bytes] = {}
    events: list[str] = []
    br, cas = _branch(tmp_path, base={"del-me": b"x"}, sink=sink, events=events)
    br.put("k1", b"v1")
    br.delete("del-me")
    manifest_cas = br.commit(trace_id="tr_1")
    assert sink == {"k1": b"v1"}             # 物化 put;墓碑走 sink_delete
    assert br.state == "committed"
    assert manifest_cas is not None
    manifest = cas.get(manifest_cas)
    assert b"naga-branch/0" in manifest and b'"trace_id":"tr_1"' in manifest
    assert events == []  # commit 不另入账(manifest+物化即审计轨迹;discard 才入账)
    with pytest.raises(ValueError):
        br.put("late", b"no")                # 已闭合


def test_commit_empty_diff_no_manifest(tmp_path):
    br, _cas = _branch(tmp_path)
    assert br.commit() is None               # 零差异零开销


def test_discard_restores_environment(tmp_path):
    sink: dict[str, bytes] = {}
    events: list[str] = []
    br, _ = _branch(tmp_path, base={"a": b"global"}, sink=sink, events=events)
    br.put("a", b"local")
    br.put("b", b"temp")
    br.discard()
    assert sink == {}                        # 零全局影响
    assert br.state == "discarded" and br.diff_keys() == []
    assert events == ["branch.discarded"]


# ---- 级联裁决(§5.4) ----

def test_cascade_commit_and_discard(tmp_path):
    def family(tmp):
        root, _ = _branch(tmp)
        c1 = root.spawn_child()
        c2 = root.spawn_child()
        return root, c1, c2

    root, c1, c2 = family(tmp_path)
    c1.put("x", b"1")
    root.put("y", b"2")
    root.commit("tr_root")                   # 父 commit → open 子分支级联
    assert c1.state == "committed" and c2.state == "committed"

    root2, k1, k2 = family(tmp_path)
    k1.commit("tr_k")                        # 子先 commit:已定,不受父影响
    root2.discard()                          # 父 discard → open 子分支陪葬
    assert k1.state == "committed"
    assert k2.state == "discarded" and root2.state == "discarded"


def test_spawn_child_requires_open_parent(tmp_path):
    root, _ = _branch(tmp_path)
    root.commit()
    with pytest.raises(ValueError):
        root.spawn_child()


def test_branch_pool_registry(tmp_path):
    pool = BranchPool()
    br, _ = _branch(tmp_path)
    pool.bind(br)
    assert pool.of("s1") is br
    pool.unbind("s1")
    assert pool.of("s1") is None


# ---- 全链路:子任务沉淀走分支,父裁决物化 ----

class _SearchTool:
    name, description, parameters = "web_search", "d", {"type": "object"}
    side_effect, risk_level = "read", 0

    async def run(self, args):
        return {"results": []}


def test_child_sediment_settled_by_parent_commit(tmp_path):
    """子任务沉淀写入子分支(不落全局);父成功 → 级联 commit → local 技能物化。"""
    home = str(tmp_path / "h")
    storage.init_home(home)
    led = DurableLedger(home)

    child_calls = {"n": 0}

    class _ChildAdapter:
        """5 次工具调用触发沉淀(H.4),再给最终答复。"""

        async def chat(self, messages, tools=None, *, max_tokens=1024):
            child_calls["n"] += 1
            if child_calls["n"] <= 5:
                return ChatResponse(
                    content=None, prompt_tokens=1, completion_tokens=1,
                    tool_calls=[{"id": f"c{child_calls['n']}", "name": "web_search",
                                 "arguments": "{}"}])
            return ChatResponse(content="子任务成果", prompt_tokens=1,
                                completion_tokens=1)

    parent_calls = {"n": 0}

    class _ParentAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            parent_calls["n"] += 1
            if parent_calls["n"] == 1:
                import json as _json
                return ChatResponse(
                    content=None, prompt_tokens=1, completion_tokens=1,
                    tool_calls=[{"id": "p1", "name": "spawn_subagents",
                                 "arguments": _json.dumps(
                                     {"subtasks": [{"goal": "子任务"}]})}])
            return ChatResponse(content="父级汇总", prompt_tokens=1,
                                completion_tokens=1)

    adapters = {"child": _ChildAdapter(), "parent": _ParentAdapter()}
    which = {"mode": "parent"}

    class _SwitchAdapter:
        async def chat(self, messages, tools=None, *, max_tokens=1024):
            mode = which["mode"]
            which["mode"] = "child"          # 派生调用后的连续 6 次都是子任务
            if mode == "parent":
                which["mode"] = "child"
                return await adapters["parent"].chat(messages, tools,
                                                     max_tokens=max_tokens)
            # 子任务 6 次后翻回父:靠 child 计数
            if child_calls["n"] >= 6 and child_calls["n"] % 6 == 0:
                which["mode"] = "parent"
            return await adapters["child"].chat(messages, tools, max_tokens=max_tokens)

    gw = Gateway(led, make_adapter=lambda: _SwitchAdapter(),
                 tools=[_SearchTool()], home=home)
    result = asyncio.run(gw.run_agent(
        session_key="wechat:dm:p", goal="调研并沉淀", budget=200_000,
        trace_id="tr_m32", actor_trust="owner"))
    assert result.outcome == "success" and result.reply == "父级汇总"

    from naga.skills import SkillStore
    SkillStore(led)
    local_rows = led.query(
        "SELECT name FROM skill_nodes WHERE branch='local:wechat:dm:p'")
    assert local_rows, "父 commit 应级联物化子任务沉淀的 local 技能"
    led.close()


# ---- K.2 写捕获 SLO:百并发 ≤10% ----

def test_write_capture_overhead_slo_100_sessions(tmp_path):
    """R3-4/K.2:100 并发会话,分支写捕获(捕获+commit 物化)对等价直写
    (无分支,直接 SkillStore.commit)的开销 ≤10%。

    负载按真实形态:每会话沉淀 2 个技能(触发阈值=1/任务)、内容 8KB
    (技能文档典型量级,上限 15KB);直写基线是真实的无分支路径
    (SkillStore.commit ≈ 1.9ms/次,fsync 主导)——两边同店同负载,
    差值即拦截层。三轮取各自最小值滤噪。
    """
    from naga.skills import SkillStore

    sessions, keys_per = 100, 2
    payload = "x" * 8190 + "\n"

    def one_round(tag: str) -> tuple[float, float]:
        # 分支侧:会话分支,sink=真实 SkillStore.commit
        home_a = str(tmp_path / f"{tag}-a")
        storage.init_home(home_a)
        led_a = DurableLedger(home_a)
        skills_a = SkillStore(led_a)
        branches = [SessionBranch(
            owner_session=f"s{i}", base_get=lambda k: None, base_has=lambda k: False,
            cas_put=led_a.cas.put,
            sink_put=lambda k, v, _s=skills_a, _i=i: _s.commit(
                k.removeprefix("skill:"), v.decode("utf-8"),
                provenance={"source": "agent"}, branch=f"local:s{_i}"))
            for i in range(sessions)]
        t0 = time.perf_counter()
        for n, br in enumerate(branches):
            for j in range(keys_per):
                br.put(f"skill:sk-{n}-{j}",
                       f"---\nname: sk-{n}-{j}\n---\n{n}-".encode() + payload.encode())
        for br in branches:
            br.commit()
        captured_s = time.perf_counter() - t0
        led_a.close()

        # 直写侧:同样的写入,无分支,直接 SkillStore.commit
        home_b = str(tmp_path / f"{tag}-b")
        storage.init_home(home_b)
        led_b = DurableLedger(home_b)
        skills_b = SkillStore(led_b)
        t0 = time.perf_counter()
        for n in range(sessions):
            for j in range(keys_per):
                skills_b.commit(
                    f"sk-{n}-{j}", f"---\nname: sk-{n}-{j}\n---\n{n}-" + payload,
                    provenance={"source": "agent"}, branch=f"local:s{n}")
        direct_s = time.perf_counter() - t0
        led_b.close()
        return captured_s, direct_s

    captured_best, direct_best = float("inf"), float("inf")
    for r in range(3):
        c, d = one_round(f"r{r}")
        captured_best, direct_best = min(captured_best, c), min(direct_best, d)

    overhead = captured_best / direct_best - 1
    print(f"\n[write-capture SLO] captured={captured_best:.3f}s "
          f"direct={direct_best:.3f}s overhead={overhead:.1%} "
          f"({sessions} sessions x {keys_per} skills x 8KB, best of 3)")
    assert overhead <= WRITE_CAPTURE_OVERHEAD_SLO, (
        f"写捕获开销 {overhead:.1%} 超 K.2 SLO {WRITE_CAPTURE_OVERHEAD_SLO:.0%}")


# ---- 辅助:kernel 兼容(占位避免未用导入) ----

def test_kernel_import_compatible():
    assert kernel.Budget(10).remaining == 10
