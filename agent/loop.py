"""
agent/loop.py — The Pure Agent Loop (Ring 3: User Space).

This process has ZERO knowledge of:
  - Effect types or replay semantics
  - SQLite, JSONL, or any logging mechanism
  - Sandbox SDK or credentials
  - Policy enforcement

It simply:
  1. POST /session/start  → gets back restored messages (if resuming)
  2. Reads user input
  3. POST /llm/generate   → gets LLM response (sidecar calls Claude + logs)
  4. For each tool_use: POST /tool/execute → gets result (sidecar runs + logs)
  5. Loops back to 3

The Sidecar is its entire world.
"""
import json
import os
import sys
import urllib.request
import urllib.error

SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://127.0.0.1:7878")

SYSTEM_PROMPT = """You are a helpful AI assistant with access to a secure bash sandbox and a host Capability Gateway.

You have three bash tools:
- bash_read: for pure read-only commands (cat, ls, find, echo)
- bash_write: for commands that create or modify files and install packages (mkdir, pip install, writing scripts)
- bash_run: for any other commands that may have external side effects

For any HTTP/web requests, you MUST use fetch_url. Never use curl or wget inside the sandbox.
"""


def sidecar_post(path: str, data: dict) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{SIDECAR_URL}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        raise RuntimeError(f"Sidecar {path} HTTP {e.code}: {raw}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Sidecar unreachable at {SIDECAR_URL}: {e.reason}")


def run(resume_session_id: str | None = None, fork_at: int | None = None):
    # ── Start session (creates sandbox, replays if resuming) ─────────────────
    start_payload = {}
    if resume_session_id:
        start_payload["resume_session_id"] = resume_session_id
        if fork_at is not None:
            start_payload["fork_at"] = fork_at

    session_info = sidecar_post("/session/start", start_payload)
    session_id = session_info["session_id"]
    replay_mode = session_info.get("replay_mode", False)

    # Restore messages from checkpoint
    messages: list = session_info.get("restored_messages", [])

    print(f"\n--- Agent Ready! Session: {session_id} {'[REPLAY MODE]' if replay_mode else ''} ---")
    print("Type 'exit' or 'quit' to terminate.\n")
    sys.stdout.flush()

    # Auto-trigger if we have pending messages from a prior session
    auto_trigger = bool(messages) and messages[-1]["role"] == "user"
    if auto_trigger:
        print("[Agent] Auto-triggering LLM from restored checkpoint...")
        sys.stdout.flush()

    try:
        while True:
            # ── Get user input (or auto-trigger) ─────────────────────────────
            if not auto_trigger:
                try:
                    user_msg = input("\n>>> ")
                except (KeyboardInterrupt, EOFError):
                    break

                if user_msg.lower() in ("exit", "quit"):
                    break
                if not user_msg.strip():
                    continue

                messages.append({"role": "user", "content": user_msg})
            else:
                auto_trigger = False

            # ── Agentic loop: call LLM until we get a text stop ──────────────
            while True:
                gen = sidecar_post("/llm/generate", {
                    "messages": messages,
                    "system": SYSTEM_PROMPT,
                })

                content = gen["content"]
                stop_reason = gen["stop_reason"]

                # Add assistant turn to local message list
                messages.append({"role": "assistant", "content": content})

                if stop_reason == "tool_use":
                    tool_results = []
                    for block in content:
                        if block["type"] != "tool_use":
                            continue

                        tool_use_id = block["id"]
                        tool_name = block["name"]
                        tool_input = block["input"]

                        exec_resp = sidecar_post("/tool/execute", {
                            "tool_use_id": tool_use_id,
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "session_id": session_id,
                        })

                        result = exec_resp.get("result", "[No result]")
                        if exec_resp.get("replayed"):
                            print(f"\n[↺ {tool_name}] (replayed)")
                        else:
                            print(f"\n[⚡ {tool_name}] ({exec_resp.get('effect', '?')})")
                        # Show output to user (truncate if long)
                        lines = str(result).splitlines()
                        if len(lines) <= 20:
                            print(result)
                        else:
                            print("\n".join(lines[:20]))
                            print(f"... [{len(lines) - 20} more lines]")
                        sys.stdout.flush()

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": str(result),
                        })

                    messages.append({"role": "user", "content": tool_results})

                else:
                    # Text response
                    text = "\n".join(
                        b["text"] for b in content if b.get("type") == "text"
                    )
                    print(f"\nAssistant: {text}")
                    sys.stdout.flush()
                    break

    finally:
        try:
            sidecar_post("/session/end", {"session_id": session_id})
        except Exception:
            pass  # Sidecar may already be gone (e.g. Ctrl+C)

    return session_id


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NanoAgent Loop")
    parser.add_argument("--resume", type=str, default=None, help="Session ID to resume")
    parser.add_argument("--fork-at", type=int, default=None, help="Step to fork at")
    args = parser.parse_args()
    run(resume_session_id=args.resume, fork_at=args.fork_at)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Orchestrator] Interrupted.")
