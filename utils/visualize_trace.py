import json
import sys
import os
import argparse

def generate_mermaid_graph(jsonl_path, output_path=None):
    if not os.path.exists(jsonl_path):
        print(f"Error: File {jsonl_path} does not exist.")
        sys.exit(1)

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    mermaid_lines = [
        "%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#f4f4f4', 'edgeLabelBackground':'#ffffff', 'tertiaryColor': '#f0f0f0'}}}%%",
        "graph TD",
        "    classDef user fill:#e1f5fe,stroke:#0288d1,stroke-width:2px;",
        "    classDef llm fill:#fff3e0,stroke:#f57c00,stroke-width:2px;",
        "    classDef tool fill:#e8f5e9,stroke:#388e3c,stroke-width:2px;",
        "    classDef error fill:#ffebee,stroke:#d32f2f,stroke-width:2px;",
    ]

    node_counter = 0
    def get_node_id():
        nonlocal node_counter
        node_counter += 1
        return f"N{node_counter}"

    prev_node = None

    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("event_type")
        data = event.get("data", {})

        current_node = get_node_id()
        label = ""
        node_class = ""

        if event_type == "user_input":
            content = data.get("content", "").replace('"', "'").replace("\n", " ").strip()
            if len(content) > 50:
                content = content[:47] + "..."
            label = f"User Request:<br/>{content}"
            mermaid_lines.append(f'    {current_node}("{label}"):::user')
            node_class = "user"

        elif event_type == "llm_generation":
            thoughts = []
            tool_calls = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "").replace('"', "'").replace("\n", " ").strip()
                    if len(text) > 40:
                        text = text[:37] + "..."
                    thoughts.append(text)
                elif block.get("type") == "tool_use":
                    tool_calls.append(block.get("name"))
            
            thought_text = "<br/>".join(thoughts) if thoughts else "No explicit thought"
            if tool_calls:
                tools_text = "Calls: " + ", ".join(tool_calls)
                label = f"LLM Planning:<br/>{thought_text}<br/><b>{tools_text}</b>"
            else:
                label = f"LLM Response:<br/>{thought_text}"
            
            mermaid_lines.append(f'    {current_node}("{label}"):::llm')
            node_class = "llm"

        elif event_type == "tool_execution":
            tool_name = data.get("tool_name", "unknown_tool")
            effect_type = data.get("effect_type", "unknown")
            cmd = data.get("command", "").replace('"', "'").replace("\n", " ").strip()
            if len(cmd) > 40:
                cmd = cmd[:37] + "..."
                
            res = data.get("result", "").replace('"', "'").replace("\n", " ").strip()
            status = "Success"
            if "[error]" in res or "Error:" in res:
                status = "Error"
                
            label = f"Tool: {tool_name}<br/>Effect: {effect_type}<br/>Cmd: {cmd}<br/>Status: {status}"
            
            if status == "Error":
                mermaid_lines.append(f'    {current_node}("{label}"):::error')
            else:
                mermaid_lines.append(f'    {current_node}("{label}"):::tool')
            node_class = "tool"
            
        else:
            continue

        if prev_node:
            mermaid_lines.append(f'    {prev_node} --> {current_node}')
        
        prev_node = current_node

    mermaid_code = "\n".join(mermaid_lines)
    
    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(f"```mermaid\n{mermaid_code}\n```\n")
        print(f"[{jsonl_path}] -> Mermaid graph saved to {output_path}")
    else:
        print("\n```mermaid\n" + mermaid_code + "\n```\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visualize Trajectory JSONL as a Mermaid Graph")
    parser.add_argument("jsonl_file", help="Path to the JSONL trajectory file")
    parser.add_argument("-o", "--output", help="Optional path to output markdown file (e.g. graph.md)")
    args = parser.parse_args()
    
    generate_mermaid_graph(args.jsonl_file, args.output)
