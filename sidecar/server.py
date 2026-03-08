"""
sidecar/server.py — The Capability Gateway HTTP Server.

This is a standalone process that:
  1. Starts/owns the OpenSandbox container
  2. Exposes a JSON API on localhost:7878
  3. Intercepts ALL tool calls from the Agent:
     - Runs policy checks
     - Executes in Sandbox or via Gateway
     - Writes Effect Log to SQLite
     - Handles replay transparently — Agent doesn't know
  4. Intercepts LLM calls — calls Claude and records the generation

The Agent process sends HTTP POST requests here. It has zero knowledge
of replay, logging, policy, or credentials.
"""
import asyncio
import json
import os
import sys
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from dotenv import load_dotenv

# Add parent to path so imports work when run as subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from anthropic import AsyncAnthropic
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

from sidecar.effect_log import (
    init_db, create_session, log_tool_event, log_llm_event,
    save_checkpoint, load_checkpoint, get_effect_log_from, get_last_llm_step,
)
from sidecar.policy import PolicyEngine, Effect, build_llm_tool_schema, TOOL_REGISTRY
from sidecar.gateway import fetch_url

PORT = int(os.environ.get("SIDECAR_PORT", "7878"))

# ── Global state (single-session sidecar for now) ─────────────────────────────
_sandbox: Sandbox | None = None
_session_id: str | None = None
_policy = PolicyEngine()
_replay_mode = False          # True → use cached results
_replay_cursor = 0            # current step pointer during replay
_replay_events: list = []     # pre-loaded effect log rows for replay

_anthropic_client = AsyncAnthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", "dummy"),
    base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic"),
    default_headers={"Authorization": f"Bearer {os.environ.get('ANTHROPIC_API_KEY', 'dummy')}"},
)
MODEL = os.environ.get("LLM_MODEL", "MiniMax-M2.5")

# asyncio event loop (runs in background thread)
_loop: asyncio.AbstractEventLoop | None = None


# ── Async helpers ──────────────────────────────────────────────────────────────

async def _create_sandbox() -> Sandbox:
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


async def _run_sandbox_command(command: str) -> str:
    execution = await _sandbox.commands.run(command)
    outputs = []
    for msg in execution.logs.stdout:
        outputs.append(msg.text)
    for msg in execution.logs.stderr:
        outputs.append(f"[stderr] {msg.text}")
    if execution.error:
        outputs.append(f"[error] {execution.error.name}: {execution.error.value}")
    result = "\n".join(outputs).strip()
    return result if result else "(executed with no output)"


# ── Request Handlers ───────────────────────────────────────────────────────────

def handle_session_start(body: dict) -> dict:
    global _sandbox, _session_id, _policy, _replay_mode, _replay_cursor, _replay_events

    resume_session_id = body.get("resume_session_id")
    fork_at = body.get("fork_at")  # step number to fork from

    _policy = PolicyEngine()

    if resume_session_id:
        _session_id = resume_session_id
        _replay_mode = True

        # Load cached events for replay
        _replay_events = get_effect_log_from(_session_id, from_step=fork_at or 0)
        _replay_cursor = 0
        print(f"[Sidecar] REPLAY mode. Session: {_session_id}, {len(_replay_events)} events to replay.")
    else:
        _session_id = create_session()
        _replay_mode = False
        _replay_events = []
        print(f"[Sidecar] NEW session: {_session_id}")

    # Always create a fresh sandbox
    future = asyncio.run_coroutine_threadsafe(_create_sandbox(), _loop)
    _sandbox = future.result(timeout=60)
    print(f"[Sidecar] Sandbox created: {_sandbox.id}")

    # If resuming, replay replayable actions to restore sandbox state
    if _replay_mode:
        for event in _replay_events:
            if event["effect"] == Effect.REPLAYABLE:
                print(f"[Sidecar] Fast-forward REPLAYABLE: {event['command'][:60]}")
                future = asyncio.run_coroutine_threadsafe(
                    _run_sandbox_command(event["command"]), _loop
                )
                future.result(timeout=30)

    # Restore LLM messages from checkpoint
    messages, cursor = load_checkpoint(_session_id)

    return {
        "session_id": _session_id,
        "sandbox_id": _sandbox.id,
        "replay_mode": _replay_mode,
        "restored_messages": messages,
        "restored_cursor": cursor,
    }


def handle_tool_execute(body: dict) -> dict:
    global _replay_cursor

    tool_name = body["tool_name"]
    tool_input = body.get("tool_input", {})
    tool_use_id = body.get("tool_use_id", "unknown")

    # ── Policy Check ─────────────────────────────────────────────────────────
    command = tool_input.get("command", tool_input.get("url", ""))
    try:
        effect = _policy.check(tool_name, command)
    except Exception as e:
        return {"error": str(e), "result": f"[PolicyViolation] {e}"}

    # ── Replay Mode ──────────────────────────────────────────────────────────
    if _replay_mode and _replay_cursor < len(_replay_events):
        cached = _replay_events[_replay_cursor]
        _replay_cursor += 1
        if effect == Effect.REPLAYABLE:
            print(f"[Sidecar] [REPLAY] REPLAYABLE '{tool_name}' already replayed during setup. Returning cached.")
        elif effect == Effect.NO_SIDE_EFFECTS:
            print(f"[Sidecar] [REPLAY] Skipping NO_SIDE_EFFECTS '{tool_name}' — returning cached result.")
        else:
            print(f"[Sidecar] [REPLAY] Blocking IRREVERSIBLE '{tool_name}' — returning cached fake result.")
        return {"result": cached["result"], "effect": str(effect), "replayed": True}

    # ── Real Execution ────────────────────────────────────────────────────────
    print(f"[Sidecar] Executing '{tool_name}' ({effect}): {command[:80]}")

    if tool_name in ("bash_read", "bash_write", "bash_run"):
        future = asyncio.run_coroutine_threadsafe(
            _run_sandbox_command(command), _loop
        )
        result = future.result(timeout=30)

    elif tool_name == "fetch_url":
        url = tool_input["url"]
        method = tool_input.get("method", "GET")
        data = tool_input.get("data")
        resp = fetch_url(url, method, data)
        if resp["error"]:
            result = f"[Gateway Error] {resp['error']}"
        else:
            result = f"Status: {resp['status']}\n\n{resp['body']}"
    else:
        result = f"[Sidecar Error] Unknown tool: {tool_name}"

    # ── Log to Effect DB ──────────────────────────────────────────────────────
    log_tool_event(_session_id, tool_name, str(effect), command, result)

    return {"result": result, "effect": str(effect), "replayed": False}


def handle_llm_generate(body: dict) -> dict:
    """
    The agent sends its messages list. The Sidecar calls the LLM,
    records the generation, saves a checkpoint, and returns the response.
    """
    messages = body["messages"]
    tools = build_llm_tool_schema()

    from anthropic.types import TextBlock, ToolUseBlock  # noqa

    future = asyncio.run_coroutine_threadsafe(
        _anthropic_client.messages.create(
            model=MODEL,
            system=body.get("system", "You are a helpful AI assistant with access to a sandbox."),
            messages=messages,
            tools=tools,
            max_tokens=4096,
        ),
        _loop,
    )
    response = future.result(timeout=120)

    # Serialize content blocks for transport
    content_blocks = []
    for block in response.content:
        if block.type == "text":
            content_blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content_blocks.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

    # Log LLM generation
    step = log_llm_event(_session_id, content_blocks)

    # Atomic checkpoint: messages + new assistant turn + cursor
    new_messages = messages + [{"role": "assistant", "content": content_blocks}]
    save_checkpoint(_session_id, new_messages, step)

    return {
        "stop_reason": response.stop_reason,
        "content": content_blocks,
        "usage": response.usage.model_dump() if hasattr(response, "usage") else None,
    }


def handle_session_end(body: dict) -> dict:
    global _sandbox
    if _sandbox:
        future = asyncio.run_coroutine_threadsafe(_sandbox.kill(), _loop)
        try:
            future.result(timeout=15)
        except Exception:
            pass
        _sandbox = None
    return {"ok": True}


# ── HTTP Handler ───────────────────────────────────────────────────────────────

ROUTES = {
    "/session/start": handle_session_start,
    "/session/end": handle_session_end,
    "/tool/execute": handle_tool_execute,
    "/llm/generate": handle_llm_generate,
}


class SidecarHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default access log

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok", "session": _session_id})
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        handler = ROUTES.get(self.path)
        if not handler:
            self._send_json({"error": f"Unknown route: {self.path}"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except Exception as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return

        try:
            result = handler(body)
            self._send_json(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)


# ── Entry Point ────────────────────────────────────────────────────────────────

def _start_event_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def main():
    init_db()

    # Start asyncio loop in background thread (for Sandbox + Anthropic async calls)
    t = Thread(target=_start_event_loop, daemon=True)
    t.start()

    # Wait for loop to be ready
    import time
    while _loop is None:
        time.sleep(0.05)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), SidecarHandler)
    print(f"[Sidecar] Listening on http://127.0.0.1:{PORT}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[Sidecar] Shutting down.")
        if _sandbox:
            future = asyncio.run_coroutine_threadsafe(_sandbox.kill(), _loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass


if __name__ == "__main__":
    main()
