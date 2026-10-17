"""卫生微批回归测试(#87):WebChat 路由面板钩子 + job list 默认过滤。"""

import json
from pathlib import Path
from typing import Any

from naga import cli

_WEBCHAT = Path(__file__).parent.parent / "src" / "naga" / "webchat" / "index.html"


def test_webchat_has_routing_panel_hooks():
    """面板必须在位:数据源 /routing-stats 端点 + 渲染容器 + 降级提示。"""
    html = _WEBCHAT.read_text(encoding="utf-8")
    assert 'fetch("/routing/stats")' in html          # 数据源(M4b 仪表盘端点)
    assert 'id="routingBody"' in html                 # 渲染容器
    assert "路由未启用" in html                        # 404 降级提示
    assert 'id="routingPanel"' in html
    assert "wxCheck();" in html                       # 挤占回归:微信初始检查仍在


def test_webchat_panel_escapes_via_text_content():
    """面板渲染全部走 textContent/DOM 构造,无 innerHTML 注入面。"""
    html = _WEBCHAT.read_text(encoding="utf-8")
    start = html.index("路由仪表盘(/routing/stats")
    block = html[start:]
    assert "innerHTML" not in block.split("wxCheck")[0]


def _item(job_id: str, state: str) -> dict[str, Any]:
    return {"id": job_id, "state": state, "schedule": "0 9 * * *",
            "timezone": "Asia/Shanghai", "misfire": "skip", "r_ceiling": "R1",
            "goal_cas": "sha256:t", "budget": {"tokens": 1, "wall_ms": 1}}


def _ws_items_result(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


def test_job_list_hides_deleted_by_default(monkeypatch, capsys):
    captured: list[dict[str, Any]] = []

    async def fake_ws(method: str, params: dict[str, Any],
                      idempotency_key: str | None = None) -> dict[str, Any]:
        captured.append({"method": method, "params": params})
        return _ws_items_result([
            _item("job_a", "active"), _item("job_b", "paused"),
            _item("job_c", "deleted")])

    monkeypatch.setattr(cli, "_ws_call", fake_ws)
    assert cli.main(["job", "list"]) == 0
    out = capsys.readouterr().out
    assert "job: 2 条" in out
    assert "job_c" not in out                      # 默认隐藏已删
    assert captured[-1]["params"] == {}            # WS API 保持通用(不过滤)

    assert cli.main(["--json", "job", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert all(i["state"] != "deleted" for i in payload["items"])


def test_job_list_state_deleted_explicit(monkeypatch, capsys):
    async def fake_ws(method: str, params: dict[str, Any],
                      idempotency_key: str | None = None) -> dict[str, Any]:
        return _ws_items_result([_item("job_c", "deleted")])

    monkeypatch.setattr(cli, "_ws_call", fake_ws)
    assert cli.main(["job", "list", "--state", "deleted"]) == 0
    out = capsys.readouterr().out
    assert "job: 1 条" in out and "job_c" in out   # 显式 --state 不再过滤
