"""LoL 前台窗口检测（Windows 专用）。

只认游戏本体进程 League of Legends.exe：
- 连招只在对局中有用（依赖 Ongoing 英雄识别），大厅/客户端前台时无英雄，
  认客户端无意义。
- 前台检测失败时抛异常，由调用方决定放行/拦截策略
  （ComboManager 策略：失败放行 + 打日志，避免"游戏里按了没反应"）。

实现：GetForegroundWindow → GetWindowThreadProcessId → psutil 查进程名。
ctypes 为标准库；psutil 已是项目依赖（app/lol/lcu.py 在用）。
"""

_GAME_PROCESS_NAMES = ("League of Legends.exe",)


def is_lol_foreground() -> bool:
    """前台窗口是否属于 LoL 游戏本体。

    Returns:
        True：前台窗口是 League of Legends.exe。
        False：前台是其他窗口（或不是游戏本体）。
    Raises:
        OSError：前台窗口/进程查询失败（调用方负责降级策略）。
    """
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    # [诊断] 逐环节打印，定位实机"LoL 前台被误判 False"的原因
    print(f"[Combo][focus] hwnd={hwnd:#x}")
    if not hwnd:
        print("[Combo][focus] hwnd=0 → OSError（无前台窗口）")
        raise OSError("GetForegroundWindow 返回空句柄（无前台窗口）")
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    print(f"[Combo][focus] pid={pid.value}")
    if not pid.value:
        print("[Combo][focus] pid=0 → OSError")
        raise OSError("GetWindowThreadProcessId 返回空 PID")

    import psutil

    try:
        proc = psutil.Process(pid.value)
        name = proc.name()
        print(f"[Combo][focus] proc_name={name!r}")
        matched = name in _GAME_PROCESS_NAMES
        print(f"[Combo][focus] match={matched} (target={_GAME_PROCESS_NAMES})")
        return matched
    except psutil.Error as e:
        print(f"[Combo][focus] psutil error={e!r} → OSError")
        raise OSError(f"前台进程 {pid.value} 查询失败: {e}") from None
