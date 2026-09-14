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


class TestOpenAIAPIIntent:
    """OpenAIAPIIntent (LM Studio) 测试 - 使用 mock"""

    def _make_mock_intent(self, enable_no_think=True):
        """创建一个带 mock client 的 OpenAIAPIIntent"""
        from app.voice.intent import OpenAIAPIIntent
        intent = OpenAIAPIIntent(enable_no_think=enable_no_think)
        intent._loaded = True
        return intent

    def test_no_think_in_prompt(self):
        """验证 /no_think 确实存在于发送给模型的 prompt"""
        intent = self._make_mock_intent(enable_no_think=True)
        # 检查 enable_no_think 标志
        assert intent.enable_no_think is True

        # 模拟 client，检查发送的消息
        sent_messages = []

        class MockChoice:
            message = type('obj', (object,), {'content': '{"intent": "music.next", "confidence": 0.95, "params": {}}'})()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                sent_messages.extend(kwargs.get("messages", []))
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        intent.parse("下一首")

        # 检查 system message 包含 /no_think
        sys_content = sent_messages[0]["content"]
        assert "/no_think" in sys_content, "system prompt 应包含 /no_think"

    def test_no_no_think_when_disabled(self):
        """验证 enable_no_think=False 时不包含 /no_think"""
        intent = self._make_mock_intent(enable_no_think=False)
        sent_messages = []

        class MockChoice:
            message = type('obj', (object,), {'content': '{"intent": "music.next", "confidence": 0.95, "params": {}}'})()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                sent_messages.extend(kwargs.get("messages", []))
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        intent.parse("测试")

        sys_content = sent_messages[0]["content"]
        assert "/no_think" not in sys_content

    def test_valid_json_parses(self):
        """LLM 返回合法 JSON → 正确 Intent"""
        intent = self._make_mock_intent()

        class MockChoice:
            message = type('obj', (object,), {
                'content': '{"intent": "music.search_play", "confidence": 0.9, "params": {"query": "安静的音乐"}}'
            })()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        result = intent.parse("播放安静的音乐")

        assert result.intent_type == "music.search_play"
        assert result.confidence == 0.9
        assert result.params.get("query") == "安静的音乐"

    def test_invalid_json_fails_safe(self):
        """LLM 返回非法 JSON → 安全失败（system.unknown）"""
        intent = self._make_mock_intent()

        class MockChoice:
            message = type('obj', (object,), {'content': '这不是JSON'})()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        result = intent.parse("测试")

        assert result.intent_type == "system.unknown"
        assert result.confidence == 0.0

    def test_unknown_intent_blocked(self):
        """LLM 返回未知 intent → 安全拦截"""
        intent = self._make_mock_intent()

        class MockChoice:
            message = type('obj', (object,), {
                'content': '{"intent": "system.hack_computer", "confidence": 0.99, "params": {}}'
            })()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        result = intent.parse("帮我黑进电脑")

        assert result.intent_type == "system.unknown"
        assert result.confidence == 0.0

    def test_params_preserved(self):
        """验证 params 能正常保留"""
        intent = self._make_mock_intent()

        class MockChoice:
            message = type('obj', (object,), {
                'content': '{"intent": "alarm.custom", "confidence": 0.95, "params": {"time": "2分钟"}}'
            })()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        result = intent.parse("两分钟提醒我")

        assert result.intent_type == "alarm.custom"
        assert result.params.get("time") == "2分钟"

    def test_json_extraction_from_text(self):
        """LLM 返回带额外文字的 JSON → 能提取"""
        intent = self._make_mock_intent()

        class MockChoice:
            message = type('obj', (object,), {
                'content': '这是结果：{"intent": "music.next", "confidence": 0.95, "params": {}} 完成'
            })()

        class MockResponse:
            choices = [MockChoice()]
            usage = None

        class MockCompletions:
            def create(self, **kwargs):
                return MockResponse()

        class MockChat:
            completions = MockCompletions()

        class MockClient:
            chat = MockChat()

        intent._client = MockClient()
        result = intent.parse("下一首")

        assert result.intent_type == "music.next"

