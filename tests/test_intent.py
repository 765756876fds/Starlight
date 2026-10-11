"""
Intent 模块测试：规则匹配（纯规则版，无 LLM fallback）
"""
import pytest

from app.voice.intent import RuleBasedIntent, Intent, IntentRouter


class TestRuleBasedIntent:
    """规则匹配 Intent 测试"""

    def test_next_song(self):
        intent = RuleBasedIntent.match("下一首")
        assert intent is not None
        assert intent.intent_type == "music.next"

    def test_punctuation_ignored_everywhere(self):
        """ASR 自带标点：指令中任意位置的标点都不影响识别。"""
        cases = [
            ("高潮模式，开。", "music.highlight_mode", True),
            ("高潮模式开，", "music.highlight_mode", True),
            ("随机播放。", "music.set_play_mode", "shuffle"),
            ("下一首！", "music.next", None),
            ("暂停，一下。", "music.pause", None),
            ("顺序模式？", "music.set_play_mode", "sequential"),
        ]
        for text, expected_type, expected_param in cases:
            intent = RuleBasedIntent.match(text)
            assert intent is not None, f"{text!r} 应被识别"
            assert intent.intent_type == expected_type, f"{text!r} -> {intent.intent_type}"
            if expected_param is not None:
                if isinstance(expected_param, bool):
                    assert intent.params.get("enabled") is expected_param, text
                else:
                    assert intent.params.get("mode") == expected_param, text

    def test_next_song_alternate(self):
        intent = RuleBasedIntent.match("换一首")
        assert intent is not None
        assert intent.intent_type == "music.next"

    def test_prev_song(self):
        intent = RuleBasedIntent.match("上一首")
        assert intent is not None
        assert intent.intent_type == "music.previous"

    def test_pause(self):
        intent = RuleBasedIntent.match("暂停")
        assert intent is not None
        assert intent.intent_type == "music.pause"

    def test_pause_variant(self):
        intent = RuleBasedIntent.match("暂停音乐")
        assert intent is not None
        assert intent.intent_type == "music.pause"

    def test_resume(self):
        intent = RuleBasedIntent.match("继续播放")
        assert intent is not None
        assert intent.intent_type == "music.resume"

    def test_volume_up(self):
        intent = RuleBasedIntent.match("音量大一点")
        assert intent is not None
        assert intent.intent_type == "music.volume_up"

    def test_volume_down(self):
        intent = RuleBasedIntent.match("音量小一点")
        assert intent is not None
        assert intent.intent_type == "music.volume_down"

    def test_custom_alarm_minutes(self):
        intent = RuleBasedIntent.match("两分钟后提醒我")
        assert intent is not None
        assert intent.intent_type == "alarm.custom"
        assert "duration" in intent.params
        assert intent.params["duration"] == 120.0  # 2分钟 = 120秒

    def test_custom_alarm_seconds(self):
        intent = RuleBasedIntent.match("三十秒后提醒我")
        assert intent is not None
        assert intent.intent_type == "alarm.custom"
        assert intent.params["duration"] == 30.0

    def test_ward_record(self):
        intent = RuleBasedIntent.match("这里插了个眼")
        assert intent is not None
        assert intent.intent_type == "alarm.ward"

    def test_search_play(self):
        intent = RuleBasedIntent.match("播放周杰伦的歌")
        assert intent is not None
        assert intent.intent_type == "music.search_play"
        assert "query" in intent.params

    def test_unknown_command(self):
        intent = RuleBasedIntent.match("今天天气怎么样")
        assert intent is None or intent.intent_type == "system.unknown"

    def test_rule_high_confidence(self):
        """规则匹配为高置信度确定性命中"""
        intent = RuleBasedIntent.match("下一首")
        assert intent.confidence > 0.95


class TestAlarmSafetyBoundary:
    """
    Alarm/Cooldown 写操作安全边界（从旧 test_fallback_safety_gate.py 迁移 + 演进）。

    纯规则系统下，Rule 本身就是安全边界：
    - 裸陈述（无明确动作动词）→ 不产生 alarm 副作用
    - 明确动作（"记一下"/"用了"/"插了"/"提醒我"）→ 才产生写操作
    - 交闪报告（"对面XX闪现了"）已演进为 cooldown.start（敌方召唤师技能 CD 计时），
      由 COOLDOWN_RULES 优先拦截；alarm.flash 保留但只在回忆/非交闪场景可达。
    不再有 LLM fallback 来"补全"意图。
    """

    def test_bare_flash_report_starts_cooldown(self):
        """裸交闪报告"对面莎弥拉闪现了" → cooldown.start（敌方交闪 CD 计时，非 alarm）"""
        intent = RuleBasedIntent.match("对面莎弥拉闪现了")
        assert intent is not None
        assert intent.intent_type == "cooldown.start"
        assert intent.params.get("champion") == "莎弥拉"
        assert intent.params.get("skill") == "闪现"
        assert intent.params.get("side") == "enemy"

    def test_bare_flash_report_via_router(self):
        """通过 IntentRouter 走完整 parse 链路：交闪报告 → cooldown.start"""
        router = IntentRouter({})
        result = router.parse("对面莎弥拉闪现了")
        assert result.intent_type == "cooldown.start"
        assert result.params.get("side") == "enemy"

    def test_explicit_record_flash_starts_cooldown(self):
        """明确"帮我记一下对面莎弥拉闪现了" → cooldown.start（记录交闪=启动计时）"""
        intent = RuleBasedIntent.match("帮我记一下对面莎弥拉闪现了")
        assert intent is not None
        assert intent.intent_type == "cooldown.start"
        assert "莎弥拉" in intent.params.get("champion", "")

    def test_bare_spell_statement_no_alarm(self):
        """裸技能陈述不通过 alarm.spell 分支（cooldown 优先或 unknown）"""
        intent = RuleBasedIntent.match("莎弥拉W用了")
        # 实际命中 cooldown.start（cooldown 规则优先），不是 alarm.spell 误触发
        assert intent is not None
        assert intent.intent_type != "alarm.spell"

    def test_explicit_ward_creates_alarm(self):
        """明确"插了个眼" → alarm.ward"""
        intent = RuleBasedIntent.match("这里插了个眼")
        assert intent is not None
        assert intent.intent_type == "alarm.ward"

    def test_explicit_custom_timer(self):
        """明确"N分钟后提醒我" → alarm.custom"""
        intent = RuleBasedIntent.match("两分钟后提醒我喝水")
        assert intent is not None
        assert intent.intent_type == "alarm.custom"
        assert intent.params.get("content") == "喝水"

    def test_router_no_llm_fallback(self):
        """IntentRouter 未命中规则时直接返回 system.unknown，无 LLM fallback"""
        router = IntentRouter({})
        result = router.parse("随便说点什么胡言乱语")
        assert result.intent_type == "system.unknown"
        assert result.confidence == 0.0

    def test_empty_text_unknown(self):
        """空文本 → system.unknown"""
        router = IntentRouter({})
        assert router.parse("").intent_type == "system.unknown"
        assert router.parse("   ").intent_type == "system.unknown"


class TestPunctuationNormalization:
    """ASR 句末标点归一化测试"""

    @pytest.mark.parametrize("text", [
        "打开高潮模式",
        "打开高潮模式。",
        "打开高潮模式！",
        "打开高潮模式？",
    ])
    def test_highlight_mode_on(self, text):
        intent = RuleBasedIntent.match(text)
        assert intent is not None
        assert intent.intent_type == "music.highlight_mode"
        assert intent.params.get("enabled") is True

    def test_highlight_mode_off(self):
        intent = RuleBasedIntent.match("关闭高潮模式。")
        assert intent is not None
        assert intent.intent_type == "music.highlight_mode"
        assert intent.params.get("enabled") is False

    @pytest.mark.parametrize("text", [
        "随机播放。",
        "随机播放！",
    ])
    def test_shuffle(self, text):
        intent = RuleBasedIntent.match(text)
        assert intent is not None
        assert intent.intent_type == "music.set_play_mode"
        assert intent.params.get("mode") == "shuffle"

    def test_repeat_one(self):
        intent = RuleBasedIntent.match("单曲循环。")
        assert intent is not None
        assert intent.intent_type == "music.set_play_mode"
        assert intent.params.get("mode") == "repeat_one"

    def test_sequential(self):
        intent = RuleBasedIntent.match("连续播放。")
        assert intent is not None
        assert intent.intent_type == "music.set_play_mode"
        assert intent.params.get("mode") == "sequential"

    def test_next_with_punct(self):
        intent = RuleBasedIntent.match("下一首。")
        assert intent is not None
        assert intent.intent_type == "music.next"
