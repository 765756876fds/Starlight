"""英雄目录：从 data/lol_data/champions.json 加载，失败回退内置最小集。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Champion:
    cid: int
    name_en: str
    name_zh: str
    aliases: tuple = field(default_factory=tuple)


# 内置 fallback（网络失败且无缓存时）
_BUILTIN: list[Champion] = [
    Champion(157, "Yasuo", "亚索", ("风男", "疾风剑豪")),
    Champion(22, "Ashe", "艾希", ("寒冰", "冰弓")),
    Champion(99, "Lux", "拉克丝", ("光辉", "光女")),
]

from app.core.path_resolve import PROJECT_ROOT

DEFAULT_DATA = PROJECT_ROOT / "data" / "lol_data" / "champions.json"

# 俗称 / 常见 ASR 误听 → 官方中文名（key 必须是 champions.json 里的 name）
_NAME_ALIASES: dict[str, tuple] = {
    # 上单 / 坦克
    "墨菲特": ("石头人", "大石头", "制图人"),
    "盖伦": ("大宝剑", "草丛伦", "德玛"),
    "嘉文四世": ("皇子",),
    "赵信": ("菊花信",),
    "易": ("剑圣",),
    "沃里克": ("狼人",),
    "雷克顿": ("鳄鱼",),
    "内瑟斯": ("狗头",),
    "德莱厄斯": ("诺手",),
    "莫德凯撒": ("铁男",),
    # 打野
    "李青": ("盲僧", "瞎子"),
    "卡兹克": ("螳螂",),
    "雷恩加尔": ("狮子狗",),
    "伊莉丝": ("蜘蛛",),
    "奈德丽": ("豹女",),
    "瑟庄妮": ("猪妹",),
    "孙悟空": ("猴子", "悟空",),
    "阿木木": ("木木",),
    # 中单 / AP
    "阿狸": ("狐狸",),
    "安妮": ("火女",),
    "维迦": ("小法",),
    "布兰德": ("火男",),
    "奥莉安娜": ("发条",),
    "乐芙兰": ("妖姬",),
    "拉克丝": ("光辉", "光女"),
    "崔斯特": ("卡牌",),
    "玛尔扎哈": ("蚂蚱",),
    "卡西奥佩娅": ("蛇女",),
    "瑞兹": ("流浪法师", "光头"),
    "凯尔": ("天使",),
    "莫甘娜": ("堕天使",),
    # ADC
    "艾希": ("寒冰", "冰弓", "寒冰射手"),
    "厄运小姐": ("女枪", "赏金", "赏金猎人"),
    "凯特琳": ("女警",),
    "崔丝塔娜": ("小炮",),
    "克格莫": ("大嘴",),
    "图奇": ("老鼠",),
    "薇恩": ("VN", "vn"),
    "伊泽瑞尔": ("EZ", "ez"),
    "希维尔": ("轮子妈",),
    "卢锡安": ("奥巴马",),
    "卡莎": ("卡萨",),
    "菲兹": ("小鱼人",),
    # 辅助
    "索拉卡": ("奶妈",),
    "迦娜": ("风女",),
    "娑娜": ("琴女",),
    "蕾欧娜": ("日女",),
    "阿利斯塔": ("牛头",),
    # 其他热门
    "亚索": ("风男", "疾风剑豪"),
    "锐雯": ("瑞文",),
    "菲奥娜": ("剑姬",),
    "艾瑞莉娅": ("刀妹",),
    "泰隆": ("男刀",),
    "凯南": ("电耗子",),
}


class ChampionCatalog:
    """英雄查询，支持按 id / 中文名 / 别名。"""

    def __init__(self, champions: list[Champion] | None = None):
        if champions is None:
            champions = self._load_default()
        self._by_id: dict[int, Champion] = {c.cid: c for c in champions}
        self._by_zh: dict[str, Champion] = {}
        for c in champions:
            self._by_zh[c.name_zh] = c
            if c.name_en:
                self._by_zh[c.name_en] = c
                self._by_zh[c.name_en.lower()] = c
            for a in c.aliases:
                self._by_zh[a] = c
            for a in _NAME_ALIASES.get(c.name_zh, ()):
                self._by_zh[a] = c

    @staticmethod
    def _load_default() -> list[Champion]:
        try:
            p = DEFAULT_DATA
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                out = []
                skipped = 0
                for c in data:
                    cid = int(c["id"])
                    # 跳过皮肤变体（id >= 60000），只保留标准英雄（id < 10000）
                    if cid >= 60000:
                        skipped += 1
                        continue
                    out.append(Champion(
                        cid=cid,
                        name_en=c.get("alias") or "",
                        name_zh=c.get("name") or c.get("title") or "",
                        aliases=tuple([c.get("title") or ""]),
                    ))
                print(f"[data] 加载 champions.json：{len(out)} 个英雄（跳过 {skipped} 个皮肤变体）")
                return out
        except Exception as e:
            print(f"[data] champions.json 加载失败：{e}")
        print("[data] 使用内置 fallback（仅 3 英雄），完整数据不可用")
        return list(_BUILTIN)

    def get_by_id(self, cid: int) -> Champion | None:
        return self._by_id.get(cid)

    def get_by_name(self, name: str) -> Champion | None:
        if not name:
            return None
        n = name.strip()
        if n in self._by_zh:
            return self._by_zh[n]
        # 模糊：子串匹配
        for k, v in self._by_zh.items():
            if k and len(k) >= 2 and k in n:
                return v
        return None

    def resolve_raw_champion_name(self, raw_name: str) -> Champion | None:
        """从 2999 rawChampionName（如 game_character_displayname_Hecarim）解析出 Champion。"""
        if not raw_name:
            return None
        # 提取 game_character_displayname_XXX 中的 XXX
        if raw_name.startswith("game_character_displayname_"):
            token = raw_name.replace("game_character_displayname_", "")
            return self.get_by_name(token)
        # 其他格式直接尝试
        return self.get_by_name(raw_name)

    def resolve_player_champion(self, champion_name: str, raw_champion_name: str = "") -> Champion | None:
        """从 2999 PlayerInfo 的 championName / rawChampionName 解析出 Champion。"""
        # 优先用 rawChampionName（英文名，更可靠）
        if raw_champion_name:
            c = self.resolve_raw_champion_name(raw_champion_name)
            if c:
                return c
        # 回退到 championName（中文称号/名字）
        if champion_name:
            return self.get_by_name(champion_name)
        return None

    def all(self) -> list[Champion]:
        return list(self._by_id.values())
