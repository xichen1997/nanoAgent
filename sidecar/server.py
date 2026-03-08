"""
sidecar/server.py — Thin HTTP wrapper over SidecarSession SDK.

Exposes the SidecarSession API over HTTP so that the Agent subprocess
(agent/loop.py) can communicate with it via HTTP JSON.

The HTTP layer is intentionally minimal: parse JSON, call session method,
return JSON. All logic lives in sidecar/session.py.
"""
import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sidecar.session import SidecarSession

PORT = int(os.environ.get("SIDECAR_PORT", "7878"))

# ── Single shared session (one sidecar = one active session) ──────────────────
_session: SidecarSession | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _run_async(coro, timeout=180):
    """Bridge: run an async coroutine from a synchronous HTTP handler thread."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


# ── Route Handlers ─────────────────────────────────────────────────────────────

def route_session_start(body: dict) -> dict:
    global _session
    _session = SidecarSession(debug=True)
    return _run_async(
        _session.start(
            resume_session_id=body.get("resume_session_id"),
            fork_at=body.get("fork_at"),
        ),
        timeout=120,
    )


def route_session_end(body: dict) -> dict:
    if _session:
        return _run_async(_session.end())
    return {"ok": True}


def route_tool_execute(body: dict) -> dict:
    if not _session:
        return {"error": "No active session. Call /session/start first.", "result": ""}
    return _run_async(
        _session.execute_tool(
            tool_name=body["tool_name"],
            tool_input=body.get("tool_input", {}),
            tool_use_id=body.get("tool_use_id"),
        )
    )


def route_llm_generate(body: dict) -> dict:
    if not _session:
        return {"error": "No active session. Call /session/start first."}
    return _run_async(
        _session.llm_generate(
            messages=body["messages"],
            system=body.get("system", ""),
        ),
        timeout=180,
    )


def route_session_revive(body: dict) -> dict:
    """Kill the current sandbox and revive it, replaying REPLAYABLE events."""
    if not _session:
        return {"error": "No active session."}
    return _run_async(_session.revive_sandbox(), timeout=120)


def route_sandbox_kill(body: dict) -> dict:
    """
    Forcibly kill the active sandbox without ending the session.
    Used by integration tests to simulate a container crash.
    The session_id and effect_log remain intact.
    """
    if not _session or not _session._sandbox:
        return {"error": "No active sandbox."}
    try:
        _run_async(_session._sandbox.kill())
    except Exception:
        pass
    _session._sandbox = None
    return {"killed": True}


ROUTES = {
    "/session/start":   route_session_start,
    "/session/end":     route_session_end,
    "/session/revive":  route_session_revive,
    "/sandbox/kill":    route_sandbox_kill,
    "/tool/execute":    route_tool_execute,
    "/llm/generate":    route_llm_generate,
}


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class SidecarHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default Apache-style access log

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({
                "status": "ok",
                "session": _session.session_id if _session else None,
                "replay": _session.is_replay if _session else False,
            })
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
            self._send_json({"error": f"Bad JSON: {e}"}, 400)
            return

        try:
            self._send_json(handler(body))
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
    import time

    t = Thread(target=_start_event_loop, daemon=True)
    t.start()
    while _loop is None:
        time.sleep(0.05)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), SidecarHandler)
    print(f"[Sidecar] Listening on http://127.0.0.1:{PORT}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[Sidecar] Shutting down.")
        if _session:
            asyncio.run_coroutine_threadsafe(_session.end(), _loop)


if __name__ == "__main__":
    main()
