"""Overlay 资源管理器：下载和缓存英雄头像、召唤师技能图标。

资源来源：
- 英雄头像：Data Dragon Champion Portrait
- 召唤师技能图标：Data Dragon Summoner Spell Icon

缓存位置：
- data/assets/champions/{patch}/{name_en}.png
- data/assets/summoner_spells/{patch}/{name_en}.png
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap

from app.core.path_resolve import PROJECT_ROOT, USER_DATA_DIR

# Data Dragon 版本
_DDRAGON_VERSION = "16.18.1"
_DDRAGON_BASE = f"https://ddragon.leagueoflegends.com/cdn/{_DDRAGON_VERSION}/img"

# 缓存目录（R1: 用户可写资源缓存走 USER_DATA_DIR，Program Files 只读）
_CACHE_DIR = USER_DATA_DIR / "data" / "assets"
_CHAMPIONS_DIR = _CACHE_DIR / "champions" / _DDRAGON_VERSION
_SUMMONER_SPELLS_DIR = _CACHE_DIR / "summoner_spells" / _DDRAGON_VERSION

# 确保目录存在
_CHAMPIONS_DIR.mkdir(parents=True, exist_ok=True)
_SUMMONER_SPELLS_DIR.mkdir(parents=True, exist_ok=True)


def _download_file(url: str, cache_path: Path) -> bool:
    """下载文件到缓存路径。P0-A: 显式使用 certifi CA bundle，不依赖系统默认路径。"""
    if cache_path.exists():
        return True

    try:
        import ssl
        import urllib.request
        import certifi
        print(f"[Overlay Asset] 下载: {url}")
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(url, headers={"User-Agent": "Starlight/2.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            with open(cache_path, "wb") as f:
                f.write(resp.read())
        return cache_path.exists()
    except ssl.SSLError as e:
        print(f"[Overlay Asset] SSL 证书链错误（请检查网络/代理是否拦截 HTTPS）: {e}")
        return False
    except Exception as e:
        print(f"[Overlay Asset] 下载失败: {e}")
        return False


# 模块级内存缓存：原始 QPixmap 常驻内存，首次磁盘读后不再 IO
# 不清理：总量有限（~173 头像 + ~10 技能图标，合计几 MB）
# 失败不缓存：保持"下次可重试"的降级行为（调用方有占位图兜底）
_portrait_cache: dict[str, QPixmap | None] = {}
_spell_icon_cache: dict[str, QPixmap | None] = {}


def get_champion_portrait(name_en: str) -> QPixmap | None:
    """获取英雄头像（自动下载并缓存，内存缓存命中后无磁盘 IO）。"""
    if not name_en:
        return None
    if name_en in _portrait_cache:
        return _portrait_cache[name_en]
    
    # Data Dragon 英雄头像 URL
    url = f"{_DDRAGON_BASE}/champion/{name_en}.png"
    cache_file = _CHAMPIONS_DIR / f"{name_en}.png"
    
    if not cache_file.exists():
        if not _download_file(url, cache_file):
            return None
    
    pixmap = QPixmap(str(cache_file))
    if pixmap.isNull():
        return None
    
    # 裁剪成圆形头像（简单实现：裁剪成正方形）
    w = pixmap.width()
    h = pixmap.height()
    size = min(w, h)
    x = (w - size) // 2
    y = (h - size) // 2
    result = pixmap.copy(x, y, size, size)

    # 预乘 30% 透明度（替代 QGraphicsOpacityEffect——每帧创建 effect 是主线程重活）
    translucent = QPixmap(result.size())
    translucent.fill(Qt.transparent)
    p = QPainter(translucent)
    p.setOpacity(0.3)
    p.drawPixmap(0, 0, result)
    p.end()
    result = translucent

    _portrait_cache[name_en] = result
    return result


def get_summoner_spell_icon(name_en: str) -> QPixmap | None:
    """获取召唤师技能图标（自动下载并缓存，内存缓存命中后无磁盘 IO）。"""
    if not name_en:
        return None
    if name_en in _spell_icon_cache:
        return _spell_icon_cache[name_en]
    
    # 如果是惩戒，尝试多个变体
    name_variants = [name_en]
    if name_en.lower() == "smite":
        name_variants = _SMITE_VARIANTS
    
    for name in name_variants:
        # Data Dragon 召唤师技能 URL
        url = f"{_DDRAGON_BASE}/spell/Summoner{name}.png"
        cache_file = _SUMMONER_SPELLS_DIR / f"{name}.png"
        
        if not cache_file.exists():
            if not _download_file(url, cache_file):
                continue
        
        pixmap = QPixmap(str(cache_file))
        if not pixmap.isNull():
            _spell_icon_cache[name_en] = pixmap
            return pixmap
    
    return None


# 召唤师技能中文名 → 英文名映射（Data Dragon 内部名称）
_SPELL_ZH_TO_EN = {
    # 闪现
    "闪现": "Flash",
    "闪": "Flash",
    
    # 引燃 / 点燃
    "引燃": "Dot",
    "点燃": "Dot",
    "点火": "Dot",
    
    # 传送（普通 + 升级变体）
    "传送": "Teleport",
    "tp": "Teleport",
    "TP": "Teleport",
    "解封的传送": "Teleport",  # 升级后仍使用普通传送图标
    
    # 疾跑 / 幽灵疾步
    "疾跑": "Haste",
    "幽灵疾步": "Haste",
    
    # 治疗
    "治疗": "Heal",
    "治疗术": "Heal",
    "奶": "Heal",
    
    # 屏障
    "屏障": "Barrier",
    "护盾": "Barrier",
    
    # 虚弱
    "虚弱": "Exhaust",
    
    # 净化
    "净化": "Boost",
    
    # 惩戒（所有升级形态都映射到 Smite 图标）
    "惩戒": "Smite",
    "打野刀": "Smite",
    "原初惩戒": "Smite",
    "惩戒（深寒）": "Smite",
    "惩戒（灰烬）": "Smite",
    "解封的惩戒": "Smite",
    "深寒惩戒": "Smite",
    "灰烬惩戒": "Smite",
    "踏苔蜥幼苗": "Smite",  # 惩戒升级形态
    "蜂窝果实": "Smite",
    "海浪之爪": "Smite",
    "暗影印记": "Smite",
}

# 惩戒的变体（不同版本惩戒名称不同）
_SMITE_VARIANTS = ["Smite", "S11_Smite", "s11_smite", "5smite", "6smite", "7smite"]


# 召唤师技能 sid → 英文名映射（Data Dragon 内部名称）
_SPELL_SID_TO_EN = {
    4: "Flash",
    12: "Teleport",
    14: "Dot",      # 引燃
    21: "Barrier",
    1: "Boost",     # 净化
    3: "Exhaust",
    6: "Haste",     # 疾跑
    7: "Heal",
    11: "Smite",    # 惩戒
}


def get_summoner_spell_icon_by_sid(sid: int) -> QPixmap | None:
    """根据 sid 获取召唤师技能图标。"""
    name_en = _SPELL_SID_TO_EN.get(sid)
    if not name_en:
        return None
    return get_summoner_spell_icon(name_en)


def get_summoner_spell_icon_by_zh(name_zh: str) -> QPixmap | None:
    """根据中文名获取召唤师技能图标。"""
    name_en = _SPELL_ZH_TO_EN.get(name_zh)
    if not name_en:
        return None
    return get_summoner_spell_icon(name_en)

