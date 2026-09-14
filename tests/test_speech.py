"""
TTS 测试：Edge TTS 基础生成
"""
import os
import pytest

# 标记为慢测试，默认可以 skip
pytestmark = pytest.mark.asyncio


class TestEdgeTTS:
    """Edge TTS 测试"""

    def test_tts_module_importable(self):
        """验证 TTS 模块可导入"""
        from app.speech.tts import TTSController
        assert TTSController is not None

    @pytest.mark.skipif(
        not os.environ.get("TTS_TEST_ENABLED", ""),
        reason="TTS 测试需要网络，默认 skip（设置 TTS_TEST_ENABLED=1 运行）"
    )
    def test_generate_audio(self, tmp_path):
        """测试音频生成（需要网络）"""
        import asyncio
        import edge_tts

        output = str(tmp_path / "test_tts.mp3")

        async def generate():
            communicate = edge_tts.Communicate("测试语音", voice="zh-CN-XiaoxiaoNeural")
            await communicate.save(output)

        asyncio.run(generate())

        assert os.path.exists(output)
        assert os.path.getsize(output) > 1000  # 至少 1KB

    def test_speech_judge_exists(self):
        """验证 SpeechJudge 模块存在"""
        from app.speech.speech_judge import SpeechJudge
        assert SpeechJudge is not None
