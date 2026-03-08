# NanoAgent Architecture

> **Design philosophy**: The Agent should be as dumb as possible about infrastructure. Separation of concerns is enforced through process isolation — the Agent, Sidecar, and Sandbox each live in their own process and can only communicate through well-defined interfaces.

---

## Table of Contents

1. [Process Topology](#1-process-topology)
2. [Layer Descriptions](#2-layer-descriptions)
3. [Data Flow](#3-data-flow)
4. [Effect Log & Checkpoints (WAL)](#4-effect-log--checkpoints-wal)
5. [Tool Effect Classification](#5-tool-effect-classification)
6. [Filesystem Snapshots](#6-filesystem-snapshots)
7. [Policy Engine](#7-policy-engine)
8. [Capability Gateway](#8-capability-gateway)
9. [Crash Recovery](#9-crash-recovery)
10. [Concurrent Trunk Tasks](#10-concurrent-trunk-tasks)
11. [Orchestrator](#11-orchestrator-runpy)
12. [HTTP API Reference](#12-sidecar-http-api)
13. [SDK Usage](#13-sdk-usage)
14. [Roadmap (v3)](#14-roadmap-v3)

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
│  │  ├── Snapshot Store (tar.gz)    │  Fast crash recovery.       │
│  │  ├── Trunk/Fork Registry        │  Parallel task state.       │
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
- Effect types (`REPLAYABLE_FAST`, `IRREVERSIBLE`, etc.)
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
| `effect_log.py` | SQLite WAL — all 6 tables + schema migration |
| `policy.py` | Tool Registry, `Effect` enum, `PolicyEngine` |
| `gateway.py` | Host-side HTTP fetcher (the only internet egress) |

**Core class:** `SidecarSession`
- One instance = one agent session
- Owns the sandbox, the effect log cursor, the replay state, snapshot bookkeeping
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
Internal sandbox services (localhost) are reachable via `bash_run curl localhost:port`.

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
                                  │ saves checkpoint (messages + cursor + snapshot_id)
                                  ◄──── returns content + stop_reason
  │ (stop_reason == tool_use)
  ▼
Agent  ──POST /tool/execute──►  Sidecar
                                  │ PolicyEngine.check()  [L1/L2/L3]
                                  │ calls Sandbox or Gateway
                                  │ logs tool_execution to effect_log
                                  │ if REPLAYABLE_EXPENSIVE → _take_snapshot()
                                  ◄──── returns result + effect + replayed=False
  │
  ▼
(loop back to llm/generate)
```

### Replay turn (after resume):

```
POST /session/start {resume_session_id}
  │
  ├── loads effect_log from SQLite
  ├── creates FRESH sandbox
  ├── check checkpoint.snapshot_id:
  │   ├── snapshot exists → _restore_snapshot() (fast: tar extract)
  │   │                     replay REPLAYABLE* events AFTER snapshot step only
  │   └── no snapshot     → replay ALL REPLAYABLE* events from step 0
  └── loads checkpoint (messages + cursor)

POST /tool/execute  →  Sidecar returns cached result from effect_log
                        replayed=True, no real execution
```

---

## 4. Effect Log & Checkpoints (WAL)

All tables live in `sidecar_data/effect_log.db` (SQLite, WAL mode).

### `effect_log` table

One row per event, **append-only**:

| Column | Description |
|---|---|
| `id` | Auto-increment PK |
| `session_id` | UUID |
| `step` | Monotonically increasing per session |
| `event_type` | `tool_execution` or `llm_generation` |
| `tool_name` | e.g. `bash_build` |
| `effect` | `replayable_fast`, `replayable_expensive`, `irreversible`, etc. |
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
| `snapshot_id` | Latest filesystem snapshot at this checkpoint (nullable) |

**Why atomic?** The messages + cursor + snapshot_id must be consistent — a partial write would corrupt replay. SQLite `INSERT OR REPLACE` within a single transaction guarantees this.

### `snapshots`, `trunk`, `forks` tables

See [Section 6](#6-filesystem-snapshots) and [Section 10](#10-concurrent-trunk-tasks).

---

## 5. Tool Effect Classification

Defined in `sidecar/policy.py` — the **only** place effect type is declared:

| Tool | Effect | On Replay | Snapshot? |
|---|---|---|---|
| `bash_read` | `NO_SIDE_EFFECTS` | Skip — inject cached result | No |
| `bash_write` | `REPLAYABLE_FAST` | Re-execute — reconstructs sandbox filesystem | No |
| `bash_build` | `REPLAYABLE_EXPENSIVE` | Re-execute — reconstructs sandbox filesystem | **Yes** |
| `bash_run` | `IRREVERSIBLE` | Return cached result — never re-run | No |
| `fetch_url` | `IRREVERSIBLE` | Return cached result — never re-call | No |

**Backwards-compat:** The legacy `REPLAYABLE` value (from old DB rows) is treated identically to `REPLAYABLE_FAST`.

**Networking split:**
- `fetch_url` — external internet (public APIs, https://) — routes through host Gateway since the sandbox is air-gapped
- `bash_run curl localhost:port` — internal sandbox services (container-local, no Gateway needed)

---

## 6. Filesystem Snapshots

After any `REPLAYABLE_EXPENSIVE` (`bash_build`) tool call completes, the Sidecar:

1. Runs `tar -czf - /tmp /workspace /root /app ...` inside the sandbox
2. Base64-encodes stdout for safe transport over sandbox API
3. Decodes and writes a `.tar.gz` file to `sidecar_data/snapshots/<session_id>/`
4. Inserts a row into the `snapshots` table
5. Upserts the `checkpoints.snapshot_id` pointer

On crash recovery:

```
checkpoint.snapshot_id present?
  YES → _restore_snapshot():
          1. Read .tar.gz from host disk
          2. Base64-encode, write in chunks to /tmp/_snap_restore.b64 inside sandbox
          3. base64 -d | tar -xzf - -C / to restore filesystem
          4. Replay only REPLAYABLE* events AFTER snapshot step
  NO  → Full replay from step 0 (legacy path)
```

**`snapshots` table:**

| Column | Description |
|---|---|
| `snapshot_id` | UUID PK |
| `session_id` | Owner session |
| `step` | Effect log step this snapshot captures up to |
| `storage_path` | Abs path to `.tar.gz` on Sidecar host |
| `size_bytes` | File size |

---

## 7. Policy Engine

`PolicyEngine` in `sidecar/policy.py` runs three layers on every tool call:

```
L1 — Effect lookup     Is this tool in the TOOL_REGISTRY? What effect?
L2 — Dynamic quota     Has this session exceeded MAX_IRREVERSIBLE_PER_SESSION?
L3 — Blocklist         Does the command contain a forbidden pattern?
     (rm -rf /, mkfs, fork bomb, dd if=, ...)
```

Helper: `PolicyEngine.is_replayable(effect)` — returns `True` for `REPLAYABLE_FAST`, `REPLAYABLE_EXPENSIVE`, and legacy `REPLAYABLE`.

If any layer raises `PolicyViolation`, the tool call returns an error to the Agent and nothing is logged.

---

## 8. Capability Gateway

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

For **internal** sandbox services the Gateway is not needed:

```
Agent  ──bash_run "curl localhost:8080/api"──►  Sidecar  ──Sandbox SDK──►  container-local
```

**Why this matters for replay:** `fetch_url` results are cached in `effect_log`. On replay, the Sidecar returns the cached response without hitting the real API — no double payments, no double emails.

---

## 9. Crash Recovery

### Scenario A: Agent/Sidecar Crash

The full process dies. SQLite is the only survivor.

```
1. Orchestrator (run.py) detects agent exit code ≠ 0
2. Reads last session_id from /health (before killing Sidecar)
3. Kills old Sidecar subprocess
4. Starts new Sidecar subprocess (cold start)
5. Starts new Agent subprocess with --resume <session_id>
6. New SidecarSession.start():
   a. Loads effect_log from SQLite
   b. Creates fresh sandbox
   c. checkpoint.snapshot_id present?
      YES → restore snapshot (fast) + replay events after snapshot
      NO  → replay all REPLAYABLE* commands from step 0
   d. Loads checkpoint (messages + cursor)
7. Agent resumes from last user message
8. Replay cursor feeds cached results for already-logged tools
9. After replay exhausted → normal execution resumes
```

**Guarantee:** No `IRREVERSIBLE` action is ever duplicated. The Agent sees the same conversation history as before the crash.

---

### Scenario B: Sandbox Container Crash

The Sidecar process and SQLite survive. Only the sandbox container dies (OOM, eviction).

```
1. Tool call to sandbox raises exception (container unreachable)
2. Caller invokes POST /session/revive
3. SidecarSession.revive_sandbox():
   a. Kills the dead sandbox reference (best-effort)
   b. Creates a fresh sandbox
   c. Latest snapshot present?
      YES → restore snapshot + replay events after snapshot step
      NO  → replay all REPLAYABLE* events from step 0
4. Tool calls resume normally on new sandbox
```

**HTTP endpoint:** `POST /session/revive`  
**Test kill endpoint:** `POST /sandbox/kill` (test use only — kills container, keeps session)

---

## 10. Concurrent Trunk Tasks

The Trunk model enables multiple agent sessions to work in parallel on isolated sandboxes, committing back to a shared canonical state.

### Concept

```
Ring 0: Main Trunk  ───────────────────────────────────────────────►
                         │                    │
Ring 3:           Task A (fork)          Task B (fork)
                  sandbox_a              sandbox_b
                  runs async             runs async
                         │                    │
                  finishes first         still running
                         │
                  POST /trunk/commit ──► new trunk (snapshot of A's sandbox)
                                               │
                                       Task B: POST /trunk/commit
                                       → {conflict: true}
                                       → POST /trunk/abort
                                       → POST /trunk/fork (new trunk)
                                       → re-do work, inherits A's files
                                       → POST /trunk/commit → {ok}
```

### DB Tables

**`trunk`** — one row per canonical version:

| Column | Description |
|---|---|
| `trunk_id` | UUID PK |
| `parent_id` | Previous trunk version (NULL for root) |
| `snapshot_path` | `.tar.gz` of the trunk sandbox state |
| `effect_cursor` | Effect log step this trunk represents |

**`forks`** — one row per parallel task session:

| Column | Description |
|---|---|
| `fork_id` | UUID PK (= session_id) |
| `trunk_id` | Trunk version this fork diverged from |
| `status` | `active` / `committed` / `conflicted` / `aborted` |
| `changeset` | JSON list of REPLAYABLE commands added by this fork |

### Rules

- Every new fork starts from the **latest trunk snapshot** (filesystem already restored)
- `commit_to_trunk`: snapshots the **fork's own live sandbox** (already has all the work) → new trunk
- Conflict if `fork.trunk_id ≠ current_trunk.trunk_id` at commit time → must re-fork
- Conflict resolution: kill stale fork, re-fork from new trunk, re-run task

---

## 11. Orchestrator (`run.py`)

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

## 12. Sidecar HTTP API

All requests/responses are JSON over `http://127.0.0.1:7878`.

### Core

| Method | Path | Input | Output |
|---|---|---|---|
| `GET` | `/health` | — | `{status, session, replay}` |
| `POST` | `/session/start` | `{resume_session_id?, fork_at?}` | `{session_id, sandbox_id, replay_mode, restored_messages}` |
| `POST` | `/session/end` | `{}` | `{ok}` |
| `POST` | `/session/revive` | `{}` | `{sandbox_id, replayed_events}` |
| `POST` | `/sandbox/kill` | `{}` | `{killed}` *(test use only)* |
| `POST` | `/tool/execute` | `{tool_name, tool_input, tool_use_id?}` | `{result, effect, replayed}` |
| `POST` | `/llm/generate` | `{messages, system?}` | `{content, stop_reason, usage}` |

### Snapshots

| Method | Path | Input | Output |
|---|---|---|---|
| `POST` | `/snapshot/take` | `{}` | `{snapshot_id}` |
| `GET` | `/snapshot/list` | — | `{snapshots: [...]}` |

### Trunk / Fork

| Method | Path | Input | Output |
|---|---|---|---|
| `POST` | `/trunk/init` | `{}` | `{trunk_id, snapshot_path}` |
| `POST` | `/trunk/fork` | `{trunk_id?}` | `{session_id, fork_id, trunk_id, sandbox_id}` |
| `POST` | `/trunk/commit` | `{}` | `{ok, new_trunk_id}` or `{conflict: true, current_trunk_id}` |
| `POST` | `/trunk/abort` | `{}` | `{ok}` |
| `GET` | `/trunk/status` | — | `{trunk_id, effect_cursor, active_forks}` |

---

## 13. SDK Usage

`SidecarSession` can be used directly (embedded) without the HTTP layer:

```python
from sidecar import SidecarSession

session = SidecarSession(debug=True)

# Fresh session
info = await session.start()

# Resume from crash (snapshot-aware)
info = await session.start(resume_session_id="<uuid>")

# Execute a tool (REPLAYABLE_EXPENSIVE → auto-snapshots)
result = await session.execute_tool("bash_build", {"command": "pip install torch"})
# → {"result": "...", "effect": "replayable_expensive", "replayed": False}
# → snapshot taken automatically after execution

# Call LLM (checkpoint saved with snapshot_id pointer)
gen = await session.llm_generate(messages, system="You are a helpful assistant.")
# → {"content": [...], "stop_reason": "tool_use", "usage": {...}}

# Revive after sandbox crash (snapshot-aware)
revival = await session.revive_sandbox()
# → {"sandbox_id": "new-uuid", "replayed_events": 2}

# Trunk/Fork operations
trunk = await session.init_trunk()
fork_info = await session.fork_from_trunk(trunk_id="<uuid>")
commit = await session.commit_to_trunk()
# → {"ok": True, "new_trunk_id": "..."} or {"conflict": True, "current_trunk_id": "..."}

await session.end()
```

> **Note:** Embedding `SidecarSession` in the same process as application code is fine for tests and scripts, but production use should always run it as a separate subprocess via `server.py` to maintain process isolation.

---

## 14. Roadmap (v3)

### `/step` — Agent-Agnostic State Machine

Move all orchestration logic into Ring 0. The Agent becomes a pure, stateless executor:

```
POST /step
{ "session_id": "...", "event": { "type": "tool_result" | "user_message" | "approval_granted", ... } }
→ { "next_action": { "type": "call_llm" | "execute_tool" | "wait_user" | "wait_approval" | "done" } }
```

Benefits:
- Any agent (LangGraph, Rust, Python, human) drives the same Sidecar without modification
- Human-in-the-loop approval becomes a first-class `wait_approval` action
- Concurrent tool dispatch policy (parallel vs. sequential) centralized in Ring 0
- Replay precision improves to single-event granularity

---

*Last updated: 2026-03-08*
