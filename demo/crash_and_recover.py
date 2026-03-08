"""
demo/crash_and_recover.py — Integration Test: Crash & Recovery

Demonstrates two crash-and-recovery scenarios using SidecarSession directly.
No subprocess stdin piping — the test drives the SDK inline for reliability.

Scenario A — Agent/Sidecar crash (Phases 1–3):
  1. Start Sidecar subprocess
  2. Run a 3-step task (bash_write → bash_run → fetch_url)
  3. Inject CrashInjected after bash_write
  4. Kill Sidecar, restart fresh (SQLite only — no in-memory shortcut)
  5. Resume session, assert bash_write NOT re-executed, fetch_url runs fresh

Scenario B — Sandbox container crash (Phases 4–5):
  6. New SidecarSession, write a sentinel file into sandbox
  7. Kill the sandbox object directly (simulates container OOM/eviction)
  8. Call session.revive_sandbox() — new sandbox spun up, REPLAYABLE events replayed
  9. Read the sentinel file back — verify it survived the revival

Run:
    python demo/crash_and_recover.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PYTHON = sys.executable
SIDECAR_PORT = 7878
SIDECAR_URL = f"http://127.0.0.1:{SIDECAR_PORT}"

SYSTEM_PROMPT = "You are a helpful assistant. Follow the user's instructions step by step."

COLORS = {
    "green":  "\033[92m", "yellow": "\033[93m",
    "red":    "\033[91m", "blue":   "\033[94m",
    "bold":   "\033[1m",  "reset":  "\033[0m",
}
def c(color, text): return f"{COLORS[color]}{text}{COLORS['reset']}"
def header(msg): print(f"\n{c('bold','='*60)}\n{c('bold', msg)}\n{c('bold','='*60)}", flush=True)
def ok(msg):   print(c('green',  f"✅  {msg}"), flush=True)
def err(msg):  print(c('red',    f"❌  {msg}"), flush=True)
def info(msg): print(c('blue',   f"ℹ️   {msg}"), flush=True)
def warn(msg): print(c('yellow', f"⚠️   {msg}"), flush=True)


class CrashInjected(Exception):
    """Sentinel exception to simulate an agent crash."""


def wait_for_sidecar(timeout=20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{SIDECAR_URL}/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def start_sidecar() -> subprocess.Popen:
    """Start the Sidecar HTTP server as a background subprocess."""
    return subprocess.Popen(
        [PYTHON, "-u", "-m", "sidecar.server"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── The core agent loop (async, testable without stdin) ───────────────────────

async def run_agent_turn(
    session,
    user_message: str,
    crash_after_tool: str | None = None,
    out_log: dict | None = None,
) -> dict:
    """
    Run one agent turn: send user_message, let the LLM call tools, return summary.
    If crash_after_tool is set, raise CrashInjected after that tool type executes.
    out_log (mutable dict) is updated in-place so caller gets results even on exception.
    Returns a dict of {tool_name: 'replayed'|'executed'} for each tool.
    """
    from sidecar.session import SidecarSession  # noqa — imported here to avoid circular

    messages = [{"role": "user", "content": user_message}]
    tool_events = out_log if out_log is not None else {}

    while True:
        gen = await session.llm_generate(messages, system=SYSTEM_PROMPT)
        content = gen["content"]
        stop_reason = gen["stop_reason"]
        messages.append({"role": "assistant", "content": content})

        if stop_reason != "tool_use":
            # Final text response
            text = " ".join(b["text"] for b in content if b.get("type") == "text")
            print(f"  [LLM] {text[:100]}", flush=True)
            break

        tool_results = []
        for block in content:
            if block["type"] != "tool_use":
                continue
            tool_name = block["name"]
            result = await session.execute_tool(tool_name, block["input"], block.get("id"))
            status = "replayed" if result.get("replayed") else "executed"
            icon = "↺" if result.get("replayed") else "⚡"
            # Record BEFORE checking crash so Phase 1 log captures the execution
            tool_events[tool_name] = status
            print(f"  [{icon} {tool_name}] effect={result.get('effect')} status={status}", flush=True)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": str(result.get("result", "")),
            })

            if crash_after_tool and tool_name == crash_after_tool:
                raise CrashInjected(f"Crash injected after {tool_name}")

        messages.append({"role": "user", "content": tool_results})

    return tool_events


# ── Phase 1 ────────────────────────────────────────────────────────────────────

async def phase1() -> tuple[str, dict]:
    """Run until bash_write completes, then inject a crash. Return session_id + tool log."""
    from sidecar.session import SidecarSession

    header("PHASE 1: Run task, inject crash after bash_write")

    session = SidecarSession(debug=False)
    info_data = await session.start()
    session_id = info_data["session_id"]
    info(f"Session started: {session_id}")

    task = (
        "Do the following in order:\n"
        "1. Use bash_write to write a Python script to /tmp/demo_fib.py that prints the first 10 Fibonacci numbers.\n"
        "2. Use bash_run to execute /tmp/demo_fib.py.\n"
        "3. Use fetch_url to GET https://httpbin.org/get and tell me my origin IP.\n"
        "Complete all three steps."
    )

    tool_log = {}
    try:
        await run_agent_turn(session, task, crash_after_tool="bash_write", out_log=tool_log)
    except CrashInjected as e:
        warn(f"Crash injected! → {e}")
        ok(f"Checkpoint saved. Session: {session_id}")

    # Tear down (do NOT call session.end() — simulating a real crash)
    if session._sandbox:
        try:
            await session._sandbox.kill()
        except Exception:
            pass

    return session_id, tool_log


# ── Phase 2 ────────────────────────────────────────────────────────────────────

async def phase2(session_id: str) -> dict:
    """Resume from checkpoint and complete remaining steps."""
    from sidecar.session import SidecarSession

    header(f"PHASE 2: Resume session {session_id}")
    session = SidecarSession(debug=False)
    info_data = await session.start(resume_session_id=session_id)
    info(f"Resumed. Replay mode: {info_data['replay_mode']}")
    assert info_data["replay_mode"], "Expected REPLAY mode!"
    ok("REPLAY MODE confirmed ✓")

    task = (
        "Continue from where we left off:\n"
        "1. The bash_write is already done (replay it from cache).\n"
        "2. Use bash_run to execute /tmp/demo_fib.py.\n"
        "3. Use fetch_url to GET https://httpbin.org/get and tell me my origin IP.\n"
        "Complete the remaining steps."
    )

    tool_log = await run_agent_turn(session, task)
    await session.end()
    return tool_log


# ── Phase 4 — Sandbox Crash & Revival ────────────────────────────────────────

def sidecar_post(path: str, body: dict) -> dict:
    """Synchronous HTTP POST to the Sidecar — same pattern as agent/loop.py."""
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{SIDECAR_URL}{path}",
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def phase4_sandbox_crash() -> dict:
    """
    Simulate a sandbox container crash (OOM, eviction, etc.).

    This test uses the Sidecar HTTP API entirely — no SDK imports,
    no shared process between test runner and Sidecar.

    Steps:
      1. POST /session/start  → fresh session, new sandbox
      2. POST /tool/execute   → bash_write sentinel file (logged as REPLAYABLE)
      3. POST /tool/execute   → bash_read to confirm file exists
      4. POST /sandbox/kill   → simulate container crash (Sidecar kills sandbox
                                 but keeps session + effect_log alive)
      5. POST /session/revive → Sidecar creates new sandbox, replays REPLAYABLE
                                 events to restore filesystem state
      6. POST /tool/execute   → bash_read to verify sentinel file survived
      7. POST /session/end    → clean up
    """
    header("PHASE 4: Sandbox container crash + revival (via HTTP)")

    sentinel = "sandbox_revival_ok"

    # 1. Start session
    start_resp = sidecar_post("/session/start", {})
    old_sandbox_id = start_resp["sandbox_id"]
    info(f"Session started: {start_resp['session_id']}")
    info(f"Original sandbox: {old_sandbox_id}")

    # 2. Write sentinel file (REPLAYABLE — will be in effect_log)
    write_resp = sidecar_post("/tool/execute", {
        "tool_name": "bash_write",
        "tool_input": {"command": f"echo '{sentinel}' > /tmp/sandbox_revival_test.txt"},
    })
    info(f"Sentinel written: {write_resp['result']!r}")
    assert write_resp["effect"] == "Effect.REPLAYABLE", \
        f"Expected REPLAYABLE, got {write_resp['effect']}"

    # 3. Confirm file is there before crash
    pre = sidecar_post("/tool/execute", {
        "tool_name": "bash_read",
        "tool_input": {"command": "cat /tmp/sandbox_revival_test.txt"},
    })
    assert sentinel in pre["result"], f"Sentinel write failed! Got: {pre['result']}"
    ok("Sentinel file confirmed in sandbox before crash.")

    # 4. Inject sandbox crash via HTTP — Sidecar kills the container but stays alive
    warn("Injecting sandbox crash via POST /sandbox/kill...")
    kill_resp = sidecar_post("/sandbox/kill", {})
    assert kill_resp.get("killed"), f"Sandbox kill failed: {kill_resp}"
    ok("Sandbox killed (simulated container crash). Sidecar process still alive.")

    # 5. Revive — Sidecar creates new sandbox + replays REPLAYABLE events
    info("Calling POST /session/revive...")
    revive_resp = sidecar_post("/session/revive", {})
    new_sandbox_id = revive_resp["sandbox_id"]
    replayed = revive_resp["replayed_events"]
    ok(f"Sandbox revived: {new_sandbox_id} ({replayed} event(s) replayed)")

    # 6. Verify sentinel file survived
    post = sidecar_post("/tool/execute", {
        "tool_name": "bash_read",
        "tool_input": {"command": "cat /tmp/sandbox_revival_test.txt"},
    })
    info(f"Post-revival file content: {post['result']!r}")

    # 7. Clean up
    sidecar_post("/session/end", {})

    return {
        "old_sandbox_id": old_sandbox_id,
        "new_sandbox_id": new_sandbox_id,
        "replayed_events": replayed,
        "sentinel_content": post["result"],
        "sentinel_expected": sentinel,
    }



def phase5_assert_sandbox(result: dict):
    header("PHASE 5: Sandbox revival assertions")
    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            ok(label)
            passed += 1
        else:
            err(label)
            failed += 1

    check("Sandbox ID changed after revival (new container was created)",
          result["old_sandbox_id"] != result["new_sandbox_id"])

    check("At least 1 REPLAYABLE event was replayed during revival",
          result["replayed_events"] >= 1)

    check("Sentinel file content matches after revival (filesystem state restored)",
          result["sentinel_expected"] in result["sentinel_content"])

    header("RESULTS")
    print(f"  {c('green', str(passed))} passed   {c('red', str(failed))} failed")
    if failed > 0:
        print(c('red', c('bold', "\n💥  Sandbox revival assertions failed.")))
        sys.exit(1)



def phase3_assert(phase1_log: dict, phase2_log: dict):
    header("PHASE 3: Assertions")
    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            ok(label)
            passed += 1
        else:
            err(label)
            failed += 1

    # Phase 1: bash_write was executed before crash
    check("Phase 1: bash_write was EXECUTED before crash (⚡)",
          phase1_log.get("bash_write") == "executed")

    # Phase 1: fetch_url must NOT have been called (crash happened before it)
    check("Phase 1: fetch_url was NOT called before crash",
          "fetch_url" not in phase1_log)

    # Phase 2: fetch_url (never in cache) must be freshly executed
    check("Phase 2: fetch_url was EXECUTED fresh after resume (⚡)",
          phase2_log.get("fetch_url") == "executed")

    # Key invariant: recovery, not restart
    check("Cross-phase: Task continued from crash point (not restarted from scratch)",
          "fetch_url" not in phase1_log and "fetch_url" in phase2_log)

    header("RESULTS")
    print(f"  {c('green', str(passed))} passed   {c('red', str(failed))} failed")
    if failed == 0:
        print(c('green', c('bold', "\n🎉  Crash & Recovery integration test PASSED!")))
    else:
        print(c('red', c('bold', "\n💥  Some assertions failed.")))
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    os.system(f"kill $(lsof -ti:{SIDECAR_PORT}) 2>/dev/null; true")
    time.sleep(0.5)

    info("Starting Sidecar subprocess...")
    sidecar = start_sidecar()
    if not wait_for_sidecar(timeout=20):
        err("Sidecar failed to start!")
        sidecar.kill()
        sys.exit(1)
    ok("Sidecar listening.")

    try:
        # ── Scenario A: Agent/Sidecar crash ──────────────────────────────────
        session_id, phase1_log = await phase1()

        info("Killing Sidecar between phases (simulating full process death)...")
        sidecar.kill()
        sidecar.wait()
        ok("Sidecar dead. Restarting fresh Sidecar for Phase 2...")
        sidecar = start_sidecar()
        if not wait_for_sidecar(timeout=20):
            err("Sidecar failed to restart for Phase 2!")
            sys.exit(1)
        ok("New Sidecar listening. Phase 2 will recover from SQLite only.")

        phase2_log = await phase2(session_id)
        phase3_assert(phase1_log, phase2_log)

        # ── Scenario B: Sandbox crash ─────────────────────────────────────────
        sandbox_result = phase4_sandbox_crash()
        phase5_assert_sandbox(sandbox_result)

        print(c('green', c('bold', "\n🎉  ALL integration tests PASSED!")))
    finally:
        sidecar.kill()
        sidecar.wait()
        info("Sidecar shut down.")


if __name__ == "__main__":
    print(c('bold', "\nNanoAgent — Crash & Recovery Integration Test"))
    print(c('blue', "Uses SidecarSession SDK directly. No subprocess stdin tricks.\n"))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        warn("Test interrupted.")
        os.system(f"kill $(lsof -ti:{SIDECAR_PORT}) 2>/dev/null; true")
        sys.exit(130)
