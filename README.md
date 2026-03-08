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
│  RING 0 — Sidecar (sidecar/server.py)        │  Subprocess B
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
| LLM reasoning | `agent/loop.py` | Decoupled — swap in LangGraph or any framework |
| Effect logging, policy, replay | `sidecar/server.py` | Infra, not business logic |
| Dangerous code execution | OpenSandbox container | Physical isolation |
| Real credentials & API keys | Sidecar only | Never touches Agent or Sandbox |

## Tool Effect Classifications

The **Tool Registry** (`sidecar/policy.py`) is the single source of truth for every tool's side-effect type. The Agent calls tools by name only — it has zero knowledge of effects or replay.

| Tool | Effect | Replay Behavior |
|---|---|---|
| `bash_read` | `no_side_effects` | Skip — inject cached result |
| `bash_write` | `replayable` | Re-execute to reconstruct env |
| `bash_run` | `irreversible` | Return cached fake success |
| `fetch_url` | `irreversible` | Return cached fake success |

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
# Start everything (Sidecar + Agent) with full logs
python run.py --debug

# Resume a crashed session by its UUID
python run.py --resume <session-id> --debug
```

The Sidecar can also be called directly via `curl` for testing without an Agent:

```bash
python -m sidecar.server &

# Start a session
curl -X POST http://127.0.0.1:7878/session/start -H 'Content-Type: application/json' -d '{}'

# Execute a tool
curl -X POST http://127.0.0.1:7878/tool/execute \
  -H 'Content-Type: application/json' \
  -d '{"tool_name": "bash_read", "tool_input": {"command": "uname -a"}}'
```

## Data

Sessions are stored in `sidecar_data/effect_log.db` (SQLite). Two tables:

- **`effect_log`** — every tool call (Intent + Completion) and LLM generation, with idempotency keys
- **`checkpoints`** — atomic snapshot of LLM message history + effect log cursor, updated after each generation

## Technical Documentation

See [AGENT_LOGIC.md](AGENT_LOGIC.md) for a full walkthrough of the replay mechanics, policy engine layers, and gateway design.

---
*Built to push the limits of Agent isolation, determinism, and crash-resilience.*
