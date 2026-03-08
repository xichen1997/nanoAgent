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
import json
import os
import uuid
from datetime import timedelta
from typing import Optional

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

from sidecar.effect_log import (
    create_session,
    get_effect_log_from,
    init_db,
    load_checkpoint,
    log_llm_event,
    log_tool_event,
    save_checkpoint,
)
from sidecar.gateway import fetch_url
from sidecar.policy import Effect, PolicyEngine, build_llm_tool_schema

load_dotenv()


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

        # Fast-forward replayable events to restore sandbox state
        if self._replay_mode:
            for event in self._replay_events:
                if event["effect"] == str(Effect.REPLAYABLE):
                    self._log(f"Fast-forward REPLAYABLE: {event['command'][:60]}")
                    await self._run_bash(event["command"])

        messages, cursor = load_checkpoint(self._session_id)

        return {
            "session_id": self._session_id,
            "sandbox_id": self._sandbox.id,
            "replay_mode": self._replay_mode,
            "restored_messages": messages,
            "restored_cursor": cursor,
        }

    async def end(self) -> dict:
        """Kill the sandbox and close the session."""
        if self._sandbox:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
        return {"ok": True}

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
                str(Effect.REPLAYABLE): "already re-executed during setup",
                str(Effect.NO_SIDE_EFFECTS): "skipped (no side effects)",
                str(Effect.IRREVERSIBLE): "BLOCKED (irreversible)",
            }.get(str(effect), "cached")
            self._log(f"[REPLAY] '{tool_name}' {label}")
            return {"result": cached["result"], "effect": str(effect), "replayed": True}

        # ── Real Execution ────────────────────────────────────────────────────
        self._log(f"Executing '{tool_name}' ({effect}): {command[:80]}")

        if tool_name in ("bash_read", "bash_write", "bash_run"):
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

        log_tool_event(self._session_id, tool_name, str(effect), command, result)
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
        save_checkpoint(self._session_id, new_messages, step)

        return {
            "stop_reason": response.stop_reason,
            "content": content_blocks,
            "usage": response.usage.model_dump() if hasattr(response, "usage") else None,
        }

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
