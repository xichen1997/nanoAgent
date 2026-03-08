# NanoAgent Architecture

> **Design philosophy**: The Agent should be as dumb as possible about infrastructure. Separation of concerns is enforced through process isolation — the Agent, Sidecar, and Sandbox each live in their own process and can only communicate through well-defined interfaces.

---

## Table of Contents

1. [Process Topology](#1-process-topology)
2. [Layer Descriptions](#2-layer-descriptions)
   - [Ring 3 — Agent](#ring-3--agent-agentlooppy)
   - [Ring 0 — Sidecar](#ring-0--sidecar-sidecar)
   - [Execution Plane — OpenSandbox](#execution-plane--opensandbox)
3. [Data Flow](#3-data-flow)
4. [Effect Log & Checkpoints (WAL)](#4-effect-log--checkpoints-wal)
5. [Tool Effect Classification](#5-tool-effect-classification)
6. [Policy Engine](#6-policy-engine)
7. [Capability Gateway](#7-capability-gateway)
8. [Crash Recovery](#8-crash-recovery)
   - [Scenario A: Agent / Sidecar Crash](#scenario-a-agentsidecar-crash)
   - [Scenario B: Sandbox Container Crash](#scenario-b-sandbox-container-crash)
9. [Orchestrator](#9-orchestrator-runpy)
10. [HTTP API Reference](#10-sidecar-http-api)
11. [SDK Usage](#11-sdk-usage)
12. [Roadmap (v2)](#12-roadmap-v2)

---

## 1. Process Topology

```
┌──────────────────────────────────────────────────────────────────┐
│  run.py  ← Orchestrator (manages lifecycle + crash recovery)     │
│                                                                  │
│  ┌─────────────────────────────────┐                             │
│  │  Process A: Agent               │                             │
│  │  agent/loop.py                  │  Zero infra knowledge.      │
│  │  No API keys. No sandbox SDK.   │  Only speaks HTTP JSON.     │
│  └──────────────┬──────────────────┘                             │
│                 │  HTTP JSON  (localhost:7878)                    │
│  ┌──────────────▼──────────────────┐                             │
│  │  Process B: Sidecar             │                             │
│  │  sidecar/server.py              │  The infrastructure kernel. │
│  │  ├── SidecarSession (SDK core)  │  Holds all credentials.     │
│  │  ├── PolicyEngine               │  Enforces all policies.     │
│  │  ├── EffectLog (SQLite WAL)     │  Logs every action.         │
│  │  └── Capability Gateway         │  Only path to internet.     │
│  └──────────────┬──────────────────┘                             │
│                 │  opensandbox SDK  (remote API)                  │
│  ┌──────────────▼──────────────────┐  ┌─────────────────────┐   │
│  │  Execution Plane: OpenSandbox   │  │  External Internet   │   │
│  │  Air-gapped container           │  │  (via Gateway only)  │   │
│  │  No credentials. No net egress. │  └─────────────────────┘   │
│  └─────────────────────────────────┘                             │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Layer Descriptions

### Ring 3 — Agent (`agent/loop.py`)

The Agent is a **pure LLM reasoning loop**. It has no knowledge of:
- Effect types (`REPLAYABLE`, `IRREVERSIBLE`, etc.)
- SQLite, replay semantics, or checkpointing
- Sandbox SDK or credentials
- Policy enforcement

**What it does:**
1. `POST /session/start` → gets back restored messages (if resuming)
2. Reads user input (`>>>` prompt)
3. `POST /llm/generate` → gets LLM response (Sidecar calls the LLM + logs)
4. For each `tool_use` block: `POST /tool/execute` → gets result
5. Loops back to step 3

**Why separate process?**  
An Agent can be replaced with any other framework (LangGraph, Autogen, a human) without touching infrastructure. If the Agent crashes, the Sidecar survives with a clean checkpoint.

---

### Ring 0 — Sidecar (`sidecar/`)

The Sidecar is the **infrastructure kernel** — the single source of truth for all side effects.

| File | Responsibility |
|---|---|
| `session.py` | Core async SDK class (`SidecarSession`) |
| `server.py` | Thin HTTP wrapper over `SidecarSession` |
| `effect_log.py` | SQLite WAL: `effect_log` + `checkpoints` tables |
| `policy.py` | Tool Registry, `Effect` enum, `PolicyEngine` |
| `gateway.py` | Host-side HTTP fetcher (the only internet egress) |

**Core class:** `SidecarSession`
- One instance = one agent session
- Owns the sandbox, the effect log cursor, the replay state
- All methods are `async`; `server.py` bridges them to sync HTTP via `asyncio.run_coroutine_threadsafe`

**Why separate process?**  
The Sidecar holds API keys, sandbox credentials, and the authoritative effect log. Isolating it means a buggy/compromised Agent cannot read secrets or tamper with the log.

---

### Execution Plane — OpenSandbox

A remote container managed via the `opensandbox` Python SDK. It:
- Runs arbitrary bash commands
- Has **no outbound network access** (air-gapped)
- Has **no credentials** (can't reach external APIs directly)
- Is ephemeral — can be killed and replaced

All external HTTP calls must route through the **Capability Gateway** in the Sidecar.

---

## 3. Data Flow

### Normal (non-replay) turn:

```
User input
  │
  ▼
Agent  ──POST /llm/generate──►  Sidecar
                                  │ calls AsyncAnthropic
                                  │ logs llm_generation to effect_log
                                  │ saves checkpoint (messages + cursor)
                                  ◄──── returns content + stop_reason
  │ (stop_reason == tool_use)
  ▼
Agent  ──POST /tool/execute──►  Sidecar
                                  │ PolicyEngine.check()  [L1/L2/L3]
                                  │ calls Sandbox or Gateway
                                  │ logs tool_execution to effect_log
                                  ◄──── returns result + effect + replayed=False
  │
  ▼
(loop back to llm/generate)
```

### Replay turn (after resume):

```
POST /session/start?resume_session_id=<id>
  │
  ├── loads effect_log from SQLite
  ├── creates FRESH sandbox
  ├── fast-forwards REPLAYABLE events → re-runs bash_write commands to restore fs
  └── loads checkpoint (messages + cursor)

POST /tool/execute  →  Sidecar returns cached result from effect_log
                        replayed=True, no real execution
```

---

## 4. Effect Log & Checkpoints (WAL)

Both tables live in a single SQLite database: `sidecar_data/effect_log.db`, operating in WAL mode.

### `effect_log` table

One row per event, append-only:

| Column | Description |
|---|---|
| `id` | Auto-increment PK |
| `session_id` | UUID |
| `step` | Monotonically increasing per session |
| `event_type` | `tool_execution` or `llm_generation` |
| `tool_name` | e.g. `bash_write` |
| `effect` | `Effect.REPLAYABLE`, etc. |
| `command` | The command / prompt text |
| `result` | The output / response |
| `idempotency_key` | UUID per write (deduplication) |

### `checkpoints` table

One row per session, **upserted atomically** after each LLM generation:

| Column | Description |
|---|---|
| `session_id` | FK to sessions |
| `messages` | Full LLM message history (JSON) |
| `cursor` | `effect_log.step` at point of checkpoint |

**Why atomic?** The messages + cursor must be consistent — if a crash happens between saving messages and cursor, replay would use wrong events. SQLite `INSERT OR REPLACE` within a single transaction guarantees this.

---

## 5. Tool Effect Classification

Defined in `sidecar/policy.py` — the **only** place effect type is declared:

| Tool | Effect | On Replay |
|---|---|---|
| `bash_read` | `NO_SIDE_EFFECTS` | Skip — inject cached result |
| `bash_write` | `REPLAYABLE` | Re-execute — reconstructs sandbox filesystem |
| `bash_run` | `IRREVERSIBLE` | Return cached result — never re-run |
| `fetch_url` | `IRREVERSIBLE` | Return cached result — never re-call |

**Replay invariant:** After recovery, the sandbox filesystem is identical to pre-crash state because all `REPLAYABLE` (write) commands are re-run in order. `IRREVERSIBLE` commands are never duplicated.

---

## 6. Policy Engine

`PolicyEngine` in `sidecar/policy.py` runs three layers on every tool call:

```
L1 — Effect lookup     Is this tool in the TOOL_REGISTRY? What effect?
L2 — Dynamic quota     Has this session exceeded MAX_IRREVERSIBLE_PER_SESSION?
L3 — Blocklist         Does the command contain a forbidden pattern?
     (rm -rf /, mkfs, fork bomb, dd if=, ...)
```

If any layer raises `PolicyViolation`, the tool call returns an error to the Agent and nothing is logged.

---

## 7. Capability Gateway

`sidecar/gateway.py` is the **only path from the Sandbox to the external internet**.

```
Sandbox (air-gapped)
  │  [no internet]
  │
Agent  ──fetch_url──►  Sidecar (gateway.py)  ──urllib──►  External API
                       │ runs on host
                       │ has credentials if needed
                       │ logs the request + response to effect_log
                       ◄──── returns body to Agent
```

**Why this matters for replay:** If `fetch_url` were run inside the sandbox, re-running it during replay would cause real side effects (double POST, double payment, etc.). By routing through the Gateway, the Sidecar can intercept and return the cached response.

---

## 8. Crash Recovery

### Scenario A: Agent/Sidecar Crash

The full process dies. SQLite is the only survivor.

```
1. Orchestrator (run.py) detects agent exit code ≠ 0
2. Reads last session_id from /health (or from DB)
3. Kills old Sidecar subprocess
4. Starts new Sidecar subprocess (cold start)
5. Starts new Agent subprocess with --resume <session_id>
6. New SidecarSession:
   a. Loads effect_log from SQLite
   b. Creates fresh sandbox
   c. Re-runs all REPLAYABLE commands to restore filesystem
   d. Loads checkpoint (messages + cursor)
7. Agent resumes from last user message
8. Replay cursor feeds cached results for tools already in log
9. After replay exhausted → normal execution resumes
```

**Guarantee:** No `IRREVERSIBLE` action is ever duplicated. The Agent sees the same conversation history as before the crash.

---

### Scenario B: Sandbox Container Crash

The Sidecar process and SQLite survive. Only the sandbox container dies (OOM, eviction).

```
1. Tool call to sandbox raises exception (container unreachable)
2. Caller invokes session.revive_sandbox() (or POST /session/revive)
3. SidecarSession:
   a. Kills the dead sandbox reference (best-effort)
   b. Creates a fresh sandbox
   c. Reads all REPLAYABLE events from effect_log (step 0 → now)
   d. Re-runs each one to restore filesystem state
4. Tool calls resume normally on the new sandbox
```

**HTTP endpoint:** `POST /session/revive`  
**Test kill endpoint:** `POST /sandbox/kill` (test use only — kills container, keeps session)

---

## 9. Orchestrator (`run.py`)

```python
while retries <= MAX_RETRIES:
    sidecar = start_sidecar()          # subprocess B
    wait_for_sidecar()                 # poll /health
    agent   = start_agent(resume_id)   # subprocess A

    exit_code = agent.wait()

    if exit_code == 0:
        break                          # clean exit
    else:
        resume_id = get_last_session_id()  # from /health
        sidecar.terminate()
        retries += 1
        sleep(RETRY_WAIT_SEC)          # brief backoff
```

**Key design decisions:**
- Sidecar is always restarted too — avoids in-memory state from old session leaking into recovery
- `get_last_session_id()` reads from the running Sidecar's `/health` response before killing it
- Max 5 retries to prevent infinite crash loops

---

## 10. Sidecar HTTP API

All requests/responses are JSON over `http://127.0.0.1:7878`.

| Method | Path | Input | Output |
|---|---|---|---|
| `GET` | `/health` | — | `{status, session, replay}` |
| `POST` | `/session/start` | `{resume_session_id?, fork_at?}` | `{session_id, sandbox_id, replay_mode, restored_messages}` |
| `POST` | `/session/end` | `{}` | `{ok}` |
| `POST` | `/session/revive` | `{}` | `{sandbox_id, replayed_events}` |
| `POST` | `/sandbox/kill` | `{}` | `{killed}` *(test use only)* |
| `POST` | `/tool/execute` | `{tool_name, tool_input, tool_use_id?}` | `{result, effect, replayed}` |
| `POST` | `/llm/generate` | `{messages, system?}` | `{content, stop_reason, usage}` |

---

## 11. SDK Usage

`SidecarSession` can be used directly (embedded) without the HTTP layer:

```python
from sidecar import SidecarSession

session = SidecarSession(debug=True)

# Fresh session
info = await session.start()

# Resume from crash
info = await session.start(resume_session_id="<uuid>")

# Execute a tool
result = await session.execute_tool("bash_write", {"command": "echo hello > /tmp/x.txt"})
# → {"result": "(executed with no output)", "effect": "Effect.REPLAYABLE", "replayed": False}

# Call LLM
gen = await session.llm_generate(messages, system="You are a helpful assistant.")
# → {"content": [...], "stop_reason": "tool_use", "usage": {...}}

# Revive after sandbox crash
revival = await session.revive_sandbox()
# → {"sandbox_id": "new-uuid", "replayed_events": 3}

await session.end()
```

> **Note:** Embedding `SidecarSession` in the same process as application code is fine for tests and scripts, but production use should always run it as a separate subprocess via `server.py` to maintain process isolation.

---

## 12. Roadmap (v2)

### 1. Agent-Agnostic State Machine

Collapse all orchestration into a single `/step` endpoint in the Sidecar. The Agent becomes a dumb executor:

```
POST /step
{ "session_id": "...", "event": { "type": "tool_result" | "user_message", ... } }
→ { "next_action": { "type": "call_llm" | "execute_tool" | "wait_user" | "done" } }
```

Any agent implementation (LangGraph, Autogen, raw loop, human) can drive the Sidecar without modification.

---

### 2. MicroVM Snapshot Checkpointing

For expensive `REPLAYABLE` operations (compiling LLVM, pulling large datasets), re-executing from scratch on recovery is too slow. Instead:

1. After an expensive `bash_write` completes, take a **physical snapshot** of the sandbox filesystem (Firecracker ext4 diff or CoW).
2. Store `snapshot_id` in the checkpoint JSON alongside `llm_history` and `effect_log_cursor`.
3. On recovery, restore the snapshot directly (milliseconds) instead of replaying commands (minutes).

New effect tier in `policy.py`:

| Effect | Replay behavior | Snapshot |
|---|---|---|
| `no_side_effects` | Skip | No |
| `replayable_fast` | Re-execute command | No |
| `replayable_expensive` | Restore snapshot | **Yes** |
| `irreversible` | Return cached result | No |

---

### 3. Concurrent Tasks — Trunk-based Fork & Patch

**Current:** One session = one agent = one sandbox. Serial only.

**v2:** Ring 3 spawns parallel agents. Ring 0 maintains a **single source of truth** (the Main Trunk):

```
Ring 0: Main Trunk  ──────────────────────────────────────────────►
                         │                    │
Ring 3:           Task A (fork)          Task B (fork)
                  sandbox_a              sandbox_b  ← parallel
                         │                    │
                  finishes first         still running
                         │
                  submits changeset ──► Ring 0 applies to trunk
                                              │
                                      Task B detects conflict
                                      → killed, re-forked from new trunk
                                      → re-runs with updated context
```

**Rules:**
- Every new sandbox forks from the latest trunk state
- Completed tasks submit **changesets** (git patches / SQL diffs) to Ring 0 — never overwrite directly
- Ring 0 serialises all trunk mutations (WAL already provides this primitive)
- Conflict resolution: kill stale agent, re-fork, re-run (cheap, correct)

---

*Last updated: 2026-03-08*
