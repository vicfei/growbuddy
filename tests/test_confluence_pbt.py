"""汇流性首属性(INV-1 / M0 出口标准①的最小切片)。

种子 T4 单例的泛化:对四件套插件宇宙(logger / counter / greeter / announcer,
依赖链 announcer→greeter→counter),任意 load/unload 操作序列的静止态
(observable_state)恒等于"终集一次性装配"的结果。

WS3 将扩展完整属性集(apply/revert 契约、LIFO 回放、授权代数、重放幂等);
本文件只落首属性。
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from naga import kernel

PLUGINS = {
    "announcer": kernel.announcer_plugin,
    "counter": kernel.counter_plugin,
    "greeter": kernel.greeter_plugin,
    "logger": kernel.logger_plugin,
}
NAMES = sorted(PLUGINS)

ops_strategy = st.lists(
    st.tuples(st.sampled_from(["load", "unload"]), st.sampled_from(NAMES)),
    min_size=0,
    max_size=12,
)


async def _drive(ops):
    """按序列操作内核;非法操作(load 已在载/卸载不在载)跳过。返回终集。"""
    k = kernel.Kernel()
    loaded: dict[str, object] = {}
    for op, name in ops:
        if op == "load" and name not in loaded:
            loaded[name] = await k.load(kernel.Plugin(PLUGINS[name]))
        elif op == "unload" and name in loaded:
            await k.unload(loaded.pop(name))
    return k, set(loaded)


async def _compose(final):
    """终集一次性装配(顺序取字典序——汇流性保证装配序无关)。"""
    k = kernel.Kernel()
    for name in sorted(final):
        await k.load(kernel.Plugin(PLUGINS[name]))
    return k


def _normalize(state: dict) -> dict:
    """观测归一化:零计数 listeners 条目视同缺席。

    依据(hypothesis 缩小出的反例):load counter → load greeter → unload counter
    后,greeter 的 off 效应已移除 handler,但 _listeners 残留空列表键
    {'app/ready': 0}。emit/aemit 走 .get(event, []) 迭代,空列表与缺席行为
    完全等价——INV-1 比较的是可观测行为等价,故归一化是忠实而非回避。
    空键累积本身记为 issue(#11),由 WS1 内核产品化清理。
    """
    listeners = {e: n for e, n in state["listeners"].items() if n}
    return {**state, "listeners": listeners}


class TestConfluenceInv1:
    @settings(deadline=None)  # asyncio.run 每例重建事件循环,免 deadline 误报
    @given(ops=ops_strategy)
    def test_random_sequence_matches_one_shot_composition(self, ops):
        async def compare():
            k_seq, final = await _drive(ops)
            k_ref = await _compose(final)
            return kernel.observable_state(k_seq), kernel.observable_state(k_ref)

        state_seq, state_ref = asyncio.run(compare())
        assert _normalize(state_seq) == _normalize(state_ref), (
            f"INV-1 broken: seq {state_seq} != ref {state_ref}"
        )
