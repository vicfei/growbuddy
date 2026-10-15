# Security self-audit — Naga M5 (2026-10-11)

> Internal audit preparing M5's external-audit exit. Scope: the running system
> (channels, gateway, workers, storage, evolution loops). Threat model anchors:
> design doc §10 (threat matrix), §7 (R-levels & authorization), K.2/K.3
> (isolation), K.6 (PII). Status marks: ✅ enforced & tested · 🟡 partial · ⬜ pending.

## 1. Access plane

| Threat | Defense | Status |
|---|---|---|
| Remote access to the gateway | Loopback-only bind (constitution-grade: WebChat/gateway on 127.0.0.1; outer exposure requires an SSH tunnel) | ✅ |
| Unknown WeChat peers gaining rights | Trust derivation defaults guest (wechat) / untrusted (unknown channels); QR binders auto-owner, all grants in trust.json (0600) + `session.open` records the derivation source | ✅ C3a/C3b anchored |
| Guest escalating budget/tools/events | §10.1 single point (`task_budget` / `tools_for_trust` / event-visibility matrix); guest R0 — spawn & write-class tools structurally invisible | ✅ C2 anchored, cell-tested |
| Group-chat prompt injection | iLink bots have no group capability (verified in production); multi-peer deferred to future channels with per-peer trust | ✅ (n/a) |

## 2. Data & credentials

| Threat | Defense | Status |
|---|---|---|
| API keys leaking into traces/logs | Keys only read from 0600 credential files; two-stage redaction gate before bundles; W3 media flow never logs keys | ✅ |
| Media payloads as an attack surface | Fixed CDN base (self-constructed URLs — SSRF-immune), 20 MB cap, AES decrypt with strict PKCS#7 validation, vision prompts capped at 400 chars | ✅ |
| Ledger tampering | Hash chain + Merkle-sealed segments; crash-fuzz verified (1,000 iterations); tamper → loud refusal | ✅ |
| Ledger DB corruption | Single-writer (INV-4); SQLite WAL; projection rebuildable from the log | ✅ |

## 3. Runtime isolation

| Threat | Defense | Status |
|---|---|---|
| External plugin crashing the host | All external-source tools in subprocess workers; crash/hang → degraded call, worker rebuilt; lane consumers survive (M2 exit ③) | ✅ |
| Sub-agent runaway recursion | Authorization algebra (strictly-decreasing budgets, depth caps, wall-clock kill); crumb-quota spawn floor (real-bug fix) | ✅ |
| Adversarial skills entering the global library | Health-check adversarial patterns (pinned tests), shadow verification, R2+ human review queue; GEPA proposals health-gated with zero gate privileges | ✅ |
| Skill prompt-injection ("跳过确认" style) | Rejected at health check before any promotion cost | ✅ |

## 4. Constitution layer (this batch)

| Threat | Defense | Status |
|---|---|---|
| Runtime path mutating safety policy | R5 rules enumerated with live anchors (policy values pinned); drift → hard fuse (graceful halt, P0 recorded) | ✅ M5-1 |
| Constitution itself being rewritten | Constitution hash anchored in the append-only ledger (`constitution.anchored`); re-anchoring only via auditable RFC events | ✅ |
| Kernel twin drift | Kernel file hash pinned as rule C1 | ✅ |

## 5. Open items (for the external audit & M5 completion)

- ⬜ **External audit report** — requires a third party; this document is the
  input package. (Owner action.)
- 🟡 **K.3 full form** — privilege decisions (trust/R-gates/constitution checks)
  still run in-process; next step is an independent decision process over IPC so
  a fully compromised host process cannot forge them. Current compensations:
  ledger anchoring + tamper-evident fuse.
- 🟡 **Price table** — `2026-10-est` estimates; savings dashboard precision
  improves once confirmed prices are versioned in.
- ⬜ **Soak exit** — clock starts at M5-2 deploy; 30 days P0-free completes
  M5 exit ① (`naga soak` to monitor).
- 🟡 **Memory writes (§6.5)** — semantic-memory gating not yet wired (memory
  batch pending); channel trust already flows into every task.

## 6. Audit trails available to the external auditor

`ledger.head.log` (full event chain), `traces/` + ReplayBundles (per-task
redacted replays), `state/daemon.log` (operational slit), `promotion_requests`
/ `model_scorecard` / `workflow_runs` projections, register
`.context/register.md` (batch history with real-traffic evidence pointers).
