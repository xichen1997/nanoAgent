import asyncio
import json
import os
import sys
import argparse
from datetime import timedelta, datetime
import time

from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

load_dotenv()

# We use the anthropic SDK to support Minimax's Anthropic-compatible endpoint
api_key = os.environ.get("ANTHROPIC_API_KEY", "dummy-key")
client = AsyncAnthropic(
    api_key=api_key,
    base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic"),
    default_headers={"Authorization": f"Bearer {api_key}"}
)
MODEL_NAME = os.environ.get("LLM_MODEL", "MiniMax-M2.5")

# The unified tool schema for Anthropic tool calling
TOOLS = [
    {
        "name": "sandbox_no_side_effects",
        "description": "[SAFE TO SKIP] Executes a bash command that has absolutely NO side effects (e.g., cat, ls, find). During system recovery (Replay), this won't be executed again; cached results will be used.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line string to execute."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "sandbox_action_replayable",
        "description": "[SAFE TO REPLAY] Executes a bash command that modifies the sandbox state but can be safely re-executed (e.g., writing files, apt-get install, mkdir). During system recovery (Replay), this WILL be re-executed to rebuild the environment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line string to execute."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "sandbox_action_irreversible",
        "description": "[NEVER REPLAY] Executes a bash command that interacts with external APIs or performs irreversible real-world actions (e.g., curl POST, dropping a DB). During system recovery (Replay), this is STRICTLY BLOCKED from executing twice; cached results will be used.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line string to execute."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "gateway_fetch_url",
        "description": "Performs an HTTP request from the host gateway (bypassing sandbox restrictions). Used for accessing the internet, fetching APIs, or querying web pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full HTTP/HTTPS URL to request."
                },
                "method": {
                    "type": "string",
                    "description": "The HTTP method (e.g., GET, POST, PUT, DELETE). Default is GET.",
                    "default": "GET"
                },
                "data": {
                    "type": "string",
                    "description": "Optional payload data for POST/PUT requests."
                }
            },
            "required": ["url"]
        }
    }
]

SYSTEM_PROMPT = """You are a helpful AI assistant with access to a secure bash sandbox AND a host Capability Gateway. 

CRITICAL SECURITY RULES:
1. The sandbox environment has NO external network access. DO NOT use `curl`, `wget`, or any custom python/node scripts inside the sandbox to access the internet.
2. For all external web requests or API calls, you MUST use the `gateway_fetch_url` tool.
3. You MUST classify sandbox actions precisely:
   - 'sandbox_no_side_effects' for pure reads (e.g., cat, ls).
   - 'sandbox_action_replayable' for state mutations that can be safely repeated (e.g., mkdir, writing config files).
   - 'sandbox_action_irreversible' only when strictly unavoidable inside the sandbox.
"""

class TrajectoryLogger:
    def __init__(self, enabled: bool, resume_file: str = None, fork_at: int = None):
        self.enabled = enabled
        self.filepath = None
        self.logged_lines = 0
        if self.enabled:
            os.makedirs("logs", exist_ok=True)
            self.filepath = f"logs/trajectory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            print(f"\n[*] Debug/RL Trajectory logging enabled: {self.filepath}")

            # Copy previous trajectory if resuming
            if resume_file and os.path.exists(resume_file):
                with open(resume_file, "r", encoding="utf-8") as rf:
                    lines = rf.readlines()
                if fork_at is not None:
                    lines = lines[:fork_at]
                with open(self.filepath, "a", encoding="utf-8") as wf:
                    for line in lines:
                        wf.write(line)
                self.logged_lines = len(lines)
                print(f"[*] Trajectory successfully ported. Copied {self.logged_lines} historical events.")

    def log_event(self, event_type: str, data: dict):
        if not self.enabled:
            return
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "data": data
        }
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.logged_lines += 1

async def _print_execution_logs(execution) -> str:
    """Helper to collect and return the execution logs as a string."""
    outputs = []
    
    for msg in execution.logs.stdout:
        print(f"[sandbox stdout] {msg.text}")
        outputs.append(msg.text)
        
    for msg in execution.logs.stderr:
        print(f"[sandbox stderr] {msg.text}")
        outputs.append(f"[stderr] {msg.text}")
        
    if execution.error:
        print(f"[sandbox error] {execution.error.name}: {execution.error.value}")
        outputs.append(f"[error] {execution.error.name}: {execution.error.value}")
        
    res = "\n".join(outputs).strip()
    return res if res else "(command executed successfully with no output)"

async def handle_tool_call(tool_use_block, sandbox: Sandbox, logger: TrajectoryLogger) -> str:
    """Execute a specific tool call coming from the LLM"""
    tool_name = tool_use_block.name
    
    start_time = time.time()
    
    if tool_name in ["sandbox_no_side_effects", "sandbox_action_replayable", "sandbox_action_irreversible"]:
        effect_type = tool_name.replace("sandbox_", "")
        command_executed = tool_use_block.input["command"]
        print(f"\n[Agent Tool Calling] ({effect_type}) Executing: {command_executed[:80]}...\n")
        execution = await sandbox.commands.run(command_executed)
        result = await _print_execution_logs(execution)
        
    elif tool_name == "gateway_fetch_url":
        import urllib.request
        import urllib.error
        url = tool_use_block.input["url"]
        method = tool_use_block.input.get("method", "GET").upper()
        data = tool_use_block.input.get("data", None)
        
        # As per strict isolation requirements, ALL external gateway requests (even GET) 
        # are considered irreversible side effects that must be faked during Replay 
        # to prevent drift or hitting external targets again.
        effect_type = "action_irreversible"
        command_executed = f"Gateway {method} {url}"
        print(f"\n[Gateway Intercept] ({effect_type}) Fetching URL: {url}\n")
        
        try:
            req_data = data.encode('utf-8') if data else None
            req = urllib.request.Request(url, data=req_data, method=method)
            # Add a generic user agent to avoid basic blocks
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
            with urllib.request.urlopen(req, timeout=15) as response:
                html = response.read().decode('utf-8')
                result = f"Status: {response.status}\n\n{html[:4000]}" # truncate to avoid blowing up context
                if len(html) > 4000:
                    result += "\n...[Content Truncated]..."
        except urllib.error.URLError as e:
            result = f"Gateway HTTP Error: {str(e)}"
        except Exception as e:
            result = f"Gateway System Error: {str(e)}"
            
    else:
        return f"Error: Unknown tool {tool_name}"

    end_time = time.time()
    
    logger.log_event("tool_execution", {
        "tool_use_id": tool_use_block.id,
        "tool_name": tool_name,
        "effect_type": effect_type,
        "command": command_executed,
        "execution_time_sec": round(end_time - start_time, 3),
        "result": result
    })
    return result

async def replay_trajectory(filepath: str, sandbox: Sandbox, max_steps: int = None) -> list:
    """Read a JSONL trajectory, reconstruct LLM memory, and replay environment commands."""
    messages = []
    current_tool_results = []
    
    print(f"\n[Replayer] Reconstructing state from {filepath} (Fork at step: {max_steps if max_steps is not None else 'end'})...")
    
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    if max_steps is not None:
        lines = lines[:max_steps]
        
    replayed_cmds = 0
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
            
        event_type = event.get("event_type")
        data = event.get("data", {})
        
        # If we encounter a new user_input or llm_generation, flush any pending tool_results
        if event_type in ("user_input", "llm_generation") and current_tool_results:
            messages.append({"role": "user", "content": current_tool_results})
            current_tool_results = []

        if event_type == "user_input":
            messages.append({"role": "user", "content": data.get("content", "")})
            
        elif event_type == "llm_generation":
            messages.append({"role": "assistant", "content": data.get("content", [])})
            
        elif event_type == "tool_execution":
            tool_name = data.get("tool_name", "sandbox_action_irreversible")
            effect_type = data.get("effect_type", "action_irreversible")
            cmd = data.get("command", "")
            
            # Legacy traces translation
            if tool_name in ["run_code_in_sandbox", "run_command_in_sandbox", "write_file_in_sandbox"]:
                tool_name = "sandbox_action_replayable"
                effect_type = "action_replayable"
            elif tool_name == "read_file_in_sandbox":
                tool_name = "sandbox_no_side_effects"
                effect_type = "no_side_effects"
                
            print(f"[Replayer] FAST-FORWARD step {idx} ({effect_type}): '{tool_name}' -> {cmd[:60]}...")
            
            if effect_type == "no_side_effects":
                print(f"  -> [Skipped Sandbox/Gateway Execution] Pure read. Providing cached result directly.")
            elif effect_type == "action_irreversible":
                print(f"  -> [Skipped Sandbox/Gateway Execution] DANGER: Irreversible action. Providing cached fake result to resume securely.")
            else:
                # action_replayable
                # We specifically only want to replay sandbox container state
                if tool_name != "gateway_fetch_url":
                    # Execute in the real environment to restore state
                    await sandbox.commands.run(cmd)
                    replayed_cmds += 1
                else:
                    print(f"  -> [Skipped Gateway Execution] Gateway actions cannot reconstruct sandbox state.")
                
            current_tool_results.append({
                "type": "tool_result",
                "tool_use_id": data.get("tool_use_id"),
                "content": data.get("result", ""),
            })

    # Flush dangling
    if current_tool_results:
        messages.append({"role": "user", "content": current_tool_results})
        
    print(f"[Replayer] Done! Restored {len(messages)} message turns and fast-forwarded {replayed_cmds} commands.\n")
    return messages

async def run_agent_session(resume_file: str, fork_at: int, debug_mode: bool):
    print("Setting up OpenSandbox...")
    domain = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
    api_key = os.getenv("SANDBOX_API_KEY")
    # Using the standard image unless specified
    image = os.getenv("SANDBOX_IMAGE", "ubuntu:22.04")

    config = ConnectionConfig(
        domain=domain,
        api_key=api_key,
        request_timeout=timedelta(seconds=60),
    )

    try:
        sandbox = await Sandbox.create(
            image,
            connection_config=config,
            timeout=timedelta(minutes=30),  # Keep alive for 30 minutes
        )
        print(f"Sandbox created successfully! ID: {sandbox.id}")
    except Exception as e:
        print(f"Failed to create OpenSandbox: {e}\nEnsure your server is running and configured correctly.")
        return "setup_failed", resume_file, fork_at

    logger = TrajectoryLogger(debug_mode, resume_file, fork_at)
    
    replay_file = resume_file
    replay_fork = fork_at
    if logger.filepath and os.path.exists(logger.filepath):
        # We replay from the newly ported active log where lines are already cleanly truncated
        replay_file = logger.filepath
        replay_fork = None

    messages = []
    
    if replay_file and os.path.exists(replay_file):
        try:
            messages = await replay_trajectory(replay_file, sandbox, replay_fork)
        except Exception as e:
            print(f"Error resuming trajectory: {e}")

    print("\n--- Agent Ready! Type 'exit' or 'quit' to terminate. ---")
    
    auto_trigger = False
    if messages and messages[-1]["role"] == "user":
        auto_trigger = True
        print("[Auto-Recovery] Resuming LLM generation from last user state automatically...")

    try:
        async with sandbox:
            while True:
                try:
                    if not auto_trigger:
                        user_msg = input("\nUser: ")
                        if user_msg.lower() in ("exit", "quit"):
                            return "exit", None, None
                        if not user_msg.strip():
                            continue
                        
                        # Update memory
                        messages.append({"role": "user", "content": user_msg})
                        logger.log_event("user_input", {"content": user_msg})
                    else:
                        auto_trigger = False # Only trigger once upon resume

                    while True:
                        # Request LLM response
                        start_time = time.time()
                        response = await client.messages.create(
                            model=MODEL_NAME,
                            system=SYSTEM_PROMPT,
                            messages=messages,
                            tools=TOOLS,
                            max_tokens=4096,
                        )
                        end_time = time.time()
                        
                        # Add assistant reply to memory
                        messages.append({"role": "assistant", "content": response.content})

                        blocks_dump = []
                        for block in response.content:
                            if block.type == "text":
                                blocks_dump.append({"type": "text", "text": block.text})
                            elif block.type == "tool_use":
                                blocks_dump.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                                
                        logger.log_event("llm_generation", {
                            "model": MODEL_NAME,
                            "stop_reason": response.stop_reason,
                            "generation_time_sec": round(end_time - start_time, 3),
                            "content": blocks_dump,
                            "usage": response.usage.model_dump() if hasattr(response, "usage") else None
                        })

                        if response.stop_reason == "tool_use":
                            # LLM wants to call a tool
                            tool_uses = [block for block in response.content if block.type == "tool_use"]
                            
                            tool_results = []
                            for tool_use in tool_uses:
                                tool_result_str = await handle_tool_call(tool_use, sandbox, logger)
                                
                                # Anthropic tool results are passed in as "content" inside a user message
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": str(tool_result_str),
                                })
                                
                            # Append the tool result back to internal memory as a user message
                            messages.append({
                                "role": "user",
                                "content": tool_results,
                            })
                            
                            # Loop back up to let LLM generate a new message observing the tool output
                        else:
                            # LLM gave a text answer
                            text_blocks = [block.text for block in response.content if block.type == "text"]
                            reply = "\n".join(text_blocks)
                            print(f"\nAssistant: {reply}")
                            break # Break inner loop, wait for next user input
                            
                except (KeyboardInterrupt, EOFError):
                    return "exit", None, None
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    print(f"\n[Session Crash] An error occurred: {e}")
                    
                    # Calculate next resume state
                    next_fork_at = logger.logged_lines
                    if logger.filepath and os.path.exists(logger.filepath):
                        with open(logger.filepath, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        if lines:
                            try:
                                last_event = json.loads(lines[-1]).get("event_type")
                                if last_event == "llm_generation":
                                    next_fork_at = max(0, len(lines) - 1)
                                    print(f"[Auto-Recovery] Crash detected during tool execution. Rolling back 1 step to let LLM retry.")
                                else:
                                    next_fork_at = len(lines)
                            except:
                                next_fork_at = len(lines)
                                
                    return "crashed", logger.filepath, next_fork_at
    finally:
        print("\nCleaning up sandbox...")
        try:
            await sandbox.kill()
        except:
            pass

async def main():
    parser = argparse.ArgumentParser(description="OpenSandbox Agent")
    parser.add_argument("--debug", action="store_true", help="Enable trajectory logging")
    parser.add_argument("--resume", type=str, help="Path to JSONL trajectory file to resume from")
    parser.add_argument("--fork-at", type=int, default=None, help="JSONL line index (0-indexed) to fork at")
    args, _ = parser.parse_known_args()

    debug_mode = args.debug or os.environ.get("DEBUG_MODE", "false").lower() == "true"
    
    current_resume = args.resume
    current_fork = args.fork_at
    
    while True:
        status, next_resume, next_fork = await run_agent_session(current_resume, current_fork, debug_mode)
        if status == "exit":
            break
        elif status == "setup_failed":
            print("Retrying setup in 5 seconds...")
            await asyncio.sleep(5)
            # current_resume and fork stay the same
        elif status == "crashed":
            print("Agent session crashed. Initiating Auto-Recovery in 3 seconds...")
            await asyncio.sleep(3)
            current_resume = next_resume
            current_fork = next_fork
            # Always enable debug logging implicitly if we are auto-recovering
            debug_mode = True

if __name__ == "__main__":
    asyncio.run(main())
