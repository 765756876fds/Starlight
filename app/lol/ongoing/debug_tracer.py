"""对局加载全链路 X 光追踪器（毫秒级日志）。

用途：定位"进游戏 0:00 名字 ? / 战绩获取中"加载慢问题。
所有对局加载关键动作、时间戳、线程名、变量状态输出到 logs/ongoing_debug.log。

开关：_DEBUG_ONGOING = True（默认开，方便收集）；修复后一键改 False 即可关闭，
不删除任何调试代码。
"""
from __future__ import annotations

import logging
import os

# 全局调试开关：True=写 logs/ongoing_debug.log；False=静默（零开销跳过）
_DEBUG_ONGOING = True


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("OngoingTrace")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # 不冒泡到根 logger，避免与控制台日志重复
    if _DEBUG_ONGOING:
        # logs/ 位于项目根（app/../.. 相对本模块上溯三层）
        log_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "logs")
        )
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(
            os.path.join(log_dir, "ongoing_debug.log"), encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(fh)
    return logger


ongoing_logger = _build_logger()
