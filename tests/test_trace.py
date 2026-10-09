"""Trace + ReplayBundle 测试:入账字段 / CAS 往返 / 两级脱敏 / 截断 partial / 重建。"""

import json
import os

from naga.ledger_store import DurableLedger
from naga.trace import REPLAY_BUNDLE_STEP_TRUNCATE_BYTES, TraceRecorder


def test_trace_record_and_bundle_roundtrip(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    rec = TraceRecorder(led, trace_id="tr_01", session_key="webchat:dm:owner",
                        agent_id="ag_01", inbound={"text": "调研竞品定价"},
                        skills_loaded=["skill://pricing@v1"], env={"model": "fake-1"})
    rec.step(1, "tool", "web_search", True, 1240, 830,
             args={"q": "竞品定价"}, results={"hits": 3})
    rec.step(2, "llm", "reason", True, 800, 1200, args=None, results={"action": "reply"})
    trace = rec.finish(outcome="success", cost_tokens=2030, cost_cents=7,
                       task_cluster="research.pricing")

    assert trace["schema"] == "samsara-trace/1" and trace["replay_bundle_cas"].startswith("sha256:")
    assert [(s["no"], s["name"], s["ok"]) for s in trace["steps"]] == [(1, "web_search", True), (2, "reason", True)]
    row = led.query("SELECT trace_id, session_key, outcome, cost_cents, tokens, task_cluster, "
                    "replay_bundle_cas FROM traces")[0]
    assert row == ("tr_01", "webchat:dm:owner", "success", 7, 2030,
                   "research.pricing", trace["replay_bundle_cas"])

    bundle = json.loads(led.cas.get(trace["replay_bundle_cas"]))
    assert bundle["schema"] == "samsara-replay/1"
    assert bundle["session"]["inbound"] == {"text": "调研竞品定价"}
    assert bundle["assets"]["skills"] == ["skill://pricing@v1"]
    assert bundle["steps"][0]["args"]["data"] == {"q": "竞品定价"}
    assert bundle["steps"][0]["args"]["partial"] is False
    led.close()


def test_redaction_two_levels(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + "z" * 64 +
           "\n-----END RSA PRIVATE KEY-----")
    rec = TraceRecorder(led, trace_id="tr_02", session_key="webchat:dm:owner",
                        inbound={"text": "我的密钥 sk-" + "a" * 30 + " 邮箱 me@example.com "
                                           "手机 13800138000 证件 110101199003077777"})
    rec.step(1, "tool", "t", True, 1, 1,
             args={"key": "ghp_" + "b" * 36, "pem": pem},
             results={"header": "Bearer abcdefghijklmnopqrstuvwxyz123456"})
    trace = rec.finish(outcome="success")

    blob = led.cas.get(trace["replay_bundle_cas"]).decode("utf-8")
    for leaked in ("sk-aaaa", "me@example.com", "13800138000", "110101199003077777",
                   "ghp-bbbb", "Bearer abcdefghij", "MIIEowIBAAKCAQEA"):
        assert leaked not in blob, leaked
    assert blob.count("[REDACTED:secret]") >= 4
    assert blob.count("[REDACTED:pii]") >= 3
    # trace 本体(轻量 steps)同样不含秘密
    assert "sk-" + "a" * 30 not in _canon(led.entries)
    led.close()


def test_step_truncation_marks_partial(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    rec = TraceRecorder(led, trace_id="tr_03", session_key="s")
    big = "y" * (REPLAY_BUNDLE_STEP_TRUNCATE_BYTES + 2048)
    rec.step(1, "tool", "dump", True, 10, 5, args={"blob": big}, results=None)
    trace = rec.finish(outcome="success")

    bundle = json.loads(led.cas.get(trace["replay_bundle_cas"]))
    entry = bundle["steps"][0]["args"]
    assert entry["partial"] is True
    assert entry["original_bytes"] > REPLAY_BUNDLE_STEP_TRUNCATE_BYTES
    assert len(entry["data"]) <= REPLAY_BUNDLE_STEP_TRUNCATE_BYTES
    led.close()


def test_trace_projection_rebuild(tmp_path):
    home = str(tmp_path / "home")
    led = DurableLedger(home)
    rec = TraceRecorder(led, trace_id="tr_04", session_key="s")
    rec.step(1, "tool", "t", True, 1, 1, args={"a": 1}, results={"b": 2})
    trace = rec.finish(outcome="failure", failure_analysis="超时",
                       cost_tokens=10, cost_cents=1)
    rows = led.query("SELECT * FROM traces")
    led.close()

    os.remove(os.path.join(home, "ledger", "index.sqlite"))
    led2 = DurableLedger.open(home)
    assert led2.query("SELECT * FROM traces") == rows
    bundle = json.loads(led2.cas.get(trace["replay_bundle_cas"]))
    assert bundle["steps"][0]["args"]["data"] == {"a": 1}
    led2.close()


def _canon(entries: list[dict]) -> str:
    return json.dumps([e["payload"] for e in entries], ensure_ascii=False, default=str)
