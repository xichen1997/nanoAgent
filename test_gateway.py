import subprocess
import time
import os
import sys

def run_test():
    prompt = """
请使用你新获得的 `gateway_fetch_url` 工具，发送一个 GET 请求到 https://httpbin.org/get ，然后告诉我返回的 JSON 里 `url` 字段的内容是什么。
完成之后告诉我你成功了，然后打印 exit。
"""
    print("--- 启动初次运行 ---")
    p = subprocess.Popen(
        ['/home/xichen/dev/OpenSandbox/.venv/bin/python', 'agent.py', '--debug'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    stdout, _ = p.communicate(input=prompt)
    
    # 提取生成的 JSONL 路径
    jsonl_path = None
    for line in stdout.split('\n'):
        if "[*] Debug/RL Trajectory logging enabled:" in line:
            jsonl_path = line.split("enabled: ")[1].strip()
            
    print(stdout)
    
    if not jsonl_path:
        print("未找到 JSONL 轨迹文件，测试失败。")
        sys.exit(1)
        
    print(f"\n--- 初次运行完毕，轨迹已保存至 {jsonl_path} ---")
    print("\n--- 现在开始由于模型宕机引起的重演 (Replay) 测试 ---")
    print("理论上 Gateway GET 请求是 pure_read (no_side_effects)，因此重放必须跳过真实执行，直接用 JSONL 的旧响应。")
    
    time.sleep(1) # wait a moment before replay
    
    p_replay = subprocess.Popen(
        ['/home/xichen/dev/OpenSandbox/.venv/bin/python', 'agent.py', '--resume', jsonl_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    stdout_replay, _ = p_replay.communicate(input="exit\n")
    print(stdout_replay)
    
if __name__ == "__main__":
    run_test()
