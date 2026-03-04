import asyncio
import json
import os
import sys
from datetime import timedelta

from dotenv import load_dotenv
from openai import AsyncOpenAI
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

load_dotenv()

# We will use the standard openai sdk to support ANY openai compatible endpoint
# (e.g., local vllm, qwen, deepseek, or openai)
client = AsyncOpenAI(
    api_key=os.environ.get("ANTHROPIC_API_KEY", "sk-api-W_DQfkTlXiMgb5tHQKVCpYEaq9KcaR7v86pzN6ojvy9tsyqGmwdkjQ1LS2Oxd_KfbASq-p-OxnjeeysT5GMFBEn0xGzEb33JRIPfEUhScIMXGlx0UDe6a9g"),
    base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic"),
)
MODEL_NAME = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# The unified tool schema for OpenAI tool calling
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_code_in_sandbox",
            "description": "Executes bash or python commands inside a secure sandbox container and returns the standard output/error.",
            "parameters": {
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
    }
]

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

async def handle_tool_call(tool_call, sandbox: Sandbox) -> str:
    """Execute a specific tool call coming from the LLM"""
    if tool_call.function.name == "run_code_in_sandbox":
        args = json.loads(tool_call.function.arguments)
        command = args["command"]
        print(f"\n[Agent Tool Calling] Executing: {command}\n")
        
        # Run command in sandbox
        execution = await sandbox.commands.run(command)
        
        # Fetch the outputs
        result = await _print_execution_logs(execution)
        return result
    else:
        return f"Error: Unknown tool {tool_call.function.name}"

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

    # Initialize Memory
    messages = [
        {"role": "system", "content": "You are a helpful AI assistant that has access to a secure bash terminal sandbox. You can use the 'run_code_in_sandbox' tool to execute any code, read files, debug, and perform actions. You should write files directly, run them, explore the environment, and answer the user's questions based on the sandbox execution results."}
    ]

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

                while True:
                    # Request LLM response
                    response = await client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=messages,
                        tools=TOOLS,
                        tool_choice="auto"
                    )
                    
                    response_message = response.choices[0].message
                    # For openai > 1.0.0 we append the message dict representation or the object
                    messages.append(response_message)

                    if response_message.tool_calls:
                        # LLM wants to call a tool
                        for tool_call in response_message.tool_calls:
                            tool_result = await handle_tool_call(tool_call, sandbox)
                            
                            # Append the tool result back to internal memory
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.function.name,
                                "content": str(tool_result),
                            })
                        
                        # Loop back up to let LLM generate a new message observing the tool output
                    else:
                        # LLM gave a text answer
                        print(f"\nAssistant: {response_message.content}")
                        break # Break inner loop, wait for next user input
                        
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"\nAn error occurred: {e}")
                
        # Cleanup
        print("\nCleaning up sandbox...")
        await sandbox.kill()

if __name__ == "__main__":
    asyncio.run(main())
