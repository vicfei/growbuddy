"""P4 授权代数属性(INV-3,M0 出口标准①属性集 5/5)。

随机请求流驱动随机 fork 树:越权请求(预算超剩余 / caps 越界 / 信任抬升)
被 QuotaExhausted 拒绝即树的自然剪枝。对生成的树断言全局不变量:

1. caps:child ⊆ parent(父 None 表示全集则豁免);grant.caps 为副本非共享引用;
2. 信任 ratchet:child 信任级 rank ≥ parent(只降不升);
3. 配额树记账:sum(子代 total) == parent.used < parent.total;
4. 必然终止:根-叶路径 budget 严格递减(整数域每级至少 1)⇒ 树深 ≤ 初始预算;
   每个叶以 remaining+1 派生必被拒(终止性见证,F-03);
5. 全树卸载后 root 授权完全回收。
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from naga import kernel
from naga.kernel import QuotaExhausted, QuotaPolicy, QuotaReq

UNIVERSE = {"fs", "gpu", "net"}

budget_strategy = st.integers(min_value=20, max_value=120)
request_strategy = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=40),
        st.one_of(st.none(), st.sets(st.sampled_from(sorted(UNIVERSE)))),
        st.sampled_from(["owner", "known", "guest", "untrusted"]),
    ),
    max_size=12,
)


def _noop(ctx):
    return None


def _walk(f):
    yield f
    for c in f.children:
        yield from _walk(c)


def _depth(f) -> int:
    return 1 + max((_depth(c) for c in f.children), default=0)


class TestAuthorizationAlgebraInv3:
    @settings(deadline=None, max_examples=80)
    @given(total=budget_strategy, requests=request_strategy)
    def test_random_fork_tree_satisfies_algebra_and_terminates(self, total, requests):
        async def scenario():
            k = kernel.Kernel(policy=QuotaPolicy(root_caps=UNIVERSE, root_trust="owner"))
            root = await k.load(kernel.Plugin(_noop, name="root"),
                                QuotaReq(budget=total, caps=UNIVERSE, trust="owner"))
            nodes = [root]
            for i, (rb, rcaps, rtrust) in enumerate(requests):
                target = nodes[i % len(nodes)]
                try:
                    child = await target.ctx.fork(
                        kernel.Plugin(_noop, name=f"n{i}"),
                        QuotaReq(budget=rb, caps=rcaps, trust=rtrust))
                    nodes.append(child)
                except QuotaExhausted:
                    pass  # 越权即剪枝

            # 1-3: 全局不变量
            for f in _walk(root):
                for c in f.children:
                    pg, cg = f.grant, c.grant
                    if pg.caps is not None and cg.caps is not None:
                        assert cg.caps <= pg.caps
                        assert cg.caps is not pg.caps
                    assert kernel.TRUST_RANK[cg.trust] >= kernel.TRUST_RANK[pg.trust]
                    assert c.grant.budget is not None
                kids_total = sum(c.grant.budget.total for c in f.children)
                if f.children:
                    assert kids_total == f.grant.budget.used
                    assert f.grant.budget.used < f.grant.budget.total

            # 4: 必然终止 —— 深度上界与叶级配额耗尽见证
            assert _depth(root) <= total
            for f in _walk(root):
                if not f.children and f.ctx is not None:
                    try:
                        await f.ctx.fork(
                            kernel.Plugin(_noop, name="doomed"),
                            QuotaReq(budget=f.grant.budget.remaining + 1))
                        raise AssertionError("叶级超余派生必须被拒")
                    except QuotaExhausted:
                        pass

            # 5: 全树卸载 → 授权完全回收,无余账
            await k.unload(root)
            assert root.grant is None
            assert all(f.state == "disposed" for f in nodes)

        asyncio.run(scenario())
