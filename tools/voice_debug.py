"""voice_debug.py — 语音链路旁路记录器（只读诊断，不改任何业务代码）

用途
    以子进程方式启动项目主程序，捕获全部控制台输出，实时把
    「你说的话 → ASR 识别文本 → 意图结果 → 命令执行 → 音乐/播放动作」
    整理成带时间戳的时间线，控制台高亮显示并全量落盘。

用法
    py -3.11 tools/voice_debug.py          # 普通启动
    或双击 tools/start_voice_debug.bat

    退出：Ctrl+C（会同时关闭被启动的主程序）

落盘
    data/voice_debug/voice_trace_<时间戳>.log  （UTF-8，每条带时间戳+分类标签）

排查提示
    若启动后一直没有任何「识别」行，按下面顺序看日志：
    1. 有没有 [Voice] 麦克风已开启 / 正在监听 —— 没有则麦克风没开（点状态栏 🎤 按钮）
    2. 有没有 [ASR] 连接成功 —— 没有则 ASR 服务没起来
    3. 识别到了但 [Voice] 意图: Intent(system.unknown) —— 规则没匹配上
    4. 意图对了但没有 [PERF] i=xxx —— 命令没执行
    5. 执行了但没有 [mpv] cmd loadfile —— 播放链路问题（看 [Music] 行）
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")
OUT_DIR = os.path.join(ROOT, "data", "voice_debug")

# ---------- ANSI 彩色输出（Windows 10+ 终端可用；不支持则自动降级纯文本） ----------
_ANSI = True
try:
    import ctypes
    if os.name == "nt":
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
except Exception:
    _ANSI = False


def _c(s: str, code: str) -> str:
    if not _ANSI or not sys.stdout.isatty():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def full_ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# ---------- 事件分类（按顺序匹配，ERROR 放较后避免误标） ----------
RULES = [
    ("启动",  r"System started!|初始化完成|正在启动系统", "1;36"),
    ("麦克风", r"\[Voice\] 麦克风", "35"),
    ("ASR",   r"\[ASR\]", "36"),
    ("识别",  r"\[Voice\] 识别: ", "36"),
    ("意图",  r"\[Voice\] 意图: ", "33"),
    ("执行",  r"\[PERF\]", "32"),
    ("mpv",   r"\[mpv\]", "34"),
    ("音乐",  r"\[Music\]", "32"),
    ("UI",    r"\[UI\]", "34"),
    ("错误",  r"error|Error|失败|异常|exception|Exception|Traceback", "1;31"),
    ("其他",  r"", ""),
]
_COMPILED = [(name, re.compile(pat), color) for name, pat, color in RULES]


def classify(line: str):
    for name, rx, color in _COMPILED:
        if rx.search(line):
            return name, color
    return "其他", ""


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    log_path = os.path.join(OUT_DIR, f"voice_trace_{full_ts()}.log")
    print(_c(f"[voice_debug] 记录文件: {log_path}", "1;36"))
    print(_c("[voice_debug] 正在启动主程序（请稍候，出现「System started!」即可开始说话）...", "36"))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # 强制主程序 print 即时刷出，保证实时性
    proc = subprocess.Popen(
        [sys.executable, MAIN],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stats = {"识别": 0, "意图": 0, "执行": 0, "错误": 0, "unknown": 0}
    last_voice_at = None
    mic_seen = False
    started_at = time.time()
    lock = threading.Lock()

    def watchdog():
        """静默检测：长时间没有任何识别/意图时给出方向提示（不阻塞主线程）。"""
        while True:
            time.sleep(20)
            now = time.time()
            with lock:
                last = last_voice_at
            if now - started_at < 45:
                continue
            idle = (now - last) if last is not None else now - started_at
            if idle >= 60:
                print(_c("[voice_debug] ⚠ 已 60 秒没有任何识别。请检查："
                         "①日志里有没有 [Voice] 麦克风已开启/正在监听（没有就点状态栏 🎤 按钮）；"
                         "②有没有 [ASR] 连接成功（没有则 ASR 服务未就绪）。", "1;33"))

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            name, color = classify(line)
            with lock:
                if name == "识别":
                    stats["识别"] += 1
                    last_voice_at = time.time()
                elif name == "意图":
                    stats["意图"] += 1
                    if "system.unknown" in line:
                        stats["unknown"] += 1
                elif name == "执行":
                    stats["执行"] += 1
                elif name == "错误":
                    stats["错误"] += 1
                elif name == "麦克风":
                    if "已开启" in line or "正在监听" in line:
                        mic_seen = True
            stamp = ts()
            # 落盘（UTF-8）
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} [{name}] {line}\n")
            # 控制台：关键行彩色高亮，其余原样转发
            if color:
                print(_c(line, color))
            else:
                print(line)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print()
    print(_c("[voice_debug] 已停止。统计：", "1;36"))
    print(f"  识别文本 {stats['识别']} 条 | 意图 {stats['意图']} 条（其中 system.unknown {stats['unknown']} 条）"
          f" | 命令执行 {stats['执行']} 条 | 错误行 {stats['错误']} 条")
    if mic_seen:
        mic_state = "是"
    else:
        mic_state = "否 —— 若你没有点过 🎤 按钮，这就是「语音没反应」的最大嫌疑"
    print(f"  麦克风曾开启: {mic_state}")
    print(f"  完整时间线: {log_path}")


if __name__ == "__main__":
    main()
