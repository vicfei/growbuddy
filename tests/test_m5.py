"""M5 tests: constitution layer (enumerable anchors, ledger anchoring, tamper
fuse) and the soak runtime (clock, P0 recording, heartbeats, report)."""


import pytest

from naga import soak as soak_mod
from naga import storage
from naga.constitution import (
    ConstitutionViolation,
    anchor,
    anchored_sha,
    constitution_sha,
    evaluate,
    verify,
)
from naga.ledger_store import DurableLedger
from naga.soak import (
    SOAK_DAYS,
    ensure_started,
    health_check,
    load_soak,
    record_heartbeat,
    record_p0,
    soak_report,
)


def _home(tmp_path) -> tuple[str, DurableLedger]:
    home = str(tmp_path / "h")
    storage.init_home(home)
    return home, DurableLedger(home)


# ---- 宪法:可枚举、可校验(INV-5 应用层语义) ----

def test_constitution_enumerable_and_green():
    report = evaluate()
    assert report.ok and not report.violations
    assert len(constitution_sha()) == 64  # 摘要稳定可复算


def test_tamper_policy_value_triggers_fuse(tmp_path, monkeypatch):
    _home_dir, led = _home(tmp_path)
    anchor(led)
    from naga import gateway
    monkeypatch.setattr(gateway, "GUEST_TASK_TOKEN_BUDGET", 999_999)  # 越权改预算
    report = evaluate()
    assert not report.ok and any("C2a" in v for v in report.violations)
    with pytest.raises(ConstitutionViolation):
        verify(led)
    led.close()


def test_ledger_anchoring_and_drift(tmp_path):
    _home_dir, led = _home(tmp_path)
    assert anchored_sha(led) is None
    sha = anchor(led, note="test")
    assert anchored_sha(led) == sha
    report = verify(led)  # 锚定一致:通过
    assert report.ok

    # 篡改宪法(改规则期望值 → 摘要变化)→ 与账本锚定漂移 → 熔断
    from naga import constitution as c
    original = c.rules
    tampered = list(original())
    tampered[1] = type(tampered[1])(rid="C2a", domain=tampered[1].domain,
                                    statement=tampered[1].statement,
                                    module=tampered[1].module,
                                    attr=tampered[1].attr, equals=1)
    def monkey_rules(*args, **kwargs):
        return tampered
    c.rules = monkey_rules  # type: ignore[assignment]
    try:
        with pytest.raises(ConstitutionViolation, match="漂移"):
            verify(led)
    finally:
        c.rules = original  # type: ignore[assignment]
    led.close()


def test_reanchor_is_auditable(tmp_path):
    _home_dir, led = _home(tmp_path)
    anchor(led, note="first")
    anchor(led, reanchor=True, note="RFC batch")
    rows = led.query("SELECT kind FROM ledger_entries WHERE kind LIKE "
                     "'constitution.%' ORDER BY seq")
    assert [r[0] for r in rows] == ["constitution.anchored",
                                    "constitution.reanchored"]
    assert verify(led).ok
    led.close()


# ---- soak:时钟 / P0 / 心跳 / 报告 ----

def test_soak_clock_idempotent(tmp_path):
    home, _led = _home(tmp_path)
    ensure_started(home)
    ensure_started(home)  # 幂等:不重置时钟
    assert len(load_soak(home).get("p0_events", [])) == 0
    assert soak_report(home)["days_elapsed"] < SOAK_DAYS


def test_soak_p0_recording_and_report(tmp_path):
    home, _led = _home(tmp_path)
    ensure_started(home)
    record_p0(home, "daemon-unclean-exit", "kill -9")
    report = soak_report(home)
    assert report["p0_count"] == 1
    assert report["p0_events"][0]["kind"] == "daemon-unclean-exit"
    assert not report["on_track"]


def test_health_check_signals(tmp_path):
    home, led = _home(tmp_path)

    class _DeadVerify:
        def __init__(self) -> None:
            self.entries = [1]

        def verify(self):
            return False

    health = health_check(_DeadVerify(), {"b1": False}, home)  # 链坏+渠道全灭
    assert health["p0"] and "账本链" in health["issues"][0]
    assert any("渠道全灭" in i for i in health["issues"])

    good = health_check(led, {"b1": False, "b2": True}, home)
    assert not good["p0"]  # 有活渠道 + 链好 + 磁盘足
    led.close()


def test_heartbeat_recording(tmp_path):
    home, _led = _home(tmp_path)
    ensure_started(home)
    record_heartbeat(home, {"chain_ok": True, "issues": []})
    record_heartbeat(home, {"chain_ok": True, "issues": []})
    report = soak_report(home)
    assert report["heartbeats"] == 2
    assert report["last_heartbeat"]["chain_ok"] is True


def test_soak_state_private(tmp_path):
    import os
    home, _led = _home(tmp_path)
    ensure_started(home)
    assert os.stat(soak_mod.soak_path(home)).st_mode & 0o777 == 0o600


# ---- doctor 集成 ----

def test_doctor_constitution_section(tmp_path):
    home, led = _home(tmp_path)
    led.close()
    from naga.cli import _constitution_report
    report = _constitution_report(home)
    assert report["ok"] and report["anchored_match"] is None or \
        report["anchored_match"]  # 未锚定(only evaluate)或锚定一致
