# Naga

Samsara 的 Python 路线实现(双轨赛马:TS=Samsara / Python=Naga,架构文档为共同上游)。
当前阶段:M0 —— 可组合内核 L0(Context / 效应三形态 / 反应式余效应 / 账本 / 回滚)。

设计事实来源在 `docs/design/`(R3 级资产:四份设计文档、spec-constants、收敛账本、
冻结检查器、种子代码;修改走 RFC,不直接编辑)。工作快照与登记册在仓库外 `.context/`,不入库。

## 布局

- `src/naga/` —— 运行时代码
- `tests/` —— pytest(种子套件 T1–T27 为回归底线)
- `docs/design/` —— 冻结设计基线(第九轮评审收官态)
- `.github/workflows/ci.yml` —— ruff / pyright / pytest / 冻结检查器

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
python docs/design/freeze_check.py    # 冻结基线 20 规则
```

## 约定

- 变更走 issue + PR,不直推 main;合并用 merge commit。
- 冻结基线中的种子代码(`docs/design/kernel_v34.py` 等)保持原样;代码演进只发生在 `src/` 下。
- M0 出口标准:汇流性 PBT、崩溃恢复模糊测试、pyright strict、CI 全绿。
