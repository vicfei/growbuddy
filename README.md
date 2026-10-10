# Naga

[![CI](https://github.com/vicfei/naga/actions/workflows/ci.yml/badge.svg)](https://github.com/vicfei/naga/actions/workflows/ci.yml)

**A self-evolving AI agent runtime with a reversible, crash-recoverable core.**

Naga is a personal AI agent that answers on your chat channels, turns experience
into reusable skills, and treats *every* change it makes to itself as an
auditable, revertible ledger entry — so autonomy never costs you control.

[中文版 README](README.zh-CN.md)

## Why it is different

Most agent frameworks optimize for the demo. Naga optimizes for the other
11 months: what happens when the agent modifies its own state, a plugin
crashes at 3 a.m., a tool misbehaves, or you simply want yesterday back.
The architecture is built around that, from the kernel up:

- **Temporal composability — every change is reversible.** Any state change
  registers its inverse; the system can roll back to any historical point in
  LIFO order, including rolling back a rollback. Effects come in three
  auditable forms (register / perform / bracket), all journaled.
- **Spatial composability — declare, never wire.** Components declare
  dependencies and coeffects reactively; the runtime drives lifecycles.
  Shadow branches plug in without polluting the live system.
- **Confluence as a tested invariant.** Any sequence of plugin
  activation/deactivation converges to the same quiescent state as a
  one-shot dependency-ordered composition. This is not a hope — it is
  property-based tested, shrunk-counterexample-hardened.
- **RSI with a leash — recursive self-improvement under tiered authority.**
  Experience sediments into versioned skill assets (a Git-like content-addressed
  tree); promotion to global scope requires shadow verification and
  R-level authorization. Exploration happens in branches; merging happens
  at gates. (Meta-improvement layer lands in M4.)
- **A ledger, not a log.** An append-only, hash-chained ledger with
  Merkle-sealed segments, snapshots, and a SQLite projection that can be
  rebuilt from the log at any time. Crash recovery is fuzz-tested at
  1,000 iterations — torn writes, mid-append deaths, and tampering all
  recover or refuse loudly.
- **Capability security end to end.** Peers derive trust levels
  (owner / known / guest / untrusted); budgets, tool visibility
  (R0 read-only … R4 destructive), and event visibility are enforced from
  a single declarative table — a guest can use the agent without ever
  seeing a write-capability or another session's events.
- **Crash containment by construction.** External-source plugins execute in
  isolated worker subprocesses: a segfault, hang, or memory leak cannot take
  down the host; the call degrades, the worker is rebuilt, the task
  continues.
- **Real channels, real media.** Ships with a WeChat personal-bot channel
  (text, typing indicator, chunked replies, images with on-device decryption
  + CAS archiving + vision recognition, voice transcription) and a WebChat
  console — all routed through one session-serializing gateway.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ L3  Meta-improvement (RSI spark)                          │
│     reflector · optimizer · R-level gates · shadow verify │
├──────────────────────────────────────────────────────────┤
│ L2  Experience assets                                     │
│     skill library (version tree) · memory · curation      │
├──────────────────────────────────────────────────────────┤
│ L1  Execution                                             │
│     agent loop · trace recorder · parallel sessions       │
├──────────────────────────────────────────────────────────┤
│ L0.5 Access plane                                         │
│     channels · session routing · lane queues · trust      │
├──────────────────────────────────────────────────────────┤
│ L0  Composable kernel                                     │
│     Context · reversible effects · coeffects · ledger     │
└──────────────────────────────────────────────────────────┘
```

Key invariants enforced by the kernel (each backed by tests):

| # | Invariant |
|---|---|
| INV-1 | Confluence — arbitrary activation orders converge |
| INV-2 | Reversibility — rollback to any point, including rollbacks |
| INV-3 | Monotonic capability decay down the spawn chain |
| INV-4 | Single writer — shared state only via the serial ledger |
| INV-5 | Constitution unreachable at runtime |
| INV-6 | Mode ratchet — privileges auto-lower, never auto-raise |

## Status

| Milestone | Scope | Exit evidence |
|---|---|---|
| **M0 — composable kernel** ✅ | Context, effect triad, reactive coeffects, durable ledger, CAS, rollback | Confluence PBT 5/5; crash-recovery fuzz (1,000 iterations, zero inconsistencies); pyright strict; CI green |
| **M1 — it runs** ✅ | LLM adapters, agent loop, skill sedimentation/reuse, WebSocket gateway + WebChat, traces & replay bundles | End-to-end dialogue, skill reuse, non-empty replay bundles — all with real LLM traffic |
| **M2 — it connects** ✅ | WeChat channel (text/typing/media), trust-level degradation, worker isolation, hot plug/unplug | Real WeChat traffic (incl. image recognition & voice); trust matrix tested cell-by-cell; worker-crash penetration tests; bind→unbind→rebind without restart |
| M3 — it forks | Parallel sessions, sub-agents, authorization algebra, branch model | — |
| M4 — it evolves | Promotion pipeline, shadow verification, workflow compilation | — |
| M5 — hands off | Constitution hardening, 30-day soak | — |

The authoritative design baseline lives in [`docs/design/`](docs/design/)
(frozen, R3-graded; changes go through RFC). Research notes are in
[`docs/research/`](docs/research/).

## Quick start

Requires Python 3.12+.

```bash
git clone https://github.com/vicfei/naga.git
cd naga
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest -q                    # full suite (unit + PBT + fuzz + channels)
.venv/bin/ruff check src tests         # lint
.venv/bin/pyright src                  # strict type check
```

Running the daemon (gateway on loopback + WeChat channel) additionally needs a
credentials file at `~/.naga/credentials/providers.json` (0600) — see
`src/naga/llm.py` for the expected provider entries.

```bash
naga daemon start      # http://127.0.0.1:18789 — WebChat console
naga doctor            # self-check
```

## Repository layout

```
src/naga/
  kernel.py        # L0 seed kernel (frozen baseline twin; in-place evolution follows RFCs)
  ledger_store.py  # durable ledger: append log, Merkle seals, snapshots, CAS, SQLite projection
  gateway.py       # WebSocket gateway: lane queues, trust policy, event visibility matrix
  agent.py         # agent loop with tracing and skill sedimentation
  skills.py        # version-tree skill store (content-addressed)
  trust.py         # peer trust derivation (allowlist + channel defaults)
  worker.py        # subprocess isolation for external-source plugins
  wechat.py        # WeChat iLink channel: polling, typing, chunking, media, vision
  daemon.py cli.py # runtime host and CLI
docs/design/       # frozen design baseline (4 documents + spec-constants + checker)
docs/research/     # research notes and RFC material
tests/             # unit, property-based, crash-fuzz, channel and integration suites
```

## Contributing

Issues and pull requests are written in **English** (the project's working
language for public artifacts; either language is fine in discussion).
Changes go through issue → branch → PR → CI (ruff / pyright strict /
pytest / freeze checker) → merge commit. The frozen design baseline under
`docs/design/` is never edited in place — propose changes via RFC
(see `docs/research/rfc-m2-registry-addendum.md` for the format).

## License

[MIT](LICENSE) © 2026 vicfei
