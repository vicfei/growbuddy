"""P3 LIFO 回放序属性(M0 出口标准①属性集 3/5)。

属性:随机效应栈(三形态 effect/perform/bracket 混合 + 失败注入):

1. 卸载期回放序 = 栈内容(注册成功的效应)的精确逆序;
2. 失败路径(perform/bracket 抛出)的回放是即时的、按程序序,与卸载期回放
   以执行相位区分(effect 登记形态总是留到卸载期);
3. 每个 op 的回放恰一次。
"""

import asyncio
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from naga import kernel

ops_strategy = st.lists(
    st.tuples(st.sampled_from(["effect", "perform", "bracket"]), st.booleans()),
    max_size=10,
)


class TestLifoReplayOrder:
    @settings(deadline=None)
    @given(ops=ops_strategy)
    def test_drain_order_is_reverse_of_registration(self, ops):
        phase = ["run"]      # run = 插件执行期;drain = 卸载回放期
        stack: list[int] = []    # 注册成功、留在效应栈上的 op 序号(== disposers 序)
        revert_log: list[tuple[int, str]] = []

        async def plugin(ctx):
            for i, (form, fails) in enumerate(ops):
                def do_apply(fails=fails, i=i):
                    if fails:
                        raise RuntimeError("boom")
                    return i

                def do_revert(i=i, phase=phase, revert_log=revert_log):
                    revert_log.append((i, phase[0]))

                if form == "effect":
                    ctx.effect(f"fx:{i}", do_revert)
                    stack.append(i)  # 登记形态恒留栈(失败也只是 revert 为 no-op)
                elif form == "perform":
                    try:
                        await ctx.perform(f"fx:{i}", do_apply, do_revert)
                        stack.append(i)
                    except RuntimeError:
                        pass  # 即时回放已在 revert_log 留下 ("run" 相位)
                else:  # bracket
                    try:
                        async with ctx.bracket(f"fx:{i}", do_revert):
                            if fails:
                                raise RuntimeError("boom")
                        stack.append(i)
                    except RuntimeError:
                        pass

        async def scenario():
            k = kernel.Kernel()
            f = await k.load(kernel.Plugin(plugin, name="p3"))
            phase[0] = "drain"
            await k.unload(f)

        asyncio.run(scenario())

        drain = [i for i, ph in revert_log if ph == "drain"]
        immediate = [i for i, ph in revert_log if ph == "run"]
        expected_immediate = [i for i, (form, fails) in enumerate(ops)
                              if fails and form in ("perform", "bracket")]

        assert drain == list(reversed(stack)), "卸载期回放序必须恰为栈的逆序"
        assert immediate == expected_immediate, "失败回放须即时且按程序序"
        assert Counter(i for i, _ in revert_log) == Counter(range(len(ops))), "每 op 回放恰一次"
