"""
Intent 模块测试：规则匹配（纯规则版，无 LLM fallback）
"""
import pytest

pytestmark = pytest.mark.unit

import pytest

from app.voice.intent import RuleBasedIntent, Intent, IntentRouter


class TestRuleBasedIntent:
    """规则匹配 Intent 测试"""

    def test_next_song(self):
        intent = RuleBasedIntent.match("下一首")
        assert intent is not None
        assert intent.intent_type == "music.next"

    @pytest.mark.parametrize("text, enabled", [
        ("打开高潮模式", True),
        ("打开高潮模式啊", True),  # 轻微 ASR/语气词偏差
        ("高潮模式关", False),
    ])
    def test_highlight_mode_keeps_explicit_on_off(self, text, enabled):
        intent = RuleBasedIntent.match(text)
        assert intent is not None
        assert intent.intent_type == "music.highlight_mode"
        assert intent.params["enabled"] is enabled

    @pytest.mark.parametrize("text", [
        "别打开高潮模式",
        "我刚才听到有人说打开高潮模式",
        "今天播放列表挺好听的",
    ])
    def test_highlight_mode_does_not_trigger_on_negation_or_chat(self, text):
        intent = RuleBasedIntent.match(text)
        assert intent is None or intent.intent_type != "music.highlight_mode"

    @pytest.mark.parametrize("text, expected", [
        ("重新扫描", "music.rescan"),
        ("更新音乐库啊", "music.rescan"),
        ("清理音乐库", "music.cleanup"),
        ("清除失效歌曲。", "music.cleanup"),
    ])
    def test_music_maintenance_commands_keep_explicit_scope(self, text, expected):
        intent = RuleBasedIntent.match(text)
        assert intent is not None
        assert intent.intent_type == expected

    @pytest.mark.parametrize("text", [
        "别重新扫描音乐库",
        "我刚才听说重新扫描音乐库",
        "不要清理音乐库",
        "有人说清理失效歌曲",
    ])
    def test_music_maintenance_negation_and_quotation_do_not_trigger(self, text):
        intent = RuleBasedIntent.match(text)
        assert intent is None or intent.intent_type not in {
            "music.rescan", "music.cleanup"
        }

    @pytest.mark.parametrize("text, command, enabled", [
        ("打开自动排队", "auto.queue_set", True),
        ("关闭自动确认", "auto.accept_set", False),
        ("开启自动禁用", "auto.banpick_set", True),
        ("关掉自动连招", "auto.combo_set", False),
    ])
    def test_game_automation_requires_explicit_switch(self, text, command, enabled):
        intent = RuleBasedIntent.match(text)
        assert intent and intent.intent_type == command
        assert intent.params["enabled"] is enabled

    @pytest.mark.parametrize("text", [
        "别打开自动排队", "我刚才听说打开自动确认",
        "自动连招怎么样", "有人说关闭自动禁用",
    ])
    def test_game_automation_negation_and_chat_do_not_trigger(self, text):
        intent = RuleBasedIntent.match(text)
        assert intent is None or not intent.intent_type.startswith("auto.")

    def test_sleep_timer_parses_minutes_and_hours(self):
        minute = RuleBasedIntent.match("设置睡眠定时30分钟")
        hour = RuleBasedIntent.match("睡眠定时1小时")
        assert minute.params["seconds"] == 1800
        assert hour.params["seconds"] == 3600

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

    @pytest.mark.parametrize("text, expected", [
        ("播放音乐", "music.play"),
        ("暂停歌曲", "music.pause"),
        ("继续放歌", "music.resume"),
        ("停止音乐", "music.stop"),
        ("切歌", "music.next"),
        ("上一首歌", "music.previous"),
    ])
    def test_music_action_object_phrases(self, text, expected):
        intent = RuleBasedIntent.match(text)
        assert intent is not None and intent.intent_type == expected

    def test_speed_and_sleep_timer_missing_parameters_are_explicit(self):
        speed = RuleBasedIntent.match("倍速")
        timer = RuleBasedIntent.match("设置睡眠定时")
        assert speed and speed.intent_type == "music.set_speed" and speed.params == {}
        assert timer and timer.intent_type == "music.sleep_timer_set" and timer.params == {}

    def test_sleep_timer_voice_accepts_existing_ui_options_in_chinese(self):
        for text, seconds in (("设置睡眠定时三十分钟", 1800),
                              ("设置睡眠定时一小时", 3600),
                              ("睡眠定时九十分钟", 5400)):
            intent = RuleBasedIntent.match(text)
            assert intent and intent.params["seconds"] == seconds

        for text in ("取消睡眠定时", "关闭睡眠定时"):
            intent = RuleBasedIntent.match(text)
            assert intent and intent.intent_type == "music.sleep_timer_cancel"

    def test_lol_automation_status_queries_are_read_only_intents(self):
        cases = {
            "游戏，查询自动排队状态": "queue",
            "游戏，自动同意开着吗": "accept",
            "游戏，查询自动选人状态": "banpick",
            "游戏，自动连招开了吗": "combo",
        }
        for text, feature in cases.items():
            intent = RuleBasedIntent.match(text)
            assert intent and intent.intent_type == "auto.status_query"
            assert intent.params == {"feature": feature}

    @pytest.mark.parametrize(("text", "command", "enabled"), [
        ("游戏，打开自动排队", "auto.queue_set", True),
        ("自动排队，关闭", "auto.queue_set", False),
        ("停止自动排队", "auto.queue_set", False),
        ("自动接受，打开", "auto.accept_set", True),
        ("自动接受，关闭", "auto.accept_set", False),
        ("游戏，打开自动连招", "auto.combo_set", True),
        ("自动连招，关闭", "auto.combo_set", False),
        ("游戏，打开自动选人", "auto.banpick_set", True),
        ("自动禁用，关闭", "auto.banpick_set", False),
        ("连招总开关打开", "auto.combo_set", True),
        ("打开连招总开关", "auto.combo_set", True),
        ("连招总开关关闭", "auto.combo_set", False),
        ("关闭连招总开关", "auto.combo_set", False),
    ])
    def test_lol_automation_action_object_variants(self, text, command, enabled):
        intent = RuleBasedIntent.match(text)
        assert intent and intent.intent_type == command
        assert intent.params == {"enabled": enabled}

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

    def test_tail_particle_cleaned(self):
        """句尾语气词清理：下一首啊/暂停呗/上一首呢 → 命中"""
        cases = [
            ("下一首啊", "music.next"),
            ("暂停呗", "music.pause"),
            ("上一首呢", "music.previous"),
        ]
        for text, expected in cases:
            intent = RuleBasedIntent.match(text)
            assert intent is not None, f"{text} 应命中"
            assert intent.intent_type == expected, f"{text} → {intent.intent_type}"

    def test_particle_in_word_not_cleaned(self):
        """句中字不清：阿卡丽 不应被误清成可识别指令"""
        intent = RuleBasedIntent.match("阿卡丽")
        assert intent is None

    def test_search_play_query_intact(self):
        """强反例：播放阿卡丽 → 命中 search_play，查询词完整（清尾规则不动句中字）"""
        intent = RuleBasedIntent.match("播放阿卡丽")
        assert intent is not None
        assert intent.intent_type == "music.search_play"
        assert intent.params.get("query") == "阿卡丽"

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
