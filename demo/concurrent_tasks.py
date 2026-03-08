"""
demo/concurrent_tasks.py — Integration Test: Trunk-based Concurrent Tasks

Tests the fork/commit/conflict cycle:

  Setup:
    1. Start Sidecar subprocess
    2. POST /session/start  → fresh sandbox
    3. POST /trunk/init     → create initial trunk from empty sandbox

  Phase 1 — Two forks run in parallel (simulated serially via two Sidecars):
    4. Sidecar A: POST /trunk/fork  → Fork A on sandbox_a
    5. Sidecar A: POST /tool/execute bash_write  → writes /tmp/task_a.txt
    6. Sidecar B: POST /trunk/fork  → Fork B on sandbox_b (same trunk)
    7. Sidecar B: POST /tool/execute bash_write  → writes /tmp/task_b.txt

  Phase 2 — Fork A commits first:
    8. Sidecar A: POST /trunk/commit  → {ok, new_trunk_id}
    9. new_trunk_id is now the canonical state

  Phase 3 — Fork B tries to commit → CONFLICT (its trunk_id is stale):
    10. Sidecar B: POST /trunk/commit → {conflict: true, current_trunk_id}
    11. Sidecar B: POST /trunk/abort

  Phase 4 — Fork B re-forks from new trunk and commits:
    12. Sidecar B2: POST /trunk/fork {trunk_id: new_trunk_id}
    13. Sidecar B2: POST /tool/execute bash_write → writes /tmp/task_b.txt
    14. Sidecar B2: POST /trunk/commit → {ok, final_trunk_id}

  Phase 5 — Assertions.

Run:
    python demo/concurrent_tasks.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PYTHON = sys.executable
SIDECAR_A_PORT = 7878
SIDECAR_B_PORT = 7879
SIDECAR_A_URL = f"http://127.0.0.1:{SIDECAR_A_PORT}"
SIDECAR_B_URL = f"http://127.0.0.1:{SIDECAR_B_PORT}"

COLORS = {
    "green":  "\033[92m", "yellow": "\033[93m",
    "red":    "\033[91m", "blue":   "\033[94m",
    "bold":   "\033[1m",  "reset":  "\033[0m",
    "cyan":   "\033[96m",
}
def c(color, text): return f"{COLORS[color]}{text}{COLORS['reset']}"
def header(msg): print(f"\n{c('bold','='*60)}\n{c('bold', msg)}\n{c('bold','='*60)}", flush=True)
def ok(msg):   print(c('green',  f"✅  {msg}"), flush=True)
def err(msg):  print(c('red',    f"❌  {msg}"), flush=True)
def info(msg): print(c('blue',   f"ℹ️   {msg}"), flush=True)
def warn(msg): print(c('yellow', f"⚠️   {msg}"), flush=True)
def nota(label, msg): print(c('cyan', f"[{label}] {msg}"), flush=True)


def sidecar_post(base_url: str, path: str, body: dict) -> dict:
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def wait_for_sidecar(url: str, timeout=20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def start_sidecar(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["SIDECAR_PORT"] = str(port)
    return subprocess.Popen(
        [PYTHON, "-u", "-m", "sidecar.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def check(label: str, cond: bool, results: list):
    if cond:
        ok(label)
        results.append(True)
    else:
        err(label)
        results.append(False)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Kill any leftover sidecars on these ports
    for port in (SIDECAR_A_PORT, SIDECAR_B_PORT):
        os.system(f"kill $(lsof -ti:{port}) 2>/dev/null; true")
    time.sleep(0.5)

    info("Starting Sidecar A and Sidecar B...")
    sidecar_a = start_sidecar(SIDECAR_A_PORT)
    sidecar_b = start_sidecar(SIDECAR_B_PORT)

    if not wait_for_sidecar(SIDECAR_A_URL):
        err("Sidecar A failed to start!")
        sidecar_a.kill(); sidecar_b.kill(); sys.exit(1)
    if not wait_for_sidecar(SIDECAR_B_URL):
        err("Sidecar B failed to start!")
        sidecar_a.kill(); sidecar_b.kill(); sys.exit(1)
    ok("Both Sidecars listening.")

    results = []

    try:
        # ── Setup: create trunk ────────────────────────────────────────────────
        header("SETUP: Create initial trunk")
        setup = sidecar_post(SIDECAR_A_URL, "/session/start", {})
        info(f"Setup session: {setup['session_id']}")
        trunk_resp = sidecar_post(SIDECAR_A_URL, "/trunk/init", {})
        initial_trunk_id = trunk_resp["trunk_id"]
        ok(f"Initial trunk created: {initial_trunk_id}")
        sidecar_post(SIDECAR_A_URL, "/session/end", {})

        # ── Phase 1: Fork A ────────────────────────────────────────────────────
        header("PHASE 1A: Fork A from trunk")
        fork_a = sidecar_post(SIDECAR_A_URL, "/trunk/fork", {})
        nota("A", f"Fork session: {fork_a['session_id']}, trunk: {fork_a['trunk_id']}")
        check("Fork A based on initial trunk",
              fork_a["trunk_id"] == initial_trunk_id, results)

        # Fork A does some work
        sidecar_post(SIDECAR_A_URL, "/tool/execute", {
            "tool_name": "bash_write",
            "tool_input": {"command": "echo 'task_a_complete' > /tmp/task_a.txt"},
        })
        nota("A", "Wrote /tmp/task_a.txt")

        # ── Phase 1: Fork B ────────────────────────────────────────────────────
        header("PHASE 1B: Fork B from same trunk")
        fork_b = sidecar_post(SIDECAR_B_URL, "/trunk/fork", {})
        nota("B", f"Fork session: {fork_b['session_id']}, trunk: {fork_b['trunk_id']}")
        check("Fork B based on same initial trunk",
              fork_b["trunk_id"] == initial_trunk_id, results)

        # Fork B does some work
        sidecar_post(SIDECAR_B_URL, "/tool/execute", {
            "tool_name": "bash_write",
            "tool_input": {"command": "echo 'task_b_complete' > /tmp/task_b.txt"},
        })
        nota("B", "Wrote /tmp/task_b.txt")

        # ── Phase 2: Fork A commits first ─────────────────────────────────────
        header("PHASE 2: Fork A commits → new trunk")
        commit_a = sidecar_post(SIDECAR_A_URL, "/trunk/commit", {})
        nota("A", f"Commit result: {commit_a}")
        check("Fork A commit succeeds (no conflict)",
              commit_a.get("ok") is True and not commit_a.get("conflict"), results)
        new_trunk_id = commit_a.get("new_trunk_id")
        ok(f"New trunk after A: {new_trunk_id}")

        # ── Phase 3: Fork B tries to commit → CONFLICT ────────────────────────
        header("PHASE 3: Fork B commit → CONFLICT (trunk advanced)")
        commit_b = sidecar_post(SIDECAR_B_URL, "/trunk/commit", {})
        nota("B", f"Commit result: {commit_b}")
        check("Fork B gets conflict (trunk advanced by A)",
              commit_b.get("conflict") is True, results)
        check("Conflict reports correct current trunk",
              commit_b.get("current_trunk_id") == new_trunk_id, results)

        # Abort stale fork B
        sidecar_post(SIDECAR_B_URL, "/trunk/abort", {})
        nota("B", "Stale fork aborted.")

        # ── Phase 4: Fork B re-forks from new trunk and commits ──────────────
        header("PHASE 4: Fork B2 → re-fork from new trunk → commit")
        fork_b2 = sidecar_post(SIDECAR_B_URL, "/trunk/fork", {"trunk_id": new_trunk_id})
        nota("B2", f"Re-forked from trunk: {fork_b2['trunk_id']}")
        check("Fork B2 based on new trunk (A's committed state)",
              fork_b2["trunk_id"] == new_trunk_id, results)

        # B2 re-does its work on the new trunk base
        r = sidecar_post(SIDECAR_B_URL, "/tool/execute", {
            "tool_name": "bash_write",
            "tool_input": {"command": "echo 'task_b_complete' > /tmp/task_b.txt"},
        })
        nota("B2", f"Re-wrote /tmp/task_b.txt: {r['result']!r}")

        # Verify /tmp/task_a.txt is also there (inherited from trunk via A's commit)
        read_a = sidecar_post(SIDECAR_B_URL, "/tool/execute", {
            "tool_name": "bash_read",
            "tool_input": {"command": "cat /tmp/task_a.txt"},
        })
        nota("B2", f"/tmp/task_a.txt content: {read_a['result']!r}")
        check("B2 sandbox contains A's work (inherited from trunk)",
              "task_a_complete" in read_a.get("result", ""), results)

        commit_b2 = sidecar_post(SIDECAR_B_URL, "/trunk/commit", {})
        nota("B2", f"Commit result: {commit_b2}")
        check("Fork B2 commit succeeds after re-fork",
              commit_b2.get("ok") is True, results)
        final_trunk_id = commit_b2.get("new_trunk_id")
        ok(f"Final trunk: {final_trunk_id}")

        check("Three distinct trunk versions created (init → after A → after B2)",
              len({initial_trunk_id, new_trunk_id, final_trunk_id}) == 3, results)

        # ── Phase 5: Results ───────────────────────────────────────────────────
        header("PHASE 5: Results")
        passed = sum(results)
        failed = len(results) - passed
        print(f"  {c('green', str(passed))} passed   {c('red', str(failed))} failed")
        if failed == 0:
            print(c('green', c('bold', "\n🎉  Concurrent Tasks integration test PASSED!")))
        else:
            print(c('red', c('bold', "\n💥  Some assertions failed.")))
            sys.exit(1)

    finally:
        sidecar_a.kill(); sidecar_a.wait()
        sidecar_b.kill(); sidecar_b.wait()
        info("Sidecars shut down.")


if __name__ == "__main__":
    print(c('bold', "\nNanoAgent — Concurrent Tasks Integration Test"))
    print(c('blue', "Tests fork/commit/conflict/refork cycle using two Sidecar processes.\n"))
    try:
        main()
    except KeyboardInterrupt:
        warn("Test interrupted.")
        for port in (SIDECAR_A_PORT, SIDECAR_B_PORT):
            os.system(f"kill $(lsof -ti:{port}) 2>/dev/null; true")
        sys.exit(130)
