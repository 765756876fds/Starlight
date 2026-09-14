"""
Intent 模块测试：规则匹配
"""
import pytest

from app.voice.intent import RuleBasedIntent, Intent


class TestRuleBasedIntent:
    """规则匹配 Intent 测试"""

    def test_next_song(self):
        intent = RuleBasedIntent.match("下一首")
        assert intent is not None
        assert intent.intent_type == "music.next"

    def test_next_song_alternate(self):
        intent = RuleBasedIntent.match("换一首")
        assert intent is not None
        assert intent.intent_type == "music.next"

    def test_prev_song(self):
        intent = RuleBasedIntent.match("上一首")
        assert intent is not None
        assert intent.intent_type == "music.prev"

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

    def test_flash_record(self):
        intent = RuleBasedIntent.match("对面莎弥拉闪现了")
        assert intent is not None
        assert intent.intent_type == "alarm.flash"
        assert "莎弥拉" in intent.params.get("champion", "")

    def test_spell_record(self):
        intent = RuleBasedIntent.match("莎弥拉W用了")
        assert intent is not None
        assert intent.intent_type == "alarm.spell"
        assert intent.params.get("spell") == "W"
        assert "莎弥拉" in intent.params.get("champion", "")

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

    def test_rule_first_not_llm(self):
        """验证规则优先，不调用 LLM"""
        intent = RuleBasedIntent.match("下一首")
        assert intent.confidence > 0.95  # 高置信度规则匹配
