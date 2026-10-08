"""P2 apply/revert 契约属性(M0 出口标准①属性集 2/5,主文档 §3.4)。

宇宙定义:模拟环境为 key→int 计数器;效应 = 对某 key 施加 delta,逆操作 =
减去 delta 并在归零时删除键(严格逆)。apply 原子性入宇宙假设——失败即
变异前抛出(契约测试的常规前提)。

属性:随机效应序列(三形态 effect/perform/bracket 混合,可注入失败)在
插件卸载(LIFO 回放全部在册效应)后,环境恢复初值——即环境哈希不变。
失败注入同时泛化 T13b/T14 的"失败回滚恰一次"。
"""

import asyncio
import hashlib

from hypothesis import given, settings
from hypothesis import strategies as st

from naga import kernel


def _env_hash(env: dict[str, int]) -> str:
    return hashlib.sha256(repr(sorted(env.items())).encode()).hexdigest()


ops_strategy = st.lists(
    st.tuples(
        st.sampled_from(["effect", "perform", "bracket"]),
        st.sampled_from(["a", "b", "c"]),
        st.integers(min_value=1, max_value=9),
        st.booleans(),  # 失败注入:apply 在变异前抛出
    ),
    max_size=10,
)


class TestApplyRevertContract:
    @settings(deadline=None)
    @given(ops=ops_strategy)
    def test_environment_hash_unchanged_after_dispose(self, ops):
        env: dict[str, int] = {}
        initial = _env_hash(env)

        async def plugin(ctx):
            for form, key, delta, fails in ops:
                applied = [False]  # 本 op 是否真实变异过(失败 op 的 revert 须为 no-op)

                def do_apply(key=key, delta=delta, fails=fails, applied=applied):
                    if fails:
                        raise RuntimeError("boom")
                    env[key] = env.get(key, 0) + delta
                    applied[0] = True
                    return delta

                def do_revert(key=key, delta=delta, applied=applied):
                    if applied[0]:
                        env[key] -= delta
                        if env[key] == 0:
                            del env[key]

                if form == "effect":
                    # 登记形态:apply 由登记方完成(kernel 的 provide/on 同款用法)
                    if not fails:
                        env[key] = env.get(key, 0) + delta
                        applied[0] = True
                    ctx.effect(f"fx:{key}", do_revert)
                elif form == "perform":
                    try:
                        await ctx.perform(f"fx:{key}", do_apply, do_revert)
                    except RuntimeError:
                        pass  # 失败即回滚:env 未变异,revert 为 no-op 后不留痕
                else:  # bracket:主体即 apply
                    try:
                        async with ctx.bracket(f"fx:{key}", do_revert):
                            if fails:
                                raise RuntimeError("boom")
                            env[key] = env.get(key, 0) + delta
                            applied[0] = True
                    except RuntimeError:
                        pass

        async def scenario():
            k = kernel.Kernel()
            f = await k.load(kernel.Plugin(plugin, name="p2"))
            await k.unload(f)  # LIFO 回放全部在册效应
            assert not k._fibers

        asyncio.run(scenario())
        assert env == {}
        assert _env_hash(env) == initial
