# NanoAgent — Architecture & Logic

## System Overview

NanoAgent is built around **three physically isolated processes** that communicate via HTTP JSON.

```
Agent (loop.py)  ──HTTP──►  Sidecar (server.py)  ──►  OpenSandbox / External APIs
  "想"                          "管"                         "干"
```

- **Agent**: Pure LLM reasoning loop. Sends Intents, receives Results. Zero knowledge of logging, replay, or credentials.
- **Sidecar**: Infrastructure kernel. Owns the sandbox, the Effect Log, the Policy Engine, and the LLM call recording.
- **Sandbox**: Air-gapped execution container. No credentials. No direct internet. One network hole back to Sidecar.

---

## 1. Tool Registry & Policy Engine (`sidecar/policy.py`)

Effect types are declared **once** in the `TOOL_REGISTRY` — not in tool names. The Agent calls tools by name only.

| Tool | Effect | Replay Behavior |
|---|---|---|
| `bash_read` | `no_side_effects` | **Skip** — inject cached result to prevent data drift |
| `bash_write` | `replayable` | **Re-execute** — rebuild sandbox environment |
| `bash_run` | `irreversible` | **Block** — return cached result, never re-run |
| `fetch_url` | `irreversible` | **Block** — return cached result, never re-call |

**Policy Layers:**
- **L1** (static): Effect type from registry — applied to every call
- **L2** (dynamic quota): Alert if `irreversible` calls exceed per-session threshold
- **L3** (blocklist): Hard-reject commands matching dangerous patterns (`rm -rf /`, fork bombs, etc.)

---

## 2. Effect Log WAL (`sidecar/effect_log.py`)

All tool calls and LLM generations are recorded to **SQLite** (`sidecar_data/effect_log.db`) — not JSONL.

**Two tables, one DB (for atomic writes):**

```sql
-- Every tool call with its command, result, idempotency key, and effect type
effect_log (session_id, step, event_type, tool_name, effect, command, result, idempotency_key, timestamp)

-- LLM message history + effect_log cursor, updated atomically after every generation
checkpoints (session_id, messages JSON, cursor)
```

The `checkpoints` cursor and the `effect_log` step are written in the **same SQLite transaction**, eliminating any possibility of the two getting out of sync during crash recovery.

---

## 3. Capability Gateway (`sidecar/gateway.py`)

The Sandbox container has **no direct internet access**. All external HTTP calls (`fetch_url`) go through `gateway.py` running on the Sidecar host.

- During normal execution: makes the real HTTP call, logs to effect_log as `irreversible`
- During replay: **returns the cached result** from effect_log — the external endpoint is never hit again

This prevents:
- **Data Drift**: replaying a "read" that would now return different data
- **Duplicate Side Effects**: e.g., making a payment API call twice

---

## 4. Replay & Crash Recovery (`sidecar/server.py` + `run.py`)

When `run.py` detects that the Agent subprocess exited with a non-zero code:

1. Reads the last `session_id` from the Sidecar's `/health` endpoint
2. Restarts both processes in **resume mode** (`--resume <session_id>`)
3. Sidecar loads the checkpoint: restores LLM message history and replays `replayable` bash commands into a fresh sandbox
4. Sidecar sets `auto_trigger=True` if the last checkpoint message is a user turn — Agent resumes LLM generation automatically, no human input needed

---

## 5. Orchestrator (`run.py`)

```
run.py
  ├─ spawn sidecar/server.py   (subprocess, port 7878)
  ├─ wait for /health OK
  ├─ spawn agent/loop.py       (subprocess)
  └─ on crash: sleep 3s → restart both in resume mode (up to MAX_RETRIES=5)
```
