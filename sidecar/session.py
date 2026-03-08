"""
sidecar/session.py — Core SDK: SidecarSession

This is the single source of business logic for the Sidecar layer.
It is a clean async class with no HTTP, no global state, no threading.

Usage (embedded):
    session = SidecarSession(debug=True)
    info = await session.start()
    result = await session.execute_tool("bash_read", {"command": "uname -a"})
    gen = await session.llm_generate(messages, system="...")
    await session.end()

Usage (via HTTP server):
    sidecar/server.py creates one SidecarSession and delegates all
    route handlers to it via asyncio.run_coroutine_threadsafe.
"""
import base64
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

from sidecar.effect_log import (
    create_fork,
    create_session,
    create_trunk,
    get_effect_log_from,
    get_fork,
    get_latest_snapshot_before,
    get_latest_trunk,
    get_snapshot,
    get_trunk,
    init_db,
    list_snapshots,
    load_checkpoint,
    log_llm_event,
    log_tool_event,
    save_checkpoint,
    save_snapshot,
    update_fork_status,
)
from sidecar.gateway import fetch_url
from sidecar.policy import Effect, PolicyEngine, build_llm_tool_schema

load_dotenv()

# Directories snapshotted inside the sandbox (avoid snapshotting all of /)
_SNAPSHOT_DIRS = ["/tmp", "/workspace", "/root", "/app", "/home"]


class SidecarSession:
    """
    The Capability Gateway SDK.

    One instance = one agent session (one sandbox, one effect log cursor).
    All methods are async and thread-safe to call from a synchronous context
    via asyncio.run_coroutine_threadsafe (used by server.py).
    """

    def __init__(self, debug: bool = False):
        self.debug = debug
        self._session_id: Optional[str] = None
        self._sandbox: Optional[Sandbox] = None
        self._policy = PolicyEngine()
        self._replay_mode: bool = False
        self._replay_events: list = []
        self._replay_cursor: int = 0
        self._current_snapshot_id: Optional[str] = None  # latest snapshot for this session

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self._llm = AsyncAnthropic(api_key=api_key, base_url=base_url)
        self._model = os.environ.get("LLM_MODEL", "claude-3-5-sonnet-20241022")

        init_db()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_replay(self) -> bool:
        return self._replay_mode

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(
        self,
        resume_session_id: Optional[str] = None,
        fork_at: Optional[int] = None,
    ) -> dict:
        """
        Create a Sandbox and initialise the session.
        If resume_session_id is given, restore checkpoint and prepare replay events.

        Snapshot-aware replay:
          1. If checkpoint has a snapshot_id, restore that snapshot (fast).
          2. Replay only REPLAYABLE* events AFTER the snapshot step.
          3. If no snapshot, replay all events from step 0 (legacy path).

        Returns a dict with session_id, sandbox_id, replay_mode, and restored_messages.
        """
        self._policy = PolicyEngine()

        if resume_session_id:
            self._session_id = resume_session_id
            self._replay_mode = True
            self._replay_events = get_effect_log_from(
                self._session_id, from_step=fork_at or 0
            )
            self._replay_cursor = 0
            self._log(
                f"REPLAY mode — session {self._session_id}, "
                f"{len(self._replay_events)} cached events."
            )
        else:
            self._session_id = create_session()
            self._replay_mode = False
            self._replay_events = []
            self._log(f"NEW session: {self._session_id}")

        # Create a fresh sandbox every time
        self._sandbox = await self._create_sandbox()
        self._log(f"Sandbox created: {self._sandbox.id}")

        # ── Snapshot-aware replay ────────────────────────────────────────────
        if self._replay_mode:
            messages, cursor, snapshot_id = load_checkpoint(self._session_id)
            self._current_snapshot_id = snapshot_id

            if snapshot_id:
                snap = get_snapshot(snapshot_id)
                if snap and Path(snap["storage_path"]).exists():
                    self._log(f"Restoring from snapshot {snapshot_id} (step {snap['step']})")
                    await self._restore_snapshot(snapshot_id)
                    # Replay only events AFTER the snapshot's step
                    remaining = [e for e in self._replay_events if e["step"] > snap["step"]]
                    self._log(
                        f"Snapshot covered steps 0-{snap['step']}. "
                        f"Replaying {len(remaining)} events after snapshot."
                    )
                    for event in remaining:
                        if PolicyEngine.is_replayable(event["effect"]):
                            self._log(f"Fast-forward: {event['command'][:60]}")
                            await self._run_bash(event["command"])
                else:
                    self._log("Snapshot file missing — falling back to full replay.")
                    await self._full_replay()
            else:
                await self._full_replay()
        else:
            messages, cursor, _ = load_checkpoint(self._session_id)

        return {
            "session_id": self._session_id,
            "sandbox_id": self._sandbox.id,
            "replay_mode": self._replay_mode,
            "restored_messages": messages,
            "restored_cursor": cursor,
        }

    async def _full_replay(self):
        """Replay all REPLAYABLE* events from step 0 (legacy / no-snapshot path)."""
        for event in self._replay_events:
            if PolicyEngine.is_replayable(event["effect"]):
                self._log(f"Fast-forward REPLAYABLE: {event['command'][:60]}")
                await self._run_bash(event["command"])

    async def end(self) -> dict:
        """Kill the sandbox and close the session."""
        if self._sandbox:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
        return {"ok": True}

    async def revive_sandbox(self) -> dict:
        """
        Sandbox crash recovery: kill the dead sandbox, spin up a fresh one,
        and restore the latest snapshot + replay remaining REPLAYABLE events.
        """
        self._log("[SANDBOX CRASH] Starting sandbox revival...")

        if self._sandbox:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None

        self._sandbox = await self._create_sandbox()
        self._log(f"[SANDBOX CRASH] New sandbox: {self._sandbox.id}")

        all_events = get_effect_log_from(self._session_id, from_step=0)
        replayed = 0

        # Try snapshot-based restore first
        latest_snap = get_latest_snapshot_before(self._session_id, step=99999)
        if latest_snap and Path(latest_snap["storage_path"]).exists():
            self._log(f"[SANDBOX CRASH] Restoring snapshot {latest_snap['snapshot_id']}")
            await self._restore_snapshot(latest_snap["snapshot_id"])
            events_after = [e for e in all_events if e["step"] > latest_snap["step"]]
            for event in events_after:
                if PolicyEngine.is_replayable(event["effect"]):
                    self._log(f"[SANDBOX CRASH] Replaying: {event['command'][:60]}")
                    await self._run_bash(event["command"])
                    replayed += 1
            replayed += 1  # count the snapshot itself as one replay unit
        else:
            # Fallback: replay all REPLAYABLE events
            for event in all_events:
                if PolicyEngine.is_replayable(event["effect"]):
                    self._log(f"[SANDBOX CRASH] Replaying: {event['command'][:60]}")
                    await self._run_bash(event["command"])
                    replayed += 1

        self._log(f"[SANDBOX CRASH] Revival complete. {replayed} events replayed.")
        return {"sandbox_id": self._sandbox.id, "replayed_events": replayed}

    # ── Core SDK Methods ──────────────────────────────────────────────────────

    async def execute_tool(
        self,
        tool_name: str,
        tool_input: dict,
        tool_use_id: Optional[str] = None,
    ) -> dict:
        """
        Execute a tool call through the Capability Gateway.

        Steps:
          1. Policy check (L1 effect lookup + L3 blocklist + L2 quota)
          2. If in replay mode: return cached result
          3. Otherwise: execute + log to effect_log
          4. If effect is REPLAYABLE_EXPENSIVE: take a filesystem snapshot
        """
        command = tool_input.get("command", tool_input.get("url", ""))

        try:
            effect = self._policy.check(tool_name, command)
        except Exception as e:
            return {"error": str(e), "result": f"[PolicyViolation] {e}"}

        # ── Replay Mode ──────────────────────────────────────────────────────
        if self._replay_mode and self._replay_cursor < len(self._replay_events):
            cached = self._replay_events[self._replay_cursor]
            self._replay_cursor += 1
            label = {
                str(Effect.REPLAYABLE_FAST):      "already re-executed during setup",
                str(Effect.REPLAYABLE_EXPENSIVE):  "already restored from snapshot",
                str(Effect.REPLAYABLE):            "already re-executed during setup",
                str(Effect.NO_SIDE_EFFECTS):       "skipped (no side effects)",
                str(Effect.IRREVERSIBLE):          "BLOCKED (irreversible)",
            }.get(str(effect), "cached")
            self._log(f"[REPLAY] '{tool_name}' {label}")
            return {"result": cached["result"], "effect": str(effect), "replayed": True}

        # ── Real Execution ────────────────────────────────────────────────────
        self._log(f"Executing '{tool_name}' ({effect}): {command[:80]}")

        if tool_name in ("bash_read", "bash_write", "bash_build", "bash_run"):
            result = await self._run_bash(command)
        elif tool_name == "fetch_url":
            resp = fetch_url(
                tool_input["url"],
                tool_input.get("method", "GET"),
                tool_input.get("data"),
            )
            result = (
                f"[Gateway Error] {resp['error']}"
                if resp["error"]
                else f"Status: {resp['status']}\n\n{resp['body']}"
            )
        else:
            result = f"[SidecarSession] Unknown tool: '{tool_name}'"

        step = log_tool_event(self._session_id, tool_name, str(effect), command, result)

        # ── Post-execution snapshot for expensive operations ──────────────────
        if effect == Effect.REPLAYABLE_EXPENSIVE:
            self._log(f"[SNAPSHOT] Triggered by {tool_name} at step {step}")
            try:
                snap_id = await self._take_snapshot(step)
                self._current_snapshot_id = snap_id
                self._log(f"[SNAPSHOT] Saved: {snap_id}")
            except Exception as e:
                self._log(f"[SNAPSHOT] Warning: snapshot failed — {e}")

        return {"result": result, "effect": str(effect), "replayed": False}

    async def llm_generate(self, messages: list, system: str = "") -> dict:
        """
        Call the LLM, record the generation, atomically save checkpoint.
        Returns serialised content blocks + stop_reason.
        """
        tools = build_llm_tool_schema()

        response = await self._llm.messages.create(
            model=self._model,
            system=system or "You are a helpful AI assistant.",
            messages=messages,
            tools=tools,
            max_tokens=4096,
        )

        content_blocks = []
        for block in response.content:
            if block.type == "text":
                content_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        step = log_llm_event(self._session_id, content_blocks)

        new_messages = messages + [{"role": "assistant", "content": content_blocks}]
        save_checkpoint(self._session_id, new_messages, step,
                        snapshot_id=self._current_snapshot_id)

        return {
            "stop_reason": response.stop_reason,
            "content": content_blocks,
            "usage": response.usage.model_dump() if hasattr(response, "usage") else None,
        }

    # ── Trunk / Fork API ─────────────────────────────────────────────────────

    async def init_trunk(self) -> dict:
        """
        Create the initial trunk by snapshotting the current sandbox state.
        Should be called once after setting up the shared base environment.
        Returns {trunk_id, snapshot_path}.
        """
        self._log("[TRUNK] Initialising trunk from current sandbox...")
        snap_path = await self._take_raw_snapshot(self._session_id, step=0, label="trunk_init")
        trunk_id = create_trunk(snapshot_path=snap_path, effect_cursor=0)
        self._log(f"[TRUNK] Created trunk {trunk_id}")
        return {"trunk_id": trunk_id, "snapshot_path": snap_path}

    async def fork_from_trunk(self, trunk_id: Optional[str] = None) -> dict:
        """
        Start a new session forked from a trunk version.
        Creates a fresh sandbox and populates it from the trunk snapshot.
        Returns {session_id, fork_id, trunk_id, sandbox_id}.
        """
        trunk = get_trunk(trunk_id) if trunk_id else get_latest_trunk()
        if not trunk:
            raise RuntimeError("No trunk found. Call /trunk/init first.")

        # Create a new session for this fork
        self._session_id = create_session()
        self._replay_mode = False
        self._replay_events = []
        self._current_snapshot_id = None

        # Spin up sandbox and restore trunk state
        self._sandbox = await self._create_sandbox()
        snap_path = trunk["snapshot_path"]
        if Path(snap_path).exists():
            self._log(f"[FORK] Restoring trunk {trunk['trunk_id']} into {self._sandbox.id}")
            await self._restore_raw_snapshot(snap_path)
        else:
            self._log("[FORK] Warning: trunk snapshot missing — starting with empty sandbox")

        fork_id = create_fork(self._session_id, trunk["trunk_id"])
        self._log(f"[FORK] Fork {fork_id} from trunk {trunk['trunk_id']}")

        return {
            "session_id": self._session_id,
            "fork_id": fork_id,
            "trunk_id": trunk["trunk_id"],
            "sandbox_id": self._sandbox.id,
        }

    async def commit_to_trunk(self) -> dict:
        """
        Commit this fork's changes to the trunk.

        1. Check that fork's trunk_id == latest trunk_id (no conflict).
        2. Snapshot THIS fork's live sandbox — it already has all the work.
        3. Create a new trunk version pointing at that snapshot.
        4. Mark fork as committed.

        Returns {ok, new_trunk_id} or {conflict: True, current_trunk_id}.
        """
        fork = get_fork(self._session_id)
        if not fork:
            raise RuntimeError(f"Session {self._session_id} is not a registered fork.")

        current_trunk = get_latest_trunk()
        if not current_trunk:
            raise RuntimeError("No trunk found.")

        # Conflict check
        if fork["trunk_id"] != current_trunk["trunk_id"]:
            self._log(f"[COMMIT] CONFLICT: fork based on {fork['trunk_id']}, "
                      f"current trunk is {current_trunk['trunk_id']}")
            update_fork_status(fork["fork_id"], "conflicted")
            return {"conflict": True, "current_trunk_id": current_trunk["trunk_id"]}

        # Build changeset for audit record
        changeset = self._build_changeset(trunk_effect_cursor=current_trunk["effect_cursor"])
        self._log(f"[COMMIT] Changeset: {len(changeset)} commands")

        # Snapshot THIS fork's sandbox — it's already in the committed state.
        all_events = get_effect_log_from(self._session_id, from_step=0)
        new_cursor = len(all_events)
        new_snap_path = await self._take_raw_snapshot(
            self._session_id, step=new_cursor, label="trunk_commit"
        )

        new_trunk_id = create_trunk(
            snapshot_path=new_snap_path,
            effect_cursor=new_cursor,
            parent_id=current_trunk["trunk_id"],
        )

        update_fork_status(fork["fork_id"], "committed", changeset=changeset)
        self._log(f"[COMMIT] New trunk: {new_trunk_id}")
        return {"ok": True, "new_trunk_id": new_trunk_id}


    async def abort_fork(self) -> dict:
        """Mark this fork as aborted and kill its sandbox."""
        fork = get_fork(self._session_id)
        if fork:
            update_fork_status(fork["fork_id"], "aborted")
        await self.end()
        return {"ok": True}

    def _build_changeset(self, trunk_effect_cursor: int) -> list[dict]:
        """Return REPLAYABLE* commands added to this fork after the trunk's cursor."""
        events = get_effect_log_from(self._session_id, from_step=trunk_effect_cursor)
        return [
            {"command": e["command"]}
            for e in events
            if PolicyEngine.is_replayable(e["effect"])
        ]

    def trunk_status(self) -> dict:
        from sidecar.effect_log import list_active_forks
        trunk = get_latest_trunk()
        if not trunk:
            return {"trunk": None, "active_forks": []}
        active = list_active_forks(trunk["trunk_id"])
        return {
            "trunk_id": trunk["trunk_id"],
            "effect_cursor": trunk["effect_cursor"],
            "created_at": trunk["created_at"],
            "active_forks": [f["fork_id"] for f in active],
        }

    # ── Snapshot Helpers ──────────────────────────────────────────────────────

    async def _take_snapshot(self, step: int) -> str:
        """
        Tar the sandbox filesystem, base64-encode it, save to Sidecar host.
        Returns snapshot_id.
        """
        snapshot_id = str(uuid.uuid4())
        path = await self._take_raw_snapshot(self._session_id, step, label=snapshot_id[:8])
        raw_size = Path(path).stat().st_size if Path(path).exists() else 0
        save_snapshot(self._session_id, snapshot_id, step, path, raw_size)
        return snapshot_id

    async def _take_raw_snapshot(self, session_id: str, step: int, label: str = "") -> str:
        """
        Tar selected sandbox directories, base64-decode, save as .tar.gz.
        Returns the absolute path to the saved file.
        """
        dirs = " ".join(
            d for d in _SNAPSHOT_DIRS
            if True  # The sandbox will skip non-existent dirs if || true
        )
        # Run tar inside the sandbox; base64-encode stdout for safe transport
        b64 = await self._run_bash(
            f"tar -czf - {dirs} 2>/dev/null | base64 -w 0; echo ''"
        )
        b64 = b64.strip()

        snap_dir = Path(f"sidecar_data/snapshots/{session_id}")
        snap_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(snap_dir / f"{label}_{step}.tar.gz")

        try:
            raw = base64.b64decode(b64)
            with open(out_path, "wb") as f:
                f.write(raw)
            self._log(f"[SNAPSHOT] Saved {len(raw)} bytes → {out_path}")
        except Exception as e:
            self._log(f"[SNAPSHOT] base64 decode failed: {e}. Saving empty stub.")
            with open(out_path, "wb") as f:
                pass

        return out_path

    async def _restore_snapshot(self, snapshot_id: str):
        """Restore a sandbox filesystem from a stored snapshot record."""
        snap = get_snapshot(snapshot_id)
        if not snap:
            raise RuntimeError(f"Snapshot {snapshot_id} not found in DB.")
        await self._restore_raw_snapshot(snap["storage_path"])

    async def _restore_raw_snapshot(self, storage_path: str):
        """Read a .tar.gz from host, base64-encode, pipe into sandbox to extract."""
        if not Path(storage_path).exists():
            self._log(f"[SNAPSHOT] File missing: {storage_path} — skipping restore")
            return

        with open(storage_path, "rb") as f:
            raw = f.read()

        b64 = base64.b64encode(raw).decode()
        self._log(f"[SNAPSHOT] Restoring {len(raw)} bytes from {storage_path}")

        # Write b64 in chunks to avoid ARG_MAX limits, then decode + extract
        chunk_size = 8000
        chunks = [b64[i:i + chunk_size] for i in range(0, len(b64), chunk_size)]
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            # Use printf to avoid interpretation of special chars
            await self._run_bash(
                f"printf '%s' {chunk!r} {op} /tmp/_snap_restore.b64"
            )

        await self._run_bash(
            "base64 -d /tmp/_snap_restore.b64 | tar -xzf - -C / 2>/dev/null || true; "
            "rm -f /tmp/_snap_restore.b64"
        )
        self._log("[SNAPSHOT] Restore complete.")

    # ── Internal Helpers ──────────────────────────────────────────────────────

    async def _create_sandbox(self) -> Sandbox:
        config = ConnectionConfig(
            domain=os.getenv("SANDBOX_DOMAIN", "localhost:8080"),
            api_key=os.getenv("SANDBOX_API_KEY"),
            request_timeout=timedelta(seconds=60),
        )
        return await Sandbox.create(
            os.getenv("SANDBOX_IMAGE", "ubuntu:22.04"),
            connection_config=config,
            timeout=timedelta(minutes=30),
        )

    async def _run_bash(self, command: str) -> str:
        execution = await self._sandbox.commands.run(command)
        lines = []
        for msg in execution.logs.stdout:
            lines.append(msg.text)
        for msg in execution.logs.stderr:
            lines.append(f"[stderr] {msg.text}")
        if execution.error:
            lines.append(f"[error] {execution.error.name}: {execution.error.value}")
        result = "\n".join(lines).strip()
        return result or "(executed with no output)"

    def _log(self, msg: str):
        if self.debug:
            print(f"[SidecarSession] {msg}", flush=True)
