"""CLI 最小面测试:daemon 生命周期 + doctor 自检(子进程边界,--json 可解析)。"""

import json
import os
import subprocess
import sys

from naga import storage
from naga.ledger_store import DurableLedger

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _cli(home: str, *argv: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "naga.cli", "--home", home, *argv],
        env=env, capture_output=True, text=True, timeout=60, check=False)


def test_daemon_lifecycle_and_idempotency(tmp_path):
    home = str(tmp_path / "home")
    # daemon 启动需要凭据仓(按 home 解析);注入 dummy 条目,lifecycle 不触达真实 API
    os.makedirs(os.path.join(home, "credentials"), exist_ok=True)
    with open(os.path.join(home, "credentials", "providers.json"), "w") as fh:
        json.dump({"deepseek": {"base_url": "http://127.0.0.1:9", "api_key": "sk-dummy",
                                "chat_model": "dummy"}}, fh)
    r = _cli(home, "--json", "daemon", "start")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["daemon"] == "started"

    r = _cli(home, "--json", "daemon", "start")  # 幂等
    assert r.returncode == 0 and json.loads(r.stdout)["daemon"] == "running"

    r = _cli(home, "--json", "daemon", "status")
    assert r.returncode == 0 and json.loads(r.stdout)["daemon"] == "running"

    r = _cli(home, "--json", "daemon", "stop")
    assert r.returncode == 0 and json.loads(r.stdout)["daemon"] == "stopped"

    r = _cli(home, "--json", "daemon", "stop")  # 幂等
    assert r.returncode == 0 and json.loads(r.stdout)["daemon"] == "stopped"

    r = _cli(home, "--json", "daemon", "status")
    assert json.loads(r.stdout)["daemon"] == "stopped"
    assert not os.path.exists(os.path.join(home, "daemon.pid"))


def test_doctor_fresh_and_populated_home(tmp_path):
    home = str(tmp_path / "home")
    # 未初始化的 home:doctor 只读诊断,layout 缺失 → 退出码 1(不建目录,无副作用)
    r = _cli(home, "--json", "doctor")
    assert r.returncode == 1
    assert json.loads(r.stdout)["layout"]["ok"] is False

    storage.init_home(home)
    r = _cli(home, "--json", "doctor")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["layout"]["ok"] is True
    assert report["ledger"] == {"entries": 0, "verify": True, "recovery": None}

    led = DurableLedger(home)
    for i in range(3):
        led.append("session.open", "f0001", {"n": i})
    led.close()

    r = _cli(home, "--json", "doctor")
    assert r.returncode == 0
    report = json.loads(r.stdout)
    assert report["ledger"]["entries"] == 3 and report["ledger"]["verify"] is True


def test_doctor_fails_on_corrupted_ledger(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    led.append("session.open", "f0001", {"n": 1})
    led.close()
    head = os.path.join(home, "ledger", "head.log")
    with open(head, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    lines[0] = lines[0].replace('"f0001"', '"f0999"')  # 篡改 owner,哈希不动
    with open(head, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    r = _cli(home, "--json", "doctor")
    assert r.returncode == 1
    report = json.loads(r.stdout)
    assert report["ledger"]["verify"] is False
