"""路径解析工具：外部可执行文件查找链（配置 → 自动搜索 → 降级）。
P0 路径自适应（2026-10-16）：llama-server 定位统一走这里；
后续 P1（lol_data 项目根统一）也可复用本模块模式。
"""
import glob
import os
import shutil


def find_llama_server(asr_config: dict) -> str:
    """llama-server 查找链：
    1. 配置 asr.llama_server（settings.yaml，空则不指定）
    2. PATH：shutil.which("llama-server")
    3. LM Studio 扩展目录：~/.lmstudio/extensions/backends/*/llama-server.exe（取最近安装）
    找不到返回 ""（调用方保持降级，不抛异常）。
    """
    path = ((asr_config or {}).get("llama_server") or "").strip()
    if path and os.path.isfile(path):
        return path
    found = shutil.which("llama-server")
    if found:
        return found
    cands = glob.glob(
        os.path.expanduser(r"~/.lmstudio/extensions/backends/*/llama-server.exe")
    )
    if cands:
        return max(cands, key=os.path.getmtime)
    return ""
