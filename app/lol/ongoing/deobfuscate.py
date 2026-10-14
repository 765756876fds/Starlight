# app/lol/ongoing/deobfuscate.py（新文件）
"""ChampSelect 选人阶段的 obfuscatedPuuid 还原。

背景：腾讯服在选人阶段对队友身份做 HIDDEN 混淆（nameVisibilityType="HIDDEN"），
只给 obfuscatedPuuid。该值 = 真 puuid 与固定 16 字节掩码逐字节 XOR。
XOR 可逆，本模块提供还原函数。

合规边界：只处理 LCU 已公开返回的 obfuscatedPuuid 字段，纯本地运算，
不涉及内存读取、不涉及注入、不涉及 hook。
"""
from __future__ import annotations

import re

_MASK = bytes([129, 112, 118, 169, 244, 81, 80, 155,
               149, 152, 104, 19, 206, 145, 23, 231])

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def deobfuscate_puuid(obfuscated: str) -> str:
    """还原 obfuscatedPuuid → 真 puuid。输入非法返回空字符串。

    纯本地 XOR，无副作用，无异常（所有异常路径返回 ""）。
    """
    if not obfuscated:
        return ""
    try:
        normalized = obfuscated.strip().lower()
        if not _UUID_RE.match(normalized):
            return ""
        src = bytes.fromhex(normalized.replace("-", ""))
        if len(src) != len(_MASK):
            return ""
        out = bytes(b ^ m for b, m in zip(src, _MASK))
        h = out.hex()
        result = f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        # 输出验证：version=4/5、合法 variant
        if result[14] not in ("4", "5"):
            return ""
        if result[19] not in ("8", "9", "a", "b"):
            return ""
        return result
    except Exception:
        return ""