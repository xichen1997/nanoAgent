# NanoAgent

**NanoAgent** is a transactional, crash-resilient AI Agent Engine with full **process isolation** between its core layers. It was designed around a single philosophical constraint: *the Agent should be as dumb as possible about infrastructure*.

## Architecture

```
┌─────────────────────────────────────────────┐
│  RING 3 — Agent (agent/loop.py)              │  Subprocess A
│  Pure LLM loop. Sends Intent JSON.           │  Zero infra knowledge.
│  Cannot curl. Cannot log. Cannot recover.    │
└──────────────────┬──────────────────────────┘
                   │ HTTP JSON (localhost:7878)
┌──────────────────▼──────────────────────────┐
│  RING 0 — Sidecar (sidecar/)                 │  Subprocess B (or embedded SDK)
│  ├─ SidecarSession  ← core SDK class         │
│  ├─ Policy Engine (L1 effect · L2 quota)     │
│  ├─ Effect Log WAL (SQLite, atomic)          │
│  ├─ Semantic Checkpoint (messages + cursor)  │
│  └─ Capability Gateway (host-side fetch)     │
└──────────────┬───────────────────┬──────────┘
               │ Sandbox SDK       │ urllib
┌──────────────▼──────┐  ┌────────▼────────────┐
│  OpenSandbox (ctr)  │  │  External APIs/Web   │
│  Air-gapped, no creds│  │  Credentials here    │
└─────────────────────┘  └─────────────────────┘
```

The **Orchestrator** (`run.py`) owns both subprocesses and handles automatic crash recovery — if either crashes, it reads the last SQLite checkpoint and restarts in replay mode.

## Why Three Processes?

| Concern | Location | Rationale |
|---|---|---|
| LLM reasoning | `agent/loop.py` | Decoupled — swap in LangGraph or any other framework |
| Effect logging, policy, replay | `sidecar/` | Infra, not business logic |
| Dangerous code execution | OpenSandbox container | Physical isolation |
| Real credentials & API keys | Sidecar only | Never touches Agent or Sandbox |

## Tool Effect Classifications

The **Tool Registry** (`sidecar/policy.py`) is the single source of truth for every tool's side-effect type. The Agent calls tools by name only — it has zero knowledge of effects or replay.

| Tool | Effect | Replay Behavior |
|---|---|---|
| `bash_read` | `no_side_effects` | Skip — inject cached result |
| `bash_write` | `replayable` | Re-execute to reconstruct env |
| `bash_run` | `irreversible` | Return cached result, never re-run |
| `fetch_url` | `irreversible` | Return cached result, never re-call |

## Project Layout

```
opensandbox-agent/
├── run.py              ← Process orchestrator (start + crash recovery)
├── agent/
│   └── loop.py         ← Pure LLM loop (zero infra knowledge)
├── sidecar/
│   ├── session.py      ← Core SDK: SidecarSession (async class)
│   ├── server.py       ← Thin HTTP wrapper over SidecarSession
│   ├── policy.py       ← Tool Registry + Policy Engine
│   ├── effect_log.py   ← SQLite WAL (effect_log + checkpoints tables)
│   └── gateway.py      ← Host-side URL fetcher
├── demo/
│   └── crash_and_recover.py  ← Integration test: inject crash, resume
├── AGENT_LOGIC.md      ← Internals: replay, WAL, policy layers
└── .env
```

## Setup

**Prerequisites:** Python 3.10+, a running OpenSandbox instance, an Anthropic-compatible API key.

```bash
git clone https://github.com/xichen1997/nanoAgent.git
cd nanoAgent
pip install -r requirements.txt
```

Create `.env`:
```dotenv
ANTHROPIC_API_KEY="your-api-key"
ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic"
LLM_MODEL="MiniMax-M2.5"

SANDBOX_DOMAIN="localhost:8080"
SANDBOX_API_KEY="your-sandbox-key"
SANDBOX_IMAGE="ubuntu:22.04"

SIDECAR_PORT=7878   # optional, default 7878
```

## Usage

```bash
# Start everything (Sidecar + Agent)
python run.py

# With verbose sidecar logs
python run.py --debug

# Resume a crashed session by its UUID
python run.py --resume <session-id>
```

### Sidecar as a Python SDK

`SidecarSession` can be used directly without the HTTP layer:

```python
from sidecar import SidecarSession

session = SidecarSession(debug=True)
info = await session.start()

result = await session.execute_tool("bash_read", {"command": "uname -a"})
gen = await session.llm_generate(messages, system="...")

await session.end()
```

### Sidecar HTTP API (for manual testing)

```bash
python -m sidecar.server &

curl -X POST http://127.0.0.1:7878/session/start -H 'Content-Type: application/json' -d '{}'
curl -X POST http://127.0.0.1:7878/tool/execute \
  -H 'Content-Type: application/json' \
  -d '{"tool_name": "bash_read", "tool_input": {"command": "uname -a"}}'
```

## Crash & Recovery Demo

```bash
python demo/crash_and_recover.py
```

Runs a 3-step task (bash_write → bash_run → fetch_url), injects a crash after `bash_write`, then resumes from the SQLite checkpoint and verifies:
- `bash_write` executed before crash ✅
- `fetch_url` executed fresh after resume (was never called before crash) ✅
- Task did not restart from scratch ✅

## Data

Sessions are stored in `sidecar_data/effect_log.db` (SQLite). Two tables:

- **`effect_log`** — every tool call (Intent + Completion) and LLM generation, with idempotency keys
- **`checkpoints`** — atomic snapshot of LLM message history + effect log cursor, updated after each generation

## Roadmap (v2 Design)

These capabilities are designed but not yet implemented.

---

### 1. Agent-Agnostic State Machine

**Current:** `sidecar/session.py` is decoupled from HTTP transport but is still written assuming a single conversation-style agent loop.

**v2:** The Sidecar should become a pure **state machine** — given any sequence of `(intent, result)` pairs, it transitions state and returns the next instruction. It should have zero assumptions about:
- How the agent is implemented (LangGraph, raw loop, C++ state machine)
- Whether there is a human in the loop
- How many agents are talking to it at once

The ideal interface is a single endpoint:

```
POST /step
{ "session_id": "...", "event": { "type": "tool_result" | "user_message", ... } }
→ { "next_action": { "type": "call_llm" | "execute_tool" | "wait_user" | "done" } }
```

The agent becomes a dumb executor that calls `/step` in a loop. All orchestration intelligence lives in Ring 0.

---

### 2. MicroVM Snapshot Checkpointing

**Current:** `bash_write` steps are re-executed during replay to reconstruct sandbox state. This is fine for fast commands but breaks down for expensive operations (compiling LLVM, pulling GB-scale datasets).

**v2:** After any expensive `bash_write` completes, the Sidecar immediately takes a **physical snapshot** of the sandbox filesystem (e.g. Firecracker ext4 differential snapshot, or copy-on-write via something like Sprites.dev). The checkpoint JSON gains a `sandbox_state` pointer:

```json
{
  "step": 12,
  "llm_history": ["..."],
  "effect_log_cursor": "uuid-1234",
  "sandbox_state": {
    "provider": "firecracker",
    "snapshot_id": "snap_v12_compiled_lib"
  }
}
```

On crash recovery, instead of replaying `bash_write` commands, the scheduler restores the snapshot in milliseconds — the compiled library is already there.

The policy engine (`sidecar/policy.py`) would gain a new effect tier:

| Effect | Behavior | Snapshot? |
|---|---|---|
| `no_side_effects` | Skip on replay | No |
| `replayable_fast` | Re-execute on replay | No |
| `replayable_expensive` | Restore from snapshot | **Yes** |
| `irreversible` | Return cached result | No |

---

### 3. Concurrent Tasks — Trunk-based Fork & Patch

**Current:** One session = one agent = one sandbox. Serial execution only.

**v2:** Concurrency is orchestrated in Ring 3 (the agent), while Ring 0 maintains the **single source of truth** using a Git-style trunk model.

```
Ring 0: Main Trunk  ─────────────────────────────────────────────────►
                         │                    │
Ring 3:           Task A (fork)          Task B (fork)
                  sandbox_a              sandbox_b
                  runs async             runs async
                         │                    │
                  finishes first         still running
                         │
                  generates patch ──► Ring 0 applies to trunk
                                              │
                                      Task B gets merge conflict
                                      → killed, re-forked from new trunk
                                      → re-runs with updated context
```

**Rules:**
- Any new sandbox is always forked from the **latest trunk state**
- Completed tasks never overwrite trunk directly — they submit a **changesets** (git patch / SQL diff) via Intent IR to Ring 0
- Ring 0 serialises all trunk mutations (no concurrent writes)
- Conflict resolution is brute-force: kill the stale agent, give it the new trunk context, re-run

This maps naturally to the current design: effect log idempotency keys + WAL already provide the serialisation primitive. What's missing is the fork registry and changesets endpoint.

---

See [AGENT_LOGIC.md](AGENT_LOGIC.md) for the current implementation internals.

---
*Built to push the limits of Agent isolation, determinism, and crash-resilience.*
