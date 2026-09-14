"""
Intent / 人话理解

将用户自然语言转换为结构化意图。
采用：规则匹配优先 -> Qwen Function Calling 兜底

AI 只负责"人话 -> 意图"，不直接控制电脑。
"""
import json
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

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
    规则匹配意图识别

    明确命令不走 LLM，直接用规则匹配。
    覆盖最常用的音乐控制和游戏提醒命令。
    """

    # 音乐控制规则
    MUSIC_RULES = [
        # 下一首
        (r"^(下一首|下一曲|换一首|切歌|切下一首|下一个|换歌|不好听换一个)$",
         CommandType.MUSIC_NEXT, {}),
        (r"(下一首|下一曲|换一首|切歌)", CommandType.MUSIC_NEXT, {}),
        # 上一首
        (r"^(上一首|上一曲|前一首|回到上一首)$", CommandType.MUSIC_PREV, {}),
        # 暂停
        (r"^(暂停|暂停一下|暂停音乐|停一下|停下|别放了)$",
         CommandType.MUSIC_PAUSE, {}),
        # 继续/播放
        (r"^(继续|继续播放|播放|放歌|开始播放|接着放)$",
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
        # 跳高潮
        (r"(高潮|跳到高潮|跳高潮|副歌)", CommandType.MUSIC_HIGHLIGHT, {}),
        # 重新扫描
        (r"(重新扫描|扫描音乐|更新音乐库)", CommandType.MUSIC_RESCAN, {}),
    ]

    # 游戏提醒规则
    ALARM_RULES = [
        # 闪现记录："对面XX闪现了" / "XX闪现了" / "帮我记一下XX闪现"
        (r"(?:对面|敌方|对面的|敌方的)?(.+?)(?:的)?闪现(?:了|用了|交了)",
         "alarm.flash", {"champion_group": 1}),
        (r"帮我记(?:一下|下)?(?:对面|敌方)?(.+?)闪现",
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
    ]

    # 中文数字映射
    CHINESE_NUMBERS = {
        "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "半": 0.5,
    }

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
                        params.pop("time_group", None)
                        params.pop("unit", None)
                    except (IndexError, ValueError):
                        pass

                return Intent(intent_type, confidence=0.95, params=params)

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
                # 排除已经被其他规则匹配的
                if query and not any(kw in query for kw in ["下一首", "暂停", "继续"]):
                    return Intent(
                        CommandType.MUSIC_SEARCH_PLAY,
                        confidence=0.9,
                        params={"query": query}
                    )

        return None


class LLMIntent:
    """
    LLM 意图识别（Qwen3 Function Calling）

    只在规则匹配失败时使用。
    输出结构化 JSON，不直接执行操作。
    """

    # 支持的意图列表（用于 prompt）
    SUPPORTED_INTENTS = {
        "music.next": "下一首/换一首",
        "music.previous": "上一首",
        "music.pause": "暂停播放",
        "music.resume": "继续播放",
        "music.volume_set": "设置音量，参数 volume (0-100)",
        "music.volume_up": "音量增加",
        "music.volume_down": "音量减少",
        "music.search_play": "搜索并播放，参数 query",
        "music.highlight": "跳到高潮部分",
        "alarm.flash": "记录闪现，参数 champion",
        "alarm.spell": "记录技能，参数 champion, spell, cooldown",
        "alarm.ward": "记录眼位，参数 location",
        "alarm.custom": "自定义计时器，参数 duration(秒), title",
        "voice.disable": "关闭语音识别",
        "tts.disable": "关闭语音播报",
        "system.unknown": "无法识别的意图",
    }

    def __init__(self, model_path: str, n_ctx: int = 4096,
                 n_threads: int = 6, temperature: float = 0.1):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.temperature = temperature
        self._model = None
        self._lock = threading.Lock()
        self._loaded = False

    def load(self):
        """加载模型"""
        if self._loaded:
            return
        try:
            from llama_cpp import Llama
            self._model = Llama(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                verbose=False,
            )
            self._loaded = True
            print(f"[Intent] Qwen3 模型已加载: {self.model_path}")
        except ImportError:
            print("[Intent] llama-cpp-python 未安装")
        except Exception as e:
            print(f"[Intent] 模型加载失败: {e}")

    def parse(self, text: str) -> Intent:
        """
        使用 LLM 解析意图

        Returns:
            结构化 Intent
        """
        if not self._loaded or not self._model:
            return Intent("system.unknown", confidence=0.0)

        with self._lock:
            try:
                # 构建 prompt
                intent_list = "\n".join(
                    f"- {k}: {v}" for k, v in self.SUPPORTED_INTENTS.items()
                )

                prompt = f"""你是一个意图识别助手。将用户的自然语言转换为结构化意图。

支持的意图类型：
{intent_list}

用户输入："{text}"

请输出 JSON 格式，包含以下字段：
- intent: 意图类型（必须是上面列出的类型之一）
- confidence: 置信度 (0.0-1.0)
- params: 参数对象

只输出 JSON，不要输出其他内容。"""

                response = self._model.create_chat_completion(
                    messages=[
                        {"role": "system", "content": "你是一个专业的意图识别助手，只输出 JSON。"},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=self.temperature,
                    max_tokens=256,
                    response_format={"type": "json_object"},
                )

                content = response["choices"][0]["message"]["content"].strip()

                # 解析 JSON
                try:
                    result = json.loads(content)
                    intent_type = result.get("intent", "system.unknown")
                    confidence = float(result.get("confidence", 0.5))
                    params = result.get("params", {})
                    return Intent(intent_type, confidence, params)
                except json.JSONDecodeError:
                    # 尝试从文本中提取 JSON
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)
                    if json_match:
                        try:
                            result = json.loads(json_match.group())
                            return Intent(
                                result.get("intent", "system.unknown"),
                                float(result.get("confidence", 0.5)),
                                result.get("params", {})
                            )
                        except json.JSONDecodeError:
                            pass
                    return Intent("system.unknown", confidence=0.0)

            except Exception as e:
                print(f"[Intent] LLM 解析失败: {e}")
                return Intent("system.unknown", confidence=0.0)

    def is_available(self) -> bool:
        return self._loaded and self._model is not None

    def unload(self):
        self._model = None
        self._loaded = False


class OpenAIAPIIntent:
    """
    OpenAI 兼容 API 意图识别（用于 LM Studio / Ollama）

    通过 HTTP API 调用本地模型，不需要 llama-cpp-python。
    默认启用 non-thinking（/no_think）模式。
    """

    SUPPORTED_INTENTS = LLMIntent.SUPPORTED_INTENTS

    # 允许的 Intent 白名单（安全校验）
    ALLOWED_INTENTS = set(SUPPORTED_INTENTS.keys()) | {"system.unknown"}

    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1",
                 model: str = "local-model",
                 api_key: str = "lm-studio",
                 temperature: float = 0.1,
                 enable_no_think: bool = True):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.enable_no_think = enable_no_think
        self._client = None
        self._loaded = False

    def load(self):
        """初始化 OpenAI 客户端"""
        try:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
            )
            # 测试连接
            try:
                models = self._client.models.list()
                if models.data:
                    self.model = models.data[0].id
                    print(f"[Intent] LM Studio 已连接，使用模型: {self.model}")
                self._loaded = True
            except Exception as e:
                print(f"[Intent] LM Studio 连接测试失败（模型可能未加载）: {e}")
                # 即使连接测试失败，也标记为已加载，运行时再试
                self._loaded = True
        except ImportError:
            print("[Intent] openai 库未安装")
        except Exception as e:
            print(f"[Intent] OpenAI API 初始化失败: {e}")

    def parse(self, text: str) -> Intent:
        """使用 API 解析意图"""
        if not self._loaded or not self._client:
            return Intent("system.unknown", confidence=0.0)

        try:
            intent_list = "\n".join(
                f"- {k}: {v}" for k, v in self.SUPPORTED_INTENTS.items()
            )

            # system prompt：non-thinking 模式
            sys_prompt = "你是一个专业的意图识别助手，只输出 JSON。"
            if self.enable_no_think:
                sys_prompt = "/no_think\n" + sys_prompt

            prompt = f"""将用户的自然语言转换为结构化意图。

支持的意图类型：
{intent_list}

用户输入："{text}"

请输出 JSON 格式，包含以下字段：
- intent: 意图类型（必须是上面列出的类型之一）
- confidence: 置信度 (0.0-1.0)
- params: 参数对象

只输出 JSON，不要输出其他内容。"""

            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=self.temperature,
                max_tokens=256,
            )

            content = response.choices[0].message.content.strip()

            # 解析 JSON
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        return Intent("system.unknown", confidence=0.0)
                else:
                    return Intent("system.unknown", confidence=0.0)

            # 安全校验：Intent 白名单
            intent_type = result.get("intent", "system.unknown")
            if intent_type not in self.ALLOWED_INTENTS:
                print(f"[Intent] 未知 Intent（安全拦截）: {intent_type}")
                return Intent("system.unknown", confidence=0.0)

            return Intent(
                intent_type,
                float(result.get("confidence", 0.5)),
                result.get("params", {})
            )

        except Exception as e:
            print(f"[Intent] API 解析失败: {e}")
            return Intent("system.unknown", confidence=0.0)

    def is_available(self) -> bool:
        return self._loaded and self._client is not None

    def unload(self):
        self._client = None
        self._loaded = False


class IntentRouter:
    """
    意图路由器

    处理流程：
    语音文本 -> 规则匹配 -> 明确命令？ -> 是 -> 直接执行
                                    -> 否 -> LLM -> 结构化意图 -> 执行

    支持多种 LLM 后端：
    - llama-cpp-python（直接加载 GGUF）
    - OpenAI 兼容 API（LM Studio / Ollama）
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.rule_based = RuleBasedIntent()
        self.llm_intent: Optional[LLMIntent] = None
        self.api_intent: Optional[OpenAIAPIIntent] = None
        self._llm_enabled = config.get("rule_based_first", True)

    def initialize(self):
        """初始化 LLM 意图识别（尝试多种后端）"""
        # 1. 尝试 llama-cpp-python 直接加载
        model_path = self.config.get("model_path", "")
        if model_path:
            try:
                import llama_cpp
                self.llm_intent = LLMIntent(
                    model_path=model_path,
                    n_ctx=self.config.get("n_ctx", 4096),
                    n_threads=self.config.get("n_threads", 6),
                    temperature=self.config.get("temperature", 0.1),
                )
                self.llm_intent.load()
                if self.llm_intent.is_available():
                    print("[Intent] 使用 llama-cpp-python 后端")
                    return
            except ImportError:
                print("[Intent] llama-cpp-python 不可用，尝试 API 后端")

        # 2. 尝试 OpenAI 兼容 API（LM Studio）
        api_config = self.config.get("openai_api", {})
        if api_config.get("enabled", True):
            self.api_intent = OpenAIAPIIntent(
                base_url=api_config.get("base_url", "http://127.0.0.1:1234/v1"),
                model=api_config.get("model", "local-model"),
                api_key=api_config.get("api_key", "lm-studio"),
                temperature=self.config.get("temperature", 0.1),
                enable_no_think=api_config.get("enable_no_think", True),
            )
            self.api_intent.load()
            if self.api_intent.is_available():
                print("[Intent] 使用 OpenAI API 后端（LM Studio）")

    def parse(self, text: str) -> Intent:
        """
        解析意图

        优先规则匹配，失败时用 LLM。
        """
        if not text or not text.strip():
            return Intent("system.unknown", confidence=0.0)

        # 1. 规则匹配
        intent = self.rule_based.match(text)
        if intent:
            return intent

        # 2. LLM 解析（优先 llama-cpp，其次 API）
        if self.llm_intent and self.llm_intent.is_available():
            intent = self.llm_intent.parse(text)
            if intent.confidence > 0.3:
                return intent

        if self.api_intent and self.api_intent.is_available():
            intent = self.api_intent.parse(text)
            if intent.confidence > 0.3:
                return intent

        return Intent("system.unknown", confidence=0.0)

    def unload(self):
        if self.llm_intent:
            self.llm_intent.unload()
        if self.api_intent:
            self.api_intent.unload()
