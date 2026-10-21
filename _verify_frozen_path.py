"""临时：frozen 模拟验证 asr_bootstrap 路径逻辑（不依赖模型文件）。"""
import os
import sys

# 模拟打包态：sys.executable = 假设的安装版 exe
FAKE_EXE = r"D:\新建文件夹 (3)\星芒\Starlight.exe"
sys.frozen = True
sys.executable = FAKE_EXE

from app.core.path_resolve import PROJECT_ROOT
from app.voice import asr_bootstrap

print("PROJECT_ROOT  =", PROJECT_ROOT)
print("MODEL_DIR     =", asr_bootstrap.MODEL_DIR)
print("LLAMA_DIR     =", asr_bootstrap.LLAMA_DIR)
print("LLAMA_SERVER  =", asr_bootstrap.LLAMA_SERVER_EXE)

# 期望：MODEL_DIR = D:\新建文件夹 (3)\星芒\models（exe 同级）
assert str(asr_bootstrap.MODEL_DIR).lower() == r"D:\新建文件夹 (3)\星芒\models".lower(), "MODEL_DIR 不在 exe 同级!"
assert str(asr_bootstrap.LLAMA_DIR).lower() == r"D:\新建文件夹 (3)\星芒\third_party\llama".lower(), "LLAMA_DIR 不在 exe 同级!"
print("PATH_LOGIC_OK: frozen 下模型/服务器目录 = exe 同级（正确）")
