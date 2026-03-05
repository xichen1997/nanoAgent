import asyncio
import json
import os
import sys
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
MODEL_NAME = os.environ.get("LLM_MODEL", "MiniMax-M2.1")

# The unified tool schema for Anthropic tool calling
TOOLS = [
    {
        "name": "run_code_in_sandbox",
        "description": "Executes bash or python commands inside a secure sandbox container and returns the standard output/error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line string to execute, for example 'python3 -c \"print(1+1)\"' or 'ls -l'."
                }
            },
            "required": ["command"]
        }
    }
]

SYSTEM_PROMPT = "You are a helpful AI assistant that has access to a secure bash terminal sandbox. You can use the 'run_code_in_sandbox' tool to execute any code, read files, debug, and perform actions. You should write files directly, run them, explore the environment, and answer the user's questions based on the sandbox execution results."

class TrajectoryLogger:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.filepath = None
        if self.enabled:
            os.makedirs("logs", exist_ok=True)
            self.filepath = f"logs/trajectory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            print(f"\n[*] Debug/RL Trajectory logging enabled: {self.filepath}")

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
    if tool_use_block.name == "run_code_in_sandbox":
        command = tool_use_block.input["command"]
        print(f"\n[Agent Tool Calling] Executing: {command}\n")
        
        start_time = time.time()
        # Run command in sandbox
        execution = await sandbox.commands.run(command)
        
        # Fetch the outputs
        result = await _print_execution_logs(execution)
        end_time = time.time()
        
        logger.log_event("tool_execution", {
            "tool_use_id": tool_use_block.id,
            "tool_name": tool_use_block.name,
            "command": command,
            "execution_time_sec": round(end_time - start_time, 3),
            "result": result
        })
        return result
    else:
        return f"Error: Unknown tool {tool_use_block.name}"

async def main():
    print("Setting up OpenSandbox...")
    domain = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
    api_key = os.getenv("SANDBOX_API_KEY")
    # Using the standard image unless specified
    image = os.getenv(
        "SANDBOX_IMAGE",
        "ubuntu:22.04",
    )

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
        return

    debug_mode = "--debug" in sys.argv or os.environ.get("DEBUG_MODE", "false").lower() == "true"
    logger = TrajectoryLogger(debug_mode)

    # Initialize Memory
    messages = []

    print("\n--- Agent Ready! Type 'exit' or 'quit' to terminate. ---")
    
    async with sandbox:
        while True:
            try:
                user_msg = input("\nUser: ")
                if user_msg.lower() in ("exit", "quit"):
                    break
                if not user_msg.strip():
                    continue
                
                # Update memory
                messages.append({"role": "user", "content": user_msg})
                logger.log_event("user_input", {"content": user_msg})

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
                        
            except KeyboardInterrupt:
                break
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"\nAn error occurred: {e}")
                
        # Cleanup
        print("\nCleaning up sandbox...")
        await sandbox.kill()

if __name__ == "__main__":
    asyncio.run(main())
