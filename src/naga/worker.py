"""外部来源插件 worker 隔离(K.3 时刻表 M2 行,出口③)。

规格(主文档 §12/K.3):外部来源插件(MCP/市场技能)一律入 worker/子进程,
结构化克隆边界通信;签名+检疫仅为准入,不作运行时防线。bocha web_search
属外部来源——元数据(name/description/parameters/R 级)留在父进程供
AgentLoop 装配,执行体在子进程。

协议:父进程 stdin 写 JSON 请求行 {id, tool, args};子进程 stdout 回
{id, ok, result|error}。一次调用一超时;超时/崩溃(EOF)即杀 worker、
下次调用自动重建,当次调用以 ValueError 降级——lane 消费者与主进程
不受波及(穿透测试见 tests/test_worker.py)。

子进程自灭语义:stdin 关闭(父进程退出)→ 循环结束 → 干净退出。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, cast

from naga.llm import CredentialStore

CALL_TIMEOUT_SEC = 45.0   # 单次调用总额限(bocha 内部 httpx 30s,留头寸)
START_TIMEOUT_SEC = 15.0  # 子进程冷启动上限

# 测试专用工具注册表(NAGA_WORKER_TEST_TOOLS=1 才装配;零外网)
_TEST_TOOLS_NOTE = "仅测试装配(NAGA_WORKER_TEST_TOOLS=1),不进生产"


def _child_main(providers_path: str) -> int:
    """子进程入口:注册表 + stdin→stdout JSON 行循环。"""
    registry: dict[str, Any] = {}
    try:
        from naga.tools import WebSearchTool

        store = CredentialStore(providers_path)
        registry["web_search"] = WebSearchTool(store)
    except (OSError, KeyError):
        pass  # 凭据缺失:该工具调用将按 unknown tool 降级,不影响其他工具
    if os.environ.get("NAGA_WORKER_TEST_TOOLS", "") == "1":
        registry["echo"] = _EchoTool()
        registry["fail"] = _FailTool()
        registry["boom"] = _BoomTool()
        registry["hang"] = _HangTool()
    for line in sys.stdin:  # 父进程退出 → EOF → 循环结束 → 干净退出
        rid: Any = None
        try:
            raw: Any = json.loads(line)
            req = cast("dict[str, Any]", raw if isinstance(raw, dict) else {})
            rid = req.get("id")
            tool = registry.get(str(req.get("tool", "")))
            if tool is None:
                resp: dict[str, Any] = {"id": rid, "ok": False,
                                        "error": f"unknown tool: {req.get('tool')}"}
            else:
                result = asyncio.run(tool.run(req.get("args") or {}))
                resp = {"id": rid, "ok": True, "result": result}
        except Exception as err:  # noqa: BLE001 —— 工具异常转错误响应,worker 不死
            resp = {"id": rid, "ok": False, "error": f"{type(err).__name__}: {err}"}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    return 0


class _EchoTool:
    name, description, parameters = "echo", _TEST_TOOLS_NOTE, {"type": "object"}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"echo": args}


class _FailTool:
    name, description, parameters = "fail", _TEST_TOOLS_NOTE, {"type": "object"}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("工具侧业务失败(测试)")


class _BoomTool:
    """硬崩溃(kill -9 语义):子进程即刻消失,父进程只应看到 EOF。"""
    name, description, parameters = "boom", _TEST_TOOLS_NOTE, {"type": "object"}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        os._exit(1)
        raise AssertionError("unreachable")  # os._exit 为 NoReturn,类型收紧


class _HangTool:
    name, description, parameters = "hang", _TEST_TOOLS_NOTE, {"type": "object"}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(999)
        return {}


class WorkerTool:
    """工具的 worker 隔离形态(K.3):元数据在父进程,执行在子进程。

    失败语义与直接工具一致:run() 抛 ValueError → AgentLoop 记 ok=False
    步并继续;worker 超时/崩溃额外杀进程并标记重建。调用串行(asyncio.Lock)
    ——单管道协议不支持交错,M2 规模足够。
    """

    def __init__(self, *, name: str, description: str, parameters: dict[str, Any],
                 providers_path: str,
                 side_effect: str | None = None,
                 risk_level: int | None = None,
                 call_timeout: float = CALL_TIMEOUT_SEC,
                 python: str = sys.executable,
                 spawn_env: dict[str, str] | None = None):
        self.name = name
        self.description = description
        self.parameters = parameters
        if side_effect is not None:
            self.side_effect = side_effect
        if risk_level is not None:
            self.risk_level = risk_level
        self._providers_path = providers_path
        self._call_timeout = call_timeout
        self._python = python
        self._spawn_env = spawn_env
        self._proc: asyncio.subprocess.Process | None = None
        self._seq = 0
        self._lock = asyncio.Lock()

    @classmethod
    def of(cls, tool_cls: Any, *, providers_path: str, **kw: Any) -> WorkerTool:
        """从直接工具类镜像元数据(执行永远在子进程)。"""
        return cls(name=tool_cls.name, description=tool_cls.description,
                   parameters=tool_cls.parameters,
                   side_effect=getattr(tool_cls, "side_effect", None),
                   risk_level=getattr(tool_cls, "risk_level", None),
                   providers_path=providers_path, **kw)

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            proc = await self._ensure()
            self._seq += 1
            rid = f"w{self._seq}"
            request = json.dumps({"id": rid, "tool": self.name, "args": args},
                                 ensure_ascii=False) + "\n"
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(request.encode())
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), self._call_timeout)
            except TimeoutError:
                await self._kill()
                raise ValueError(f"worker 超时({self._call_timeout:.0f}s),已终止待重建") from None
            except (BrokenPipeError, ConnectionResetError, OSError) as err:
                self._proc = None
                raise ValueError(f"worker 管道断裂({type(err).__name__}),待重建") from None
            if not line:
                code = proc.returncode
                self._proc = None
                raise ValueError(f"worker 崩溃(exit={code}),待重建")
            resp_raw: Any = json.loads(line)
            if not isinstance(resp_raw, dict):
                resp_raw = {}  # 格式错乱折算为序号错乱(同一协议破坏路径)
            resp = cast("dict[str, Any]", resp_raw)
            if resp.get("id") != rid:
                raise ValueError("worker 响应序号错乱")  # 协议破坏:宁可硬错
            if not resp.get("ok"):
                raise ValueError(str(resp.get("error", "worker 工具失败")))
            result: Any = resp.get("result")
            if isinstance(result, dict):
                return cast("dict[str, Any]", result)
            return {"result": result}

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            env = None
            if self._spawn_env is not None:
                env = {**os.environ, **self._spawn_env}
            try:
                self._proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        self._python, "-m", "naga.worker", self._providers_path,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, env=env),
                    START_TIMEOUT_SEC)
            except (TimeoutError, OSError) as err:
                self._proc = None
                raise ValueError(f"worker 启动失败({type(err).__name__})") from None
        return self._proc

    async def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            await self._proc.wait()
        self._proc = None

    async def aclose(self) -> None:
        """优雅回收:关 stdin → 子进程 EOF 自灭;超时则杀。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), START_TIMEOUT_SEC)
        except TimeoutError:
            proc.kill()
            await proc.wait()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m naga.worker <providers.json>", file=sys.stderr)
        return 2
    return _child_main(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
