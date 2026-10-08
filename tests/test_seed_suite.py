"""T1-T27 种子套件驱动:与 docs/design 基线的 kernel __main__ 同一套件。"""

import asyncio

from naga import kernel


def test_seed_suite_t1_t27(capsys):
    asyncio.run(kernel.main())
    out = capsys.readouterr().out
    assert "全部测试通过(T1-T27)" in out
