"""临时复现：用安装版模型 + llama-server 复现模型加载问题（8099 端口，不碰 8080）。"""
import subprocess
import time

import requests

server = r"D:\新建文件夹 (3)\星芒\third_party\llama\llama-server.exe"
model = r"D:\新建文件夹 (3)\星芒\models\Qwen3-ASR-0.6B-Q8_0.gguf"
mmproj = r"D:\新建文件夹 (3)\星芒\models\mmproj-Qwen3-ASR-0.6B-bf16.gguf"

print("llama-server 存在:", subprocess.os.path.isfile(server))
print("model 存在:", subprocess.os.path.isfile(model))
print("mmproj 存在:", subprocess.os.path.isfile(mmproj))

# 完全复刻 asr_bootstrap 的启动参数（含 --parallel 2）
args = [server, "-m", model, "--port", "8099", "--host", "127.0.0.1",
        "--mmproj", mmproj, "--parallel", "2"]
p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(18)  # 给模型加载时间（767MB）

try:
    r = requests.get("http://127.0.0.1:8099/v1/models", timeout=5)
    print("=== /v1/models ===", r.text[:300])
except Exception as e:
    print("=== 探测失败 ===", e)

print("=== 进程状态 ===", "poll:", p.poll())
print("=== llama-server 输出（前 40 行）===")
lines = p.stdout.read().decode("utf-8", "replace").splitlines() if p.stdout else []
for ln in lines[:40]:
    print(ln)
p.terminate()
