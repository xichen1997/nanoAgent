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
│  RING 0 — Sidecar (sidecar/)                 │  Subprocess B
│  ├─ SidecarSession  ← core SDK class         │
│  ├─ Policy Engine (L1 effect · L2 quota)     │
│  ├─ Effect Log WAL (SQLite, atomic)          │
│  ├─ Filesystem Snapshots (tar.gz)            │
│  ├─ Trunk / Fork Registry (parallel tasks)  │
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
| `bash_write` | `replayable_fast` | Re-execute to reconstruct env |
| `bash_build` | `replayable_expensive` | ⚡ Re-execute **and** take filesystem snapshot afterwards |
| `bash_run` | `irreversible` | Return cached result, never re-run |
| `fetch_url` | `irreversible` | Return cached result, never re-call |

**Networking rules:**
- External internet → `fetch_url` (routes through host Gateway, sandbox is air-gapped)
- Internal sandbox services (localhost) → `bash_run curl localhost:port` (container-local, no Gateway needed)

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
│   ├── effect_log.py   ← SQLite WAL (all tables: effect_log, checkpoints, snapshots, trunk, forks)
│   └── gateway.py      ← Host-side URL fetcher
├── demo/
│   ├── crash_and_recover.py  ← Integration test: agent/sidecar crash + sandbox crash
│   └── concurrent_tasks.py   ← Integration test: fork / conflict / refork cycle
├── ARCHITECTURE.md     ← Full system architecture reference
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
# Agent/Sidecar crash + sandbox container crash
python demo/crash_and_recover.py
```

Runs two scenarios, verifying:
- `bash_write` replayed correctly after agent crash ✅
- `fetch_url` never duplicated ✅
- Sandbox crash recovered via `revive_sandbox()` ✅

## Concurrent Tasks Demo

```bash
python demo/concurrent_tasks.py
```

Exercises the trunk-based fork/commit model:
- Fork A and Fork B spawn from the same trunk ✅
- Fork A commits first → new trunk ✅
- Fork B detects conflict, aborts ✅
- Fork B re-forks from A's trunk, inherits A's files ✅
- Fork B commits successfully → final trunk ✅

## Data

Sessions are stored in `sidecar_data/effect_log.db` (SQLite, WAL mode). Six tables:

| Table | Description |
|---|---|
| `sessions` | Session registry |
| `effect_log` | Every tool call + LLM generation (append-only) |
| `checkpoints` | LLM message history + effect cursor + snapshot pointer (atomic upsert) |
| `snapshots` | Filesystem .tar.gz snapshot records for `REPLAYABLE_EXPENSIVE` recovery |
| `trunk` | Trunk version history (canonical sandbox states for parallel tasks) |
| `forks` | Fork registry with status (active / committed / conflicted / aborted) |

## Roadmap (v3)

### `/step` — Agent-Agnostic State Machine

Move all orchestration into the Sidecar. The Agent becomes a pure executor:

```
POST /step
{ "session_id": "...", "event": { "type": "tool_result" | "user_message", ... } }
→ { "next_action": { "type": "call_llm" | "execute_tool" | "wait_user" | "done" } }
```

Any agent implementation (LangGraph, Autogen, raw loop, human-in-the-loop) drives the same Sidecar without changes.

---

*Built to push the limits of Agent isolation, determinism, and crash-resilience.*
