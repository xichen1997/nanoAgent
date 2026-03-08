"""
run.py — Process Orchestrator.

Starts the Sidecar (server.py) as a subprocess, waits for it to be ready,
then starts the Agent (loop.py) as a subprocess.

If the Agent crashes (non-zero exit), the orchestrator reads the last
session_id from the Sidecar, then restarts both in resume mode.
Auto-recovery is fully transparent to both child processes.

Usage:
    python run.py                          # fresh session
    python run.py --resume <session_id>    # manual resume
    python run.py --debug                  # verbose output
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request
import json

PYTHON = sys.executable
SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "7878"))
SIDECAR_URL = f"http://127.0.0.1:{SIDECAR_PORT}"
MAX_RETRIES = 5
RETRY_WAIT_SEC = 3


def wait_for_sidecar(timeout=20.0) -> bool:
    """Poll /health until Sidecar is up. Returns True if ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{SIDECAR_URL}/health", timeout=2) as r:
                data = json.loads(r.read())
                if data.get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def get_last_session_id() -> str | None:
    """Ask the running (or recently crashed) Sidecar for the current session id."""
    try:
        with urllib.request.urlopen(f"{SIDECAR_URL}/health", timeout=3) as r:
            data = json.loads(r.read())
            return data.get("session")
    except Exception:
        return None


def start_sidecar(debug: bool) -> subprocess.Popen:
    kwargs = {} if debug else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    proc = subprocess.Popen(
        [PYTHON, "-u", "-m", "sidecar.server"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        **kwargs,
    )
    return proc


def start_agent(resume_session_id: str | None, fork_at: int | None, debug: bool) -> subprocess.Popen:
    cmd = [PYTHON, "-u", "-m", "agent.loop"]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if fork_at is not None:
        cmd += ["--fork-at", str(fork_at)]
    stderr = None if debug else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stderr=stderr,
    )
    return proc


def main():
    parser = argparse.ArgumentParser(description="NanoAgent Orchestrator")
    parser.add_argument("--resume", default=None, help="Resume an existing session id")
    parser.add_argument("--fork-at", type=int, default=None)
    parser.add_argument("--debug", action="store_true", help="Show sidecar logs")
    args = parser.parse_args()

    resume_session_id = args.resume
    fork_at = args.fork_at
    retries = 0

    while retries <= MAX_RETRIES:
        print(f"\n[Orchestrator] Starting Sidecar (port {SIDECAR_PORT})...")
        sidecar_proc = start_sidecar(args.debug)

        if not wait_for_sidecar():
            print("[Orchestrator] ❌ Sidecar failed to start. Retrying...")
            sidecar_proc.terminate()
            retries += 1
            time.sleep(RETRY_WAIT_SEC)
            continue

        print(f"[Orchestrator] ✅ Sidecar ready.")
        print(f"[Orchestrator] Starting Agent{' (resume: ' + resume_session_id + ')' if resume_session_id else ''}...")

        agent_proc = start_agent(resume_session_id, fork_at, args.debug)
        exit_code = agent_proc.wait()

        if exit_code == 0:
            # Clean exit (user typed exit/quit)
            print("\n[Orchestrator] Agent exited cleanly. Shutting down.")
            sidecar_proc.terminate()
            break
        else:
            # Crash — Auto-Recovery
            retries += 1
            crashed_session = get_last_session_id()
            print(f"\n[Orchestrator] ⚠️  Agent crashed (exit code {exit_code}).")
            if crashed_session and retries <= MAX_RETRIES:
                print(f"[Orchestrator] Auto-Recovery: resuming session {crashed_session} in {RETRY_WAIT_SEC}s...")
                resume_session_id = crashed_session
                fork_at = None  # Sidecar will find last step from DB
                sidecar_proc.terminate()
                time.sleep(RETRY_WAIT_SEC)
            else:
                print("[Orchestrator] Max retries exceeded or no session to resume. Giving up.")
                sidecar_proc.terminate()
                sys.exit(1)


if __name__ == "__main__":
    main()
