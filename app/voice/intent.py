"""
Intent / 人话理解

将用户自然语言转换为结构化意图。
采用：RuleBasedIntent 规则匹配（纯确定性，无 LLM fallback）

AI 不参与意图识别。规则要求明确动词才产生写操作副作用，
裸陈述（如"对面莎弥拉闪现了"）返回 system.unknown。
"""
import re
from typing import Any, Dict, Optional

from ..core.command_bus import CommandType


class Intent:
    """结构化意图"""

    def __init__(self, intent_type: str, confidence: float = 1.0,
                 params: Optional[Dict[str, Any]] = None):
        self.intent_type = intent_type
        self.confidence = confidence
        self.params = params or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent_type,
            "confidence": self.confidence,
            "params": self.params,
        }

    def __repr__(self):
        return f"Intent({self.intent_type}, conf={self.confidence:.2f}, params={self.params})"


class RuleBasedIntent:
    """
    规则匹配意图识别（唯一意图来源）

    明确命令直接用规则匹配，无 LLM fallback。
    覆盖音乐控制、游戏提醒、冷却计时、玩家查询。
    写操作（alarm.* / cooldown.*）要求明确动词，裸陈述不命中。
    """

    # 音乐控制规则
    MUSIC_RULES = [
        # 下一首（同义表达扩充）
        (r"^(下一首|下一曲|换一首|切歌|切下一首|下一个|换歌|不好听换一个|"
         r"播放下一首|播放下一曲|播下一首|播下一曲|换一曲|换个歌|换首歌|换首|"
         r"切下一曲|下一首歌|下一首音乐|跳下一首|跳到下一首|这首换掉|"
         r"不听这首了|换首歌听|给我换一首|给我换首歌|换个音乐|换一首音乐)$",
         CommandType.MUSIC_NEXT, {}),
        # 上一首（同义表达扩充）
        (r"^(上一首|上一曲|前一首|回到上一首|播放上一首|播放上一曲|上一首歌|"
         r"上一曲音乐|返回上一首|回到上一首|切上一首|切回上一首|换回上一首|"
         r"播上一首|听上一首|我要上一首)$",
         CommandType.MUSIC_PREV, {}),
        # 暂停（同义表达扩充）
        (r"^(暂停|暂停一下|暂停音乐|停一下|停下|别放了|暂停播放|音乐暂停|"
         r"把音乐停一下|停一下音乐|先停一下|歌停一下|歌曲暂停|暂停这首|先暂停)$",
         CommandType.MUSIC_PAUSE, {}),
        # 继续/播放（同义表达扩充）
        (r"^(继续|继续播放|播放|放歌|开始播放|接着放|继续音乐|继续放歌|"
         r"接着播放|接着听|恢复播放|继续刚才的歌|把音乐继续起来|音乐继续)$",
         CommandType.MUSIC_RESUME, {}),
        # 切换播放/暂停
        (r"^(播放暂停|切换)$", CommandType.MUSIC_TOGGLE, {}),
        # 音量+
        (r"^(声音大一点|音量大一点|大声一点|声音大点|音量大点|大点声|调大音量)$",
         CommandType.MUSIC_VOLUME_UP, {}),
        (r"音量加(\d+)", CommandType.MUSIC_VOLUME_UP, {"step": 1}),
        # 音量-
        (r"^(声音小一点|音量小一点|小声一点|声音小点|音量小点|小点声|调小音量|静音)$",
         CommandType.MUSIC_VOLUME_DOWN, {}),
        (r"音量减(\d+)", CommandType.MUSIC_VOLUME_DOWN, {"step": 1}),
        # 设置音量
        (r"音量(\d+)", CommandType.MUSIC_VOLUME_SET, {}),
        # 高潮模式开关（必须在通用"高潮"之前）
        (r"(关闭高潮模式|退出高潮模式|高潮模式关|关掉高潮模式)$",
         CommandType.MUSIC_HIGHLIGHT_MODE, {"enabled": False}),
        (r"(打开高潮模式|开启高潮模式|高潮模式开|进入高潮模式)$",
         CommandType.MUSIC_HIGHLIGHT_MODE, {"enabled": True}),
        # 跳高潮（一次性）
        (r"(播放高潮|放高潮|听高潮|播放这首歌高潮|跳到高潮|跳高潮|高潮|副歌|播放高光|放高光|听高光)", CommandType.MUSIC_HIGHLIGHT, {}),
        # 播放顺序模式
        # sequential：顺序播放/连续播放/正常播放（播完停止）；repeat_all：列表/全部循环
        (r"(连续播放|顺序播放|正常播放|顺序模式)$",
         CommandType.MUSIC_SET_PLAY_MODE, {"mode": "sequential"}),
        (r"(列表循环|循环播放|全部循环)$",
         CommandType.MUSIC_SET_PLAY_MODE, {"mode": "repeat_all"}),
        (r"(随机播放|随机模式|随机曲|随机)$",
         CommandType.MUSIC_SET_PLAY_MODE, {"mode": "shuffle"}),
        (r"(单曲循环|单曲循环播放|循环这首|循环这首歌|就循环这一首)$",
         CommandType.MUSIC_SET_PLAY_MODE, {"mode": "repeat_one"}),
        # 重新扫描
        (r"(重新扫描|扫描音乐|更新音乐库)", CommandType.MUSIC_RESCAN, {}),
        # 清理失效歌曲
        (r"(清理音乐库|清理失效歌曲|清理坏歌|清除失效歌曲)", CommandType.MUSIC_CLEANUP, {}),
        # 清空队列（整句锚定；"清空计时"等含"计时"的表述不匹配，不会吞 COOLDOWN）
        (r"^(清空队列|清空播放列表|清除队列|清空列表|清空歌单)$",
         CommandType.MUSIC_CLEAR_QUEUE, {}),
        # 哼唱识曲（"播放XX"搜索规则不含这些词，无冲突；放在音乐规则末尾）
        (r"^(听听这是什么歌|听歌识曲|帮我找找这首歌|这是什么歌|识曲|帮我听听这是什么歌|"
         r"帮我识曲|听一下这是什么歌|帮我听一下这是什么歌|听歌识曲一下|识曲一下)$",
         CommandType.MUSIC_IDENTIFY, {}),
    ]

    # 搜索播放的"纯否定"query：意图是停止/取消，不是搜歌。
    # 仅在 query 完全等于这些词时跳过搜索规则（歌名如"不要说话"不受影响）。
    SEARCH_NEGATION_WORDS = {
        "停止", "停", "别", "不要", "不", "取消", "关", "关闭",
    }

    @staticmethod
    def _cn_episode_to_int(s):
        """语音集数：中文/阿拉伯数字 → int（"第五集"→5、"25集"→25）；不支持返回 None。"""
        if not s:
            return None
        if s.startswith("第"):
            s = s[1:]
        if s.isdigit():
            return int(s)
        digits = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
                  "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        total = 0
        n = 0
        for ch in s:
            if ch in digits:
                n = digits[ch]
            elif ch == "十":
                total += (n or 1) * 10
                n = 0
            elif ch == "百":
                total += (n or 1) * 100
                n = 0
            else:
                return None
        return total + n if (total or n) else None

    # 游戏提醒规则
    ALARM_RULES = [
        # 闪现记录：必须出现明确的"记"动作才创建 USER_MEMORY。
        # 裸陈述"对面XX闪现了"（无记忆动作）不自动创建，避免误判。
        (r"(?:帮我)?记(?:一下|下|住)?(?:对面|敌方)?(.+?)闪现(?:了|用了|交了)?",
         "alarm.flash", {"champion_group": 1}),
        (r"(?:对面|敌方)?(.+?)闪现(?:了|用了|交了)[，,]?\s*(?:帮我)?记(?:一下|下|住)?",
         "alarm.flash", {"champion_group": 1}),
        # 技能记录："XX W用了" / "XX大招用了"
        (r"(.+?)\s*([QqWwEeRr]|大招|闪现|点燃|治疗|净化|屏障|虚弱|疾跑|传送|惩戒)(?:用了|交了|放了)",
         "alarm.spell", {"champion_group": 1, "spell_group": 2}),
        # 眼位记录
        (r"(?:这里|这儿|这个位置|这个地方)(?:插了|放了|有)(?:个)?眼",
         "alarm.ward", {"location": "当前位置"}),
        (r"帮我记(?:一下|下)?(?:这里|这儿)的眼",
         "alarm.ward", {"location": "当前位置"}),
        # 自定义计时器（支持阿拉伯数字和中文数字）
        (r"([\d一二两三四五六七八九十半]+)(?:分钟|分)(?:后)?(?:提醒我|叫我)",
         "alarm.custom", {"time_group": 1, "unit": "minute"}),
        (r"([\d一二两三四五六七八九十半]+)(?:秒)(?:后)?(?:提醒我|叫我)",
         "alarm.custom", {"time_group": 1, "unit": "second"}),
        (r"([\d一二两三四五六七八九十半]+)(?:分钟|分)(?:后)?提醒",
         "alarm.custom", {"time_group": 1, "unit": "minute"}),
        # 查询/取消 临时记忆
        (r"(刚才|之前|刚刚)?记了(什么|哪些|啥|哪几个)",
         "alarm.list", {}),
        (r"(还有多久|还剩多久|还有多长时间|还有多少时间|什么时候提醒|几点提醒)",
         "alarm.remaining", {}),
        # "取消/删掉"分支：保持宽匹配（口语"取消刚才那个"宾语可省略）
        (r"(取消|删掉|取消掉)(刚才|之前|刚刚)?(那个|上一个|这个|那条)?(提醒|记录|计时)?",
         "alarm.cancel_last", {}),
        # "不要"分支：必须带宾语（提醒/记录/计时），避免"不要播放"等被吞成删提醒
        (r"不要(刚才|之前|刚刚)?(那个|上一个|这个|那条)?(提醒|记录|计时)",
         "alarm.cancel_last", {}),
    ]

    # 中文数字映射
    CHINESE_NUMBERS = {
        "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "半": 0.5,
    }

    # ===== Cooldown 手动计时器规则 =====
    COOLDOWN_RULES = [
        (r"取消所有(?:技能)?计时", "cooldown.cancel_all", {}),
        (r"取消(.+?)(?:计时|的计时)$", "cooldown.cancel", {"champion_group": 1}),
        # --- 静态 CD 查询（cooldown.query）：纯召唤师技能 ---
        (r"^(闪现|点燃|治疗|净化|屏障|虚弱|疾跑|传送|惩戒|tp|TP)\s*(?:的)?(?:冷却时间是多少秒|冷却时间是多少|冷却时间多少|冷却多久|冷却时间|冷却多少秒|冷却多少|冷却CD是多少秒|冷却CD多少秒|CD多久|cd多久|CD是多少|CD是多少秒|是多少秒|是多少|冷却|CD|cd|多久|多长|多少秒|几秒|多少|还有多久|还要多久|剩多久|剩多少秒|剩多少时间|什么时候好|什么时候转好|多久转好|好了没|好了吗|转好了吗|冷却好了吗|冷却好了没有|CD好了吗|CD还有多久|还在CD吗|还没好吗)$",
         "cooldown.query", {"skill_group": 1}),
        # --- 静态 CD 查询：指定英雄 / 召唤师技能 ---
        (r"^(?:对面|敌方)?(.+?)\s*(?:的)?\s*(闪现|点燃|治疗|净化|屏障|虚弱|疾跑|传送|惩戒|tp|TP)\s*(?:的)?(?:冷却时间是多少秒|冷却时间是多少|冷却时间多少|冷却多久|冷却时间|冷却多少秒|冷却多少|冷却CD是多少秒|冷却CD多少秒|CD多久|cd多久|CD是多少|CD是多少秒|是多少秒|是多少|冷却|CD|cd|多久|多长|多少秒|几秒|多少|还有多久|还要多久|剩多久|剩多少秒|剩多少时间|什么时候好|什么时候转好|多久转好|好了没|好了吗|转好了吗|冷却好了吗|冷却好了没有|CD好了吗|CD还有多久|还在CD吗|还没好吗)$",
         "cooldown.query", {"champion_group": 1, "skill_group": 2}),
        (r"^(?:对面|敌方)?(.+?)\s*(?:的)?\s*([QqWwEeRr]|大招)\s*(?:的)?(?:冷却时间是多少秒|冷却时间是多少|冷却时间多少|冷却多久|冷却时间|冷却多少秒|冷却多少|冷却CD是多少秒|冷却CD多少秒|CD多久|cd多久|CD是多少|CD是多少秒|是多少秒|是多少|冷却|CD|cd|多久|多长|多少秒|几秒|多少|还有多久|还要多久|剩多久|剩多少秒|剩多少时间|什么时候好|什么时候转好|多久转好|好了没|好了吗|转好了吗|冷却好了吗|冷却好了没有|CD好了吗|CD还有多久|还在CD吗|还没好吗)$",
         "cooldown.query", {"champion_group": 1, "skill_group": 2}),
        # 召唤师技能：必须出现闪现/点燃等关键词，允许 side 前缀
        # 排除"我记得"开头的陈述句
        (r"^(?!我记得|我想起来|我记得对面)(对面|敌方|我方|队友)?(.+?)(闪现|点燃|治疗|净化|屏障|虚弱|疾跑|传送|惩戒|tp|TP)"
         r"(?:交了|用了|放了|没了|好了|交闪了|把闪现交了|刚交闪|刚刚用了闪现|刚用了闪现|闪现已经用了|闪现没了|闪现进入冷却|了)$",
         "cooldown.start", {"champion_group": 2, "skill_group": 3, "side_group": 1}),
        # 口语化"交闪"单独匹配
        (r"^(对面|敌方|我方|队友)?(.+?)(?:交闪|交闪现|用闪|用了闪|刚交闪)(?:了)?$",
         "cooldown.start", {"champion_group": 2, "skill_group": 3, "side_group": 1, "skill_override": "闪现"}),
        # 动作在前、技能在后："石头人刚刚用了闪现"
        (r"^(对面|敌方|我方|队友)?(.+?)(?:刚刚)?(?:用了|交了|放了)(闪现|点燃|治疗|净化|屏障|虚弱|疾跑|传送|惩戒)$",
         "cooldown.start", {"champion_group": 2, "skill_group": 3, "side_group": 1}),
        # 英雄技能：必须有 side 前缀（对面/敌方/我方/队友），避免与旧 alarm.spell 冲突
        (r"^(对面|敌方|我方|队友)(?:的)?(.*?)([QqWwEeRr]|大招)"
         r"(?:交了|用了|放了|没了|好了)",
         "cooldown.start", {"champion_group": 2, "skill_group": 3, "side_group": 1}),
        # 英雄技能：无 side 前缀短指令（"莎弥拉 W 用了"/"石头人 R 了"）
        (r"^(?:对面|敌方|我方|队友)?(?:的)?(.+?)\s*(?:的)?\s*([QqWwEeRr]|大招)\s*(?:的)?(?:用了|交了|放了)$",
         "cooldown.start", {"champion_group": 1, "skill_group": 2}),
        (r"^(?:对面|敌方|我方|队友)?(?:的)?(.+?)\s*(?:的)?\s*([QqWwEeRr])\s*(?:的)?了$",
         "cooldown.start", {"champion_group": 1, "skill_group": 2}),
    ]

    @classmethod
    def _parse_number(cls, text: str) -> float:
        """解析数字（支持阿拉伯数字和中文数字）"""
        text = text.strip()
        # 纯阿拉伯数字
        if text.isdigit():
            return float(text)
        # 中文数字
        if text in cls.CHINESE_NUMBERS:
            return float(cls.CHINESE_NUMBERS[text])
        # 简单的中文数字组合（如 "十二"、"二十"）
        if "十" in text:
            parts = text.split("十")
            tens = cls.CHINESE_NUMBERS.get(parts[0], 1) if parts[0] else 1
            ones = cls.CHINESE_NUMBERS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return float(tens * 10 + ones)
        # 尝试直接转换
        try:
            return float(text)
        except ValueError:
            return 1.0  # 默认 1

    @classmethod
    def match(cls, text: str) -> Optional[Intent]:
        """
        规则匹配

        Returns:
            匹配到的 Intent，未匹配返回 None
        """
        text = text.strip()
        # 忽略所有标点：ASR 自带逗号/句号/问号等，出现在任意位置都不应影响匹配
        # （例：「高潮模式，开。」与「高潮模式开」应识别为同一指令）
        text = re.sub(r"[，。！？；：、,.;:!?\"'()\[\]{}<>《》【】「」『』（）〈〉…—～·\u3000]", "", text)
        # 确定性归一化（P2）：
        # 1) "R大招"/"W大招" 重复说法 -> 只保留技能字母
        text = re.sub(r'([QqWwEeRr])\s*大招', r'\1', text)
        # 2) 英雄名与技能字母之间的中文标点 -> 去掉（"石头人，R" -> "石头人R"）
        text = re.sub(r'([一-鿿])\s*[，,、]\s*([QqWwEeRr])', r'\1\2', text)
        # 句尾语气词清理：ASR 常带"下一首啊"/"暂停呗"等句尾语气词，strip 后不影响匹配
        # 只清句尾（"啊"若出现在句中不碰）；清完变空则保留原文（避免"啊"单独输入被清成空）
        _tail = text
        text = re.sub(r"[啊呢呀哦吧哈嘛噢呗啦哟诶]+$", "", text)
        if not text:
            text = _tail

        # 音乐控制
        for pattern, command, base_params in cls.MUSIC_RULES:
            match = re.search(pattern, text)
            if match:
                params = dict(base_params)
                # 提取分组参数
                if "step" in params and match.groups():
                    try:
                        params["step"] = int(match.group(1))
                    except (ValueError, IndexError):
                        pass
                if command == CommandType.MUSIC_VOLUME_SET and match.groups():
                    try:
                        params["volume"] = int(match.group(1))
                    except (ValueError, IndexError):
                        pass
                return Intent(command, confidence=0.99, params=params)

        # ===== Cooldown 手动计时器（优先于 alarm 规则，避免被旧规则吞掉） =====
        for pattern, intent_type, base_params in cls.COOLDOWN_RULES:
            match = re.search(pattern, text)
            if match:
                params = {}
                cg = base_params.get("champion_group")
                sg = base_params.get("skill_group")
                sdg = base_params.get("side_group")
                try:
                    if cg:
                        v = match.group(int(cg)).strip() if match.group(int(cg)) else ""
                        v = re.sub(r"^(对面|敌方|对面的|敌方的|我方|队友|的)", "", v)
                        v = v.strip("，。！？、,.!?;： ")
                        params["champion"] = v or None
                    if sg:
                        params["skill"] = match.group(int(sg)).strip()
                    if sdg:
                        s = match.group(int(sdg)) or "敌方"
                        params["side"] = {"对面": "enemy", "敌方": "enemy",
                                          "我方": "ally", "队友": "ally"}.get(s, "enemy")
                except (IndexError, ValueError):
                    pass
                return Intent(intent_type, confidence=0.95, params=params)

        # 游戏提醒
        for pattern, intent_type, base_params in cls.ALARM_RULES:
            match = re.search(pattern, text)
            if match:
                params = dict(base_params)
                # 提取英雄名
                champion_group = base_params.get("champion_group")
                if champion_group and match.groups():
                    try:
                        idx = int(champion_group) if isinstance(champion_group, str) and champion_group.isdigit() else 1
                        champion = match.group(idx).strip()
                        # 清理常见前缀
                        champion = re.sub(r"^(对面|敌方|对面的|敌方的|的)", "", champion)
                        params["champion"] = champion
                        params.pop("champion_group", None)
                    except (IndexError, ValueError):
                        pass

                # 提取技能名
                spell_group = base_params.get("spell_group")
                if spell_group and match.groups():
                    try:
                        idx = int(spell_group) if isinstance(spell_group, str) and spell_group.isdigit() else 2
                        params["spell"] = match.group(idx).strip()
                        params.pop("spell_group", None)
                    except (IndexError, ValueError):
                        pass

                # 提取时间
                time_group = base_params.get("time_group")
                if time_group and match.groups():
                    try:
                        idx = int(time_group) if isinstance(time_group, str) and time_group.isdigit() else 1
                        raw_value = match.group(idx).strip()
                        value = cls._parse_number(raw_value)
                        unit = base_params.get("unit", "minute")
                        params["duration"] = value * 60 if unit == "minute" else value
                        params["title"] = f"{raw_value}{'分钟' if unit == 'minute' else '秒'}提醒"
                        # 提取"提醒我/叫我"后面的宾语作为 content（如"喝水"），到期播报它
                        tail = match.string[match.end():].strip("，。,. ")
                        if tail:
                            params["content"] = tail
                        params.pop("time_group", None)
                        params.pop("unit", None)
                    except (IndexError, ValueError):
                        pass

                return Intent(intent_type, confidence=0.95, params=params)

        # 电影搜索播放（包2b）：显式带"电影/影片/片子"才走电影；裸"播放XX"保持音乐。
        # 必须在 search_patterns 之前：否则"播放电影流浪地球"会被 ^播放(.+)$ 截成音乐搜索。
        # 包2c：第一条带集数（"第X集/第X话/X集"，中文+阿拉伯数字）→ params["episode"] 直跳；
        # 第二条无集数 → 播第一集（现状不变）。
        movie_patterns = [
            r"^(?:播放|放|看)(?:一部)?(电影|影片|片子)(.+?)(?:第)?([0-9零一二两三四五六七八九十百]+)(?:集|话|期)$",
            r"^(?:播放|放|看)(?:一部)?(电影|影片|片子)(.+)$",
        ]
        for pattern in movie_patterns:
            match = re.match(pattern, text)
            if match:
                query = match.group(2).strip()
                if query and query not in cls.SEARCH_NEGATION_WORDS:
                    params = {"query": query}
                    if len(match.groups()) > 2 and match.group(3):
                        episode = cls._cn_episode_to_int(match.group(3))
                        if episode is not None:
                            params["episode"] = episode
                    return Intent(
                        CommandType.MOVIE_SEARCH_PLAY,
                        confidence=0.9,
                        params=params,
                    )

        # 搜索播放："播放XXX" / "来一首XXX" / "放点XXX"
        search_patterns = [
            r"^播放(.+)$",
            r"^来一首(.+)$",
            r"^来首(.+)$",
            r"^放点(.+)$",
            r"^放一首(.+)$",
            r"^听(.+)$",
            r"^我想听(.+)$",
        ]
        for pattern in search_patterns:
            match = re.match(pattern, text)
            if match:
                query = match.group(1).strip()
                # "停止播放"/"播放停止" → query="停止"：意图是停止，跳过搜歌（最终 unknown）
                if query in cls.SEARCH_NEGATION_WORDS:
                    continue
                # 排除已经被其他规则匹配的
                if query and not any(kw in query for kw in ["下一首", "暂停", "继续"]):
                    return Intent(
                        CommandType.MUSIC_SEARCH_PLAY,
                        confidence=0.9,
                        params={"query": query}
                    )

        # ===== Player Query 规则（仅当前局实时查询，历史战绩已下线）=====
        # 用子串包含匹配，容忍 ASR 句尾语气词（啊/呢/呀）和问号。
        # 已移除：经济差/谁最肥（2999 无敌方金币，数据不可用）
        if any(k in text for k in ("几杀几死", "我现在战绩", "现在杀了多少", "我现在几杀")):
            return Intent("player.query", confidence=0.95,
                          params={"query_type": "current_self_kda"})
        # 对面都有谁
        if any(k in text for k in ("对面都有谁", "敌方都有谁", "对面有哪些人", "对面都有哪些人")):
            return Intent("player.query", confidence=0.95,
                          params={"query_type": "enemy_roster"})
        # 谁死得最多
        if any(k in text for k in ("谁死得最多", "谁死亡最多", "现在谁死得最多", "谁死的最多")):
            return Intent("player.query", confidence=0.95,
                          params={"query_type": "most_deaths"})
        # 我几级
        if any(k in text for k in ("我几级", "我现在多少级", "我当前等级", "我多少级")):
            return Intent("player.query", confidence=0.95,
                          params={"query_type": "current_self_level"})
        # 某英雄几级 / 某英雄战绩
        # 匹配 "XX几级" / "XX战绩" 模式
        # 英雄几级
        m = re.match(r'.*?([\u4e00-\u9fa5A-Za-z]{1,10})(几级|多少级|多少等级).*', text)
        if m and "我" not in m.group(1):
            champ_name = m.group(1)
            return Intent("player.query", confidence=0.9,
                          params={"query_type": "champion_level", "champion_name": champ_name})
        # 英雄战绩
        m = re.match(r'.*?([\u4e00-\u9fa5A-Za-z]{1,10})(战绩怎么样|战绩|几杀几死|几杀).*', text)
        if m and "我" not in m.group(1):
            champ_name = m.group(1)
            return Intent("player.query", confidence=0.9,
                          params={"query_type": "champion_kda", "champion_name": champ_name})
        return None


class IntentRouter:
    """
    意图路由器（纯规则版）

    处理流程：
    语音文本 -> RuleBasedIntent.match -> 命中：返回 Intent
                                    -> 未命中：返回 system.unknown

    无 LLM fallback。写操作（alarm.* / cooldown.*）只由规则产生，
    规则要求明确动词（"记一下"/"用了"/"插了"/"提醒我"），
    裸陈述（如"对面莎弥拉闪现了"）不会产生任何副作用。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.rule_based = RuleBasedIntent()

    def parse(self, text: str) -> Intent:
        """
        解析意图：规则命中返回 Intent，否则返回 system.unknown。
        """
        if not text or not text.strip():
            return Intent("system.unknown", confidence=0.0)

        intent = self.rule_based.match(text)
        if intent:
            return intent

        return Intent("system.unknown", confidence=0.0)




